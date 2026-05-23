import hashlib
import time
import urllib.parse
from pathlib import Path
from typing import Any
import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from core.config import load_state, save_state, alist_accounts, BILI_USER_AGENT, logger

router = APIRouter()

def is_alist_auth_error(data: dict) -> bool:
    code = data.get("code")
    message = str(data.get("message", "")).lower()
    if code in {401, 403}:
        return True
    for kw in ("token", "expired", "unauthorized", "jwt", "auth", "login", "invalid"):
        if kw in message:
            return True
    return False

class AlistLoginReq(BaseModel):
    host: str
    username: str = ""
    password: str = ""
    hashedPassword: str = ""

class AlistListReq(BaseModel):
    path: str = "/"

def normalize_host(host: str) -> str:
    host = host.strip().rstrip("/")
    parsed = urllib.parse.urlparse(host)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Alist 地址必须是 http 或 https URL")
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))

def alist_server_id(host: str, username: str) -> str:
    return hashlib.sha1(f"{host}|{username}".encode("utf-8")).hexdigest()[:12]

def alist_hash_password(password: str) -> str:
    salt = "-https://github.com/alist-org/alist"
    return hashlib.sha256((password + salt).encode("utf-8")).hexdigest()

def parse_json_response(resp: httpx.Response, upstream: str) -> dict[str, Any]:
    try:
        return resp.json()
    except ValueError as exc:
        text = resp.text[:200]
        raise HTTPException(
            status_code=502,
            detail=f"{upstream} 返回了非 JSON 响应：HTTP {resp.status_code} {text}",
        ) from exc

async def proxy_stream(url: str, headers: dict[str, str], request: Request) -> StreamingResponse:
    # Retained helper for the legacy /api/proxy/alist endpoint
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header
    headers.setdefault("Accept-Encoding", "identity")
    headers.setdefault("Connection", "close")

    client = httpx.AsyncClient(follow_redirects=True, timeout=None)
    try:
        upstream = await client.stream("GET", url, headers=headers).__aenter__()
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"视频上游连接失败：{exc}") from exc

    async def body():
        try:
            async for chunk in upstream.aiter_bytes(1024 * 64):
                yield chunk
        except httpx.HTTPError as exc:
            logger.warning("proxy upstream read failed url_path=%s error=%s", urllib.parse.urlparse(url).path[-80:], exc)
            return
        finally:
            await upstream.aclose()
            await client.aclose()

    response_headers = {}
    for name in ("content-type", "content-length", "content-range", "accept-ranges"):
        if name in upstream.headers:
            response_headers[name] = upstream.headers[name]
    response_headers["Cache-Control"] = "no-store"
    return StreamingResponse(body(), status_code=upstream.status_code, headers=response_headers)

@router.post("/api/vendor/alist/login")
async def alist_login(req: AlistLoginReq):
    host = normalize_host(req.host)
    hashed = req.hashedPassword or (alist_hash_password(req.password) if req.password else "")
    if not req.username:
        raise HTTPException(status_code=400, detail="请输入 Alist 用户名")
    if not hashed:
        raise HTTPException(status_code=400, detail="请输入 Alist 密码")

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{host}/api/auth/login/hash",
            json={"username": req.username, "password": hashed},
        )
        data = parse_json_response(resp, "Alist 登录接口")
    if data.get("code") != 200:
        raise HTTPException(status_code=400, detail=data.get("message") or "Alist 登录失败")

    token = data.get("data", {}).get("token")
    if not token:
        raise HTTPException(status_code=400, detail="Alist 没有返回 token")

    sid = alist_server_id(host, req.username)
    state = load_state()
    state["alist"][sid] = {
        "host": host,
        "username": req.username,
        "hashedPassword": hashed,
        "token": token,
        "updatedAt": time.time(),
    }
    save_state(state)
    return {"id": sid, "host": host, "username": req.username}

async def refresh_alist_token(server_id: str, account: dict[str, Any]) -> str:
    host = account["host"]
    username = account["username"]
    hashed = account.get("hashedPassword")
    if not hashed:
        raise HTTPException(status_code=401, detail="Alist token 已过期且没有保存密码，请重新登录")
    
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{host}/api/auth/login/hash",
            json={"username": username, "password": hashed},
        )
        data = parse_json_response(resp, "Alist 自动登录接口")
    if data.get("code") != 200:
        raise HTTPException(status_code=400, detail=data.get("message") or "Alist 自动登录刷新失败，请重新登录")
    
    token = data.get("data", {}).get("token")
    if not token:
        raise HTTPException(status_code=400, detail="Alist 刷新 token 失败")
    
    # Save the new token in state
    state = load_state()
    if server_id in state.get("alist", {}):
        state["alist"][server_id]["token"] = token
        state["alist"][server_id]["updatedAt"] = time.time()
        save_state(state)
        # Update the local account dictionary so the caller has it immediately
        account["token"] = token
    return token

@router.post("/api/vendor/alist/{server_id}/list")
async def alist_list(server_id: str, req: AlistListReq):
    account = alist_accounts().get(server_id)
    if not account:
        raise HTTPException(status_code=404, detail="Alist 账号不存在")
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{account['host']}/api/fs/list",
            headers={"Authorization": account["token"]},
            json={"path": req.path or "/", "password": "", "page": 1, "per_page": 0, "refresh": False},
        )
        data = parse_json_response(resp, "Alist 目录接口")
        
        # If token is expired, refresh it and retry once!
        if is_alist_auth_error(data):
            logger.info("Alist token expired for account %s, attempting auto-refresh", server_id)
            new_token = await refresh_alist_token(server_id, account)
            resp = await client.post(
                f"{account['host']}/api/fs/list",
                headers={"Authorization": new_token},
                json={"path": req.path or "/", "password": "", "page": 1, "per_page": 0, "refresh": False},
            )
            data = parse_json_response(resp, "Alist 目录接口 (重试)")
            
    if data.get("code") != 200:
        raise HTTPException(status_code=400, detail=data.get("message") or "读取 Alist 目录失败")
    content = data.get("data", {}).get("content") or []
    return {
        "path": req.path or "/",
        "items": [
            {
                "name": item.get("name"),
                "isDir": item.get("is_dir", False),
                "size": item.get("size", 0),
                "modified": item.get("modified", ""),
                "sign": item.get("sign", ""),
            }
            for item in content
        ],
    }

@router.get("/api/vendor/alist/{server_id}/play")
async def alist_play(server_id: str, path: str):
    account = alist_accounts().get(server_id)
    if not account:
        raise HTTPException(status_code=404, detail="Alist 账号不存在")
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{account['host']}/api/fs/get",
            headers={"Authorization": account["token"]},
            json={"path": path, "password": ""},
        )
        data = parse_json_response(resp, "Alist 文件接口")
        
        # If token is expired, refresh it and retry once!
        if is_alist_auth_error(data):
            logger.info("Alist token expired during play for account %s, attempting auto-refresh", server_id)
            new_token = await refresh_alist_token(server_id, account)
            resp = await client.post(
                f"{account['host']}/api/fs/get",
                headers={"Authorization": new_token},
                json={"path": path, "password": ""},
            )
            data = parse_json_response(resp, "Alist 文件接口 (重试)")
            
        if data.get("code") != 200:
            raise HTTPException(status_code=400, detail=data.get("message") or "读取 Alist 文件失败")
        raw = data.get("data", {}).get("raw_url") or ""
        if not raw:
            raise HTTPException(status_code=404, detail="Alist 没有返回 raw_url")
        
        # Subtitle discovery logic
        subtitles = []
        parts = path.split("/")
        if len(parts) > 1:
            parent_path = "/".join(parts[:-1]) or "/"
            filename = parts[-1]
        else:
            parent_path = "/"
            filename = path
            
        video_stem = filename
        if "." in filename:
            video_stem = ".".join(filename.split(".")[:-1])
            
        matched_subs = []
        
        # 1. Check if Alist get response already has related items
        related = data.get("data", {}).get("related") or []
        for rel in related:
            name = rel.get("name", "")
            if rel.get("is_dir", False):
                continue
            ext = Path(name).suffix.lower()
            if ext in {".srt", ".vtt", ".ass", ".ssa"}:
                if name.lower().startswith(video_stem.lower()):
                    matched_subs.append(name)
                    
        # 2. Call fs/list on parent_path to scan the directory
        try:
            list_resp = await client.post(
                f"{account['host']}/api/fs/list",
                headers={"Authorization": account["token"]},
                json={"path": parent_path, "password": "", "page": 1, "per_page": 0, "refresh": False},
            )
            list_data = parse_json_response(list_resp, "Alist 目录接口")
            if list_data.get("code") == 200:
                content = list_data.get("data", {}).get("content") or []
                for item in content:
                    name = item.get("name", "")
                    if item.get("is_dir", False):
                        continue
                    ext = Path(name).suffix.lower()
                    if ext in {".srt", ".vtt", ".ass", ".ssa"}:
                        if name.lower().startswith(video_stem.lower()) and name not in matched_subs:
                            matched_subs.append(name)
        except Exception as e:
            logger.error(f"Error scanning parent directory {parent_path} for subtitles: {e}")
            
        # Limit to the first 3 subtitles to avoid too many API calls
        matched_subs = matched_subs[:3]
        
        # 3. Concurrently fetch raw_url for each matched subtitle
        def join_path(base: str, name: str) -> str:
            base = base.rstrip("/")
            if not base:
                return f"/{name}"
            return f"{base}/{name}"
            
        async def get_sub_url(sub_name):
            sub_path = join_path(parent_path, sub_name)
            try:
                sub_resp = await client.post(
                    f"{account['host']}/api/fs/get",
                    headers={"Authorization": account["token"]},
                    json={"path": sub_path, "password": ""},
                )
                sub_data = parse_json_response(sub_resp, f"Alist 字幕文件接口: {sub_name}")
                if sub_data.get("code") == 200:
                    sub_raw = sub_data.get("data", {}).get("raw_url")
                    if sub_raw:
                        return {"name": sub_name, "url": sub_raw}
            except Exception as ex:
                logger.error(f"Error getting raw url for subtitle {sub_name}: {ex}")
            return None
            
        if matched_subs:
            import asyncio
            sub_tasks = [get_sub_url(name) for name in matched_subs]
            sub_results = await asyncio.gather(*sub_tasks)
            subtitles = [r for r in sub_results if r is not None]
            
    # Requirement: "alist视频播放不需要经过服务器"
    # Return the raw sign URL directly so that the client plays it directly from Alist CDN.
    return {"title": Path(path).name, "url": raw, "rawUrl": raw, "subtitles": subtitles}

@router.delete("/api/vendor/alist/{server_id}")
async def alist_delete(server_id: str):
    state = load_state()
    state.get("alist", {}).pop(server_id, None)
    save_state(state)
    return {"ok": True}

@router.get("/api/proxy/alist")
async def alist_proxy(url: str, request: Request):
    return await proxy_stream(url, {"User-Agent": BILI_USER_AGENT}, request)

def srt_to_vtt(srt_content: str) -> str:
    import re
    
    def normalize_timestamp(t_str: str) -> str:
        # Matches hh:mm:ss.mmm or h:mm:ss.mmm or similar with comma or dot
        m = re.match(r"^(\d+):(\d+):(\d+)[\.,](\d+)", t_str.strip())
        if m:
            h, mins, s, ms = m.groups()
            h = h.zfill(2)
            mins = mins.zfill(2)
            s = s.zfill(2)
            ms = ms.ljust(3, "0")[:3]
            return f"{h}:{mins}:{s}.{ms}"
        return t_str.strip()

    content = srt_content.replace("\r\n", "\n").replace("\r", "\n")
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if "-->" in line:
            parts = line.split("-->")
            if len(parts) == 2:
                start_part = parts[0].strip()
                end_part = parts[1].strip()
                
                # Check for settings / position info after the end timestamp
                start_match = re.match(r"^(\d+:\d+:\d+[\.,]\d+)(.*)$", start_part)
                end_match = re.match(r"^(\d+:\d+:\d+[\.,]\d+)(.*)$", end_part)
                
                if start_match and end_match:
                    start_ts, start_extra = start_match.groups()
                    end_ts, end_extra = end_match.groups()
                    lines[i] = f"{normalize_timestamp(start_ts)}{start_extra} --> {normalize_timestamp(end_ts)}{end_extra}"
    return "WEBVTT\n\n" + "\n".join(lines)

def ass_to_vtt(ass_content: str) -> str:
    import re
    lines = ass_content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    vtt_lines = ["WEBVTT", ""]
    
    for line in lines:
        if line.startswith("Dialogue:"):
            dialogue_data = line[len("Dialogue:"):].strip()
            parts = dialogue_data.split(",", 9)
            if len(parts) >= 10:
                start_time = parts[1].strip()
                end_time = parts[2].strip()
                text = parts[9]
                
                def norm_time(t_str):
                    t_parts = t_str.split(":")
                    if len(t_parts) == 3:
                        h, m, s_ms = t_parts
                    elif len(t_parts) == 2:
                        h = "00"
                        m, s_ms = t_parts
                    else:
                        return t_str
                    
                    h = h.zfill(2)
                    m = m.zfill(2)
                    
                    if "." in s_ms:
                        s, ms = s_ms.split(".")
                        s = s.zfill(2)
                        ms = ms.ljust(3, "0")[:3]
                    else:
                        s = s_ms.zfill(2)
                        ms = "000"
                    return f"{h}:{m}:{s}.{ms}"
                
                vtt_start = norm_time(start_time)
                vtt_end = norm_time(end_time)
                
                clean_text = re.sub(r"\{[^}]*\}", "", text)
                clean_text = clean_text.replace(r"\N", "\n").replace(r"\n", "\n")
                
                vtt_lines.append(f"{vtt_start} --> {vtt_end}")
                vtt_lines.append(clean_text)
                vtt_lines.append("")
    return "\n".join(vtt_lines)

@router.get("/api/proxy/subtitle")
async def subtitle_proxy(url: str, request: Request):
    if not url:
        raise HTTPException(status_code=400, detail="Missing url parameter")
    from fastapi.responses import Response
    
    if url.startswith("//"):
        url = "https:" + url
        
    headers = {}
    ua = request.headers.get("user-agent")
    if ua:
        headers["User-Agent"] = ua
    else:
        headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        try:
            resp = await client.get(url, headers=headers)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to fetch subtitle: {e}")
            
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="Failed to fetch subtitle from upstream")
        
    content_bytes = resp.content
    content_str = ""
    for encoding in ("utf-8", "gbk", "gb18030", "utf-16", "latin-1"):
        try:
            content_str = content_bytes.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        content_str = content_bytes.decode("utf-8", errors="ignore")
        
    if content_str.startswith("\ufeff"):
        content_str = content_str[1:]
        
    # Clean up <font ...> and </font> tags case-insensitively
    import re
    content_str = re.sub(r"</?[fF][oO][nN][tT][^>]*>", "", content_str)
        
    url_path = urllib.parse.urlparse(url).path.lower()
    
    vtt_content = ""
    if url_path.endswith(".srt") or "-->" in content_str and "," in content_str:
        vtt_content = srt_to_vtt(content_str)
    elif url_path.endswith(".ass") or url_path.endswith(".ssa") or "[Script Info]" in content_str:
        vtt_content = ass_to_vtt(content_str)
    elif url_path.endswith(".vtt") or "WEBVTT" in content_str:
        vtt_content = content_str
    else:
        if "-->" in content_str:
            vtt_content = srt_to_vtt(content_str)
        else:
            vtt_content = content_str
            
    return Response(content=vtt_content, media_type="text/vtt; charset=utf-8")
