"""登录相关的纯函数：常量时间比较、令牌哈希、Basic 头解析、客户端 IP、会话 Cookie。不依赖 app.py 的全局状态。"""
import base64, hashlib, secrets

COOKIE = "qb_session"


def ceq(a, b) -> bool:
    return secrets.compare_digest(str(a).encode(), str(b).encode())


def tok_hash(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def basic_creds(header: str):
    """Authorization 头 → (用户, 密码)；不是 Basic 或解析失败返回 None。"""
    if not (header or "").lower().startswith("basic "):
        return None
    try:
        u, _, p = base64.b64decode(header[6:]).decode().partition(":")
    except Exception:
        return None
    return u, p


def client_ip(req) -> str:
    return (req.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (req.client.host if req.client else "?"))


def set_session_cookie(resp, tok: str, req, days: int):
    secure = req.url.scheme == "https" or req.headers.get("x-forwarded-proto") == "https"
    resp.set_cookie(COOKIE, tok, max_age=days * 86400, httponly=True, samesite="lax", secure=secure, path="/")
