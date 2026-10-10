"""入站消息解析：时间容错 / CQ 码清洗 / 微信通知格式。0.34.37 从 app.py 拆出（纯函数，无状态）。"""
import os, re, time
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.getenv("APP_TZ") or "Asia/Shanghai")


def norm_ts(ts, now=None) -> int:
    """消息时间容错：毫秒/字符串/ISO/0/负数/来自未来（手机时间不准）/早于保留期的，都收敛成合理的秒数；拿不准就用收到的时间。"""
    now = int(now or time.time())
    try:
        if isinstance(ts, str):
            t = ts.strip()
            if re.fullmatch(r"\d+(\.\d+)?", t):
                v = float(t)
            else:  # 2026-10-09 20:01:02 / 2026-10-09T20:01:02+08:00
                d = datetime.fromisoformat(t.replace("Z", "+00:00").replace("/", "-"))
                v = (d if d.tzinfo else d.replace(tzinfo=TZ)).timestamp()
        else:
            v = float(ts)
    except (TypeError, ValueError, OverflowError):
        return now
    if v > 1e11:  # 毫秒
        v /= 1000
    if v != v or v < 1e9 or v > now + 300 or v < now - 30 * 86400:
        return now
    return int(v)



CQ = re.compile(r"\[CQ:(\w+)([^\]]*)\]")
CQ_NAME = {"image": "[图片]", "face": "", "record": "[语音]", "video": "[视频]", "file": "[文件]",
           "reply": "", "forward": "[聊天记录]", "json": "[卡片]", "xml": "[卡片]", "mface": "[表情]"}


def cq_images(raw: str) -> str:
    """提取 CQ 图片的 http(s) URL，空格分隔，最多 4 张。"""
    out = []
    for m in CQ.finditer(raw or ""):
        if m.group(1) == "image":
            u = re.search(r"url=(https?://[^,\]]+)", m.group(2))
            if u:
                out.append(u.group(1).replace("&amp;", "&"))
    return " ".join(out[:4])


def clean_cq(raw: str, self_id: str) -> str:
    def rep(m):
        kind, args = m.group(1), m.group(2)
        if kind == "at":
            q = re.search(r"qq=(\w+)", args)
            if q and q.group(1) == "all":
                return "@全体成员"
            if q and q.group(1) == self_id:
                return "@我"
            n = re.search(r"name=([^,\]]+)", args)
            return f"@{n.group(1)}" if n else "@某人"
        return CQ_NAME.get(kind, "")
    return CQ.sub(rep, raw).replace("&#91;", "[").replace("&#93;", "]").replace("&#44;", ",").replace("&amp;", "&").strip()



WX_COUNT = re.compile(r"^\[\d+条\]\s*")
WX_SKIP = ("你收到了一条消息", "收到一条新消息", "正在运行", "条新消息")


def _pick(d, *keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def parse_wx(d: dict):
    """把各种通知转发 App 的格式统一成 (chat, sender, text, at_me)。
    支持：{chat,sender,text} 原生格式；{title,content|text|msg} 通知格式；只有一个 content/msg 字段时第一行当标题。"""
    if d.get("chat") and d.get("text"):
        t = str(d["text"])
        return str(d["chat"]), str(d.get("sender") or ""), t, bool(d.get("at_me")) or "@我" in t or "[有人@我]" in t
    title = _pick(d, "title", "android.title", "name")
    body = _pick(d, "text", "content", "msg", "message", "body", "android.text")
    if not title and "\n" in body:
        title, body = body.split("\n", 1)
    title, body = title.strip(), body.strip()
    if not body or title in ("微信", "WeChat") or any(k in body for k in WX_SKIP):
        return None
    body = WX_COUNT.sub("", body)
    at_me = "[有人@我]" in body or "@所有人" in body
    body = body.replace("[有人@我]", "").strip()
    m = re.match(r"^([^:：\n]{1,32})[:：]\s?(.+)$", body, re.S)
    if m:  # 群消息通知：标题是群名，内容是「发送人: 内容」
        return title or "未知群", m.group(1).strip(), m.group(2).strip(), at_me
    return f"私聊·{title or '未知'}", title, body, True  # 私聊通知：标题是好友名


