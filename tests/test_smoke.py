"""冒烟测试：python -m pytest tests -q（需要 pip install fastapi httpx pytest）。不连真实大模型和 QQ。"""
import importlib, os, sys, tempfile, base64, json, time

os.environ.update(DB_PATH=os.path.join(tempfile.mkdtemp(), "t.db"), WEB_PASS="pw", INGEST_TOKEN="tok", LLM_API_KEY="")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "digest"))
app_mod = importlib.import_module("app")
from fastapi.testclient import TestClient

c = TestClient(app_mod.app)
AUTH = {"Authorization": "Basic " + base64.b64encode(b"me:pw").decode()}


def test_auth_required():
    assert c.get("/api/state").status_code == 401
    assert c.get("/api/state", headers=AUTH).status_code == 200


def test_onebot_group():
    e = {"post_type": "message", "message_type": "group", "group_id": 1, "self_id": 9, "user_id": 2,
         "sender": {"card": "王老师"}, "raw_message": "[CQ:at,qq=9] 周五前交报告", "time": 1}
    app_mod._group_names[1] = "计科2201"
    assert c.post("/onebot", json=e).status_code == 200
    ms = c.get("/api/messages?chat=计科2201", headers=AUTH).json()
    assert ms[-1]["text"] == "@我 周五前交报告" and ms[-1]["at_me"]


def test_wechat_formats():
    assert c.post("/ingest", json={"title": "x", "text": "y"}).status_code == 401
    r = c.post("/ingest?token=tok", json={"title": "班级群", "text": "[3条]李华: 明天 8 点集合"}).json()
    assert r["chat"] == "班级群" and r["sender"] == "李华"
    assert c.post("/ingest?token=tok", json={"title": "班级群", "text": "[3条]李华: 明天 8 点集合"}).json().get("dup")
    r = c.post("/ingest", headers={"X-Token": "tok"}, json={"title": "妈妈", "content": "吃饭了吗"}).json()
    assert r["chat"] == "私聊·妈妈"
    r = c.post("/ingest?token=tok", content="title=社团&content=%5B有人%40我%5D张三：@小明 来一下",
               headers={"content-type": "application/x-www-form-urlencoded"}).json()
    assert r["chat"] == "社团" and r["sender"] == "张三"
    assert c.post("/ingest?token=tok", json={"title": "微信", "text": "你收到了一条消息"}).json().get("skipped")
    ms = c.get("/api/messages?chat=社团&source=微信", headers=AUTH).json()
    assert ms[-1]["at_me"] and ms[-1]["text"] == "@小明 来一下"
    st = c.get("/api/state", headers=AUTH).json()["status"]
    assert st["wx"]["online"] and st["wx"]["ready"]
    chats = c.get("/api/chats?source=微信", headers=AUTH).json()
    assert {x["chat"] for x in chats} == {"班级群", "私聊·妈妈", "社团"}


def test_login_cookie_session():
    s = TestClient(app_mod.app)
    r = s.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert "密码" in s.get("/login").text
    assert s.post("/api/login", json={"user": "me", "password": "wrong"}).status_code == 401
    r = s.post("/api/login", json={"user": "me", "password": "pw"})
    assert r.status_code == 200 and "qb_session" in r.cookies
    assert "max-age=2592000" in r.headers["set-cookie"].lower() and "httponly" in r.headers["set-cookie"].lower()
    assert s.get("/api/state").status_code == 200
    assert s.get("/", follow_redirects=False).status_code == 200
    # 改密码后旧会话失效
    old = app_mod.WEB_PASS
    app_mod.WEB_PASS = "new"
    try:
        assert s.get("/api/state").status_code == 401
    finally:
        app_mod.WEB_PASS = old
    assert s.get("/api/state").status_code == 200
    s.post("/api/logout")
    s.cookies.clear()
    assert s.get("/api/state").status_code == 401


def test_login_rate_limit():
    s = TestClient(app_mod.app, headers={"x-forwarded-for": "9.9.9.9"})
    codes = [s.post("/api/login", json={"password": "x"}).status_code for _ in range(9)]
    assert codes[-1] == 429
    app_mod._fails.clear()


def test_parse_due_and_reminders(monkeypatch):
    import asyncio
    from datetime import datetime, timedelta
    now = datetime(2026, 10, 8, 12, 0, tzinfo=app_mod.TZ)
    p = app_mod.parse_due
    assert p("10月10日 23:59", now) == now.replace(day=10, hour=23, minute=59)
    assert p("明天 18:00", now) == now.replace(day=9, hour=18, minute=0)
    assert p("2026-10-09", now) == now.replace(day=9, hour=23, minute=59)
    assert p("明天下午3点", now).hour == 15
    assert p("", now) is None and p("尽快", now) is None
    sent = []

    async def fake(title, body, key="", force=False):
        sent.append(title)
        return True
    monkeypatch.setattr(app_mod, "push", fake)
    due = datetime.now(app_mod.TZ) + timedelta(hours=2)
    body = {"todos": [{"title": "交作业", "chat": "班级群", "due": due.strftime("%m月%d日 %H:%M")}]}
    app_mod.save_settings({"bark_url": "http://x", "remind_hours": 3})
    with app_mod.db() as c:
        c.execute("DELETE FROM reminded")
        c.execute("INSERT INTO digests(ts,hours,body) VALUES(?,?,?)", (int(time.time()), 24, json.dumps(body, ensure_ascii=False)))
    assert asyncio.run(app_mod.check_reminders()) == 1
    assert asyncio.run(app_mod.check_reminders()) == 0  # 只提醒一次
    assert sent and "交作业" in sent[0]
    app_mod.save_settings({"bark_url": "", "remind_hours": 3})


def test_quiet_hours(monkeypatch):
    import asyncio
    from datetime import datetime
    t = lambda h: datetime(2026, 10, 8, h, 0, tzinfo=app_mod.TZ)
    s = {"quiet_start": 23, "quiet_end": 7}
    assert app_mod.in_quiet(s, t(23)) and app_mod.in_quiet(s, t(3)) and not app_mod.in_quiet(s, t(7))
    assert not app_mod.in_quiet({"quiet_start": -1, "quiet_end": 7}, t(3))
    sent = []

    async def fake_post(self, url, json=None):
        sent.append(json)
        class R: status_code = 200
        return R()
    monkeypatch.setattr(app_mod.httpx.AsyncClient, "post", fake_post)
    app_mod._held.clear()
    app_mod.save_settings({"bark_url": "http://x/k", "quiet_start": 0, "quiet_end": 23})
    now_h = datetime.now(app_mod.TZ).hour
    if now_h == 23:
        app_mod.save_settings({"quiet_start": 1, "quiet_end": 23})
    assert asyncio.run(app_mod.push("a", "b")) is False and not sent and len(app_mod._held) == 1
    app_mod.save_settings({"quiet_start": -1})
    assert asyncio.run(app_mod.flush_held()) == 1 and "1 条" in sent[0]["title"]
    app_mod.save_settings({"bark_url": ""})


def test_digest_hours():
    assert app_mod.digest_hours({"digest_hour": 21, "digest_hour2": 8}) == {8, 21}
    assert app_mod.digest_hours({"digest_hour": 21, "digest_hour2": -1}) == {21}


def test_ics():
    r = c.get("/api/ics", params={"title": "交表, 谢谢", "due": "2026-10-10 18:00", "chat": "班级群"}, headers=AUTH)
    assert r.status_code == 200 and "text/calendar" in r.headers["content-type"]
    assert "BEGIN:VEVENT" in r.text and "DTSTART:20261010T100000Z" in r.text and "交表\\, 谢谢" in r.text
    assert "VALUE=DATE" in c.get("/api/ics", params={"title": "x", "due": "随时"}, headers=AUTH).text
    assert c.get("/api/ics", headers=AUTH).status_code == 400


def test_group_levels():
    s = dict(app_mod.DEFAULTS, levels={"静群": "atonly", "要紧群": "important"})
    assert app_mod.hit_reason("静群", "a", "截止明天", False, s) is None
    assert app_mod.hit_reason("静群", "a", "x", True, s) == "@了你"
    assert app_mod.hit_reason("普通群", "a", "截止明天", False, s)
    c.post("/api/settings", headers=AUTH, json={"levels": {"静群": "atonly", "要紧群": "important"}})
    now = int(time.time())
    for ch, at in (("静群", False), ("静群", True), ("要紧群", False)):
        app_mod.db().execute("INSERT INTO msgs(ts,source,chat,sender,text,at_me) VALUES(?,?,?,?,?,?)",
                             (now, "QQ", ch, "u", f"lvtest{at}", int(at))).connection.commit()
    rows, text = app_mod.transcript(1)
    assert "lvtestFalse" in text and text.count("lvtestTrue") == 1
    assert "【重要群】" in text and sum(1 for r in rows if r["chat"] == "静群") == 1
    assert "要紧群" in app_mod.about_me(app_mod.settings())


def test_image_thumbs():
    e = {"post_type": "message", "message_type": "group", "group_id": 7, "self_id": 9, "user_id": 2,
         "sender": {"card": "小李"}, "time": 5,
         "raw_message": "看这个[CQ:image,file=a.jpg,url=https://x.cn/a.jpg?k=1&amp;b=2][CQ:image,file=b,url=javascript:x]"}
    app_mod._group_names[7] = "图片群"
    assert c.post("/onebot", json=e).status_code == 200
    m = c.get("/api/messages?chat=图片群", headers=AUTH).json()[-1]
    assert m["imgs"] == ["https://x.cn/a.jpg?k=1&b=2"] and "[图片]" in m["text"]
    assert c.get("/api/messages?chat=计科2201", headers=AUTH).json()[0]["imgs"] == []


def test_messages_filter_sender_since():
    r = c.get("/api/messages?sender=zzzz_none&since=1", headers=AUTH)
    assert r.status_code == 200 and r.json() == []
    r = c.get(f"/api/messages?until=1", headers=AUTH)
    assert r.json() == []


def test_service_worker():
    r = c.get("/sw.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"] and "fetch" in r.text
