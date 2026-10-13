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


WX_EMOJI = "微笑|撇嘴|色|发呆|得意|流泪|害羞|闭嘴|睡|大哭|尴尬|发怒|调皮|呲牙|惊讶|难过|酷|冷汗|抓狂|吐|偷笑|愉快|白眼|傲慢|困|惊恐|流汗|憨笑|悠闲|奋斗|咒骂|疑问|嘘|晕|衰|骷髅|敲打|再见|擦汗|抠鼻|鼓掌|坏笑|左哼哼|右哼哼|哈欠|鄙视|委屈|快哭了|阴险|亲亲|可怜|笑脸|生病|脸红|破涕为笑|恐惧|失望|无语|嘿哈|捂脸|奸笑|机智|皱眉|耶|吃瓜|加油|汗|天啊|Emm|社会社会|旺柴|好的|打脸|哇|翻白眼|裂开|苦涩|让我看看|叹气|合十|强|握手|胜利|抱拳|OK|拳头|爱心|心碎|玫瑰|凋谢|赞|踩"
PLACEHOLDER_ALIAS = re.compile(r"\[(?:动画表情|Sticker|" + WX_EMOJI + r")\]")  # 微信文字表情名表 + 表情类占位别名


def _cq(m) -> str:
    return CQ_LABEL.get(m.group(1), "")  # QQ 码统一一张表：有可读含义的留占位，其余去掉


def _first(m) -> str:
    return m.group(1)


_PLACEHOLDERS = r"\[(?:图片|表情|语音|视频|文件|卡片|音乐)\]"
# 顺序即语义：先还原/清标记，再折叠占位与重复，最后规整空白。新规则只往表里加一行。
STRIP_RULES = [(re.compile(p, f), r) for p, r, f in [
    (r"[\u200b-\u200d\u2060\ufeff\u00ad\u180e\u200e\u200f\u202a-\u202e]", "", 0),  # 零宽字符：锁屏是看不见的空档，还会让取件码匹配失败
    (r"\r\n?|[\u2028\u2029\x0b\x0c\x85]", "\n", 0),  # 换行统一（含 Unicode 行/段分隔符、垂直制表），免得重复行折叠和列表符匹配被 \r 挡住
    (r"(?i)<br\s*/?>", "\n", 0),  # 转发文本里的 <br> 换成真换行
    (r"(?i)</?(?:b|i|u|p|div|span|font|strong|em)\b[^>]*>", "", 0),
    (r"\[CQ:(\w+)[^\]]*\]", _cq, 0),
    (r"(?s)^\s*「[^」]*」\s*\n\s*(?:-\s*){6,}\n?", "", 0),  # 微信引用回复自带“「昵称：原话」+ 虚线”
    (r"(?m)^\s*(?:#{1,6}|[-*>]|\d{1,2}[.、)])\s+", "", 0),  # 标题/列表/引用前缀
    (r"(?m)^\s*[①-⑩⓵-⓾•·▪●○◆◇■□▶►➤➡✔✅☑]\s*", "", 0),  # 圆圈数字/圆点/箭头/对勾列表符
    (r"\[([^\]]+)\]\([^)]*\)", r"\1", 0),
    (r"https?://[\x21-\x7e]+", "[链接]", 0),  # 只吃 ASCII，网址后紧跟的中文不被一起吞掉
    (r"(?<![\w.@/])www\.[\x21-\x7e]+", "[链接]", 0),
    (r"(\[链接\][\s,，、]*){2,}", "[链接] ", 0),  # 多个网址只留一个占位
    (r"(?m)^\s*(?:[-*_=]{3,}|\|?[\s:|-]{3,}\|)\s*$", "", 0),  # 分隔线/表格对齐行
    (r"\*\*|__|~~|`", "", 0),  # 粗体/删除线/代码标记
    (r"(?<![\w*])\*([^\s*][^*\n]*?)\*(?![\w*])", r"\1", 0),  # 斜体 *x*（a*b 不动）
    (r"([!！?？。.~～])\1{2,}", r"\1\1", 0),  # 刷屏标点压成两个
    (r"([哈啊嘿呵])\1{3,}", r"\1\1\1", 0),
    (r"([\U0001F300-\U0001FAFF\u2600-\u27BF])\1{2,}", r"\1\1", 0),  # 刷屏表情压成两个
    (r"(?:…|\.){4,}", "……", 0),
    (PLACEHOLDER_ALIAS.pattern, "[表情]", 0),  # 微信文字表情/动画表情/Sticker 并入统一占位
    (r"\[(?:Photo|照片|Image|Picture)\]", "[图片]", 0),
    (r"\[(?:Voice(?: Message)?|语音消息|Audio)\]", "[语音]", 0),
    (r"\[(?:Video|小视频|视频号)\]", "[视频]", 0),
    (r"\[(?:File|Document)\]", "[文件]", 0),
    (r"(" + _PLACEHOLDERS + r")(?:\s*\1)+", _first, 0),  # 连发同类占位只留一个
    (r"(?m)^(.+)\n(?:\1\n?)+", lambda m: m.group(1) + "\n", 0),  # 复读连着几行一样的只留一行
    (r"(?m)^(?:@(?:全体成员|所有人|all)\s*)+", "", 0),  # 句首 @全体成员（紧急度已由级别表达）
    (r"^\s*(?:(?:[\[【(（]\s*(?:转发|Fwd?|自动回复|Re)\s*[\]】)）]\s*[:：]?|(?:转发|Fwd?|Re)\s*[:：])\s*)+", "", re.I),  # 转发/回复前缀
    (r"(?m)[\s—-]*(?:发自|来自|Sent from)\s*(?:我的)?\s*(?:iPhone|iPad|Android|微信|QQ|手机)\S*\s*$", "", 0),  # 签名尾巴
    (r"[【\[（(]\s*[】\]）)]", "", 0),  # 空括号（图片/表情被剥掉后留下的【】）锁屏上是噪点
    (r"[\u2002-\u200a\u00a0\u202f]", " ", 0),  # 特殊空格统一成普通空格
    (r"[ \u3000\t]{2,}", " ", 0),  # 连续空格折成一个
]]


def strip_md(t: str) -> str:
    """Bark 不渲染 Markdown，LLM/转发带的标记会原样露在锁屏上：按 STRIP_RULES 顺序清掉。"""
    t = t or ""
    for i, (rx, rep) in enumerate(STRIP_RULES):
        t = rx.sub(rep, t)
        if i == 1:
            t = html.unescape(t)  # 换行统一后再解 &amp; &lt;，其后的标签规则才看得见 &lt;b&gt; 还原出的标签
    return t.strip()


CODE_WORDS = "取件码|验证码|提取码|邀请码|兑换码|核销码|领取码|报名码|签到码|入群码|取餐码|校验码|动态码|确认码|取货码|提货码|排队号|口令|暗号|密码|会议号|会议ID|房间号|运单号|快递单号|预约号|叫号|取票码|取书码|取件号|领取号|订单号|取号|登记码|考场号|座位号"  # 码在前后两种写法共用一张词表
CODE_AFTER = re.compile(r"(?:" + CODE_WORDS + r")[^A-Za-z0-9]{0,4}([A-Za-z0-9-]{4,10})")
CODE_BEFORE = re.compile(r"(?<![\dA-Za-z])(\d{4,8})(?![\dA-Za-z])\D{0,6}(?:是|为)?(?:你的|您的)?(?:" + CODE_WORDS + r")")  # “123456 为您的验证码”


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
