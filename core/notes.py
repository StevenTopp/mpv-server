import json
import time
import logging
from pathlib import Path
from typing import Any
from core.config import BASE_DIR

logger = logging.getLogger("synctv_mine.notes")

NOTES_FILE = BASE_DIR / "data" / "notes.json"

# In-memory notes state
# Schema:
# {
#   "rooms": {
#       "pair_room_id": {
#           "notes": {
#               "user_client_id_1": "content 1",
#               "user_client_id_2": "content 2"
#           },
#           "seq": 5,
#           "updated_at": 1234567.0
#       }
#   },
#   "history": {
#       "pair_room_id": [
#           {
#               "client_id": "user_client_id_1",
#               "username": "Username 1",
#               "content": "content 1",
#               "seq": 5,
#               "updated_at": 1234567.0
#           }
#       ]
#   }
# }
_state = {"rooms": {}, "history": {}}

def load_notes():
    global _state
    try:
        if NOTES_FILE.exists():
            with open(NOTES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    _state["rooms"] = data.get("rooms", {})
                    _state["history"] = data.get("history", {})
                    logger.info(f"Loaded whispers database: {len(_state['rooms'])} rooms containing whispers.")
                    return
        _state = {"rooms": {}, "history": {}}
    except Exception as e:
        logger.error(f"Failed to load whispers from file: {e}")
        _state = {"rooms": {}, "history": {}}

def save_notes():
    try:
        NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(NOTES_FILE, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save whispers to file: {e}")

# Initial load at start
load_notes()

def get_room_whispers(room_id: str, member_client_ids: list[str]) -> dict[str, Any]:
    # Stably sort member client ids to guarantee consistent display order and color assignment
    sorted_members = sorted(member_client_ids)
    
    # Retrieve or initialize room node
    room_node = _state["rooms"].setdefault(room_id, {
        "notes": {},
        "seq": 0,
        "updated_at": time.time()
    })
    
    # Ensure every bound member has a note slot
    notes_node = room_node.setdefault("notes", {})
    
    from core.users import get_username_by_client_id
    
    notes_list = []
    for idx, client_id in enumerate(sorted_members):
        username = get_username_by_client_id(client_id)
        content = notes_node.setdefault(client_id, "")
        notes_list.append({
            "client_id": client_id,
            "username": username,
            "content": content,
            "color_index": idx % 3  # Dynamically cycles 3 pastel colors (Pink, Blue, Purple)
        })
        
    return {
        "room_id": room_id,
        "notes": notes_list,
        "seq": room_node.get("seq", 0)
    }

def update_room_whisper(room_id: str, member_client_ids: list[str], client_id: str, content: str, updated_by_username: str) -> dict[str, Any]:
    # Stably sort member client ids
    sorted_members = sorted(member_client_ids)
    
    room_node = _state["rooms"].setdefault(room_id, {
        "notes": {},
        "seq": 0,
        "updated_at": time.time()
    })
    
    notes_node = room_node.setdefault("notes", {})
    
    # Save the updated note
    notes_node[client_id] = content
    
    # Increment sequence and timestamp
    seq = room_node.get("seq", 0) + 1
    room_node["seq"] = seq
    room_node["updated_at"] = time.time()
    
    # Record history log stably bound to this room
    history_node = _state["history"].setdefault(room_id, [])
    history_node.append({
        "client_id": client_id,
        "username": updated_by_username,
        "content": content,
        "seq": seq,
        "updated_at": time.time()
    })
    
    # Limit history list length to avoid massive logs (e.g. keep last 200 edits)
    if len(history_node) > 200:
        _state["history"][room_id] = history_node[-200:]
        
    save_notes()
    
    # Build list of active notes for response
    from core.users import get_username_by_client_id
    notes_list = []
    for idx, cid in enumerate(sorted_members):
        username = get_username_by_client_id(cid)
        notes_list.append({
            "client_id": cid,
            "username": username,
            "content": notes_node.setdefault(cid, ""),
            "color_index": idx % 3
        })
        
    return {
        "room_id": room_id,
        "notes": notes_list,
        "seq": seq
    }
