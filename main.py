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
from core.pairs import get_rooms_for_client, create_paired_room, bind_client_to_room, rename_paired_room, unbind_client_from_room, get_or_create_pair_code_for_room
from core.users import register_user, login_user, verify_token
from core.notes import get_room_whispers, update_room_whisper


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

class RoomsListReq(BaseModel):
    client_id: str
    token: str

class RoomCreateReq(BaseModel):
    client_id: str
    token: str

class RoomBindReq(BaseModel):
    client_id: str
    code: str
    token: str

class RoomRenameReq(BaseModel):
    room_id: str
    new_name: str
    client_id: str
    token: str

class RoomUnbindReq(BaseModel):
    room_id: str
    client_id: str
    token: str

class RoomCodeReq(BaseModel):
    room_id: str
    client_id: str
    token: str

class RoomWhispersGetReq(BaseModel):
    room_id: str
    client_id: str
    token: str

class RoomWhispersSaveReq(BaseModel):
    room_id: str
    client_id: str
    content: str
    token: str


class UserAuthReq(BaseModel):
    username: str
    password: str

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

@app.post("/api/rooms/list")
async def rooms_list(req: RoomsListReq):
    if not verify_token(req.client_id, req.token):
        raise HTTPException(status_code=401, detail="您的会话已过期，请重新登录")
    try:
        res = get_rooms_for_client(req.client_id)
        return {"rooms": res}
    except Exception as e:
        logger.error(f"Error listing rooms: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/rooms/create")
async def rooms_create(req: RoomCreateReq):
    if not verify_token(req.client_id, req.token):
        raise HTTPException(status_code=401, detail="您的会话已过期，请重新登录")
    try:
        room_id, room_name, code = create_paired_room(req.client_id)
        # Also initialize the Room object in the global active rooms mapping
        if room_id not in rooms:
            rooms[room_id] = Room(room_id=room_id)
        return {"room_id": room_id, "room_name": room_name, "code": code}
    except Exception as e:
        logger.error(f"Error creating paired room: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/rooms/bind")
async def rooms_bind(req: RoomBindReq):
    if not verify_token(req.client_id, req.token):
        raise HTTPException(status_code=401, detail="您的会话已过期，请重新登录")
    try:
        success, error_msg, room_id, room_name = bind_client_to_room(req.code, req.client_id)
        if not success:
            raise HTTPException(status_code=400, detail=error_msg)
        # Ensure Room object exists in active rooms
        if room_id not in rooms:
            rooms[room_id] = Room(room_id=room_id)
        return {"room_id": room_id, "room_name": room_name}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error binding room: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/rooms/rename")
async def rooms_rename(req: RoomRenameReq):
    if not verify_token(req.client_id, req.token):
        raise HTTPException(status_code=401, detail="您的会话已过期，请重新登录")
    try:
        success = rename_paired_room(req.room_id, req.new_name, req.client_id)
        if not success:
            raise HTTPException(status_code=400, detail="改名失败：未找到放映厅或您没有权限")
        
        # Broadcast the rename event to all connected websocket clients in this room
        from core.rooms import broadcast
        room = rooms.get(req.room_id)
        if room:
            await broadcast(room, {
                "type": "room-rename",
                "room_id": req.room_id,
                "room_name": req.new_name.strip()
              })
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error renaming room: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/rooms/unbind")
async def rooms_unbind_post(req: RoomUnbindReq):
    if not verify_token(req.client_id, req.token):
        raise HTTPException(status_code=401, detail="您的会话已过期，请重新登录")
    try:
        success = unbind_client_from_room(req.room_id, req.client_id)
        if not success:
            raise HTTPException(status_code=400, detail="解绑失败：放映厅不存在或您并非成员")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error unbinding room: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/rooms/code")
async def rooms_code(req: RoomCodeReq):
    if not verify_token(req.client_id, req.token):
        raise HTTPException(status_code=401, detail="您的会话已过期，请重新登录")
    try:
        code = get_or_create_pair_code_for_room(req.room_id, req.client_id)
        if not code:
            raise HTTPException(status_code=400, detail="获取配对码失败：放映厅不存在或您没有权限")
        return {"code": code}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating invite code: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/rooms/wall/get")
async def api_get_room_wall(req: RoomWhispersGetReq):
    if not verify_token(req.client_id, req.token):
        raise HTTPException(status_code=401, detail="您的会话已过期，请重新登录")
    try:
        from core.pairs import get_paired_room
        p_room = get_paired_room(req.room_id)
        if not p_room or req.client_id not in p_room.get("member_client_ids", []):
            raise HTTPException(status_code=403, detail="您并非该专属放映厅成员")
            
        data = get_room_whispers(req.room_id, p_room.get("member_client_ids", []))
        return data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting whispers wall: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/rooms/wall/save")
async def api_save_room_wall(req: RoomWhispersSaveReq):
    if not verify_token(req.client_id, req.token):
        raise HTTPException(status_code=401, detail="您的会话已过期，请重新登录")
    try:
        from core.pairs import get_paired_room
        p_room = get_paired_room(req.room_id)
        if not p_room or req.client_id not in p_room.get("member_client_ids", []):
            raise HTTPException(status_code=403, detail="您并非该专属放映厅成员")
            
        from core.users import get_username_by_client_id
        username = get_username_by_client_id(req.client_id)
        
        # Save note updates
        data = update_room_whisper(
            room_id=req.room_id,
            member_client_ids=p_room.get("member_client_ids", []),
            client_id=req.client_id,
            content=req.content,
            updated_by_username=username
        )
        
        # Broadcast the updated whispers board to all WebSocket clients connected to the room
        from core.rooms import broadcast
        room = rooms.get(req.room_id)
        if room:
            await broadcast(room, {
                "type": "wall",
                "room": req.room_id,
                "wall": data
            })
            
        return {"success": True, "wall": data}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving whispers: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/user/register")
async def user_register(req: UserAuthReq):
    try:
        success, error_msg, client_id, token = register_user(req.username, req.password)
        if not success:
            raise HTTPException(status_code=400, detail=error_msg)
        return {"success": True, "client_id": client_id, "token": token, "username": req.username}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error registering user: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/user/login")
async def user_login(req: UserAuthReq):
    try:
        success, error_msg, client_id, token = login_user(req.username, req.password)
        if not success:
            raise HTTPException(status_code=400, detail=error_msg)
        return {"success": True, "client_id": client_id, "token": token, "username": req.username}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error logging in user: {e}")
        raise HTTPException(status_code=500, detail=str(e))

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
