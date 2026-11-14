"""全站唯一的时区定义（APP_TZ，默认 Asia/Shanghai）。"""
from zoneinfo import ZoneInfo

from envutil import env_str


def _load(name):
    """APP_TZ 写错（拼写/容器缺 tzdata）时回退上海时区，不让服务启动即崩。"""
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


TZ = _load(env_str("APP_TZ", "") or "Asia/Shanghai")


def dt(ts):
    """时间戳 → 本地时区 datetime，全站唯一入口。"""
    from datetime import datetime
    return datetime.fromtimestamp(ts, TZ)


def now():
    """当前本地时区 datetime，全站唯一入口。"""
    from datetime import datetime
    return datetime.now(TZ)


def clock():
    """当前时间（浮点秒），全站取时间唯一入口。"""
    import time
    return time.time()
