"""免打扰时段判断与提醒窗口（纯函数，不依赖 app.py）。"""
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.getenv("APP_TZ") or "Asia/Shanghai")


def in_quiet(s, now=None) -> bool:
    a, b = int(s.get("quiet_start", -1)), int(s.get("quiet_end", 7))
    if a < 0 or a == b:
        return False
    h = (now or datetime.now(TZ)).hour
    return (a <= h < b) if a < b else (h >= a or h < b)


def remind_start(due, n, s):
    """提醒窗口起点：默认截止前 n 小时；若这一刻落在免打扰里（凌晨 7 点的事 3 小时前是半夜 4 点，
    推了也只会被压到早上），就提前到免打扰开始前 1 小时（前一晚），睡前就知道明早有事。"""
    st = due - timedelta(hours=n)
    a = int(s.get("quiet_start", -1))
    if a >= 0 and in_quiet(s, st):
        q = due.replace(hour=a, minute=0, second=0, microsecond=0)
        if q > due:
            q -= timedelta(days=1)
        st = min(st, q - timedelta(hours=1))
    return st
