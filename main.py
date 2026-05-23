import argparse
import logging
import os
import sys
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

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
async def no_cache_for_frontend(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.endswith((".html", ".js", ".css")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# Mount Submodule Routers
app.include_router(ws_router)
app.include_router(alist_router)
app.include_router(bilibili_router)

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
