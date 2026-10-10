"""头条的纯函数：截断整理、规则拼头条、头条与事项的匹配度（0.34.31 从 app.py 拆出，不碰数据库/全局状态）。"""
import re
from datetime import datetime

from todo_match import parse_due, norm_title, _grams
from zoneinfo import ZoneInfo
import os

TZ = ZoneInfo(os.getenv("APP_TZ") or "Asia/Shanghai")


def tidy_headline(t: str, limit: int = 30) -> str:
    """头条只留一件完整的事：太长就在标点处截断，绝不从半个词中间切开。"""
    t = (t or "").strip().strip("\"“”「」'").strip()
    t = re.sub(r"^(头条|标题)[:：]\s*", "", t)
    segs = re.split(r"[、，,；;]|另外|还有|以及|并且", t)
    if (t.count("、") >= 1 and len(segs) >= 3 or len(segs) >= 4) and len(segs[0].strip()) >= 6:
        t = segs[0].strip()  # 模型罗列了好几件事：头条只说第一件（最要紧的）
    if len(t) <= limit:
        return t.rstrip("，、；,;")
    cut = max((m.end() for m in re.finditer(r"[，。；！？、,;!?]", t[:limit + 1])), default=0)
    if cut >= 8:
        return t[:cut].rstrip("，。；、,;").strip()
    return t[:limit - 1].rstrip("，、；,;") + "…"


QUIET_HEADS = ("", "群里没什么要你管的", "这段时间群里很安静")


def rule_headline(items) -> str:
    """不调模型的头条：挑最要紧的一件（@我 > 截止最早 > 高优先级），标题 + 截止。"""
    if not items:
        return ""
    now = datetime.now(TZ)
    far = datetime(2100, 1, 1, tzinfo=TZ)
    def k(t):
        d = parse_due(t.get("due", ""), now)
        return (bool(d and d < now), not t.get("at_me"), d or far, t.get("urgency") != "high")  # 已过截止的不当头条（除非只剩它）
    t = sorted(items, key=k)[0]
    lead = t["title"] + (f"，{t['due']}" if t.get("due") else "")
    if len(items) > 1:  # 头条是概括不是复制：多件时点出最急的一件 + 总数，首页待办列表里就不会再看到一模一样的一行
        return tidy_headline(f"共 {len(items)} 件待办，最急：{lead}")
    return tidy_headline(lead)


def _head_score(head, title):
    """头条有多少是在说这件事：事项标题的 2 字片段有几成出现在头条里（头条常带群名前缀、「需立刻」之类，整句比相似度会偏低）。"""
    nh, nt = norm_title(head), norm_title(title)
    if not nh or not nt:
        return 0.0
    if nt in nh or nh in nt:
        return 1.0
    g = _grams(nt)
    return len(g & _grams(nh)) / len(g)


HEAD_MATCH = 0.5


def live_head_plan(prev_body: dict, body: dict, has_latest: bool, last_model_ts: float, now: float, gap: int, model_head: bool = True):
    """首页那一期原地更新时的头条决策（纯函数）：返回 (new_items, head, want_model)。
    出现新的要紧事项（高紧急 / @我）、或头条还是「没事」而现在有事、或没有旧期 → 重写（先用规则拼）；模型头条受 gap 限流。"""
    old_ids = {(t.get("id"), t.get("title")) for t in (prev_body.get("todos") or []) + (prev_body.get("notices") or [])}
    new = [t for t in body["todos"] + body["notices"] if (t.get("id"), t.get("title")) not in old_ids and not t.get("done")]
    hot = [t for t in new if t.get("urgency") == "high" or t.get("at_me")]
    head = prev_body.get("headline") or ""
    opens = [t for t in body["todos"] if not t["done"]] + body["notices"]
    rewrite = bool(hot or (head in QUIET_HEADS and opens) or not has_latest)
    if rewrite:
        head = rule_headline(hot or opens) or head
    want_model = rewrite and model_head and now - float(last_model_ts or 0) >= gap
    return new, head, opens, want_model


def check_headline(head, opens, gone) -> str:
    """头条只能说一件还没做完的事：说的是未完成事项里的某一件（且不更像某件已完成/已过期的）就保留，
    否则（勾完成的事、过期的事、只在群要点里出现的事）一律换成规则从未完成事项里挑的一句。纯函数。"""
    head = head or ""
    if not opens:
        return head if head in QUIET_HEADS[1:] else "群里没什么要你管的"
    if head in QUIET_HEADS:
        return rule_headline(opens)
    best_open = max((_head_score(head, t.get("title", "")) for t in opens), default=0.0)
    best_gone = max((_head_score(head, t) for t in gone), default=0.0)
    if best_open >= HEAD_MATCH and best_open >= best_gone:
        return head
    return rule_headline(opens) or "群里没什么要你管的"
