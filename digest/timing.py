"""时间点与定时文案（纯函数，不依赖 app.py）：稍后提醒时间、日报整点、周报标题。"""
from datetime import datetime, timedelta


def snooze_until(h, now: datetime) -> datetime:
    """稍后提醒的时间点。「明早 9 点」：凌晨 5 点前说「明天」，指的是睡醒后的今早 9 点，不是 30 多小时以后。"""
    if h == "tomorrow":
        t = now.replace(hour=9, minute=0, second=0, microsecond=0)
        return t if now.hour < 5 else t + timedelta(days=1)
    return now + timedelta(hours=float(h or 1))



def digest_hours(s: dict) -> set:
    hs = {int(s.get("digest_hour", 21))}
    h2 = int(s.get("digest_hour2", -1))
    if 0 <= h2 <= 23:
        hs.add(h2)
    return hs



def weekly_title(d: dict) -> str:
    n = len([t for t in d.get("todos", []) if not (isinstance(t, dict) and t.get("done"))])
    return "本周群报" + (f" · {n} 件待办" if n else "")
