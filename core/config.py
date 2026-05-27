import json
import logging
import os
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config.yaml"
DATA_DIR = BASE_DIR / "data"
STATE_FILE = DATA_DIR / "vendor_state.json"

def _parse_config_value(value: str) -> Any:
    value = value.strip().strip('"').strip("'")
    lowered = value.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    try:
        return int(value)
    except ValueError:
        return value

def load_app_config() -> dict[str, Any]:
    config: dict[str, Any] = {}
    if not CONFIG_FILE.exists():
        return config
    try:
        for raw_line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            config[key.strip()] = _parse_config_value(value)
    except Exception:
        return {}
    return config

APP_CONFIG = load_app_config()

# Constants
SYNCTV_DEBUG = os.environ.get("SYNCTV_DEBUG", "").lower() in {"1", "true", "yes", "on"}
MAX_DANMAKU_HISTORY = 80
CLIENT_ID_ENABLED = bool(APP_CONFIG.get("client_id", False))
ROOM_MEMBER_GRACE_SECONDS = int(os.environ.get("SYNCTV_ROOM_MEMBER_GRACE_SECONDS", APP_CONFIG.get("room_member_grace_seconds", 1800)))
PROXY_SLICE_SIZE = 2 * 1024 * 1024
BILI_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

# Logger
logger = logging.getLogger("synctv_mine")

def default_state() -> dict[str, Any]:
    return {"bilibili": {"cookies": {}}, "alist": {}}

def load_state() -> dict[str, Any]:
    DATA_DIR.mkdir(exist_ok=True)
    if not STATE_FILE.exists():
        return default_state()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        base = default_state()
        base.update(data)
        base["bilibili"].setdefault("cookies", {})
        base.setdefault("alist", {})
        return base
    except Exception:
        return default_state()

def save_state(state: dict[str, Any]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def bilibili_cookies() -> dict[str, str]:
    return load_state().get("bilibili", {}).get("cookies", {})

def alist_accounts() -> dict[str, Any]:
    return load_state().get("alist", {})
