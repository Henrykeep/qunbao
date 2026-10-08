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


def test_weekly_digest_setting():
    assert app_mod.settings()["weekly_digest"] is True
    c.post("/api/settings", json={"weekly_digest": False}, headers=AUTH)
    assert c.get("/api/settings", headers=AUTH).json()["weekly_digest"] is False
    c.post("/api/settings", json={"weekly_digest": True}, headers=AUTH)


def test_export_and_keep_days():
    r = c.get("/api/export", headers=AUTH)
    j = r.json()
    assert "attachment" in r.headers["content-disposition"] and "messages" in j and "bark_url" not in j["settings"]
    c.post("/api/settings", json={"keep_days": 7}, headers=AUTH)
    assert c.get("/api/settings", headers=AUTH).json()["keep_days"] == 7
    c.post("/api/settings", json={"keep_days": 30}, headers=AUTH)


def test_llm_retry_and_error_state():
    import asyncio, httpx
    app_mod.LLM_KEY = "k"; app_mod.LLM_RETRY_WAIT = 0
    calls = []

    class Bad:
        def __init__(s, *a, **k): pass
        async def __aenter__(s): return s
        async def __aexit__(s, *a): pass
        async def post(s, *a, **k):
            calls.append(1)
            raise httpx.ConnectError("boom")
    orig = httpx.AsyncClient
    app_mod.httpx.AsyncClient = Bad
    try:
        try:
            asyncio.run(app_mod.llm([{"role": "user", "content": "x"}]))
            assert False
        except Exception as ex:
            assert ex.status_code == 502
    finally:
        app_mod.httpx.AsyncClient = orig; app_mod.LLM_KEY = ""
    assert len(calls) == 2
    st = c.get("/api/state", headers=AUTH).json()["status"]
    assert "连不上" in st["llm_err"]


def test_links_extracted():
    rows = [{"text": "看这个 https://a.com/x?y=1，还有 https://b.org。", "chat": "g", "sender": "u", "ts": 1},
            {"text": "重复 https://a.com/x?y=1", "chat": "g", "sender": "u", "ts": 2}]
    out = app_mod.extract_links(rows)
    assert [l["url"] for l in out] == ["https://a.com/x?y=1", "https://b.org"]
    now = int(time.time())
    with app_mod.db() as cx:
        cx.execute("INSERT INTO msgs(ts,chat,sender,text,source) VALUES(?,?,?,?,?)",
                   (now, "链接群", "甲", "资料 https://example.com/doc", "QQ"))
    assert any(l["url"] == "https://example.com/doc" for l in c.get("/api/state", headers=AUTH).json()["links"])


def test_whitelist_mode():
    s = {"muted": ["甲"], "only_mode": False, "allowed": ["乙"]}
    assert app_mod.is_muted("甲", s) and not app_mod.is_muted("乙", s)
    s["only_mode"] = True
    assert app_mod.is_muted("甲", s) and not app_mod.is_muted("乙", s) and app_mod.is_muted("丙", s)


def test_todo_snooze():
    r = c.post("/api/todo/snooze", headers=AUTH, json={"key": "交报告|群", "title": "交报告", "hours": 1}).json()
    assert r["ok"] and r["until"] > time.time()
    c.post("/api/todo/snooze", headers=AUTH, json={"key": "a|b", "hours": "tomorrow"})
    with app_mod.db() as cn:
        assert cn.execute("SELECT COUNT(*) n FROM snooze").fetchone()["n"] == 2


def test_sender_filter_ui():
    r = c.get("/", headers=AUTH)
    assert "sfchip" in r.text and "applySF" in r.text


def test_chat_find_ui():
    r = c.get("/", headers=AUTH)
    assert "cfq" in r.text and "cfind" in r.text


def test_todo_pin():
    assert c.post("/api/todo", headers=AUTH, json={"key": "k|g", "pin": True}).json()["ok"]
    assert "k|g" in c.get("/api/state", headers=AUTH).json()["pins"]
    c.post("/api/todo", headers=AUTH, json={"key": "k|g", "pin": False})
    assert "k|g" not in c.get("/api/state", headers=AUTH).json()["pins"]
    assert "pinb" in c.get("/", headers=AUTH).text


def test_weekly_title_and_prompt():
    assert app_mod.weekly_title({"todos": []}) == "本周群报"
    assert app_mod.weekly_title({"todos": [1, 2]}) == "本周群报 · 2 件待办"
    seen = {}
    async def fake(msgs, as_json=False):
        seen["u"] = msgs[-1]["content"]; return '{"headline":"h","todos":[],"notices":[],"groups":[]}'
    app_mod.llm = fake
    import asyncio
    c.post("/onebot", json={"post_type": "message", "message_type": "private", "user_id": 5, "self_id": 9,
                            "sender": {"nickname": "a"}, "raw_message": "hi", "time": int(time.time())})
    asyncio.run(app_mod.make_digest(168))
    assert "一周汇总" in seen["u"]


def test_activity():
    r = c.get("/api/activity", headers=AUTH).json()
    assert len(r["hours"]) == 24 and r["total"] == sum(r["hours"])
    assert isinstance(r["top"], list)
    assert "loadAct" in c.get("/", headers=AUTH).text


def test_my_names_mention():
    import asyncio
    c.post("/api/settings", headers=AUTH, json={"my_names": ["小李"]})
    assert app_mod.mentions_me("@小李 来一下", app_mod.settings())
    assert app_mod.mentions_me("＠小李\u2005看看", app_mod.settings())
    assert not app_mod.mentions_me("小李在吗", app_mod.settings())
    asyncio.run(app_mod.save("微信", "测试群X", "老王", "@小李 明天交表", None, False))
    r = app_mod.db().execute("SELECT at_me FROM msgs WHERE chat='测试群X'").fetchone()
    assert r["at_me"] == 1


def test_keyword_hit_reason_multi():
    s = {"vip": [], "keywords": ["截止", "DDL", "报名"], "muted": [], "levels": {}, "only_mode": False, "allowed": []}
    r = app_mod.hit_reason("群", "a", "报名截止 ddl 今天", False, s)
    assert r == "关键词「截止」、「DDL」、「报名」"


def test_done_fold_ui():
    html = open(os.path.join(os.path.dirname(__file__), "..", "digest", "index.html"), encoding="utf-8").read()
    assert "donebar" in html and "todos.fold" in html


def test_todo_diff():
    a = {"todos": [{"title": "A", "chat": "g"}, {"title": "B", "chat": "g"}]}
    b = {"todos": [{"title": "B", "chat": "g"}, {"title": "C", "chat": "g"}]}
    assert app_mod.todo_diff(b, a) == {"new": ["C"], "gone": 1, "kept": 1}
    assert app_mod.todo_diff(b, None) is None
    assert "diff" in c.get("/api/state", headers=AUTH).json()


def test_offline_bar():
    html = open(os.path.join(os.path.dirname(__file__), "..", "digest", "index.html"), encoding="utf-8").read()
    assert 'id="offbar"' in html and "addEventListener(\"offline\"" in html
    assert app_mod.VERSION in open(os.path.join(os.path.dirname(__file__), "..", "digest", "sw.js")).read()


# ---------------- 0.30.0 体验修复 ----------------
def test_fuzzy_same_todo():
    T = lambda t, ch="计科2201", d="": {"title": t, "chat": ch, "due": d}
    st = app_mod.same_todo
    assert st(T("交数据库实验报告"), T("提交数据库实验报告"))
    assert st(T("交数据库实验报告"), T("把实验报告交到学习通"))
    assert st(T("交数据库实验报告", d="10月10日 23:59"), T("提交实验报告到学习通", d="10月10日 23:59"))
    assert not st(T("交数据库实验报告"), T("参加线上班会"))
    assert not st(T("交班费", d="10月10日 23:59"), T("交实验报告", d="10月10日 23:59"))
    assert not st(T("交报告"), T("交报告", "家族群"))  # 不同群不算
    assert not st(T("交第一章作业"), T("交第二章作业")) and not st(T("任务1号要做的事"), T("任务2号要做的事"))
    assert st(T("交第一章作业"), T("提交第一章作业"))


def _fresh():
    """隔离：把水位设到当前最后一条消息，清空事项/群状态。"""
    with app_mod.db() as x:
        x.execute("DELETE FROM items"); x.execute("DELETE FROM chat_state")
        x.execute("DELETE FROM kv WHERE k IN ('auto_try')")
        top = x.execute("SELECT MAX(id) i FROM msgs").fetchone()["i"] or 0
    app_mod.kv_set("scan_id", top)
    app_mod.save_settings({"muted": [], "only_mode": False, "levels": {}})


class FakeLLM:
    """按群回答的假模型：记录每次调用的群名和输入。"""
    def __init__(self, per_chat):
        self.per_chat, self.calls = per_chat, []

    async def __call__(self, msgs, as_json=False):
        sysm, user = msgs[0]["content"], msgs[-1]["content"]
        if sysm.startswith("【群更新】"):
            chat = sysm.split("「", 1)[1].split("」", 1)[0]
            self.calls.append(("chat", chat, user))
            r = self.per_chat.get(chat, {})
            return json.dumps(r(user) if callable(r) else r, ensure_ascii=False)
        self.calls.append(("head", "", user))
        return "测试头条"

    def chats(self):
        return [c for k, c, _ in self.calls if k == "chat"]


def _ingest(chat, text, sender="甲"):
    c.post("/ingest?token=tok", json={"chat": chat, "sender": sender, "text": text})


def test_noise_filter():
    s = dict(app_mod.settings(), keywords=["奖学金"], vip=["王老师"], levels={"重要群": "important", "静群": "atonly"})
    R = lambda text, chat="普通群", at=0, sender="甲": {"text": text, "chat": chat, "at_me": at, "sender": sender}
    cl = lambda *a, **k: app_mod.classify(R(*a, **k), s)
    for t in ["收到", "好的", "哈哈哈哈", "+1", "1", "666", "[图片]", "[表情][表情]", "👍👍", "嗯嗯", "张三撤回了一条消息", "ok"]:
        assert cl(t) == "drop", t
    assert cl("收到", at=1) == "key"                        # @我 永远保留
    assert cl("明天 18:00 交材料") == "key"                 # 时间
    assert cl("班费 50 元") == "key" and cl("看这个 https://a.cn") == "key"
    assert cl("奖学金名单出来了") == "key"                   # 关键词
    assert cl("大家周末愉快呀", sender="王老师") == "key"     # 重要的人
    assert cl("这个电影挺好看的") == "keep"
    assert cl("这个电影挺好看的", chat="重要群") == "key"
    assert cl("这个电影挺好看的", chat="静群") == "drop"


def test_incremental_per_chat_and_done_stays():
    import asyncio
    _fresh()
    old = app_mod.llm
    fake = FakeLLM({
        "增量A": lambda u: {"summary": "实验报告", "new": [{"kind": "todo", "title": "交数据库实验报告", "due": "10月10日 23:59", "urgency": "high"}]}
        if "提交" not in u else
        # 第二次：模型漏传 id，把已完成的事换个说法又报成 new；还试图更新不存在的 id
        {"summary": "实验报告", "new": [{"kind": "todo", "title": "提交数据库实验报告", "due": "10月10日 23:59"}], "update": [{"id": 999999, "detail": "x"}]},
        "增量B": {"summary": "骑行", "new": [{"kind": "notice", "title": "周六骑行改到东门"}]},
    })
    app_mod.llm = fake
    try:
        _ingest("增量A", "周五晚 23:59 前交数据库实验报告", "王老师")
        _ingest("增量A", "收到")                              # 噪音
        _ingest("增量B", "周六骑行改到东门集合")
        st = c.get("/api/state", headers=AUTH).json()
        assert st["pending"] == {"msgs": 2, "chats": 2}       # 噪音不算
        d = c.post("/api/digest", headers=AUTH, json={"hours": 24}).json()
        assert sorted(fake.chats()) == ["增量A", "增量B"] and d["headline"] == "测试头条"
        assert d["stats"]["new"] == 3 and d["stats"]["sent"] == 2 and d["stats"]["chats"] == 2
        a_user = [u for k, ch, u in fake.calls if ch == "增量A"][0]
        assert "收到" not in a_user                          # 噪音没送模型
        t = [x for x in d["todos"] if x["chat"] == "增量A"][0]
        assert t["key"] == f"item:{t['id']}" and not t["done"]
        c.post("/api/todo", headers=AUTH, json={"key": t["key"], "done": True})
        # 只有 A 有新消息：B 零调用；A 只收到新消息 + 旧状态（含 done_recent）
        fake.calls.clear()
        _ingest("增量A", "提交方式改成学习通，别忘了")
        d2 = c.post("/api/digest", headers=AUTH, json={"hours": 24}).json()
        assert fake.chats() == ["增量A"]
        u = fake.calls[0][2]
        assert "提交方式改成学习通" in u and "周五晚 23:59 前交" not in u and "交数据库实验报告" in u and "done_recent" in u
        with app_mod.db() as x:
            rows = x.execute("SELECT * FROM items WHERE chat='增量A' AND kind='todo'").fetchall()
        assert len(rows) == 1 and rows[0]["status"] == "done"  # 没复活、没重复
        st = c.get("/api/state", headers=AUTH).json()
        assert all(x["done"] for x in st["digest"]["todos"] if x["chat"] == "增量A")
        # 没有新消息：直接返回上一期，不调模型
        fake.calls.clear()
        d3 = c.post("/api/digest", headers=AUTH, json={"hours": 24}).json()
        assert fake.calls == [] and d3.get("unchanged") and d3["id"] == d2["id"]
        # 只有噪音：也不调模型
        _ingest("增量B", "哈哈哈"); _ingest("增量B", "[图片]")
        d4 = c.post("/api/digest", headers=AUTH, json={"hours": 24}).json()
        assert fake.calls == [] and d4.get("unchanged")
        # 截止提醒挂在事项 id 上：已完成的不提醒
        sent = []
        async def fp(title, body, key="", force=False, test=False):
            sent.append(title); return True
        op = app_mod.push; app_mod.push = fp
        app_mod.save_settings({"bark_url": "http://x", "remind_hours": 24 * 400})
        try:
            asyncio.run(app_mod.check_reminders())
            assert not any("实验报告" in x for x in sent)
        finally:
            app_mod.push = op; app_mod.save_settings({"bark_url": "", "remind_hours": 3})
    finally:
        app_mod.llm = old


def test_id_update_no_duplicates():
    _fresh()
    old = app_mod.llm
    step = {"n": 0}
    def ans(u):
        step["n"] += 1
        if step["n"] == 1:
            return {"new": [{"kind": "todo", "title": "交体测表", "due": "", "detail": "交给班长"}]}
        iid = json.loads(u.split("旧状态：", 1)[1].split("\n新消息", 1)[0])["open_items"][0]["id"]
        return {"update": [{"id": iid, "due": "10月12日 18:00", "title": "改名也没用"}],
                "new": [{"kind": "todo", "title": "交体测表", "detail": "改交给体委"}]}  # 同一件事又报一遍
    app_mod.llm = FakeLLM({"体育群": ans})
    try:
        _ingest("体育群", "下周交体测表，交给班长")
        c.post("/api/digest", headers=AUTH, json={"hours": 24})
        _ingest("体育群", "体测表 10月12日 18:00 前交给体委")
        c.post("/api/digest", headers=AUTH, json={"hours": 24})
        with app_mod.db() as x:
            rows = x.execute("SELECT * FROM items WHERE chat='体育群'").fetchall()
        assert len(rows) == 1
        r = rows[0]
        assert r["title"] == "交体测表" and r["due"] == "10月12日 18:00" and r["detail"] == "改交给体委"
    finally:
        app_mod.llm = old


def test_chunked_merge():
    _fresh()
    old, oc = app_mod.llm, app_mod.CHUNK_MSGS
    seen = []
    def ans(u):
        st = json.loads(u.split("旧状态：", 1)[1].split("\n新消息", 1)[0])
        seen.append((len(st["open_items"]), u.count("\n#")))
        return {"summary": f"第{len(seen)}块", "new": [{"kind": "todo", "title": f"任务{len(seen)}号要做的事"}]}
    app_mod.llm, app_mod.CHUNK_MSGS = FakeLLM({"大群": ans}), 3
    try:
        for i in range(7):
            _ingest("大群", f"第 {i} 条：明天上午 9 点讨论方案 {i}")
        d = c.post("/api/digest", headers=AUTH, json={"hours": 24}).json()
        assert [n for _, n in seen] == [3, 3, 1]          # 7 条分成 3 块
        assert [k for k, _ in seen] == [0, 1, 2]          # 后一块带着前一块的结果逐块合并
        assert len([t for t in d["todos"] if t["chat"] == "大群"]) == 3
        with app_mod.db() as x:
            assert x.execute("SELECT summary FROM chat_state WHERE chat='大群'").fetchone()["summary"] == "第3块"
    finally:
        app_mod.llm, app_mod.CHUNK_MSGS = old, oc


def test_full_rebuild_keeps_ids():
    _fresh()
    old = app_mod.llm
    app_mod.llm = FakeLLM({"重建群": {"new": [{"kind": "todo", "title": "交实验报告"}]}})
    try:
        _ingest("重建群", "周五交实验报告")
        d = c.post("/api/digest", headers=AUTH, json={"hours": 24}).json()
        i1 = [t["id"] for t in d["todos"] if t["chat"] == "重建群"]
        app_mod.llm = FakeLLM({"重建群": {"new": [{"kind": "todo", "title": "提交实验报告"}]}})
        d = c.post("/api/digest", headers=AUTH, json={"hours": 24, "full": True}).json()
        assert [t["id"] for t in d["todos"] if t["chat"] == "重建群"] == i1 and d["mode"] == "full"
    finally:
        app_mod.llm = old


def test_weekly_uses_items_not_raw():
    old = app_mod.llm
    fake = FakeLLM({})
    app_mod.llm = fake
    try:
        _fresh()
        with app_mod.db() as x:
            x.execute("INSERT INTO items(source,chat,kind,title,status,first_ts,updated_ts) VALUES('QQ','周群','todo','写周记','open',?,?)",
                      (int(time.time()), int(time.time())))
        _ingest("周群", "这句原话只在原文里出现ABCXYZ，明天交")
        app_mod.llm = FakeLLM({"周群": {"summary": "周记"}})
        fake = app_mod.llm
        import asyncio
        d = asyncio.run(app_mod.make_digest(168))
        head = [u for k, _, u in fake.calls if k == "head"][0]
        assert "一周汇总" in head and "写周记" in head and "ABCXYZ" not in head
        assert d["mode"] == "weekly"
    finally:
        app_mod.llm = old


def test_migrate_old_digest():
    _fresh()
    with app_mod.db() as x:
        x.execute("DELETE FROM kv WHERE k='migrated_items'")
        x.execute("DELETE FROM todo_done")
        x.execute("INSERT INTO todo_done(k,ts) VALUES('交旧报告|旧群',?)", (int(time.time()),))
        x.execute("INSERT INTO digests(ts,hours,body) VALUES(?,?,?)", (int(time.time()), 24, json.dumps(
            {"headline": "h", "todos": [{"title": "交旧报告", "chat": "旧群"}, {"title": "买车票", "chat": "旧群"}],
             "notices": [{"title": "停水通知", "chat": "旧群"}], "groups": [{"chat": "旧群", "gist": "旧要点"}]}, ensure_ascii=False)))
    assert app_mod.migrate_items() == 3
    with app_mod.db() as x:
        st = {r["title"]: r["status"] for r in x.execute("SELECT * FROM items")}
        assert st == {"交旧报告": "done", "买车票": "open", "停水通知": "open"}
        assert x.execute("SELECT summary FROM chat_state WHERE chat='旧群'").fetchone()["summary"] == "旧要点"
    assert app_mod.migrate_items() == 0  # 只迁移一次


def test_pin_fuzzy():
    t = {"title": "交体测表", "chat": "体育群", "due": ""}
    c.post("/api/todo", headers=AUTH, json={"key": "交体测表|体育群", "pin": True, **t})
    body = app_mod.annotate_todos({"todos": [{"title": "提交体测表", "chat": "体育群"}]})
    assert body["todos"][0]["pinned"]
    c.post("/api/todo", headers=AUTH, json={"key": "提交体测表|体育群", "pin": False, "title": "提交体测表", "chat": "体育群"})
    assert not app_mod.annotate_todos({"todos": [{"title": "交体测表", "chat": "体育群"}]})["todos"][0]["pinned"]


def test_auto_digest_conditions():
    _fresh()
    with app_mod.db() as db_:
        db_.execute("DELETE FROM digests")
        db_.execute("INSERT INTO digests(ts,hours,body) VALUES(?,?,?)", (int(time.time()) - 600, 24, json.dumps({"todos": []})))
    s = dict(app_mod.settings(), auto_interval=30, muted=["吵闹群"], only_mode=False, levels={"重要群": "important"})
    chk = app_mod.auto_check
    later = lambda m: time.time() + m * 60
    assert chk(s) is None                                   # 没有新消息
    _ingest("吵闹群", "今天下午 3 点开会")
    _ingest("普通群", "哈哈哈")
    assert chk(s, now=later(60)) is None                    # 屏蔽群 / 噪音不算
    _ingest("普通群", "周末一起去爬山吗大家")
    assert chk(s) is None                                   # 才过 10 分钟 < 30
    assert chk(s, now=time.time() + 60) is None             # 间隔没到
    assert chk(s, now=later(25)) == ("interval", 24)        # 间隔到了且已安静 2 分钟
    assert chk(dict(s, auto_interval=0), now=later(999)) is None
    with app_mod.db() as db_:                               # 防抖：消息还在刷（刚刚还有）就先等
        db_.execute("UPDATE msgs SET ts=? WHERE chat='普通群'", (int(later(25)) - 30,))
    assert chk(s, now=later(25)) is None
    for i in range(50):
        _ingest("普通群", f"刷屏消息第{i}条内容比较长")
    with app_mod.db() as db_:
        db_.execute("UPDATE msgs SET ts=? WHERE chat='普通群'", (int(later(25)) - 30,))
    assert chk(s, now=later(25)) == ("interval", 24)        # 累计 ≥50 条不等安静
    _fresh()
    with app_mod.db() as db_:
        db_.execute("UPDATE digests SET ts=?", (int(time.time()) - 600,))
    _ingest("重要群", "这个周末的安排大家看一下")
    assert chk(s) == ("urgent", 24)                         # 重要群：过了 5 分钟就提前
    with app_mod.db() as db_:
        db_.execute("UPDATE digests SET ts=?", (int(time.time()) - 120,))
    assert chk(s) is None                                   # 至少间隔 5 分钟
    with app_mod.db() as db_:
        db_.execute("UPDATE digests SET ts=?, hours=72", (int(time.time()) - 3600,))
    app_mod.kv_set("auto_try", int(time.time()))
    assert chk(s) is None                                   # 刚试过（可能失败），不每分钟重试
    with app_mod.db() as db_:
        db_.execute("DELETE FROM kv WHERE k='auto_try'")
    assert chk(s)[1] == 72                                  # 时间窗口跟上一次一致
    # 自动整理：免打扰时段只更新不推送；标记 auto
    import asyncio
    sent = []
    async def fake_push(*a, **k):
        sent.append(a); return True
    ol, op = app_mod.llm, app_mod.push
    app_mod.llm, app_mod.push = FakeLLM({"重要群": {"new": [{"kind": "todo", "title": "看周末安排"}]}}), fake_push
    try:
        h = __import__("datetime").datetime.now(app_mod.TZ).hour
        q = dict(s, bark_url="http://x", push_digest=True, quiet_start=h, quiet_end=(h + 1) % 24)
        d = asyncio.run(app_mod.auto_digest(q))
        assert d and d["auto"] and not sent
        assert app_mod.auto_check(q) is None  # 整理完就没有待整理的消息了
        assert c.get("/api/digests", headers=AUTH).json()[0]["auto"]
    finally:
        app_mod.llm, app_mod.push = ol, op


def test_settings_groups_keep_fields():
    st = c.get("/api/settings", headers=AUTH).json()
    assert set(app_mod.DEFAULTS) <= set(st) and st["auto_interval"] in app_mod.AUTO_CHOICES
    before = dict(st)
    r = c.post("/api/settings", headers=AUTH, json={"auto_interval": 15}).json()
    assert r["auto_interval"] == 15 and all(r[k] == before[k] for k in before if k != "auto_interval")
    assert c.post("/api/settings", headers=AUTH, json={"auto_interval": 7}).json()["auto_interval"] == 30  # 非法值回默认
    c.post("/api/settings", headers=AUTH, json={"auto_interval": before["auto_interval"]})
    html = c.get("/", headers=AUTH).text
    # 每个设置项都还在页面上有入口（重组后不丢字段）
    for k in app_mod.DEFAULTS:
        assert f"SET.{k}" in html or f'data-k="{k}"' in html or f"SET[k]" in html and k in ("muted", "allowed"), k
    for sub in ("me", "groups", "kw", "remind", "conn", "data", "acct", "about"):
        assert f'id="sub-{sub}"' in html and f'data-sub="{sub}"' in html
    assert "refreshQuiet" in html and "已自动更新" in html and "visibilitychange" in html


def test_tidy_headline_never_cuts_mid_word():
    import importlib, sys
    app = sys.modules.get("app") or importlib.import_module("app")
    h = app.tidy_headline("Claude Max 5.5使用异常，Pro Api中转群反馈渠道、生图问题，待处理事项较多需要关注")
    assert h == "Claude Max 5.5使用异常，Pro Api中转群反馈渠道" or not h.endswith("待")
    assert not h.endswith("，") and len(h) <= 30
    assert app.tidy_headline("「周五前交实验报告」") == "周五前交实验报告"
    long = app.tidy_headline("一" * 50)
    assert long.endswith("…") and len(long) <= 30
