"""快车道判定（只影响快慢）。0.34.36 从 app.py 拆出；模式/规则事件由 app 注入，避免循环引用。"""
import contextlib, hashlib, json, re

URGENT_WORDS = re.compile(r"立刻|马上|立即|紧急|速来|尽快|赶紧|火速|十万火急")
# 交作业 / 交报告 / 报名 / 截止这类「带时间的要你做的事」：只用来走快车道（不影响送不送模型，也不生成规则待办）
DUE_TASK_RE = re.compile(r"交作业|交报告|交材料|交表|提交|上交|作业|实验报告|报名|缴费|交费|截止|ddl|签到|打卡|填表|填报|问卷", re.I)
DUE_WHEN_RE = re.compile(r"今天|今晚|今日|明天|明早|明晚|后天|下周|本周|这周|周[一二三四五六日天]|星期[一二三四五六日天]|\d{1,2}月\d{1,2}|\d{1,2}[:：]\d{2}|\d{1,2}\s*点|[一二三四五六七八九十]{1,3}点|月底|之前|以前|前交")


_hooks = {"chat_mode": lambda *a: "normal", "rule_event": lambda r: None}


def shash(s: dict) -> str:
    return hashlib.md5(json.dumps(s, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


_urg_cache = {"key": None, "v": {}}


def is_urgent(r, s) -> bool:
    """要紧 = 走快车道（约 3 秒内整理）。只影响快慢：不要紧的消息最迟 30 秒也一定送模型。"""
    key = shash(s)
    if _urg_cache["key"] != key or len(_urg_cache["v"]) > 100000:
        _urg_cache.update(key=key, v={})
    ck = (r["id"], r["source"], r["chat"]) if "id" in r.keys() else None
    if ck is not None and ck in _urg_cache["v"]:
        return _urg_cache["v"][ck]
    v = is_urgent_raw(r, s)
    if ck is not None:
        _urg_cache["v"][ck] = v
    return v


def is_urgent_raw(r, s) -> bool:
    t = r["text"] or ""
    if r["at_me"] or "@全体" in t or "@所有人" in t:
        return True
    if _hooks["chat_mode"](r["source"], r["chat"], s) == "focus":
        return True
    if r["sender"] and any(v and v in r["sender"] for v in s.get("vip") or []):
        return True
    if any(k and k.lower() in t.lower() for k in s.get("keywords") or []):
        return True
    if URGENT_WORDS.search(t):
        return True
    if DUE_TASK_RE.search(t) and DUE_WHEN_RE.search(t) and not re.search(r"[吗嘛？?]\s*$", t):
        return True
    with contextlib.suppress(Exception):
        if _hooks["rule_event"](r):
            return True
    return False


