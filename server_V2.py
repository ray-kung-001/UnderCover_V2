# server_V2.py — 多臥底版（自動投票 / 搶話提示 / 平票重講 / 起始UC= floor(n/2)-1 / 投票明細
# + 粗體輪到的人 / Host可踢人 / 可選每人發言20秒倒數
# + NEW: 每局結束彈窗揭示身份與字詞（10秒，玩家名稱粗體）、開始遊戲時顯示本局臥底人數
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Response
from fastapi.responses import HTMLResponse
import uvicorn, json, random, uuid, math, asyncio

app = FastAPI()

# ===== 內建題庫 =====
WORD_PAIRS = [
    ("可樂","汽水"),("饅頭","包子"),("籃球","排球"),("手機殼","保護套"),
    ("牛肉麵","豬肉麵"),("白開水","礦泉水"),("便當","盒飯"),("電影院","戲院"),
    ("耳機","耳麥"),("洗衣粉","洗衣精"),("地鐵","捷運"),("口罩","面罩"),
    ("拉麵","烏龍麵"),("披薩","薄餅"),("蔥油餅","蔥抓餅"),
    ("屁股","臉頰"),("小龍女","小籠包"),("西瓜","哈蜜瓜"),("水餃","鍋貼"),
    ("泡麵","麵條"),("牙刷","馬桶刷"),("燒肉","火鍋"),("統一","肉燥麵"),
    ("監獄","學校"),("榴槤","大便"),("跑步","裸奔"),("主管","渣男"),
    ("魔術師","魔法師"),("富二代","高富帥"),("情人节","光棍节"),
    ("麻婆豆腐","皮蛋豆腐"),("灰姑娘","醜小鴨"),("牛奶","豆漿"),
    ("蕾絲","絲襪"),("爺爺","外公"),("紅燒牛肉麵","清燉牛肉麵"),("老公","老公公"),
]

# ===== 房間狀態 =====
# rooms[room_id] = {
#   "host": cid,
#   "clients": { cid: {"ws":ws, "name":str, "alive":bool, "role":"civilian"|"undercover"} },
#   "status": "waiting"|"playing"|"voting"|"ended",
#   "round": int,
#   "word_pool": list[(a,b)],
#   "pair": (a,b)|None,
#   "last_pair": (a,b)|None,
#   "undercover_ids": set[cid],
#   "votes": { voter_cid: target_cid },
#   "session": int,
#   "speak_order": [cid,...],
#   "speak_index": int,
#   "spoken_this_turn": set[cid],
#   "limit_20s": bool,
#   "speak_token": str|None,
#   "timer_task": asyncio.Task|None
# }
rooms = {}

# ===== Web Pages =====
@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(HTML)

@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)

# ===== WebSocket =====
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    cid = str(uuid.uuid4())
    print(f"[ws] connected: {cid}")
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            t = msg.get("type")

            # 建房（Host）
            if t == "create_room_setup":
                room = msg["room"].strip()
                name = (msg.get("name") or "Host").strip()
                use_builtin = bool(msg.get("use_builtin", True))
                custom_list = [tuple(x) for x in (msg.get("custom_list") or []) if isinstance(x, list) and len(x)==2]
                limit_20s = bool(msg.get("limit_20s", False))

                pool = []
                if use_builtin:
                    pool.extend(WORD_PAIRS)
                pool.extend(custom_list)
                if not pool:
                    pool = list(WORD_PAIRS)

                rooms[room] = {
                    "host": cid,
                    "clients": { cid: {"ws": ws, "name": name, "alive": True, "role": "civilian"} },
                    "status": "waiting",
                    "round": 0,
                    "word_pool": pool,
                    "pair": None,
                    "last_pair": None,
                    "undercover_ids": set(),
                    "votes": {},
                    "session": 0,
                    "speak_order": [],
                    "speak_index": 0,
                    "spoken_this_turn": set(),
                    "limit_20s": limit_20s,
                    "speak_token": None,
                    "timer_task": None,
                }
                await ws.send_text(json.dumps({"type":"room_created","room":room}))
                await syslog(room, "房間已建立。")
                await broadcast_room_list(room)
                continue

            # 入房（Player）
            if t == "join_room":
                room = msg["room"].strip()
                name = (msg.get("name") or "玩家").strip()
                if room not in rooms:
                    await ws.send_text(json.dumps({"type":"error","msg":"房間不存在"}))
                    continue
                rooms[room]["clients"][cid] = {"ws": ws, "name": name, "alive": True, "role": "civilian"}
                await syslog(room, f"{name} 加入房間。")
                await broadcast_room_list(room)
                continue

            # Host 踢人
            if t == "kick":
                room = find_room(cid)
                if not room: continue
                if rooms[room]["host"] != cid:
                    continue
                target = msg.get("target")
                if not target or target not in rooms[room]["clients"]:
                    continue
                try:
                    await rooms[room]["clients"][target]["ws"].send_text(json.dumps({"type":"kicked"}))
                    await rooms[room]["clients"][target]["ws"].close()
                except:
                    pass
                rooms[room]["clients"].pop(target, None)
                await syslog(room, "已將一名玩家移出房間。")
                await broadcast_room_list(room)
                # 若踢掉當前發言者，補播提示 & 重置20秒
                if rooms[room]["status"] == "playing" and rooms[room]["speak_order"]:
                    order = rooms[room]["speak_order"]
                    if target in order:
                        order = [x for x in order if rooms[room]["clients"].get(x,{}).get("alive")]
                        rooms[room]["speak_order"] = order
                        if order:
                            if rooms[room]["speak_index"] >= len(order):
                                rooms[room]["speak_index"] %= len(order)
                            cur = order[rooms[room]["speak_index"]]
                            cur_name = rooms[room]["clients"][cur]["name"]
                            await syslog(room, f"現在輪到 <b>{cur_name}</b> 發言。", session=rooms[room]["session"])
                            await restart_speaker_timer(room)
                continue

            # 開始遊戲（Host）
            if t == "start_game":
                room = find_room(cid)
                if not room: continue
                if rooms[room]["host"] != cid: continue

                players = list(rooms[room]["clients"].keys())
                if len(players) < 3:
                    await syslog(room, "至少需要 3 名玩家才能開始。")
                    continue

                # reset
                for pcid in rooms[room]["clients"]:
                    rooms[room]["clients"][pcid]["alive"] = True
                    rooms[room]["clients"][pcid]["role"] = "civilian"
                await cancel_timer(room)
                rooms[room]["votes"] = {}
                rooms[room]["status"] = "playing"
                rooms[room]["round"] = 1
                rooms[room]["spoken_this_turn"] = set()

                # 新局（分色分區）
                rooms[room]["session"] += 1
                await broadcast(room, {"type":"chat_session","session": rooms[room]["session"]})
                await broadcast(room, {"type":"sys_session","session": rooms[room]["session"]})

                # 抽題（避免連續重複）
                pool = list(rooms[room]["word_pool"])
                last = rooms[room]["last_pair"]
                if last and len(pool) > 1:
                    pool = [p for p in pool if p != last] or list(rooms[room]["word_pool"])
                pair = random.choice(pool)
                rooms[room]["pair"] = pair
                rooms[room]["last_pair"] = pair

                # 起始臥底數：floor(n/2) - 1（>=1 且 < n）
                n = len(players)
                uc_target = max(1, min(math.floor(n/2) - 1, n-1))

                # 指派臥底 & 派字
                rooms[room]["undercover_ids"] = set(random.sample(players, uc_target))
                for pcid in players:
                    role = "undercover" if pcid in rooms[room]["undercover_ids"] else "civilian"
                    rooms[room]["clients"][pcid]["role"] = role
                    word = pair[1] if role == "undercover" else pair[0]
                    await rooms[room]["clients"][pcid]["ws"].send_text(json.dumps({
                        "type":"you_are","word":word,"alive":True,"role":role
                    }))

                # 公佈本局臥底人數（依你新需求）
                await syslog(room, f"本局臥底人數：<b>{uc_target}</b> 人。", session=rooms[room]["session"])

                # 本回合發言順序（每人一次；都講完自動投票）
                await start_new_turn(room)
                await syslog(room, "遊戲開始！第 1 回合，請依序描述。", session=rooms[room]["session"])
                await broadcast_room_list(room)
                continue

            # 開啟投票（Host 或系統自動）
            if t == "open_vote":
                room = find_room(cid)
                if not room: continue
                if rooms[room]["status"] != "playing":
                    continue
                await open_vote(room)
                continue

            # 投票（玩家）
            if t == "vote":
                room = find_room(cid)
                if not room: continue
                if rooms[room]["status"] != "voting": continue
                if not rooms[room]["clients"][cid]["alive"]:
                    await hint(room, cid, "已被淘汰，不能投票")
                    continue
                target = msg.get("target")
                if target not in rooms[room]["clients"] or not rooms[room]["clients"][target]["alive"]:
                    await hint(room, cid, "投票目標無效")
                    continue
                rooms[room]["votes"][cid] = target
                await rooms[room]["clients"][cid]["ws"].send_text(json.dumps({"type":"vote_ack"}))

                # 全部存活者都投了 -> 結算
                alive_voters = [x for x,info in rooms[room]["clients"].items() if info["alive"]]
                if all(v in rooms[room]["votes"] for v in alive_voters):
                    # 投票明細
                    vote_pairs = []
                    for voter in alive_voters:
                        v_name = rooms[room]["clients"][voter]["name"]
                        t_cid = rooms[room]["votes"].get(voter)
                        t_name = rooms[room]["clients"][t_cid]["name"] if t_cid else "(未投)"
                        vote_pairs.append({"from": v_name, "to": t_name})
                    await broadcast(room, {"type":"vote_result","pairs": vote_pairs})

                    # 計票
                    tally = {}
                    for v in rooms[room]["votes"].values():
                        tally[v] = tally.get(v, 0) + 1
                    max_votes = max(tally.values()) if tally else 0
                    top = [cid_ for cid_,cnt in tally.items() if cnt == max_votes]

                    if len(top) != 1:
                        # 平票：無人出局，留在同一回合，重新輪流發言一次
                        await syslog(room, "平票！本回合無人出局，重新輪流發言。", session=rooms[room]["session"])
                        rooms[room]["status"] = "playing"
                        rooms[room]["votes"] = {}
                        rooms[room]["spoken_this_turn"] = set()
                        await start_new_turn(room)
                        await broadcast_room_list(room)
                    else:
                        # 淘汰最高票
                        eliminated = top[0]
                        rooms[room]["clients"][eliminated]["alive"] = False
                        name = rooms[room]["clients"][eliminated]["name"]
                        await syslog(room, f"本輪淘汰：{name}", session=rooms[room]["session"])
                        await broadcast(room, {"type":"round_result","eliminated":name})
                        await rooms[room]["clients"][eliminated]["ws"].send_text(json.dumps({"type":"you_died"}))

                        # 勝負判定
                        alive_ids = [x for x,info in rooms[room]["clients"].items() if info["alive"]]
                        uc_alive = [x for x in alive_ids if x in rooms[room]["undercover_ids"]]
                        civ_alive = [x for x in alive_ids if x not in rooms[room]["undercover_ids"]]
                        if len(uc_alive) == 0:
                            rooms[room]["status"] = "ended"
                            await cancel_timer(room)
                            await syslog(room,
                                f"遊戲結束：平民勝利！本局詞語：平民「{rooms[room]['pair'][0]}」 / 臥底「{rooms[room]['pair'][1]}」。",
                                session=rooms[room]["session"])
                            # NEW: 結束彈窗揭露全部身份與詞
                            await reveal_all(room)
                            await broadcast(room, {"type":"gameover","winner":"平民"})
                        elif len(uc_alive) >= len(civ_alive):
                            rooms[room]["status"] = "ended"
                            await cancel_timer(room)
                            await syslog(room,
                                f"遊戲結束：臥底勝利！本局詞語：平民「{rooms[room]['pair'][0]}」 / 臥底「{rooms[room]['pair'][1]}」。",
                                session=rooms[room]["session"])
                            # NEW: 結束彈窗揭露全部身份與詞
                            await reveal_all(room)
                            await broadcast(room, {"type":"gameover","winner":"臥底"})
                        else:
                            # 下一回合
                            rooms[room]["status"] = "playing"
                            rooms[room]["round"] += 1
                            rooms[room]["votes"] = {}
                            rooms[room]["spoken_this_turn"] = set()
                            await start_new_turn(room)
                            await syslog(room, f"進入第 {rooms[room]['round']} 回合，請依序描述。", session=rooms[room]["session"])
                            await broadcast_room_list(room)
                continue

            # 強制下一回合（Host）
            if t == "next_round":
                room = find_room(cid)
                if not room: continue
                if rooms[room]["host"] != cid: continue
                rooms[room]["status"] = "playing"
                rooms[room]["votes"] = {}
                rooms[room]["round"] += 1
                rooms[room]["spoken_this_turn"] = set()
                await start_new_turn(room)
                await syslog(room, f"Host 已切到第 {rooms[room]['round']} 回合。", session=rooms[room]["session"])
                await broadcast_room_list(room)
                continue

            # 重置（Host）
            if t == "reset_game":
                room = find_room(cid)
                if not room: continue
                if rooms[room]["host"] != cid: continue
                rooms[room]["status"] = "waiting"
                rooms[room]["round"] = 0
                rooms[room]["pair"] = None
                rooms[room]["votes"] = {}
                rooms[room]["undercover_ids"] = set()
                rooms[room]["speak_order"] = []
                rooms[room]["speak_index"] = 0
                rooms[room]["spoken_this_turn"] = set()
                await cancel_timer(room)
                for pcid in rooms[room]["clients"]:
                    rooms[room]["clients"][pcid]["alive"] = True
                    rooms[room]["clients"][pcid]["role"] = "civilian"
                await syslog(room, "遊戲已重置；按『開始遊戲』將開啟新的一局。")
                await broadcast_room_list(room)
                continue

            # 發言
            if t == "say":
                room = find_room(cid)
                if not room: continue
                if rooms[room]["status"] not in ("playing","voting"):
                    await hint(room, cid, "目前不是發言階段")
                    continue
                if not rooms[room]["clients"][cid]["alive"]:
                    await hint(room, cid, "你已被淘汰，不能發言")
                    continue
                if rooms[room]["status"] == "voting":
                    await hint(room, cid, "目前在投票，不能發言")
                    continue
                if cid in rooms[room]["spoken_this_turn"]:
                    await hint(room, cid, "你本回合已發言")
                    continue

                # 檢查輪到誰
                order = rooms[room]["speak_order"]
                idx = rooms[room]["speak_index"]
                if not order:
                    await hint(room, cid, "尚未設定發言順序")
                    continue

                # 壓縮到存活者
                alive_set = set([x for x,info in rooms[room]["clients"].items() if info["alive"]])
                order = [x for x in order if x in alive_set]
                rooms[room]["speak_order"] = order
                if not order:
                    await hint(room, cid, "場上無人可發言")
                    continue
                idx = idx % len(order)
                rooms[room]["speak_index"] = idx
                current = order[idx]

                if cid != current:
                    name_now = rooms[room]["clients"][current]["name"]
                    await hint(room, cid, f"現在輪到 {name_now} 發言", ms=3000)
                    continue

                # 發言
                text = (msg.get("text") or "").strip()
                if text:
                    name = rooms[room]["clients"][cid]["name"]
                    await broadcast(room, {"type":"chat","from":name,"text":text,"session": rooms[room]["session"]})
                    rooms[room]["spoken_this_turn"].add(cid)

                # 指向下一位 / 或自動投票
                await advance_after_speak(room, cid)
                continue

    except WebSocketDisconnect:
        print(f"[ws] disconnected: {cid}")
        remove_client(cid)

# ===== 內部工具 =====
async def broadcast(room_id: str, payload: dict):
    data = json.dumps(payload)
    for _, info in list(rooms.get(room_id, {}).get("clients", {}).items()):
        try:
            await info["ws"].send_text(data)
        except:
            pass

async def unicast(cid: str, payload: dict):
    rid = find_room(cid)
    if not rid: return
    info = rooms[rid]["clients"].get(cid)
    if not info: return
    try:
        await info["ws"].send_text(json.dumps(payload))
    except:
        pass

async def syslog(room_id: str, text: str, session: int | None = None):
    payload = {"type":"status", "msg": text}
    if session is not None:
        payload["session"] = session
    await broadcast(room_id, payload)

async def hint(room_id: str, cid: str, text: str, ms: int = 3000):
    await unicast(cid, {"type":"hint","msg":text,"duration":ms})

async def broadcast_room_list(room_id: str):
    players = []
    r = rooms.get(room_id)
    if not r: return
    for pcid, info in r["clients"].items():
        players.append({
            "cid": pcid, "name": info["name"], "alive": info["alive"],
            "is_host": (pcid==r["host"]), "role": info.get("role","unknown")
        })
    await broadcast(room_id, {"type":"room","status":r["status"],"round":r["round"],"players":players})

def find_room(cid: str):
    for rid, room in rooms.items():
        if cid in room["clients"]:
            return rid
    return None

def remove_client(cid: str):
    rid = find_room(cid)
    if not rid: return
    room = rooms[rid]
    room["clients"].pop(cid, None)
    if not room["clients"]:
        try:
            if room.get("timer_task"):
                room["timer_task"].cancel()
        except:
            pass
        rooms.pop(rid, None)

async def start_new_turn(room_id: str):
    r = rooms[room_id]
    alive = [cid for cid, info in r["clients"].items() if info["alive"]]
    random.shuffle(alive)
    r["speak_order"] = alive
    r["speak_index"] = 0
    r["spoken_this_turn"] = set()
    if alive:
        first_name = r["clients"][alive[0]]["name"]
        await syslog(room_id, f"本回合發言順序已隨機安排。現在輪到 <b>{first_name}</b> 發言。", session=r["session"])
        await restart_speaker_timer(room_id)

async def open_vote(room_id: str):
    r = rooms[room_id]
    if r["status"] != "playing":
        return
    r["status"] = "voting"
    r["votes"] = {}
    await cancel_timer(room_id)
    alive_list = [{"cid": xcid, "name": info["name"]} for xcid, info in r["clients"].items() if info["alive"]]
    await broadcast(room_id, {"type":"chat_divider","session": r["session"]})
    await syslog(room_id, "投票開始！請選擇要淘汰的人。", session=r["session"])
    await broadcast(room_id, {"type":"voting_open","alive":alive_list})

async def advance_after_speak(room_id: str, who_cid: str):
    r = rooms[room_id]
    if r["status"] != "playing":
        return
    alive_set = set([x for x,info in r["clients"].items() if info["alive"]])
    # 全員講過？
    if alive_set.issubset(r["spoken_this_turn"]):
        await open_vote(room_id)
        return
    # 找下一位未發言者
    order = [x for x in r["speak_order"] if x in alive_set]
    if not order:
        await open_vote(room_id)
        return
    try:
        start_idx = order.index(who_cid)
    except ValueError:
        start_idx = r["speak_index"] % len(order)
    next_cid = None
    for k in range(1, len(order)+1):
        c = order[(start_idx + k) % len(order)]
        if c not in r["spoken_this_turn"]:
            next_cid = c
            r["speak_index"] = (start_idx + k) % len(order)
            break
    if next_cid is None:
        await open_vote(room_id)
        return
    next_name = r["clients"][next_cid]["name"]
    await syslog(room_id, f"現在輪到 <b>{next_name}</b> 發言。", session=r["session"])
    await restart_speaker_timer(room_id)

async def restart_speaker_timer(room_id: str):
    r = rooms[room_id]
    await cancel_timer(room_id)
    if not r.get("limit_20s"):
        return
    if r["status"] != "playing":
        return
    order = r["speak_order"]
    if not order: return
    idx = r["speak_index"] % len(order)
    current = order[idx]
    token = str(uuid.uuid4())
    r["speak_token"] = token

    async def timer():
        try:
            await asyncio.sleep(20)
            rr = rooms.get(room_id)
            if not rr or rr["status"] != "playing": return
            if rr.get("speak_token") != token: return
            rr["spoken_this_turn"].add(current)
            name = rr["clients"][current]["name"]
            await syslog(room_id, f"{name} 超過 20 秒未發言，換下一位。", session=rr["session"])
            await advance_after_speak(room_id, current)
        except asyncio.CancelledError:
            return
        except Exception:
            return

    r["timer_task"] = asyncio.create_task(timer())

async def cancel_timer(room_id: str):
    r = rooms.get(room_id)
    if not r: return
    r["speak_token"] = None
    task = r.get("timer_task")
    if task and not task.done():
        task.cancel()
        try:
            await task
        except:
            pass
    r["timer_task"] = None

async def reveal_all(room_id: str):
    """NEW: 廣播全體玩家身份與詞語，用於前端彈窗顯示 10 秒"""
    r = rooms.get(room_id)
    if not r: return
    pair = r["pair"]
    uc_ids = set(r["undercover_ids"])
    uc_count = len(uc_ids)
    detail = []
    for pcid, info in r["clients"].items():
        role = "undercover" if pcid in uc_ids else "civilian"
        word = pair[1] if role == "undercover" else pair[0]
        detail.append({
            "name": info["name"],
            "role": role,
            "word": word
        })
    payload = {
        "type": "reveal",
        "uc_count": uc_count,
        "civil_word": pair[0],
        "uc_word": pair[1],
        "players": detail
    }
    await broadcast(room_id, payload)

# ===== 前端（新增結束彈窗 reveal、開始時顯示臥底人數） =====
HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<title>誰是臥底 · 多臥底版</title>
<style>
  body{background:#fff;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,"PingFang TC","Microsoft JhengHei",Arial,sans-serif;margin:0}
  #app{max-width:960px;margin:0 auto;padding:20px}
  .hidden{display:none}
  .card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:16px;margin-bottom:16px;box-shadow:0 8px 20px rgba(0,0,0,.04)}
  h2{margin:0 0 12px}
  button{padding:8px 14px;border-radius:10px;border:1px solid #cbd5e1;background:#2563eb;color:#fff;cursor:pointer;margin:4px}
  button.ghost{background:#e2e8f0;color:#111827;border-color:#cbd5e1}
  input,select,textarea{padding:8px;border:1px solid #cbd5e1;border-radius:8px;margin:4px}
  .pill{display:inline-block;padding:4px 10px;border:1px solid #e5e7eb;border-radius:999px;margin:4px;background:#f8fafc;cursor:default}
  .pill.host{border-color:#2563eb}
  .pill.clickable{cursor:pointer}
  .danger{color:#dc2626}.muted{color:#64748b}
  .flex{display:flex;gap:12px;flex-wrap:wrap}
  .box{flex:1 1 0;min-width:300px}
  .log{max-height:260px;overflow:auto;border:1px dashed #cbd5e1;padding:10px;border-radius:10px;background:#f8fafc}
  .sec{padding:8px;border-radius:10px;margin:10px 0}
  .s1{background:#f0f9ff;border:1px solid #bae6fd}
  .s2{background:#ecfdf5;border:1px solid #bbf7d0}
  .s3{background:#fff7ed;border:1px solid #fed7aa}
  /* 3 秒提示 */
  #toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:#111827;color:#fff;padding:10px 14px;border-radius:10px;opacity:0;pointer-events:none;transition:.2s;z-index:90}
  #toast.show{opacity:0.95}

  /* Host 專用玩家選單 */
  #playerMenu{position:absolute;background:#fff;border:1px solid #cbd5e1;border-radius:8px;box-shadow:0 10px 20px rgba(0,0,0,.12);display:none;z-index:99}
  #playerMenu button{display:block;width:160px;text-align:left;background:#fff;color:#111827;border:0;border-bottom:1px solid #eef2f7;padding:8px 10px}
  #playerMenu button:last-child{border-bottom:0}
  #playerMenu button:hover{background:#f1f5f9}

  /* NEW: 結束彈窗 */
  #modalMask{position:fixed;inset:0;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;z-index:100}
  #modal{background:#fff;border-radius:14px;max-width:600px;width:92%;padding:18px;border:1px solid #e5e7eb;box-shadow:0 20px 40px rgba(0,0,0,.15)}
  #modal h3{margin:0 0 10px}
  #modal .sub{color:#64748b;margin-bottom:10px}
  #modal .row{padding:6px 0;border-bottom:1px dashed #e5e7eb}
  #modal .row:last-child{border-bottom:0}
</style>
</head>
<body>
<div id="app">
  <div id="toast"></div>

  <!-- NEW: 結束彈窗 -->
  <div id="modalMask">
    <div id="modal">
      <h3>本局結果</h3>
      <div class="sub" id="modalDesc"></div>
      <div id="modalBody"></div>
    </div>
  </div>

  <div class="card" id="screen-entry">
    <h2>誰是臥底 · 多臥底版</h2>
    <button id="goHost" type="button">我是 Host</button>
    <button id="goPlayer" type="button" class="ghost">我是 Player</button>
  </div>

  <div class="card hidden" id="screen-host">
    <h3>建立房間</h3>
    暱稱 <input id="h-name" placeholder="Host 名稱"/>
    房號 <input id="h-room" placeholder="輸入房號"/>
    <div><label><input type="checkbox" id="h-useBuiltin" checked> 包含內建清單</label></div>
    <div><label><input type="checkbox" id="h-limit20"> 是否限制發言時間為 20 秒</label></div>
    <div><textarea id="h-custom" rows="5" cols="40" placeholder="自訂題庫：每行一組，用逗號分隔（例：西瓜,哈蜜瓜）"></textarea></div>
    <div>
      <button id="h-create" type="button">建立房間並進入大廳</button>
      <button id="h-back" type="button" class="ghost">返回</button>
    </div>
  </div>

  <div class="card hidden" id="screen-join">
    <h3>加入房間</h3>
    暱稱 <input id="p-name" placeholder="你的名稱"/>
    房號 <input id="p-room" placeholder="輸入房號"/>
    <div>
      <button id="p-join" type="button">加入</button>
      <button id="p-back" type="button" class="ghost">返回</button>
    </div>
  </div>

  <div class="card hidden" id="screen-lobby">
    <div><b>狀態：</b><span id="lblStatus" class="muted">尚未開始</span>　
         <b>回合：</b><span id="lblRound">0</span></div>
    <div id="players" style="margin-top:6px; position:relative;"></div>

    <!-- Host玩家選單 -->
    <div id="playerMenu">
      <button id="btnKick">踢出該玩家</button>
      <button id="btnMenuClose">關閉</button>
    </div>

    <div id="hostPanel" class="hidden" style="margin:8px 0;">
      <button id="btnStart" type="button">開始遊戲</button>
      <button id="btnOpenVote" type="button" class="ghost">開始投票</button>
      <button id="btnNextRound" type="button" class="ghost">下一回合</button>
      <button id="btnReset" type="button" class="ghost">重新開始</button>
    </div>

    <div class="card">
      <div><b>你的詞：</b><span id="myWord" class="pill muted">尚未分配</span></div>
      <div style="margin-top:6px;">
        <input id="sayText" placeholder="說一句描述，不要暴雷～" style="width:70%;"/>
        <button id="btnSay" type="button">送出</button>
      </div>
    </div>

    <div class="flex">
      <div class="box card">
        <div class="muted">玩家聊天（依局分區）</div>
        <div id="chat" class="log"></div>
      </div>
      <div class="box card">
        <div class="muted">系統訊息（依局分區）</div>
    <div id="syslog" class="log"></div>
      </div>
    </div>

    <div class="card hidden" id="votePanel">
      <div><b>投票淘汰</b></div>
      <div>
        <select id="voteSelect"></select>
        <button id="btnVote" type="button">投票</button>
        <span id="voteInfo" class="muted"></span>
      </div>
      <div id="voteDetail" class="muted" style="margin-top:8px;"></div>
    </div>
  </div>
</div>

<script>
window.onload = function(){
  let ws=null, myName="", myRoom="", isHost=false, meAlive=true;
  let currentSession = 0;
  let toastTimer = null;

  // Host 踢人選單狀態
  let menuVisible=false, menuTargetCid=null;

  function el(id){return document.getElementById(id);}
  function show(id){ ["screen-entry","screen-host","screen-join","screen-lobby"].forEach(x=>el(x).classList.add("hidden")); el(id).classList.remove("hidden"); }
  function setControlsVisible(v){ el("hostPanel").classList.toggle("hidden", !v); }
  function setVoteVisible(v){ el("votePanel").classList.toggle("hidden", !v); }
  function colorClass(session){ return ["s1","s2","s3"][ (session-1) % 3 ]; }

  // 3 秒提示
  function toast(msg, ms=3000){
    const t = el("toast");
    t.textContent = msg;
    t.classList.add("show");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(()=>{ t.classList.remove("show"); }, ms);
  }

  // NEW: 結束彈窗（顯示 10 秒）
  let modalTimer=null;
  function showModal(descHtml, rows){
    el("modalDesc").innerHTML = descHtml;
    const body = el("modalBody");
    body.innerHTML = "";
    rows.forEach(r=>{
      const div = document.createElement("div");
      div.className="row";
      // 名稱粗體
      div.innerHTML = `<b>${r.name}</b> — ${r.role==="undercover"?"臥底":"平民"}｜詞：${r.word}`;
      body.appendChild(div);
    });
    el("modalMask").style.display="flex";
    if(modalTimer) clearTimeout(modalTimer);
    modalTimer = setTimeout(()=>{ el("modalMask").style.display="none"; }, 10000);
  }

  // 入口
  el("goHost").onclick = ()=> show("screen-host");
  el("h-back").onclick = ()=> show("screen-entry");
  el("goPlayer").onclick = ()=> show("screen-join");
  el("p-back").onclick = ()=> show("screen-entry");

  // 建房
  el("h-create").onclick = ()=>{
    myName = el("h-name").value.trim() || "Host";
    myRoom = el("h-room").value.trim() || Math.random().toString(36).slice(2,8);
    isHost = true;

    const lines = el("h-custom").value.split("\\n").map(s=>s.trim()).filter(s=>s.includes(",")).map(s=>s.split(",").map(x=>x.trim()));
    ws = new WebSocket((location.protocol==="https:"?"wss":"ws")+"://"+location.host+"/ws");
    ws.onopen = ()=>{
      ws.send(JSON.stringify({
        type:"create_room_setup",
        name:myName, room:myRoom,
        use_builtin: el("h-useBuiltin").checked,
        custom_list: lines,
        limit_20s: el("h-limit20").checked
      }));
      show("screen-lobby"); setControlsVisible(true);
    };
    ws.onmessage = onMsg;
  };

  // 入房
  el("p-join").onclick = ()=>{
    myName = el("p-name").value.trim() || "玩家";
    myRoom = el("p-room").value.trim();
    isHost = false;
    ws = new WebSocket((location.protocol==="https:"?"wss":"ws")+"://"+location.host+"/ws");
    ws.onopen = ()=>{
      ws.send(JSON.stringify({type:"join_room", name:myName, room:myRoom}));
      show("screen-lobby"); setControlsVisible(false);
    };
    ws.onmessage = onMsg;
  };

  // 控制
  el("btnStart").onclick = ()=> ws && ws.send(JSON.stringify({type:"start_game"}));
  el("btnOpenVote").onclick = ()=> ws && ws.send(JSON.stringify({type:"open_vote"}));
  el("btnNextRound").onclick = ()=> ws && ws.send(JSON.stringify({type:"next_round"}));
  el("btnReset").onclick = ()=> ws && ws.send(JSON.stringify({type:"reset_game"}));
  el("btnSay").onclick = ()=>{
    if(!meAlive){ addSys("你已被淘汰，不能發言。"); return; }
    const t = el("sayText").value.trim(); if(!t) return;
    ws.send(JSON.stringify({type:"say", text: t}));
    el("sayText").value="";
  };
  el("btnVote").onclick = ()=>{
    const target = el("voteSelect").value;
    if(target){ ws.send(JSON.stringify({type:"vote", target})); el("voteInfo").textContent="已送出投票"; }
  };

  // Host 玩家選單（踢出）
  const menu = el("playerMenu");
  el("btnMenuClose").onclick = ()=>hideMenu();
  el("btnKick").onclick = ()=>{
    if(menuTargetCid && confirm("確定要踢出該玩家嗎？")){
      ws && ws.send(JSON.stringify({type:"kick", target: menuTargetCid}));
    }
    hideMenu();
  };
  function showMenu(x,y,cid){
    menuTargetCid = cid; menu.style.left = x+"px"; menu.style.top = y+"px";
    menu.style.display = "block"; menuVisible = true;
  }
  function hideMenu(){ menu.style.display = "none"; menuVisible = false; menuTargetCid = null; }
  document.addEventListener("click", (e)=>{
    if(menuVisible && !menu.contains(e.target)) hideMenu();
  });

  // 每局分區：玩家 & 系統
  function ensureSection(rootId, session, title){
    const root = el(rootId);
    let sec = root.querySelector('[data-session="'+session+'"]');
    if(!sec){
      const wrap = document.createElement("div");
      wrap.className = "sec " + colorClass(session);
      wrap.setAttribute("data-session", session);
      const head = document.createElement("div");
      head.innerHTML = "<b>"+title+"</b>";
      head.style.marginBottom = "6px";
      wrap.appendChild(head);
      root.appendChild(wrap);
      root.scrollTop = root.scrollHeight;
      sec = wrap;
    }
    return sec;
  }
  function ensureChatSection(s){ currentSession=s; return ensureSection("chat", s, "第 "+s+" 局（玩家聊天）"); }
  function ensureSysSection(s){ return ensureSection("syslog", s, "第 "+s+" 局（系統訊息）"); }

  function addSys(msg, session=null){
    if(session){
      const sec = ensureSysSection(session);
      const line = document.createElement("div");
      line.innerHTML = msg;
      sec.appendChild(line);
      sec.scrollTop = sec.scrollHeight;
    }else{
      const base = el("syslog");
      const div = document.createElement("div");
      div.innerHTML = msg;
      base.appendChild(div);
      base.scrollTop = base.scrollHeight;
    }
  }

  function onMsg(ev){
    const m = JSON.parse(ev.data);

    if(m.type==="status"){ addSys(m.msg, m.session || null); }
    if(m.type==="hint"){ toast(m.msg, m.duration || 3000); }
    if(m.type==="error"){ addSys("<span class='danger'>"+m.msg+"</span>", m.session || null); }

    if(m.type==="room"){
      el("lblStatus") && (el("lblStatus").textContent = m.status);
      el("lblRound") && (el("lblRound").textContent = m.round);
      const box = el("players");
      box.innerHTML = "";
      (m.players || []).forEach(p=>{
        const span = document.createElement("span");
        span.className = "pill"+(p.alive?"":" danger")+(p.is_host?" host":"")+(isHost?" clickable":"");
        span.textContent = p.name + (p.is_host?"(Host)":"");
        span.dataset.cid = p.cid;
        if(isHost){
          span.onclick = (e)=>{
            const rect = box.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const y = e.clientY - rect.top;
            if(p.is_host) return;
            showMenu(x, y, p.cid);
          };
        }
        box.appendChild(span);
      });
    }

    if(m.type==="chat_session"){ ensureChatSection(m.session); }
    if(m.type==="sys_session"){ ensureSysSection(m.session); }

    if(m.type==="you_are"){
      el("myWord").textContent = m.word;
      el("myWord").classList.remove("muted");
      meAlive = true;
      el("sayText").disabled = false; el("btnSay").disabled = false;
    }

    if(m.type==="kicked"){
      alert("你已被 Host 踢出房間。");
      location.reload();
    }

    if(m.type==="you_died"){
      meAlive = false;
      el("sayText").disabled = true; el("btnSay").disabled = true;
      addSys("<span class='danger'>你已被淘汰，無法再發言。</span>", currentSession || null);
    }

    if(m.type==="chat"){
      const sec = ensureChatSection(m.session || currentSession || 1);
      const line = document.createElement("div");
      line.innerHTML = "<b>"+m.from+":</b> "+m.text;
      sec.appendChild(line);
      sec.scrollTop = sec.scrollHeight;
    }

    if(m.type==="chat_divider"){
      const sec = ensureChatSection(m.session || currentSession || 1);
      const hr = document.createElement("div");
      hr.textContent = "-------";
      hr.style.textAlign = "center";
      hr.style.color = "#64748b";
      hr.style.margin = "6px 0";
      sec.appendChild(hr);
      sec.scrollTop = sec.scrollHeight;
    }

    if(m.type==="voting_open"){
      const sel = el("voteSelect");
      sel.innerHTML = "";
      (m.alive || []).forEach(p=>{
        const opt = document.createElement("option");
        opt.value = p.cid;
        opt.textContent = p.name;
        sel.appendChild(opt);
      });
      setVoteVisible(true);
      el("voteInfo").textContent = "投票中...";
      el("voteDetail").textContent = "";
    }

    if(m.type==="vote_ack"){
      el("voteInfo").textContent = "你已投票，等待他人...";
    }

    if(m.type==="vote_result"){
      const lines = (m.pairs || []).map(p=>`${p.from} → ${p.to}`);
      el("voteDetail").textContent = lines.join("  |  ");
    }

    if(m.type==="round_result"){
      setVoteVisible(false);
      addSys("本輪淘汰：<b>"+m.eliminated+"</b>", currentSession || null);
    }

    // NEW: 結束彈窗 — 接收伺服器的 reveal
    if(m.type==="reveal"){
      // 描述：顯示臥底數、平民詞/臥底詞
      const desc = `本局臥底人數：<b>${m.uc_count}</b> 人｜平民詞：<b>${m.civil_word}</b>｜臥底詞：<b>${m.uc_word}</b>`;
      // 依序列出所有玩家（名稱粗體、角色、詞）
      const rows = (m.players || []).map(p=>({name:p.name, role:p.role, word:p.word}));
      showModal(desc, rows);
    }

    if(m.type==="gameover"){
      setVoteVisible(false);
      addSys("<b>遊戲結束！獲勝方："+m.winner+"</b>", currentSession || null);
    }

    if(m.type==="room_created"){
      addSys("房間 "+m.room+" 已建立。");
    }
  }
};
</script>
</body>
</html>
"""

if __name__ == "__main__":
    uvicorn.run("server_V2:app", host="0.0.0.0", port=8000, reload=False)
