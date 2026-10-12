"""Bark 传输层（0.34.24 从 app.py 拆出）：纯发送，不碰数据库、不 import app。"""
import asyncio
import html
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
        h = u.split("/")[0]
        # 漏写协议：域名补 https（api.day.app/KEY）；IP 或带端口的自建服务多半没证书，补 http
        selfhost = ":" in h or re.fullmatch(r"[\d.]+", h)
        u = ("http://" if selfhost else "https://") + u
    sp = urlsplit(u)
    if not sp.scheme or not sp.netloc:
        return u.rstrip("/")
    seg = [x for x in sp.path.split("/") if x]
    return f"{sp.scheme}://{sp.netloc}" + ("/" + seg[0] if seg else "")


def strip_md(t: str) -> str:
    """Bark 不渲染 Markdown，LLM 带的标记会原样露在锁屏上：去标题/列表(含 1.)/引用前缀、链接、粗体、代码。"""
    t = re.sub(r"[\u200b-\u200d\u2060\ufeff]", "", t or "")  # 微信/QQ 文本常夹零宽字符，锁屏上是看不见的空档，还会让取件码匹配失败
    t = html.unescape(t)  # 转发来的文本偶带 &amp; &lt; 之类，锁屏上原样露出
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)  # 转发文本里的 <br> 换成真换行，其余 <b></b> 之类标签直接去掉
    t = re.sub(r"(?i)</?(?:b|i|u|p|div|span|font|strong|em)\b[^>]*>", "", t)
    t = re.sub(r"\[CQ:(?:at|face|image|reply)[^\]]*\]", "", t)  # 残留的 QQ 码没有可读信息
    t = re.sub(r"(?m)^\s*(?:#{1,6}|[-*>]|\d{1,2}[.、)])\s+", "", t or "")
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://[\x21-\x7e]+", "[链接]", t)  # 锁屏上整串长网址没法读，折成占位；只吃 ASCII，网址后紧跟的中文不被一起吞掉
    t = re.sub(r"(\[链接\][\s,，、]*){2,}", "[链接] ", t)  # 一条消息贴多个网址只留一个占位，锁屏不被“[链接] [链接]”占行
    t = re.sub(r"(?m)^\s*(?:[-*_=]{3,}|\|?[\s:|-]{3,}\|)\s*$", "", t)  # 分隔线/表格对齐行（---、|---|）没有信息，整行去掉
    t = re.sub(r"\*\*|__|`", "", t)
    t = re.sub(r"([!！?？。.~～])\1{2,}", r"\1\1", t)  # “！！！！！！”“？？？？”刷屏标点压成两个，锁屏少占一行
    return re.sub(r"[ \u3000\t]{2,}", " ", t)  # 连续空格/全角空格折成一个，锁屏不出现一大段空白


async def send_bark(burl: str, title: str, body: str, level: str = "active", site_url: str = "", path: str = "/", badge: int = -1, group: str = "") -> bool:
    """发一条 Bark。level：timeSensitive / active / passive。site_url 给了就带点开地址和图标。"""
    burl = norm_bark_url(burl)
    if not burl:
        return False
    md = strip_md
    title = md(title).strip() or "群报"  # 空标题的推送在锁屏上像乱码，兜底成 App 名
    body = md(body)
    body = re.sub(r"\n\s*\n+", "\n", body.strip()) or title  # 空行折掉，锁屏上不浪费行数
    if body != title and body.startswith(title) and len(body) > len(title) + 2:
        body = body[len(title):].lstrip("：:，,。 \n") or body  # 正文以标题开头时去掉重复，锁屏不读两遍
    payload = {"title": clip(title, 60), "body": clip(body, 300), "group": clip(group, 30) or "群报", "isArchive": "1"}  # 存进 Bark 历史，手滑划掉还能回看
    if level in ("timeSensitive", "passive"):
        payload["level"] = level
    if level == "timeSensitive":
        payload["sound"] = "alarm"  # 紧急的要和普通提醒听起来不一样
        payload["volume"] = 8  # 静音键之外音量拉高（Bark 1-10），紧急事不能被忽略
        payload["call"] = "1"  # 紧急的循环响铃约 30 秒，睡着/静音时也不错过
    txt = title + " " + body
    code = re.search(r"(?:取件码|验证码|口令|暗号|密码|签到码)\D{0,4}([A-Za-z0-9-]{4,10})", txt) or re.search(r"(?<![\dA-Za-z])(\d{4,8})(?![\dA-Za-z])\D{0,6}(?:是|为)?(?:你的|您的)?(?:取件码|验证码|签到码)", txt)  # 码在前（“123456 为您的验证码”）也认
    payload["copy"] = code.group(1) if code else clip(title, 60)  # 长按/下拉复制：有取件码/验证码就复制码本身，否则复制标题；不开 autoCopy，不悄悄覆盖剪贴板
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
