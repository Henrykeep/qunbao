"""环境变量读取：.env 里写错（空串/文字）时回退默认值，不让服务启动即崩。"""
import math
import os


def env_int(name, default, lo=None, hi=None):
    """lo/hi：范围；写成 0/负数会让并发=0 卡死、分块=0 死循环，DIGEST_HOUR=25 永不触发，越界回退默认值。"""
    v = _int(name, default)
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        return int(default)
    return v


def _num(name, default):
    """数字字符串：全角数字转半角，去掉末尾单位（30s/30秒/8点），中文输入法常见。"""
    import re
    v = env_str(name, default).translate({0xff10 + i: 0x30 + i for i in range(10)})
    v = v.replace("．", ".").replace("－", "-")
    return re.sub(r"\s*(?:s|sec|秒|点|个|次|分钟|min)$", "", v, flags=re.I)


def _int(name, default):
    try:
        return int(_num(name, default))
    except (TypeError, ValueError):
        pass
    try:  # "30.0" 这类写法按整数取
        v = float(_num(name, default))
        if math.isfinite(v):
            return int(v)
    except (TypeError, ValueError):
        pass
    return int(default)


def env_float(name, default, lo=None, hi=None):
    """hi：LLM_TIMEOUT=6000 这类多写个 0 会让一个群卡住整整一小时，越界回退默认值。"""
    v = _float(name, default)
    return float(default) if (lo is not None and v < lo) or (hi is not None and v > hi) else v


def _float(name, default):
    try:
        v = float(_num(name, default))
    except (TypeError, ValueError):
        return float(default)
    # nan/inf 能被 float() 解析，但当超时/间隔用会让服务卡死或立即超时
    return v if math.isfinite(v) else float(default)


def env_str(name, default=""):
    """字符串：去首尾空白和成对引号（.env 里常见 KEY="xxx " 或复制带空格），否则口令/地址对不上。"""
    v = str(os.getenv(name, default)).replace("\ufeff", "").replace("\u200b", "").strip()  # BOM/零宽空格：Windows 记事本编辑 .env 常带
    v = v.translate({0x201c: 0x22, 0x201d: 0x22, 0x2018: 0x27, 0x2019: 0x27})  # 中文输入法的弯引号
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    return v or str(default).strip()  # 写成空值（LLM_MODEL= / WEB_USER=）等于没写，用默认值


def env_url(name, default=""):
    """地址：漏写 http:// 的补上（NAPCAT_HTTP=napcat:3000 否则请求直接报错），去尾斜杠。"""
    return norm_url(env_str(name, default))


def norm_url(raw):
    v = "".join(str(raw or "").split()).replace("：", ":").replace("／", "/")  # 复制地址常夹带空格/换行；中文输入法的全角冒号斜杠
    if v and "://" not in v:
        v = "http://" + v
    return v.rstrip("/。，,;；")  # 复制时常带上句末标点


def env_token(name, default=""):
    """口令/密钥类配置：env_str 之外再去掉从文档复制带来的 Bearer 前缀。"""
    import re
    return re.sub(r"^bearer\s+", "", env_str(name, default), flags=re.I)
