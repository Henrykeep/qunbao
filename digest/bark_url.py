"""Bark 地址清洗：用户粘贴的各种形态 → 服务器/KEY。"""
import re
import unicodedata


def norm_bark_url(u: str) -> str:
    """用户常把 Bark App 里的示例整条粘进来（…/KEY/内容?group=x），只留 服务器/KEY。"""
    from urllib.parse import urlsplit
    u = unicodedata.normalize("NFKC", u or "").replace("。", ".")  # 中文输入法的全角 ：／．等
    if re.search(r"%(20|0a|0d|09|2f|3a|3f|23)", u, re.I):  # 从网页/聊天复制出的地址带 %20 等转义空白：先还原再清掉
        from urllib.parse import unquote
        u = unquote(u)
    u = u.strip().strip("\"'“”‘’<>《》「」").strip()  # 复制时常带引号/书名号
    u = re.sub(r"^((?:https?://)?[\w.-]+\.[A-Za-z]{2,})\s+([A-Za-z0-9]{16,40})$", r"\1/\2", u)  # 域名和 Key 中间只隔了空格（api.day.app KEY）：补成路径，别拼成一个域名
    u = re.sub(r"^((?:https?://)?)www\.(?=api\.day\.app)", r"\1", u, flags=re.I)  # www.api.day.app 不存在
    u = re.sub(r"[\s\u200b-\u200f\u2060\ufeff]+", "", u)  # 微信/备忘录复制常夹换行、空格
    m = re.search(r"https?:/{0,2}[^\s\u4e00-\u9fff]+", u, re.I) or (re.search(r"(?<![A-Za-z0-9])[A-Za-z0-9]{16,40}(?![A-Za-z0-9])", u) if re.search(r"[\u4e00-\u9fff]", u) else None)
    if m and m.group(0) != u:  # 整句话粘进来（「我的 Key：abc…」「地址 https://… 复制自 Bark」）：只取其中的地址或 Key
        u = m.group(0)
    u = re.sub(r"^(?:device[_-]?)?key[:=]", "", u, flags=re.I)  # 英文标签「Key:abc…」「device_key=abc…」
    u = u.rstrip(".,;:!?)]}、")  # 句末标点（。，！）不属于地址/Key
    u = re.sub(r"^(https?):?/{1,2}(?=[^/])", r"\1://", u, flags=re.I)  # https:/、https// 少打了斜杠或冒号
    if u and "://" not in u and "." in u.split("/")[0]:
        h = u.split("/")[0]
        # 漏写协议：域名补 https（api.day.app/KEY）；IP 或带端口的自建服务多半没证书，补 http
        selfhost = ":" in h or re.fullmatch(r"[\d.]+", h)
        u = ("http://" if selfhost else "https://") + u
    u = re.sub(r"^bark:/{0,2}(?=[A-Za-z0-9])", "https://api.day.app/", u, flags=re.I)  # bark://KEY 深链：同官方服务器
    if re.fullmatch(r"[A-Za-z0-9]{16,40}", u):  # 只粘了设备 Key：默认官方服务器
        u = "https://api.day.app/" + u
    u = u.split("#", 1)[0]  # 锚点 #xxx 不属于地址
    sp = urlsplit(u)
    if not sp.scheme or not sp.netloc:
        return u.rstrip("/")
    seg = [x for x in sp.path.split("/") if x and x not in (".", "..")]  # ./ ../ 段无意义
    dk = re.search(r"device_key=([A-Za-z0-9]+)", sp.query)
    if dk and (not seg or seg[-1] == "push"):  # 粘成了 /push?device_key=KEY 这种接口地址（含子路径 /bark/push）
        seg = seg[:-1] + [dk.group(1)]
    scheme = sp.scheme.lower()
    ui, _, hp = sp.netloc.rpartition("@")  # 反代 Basic 认证 user:pass@ 区分大小写，只小写主机部分
    host = (ui + "@" if ui else "") + hp.lower()
    host = re.sub(r":(443|80)$", lambda m: "" if (m.group(1) == "443") == (scheme == "https") else m.group(0), host)  # 默认端口多余
    if host.rpartition("@")[2] == "api.day.app":
        scheme = "https"  # 官方服务器只认 https，手滑写成 http 会推送失败
    if len(seg) > 1:  # 自建 Bark 挂在反代子路径下（/bark/KEY/内容）：保留前缀，到 Key 为止
        i = next((j for j, x in enumerate(seg) if re.fullmatch(r"[A-Za-z0-9]{16,40}", x)), 0)
        seg = seg[:i + 1]
    return f"{scheme}://{host}" + ("/" + "/".join(seg) if seg else "")  # 协议/域名不分大小写，统一小写（Key 区分大小写不动）


def has_key(u) -> bool:
    """规范化后的地址路径里要有 Key（最后一段），只有域名发不出去。"""
    from urllib.parse import urlsplit
    sp = urlsplit(norm_bark_url(str(u or "")))
    seg = [x for x in sp.path.split("/") if x]
    if not seg or seg[-1].lower() in ("push", "ping", "healthz", "register"):  # 接口名不是 Key
        return False
    if (sp.hostname or "") == "api.day.app":  # 官方服务器的 Key 是 16~40 位字母数字，写短了必定推送失败
        return bool(re.fullmatch(r"[A-Za-z0-9]{16,40}", seg[-1]))
    return True


def open_url(site_url: str, path: str = "/") -> tuple:
    """推送点开地址与图标：站点地址去 ?参数/#锚点，路径去 ./ ../ 段、整条网址退回首页、中文/空格编码。站点不是 http(s) 返回 ("", "")。"""
    from urllib.parse import quote
    site_url, path = (site_url or "").strip(), (path or "").strip() or "/"
    if not re.match(r"https?://", site_url, re.I):
        return "", ""
    if re.match(r"[a-z][a-z0-9+.-]*://", path, re.I):
        path = "/"
    path = "/".join(x for x in path.split("/") if x not in ("", ".", ".."))
    site = re.split(r"[?#]", site_url, 1)[0].rstrip("/")
    return site + ("/" + quote(path, safe="/?&=#%:@+,;~") if path else "/"), site + "/icon.png"
