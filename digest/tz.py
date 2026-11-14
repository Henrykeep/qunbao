"""全站唯一的时区定义（APP_TZ，默认 Asia/Shanghai）。"""
import os
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.getenv("APP_TZ") or "Asia/Shanghai")


def dt(ts):
    """时间戳 → 本地时区 datetime，全站唯一入口。"""
    from datetime import datetime
    return datetime.fromtimestamp(ts, TZ)
