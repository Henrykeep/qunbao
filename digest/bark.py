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


CQ_LABEL = {"record": "[语音]", "video": "[视频]", "forward": "[卡片]", "json": "[卡片]", "xml": "[卡片]",
            "file": "[文件]", "music": "[音乐]", "share": "[链接]", "dice": "[骰子]", "rps": "[猜拳]", "image": "[图片]", "face": "[表情]", "mface": "[表情]", "bface": "[表情]"}


def strip_md(t: str) -> str:
    """Bark 不渲染 Markdown，LLM 带的标记会原样露在锁屏上：去标题/列表(含 1.)/引用前缀、链接、粗体、代码。"""
    t = re.sub(r"[\u200b-\u200d\u2060\ufeff]", "", t or "")  # 微信/QQ 文本常夹零宽字符，锁屏上是看不见的空档，还会让取件码匹配失败
    t = (t or "").replace("\r\n", "\n").replace("\r", "\n")  # Windows/微信换行统一，免得重复行折叠和列表符匹配被 \r 挡住
    t = html.unescape(t)  # 转发来的文本偶带 &amp; &lt; 之类，锁屏上原样露出
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)  # 转发文本里的 <br> 换成真换行，其余 <b></b> 之类标签直接去掉
    t = re.sub(r"(?i)</?(?:b|i|u|p|div|span|font|strong|em)\b[^>]*>", "", t)
    t = re.sub(r"\[CQ:(\w+)[^\]]*\]", lambda m: CQ_LABEL.get(m.group(1), ""), t)  # QQ 码统一一张表：有可读含义的留占位，其余去掉
    t = re.sub(r"(?s)^\s*「[^」]*」\s*\n\s*(?:-\s*){6,}\n?", "", t)  # 微信引用回复自带“「昵称：原话」+ 虚线”，锁屏只留回复本身
    t = re.sub(r"(?m)^\s*(?:#{1,6}|[-*>]|\d{1,2}[.、)])\s+", "", t or "")
    t = re.sub(r"(?m)^\s*[①-⑩⓵-⓾•·▪●○◆◇■□▶►➤➡✔✅☑]\s*", "", t)  # 圆圈数字/圆点/箭头/对勾列表符在锁屏上白占位
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://[\x21-\x7e]+", "[链接]", t)  # 锁屏上整串长网址没法读，折成占位；只吃 ASCII，网址后紧跟的中文不被一起吞掉
    t = re.sub(r"(?<![\w.@/])www\.[\x21-\x7e]+", "[链接]", t)  # 没带 http 的 www 网址同样折成占位（邮箱/已折叠的不动）
    t = re.sub(r"(\[链接\][\s,，、]*){2,}", "[链接] ", t)  # 一条消息贴多个网址只留一个占位，锁屏不被“[链接] [链接]”占行
    t = re.sub(r"(?m)^\s*(?:[-*_=]{3,}|\|?[\s:|-]{3,}\|)\s*$", "", t)  # 分隔线/表格对齐行（---、|---|）没有信息，整行去掉
    t = re.sub(r"\*\*|__|~~|`", "", t)
    t = re.sub(r"(?<![\w*])\*([^\s*][^*\n]*?)\*(?![\w*])", r"\1", t)  # 斜体 *x*（中文乘号/通配符 a*b 不动）  # 粗体/删除线/代码标记
    t = re.sub(r"([!！?？。.~～])\1{2,}", r"\1\1", t)  # “！！！！！！”“？？？？”刷屏标点压成两个，锁屏少占一行
    t = re.sub(r"([哈啊嘿呵])\1{3,}", r"\1\1\1", t)  # “哈哈哈哈哈哈”“啊啊啊啊啊”压成三个字
    t = re.sub(r"([\U0001F300-\U0001FAFF\u2600-\u27BF])\1{2,}", r"\1\1", t)  # 😂😂😂😂 刷屏表情压成两个
    t = re.sub(r"(?:…|\.){4,}", "……", t)  # 省略号刷屏折成一个
    t = re.sub(r"\[(?:微笑|撇嘴|色|发呆|得意|流泪|害羞|闭嘴|睡|大哭|尴尬|发怒|调皮|呲牙|惊讶|难过|酷|冷汗|抓狂|吐|偷笑|愉快|白眼|傲慢|困|惊恐|流汗|憨笑|悠闲|奋斗|咒骂|疑问|嘘|晕|衰|骷髅|敲打|再见|擦汗|抠鼻|鼓掌|坏笑|左哼哼|右哼哼|哈欠|鄙视|委屈|快哭了|阴险|亲亲|可怜|笑脸|生病|脸红|破涕为笑|恐惧|失望|无语|嘿哈|捂脸|奸笑|机智|皱眉|耶|吃瓜|加油|汗|天啊|Emm|社会社会|旺柴|好的|打脸|哇|翻白眼|裂开|苦涩|让我看看|叹气|合十|强|握手|胜利|抱拳|OK|拳头|爱心|心碎|玫瑰|凋谢|赞|踩)\]", "[表情]", t)  # 微信文字表情 [捂脸] 折成统一占位，下一行再去重
    t = re.sub(r"(\[(?:图片|表情|语音|视频|文件|卡片|音乐)\])(?:\s*\1)+", lambda m: m.group(1), t)  # 连发多张图/语音/文件等同类占位只留一个
    t = re.sub(r"(?m)^(.+)\n(?:\1\n?)+", lambda m: m.group(1) + "\n", t)  # 转发/复读连着几行一模一样的只留一行
    t = re.sub(r"(?m)^(?:@(?:全体成员|所有人|all)\s*)+", "", t)  # 句首的 @全体成员 在锁屏上白占宽度（紧急度已由级别表达）
    t = re.sub(r"^\s*(?:(?:转发|Fwd?|Re)\s*[:：]\s*)+", "", t, flags=re.I)  # 开头的“转发：/Fwd:”前缀无信息
    t = re.sub(r"(?m)[\s—-]*(?:发自|来自|Sent from)\s*(?:我的)?\s*(?:iPhone|iPad|Android|微信|QQ|手机)\S*\s*$", "", t)  # 转发带的“发自我的iPhone”签名尾巴无信息
    t = re.sub(r"[\u2002-\u200a\u00a0\u202f]", " ", t)  # 微信 @昵称 后的 U+2005 等特殊空格统一成普通空格，下行再折叠
    return re.sub(r"[ \u3000\t]{2,}", " ", t).strip()  # 首尾空行/空格一并去掉； 连续空格/全角空格折成一个，锁屏不出现一大段空白


CODE_AFTER = re.compile(r"(?:取件码|验证码|提取码|邀请码|兑换码|核销码|领取码|报名码|口令|暗号|密码|签到码)[^A-Za-z0-9]{0,4}([A-Za-z0-9-]{4,10})")
CODE_BEFORE = re.compile(r"(?<![\dA-Za-z])(\d{4,8})(?![\dA-Za-z])\D{0,6}(?:是|为)?(?:你的|您的)?(?:取件码|验证码|提取码|邀请码|兑换码|核销码|领取码|报名码|签到码)")  # 码在前（“123456 为您的验证码”）也认


def build_payload(title: str, body: str, level: str = "active", site_url: str = "", path: str = "/", badge: int = -1, group: str = "") -> dict:
    """纯函数：把标题/正文整理成 Bark 请求体（清理、去重、级别、复制内容、点开地址）。发送与重试在 send_bark。"""
    title = strip_md(title).strip() or "群报"  # 空标题的推送在锁屏上像乱码，兜底成 App 名
    body = re.sub(r"\n\s*\n+", "\n", strip_md(body).strip()) or title  # 空行折掉
    if body != title and body.startswith(title) and len(body) > len(title) + 2:
        body = body[len(title):].lstrip("：:，,。 \n") or body  # 正文以标题开头时去掉重复
    payload = {"title": clip(title, 60), "body": clip(body, 300), "group": clip(group, 30) or "群报", "isArchive": "1"}  # 存进 Bark 历史
    if level in ("timeSensitive", "passive"):
        payload["level"] = level
    if level == "timeSensitive":
        payload.update(sound="alarm", volume=8, call="1")  # 紧急：不同铃声、拉高音量、循环响铃
    txt = title + " " + body
    code = CODE_AFTER.search(txt) or CODE_BEFORE.search(txt)
    payload["copy"] = code.group(1) if code else clip(title, 60)  # 有码复制码，否则复制标题；不开 autoCopy
    if badge >= 0:
        payload["badge"] = badge  # 角标 = 未完成待办数
    if site_url:
        site = site_url.rstrip("/")
        payload["url"] = site + path if path != "/" else site_url
        payload["icon"] = site + "/icon.png"
    return payload


async def send_bark(burl: str, title: str, body: str, level: str = "active", site_url: str = "", path: str = "/", badge: int = -1, group: str = "") -> bool:
    """发一条 Bark。level：timeSensitive / active / passive。"""
    burl = norm_bark_url(burl)
    if not burl:
        return False
    payload = build_payload(title, body, level, site_url, path, badge, group)
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
