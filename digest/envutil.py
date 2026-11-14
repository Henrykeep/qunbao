"""环境变量读取：.env 里写错（空串/文字）时回退默认值，不让服务启动即崩。"""
import math
import os


def env_int(name, default, lo=None):
    """lo：下限；写成 0/负数会让并发=0 卡死、分块=0 死循环，低于下限回退默认值。"""
    v = _int(name, default)
    return int(default) if lo is not None and v < lo else v


def _int(name, default):
    try:
        return int(env_str(name, default))
    except (TypeError, ValueError):
        pass
    try:  # "30.0" 这类写法按整数取
        v = float(env_str(name, default))
        if math.isfinite(v):
            return int(v)
    except (TypeError, ValueError):
        pass
    return int(default)


def env_float(name, default, lo=None):
    v = _float(name, default)
    return float(default) if lo is not None and v < lo else v


def _float(name, default):
    try:
        v = float(env_str(name, default))
    except (TypeError, ValueError):
        return float(default)
    # nan/inf 能被 float() 解析，但当超时/间隔用会让服务卡死或立即超时
    return v if math.isfinite(v) else float(default)


def env_str(name, default=""):
    """字符串：去首尾空白和成对引号（.env 里常见 KEY="xxx " 或复制带空格），否则口令/地址对不上。"""
    v = str(os.getenv(name, default)).replace("\ufeff", "").replace("\u200b", "").strip()  # BOM/零宽空格：Windows 记事本编辑 .env 常带
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    return v
