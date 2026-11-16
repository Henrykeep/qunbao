"""环境变量读取：.env 里写错（空串/文字）时回退默认值，不让服务启动即崩。"""
import math
import os


def env_int(name, default, lo=None, hi=None, unit=None):
    """lo/hi：范围；写成 0/负数会让并发=0 卡死、分块=0 死循环，DIGEST_HOUR=25 永不触发，越界回退默认值。"""
    v = _int(name, default, unit)
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        return int(default)
    return v


_SEC = {"s": 1, "sec": 1, "秒钟": 1, "秒": 1, "分钟": 60, "分": 60, "min": 60,
        "小时": 3600, "h": 3600, "天": 86400, "d": 86400}


_CN = {c: i for i, c in enumerate('零一二三四五六七八九十')}
_CN['两'] = 2


def _num(name, default, unit=None):
    """数字字符串：全角数字转半角，去掉末尾单位（30s/30秒/8点），中文输入法常见。"""
    import re
    v = env_str(name, default).translate({0xff10 + i: 0x30 + i for i in range(10)})
    v = v.replace("．", ".").replace("－", "-")
    if re.fullmatch(r"\d{1,3}(?:[,，]\d{3})+(?:\.\d+)?", v):
        v = re.sub(r"[,，]", "", v)  # 千分位 1,000 否则读不出回退默认值
    m = re.fullmatch(r"(\d+)\s*(?:个?小时|h)\s*(\d+)\s*(?:分钟|分|min)?", v, flags=re.I)
    if m:  # 1小时30分 → 90分钟
        v = f"{int(m.group(1)) * 60 + int(m.group(2))}分钟"
    m = re.fullmatch(r"(\d+)\s*(?:天|d)\s*(\d+)\s*(?:个?小时|h)?", v, flags=re.I)
    if m:  # 1天12小时 → 36小时
        v = f"{int(m.group(1)) * 24 + int(m.group(2))}小时"
    v = re.sub(r"^(\d+)\s*个?(?=小时|天)(小时|天)半$", lambda m: str(int(m.group(1)) + 0.5) + m.group(2), v)  # 2小时半
    v = re.sub(r"^([一二两三四五六七八九十])个?(小时|天)半$", lambda m: str(_CN[m.group(1)] + 0.5) + m.group(2), v)  # 三小时半
    v = re.sub(r"^一刻钟$", "15分钟", v)
    v = re.sub(r"^半\s*个?(?=小时|分钟|天)", "0.5", v)  # 半小时
    v = re.sub(r"^([一二两三四五六七八九十])个半(?=小时|分钟|天)", lambda m: str(_CN[m.group(1)] + 0.5), v)  # 一个半小时
    v = re.sub(r"^([一二两三四五六七八九]?十)([一二三四五六七八九]?)(?=\s*个?\s*(?:小时|分钟|分|秒钟|秒|天)$)",
               lambda m: str((_CN.get(m.group(1)[0], 1) if len(m.group(1)) == 2 else 1) * 10 + _CN.get(m.group(2), 0)), v)  # 十五分钟/二十分钟
    v = re.sub(r"^([一二两三四五六七八九十])(?=\s*个?\s*(?:小时|分钟|分|秒钟|秒|天)$)", lambda m: str(_CN[m.group(1)]), v)  # 三分钟
    m = re.fullmatch(r"(.*?)\s*个?\s*(s|sec|秒钟|秒|点|个|次|分钟|分|min|小时|h|天|d)", v, flags=re.I)
    if not m:
        return v
    num, u = m.group(1), m.group(2).lower()
    if unit in _SEC and u in _SEC:  # 写「2分钟」而变量以秒计：换算，不能直接丢单位
        try:
            return repr(float(num) * _SEC[u] / _SEC[unit])
        except ValueError:
            pass
    return num


def _int(name, default, unit=None):
    try:
        return int(_num(name, default, unit))
    except (TypeError, ValueError):
        pass
    try:  # "30.0" 这类写法按整数取
        v = float(_num(name, default, unit))
        if math.isfinite(v):
            return int(v)
    except (TypeError, ValueError):
        pass
    return int(default)


def env_float(name, default, lo=None, hi=None, unit=None):
    """hi：LLM_TIMEOUT=6000 这类多写个 0 会让一个群卡住整整一小时，越界回退默认值。"""
    v = _float(name, default, unit)
    return float(default) if (lo is not None and v < lo) or (hi is not None and v > hi) else v


def _float(name, default, unit=None):
    try:
        v = float(_num(name, default, unit))
    except (TypeError, ValueError):
        return float(default)
    # nan/inf 能被 float() 解析，但当超时/间隔用会让服务卡死或立即超时
    return v if math.isfinite(v) else float(default)


def env_str(name, default="", literal=False):
    """字符串：去首尾空白和成对引号（.env 里常见 KEY="xxx " 或复制带空格），否则口令/地址对不上。"""
    v = str(os.getenv(name, default)).replace("\ufeff", "").replace("\u200b", "").strip()  # BOM/零宽空格：Windows 记事本编辑 .env 常带
    if literal:  # 登录密码：弯引号、「 #」都可能是密码本身，只去首尾空白和成对引号
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1].strip()
        return v or str(default).strip()
    v = v.translate({0x201c: 0x22, 0x201d: 0x22, 0x2018: 0x27, 0x2019: 0x27})  # 中文输入法的弯引号
    if v[:1] not in "\"'" and " #" in v:
        v = v.split(" #", 1)[0].strip()  # 行尾注释（30  # 秒）：空格+# 之后不是值
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
    v = re.sub(r"^bearer\s+", "", env_str(name, default), flags=re.I)
    return "".join(v.split())  # 密钥不含空白；从网页复制折行/夹空格会导致 401


def env_path(name, default=""):
    """路径：中文输入法的全角斜杠转半角（DB_PATH=／data／qunbao.db 否则建库失败）。"""
    return env_str(name, default).replace("／", "/")


def env_name(name, default=""):
    """模型名等标识符：全角转半角，不含空白（复制折行/夹空格会 404）。"""
    import unicodedata
    return "".join(unicodedata.normalize("NFKC", env_str(name, default)).split())
