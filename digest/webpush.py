"""Web Push 传输层：VAPID 密钥、发送、失效订阅清理（0.34.23 从 app.py 拆出）。
数据库访问由 app.py 启动时 bind(db, kv_get) 注入，本模块不 import app。"""
import os, json, time, asyncio

db = None
kv_get = None


def bind(db_fn, kv_get_fn):
    global db, kv_get
    db, kv_get = db_fn, kv_get_fn


# ---------------- Web Push：iPhone 主屏幕网页 App 的原生通知 ----------------
# VAPID 密钥第一次启动自动生成，存在数据库 kv 表里（不用配置）。订阅按设备存在 push_subs，
# 推送服务回 404/410（用户删了 App、关了通知、换了设备）就自动删掉。
VAPID_SUB = os.getenv("VAPID_SUB", "mailto:qunbao@users.noreply.github.com")   # 联系方式：mailto: 或不带路径的 https 域名；苹果拒收 localhost


def _b64u(b: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def vapid_keys() -> tuple[str, str]:
    """(私钥：32 字节 raw 的 base64url, 公钥：非压缩点的 base64url，给浏览器 applicationServerKey 用)"""
    v = kv_get("vapid")
    if v:
        d = json.loads(v)
        return d["priv"], d["pub"]
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    k = ec.generate_private_key(ec.SECP256R1())
    priv = _b64u(k.private_numbers().private_value.to_bytes(32, "big"))
    pub = _b64u(k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    with db() as c:  # 并发启动时只认第一份
        c.execute("INSERT OR IGNORE INTO kv(k,v) VALUES('vapid',?)", (json.dumps({"priv": priv, "pub": pub}),))
    d = json.loads(kv_get("vapid"))
    return d["priv"], d["pub"]


def _wp_send(sub: dict, data: str, priv: str):
    """真正发一条 Web Push（同步，放线程里跑）。测试里替换掉它。"""
    from pywebpush import webpush
    return webpush(sub, data, vapid_private_key=priv, vapid_claims={"sub": VAPID_SUB}, ttl=86400, timeout=10,
                   headers={"Urgency": "high"})


def _wp_status(ex) -> int:
    r = getattr(ex, "response", None)
    return int(getattr(r, "status_code", 0) or 0) if r is not None else 0


async def webpush_all(payload: dict, endpoint: str = "") -> int:
    """给所有订阅的设备（或指定的那一台）发一条，返回成功台数。失效的订阅自动删除。"""
    with db() as c:
        subs = c.execute("SELECT * FROM push_subs" + (" WHERE endpoint=?" if endpoint else ""),
                         (endpoint,) if endpoint else ()).fetchall()
    if not subs:
        return 0
    try:
        priv, _ = vapid_keys()
    except Exception as ex:
        print("Web Push 密钥不可用:", ex)
        return 0
    data = json.dumps(payload, ensure_ascii=False)

    def one(r):
        try:
            _wp_send({"endpoint": r["endpoint"], "keys": {"p256dh": r["p256dh"], "auth": r["auth"]}}, data, priv)
            return "ok"
        except ImportError:
            return "noimp"
        except Exception as ex:
            code = _wp_status(ex)
            if code in (404, 410):
                return "gone"
            print("Web Push 失败:", code or ex)
            return "err"
    res = await asyncio.gather(*(asyncio.to_thread(one, r) for r in subs))
    now = int(time.time())
    with db() as c:
        for r, x in zip(subs, res):
            if x == "gone":
                c.execute("DELETE FROM push_subs WHERE endpoint=?", (r["endpoint"],))
            elif x == "ok":
                c.execute("UPDATE push_subs SET ok_ts=?, fails=0 WHERE endpoint=?", (now, r["endpoint"]))
            elif x == "err":
                c.execute("UPDATE push_subs SET fails=fails+1 WHERE endpoint=?", (r["endpoint"],))
    if "noimp" in res:
        print("Web Push 需要 pywebpush：pip install pywebpush")
    return res.count("ok")


