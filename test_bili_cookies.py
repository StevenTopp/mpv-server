import sys
import os
import json
import httpx
import asyncio

# Add project root to sys.path
sys.path.insert(0, os.path.abspath("."))

from core.config import bilibili_cookies, BILI_USER_AGENT
from vendors.bilibili import resolve_bili_url, parse_bili_input

async def main():
    short_url = "https://b23.tv/nhEtUfu"
    cookies = bilibili_cookies()
    headers = {"User-Agent": BILI_USER_AGENT, "Referer": "https://www.bilibili.com/"}
    
    print(f"Cookies loaded: {list(cookies.keys())}")
    
    # 1. Resolve BVID
    # To bypass SSL EOF issue on b23.tv for this test, we use the known BVID
    bvid = "BV1Ns4y1P7Sg"
    print(f"BVID: {bvid}")
    
    async with httpx.AsyncClient(timeout=20, headers=headers, cookies=cookies) as client:
        # Get cid
        view = await client.get("https://api.bilibili.com/x/web-interface/view", params={"bvid": bvid})
        view_data = view.json()
        if view_data.get("code") != 0:
            print(f"Failed to get view: {view_data}")
            return
            
        cid = view_data["data"]["pages"][0]["cid"]
        title = view_data["data"]["title"]
        print(f"Title: {title}, CID: {cid}")
        
        # Test Direct Mode parameters (qn = 80, fnval = 0, platform = html5)
        direct_params = {"bvid": bvid, "cid": cid, "qn": 80, "fnval": 0, "fourk": 1, "platform": "html5"}
        play = await client.get("https://api.bilibili.com/x/player/playurl", params=direct_params)
        play_data = play.json()
        
        print("\n=== Playurl Response (Direct Mode) ===")
        print(f"Code: {play_data.get('code')}")
        print(f"Message: {play_data.get('message')}")
        
        data = play_data.get("data", {})
        print("Keys in data:", list(data.keys()))
        
        if "durl" in data:
            print("durl found!")
            for idx, item in enumerate(data["durl"]):
                print(f"durl {idx} URL: {item.get('url')[:120]}...")
        else:
            print("durl NOT found!")
            
        if "dash" in data:
            print("dash found!")
        else:
            print("dash NOT found!")

if __name__ == "__main__":
    asyncio.run(main())
