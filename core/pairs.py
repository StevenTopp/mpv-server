import os
import json
import time
import random
import string
import logging
from core.config import BASE_DIR

logger = logging.getLogger("synctv_mine.pairs")

PAIRS_FILE = BASE_DIR / "data" / "pairs.json"

# In-memory storage loaded from pairs.json
# Schema:
# {
#   "rooms": {
#       "pair_room_id": {
#           "room_id": "pair_room_id",
#           "room_name": "room_name",
#           "member_client_ids": ["c_client_id1", "c_client_id2"],
#           "created_at": 1234567.0
#       }
#   },
#   "codes": {
#       "123456": {
#           "code": "123456",
#           "room_id": "pair_room_id",
#           "expires_at": 1234567.0
#       }
#   }
# }
_state = {"rooms": {}, "codes": {}}

def load_pairs():
    global _state
    try:
        if PAIRS_FILE.exists():
            with open(PAIRS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    _state["rooms"] = data.get("rooms", {})
                    # Clean expired codes at startup
                    now = time.time()
                    _state["codes"] = {
                        code: item
                        for code, item in data.get("codes", {}).items()
                        if item.get("expires_at", 0) > now
                    }
                    logger.info(f"Loaded pairs: {len(_state['rooms'])} rooms, {len(_state['codes'])} codes.")
                    return
        # If not exists or invalid, create default state
        _state = {"rooms": {}, "codes": {}}
    except Exception as e:
        logger.error(f"Failed to load pairs from file: {e}")
        _state = {"rooms": {}, "codes": {}}

def save_pairs():
    try:
        PAIRS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(PAIRS_FILE, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save pairs to file: {e}")

# Load at startup
load_pairs()

def prune_expired_codes():
    now = time.time()
    expired = [code for code, item in _state["codes"].items() if item.get("expires_at", 0) <= now]
    if expired:
        for code in expired:
            _state["codes"].pop(code, None)
        save_pairs()

def get_paired_room(room_id: str) -> dict | None:
    return _state["rooms"].get(room_id)

def get_rooms_for_client(client_id: str) -> list[dict]:
    res = []
    for room in _state["rooms"].values():
        if client_id in room.get("member_client_ids", []):
            res.append({
                "room_id": room["room_id"],
                "room_name": room["room_name"],
                "member_count": len(room.get("member_client_ids", []))
            })
    # Sort by created_at desc if available, otherwise by name
    return sorted(res, key=lambda x: x["room_name"])

def gen_pair_code(length: int = 6) -> str:
    # 6-digit pure numeric pairing code
    return "".join(random.choices(string.digits, k=length))

def create_paired_room(client_id: str) -> tuple[str, str, str]:
    prune_expired_codes()
    # Generate unique room_id
    from core.rooms import gen_room_id
    room_id = f"pair_{gen_room_id(8)}"
    while room_id in _state["rooms"]:
        room_id = f"pair_{gen_room_id(8)}"
        
    room_name = "新专属放映厅"
    
    # Store room state
    _state["rooms"][room_id] = {
        "room_id": room_id,
        "room_name": room_name,
        "member_client_ids": [client_id],
        "created_at": time.time()
    }
    
    # Generate unique pairing code
    code = gen_pair_code()
    while code in _state["codes"]:
        code = gen_pair_code()
        
    # Store pairing code (valid for 10 minutes)
    _state["codes"][code] = {
        "code": code,
        "room_id": room_id,
        "expires_at": time.time() + 600.0
    }
    
    save_pairs()
    logger.info(f"Created paired room: {room_id} (code: {code}) for client: {client_id}")
    return room_id, room_name, code

def bind_client_to_room(code: str, client_id: str) -> tuple[bool, str, str, str]:
    prune_expired_codes()
    code = code.strip().replace(" ", "")
    code_item = _state["codes"].get(code)
    if not code_item:
        return False, "配对码无效或已过期，请让对方重新生成", "", ""
        
    room_id = code_item["room_id"]
    room = _state["rooms"].get(room_id)
    if not room:
        return False, "放映厅不存在", "", ""
        
    # Add client_id if not already in members
    if client_id not in room["member_client_ids"]:
        room["member_client_ids"].append(client_id)
        
    # Consume pairing code immediately
    _state["codes"].pop(code, None)
    
    save_pairs()
    logger.info(f"Client {client_id} successfully bound to room {room_id} via code {code}")
    return True, "", room_id, room["room_name"]

def rename_paired_room(room_id: str, new_name: str, client_id: str) -> bool:
    room = _state["rooms"].get(room_id)
    if not room:
        return False
    if client_id not in room.get("member_client_ids", []):
        return False
        
    room["room_name"] = new_name.strip()[:40] or "放映厅"
    save_pairs()
    logger.info(f"Room {room_id} renamed to {room['room_name']} by client {client_id}")
    return True

def unbind_client_from_room(room_id: str, client_id: str) -> bool:
    room = _state["rooms"].get(room_id)
    if not room:
        return False
    if client_id in room.get("member_client_ids", []):
        room["member_client_ids"].remove(client_id)
        # If no members left in the private room, purge it from the registry
        if not room["member_client_ids"]:
            _state["rooms"].pop(room_id, None)
        save_pairs()
        logger.info(f"Client {client_id} unbound from room {room_id}. Remaining members: {len(room.get('member_client_ids', [])) if room_id in _state['rooms'] else 0}")
        return True
    return False

def get_or_create_pair_code_for_room(room_id: str, client_id: str) -> str | None:
    prune_expired_codes()
    room = _state["rooms"].get(room_id)
    if not room:
        return None
    if client_id not in room.get("member_client_ids", []):
        return None
        
    # Generate unique pairing code
    code = gen_pair_code()
    while code in _state["codes"]:
        code = gen_pair_code()
        
    # Store pairing code (valid for 10 minutes)
    _state["codes"][code] = {
        "code": code,
        "room_id": room_id,
        "expires_at": time.time() + 600.0
    }
    save_pairs()
    logger.info(f"Generated invite pairing code {code} for existing room {room_id} by client {client_id}")
    return code
