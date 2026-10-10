"""Bark 传输层（0.34.24 从 app.py 拆出）：纯发送，不碰数据库、不 import app。"""
import httpx


async def send_bark(burl: str, title: str, body: str, level: str = "active", site_url: str = "", path: str = "/") -> bool:
    """发一条 Bark。level：timeSensitive / active / passive。site_url 给了就带点开地址和图标。"""
    burl = (burl or "").rstrip("/")
    if not burl:
        return False
    payload = {"title": title[:60], "body": body[:300], "group": "群报"}
    if level in ("timeSensitive", "passive"):
        payload["level"] = level
    if site_url:
        site = site_url.rstrip("/")
        payload["url"] = site + path if path != "/" else site_url
        payload["icon"] = site + "/icon.png"
    try:
        async with httpx.AsyncClient(timeout=8) as cl:
            r = await cl.post(burl, json=payload)
            return r.status_code < 300
    except Exception as ex:
        print("推送失败:", ex)
        return False
