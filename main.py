import argparse
import logging
import os
import sys
import base64
import time
import urllib.parse
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

if "--debug" in sys.argv:
    os.environ["SYNCTV_DEBUG"] = "1"

from core.config import BASE_DIR, SYNCTV_DEBUG, load_state
from core.rooms import Room, rooms, gen_room_id
from core.websocket import router as ws_router
from vendors.alist import router as alist_router
from vendors.bilibili import router as bilibili_router

# Setup Logging
logging.basicConfig(
    level=logging.DEBUG if SYNCTV_DEBUG else logging.INFO,
    format="%(asctime)s %(levelname).1s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
    force=True,
)
logger = logging.getLogger("synctv_mine")
for noisy_logger in ("httpcore", "httpx", "websockets"):
    logging.getLogger(noisy_logger).setLevel(logging.INFO)

# Initialize FastAPI App
app = FastAPI(title="SyncTV Mine")

@app.middleware("http")
async def cache_middleware(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith(".html"):
        # HTML 页面不缓存，保证每次都拿到最新版本
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    elif path in ("/hls.min.js", "/dash.all.min.js"):
        # 大型库文件内容不变，缓存 1 天（86400s），大幅减少重复访问的下载量
        response.headers["Cache-Control"] = "public, max-age=86400"
    return response

# Mount Submodule Routers
app.include_router(ws_router)
app.include_router(alist_router)
app.include_router(bilibili_router)

class SubtitleUploadReq(BaseModel):
    name: str
    content: str  # Base64 encoded subtitle string

class ClientLogReq(BaseModel):
    level: str = "info"
    message: str

@app.post("/api/upload-subtitle")
async def upload_subtitle(req: SubtitleUploadReq):
    # Ensure filename is safe (alphanumeric, dot, underscore, dash)
    safe_name = "".join(c for c in req.name if c.isalnum() or c in "._-").strip()
    if not safe_name:
        safe_name = "sub.srt"
        
    temp_dir = BASE_DIR / "static" / "temp_subs"
    temp_dir.mkdir(parents=True, exist_ok=True)
    
    # Expire old files (> 2 hours)
    try:
        now = time.time()
        for f in temp_dir.iterdir():
            if f.is_file() and now - f.stat().st_mtime > 7200:
                f.unlink()
    except Exception as e:
        logger.warning(f"Error cleaning temp subtitles: {e}")
        
    file_path = temp_dir / safe_name
    try:
        file_bytes = base64.b64decode(req.content)
        with open(file_path, "wb") as f:
            f.write(file_bytes)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"保存字幕失败: {e}")
        
    encoded_name = urllib.parse.quote(safe_name)
    return {"url": f"/temp_subs/{encoded_name}", "name": safe_name}

@app.post("/api/debug/client-log")
async def client_log(req: ClientLogReq, request: Request):
    message = req.message[:800]
    level = req.level.lower()[:16]
    logger.info(
        "client-log ip=%s ua=%s level=%s message=%s",
        request.client.host if request.client else "",
        request.headers.get("user-agent", "")[:180],
        level,
        message,
    )
    return {"ok": True}

@app.post("/api/create-room")
async def create_room():
    room_id = gen_room_id()
    while room_id in rooms:
        room_id = gen_room_id()
    rooms[room_id] = Room(room_id=room_id)
    logger.info(f"Created new room: {room_id}")
    return {"room": room_id}

@app.get("/api/vendors/status")
async def vendor_status():
    state = load_state()
    return {
        "bilibili": {"loggedIn": bool(state["bilibili"].get("cookies"))},
        "alist": [
            {
                "id": sid,
                "host": item.get("host", ""),
                "username": item.get("username", ""),
            }
            for sid, item in state.get("alist", {}).items()
        ],
    }

# Mount Frontend static files
app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True))

if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Run SyncTV Mine server")
    parser.add_argument("--debug", action="store_true", help="enable verbose sync/debug logging")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=30008)
    parser.add_argument("--reload", action="store_true", help="enable uvicorn reload")
    parser.add_argument("--no-reload", action="store_true", help="disable uvicorn reload")
    args = parser.parse_args()

    if args.debug:
        os.environ["SYNCTV_DEBUG"] = "1"
        logger.info("Debug mode enabled: verbose WebSocket sync logging is active")

    uvicorn.run(
        "main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="debug" if args.debug else "info",
    )
