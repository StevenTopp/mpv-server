import random
import string
import time
import json
from dataclasses import dataclass, field
from typing import Any
from fastapi import WebSocket
from core.config import MAX_DANMAKU_HISTORY, ROOM_MEMBER_GRACE_SECONDS, SYNCTV_DEBUG, logger

@dataclass
class Client:
    websocket: WebSocket
    user_id: str
    joined_at: float = field(default_factory=time.time)

@dataclass
class Member:
    user_id: str
    joined_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    connected: bool = True

@dataclass
class Room:
    room_id: str
    clients: dict[str, Client] = field(default_factory=dict)
    members: dict[str, Member] = field(default_factory=dict)
    ready_users: set[str] = field(default_factory=set)
    message_seq: int = 0
    last_status: dict[str, Any] = field(default_factory=dict)
    pending_seek: dict[str, Any] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=lambda: {
        "kind": "none",
        "url": "",
        "title": "",
        "subtitle": "",
        "subtitleTitle": "",
        "updatedBy": "",
        "updatedAt": 0,
    })
    danmaku: list[dict[str, Any]] = field(default_factory=list)

# Global registry of active rooms
rooms: dict[str, Room] = {}

def gen_room_id(length: int = 6) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))

def prune_room_members(room: Room, now: float | None = None) -> None:
    now = time.time() if now is None else now
    expired = [
        user_id
        for user_id, member in room.members.items()
        if not member.connected and now - member.last_seen > ROOM_MEMBER_GRACE_SECONDS
    ]
    for user_id in expired:
        room.members.pop(user_id, None)
        room.ready_users.discard(user_id)
    room.ready_users.intersection_update(room.members.keys())

def public_room(room: Room) -> dict[str, Any]:
    now = time.time()
    for user_id, client in room.clients.items():
        room.members.setdefault(
            user_id,
            Member(user_id=user_id, joined_at=client.joined_at, last_seen=now, connected=True),
        )
    prune_room_members(room, now)
    online_users = sorted(room.clients.keys())
    member_users = sorted(room.members.keys())
    away_users = sorted(user_id for user_id in member_users if user_id not in room.clients)
    return {
        "room": room.room_id,
        "count": len(online_users),
        "onlineCount": len(online_users),
        "memberCount": len(member_users),
        "onlineUsers": online_users,
        "awayUsers": away_users,
        "readyUsers": sorted(room.ready_users),
        "readyCount": len(room.ready_users),
        "source": room.source,
        "danmaku": room.danmaku[-MAX_DANMAKU_HISTORY:],
    }

def next_room_seq(room: Room) -> int:
    room.message_seq += 1
    return room.message_seq

def summarize_ws_payload(data: dict[str, Any]) -> str:
    msg_type = data.get("type", "?")
    parts = [f"type={msg_type}"]
    for key in ("from", "userId", "seekId", "reason", "playing", "time", "speed", "count", "readyCount", "serverSeq"):
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, float):
            value = round(value, 3)
        parts.append(f"{key}={value}")
    source = data.get("source")
    if isinstance(source, dict):
        title = str(source.get("title") or source.get("url") or "")[-80:]
        parts.append(f"sourceKind={source.get('kind')}")
        if title:
            parts.append(f"sourceTitle={title!r}")
    return " ".join(parts)

async def send_json(websocket: WebSocket, data: dict[str, Any]) -> None:
    await websocket.send_text(json.dumps(data, ensure_ascii=False))

async def broadcast(room: Room, data: dict[str, Any], exclude: str | None = None) -> None:
    dead: list[str] = []
    if SYNCTV_DEBUG:
        logger.debug(
            "ws broadcast room=%s recipients=%s exclude=%s %s",
            room.room_id,
            max(0, len(room.clients) - (1 if exclude in room.clients else 0)),
            exclude,
            summarize_ws_payload(data),
        )
    for user_id, client in list(room.clients.items()):
        if exclude and user_id == exclude:
            continue
        try:
            await send_json(client.websocket, data)
        except Exception:
            dead.append(user_id)

    for user_id in dead:
        room.clients.pop(user_id, None)
        member = room.members.get(user_id)
        if member:
            member.connected = False
            member.last_seen = time.time()
