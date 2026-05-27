import html
import json
import random
import re
import string
import hashlib
import time
import urllib.parse
from typing import Any
import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from core.config import (
    load_state,
    save_state,
    bilibili_cookies,
    BILI_USER_AGENT,
    PROXY_SLICE_SIZE,
    logger,
)

router = APIRouter()

class BiliParseReq(BaseModel):
    input: str
    proxy: bool = True  # Default is True, to be backward compatible (中转模式)
    qn: int = 80        # Default is 1080p (高清)

# Global memory caches
# Cache maps mpd_id -> (created_at, play_info_dict, max_qn, proxy_bool)
bili_mpd_cache: dict[str, tuple[float, dict[str, Any], int, bool]] = {}

async def resolve_bili_url(text: str) -> str:
    text = text.strip()
    url_match = re.search(r"https?://[^\s]+", text)
    if url_match:
        url = url_match.group(0)
        if "b23.tv" in url:
            headers = {"User-Agent": BILI_USER_AGENT}
            async with httpx.AsyncClient(timeout=10, headers=headers) as client:
                try:
                    resp = await client.head(url, follow_redirects=True)
                    if resp.status_code in {404, 405}:
                        resp = await client.get(url, follow_redirects=True)
                    return str(resp.url)
                except Exception as e:
                    logger.warning(f"Failed to resolve short URL {url} locally: {e}. Trying unshorten.me fallback...")
                    try:
                        # Fallback to public unshortening API
                        api_resp = await client.get(f"https://unshorten.me/json/{url}", timeout=10)
                        data = api_resp.json()
                        if data.get("success") and data.get("resolved_url"):
                            resolved = data["resolved_url"]
                            logger.info(f"Successfully resolved {url} via fallback API to: {resolved}")
                            return resolved
                    except Exception as fallback_err:
                        logger.error(f"Fallback unshorten API failed: {fallback_err}")
            return url
        return url
    return text

def parse_bili_input(text: str) -> dict[str, str]:
    text = text.strip()
    if re.fullmatch(r"BV[a-zA-Z0-9]+", text):
        return {"type": "bvid", "id": text}

    match = re.search(r"(BV[a-zA-Z0-9]+)", text)
    if match:
        return {"type": "bvid", "id": match.group(1)}

    match = re.search(r"ep(\d+)", text)
    if match:
        return {"type": "ep", "id": match.group(1)}

    match = re.search(r"ss(\d+)", text)
    if match:
        return {"type": "ss", "id": match.group(1)}

    raise HTTPException(status_code=400, detail="请输入 Bilibili BV 号、ep/ss 番剧链接或视频链接")

def base_url(item: dict[str, Any]) -> str:
    return str(item.get("baseUrl") or item.get("base_url") or item.get("url") or "")

def progressive_candidates(play_info: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in play_info.get("durl") or [] if item.get("url")]

def play_candidates(play_info: dict[str, Any]) -> list[str]:
    return [item["url"] for item in progressive_candidates(play_info)]

def source_debug(url: str, resp: httpx.Response | None = None, prefix: bytes = b"") -> dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    return {
        "host": parsed.netloc,
        "path": parsed.path[-80:],
        "status": resp.status_code if resp else None,
        "contentType": resp.headers.get("content-type") if resp else None,
        "contentRange": resp.headers.get("content-range") if resp else None,
        "acceptRanges": resp.headers.get("accept-ranges") if resp else None,
        "contentLength": resp.headers.get("content-length") if resp else None,
        "firstBytes": prefix[:24].hex(),
    }

def segment_base(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("SegmentBase") or item.get("segment_base") or item.get("segmentBase") or {}

def bandwidth(item: dict[str, Any]) -> int:
    value = item.get("bandwidth") or item.get("band_width") or 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

def best_dash_items(
    items: list[dict[str, Any]], 
    kind: str, 
    max_qn: int | None = None, 
    supported_codecs: list[str] | None = None
) -> list[dict[str, Any]]:
    """选出最佳 DASH 流列表。

    B 站 DASH 接口的 video[].id 字段即为 qn 值（如 80=1080P, 64=720P）。
    当指定 max_qn 时，只保留 id <= max_qn 的流，从而尊重用户的清晰度选择。
    """
    available = [item for item in items if base_url(item)]
    if kind == "video":
        # 1. 按 qn (id) 过滤：只保留不超过用户期望清晰度的流
        if max_qn is not None:
            filtered = [item for item in available if int(item.get("id") or 9999) <= max_qn]
            # 如果过滤后为空（账号权限不足），退回到全部可用流（取最低清晰度避免越权）
            available = filtered if filtered else available
            
        if not available:
            return []
            
        # 2. 找出当前过滤后可用的最高画质 (max qn) 并只保留该画质 of 流，防止 ABR 降画质
        target_qn = max(int(item.get("id") or 0) for item in available)
        available = [item for item in available if int(item.get("id") or 0) == target_qn]

        # 3. 编解码器匹配辅助函数
        def get_codec_type(codec_str: str) -> str | None:
            c = codec_str.lower()
            if "av01" in c:
                return "av01"
            if "hev" in c or "hvc" in c:
                return "hev1"
            if "avc" in c:
                return "avc1"
            return None

        # 4. 根据客户端支持的编码列表过滤和排序 (若未提供，默认优先 AV1 -> HEVC -> AVC)
        if not supported_codecs:
            supported_codecs = ["av01", "hev1", "avc1"]

        def codec_priority(item: dict[str, Any]) -> int:
            ctype = get_codec_type(item.get("codecs") or "")
            if ctype in supported_codecs:
                return supported_codecs.index(ctype)
            return 9999

        # 仅保留客户端支持的流；若都没有匹配，则使用全部候选作为兜底
        supported = [item for item in available if codec_priority(item) < 9999]
        if not supported:
            supported = available

        # 排序：优先按编解码器优先级（从小到大），其次按分辨率/带宽（从大到小）
        supported.sort(key=lambda item: (
            codec_priority(item),
            -int(item.get("width") or 0),
            -int(item.get("height") or 0),
            -bandwidth(item)
        ))
    else:
        supported = [item for item in available if str(item.get("codecs") or "").lower().startswith("mp4a")] or available
        supported.sort(key=bandwidth, reverse=True)
    return supported[:1]

def dash_duration_seconds(play_info: dict[str, Any]) -> float:
    duration = play_info.get("timelength") or play_info.get("duration") or 0
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        return 0
    return duration / 1000 if duration > 10000 else duration

def put_bili_play_info(play_info: dict[str, Any], max_qn: int, proxy: bool) -> str:
    now = time.time()
    expired = [key for key, (created_at, _, _, _) in bili_mpd_cache.items() if now - created_at > 3600]
    for key in expired:
        bili_mpd_cache.pop(key, None)
    mpd_id = hashlib.sha1(f"{now}|{random.random()}".encode("utf-8")).hexdigest()[:16]
    bili_mpd_cache[mpd_id] = (now, play_info, max_qn, proxy)
    return mpd_id

def dash_debug_info(play_info: dict[str, Any], max_qn: int | None = None) -> dict[str, Any]:
    dash = play_info.get("dash") or {}
    videos = best_dash_items(dash.get("video") or [], "video", max_qn)
    audios = best_dash_items(dash.get("audio") or [], "audio")
    return {
        "video": [
            {
                "id": item.get("id"),
                "codecs": item.get("codecs"),
                "width": item.get("width"),
                "height": item.get("height"),
                "bandwidth": bandwidth(item),
            }
            for item in videos
        ],
        "audio": [
            {"id": item.get("id"), "codecs": item.get("codecs"), "bandwidth": bandwidth(item)}
            for item in audios
        ],
    }

def build_mpd(
    play_info: dict[str, Any], 
    proxy_prefix: str | None = None, 
    max_qn: int | None = None, 
    supported_codecs: list[str] | None = None
) -> str | None:
    dash = play_info.get("dash") or {}
    videos = best_dash_items(dash.get("video") or [], "video", max_qn, supported_codecs)
    audios = best_dash_items(dash.get("audio") or [], "audio")
    if not videos:
        return None

    duration = dash_duration_seconds(play_info)
    media_presentation_duration = f' mediaPresentationDuration="PT{duration:.3f}S"' if duration else ""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" minBufferTime="PT1.5S" profiles="urn:mpeg:dash:profile:isoff-on-demand:2011"{media_presentation_duration}>',
        '  <Period id="0" start="PT0S">',
    ]

    def add_adaptation(kind: str, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        content_type = "video" if kind == "video" else "audio"
        lines.append(f'    <AdaptationSet id="{kind}" contentType="{content_type}">')
        for idx, item in enumerate(items):
            raw_url = base_url(item)
            if proxy_prefix:
                url = f'{proxy_prefix}?url={urllib.parse.quote(raw_url, safe="")}'
            else:
                url = raw_url
            attrs = {
                "id": str(item.get("id") or idx),
                "bandwidth": str(bandwidth(item) or 1),
                "mimeType": str(item.get("mimeType") or item.get("mime_type") or ("video/mp4" if kind == "video" else "audio/mp4")),
            }
            codec = str(item.get("codecs") or "")
            if codec:
                attrs["codecs"] = codec
            if kind == "video":
                if item.get("width"):
                    attrs["width"] = str(item["width"])
                if item.get("height"):
                    attrs["height"] = str(item["height"])
                if item.get("frameRate") or item.get("frame_rate"):
                    attrs["frameRate"] = str(item.get("frameRate") or item.get("frame_rate"))
            attr_text = " ".join(f'{name}="{html.escape(value, quote=True)}"' for name, value in attrs.items())
            segment = segment_base(item)
            index_range = str(segment.get("indexRange") or segment.get("index_range") or "")
            init_range = str(segment.get("Initialization") or segment.get("initialization") or "")
            lines.append(f'      <Representation {attr_text}>')
            lines.append(f'        <BaseURL>{html.escape(url)}</BaseURL>')
            if index_range or init_range:
                segment_attr = f' indexRange="{html.escape(index_range, quote=True)}"' if index_range else ""
                lines.append(f'        <SegmentBase{segment_attr}>')
                if init_range:
                    lines.append(f'          <Initialization range="{html.escape(init_range, quote=True)}"/>')
                lines.append('        </SegmentBase>')
            lines.append('      </Representation>')
        lines.append('    </AdaptationSet>')

    add_adaptation("video", videos)
    add_adaptation("audio", audios)
    lines.extend(['  </Period>', '</MPD>'])
    return "\n".join(lines)

async def probe_media_url(url: str, headers: dict[str, str], cookies: dict[str, str]) -> dict[str, Any]:
    probe_headers = dict(headers)
    probe_headers["Range"] = "bytes=0-1023"
    probe_headers.setdefault("Accept-Encoding", "identity")
    try:
        async with httpx.AsyncClient(follow_redirects=True, cookies=cookies) as client:
            resp = await client.get(url, headers=probe_headers, timeout=15)
        prefix = resp.content[:64]
        return source_debug(url, resp, prefix)
    except (httpx.HTTPError, RuntimeError) as exc:
        parsed = urllib.parse.urlparse(url)
        return {"host": parsed.netloc, "path": parsed.path[-80:], "error": str(exc)}

def parse_json_response(resp: httpx.Response, upstream: str) -> dict[str, Any]:
    try:
        return resp.json()
    except ValueError as exc:
        text = resp.text[:200]
        raise HTTPException(
            status_code=502,
            detail=f"{upstream} 返回了非 JSON 响应：HTTP {resp.status_code} {text}",
        ) from exc

def clamp_range(range_header: str | None) -> str | None:
    if not range_header:
        return None
    match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header.strip())
    if not match:
        return range_header
    start = int(match.group(1))
    requested_end = int(match.group(2)) if match.group(2) else None
    if requested_end is not None:
        # 如果明确指定了结束位置，原则上不截断，以防破坏 DASH/分片视频的完整性
        # 限制最大分片大小为 50MB，避免超大范围请求内存溢出
        size = requested_end - start + 1
        if size > 50 * 1024 * 1024:
            end = start + 50 * 1024 * 1024 - 1
            return f"bytes={start}-{end}"
        return range_header
    slice_end = start + PROXY_SLICE_SIZE - 1
    return f"bytes={start}-{slice_end}"

async def proxy_buffered_range(url: str, headers: dict[str, str], request: Request) -> Response:
    range_header = request.headers.get("range")
    upstream_range = clamp_range(range_header) or f"bytes=0-{PROXY_SLICE_SIZE - 1}"
    request_headers = dict(headers)
    request_headers["Range"] = upstream_range
    request_headers.setdefault("Accept-Encoding", "identity")
    request_headers.setdefault("Connection", "close")
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            upstream = await client.get(url, headers=request_headers)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"视频上游读取失败：{exc}") from exc

    response_headers = {}
    for name in ("content-type", "content-range", "accept-ranges"):
        if name in upstream.headers:
            response_headers[name] = upstream.headers[name]
    response_headers["Content-Length"] = str(len(upstream.content))
    response_headers["Cache-Control"] = "no-store"
    logger.info(
        "buffered proxy status=%s range=%s upstream_range=%s type=%s length=%s content_range=%s body=%s url_path=%s",
        upstream.status_code,
        range_header or "",
        upstream_range,
        upstream.headers.get("content-type", ""),
        upstream.headers.get("content-length", ""),
        upstream.headers.get("content-range", ""),
        len(upstream.content),
        urllib.parse.urlparse(url).path[-80:],
    )
    return Response(content=upstream.content, status_code=upstream.status_code, headers=response_headers)

@router.post("/api/vendor/bilibili/login/qr")
async def bili_qr():
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": BILI_USER_AGENT}) as client:
        resp = await client.get("https://passport.bilibili.com/x/passport-login/web/qrcode/generate")
        data = parse_json_response(resp, "Bilibili 二维码接口")
    if data.get("code") != 0:
        raise HTTPException(status_code=502, detail=data.get("message") or "获取 Bilibili 二维码失败")
    payload = data["data"]
    return {"url": payload["url"], "key": payload["qrcode_key"]}

@router.get("/api/vendor/bilibili/login/poll")
async def bili_qr_poll(key: str):
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": BILI_USER_AGENT}) as client:
        resp = await client.get(
            "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
            params={"qrcode_key": key},
        )
        data = parse_json_response(resp, "Bilibili 登录轮询接口")
    if data.get("code") != 0:
        raise HTTPException(status_code=502, detail=data.get("message") or "轮询 Bilibili 登录失败")

    code = data.get("data", {}).get("code")
    status_map = {0: "success", 86038: "expired", 86090: "scanned", 86101: "notScanned"}
    status = status_map.get(code, "unknown")
    if status == "success":
        state = load_state()
        cookies = {cookie.name: cookie.value for cookie in resp.cookies.jar}
        state["bilibili"]["cookies"] = cookies
        save_state(state)
    return {"status": status, "message": data.get("data", {}).get("message", "")}

@router.post("/api/vendor/bilibili/logout")
async def bili_logout():
    state = load_state()
    state["bilibili"]["cookies"] = {}
    save_state(state)
    return {"ok": True}

@router.post("/api/vendor/bilibili/parse")
async def bili_parse(req: BiliParseReq):
    resolved = await resolve_bili_url(req.input)
    bili = parse_bili_input(resolved)
    cookies = bilibili_cookies()
    headers = {"User-Agent": BILI_USER_AGENT, "Referer": "https://www.bilibili.com/"}
    async with httpx.AsyncClient(timeout=20, headers=headers, cookies=cookies) as client:
        if bili["type"] == "bvid":
            bvid = bili["id"]
            view = await client.get("https://api.bilibili.com/x/web-interface/view", params={"bvid": bvid})
            view_data = parse_json_response(view, "Bilibili 视频信息接口")
            if view_data.get("code") != 0:
                raise HTTPException(status_code=502, detail=view_data.get("message") or "获取视频信息失败")
            info = view_data["data"]
            pages = info.get("pages") or []
            if not pages:
                raise HTTPException(status_code=404, detail="没有找到视频分 P")
            cid = pages[0]["cid"]
            if req.proxy:
                # 中转模式：使用 DASH 格式 (fnval=4048) 获取用户选择的分辨率
                bili_params = {"bvid": bvid, "cid": cid, "qn": req.qn, "fnval": 4048, "fourk": 1}
            else:
                # 直链模式：使用 platform=html5 (fnval=0) 获取，直链最高只支持 1080P，因此 qn 限制在 80
                direct_qn = min(req.qn, 80)
                bili_params = {"bvid": bvid, "cid": cid, "qn": direct_qn, "fnval": 0, "fourk": 1, "platform": "html5"}

            play = await client.get(
                "https://api.bilibili.com/x/player/playurl",
                params=bili_params,
            )
            play_data = parse_json_response(play, "Bilibili 视频播放接口")
            if play_data.get("code") != 0:
                raise HTTPException(status_code=502, detail=play_data.get("message") or "获取播放地址失败")
            title = info.get("title") or bvid
            play_info = play_data.get("data", {})
            candidates = play_candidates(play_info)
        else:
            season_params = {"ep_id": bili["id"]} if bili["type"] == "ep" else {"season_id": bili["id"]}
            season = await client.get("https://api.bilibili.com/pgc/view/web/season", params=season_params)
            season_data = parse_json_response(season, "Bilibili 番剧信息接口")
            if season_data.get("code") != 0:
                raise HTTPException(status_code=502, detail=season_data.get("message") or "获取番剧信息失败")

            result = season_data.get("result") or {}
            episodes = result.get("episodes") or []
            episode = None
            if bili["type"] == "ep":
                episode = next((item for item in episodes if str(item.get("ep_id") or item.get("id")) == bili["id"]), None)
            if episode is None and episodes:
                episode = episodes[0]
            if episode is None:
                raise HTTPException(status_code=404, detail="没有找到番剧分集")

            ep_id = episode.get("ep_id") or episode.get("id")
            if req.proxy:
                # 中转模式：使用 DASH 格式 (fnval=4048) 获取用户选择的分辨率
                bili_params = {"ep_id": ep_id, "qn": req.qn, "fnval": 4048, "fourk": 1}
            else:
                # 直链模式：使用 platform=html5 (fnval=0) 获取，直链最高只支持 1080P，因此 qn 限制在 80
                direct_qn = min(req.qn, 80)
                bili_params = {"ep_id": ep_id, "qn": direct_qn, "fnval": 0, "fourk": 1, "platform": "html5"}

            play = await client.get(
                "https://api.bilibili.com/pgc/player/web/playurl",
                params=bili_params,
            )
            play_data = parse_json_response(play, "Bilibili 番剧播放接口")
            if play_data.get("code") != 0:
                raise HTTPException(status_code=502, detail=play_data.get("message") or "获取番剧播放地址失败")

            play_info = play_data.get("result") or play_data.get("data") or {}
            title_parts = [result.get("title"), episode.get("long_title") or episode.get("title")]
            title = " - ".join(str(part) for part in title_parts if part)
            candidates = play_candidates(play_info)

    # Return structure based on direct link mode (req.proxy == False) or proxy mode (req.proxy == True)
    if candidates:
        probe = await probe_media_url(candidates[0], headers, cookies)
        logger.info("Bilibili progressive probe: %s", json.dumps(probe, ensure_ascii=False))
        if req.proxy:
            encoded = urllib.parse.quote(candidates[0], safe="")
            url = f"/api/proxy/bilibili?url={encoded}"
        else:
            url = candidates[0] # Direct Bilibili URL (流量不经过服务器)

        return {
            "title": title or req.input,
            "sourceType": bili["type"],
            "sourceId": bili["id"],
            "url": url,
            "rawUrl": candidates[0],
            "sourceKind": "mp4",
            "debug": {
                "format": play_info.get("format"),
                "type": play_info.get("type"),
                "quality": play_info.get("quality"),
                "candidateCount": len(candidates),
                "probe": probe,
            },
        }

    # DASH stream
    # If req.proxy is True, MPD URLs point to /api/proxy/bilibili?url=...
    # If req.proxy is False, MPD URLs point directly to the Bilibili CDN URLs
    if play_info.get("dash"):
        if not req.proxy:
            raise HTTPException(
                status_code=400,
                detail="直链模式下无法解析该视频，未返回直链（仅提供DASH格式），解析失败",
            )
        mpd_id = put_bili_play_info(play_info, req.qn, req.proxy)
        debug = dash_debug_info(play_info, max_qn=req.qn)
        # 打印默认推荐的 DASH 视频分辨率，方便调试
        if debug["video"]:
            best = debug["video"][0]
            logger.info(
                "Bilibili DASH registered: id=%s %sx%s codecs=%s bandwidth=%s qn_req=%s",
                best.get("id"), best.get("width"), best.get("height"),
                best.get("codecs"), best.get("bandwidth"), req.qn,
            )
        return {
            "title": title or req.input,
            "sourceType": bili["type"],
            "sourceId": bili["id"],
            "url": f"/api/proxy/bilibili/mpd/{mpd_id}",
            "sourceKind": "dash",
            "dash": debug,
        }

    raise HTTPException(status_code=404, detail="没有可播放地址，可能需要登录或视频受限")

@router.get("/api/proxy/bilibili/mpd/{mpd_id}")
async def bili_mpd(mpd_id: str, codecs: str | None = None):
    item = bili_mpd_cache.get(mpd_id)
    if not item:
        raise HTTPException(status_code=404, detail="Bilibili MPD 已过期，请重新解析")
    
    _, play_info, max_qn, proxy = item
    
    supported_codecs = None
    if codecs:
        supported_codecs = [c.strip().lower() for c in codecs.split(",") if c.strip()]
        
    proxy_prefix = "/api/proxy/bilibili" if proxy else None
    mpd = build_mpd(play_info, proxy_prefix, max_qn=max_qn, supported_codecs=supported_codecs)
    if not mpd:
        raise HTTPException(status_code=404, detail="无法构建 MPD 播放列表")
        
    return StreamingResponse(iter([mpd.encode("utf-8")]), media_type="application/dash+xml")

@router.get("/api/proxy/bilibili")
async def bili_proxy(url: str, request: Request):
    headers = {"User-Agent": BILI_USER_AGENT, "Referer": "https://www.bilibili.com/"}
    cookies = bilibili_cookies()
    if cookies:
        headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in cookies.items())
    return await proxy_buffered_range(url, headers, request)
