"""全站唯一的时区定义（APP_TZ，默认 Asia/Shanghai）。"""
import os
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.getenv("APP_TZ") or "Asia/Shanghai")
