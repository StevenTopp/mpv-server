import json
import time
import asyncio
import re
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from core.config import CLIENT_ID_ENABLED, MAX_DANMAKU_HISTORY, ROOM_MEMBER_GRACE_SECONDS, SYNCTV_DEBUG, logger
from core.rooms import (
    rooms,
    Room,
    Client,
    Member,
    gen_room_id,
    public_room,
    prune_room_members,
    send_json,
    broadcast,
    next_room_seq,
    summarize_ws_payload,
)

router = APIRouter()

CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{6,80}$")

def normalize_client_id(value: str | None) -> str:
    value = (value or "").strip()
    return value if CLIENT_ID_RE.match(value) else gen_room_id(12)

def seek_ready_users(pending: dict, room: Room) -> set[str]:
    ready_users = pending.get("readyUsers")
    if not isinstance(ready_users, set):
        ready_users = set(ready_users or [])
        pending["readyUsers"] = ready_users
    ready_users.intersection_update(room.clients.keys())
    return ready_users

def seek_not_ready_users(pending: dict, room: Room) -> list[str]:
    ready_users = seek_ready_users(pending, room)
    return sorted(uid for uid in room.clients.keys() if uid not in ready_users)

async def prune_room_after_grace(room_id: str) -> None:
    await asyncio.sleep(ROOM_MEMBER_GRACE_SECONDS + 1)
    room = rooms.get(room_id)
    if not room:
        return
    prune_room_members(room)
    if not room.clients:
        rooms.pop(room_id, None)

async def finish_seek_sync(room_id: str, seek_id: str, reason: str = "ready") -> None:
    room = rooms.get(room_id)
    if not room:
        return
    pending = room.pending_seek
    if not pending or pending.get("id") != seek_id:
        return

    ready_users = seek_ready_users(pending, room)
    target_time = float(pending.get("time") or 0)
    speed = float(pending.get("speed") or 1)
    playing = bool(pending.get("playing"))
    room.pending_seek = {}

    logger.info(
        "[seek-complete] room=%s seekId=%s reason=%s target=%.3f playing=%s speed=%.3f ready=%s/%s count=%s",
        room_id,
        seek_id,
        reason,
        target_time,
        playing,
        speed,
        len(ready_users),
        len(room.clients),
        len(room.clients),
    )
    await broadcast(
        room,
        {
            "type": "seek-sync-complete",
            "seekId": seek_id,
            "time": target_time,
            "playing": playing,
            "speed": speed,
            "reason": reason,
            "readyCount": len(ready_users),
            "count": len(room.clients),
            "serverSeq": next_room_seq(room),
            "serverTs": time.time(),
        },
    )

async def finish_seek_sync_after_timeout(room_id: str, seek_id: str, timeout_ms: int) -> None:
    await asyncio.sleep(max(0.5, timeout_ms / 1000))
    room = rooms.get(room_id)
    if not room:
        return
    pending = room.pending_seek
    if not pending or pending.get("id") != seek_id:
        return

    ready_users = seek_ready_users(pending, room)
    not_ready_users = seek_not_ready_users(pending, room)
    target_time = float(pending.get("time") or 0)
    speed = float(pending.get("speed") or 1)
    playing = bool(pending.get("playing"))
    room.pending_seek = {}

    logger.info(
        "[seek-timeout] room=%s seekId=%s target=%.3f playing=%s speed=%.3f ready=%s/%s count=%s notReady=%s",
        room_id,
        seek_id,
        target_time,
        playing,
        speed,
        len(ready_users),
        len(room.clients),
        len(room.clients),
        not_ready_users,
    )
    await broadcast(
        room,
        {
            "type": "seek-sync-timeout",
            "seekId": seek_id,
            "time": target_time,
            "playing": playing,
            "speed": speed,
            "readyCount": len(ready_users),
            "count": len(room.clients),
            "notReadyUsers": not_ready_users,
            "serverSeq": next_room_seq(room),
            "serverTs": time.time(),
        },
    )

@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    room_id = websocket.query_params.get("room", "").strip()
    if not room_id:
        await websocket.close(code=4000)
        return

    client_id_param = (websocket.query_params.get("client_id") or "").strip()
    token_param = (websocket.query_params.get("token") or "").strip()

    # 1. 拦截未授权设备进入私密对等放映厅
    if room_id.startswith("pair_"):
        from core.users import verify_token
        if not verify_token(client_id_param, token_param):
            await websocket.accept()
            logger.warning(f"WS token verification failed: client={client_id_param}")
            await websocket.close(code=4003)
            return

        from core.pairs import get_paired_room
        paired_room = get_paired_room(room_id)
        if not paired_room or client_id_param not in paired_room.get("member_client_ids", []):
            await websocket.accept()
            logger.warning(f"Unauthorized WS join blocked: room={room_id} client={client_id_param}")
            await websocket.close(code=4003)
            return

    await websocket.accept()
    room = rooms.setdefault(room_id, Room(room_id=room_id))
    prune_room_members(room)
    sticky_member = CLIENT_ID_ENABLED and bool(CLIENT_ID_RE.match(client_id_param))
    user_id = normalize_client_id(client_id_param) if sticky_member else gen_room_id()
    previous_client = room.clients.get(user_id) if sticky_member else None
    if previous_client and previous_client.websocket is not websocket:
        try:
            await previous_client.websocket.close(code=4001)
        except Exception:
            pass
    was_member = sticky_member and user_id in room.members
    member = room.members.get(user_id) or Member(user_id=user_id)
    member.connected = True
    member.last_seen = time.time()
    room.members[user_id] = member
    room.clients[user_id] = Client(websocket=websocket, user_id=user_id)
    logger.info(
        "ws join room=%s user=%s clients=%s members=%s sticky=%s reconnected=%s",
        room_id,
        user_id,
        len(room.clients),
        len(room.members),
        sticky_member,
        was_member,
    )

    welcome_payload = {
        "type": "welcome",
        "userId": user_id,
        "reconnected": was_member,
        "stickyMember": sticky_member,
        "memberGraceSeconds": ROOM_MEMBER_GRACE_SECONDS,
        "serverSeq": next_room_seq(room),
        **public_room(room),
    }

    # 动态附加专属放映厅名称与悄悄话留言
    if room_id.startswith("pair_"):
        from core.pairs import get_paired_room
        p_room = get_paired_room(room_id)
        if p_room:
            welcome_payload["roomName"] = p_room.get("room_name", "专属放映厅")
            from core.notes import get_room_whispers
            welcome_payload["wall"] = get_room_whispers(room_id, p_room.get("member_client_ids", []))

    await send_json(websocket, welcome_payload)
    await broadcast(
        room,
        {
            "type": "user-reconnect" if was_member else "user-join",
            "userId": user_id,
            "stickyMember": sticky_member,
            "serverSeq": next_room_seq(room),
            **public_room(room),
        },
        exclude=user_id,
    )

    intentional_leave = False
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                if SYNCTV_DEBUG:
                    logger.debug("ws invalid-json room=%s user=%s raw=%r", room_id, user_id, raw[:300])
                continue

            msg_type = msg.get("type")
            if SYNCTV_DEBUG:
                logger.debug("ws recv room=%s user=%s %s", room_id, user_id, summarize_ws_payload(msg))

            if msg_type == "leave":
                intentional_leave = True
                logger.info("ws explicit leave room=%s user=%s", room_id, user_id)
                try:
                    await websocket.close(code=1000)
                except Exception:
                    pass
                break

            if msg_type == "source":
                kind = str(msg.get("kind") or "remote")
                url = str(msg.get("url") or "").strip()
                title = str(msg.get("title") or "").strip()
                if kind == "remote" and not url:
                    continue
                room.ready_users.clear()
                room.source = {
                    "kind": kind,
                    "url": url,
                    "title": title or url,
                    "updatedBy": user_id,
                    "updatedAt": time.time(),
                }
                logger.info("ws source room=%s user=%s kind=%s title=%r", room_id, user_id, kind, title or url[-100:])
                await broadcast(room, {"type": "source", "from": user_id, "serverSeq": next_room_seq(room), **public_room(room)})
                continue

            if msg_type == "ready":
                room.ready_users.add(user_id)
                logger.info("ws ready room=%s user=%s ready=%s/%s", room_id, user_id, len(room.ready_users), len(room.clients))
                await broadcast(room, {"type": "ready", "from": user_id, "serverSeq": next_room_seq(room), **public_room(room)})
                continue

            if msg_type == "unready":
                room.ready_users.discard(user_id)
                logger.info("ws unready room=%s user=%s ready=%s/%s", room_id, user_id, len(room.ready_users), len(room.clients))
                await broadcast(room, {"type": "ready-state", "serverSeq": next_room_seq(room), **public_room(room)})
                continue

            if msg_type == "status":
                now = time.time()
                client_ts = msg.get("clientTs")
                try:
                    client_lag_ms = int((now - float(client_ts)) * 1000) if client_ts else None
                except (TypeError, ValueError):
                    client_lag_ms = None
                msg["from"] = user_id
                msg["serverSeq"] = next_room_seq(room)
                msg["serverTs"] = now
                room.last_status = {
                    "from": user_id,
                    "playing": bool(msg.get("playing")),
                    "time": float(msg.get("time") or 0),
                    "speed": float(msg.get("speed") or 1),
                    "reason": str(msg.get("reason") or ""),
                    "serverSeq": msg["serverSeq"],
                    "serverTs": now,
                }
                logger.info(
                    "ws status room=%s seq=%s from=%s playing=%s time=%.3f speed=%.3f reason=%s clients=%s clientLagMs=%s",
                    room_id,
                    msg["serverSeq"],
                    user_id,
                    room.last_status["playing"],
                    room.last_status["time"],
                    room.last_status["speed"],
                    room.last_status["reason"],
                    len(room.clients),
                    client_lag_ms,
                )
                if SYNCTV_DEBUG and client_lag_ms is None:
                    logger.debug(
                        "ws status missing clientTs room=%s seq=%s; frontend may be stale or cached",
                        room_id,
                        msg["serverSeq"],
                    )
                await broadcast(room, msg, exclude=user_id)
                continue

            if msg_type == "seek-sync-start":
                now = time.time()
                seek_id = str(msg.get("seekId") or gen_room_id(8))
                target_time = float(msg.get("time") or 0)
                speed = float(msg.get("speed") or 1)
                playing = bool(msg.get("playing"))
                timeout_ms = int(msg.get("timeoutMs") or 8000)
                timeout_ms = min(max(timeout_ms, 1500), 30000)
                old_pending = room.pending_seek
                if old_pending and old_pending.get("id"):
                    force_user_seek = bool(msg.get("forceUserSeek"))
                    if not force_user_seek:
                        logger.info(
                            "[seek-start-ignored] reason=pending-active oldSeekId=%s newSeekId=%s from=%s activeFrom=%s",
                            old_pending.get("id"),
                            seek_id,
                            user_id,
                            old_pending.get("from"),
                        )
                        ready_users = seek_ready_users(old_pending, room)
                        await send_json(
                            websocket,
                            {
                                "type": "seek-sync-progress",
                                "seekId": old_pending.get("id"),
                                "readyCount": len(ready_users),
                                "count": len(room.clients),
                                "serverSeq": next_room_seq(room),
                            }
                        )
                        continue

                    old_ready = seek_ready_users(old_pending, room)
                    logger.info(
                        "[seek-cancel-or-replace] room=%s oldSeekId=%s newSeekId=%s from=%s target=%.3f playing=%s speed=%.3f ready=%s/%s count=%s force=true",
                        room_id,
                        old_pending.get("id"),
                        seek_id,
                        user_id,
                        float(old_pending.get("time") or 0),
                        bool(old_pending.get("playing")),
                        float(old_pending.get("speed") or 1),
                        len(old_ready),
                        len(room.clients),
                        len(room.clients),
                    )
                ready_users = set()
                room.pending_seek = {
                    "id": seek_id,
                    "from": user_id,
                    "time": target_time,
                    "speed": speed,
                    "playing": playing,
                    "startedAt": now,
                    "timeoutMs": timeout_ms,
                    "readyUsers": ready_users,
                }
                logger.info(
                    "[seek-start] room=%s seekId=%s from=%s target=%.3f current=%.3f playing=%s speed=%.3f ready=%s/%s count=%s timeoutMs=%s",
                    room_id,
                    seek_id,
                    user_id,
                    target_time,
                    float(msg.get("currentTime") or msg.get("time") or 0),
                    playing,
                    speed,
                    len(ready_users),
                    len(room.clients),
                    len(room.clients),
                    timeout_ms,
                )
                await broadcast(
                    room,
                    {
                        "type": "seek-sync-start",
                        "seekId": seek_id,
                        "from": user_id,
                        "time": target_time,
                        "playing": playing,
                        "speed": speed,
                        "readyCount": len(ready_users),
                        "count": len(room.clients),
                        "timeoutMs": timeout_ms,
                        "serverSeq": next_room_seq(room),
                        "serverTs": now,
                    },
                )
                asyncio.create_task(finish_seek_sync_after_timeout(room_id, seek_id, timeout_ms))
                continue

            if msg_type == "seek-sync-ready":
                seek_id = str(msg.get("seekId") or "")
                pending = room.pending_seek
                if not pending or pending.get("id") != seek_id:
                    logger.info(
                        "[seek-ready] room=%s seekId=%s from=%s target=%.3f current=%.3f playing=%s speed=%.3f discarded=true reason=stale-or-missing activeSeekId=%s ready=%s/%s count=%s",
                        room_id,
                        seek_id,
                        user_id,
                        float(msg.get("targetTime") or msg.get("time") or 0),
                        float(msg.get("time") or msg.get("currentTime") or 0),
                        bool(msg.get("playing")) if "playing" in msg else bool(room.last_status.get("playing")),
                        float(msg.get("speed") or room.last_status.get("speed") or 1),
                        pending.get("id") if pending else "",
                        0,
                        len(room.clients),
                        len(room.clients),
                    )
                    continue
                ready_users = seek_ready_users(pending, room)
                ready_users.add(user_id)
                ready_users.intersection_update(room.clients.keys())
                target_time = float(pending.get("time") or 0)
                speed = float(pending.get("speed") or 1)
                playing = bool(pending.get("playing"))
                current_time = float(msg.get("time") or msg.get("currentTime") or 0)
                logger.info(
                    "[seek-ready] room=%s seekId=%s from=%s target=%.3f current=%.3f playing=%s speed=%.3f ready=%s/%s count=%s",
                    room_id,
                    seek_id,
                    user_id,
                    target_time,
                    current_time,
                    playing,
                    speed,
                    len(ready_users),
                    len(room.clients),
                    len(room.clients),
                )
                logger.info(
                    "[seek-progress] room=%s seekId=%s target=%.3f playing=%s speed=%.3f ready=%s/%s count=%s notReady=%s",
                    room_id,
                    seek_id,
                    target_time,
                    playing,
                    speed,
                    len(ready_users),
                    len(room.clients),
                    len(room.clients),
                    seek_not_ready_users(pending, room),
                )
                await broadcast(
                    room,
                    {
                        "type": "seek-sync-progress",
                        "seekId": seek_id,
                        "from": user_id,
                        "readyCount": len(ready_users),
                        "count": len(room.clients),
                        "serverSeq": next_room_seq(room),
                    },
                )
                if len(ready_users) >= len(room.clients):
                    await finish_seek_sync(room_id, seek_id, "all-ready")
                continue

            if msg_type == "danmaku":
                text = str(msg.get("text") or "").strip()
                if not text:
                    continue
                item = {
                    "id": gen_room_id(),
                    "text": text[:120],
                    "color": str(msg.get("color") or "#ffffff")[:24],
                    "from": user_id,
                    "time": time.time(),
                }
                room.danmaku.append(item)
                room.danmaku = room.danmaku[-MAX_DANMAKU_HISTORY:]
                await broadcast(room, {"type": "danmaku", "item": item, "serverSeq": next_room_seq(room)})
                continue

            if msg_type == "subtitle":
                url = str(msg.get("url") or "").strip()
                title = str(msg.get("title") or "").strip()
                room.source["subtitle"] = url
                room.source["subtitleTitle"] = title
                logger.info("ws subtitle room=%s user=%s title=%r", room_id, user_id, title or url[-100:])
                await broadcast(room, {"type": "subtitle", "url": url, "title": title, "from": user_id, "serverSeq": next_room_seq(room)})
                continue

            msg["from"] = user_id
            msg["serverSeq"] = next_room_seq(room)
            await broadcast(room, msg, exclude=user_id)

    except WebSocketDisconnect:
        pass
    finally:
        current_client = room.clients.get(user_id)
        owns_active_connection = bool(current_client and current_client.websocket is websocket)
        if owns_active_connection:
            room.clients.pop(user_id, None)

        if (intentional_leave or not sticky_member) and (owns_active_connection or current_client is None):
            room.members.pop(user_id, None)
            room.ready_users.discard(user_id)
        elif owns_active_connection or current_client is None:
            member = room.members.get(user_id)
            if member:
                member.connected = False
                member.last_seen = time.time()

        logger.info(
            "ws leave room=%s user=%s clients=%s members=%s intentional=%s",
            room_id,
            user_id,
            len(room.clients),
            len(room.members),
            intentional_leave,
        )
        pending = room.pending_seek
        if pending and pending.get("id"):
            ready_users = seek_ready_users(pending, room)
            logger.info(
                "[user-leave-during-seek] room=%s seekId=%s from=%s target=%.3f playing=%s speed=%.3f ready=%s/%s count=%s notReady=%s",
                room_id,
                pending.get("id"),
                user_id,
                float(pending.get("time") or 0),
                bool(pending.get("playing")),
                float(pending.get("speed") or 1),
                len(ready_users),
                len(room.clients),
                len(room.clients),
                seek_not_ready_users(pending, room),
            )
        should_announce_leave = owns_active_connection or current_client is None
        prune_room_members(room)
        if not room.clients:
            rooms.pop(room_id, None)
        elif should_announce_leave:
            await broadcast(
                room,
                {
                    "type": "user-away" if sticky_member and not intentional_leave else "user-leave",
                    "userId": user_id,
                    "stickyMember": sticky_member,
                    "serverSeq": next_room_seq(room),
                    **public_room(room),
                },
            )
            if sticky_member and not intentional_leave:
                asyncio.create_task(prune_room_after_grace(room_id))
            if pending and pending.get("id"):
                seek_id = pending["id"]
                ready_users = pending.get("readyUsers")
                if isinstance(ready_users, set):
                    active_ready = {uid for uid in ready_users if uid in room.clients}
                    if len(active_ready) >= len(room.clients):
                        await finish_seek_sync(room_id, seek_id, "all-ready")
