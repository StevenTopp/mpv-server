import os
import json
import time
import random
import string
import hashlib
import logging
from core.config import BASE_DIR

logger = logging.getLogger("synctv_mine.users")

USERS_FILE = BASE_DIR / "data" / "users.json"

# In-memory user registry loaded from users.json
# Schema:
# {
#   "users": {
#       "username_lowercase": {
#           "username": "Username",
#           "password_hash": "...",
#           "client_id": "user_c_uuid",
#           "created_at": 1234567.0
#       }
#   },
#   "sessions": {
#       "user_c_uuid": {
#           "token": "session_token_string",
#           "expires_at": 1234567.0
#       }
#   }
# }
_state = {"users": {}, "sessions": {}}

def load_users():
    global _state
    try:
        if USERS_FILE.exists():
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    _state["users"] = data.get("users", {})
                    # Clean expired sessions at startup
                    now = time.time()
                    _state["sessions"] = {
                        cid: item
                        for cid, item in data.get("sessions", {}).items()
                        if item.get("expires_at", 0) > now
                    }
                    logger.info(f"Loaded users: {len(_state['users'])} users, {len(_state['sessions'])} sessions.")
                    return
        _state = {"users": {}, "sessions": {}}
    except Exception as e:
        logger.error(f"Failed to load users from file: {e}")
        _state = {"users": {}, "sessions": {}}

def save_users():
    try:
        USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save users to file: {e}")

# Load at startup
load_users()

def hash_password(username: str, password: str) -> str:
    # Stable salted SHA256 hashing
    salt = f"synctv_salt_{username.lower()}"
    return hashlib.sha256((password + salt).encode("utf-8")).hexdigest()

def gen_session_token() -> str:
    # 32-character secure alphanumeric token
    return "".join(random.choices(string.ascii_letters + string.digits, k=32))

def verify_token(client_id: str, token: str) -> bool:
    if not client_id or not token:
        return False
    session = _state["sessions"].get(client_id)
    if not session:
        return False
    if session.get("token") != token:
        return False
    if session.get("expires_at", 0) <= time.time():
        # Session expired
        _state["sessions"].pop(client_id, None)
        save_users()
        return False
    return True

def register_user(username: str, password: str) -> tuple[bool, str, str, str]:
    username = username.strip()
    if not username or len(username) < 3 or len(username) > 20:
        return False, "用户名长度必须在 3 到 20 个字符之间", "", ""
    if not password or len(password) < 6:
        return False, "密码长度必须至少为 6 位", "", ""
        
    username_lower = username.lower()
    if username_lower in _state["users"]:
        return False, "该用户名已被注册", "", ""
        
    # Generate stable verified client_id for this user
    from core.rooms import gen_room_id
    client_id = f"user_c_{gen_room_id(8)}"
    while any(u.get("client_id") == client_id for u in _state["users"].values()):
        client_id = f"user_c_{gen_room_id(8)}"
        
    # Save user info
    _state["users"][username_lower] = {
        "username": username,
        "password_hash": hash_password(username, password),
        "client_id": client_id,
        "created_at": time.time()
    }
    
    # Create session (valid for 30 days)
    token = gen_session_token()
    _state["sessions"][client_id] = {
        "token": token,
        "expires_at": time.time() + 30 * 86400.0
    }
    
    save_users()
    logger.info(f"Successfully registered user: {username} -> client_id: {client_id}")
    return True, "", client_id, token

def login_user(username: str, password: str) -> tuple[bool, str, str, str]:
    username = username.strip()
    username_lower = username.lower()
    user = _state["users"].get(username_lower)
    if not user:
        return False, "用户名或密码错误", "", ""
        
    expected_hash = hash_password(user["username"], password)
    if user["password_hash"] != expected_hash:
        return False, "用户名或密码错误", "", ""
        
    client_id = user["client_id"]
    
    # Create or refresh session (valid for 30 days)
    token = gen_session_token()
    _state["sessions"][client_id] = {
        "token": token,
        "expires_at": time.time() + 30 * 86400.0
    }
    
    save_users()
    logger.info(f"Successfully logged in user: {username} -> client_id: {client_id}")
    return True, "", client_id, token

def get_username_by_client_id(client_id: str) -> str:
    if not client_id:
        return "未知用户"
    for user_info in _state["users"].values():
        if user_info.get("client_id") == client_id:
            return user_info.get("username", "未知用户")
    return "未知用户"

