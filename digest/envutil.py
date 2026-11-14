"""环境变量读取：.env 里写错（空串/文字）时回退默认值，不让服务启动即崩。"""
import os


def env_int(name, default):
    try:
        return int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return int(default)
