"""Bark 传输层（0.34.24 从 app.py 拆出）：纯发送，不碰数据库、不 import app。"""
import asyncio
import re

import httpx


def clip(t: str, n: int) -> str:
    """超长截断并补省略号，让人看出被截了而不是话没说完。"""
    t = t or ""
    return t if len(t) <= n else t[:n - 1] + "…"


def norm_bark_url(u: str) -> str:
    """用户常把 Bark App 里的示例整条粘进来（…/KEY/内容?group=x），只留 服务器/KEY。"""
    from urllib.parse import urlsplit
    u = (u or "").strip()
    if u and "://" not in u and "." in u.split("/")[0]:
        u = "https://" + u  # 漏写 https:// 的（api.day.app/KEY）补上
    sp = urlsplit(u)
    if not sp.scheme or not sp.netloc:
        return u.rstrip("/")
    seg = [x for x in sp.path.split("/") if x]
    return f"{sp.scheme}://{sp.netloc}" + ("/" + seg[0] if seg else "")


async def send_bark(burl: str, title: str, body: str, level: str = "active", site_url: str = "", path: str = "/", badge: int = -1, group: str = "") -> bool:
    """发一条 Bark。level：timeSensitive / active / passive。site_url 给了就带点开地址和图标。"""
    burl = norm_bark_url(burl)
    if not burl:
        return False
    md = lambda t: re.sub(r"\*\*|__|`", "", t or "")  # Bark 不渲染 Markdown，LLM 带的 ** 会原样露在锁屏上（标题正文同理）
    title = md(title).strip() or "群报"  # 空标题的推送在锁屏上像乱码，兜底成 App 名
    body = md(body)
    body = re.sub(r"\n\s*\n+", "\n", body.strip()) or title  # 空行折掉，锁屏上不浪费行数
    payload = {"title": clip(title, 60), "body": clip(body, 300), "group": clip(group, 30) or "群报", "isArchive": "1"}  # 存进 Bark 历史，手滑划掉还能回看
    if level in ("timeSensitive", "passive"):
        payload["level"] = level
    if level == "timeSensitive":
        payload["sound"] = "alarm"  # 紧急的要和普通提醒听起来不一样
        payload["volume"] = 8  # 静音键之外音量拉高（Bark 1-10），紧急事不能被忽略
        payload["call"] = "1"  # 紧急的循环响铃约 30 秒，睡着/静音时也不错过
    payload["copy"] = clip(title, 60)  # 长按/下拉复制标题；不开 autoCopy，几十条推送不该悄悄覆盖剪贴板
    if badge >= 0:
        payload["badge"] = badge  # 桌面图标角标 = 未完成待办数，和网页 App 一致
    if site_url:
        site = site_url.rstrip("/")
        payload["url"] = site + path if path != "/" else site_url
        payload["icon"] = site + "/icon.png"
    # 大陆服务器连 Bark 偶发超时：失败（网络错/5xx）自动再试两次，4xx（地址错）不重试
    for i in range(3):
        try:
            async with httpx.AsyncClient(timeout=8) as cl:
                r = await cl.post(burl, json=payload)
            if r.status_code < 300:
                return True
            print(f"Bark 返回 {r.status_code}：{r.text[:80]}")  # 写进 docker logs，地址/Key 填错一眼能看出
            if r.status_code < 500:
                return False
        except Exception as ex:
            print("推送失败:", ex)
        if i < 2:
            await asyncio.sleep(1.5 * (i + 1))
    return False
