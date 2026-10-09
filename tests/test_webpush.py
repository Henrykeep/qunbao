"""网页通知（Web Push）：密钥、订阅存取、失效清理、推送判定。不连真实推送服务（_wp_send 换成假的）。"""
import asyncio, base64, json, os, time

import pytest

from test_smoke import AUTH, app_mod, c  # 同一个进程、同一个临时库

EP = "https://web.push.apple.com/QF-dev-"


def sub(i=1):
    return {"endpoint": f"{EP}{i}", "keys": {"p256dh": "BP" + "a" * 85, "auth": "x" * 22}}


class FakeResp:
    def __init__(self, code):
        self.status_code = code


class FakeWPErr(Exception):
    def __init__(self, code):
        super().__init__(f"push failed {code}")
        self.response = FakeResp(code)


@pytest.fixture
def clean(monkeypatch):
    with app_mod.db() as cc:
        cc.execute("DELETE FROM push_subs")
        cc.execute("DELETE FROM pushed")
    app_mod.kv_set("held", "[]")
    app_mod._last_push.clear()
    app_mod.save_settings({"bark_url": "", "quiet_start": -1, "push_digest": True, "push_at": True})
    sent = []

    def fake_send(s, data, priv):
        d = json.loads(data)
        sent.append((s["endpoint"], d))
        code = fake_send.codes.get(s["endpoint"])
        if code:
            raise FakeWPErr(code)
        return FakeResp(201)
    fake_send.codes = {}
    monkeypatch.setattr(app_mod, "_wp_send", fake_send)
    yield sent, fake_send
    with app_mod.db() as cc:
        cc.execute("DELETE FROM push_subs")
    app_mod.save_settings({"bark_url": "", "quiet_start": -1})
    app_mod.kv_set("held", "[]")


def test_vapid_key_generated_once(clean):
    k1 = c.get("/api/push/key", headers=AUTH).json()["key"]
    k2 = c.get("/api/push/key", headers=AUTH).json()["key"]
    assert k1 == k2
    raw = base64.urlsafe_b64decode(k1 + "=" * (-len(k1) % 4))
    assert len(raw) == 65 and raw[0] == 4          # P-256 非压缩公钥，浏览器 applicationServerKey 要的格式
    priv, pub = app_mod.vapid_keys()
    assert pub == k1 and len(base64.urlsafe_b64decode(priv + "=" * (-len(priv) % 4))) == 32
    assert c.get("/api/push/key").status_code == 401


def test_vapid_key_works_with_pywebpush():
    pytest.importorskip("pywebpush")
    from py_vapid import Vapid
    priv, _ = app_mod.vapid_keys()
    v = Vapid.from_string(priv)                     # pywebpush 用同样的方式读私钥
    hdr = v.sign({"sub": app_mod.VAPID_SUB, "aud": "https://web.push.apple.com", "exp": int(time.time()) + 3600})
    assert hdr["Authorization"].startswith("vapid t=")


def test_subscription_store_and_delete(clean):
    assert c.post("/api/push/sub", headers=AUTH, json={"endpoint": "http://x", "keys": {}}).status_code == 400
    assert c.post("/api/push/sub", headers=AUTH, json=sub(1)).json()["subs"] == 1
    assert c.post("/api/push/sub", headers=AUTH, json=sub(2)).json()["subs"] == 2      # 多设备
    assert c.post("/api/push/sub", headers=AUTH, json=sub(1)).json()["subs"] == 2      # 同一设备覆盖，不重复
    r = c.get("/api/push/key", params={"endpoint": EP + "1"}, headers=AUTH).json()
    assert r["known"] and r["subs"] == 2
    assert not c.get("/api/push/key", params={"endpoint": EP + "9"}, headers=AUTH).json()["known"]
    assert c.request("DELETE", "/api/push/sub", headers=AUTH, json={"endpoint": EP + "1"}).json()["removed"] == 1
    assert c.post("/api/push/sub", headers=AUTH, json={**sub(2), "remove": True}).json()["subs"] == 0
    assert c.post("/api/push/sub", json=sub(3)).status_code == 401


def test_gone_subscriptions_removed(clean):
    sent, fake = clean
    for i in (1, 2, 3, 4):
        c.post("/api/push/sub", headers=AUTH, json=sub(i))
    fake.codes = {EP + "1": 410, EP + "2": 404, EP + "3": 500}
    n = asyncio.run(app_mod.webpush_all({"title": "t", "body": "b", "url": "/"}))
    assert n == 1 and len(sent) == 4
    with app_mod.db() as cc:
        left = {r["endpoint"]: r["fails"] for r in cc.execute("SELECT * FROM push_subs")}
    assert left == {EP + "3": 1, EP + "4": 0}       # 404/410 自动删除；临时 500 只记一次失败


def test_push_sends_web_and_bark_with_same_rules(clean, monkeypatch):
    sent, _ = clean
    bark = []

    async def fake_post(self, url, json=None):
        bark.append(json)
        return FakeResp(200)
    monkeypatch.setattr(app_mod.httpx.AsyncClient, "post", fake_post)
    assert asyncio.run(app_mod.push("a", "b")) is False and not sent           # 没有任何渠道
    c.post("/api/push/sub", headers=AUTH, json=sub(1))
    assert asyncio.run(app_mod.push("@你 · 计科2201", "王老师：交报告", key="QQ|计科2201", level="timeSensitive"))
    d = sent[-1][1]
    assert d["title"] == "@你 · 计科2201" and d["url"] == "/#chat/qq/%E8%AE%A1%E7%A7%912201" and isinstance(d["badge"], int)
    assert not bark                                                            # 没填 Bark 只发网页通知
    # 同一个群限频
    assert asyncio.run(app_mod.push("again", "x", key="QQ|计科2201")) is False
    # 关键词（passive）：Web Push 不发（只 Bark 静默）
    n = len(sent)
    asyncio.run(app_mod.push("关键词", "x", key="QQ|别的群", level="passive"))
    assert len(sent) == n
    # Bark 和 Web Push 同时发
    app_mod.save_settings({"bark_url": "http://bark/k"})
    assert asyncio.run(app_mod.push("两路", "x", force=True))
    assert bark[-1]["title"] == "两路" and sent[-1][1]["title"] == "两路"
    # 定时群报这类 web=False 只走 Bark
    n = len(sent)
    asyncio.run(app_mod.push("群报 · 3 件待办", "x", force=True, web=False))
    assert len(sent) == n
    # 免打扰：都不推，攒着
    from datetime import datetime
    h = datetime.now(app_mod.TZ).hour
    app_mod.save_settings({"bark_url": "", "quiet_start": h, "quiet_end": (h + 1) % 24})
    n = len(sent)
    assert asyncio.run(app_mod.push("夜里", "x", force=True)) is False and len(sent) == n
    assert app_mod._held_get()[-1] == ["夜里", "x"]
    app_mod.save_settings({"quiet_start": -1})
    assert asyncio.run(app_mod.flush_held()) == 1 and "免打扰期间" in sent[-1][1]["title"]


def _item(chat, title, source="QQ", due="", msg_ids="", at_me=0, urgency="mid"):
    now = int(time.time())
    with app_mod.db() as cc:
        return cc.execute("INSERT INTO items(source,chat,kind,title,due,status,first_ts,updated_ts,msg_ids,at_me,urgency) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (source, chat, "todo", title, due, "open", now, now, msg_ids, at_me, urgency)).lastrowid


def test_new_todo_push_once_and_skip_already_notified(clean):
    sent, _ = clean
    c.post("/api/push/sub", headers=AUTH, json=sub(1))
    i1 = _item("推送测试群A", "交实验报告", due="周五 23:59")
    t1 = {"id": i1, "title": "交实验报告", "chat": "推送测试群A", "source": "QQ", "due": "周五 23:59", "msg_ids": ""}
    assert asyncio.run(app_mod.push_new_todos([t1]))
    d = sent[-1][1]
    assert d["title"] == "推送测试群A" and "交实验报告" in d["body"] and "周五 23:59" in d["body"] and d["url"] == f"/#todo/{i1}"
    assert d["badge"] >= 1
    n = len(sent)
    assert not asyncio.run(app_mod.push_new_todos([t1])) and len(sent) == n         # 同一件事不推第二次
    # 来源消息已经作为 @我 即时推过：整理成待办时不再推
    asyncio.run(app_mod.push_msg(99001, "@你 · 推送测试群A", "x", key="QQ|推送测试群A", force=True))
    n = len(sent)
    i2 = _item("推送测试群A", "回复老师", msg_ids="99001")
    assert not asyncio.run(app_mod.push_new_todos([{"id": i2, "title": "回复老师", "chat": "推送测试群A", "msg_ids": "99001"}]))
    assert len(sent) == n
    # 几个群各出一件：合并成一条「新待办」
    i3, i4 = _item("推送测试群B", "交班费"), _item("推送测试群C", "填表")
    asyncio.run(app_mod.push_new_todos([{"id": i3, "title": "交班费", "chat": "推送测试群B"},
                                        {"id": i4, "title": "填表", "chat": "推送测试群C"}]))
    d = sent[-1][1]
    assert d["title"] == "新待办" and "交班费" in d["body"] and "填表" in d["body"] and len(sent) == n + 1


def test_refresh_live_pushes_only_watched_groups(clean, monkeypatch):
    sent, _ = clean
    c.post("/api/push/sub", headers=AUTH, json=sub(1))
    app_mod.set_modes([("QQ", "推送正常群"), ("QQ", "推送不看群"), ("QQ", "推送只看@我群")], "normal")
    app_mod.set_modes([("QQ", "推送不看群")], "off")
    app_mod.set_modes([("QQ", "推送只看@我群")], "atonly")
    asyncio.run(app_mod.refresh_live(model_head=False))     # 先把已有事项拼进当期
    n = len(sent)
    ok = _item("推送正常群", "下周一前交论文")
    _item("推送不看群", "不该推的事")
    _item("推送只看@我群", "也不该推")
    asyncio.run(app_mod.refresh_live(model_head=False))
    new = sent[n:]
    assert len(new) == 1 and new[0][1]["title"] == "推送正常群" and new[0][1]["url"] == f"/#todo/{ok}"
    asyncio.run(app_mod.refresh_live(model_head=False))     # 再拼一次：没有新事，不推
    assert len(sent) == n + 1
    # 关掉「出现新待办时通知」就不推
    app_mod.save_settings({"push_digest": False})
    _item("推送正常群", "又一件")
    asyncio.run(app_mod.refresh_live(model_head=False))
    assert len(sent) == n + 1


def test_open_todo_badge_excludes_unwatched(clean):
    app_mod.set_modes([("QQ", "角标不看群")], "off")
    base = app_mod.open_todo_count()
    _item("角标不看群", "x")
    assert app_mod.open_todo_count() == base
    _item("角标正常群", "y")
    assert app_mod.open_todo_count() == base + 1


def test_push_test_endpoint(clean):
    sent, fake = clean
    assert c.post("/api/push/test", headers=AUTH, json={"channel": "web"}).status_code == 502
    c.post("/api/push/sub", headers=AUTH, json=sub(1))
    r = c.post("/api/push/test", headers=AUTH, json={"channel": "web", "endpoint": EP + "1"})
    assert r.status_code == 200 and r.json()["sent"] == 1 and sent[-1][1]["tag"] == "test"
    fake.codes = {EP + "1": 410}
    assert c.post("/api/push/test", headers=AUTH, json={"channel": "web", "endpoint": EP + "1"}).status_code == 502
    assert app_mod.push_sub_count() == 0
    assert c.post("/api/push/test", headers=AUTH, json={"channel": "bark"}).status_code == 400   # Bark 没填


def test_reminder_works_with_only_web_push(clean, monkeypatch):
    """只开了网页通知、没填 Bark：截止提醒照样发，点开定位到那件待办。"""
    sent, _ = clean
    from datetime import datetime, timedelta
    c.post("/api/push/sub", headers=AUTH, json=sub(1))
    due = (datetime.now(app_mod.TZ) + timedelta(hours=1)).strftime("%m月%d日 %H:%M")
    i = _item("提醒测试群", "交表格", due=due)
    app_mod.save_settings({"remind_hours": 3})
    asyncio.run(app_mod.check_reminders())
    hit = [d for _, d in sent if "交表格" in d["title"]]
    assert hit and hit[0]["url"] == f"/#todo/{i}"
    with app_mod.db() as cc:
        cc.execute("UPDATE items SET status='done' WHERE id=?", (i,))


def test_frontend_and_sw_wiring():
    here = os.path.join(os.path.dirname(__file__), "..", "digest")
    sw = open(os.path.join(here, "sw.js"), encoding="utf-8").read()
    assert 'addEventListener("push"' in sw and "showNotification" in sw and "notificationclick" in sw
    assert "setAppBadge" in sw and "openWindow" in sw and "qb-nav" in sw
    html = open(os.path.join(here, "index.html"), encoding="utf-8").read()
    assert "Notification.requestPermission()" in html and "pushManager.subscribe" in html
    assert "添加到主屏幕" in html and "HTTPS" in html and "备用：Bark" in html and "#todo" in html
    req = open(os.path.join(here, "requirements.txt"), encoding="utf-8").read()
    assert "pywebpush" in req


def test_real_pywebpush_encrypts_and_410_cleanup():
    """不 mock pywebpush：本地起一个假的推送服务，真加密、真 VAPID 签名，解密核对内容；服务回 410 时订阅被删。"""
    pytest.importorskip("pywebpush")
    import http_ece, threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
    got, codes = [], {"/ok": 201, "/gone": 410}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            got.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
            self.send_response(codes[self.path]); self.end_headers()

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    bk = ec.generate_private_key(ec.SECP256R1())          # 「浏览器」这边的密钥
    pub = bk.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth = os.urandom(16)
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    with app_mod.db() as cc:
        cc.execute("DELETE FROM push_subs")
        for p in ("/ok", "/gone"):
            cc.execute("INSERT INTO push_subs(endpoint,p256dh,auth,ts) VALUES(?,?,?,?)", (base + p, b64(pub), b64(auth), 1))
    try:
        n = asyncio.run(app_mod.webpush_all({"title": "计科2201", "body": "新待办：交实验报告", "url": "/#todo/1"}))
        assert n == 1
        path, hdr, body = next(x for x in got if x[0] == "/ok")
        assert hdr["authorization"].lower().startswith("vapid t=") and hdr["ttl"] == "86400" and hdr["content-encoding"] == "aes128gcm"
        plain = json.loads(http_ece.decrypt(body, private_key=bk, auth_secret=auth, version="aes128gcm"))
        assert plain == {"title": "计科2201", "body": "新待办：交实验报告", "url": "/#todo/1"}
        with app_mod.db() as cc:
            assert [r[0] for r in cc.execute("SELECT endpoint FROM push_subs")] == [base + "/ok"]
    finally:
        srv.shutdown()
        with app_mod.db() as cc:
            cc.execute("DELETE FROM push_subs")
