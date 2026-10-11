"""待办文本匹配：自由文本截止时间解析 + 「是不是同一件事」判断。纯函数，无数据库依赖（0.34.21 从 app.py 拆出）。"""
import os, re
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.getenv("APP_TZ") or "Asia/Shanghai")


def parse_due(text: str, now: datetime) -> datetime | None:
    """把「10月10日 23:59」「明天 18:00」「2026-10-10」之类的自由文本解析成时间；解析不了返回 None。"""
    t = (text or "").strip()
    if not t:
        return None
    day = None
    m = re.search(r"(?:(\d{4})[年/-])?(\d{1,2})[月/-](\d{1,2})[日号]?", t)
    if m:
        y = int(m.group(1) or now.year)
        try:
            day = datetime(y, int(m.group(2)), int(m.group(3)), tzinfo=now.tzinfo)
        except ValueError:
            return None
        if not m.group(1) and day.date() < now.date() - timedelta(days=30):
            day = day.replace(year=y + 1)
    else:
        for w, n in (("大后天", 3), ("后天", 2), ("明天", 1), ("明早", 1), ("明晚", 1), ("今天", 0), ("今晚", 0), ("今早", 0)):
            if w in t:
                day = (now + timedelta(days=n)).replace(hour=0, minute=0, second=0, microsecond=0)
                break
    if day is None:
        m = re.search(r"(下下|下个?|本|这)?(?:周|星期|礼拜)([一二三四五六日天])", t)
        if m:
            wd = "一二三四五六日天".index(m.group(2)) % 7 if m.group(2) != "天" else 6
            base = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            pre = m.group(1) or ""
            if pre.startswith("下下"):
                day = base + timedelta(days=14 + wd)
            elif pre.startswith("下"):
                day = base + timedelta(days=7 + wd)
            elif pre in ("本", "这"):
                day = base + timedelta(days=wd)
            else:  # 只说「周一」：指最近的下一个（今天是周四，说周一就是下周一）
                day = base + timedelta(days=wd)
                if day.date() < now.date():
                    day += timedelta(days=7)
        elif "月底" in t:
            nxt = (now.replace(day=28) + timedelta(days=4)).replace(day=1)
            day = (nxt - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        elif re.fullmatch(r"\s*(今天|今晚)?\s*(凌晨|早上|上午|中午|下午|晚上|晚)?\s*\d{1,2}\s*([:：]\d{2}|点半?)\s*(前|之前)?\s*", t):
            day = now.replace(hour=0, minute=0, second=0, microsecond=0)  # 只给了钟点：就是今天
    if day is None:
        return None
    hh, mm = 23, 59
    if not re.search(r"\d{1,2}\s*[:：点时]", t):  # 只说了上午/早上/中午，没给钟点
        if re.search(r"上午|早上|早晨|明早|今早", t):
            hh, mm = 12, 0
        elif "中午" in t:
            hh, mm = 13, 0
        elif re.search(r"下午", t):
            hh, mm = 18, 0
    m = re.search(r"(\d{1,2})\s*[:：点时]\s*(\d{1,2})?", t)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2) or (30 if re.search(r"\d\s*点半", t) else 0))
        if re.search(r"下午|晚|PM|pm", t) and hh < 12:
            hh += 12
        elif re.search(r"中午", t) and hh < 6:
            hh += 12
        if hh > 23 or mm > 59:
            return None
    return day.replace(hour=hh, minute=mm)


# ---------------- 待办同一件事的判断（LLM 每次整理都会改写标题，不能只认 "标题|群"）----------------
_PUNCT = re.compile(r"[\s\W_]+", re.U)
_NUM_RE = re.compile(r"\d+|第[一二三四五六七八九十百]+|[一二三四五六七八九十]+[章节次课题号期周]")
_STOP = ("请", "记得", "需要", "务必", "按时", "尽快", "一下", "及时", "之前")
_SYN = (("提交", "交"), ("上交", "交"), ("缴纳", "交"), ("缴", "交"), ("参加", "去"), ("参与", "去"), ("安装", "下载"))


def todo_key(t: dict) -> str:
    return (t.get("title") or "") + "|" + (t.get("chat") or "")


def split_key(k: str):
    title, sep, chat = (k or "").rpartition("|")
    return (title, chat) if sep else (k or "", "")


def _cn_hour(w: str) -> int:
    d = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if w == "十":
        return 10
    if w.startswith("十"):
        return 10 + d.get(w[1:], 0)
    if "十" in w:
        h, _, l = w.partition("十")
        return d.get(h, 1) * 10 + d.get(l, 0)
    return d.get(w, 0)


def norm_title(x: str) -> str:
    x = _PUNCT.sub("", (x or "").lower())
    for a, b in _SYN:
        x = x.replace(a, b)
    x = re.sub(r"([一二三四五六七八九十两]+)点", lambda m: str(_cn_hour(m.group(1))) + "点", x)
    for w in _STOP:
        x = x.replace(w, "")
    return x


def _norm_chat(x: str) -> str:
    return _PUNCT.sub("", (x or "").lower())


def _norm_due(x: str) -> str:
    x = (x or "").strip()
    if not x:
        return ""
    d = parse_due(x, datetime.now(TZ))
    return d.strftime("%Y-%m-%d %H:%M") if d else _PUNCT.sub("", x)


_DAYWORDS = (("大后天", "d3"), ("后天", "d2"), ("明天", "d1"), ("明晚", "d1"), ("明早", "d1"), ("明日", "d1"),
             ("今天", "d0"), ("今晚", "d0"), ("今早", "d0"), ("今日", "d0"), ("今夜", "d0"))


def _day_tokens(x: str) -> set:
    """标题里的相对日期（今晚/明晚/后天）归一成可比较的标记。"""
    out = set()
    for w, k in _DAYWORDS:
        if w in x:
            out.add(k)
            x = x.replace(w, "")
    return out


def _grams(x: str) -> set:
    return {x[i:i + 2] for i in range(len(x) - 1)} or ({x} if x else set())


def same_todo(a: dict, b: dict, cross_chat: bool = False) -> bool:
    """同一个群里、归一化后标题足够像（或截止时间相同且有共同词）就算同一件事。cross_chat=True 时不比较群。"""
    ca, cb = _norm_chat(a.get("chat")), _norm_chat(b.get("chat"))
    if not cross_chat and ca and cb and ca != cb and ca not in cb and cb not in ca:
        return False
    da_, db2 = _day_tokens(a.get("title") or ""), _day_tokens(b.get("title") or "")
    if da_ and db2 and da_ != db2:  # 「明晚8点查寝」≠「今晚八点查寝」
        return False
    ta, tb = norm_title(a.get("title")), norm_title(b.get("title"))
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    na, nb = _NUM_RE.findall(ta), _NUM_RE.findall(tb)
    if na and nb and na != nb:  # 「交第一章作业」和「交第二章作业」不是一件事
        return False
    ops = [o for o in SequenceMatcher(None, ta, tb).get_opcodes() if o[0] != "equal"]
    if len(ops) == 1 and ops[0][0] == "replace" and not re.search(r"[\d一二三四五六七八九十日天]", ta[ops[0][1]:ops[0][2]] + tb[ops[0][3]:ops[0][4]]):  # 只换了一处动作/对象：「报名篮球赛」≠「报名辩论赛」
        return False
    if min(len(ta), len(tb)) >= 3 and (ta in tb or tb in ta):
        return True
    r = SequenceMatcher(None, ta, tb).ratio()
    ga, gb = _grams(ta), _grams(tb)
    ov = len(ga & gb) / max(1, min(len(ga), len(gb)))
    if r >= 0.6 or ov >= 0.5:
        return True
    m = SequenceMatcher(None, ta, tb).find_longest_match(0, len(ta), 0, len(tb))
    if m.size >= 4 and m.size / min(len(ta), len(tb)) >= 0.5:  # 共同的长关键词，如「实验报告」
        return True
    da, db_ = _norm_due(a.get("due")), _norm_due(b.get("due"))
    return bool(da and da == db_ and (r >= 0.3 or ov >= 0.25))


def find_match(t: dict, recs: list):
    k = todo_key(t)
    for r in recs:
        if r["k"] == k:
            return r
    for r in recs:
        if same_todo(t, r):
            return r
    return None


def merge_cross_chat(todos: list) -> list:
    """多个群提到同一件事只留一条（先出现的），其余群名记入 also。已完成的不参与合并。"""
    out = []
    for t in todos:
        if t.get("done"):
            out.append(t)
            continue
        host = next((o for o in out if not o.get("done") and _norm_chat(o.get("chat")) != _norm_chat(t.get("chat"))
                     and same_todo(o, t, cross_chat=True)), None)
        if host is None:
            out.append(t)
            continue
        also = host.setdefault("also", [])
        for c in [t.get("chat")] + (t.get("also") or []):
            if c and c != host.get("chat") and c not in also:
                also.append(c)
        if t.get("pinned"):
            host["pinned"] = True
    return out
