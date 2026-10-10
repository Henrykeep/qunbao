"""冒烟测试：python -m pytest tests -q（需要 pip install fastapi httpx pytest）。不连真实大模型和 QQ。"""
import importlib, os, re, sys, tempfile, base64, json, time

os.environ.update(DB_PATH=os.path.join(tempfile.mkdtemp(), "t.db"), WEB_PASS="pw", INGEST_TOKEN="tok", LLM_API_KEY="")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "digest"))
app_mod = importlib.import_module("app")
REAL_LLM = app_mod.llm
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

    async def fake(title, body, key="", force=False, **k):
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
    app_mod.kv_set("held", "[]")
    now_h = datetime.now(app_mod.TZ).hour  # 免打扰设成「当前这个小时」，不受跑测试的时刻影响
    app_mod.save_settings({"bark_url": "http://x/k", "quiet_start": now_h, "quiet_end": (now_h + 1) % 24})
    assert asyncio.run(app_mod.push("a", "b")) is False and not sent and len(app_mod._held_get()) == 1
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
    s = dict(app_mod.DEFAULTS, modes={"QQ|静群": "atonly", "QQ|要紧群": "focus"})
    assert app_mod.hit_reason("静群", "a", "截止明天", False, s) is None
    assert app_mod.hit_reason("静群", "a", "x", True, s)[0] == "@你"
    assert app_mod.hit_reason("普通群", "a", "截止明天", False, s)
    app_mod.set_modes([("QQ", "静群")], "atonly"); app_mod.set_modes([("QQ", "要紧群")], "focus")
    now = int(time.time())
    for ch, at in (("静群", False), ("静群", True), ("要紧群", False)):
        app_mod.db().execute("INSERT INTO msgs(ts,source,chat,sender,text,at_me) VALUES(?,?,?,?,?,?)",
                             (now, "QQ", ch, "u", f"lvtest{at}", int(at))).connection.commit()
    rows, text = app_mod.transcript(1)
    # 0.31：只看@我 的群不进整理/问答原文（@我 的消息在首页「@我」里看）
    assert "lvtestFalse" in text and "lvtestTrue" not in text
    assert "【重点群】" in text and sum(1 for r in rows if r["chat"] == "静群") == 0
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
    assert c.get("/api/state", headers=AUTH).json()["status"]["llm_err"] == ""   # 偶发失败不立刻挂黄条
    app_mod.LLM_STATE["fail_since"] = time.time() - 400
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


def test_legacy_whitelist_maps_to_modes():
    s = {"muted": ["甲"], "only_mode": False, "allowed": ["乙"], "modes": {}}
    assert app_mod.chat_mode("QQ", "甲", s) == "off" and app_mod.chat_mode("QQ", "乙", s) == "normal"
    s["only_mode"] = True
    assert app_mod.chat_mode("QQ", "甲", s) == "off" and app_mod.chat_mode("QQ", "乙", s) == "normal" and app_mod.chat_mode("QQ", "丙", s) == "off"


def test_todo_snooze():
    r = c.post("/api/todo/snooze", headers=AUTH, json={"key": "交报告|群", "title": "交报告", "hours": 1}).json()
    assert r["ok"] and r["until"] > time.time()
    c.post("/api/todo/snooze", headers=AUTH, json={"key": "a|b", "hours": "tomorrow"})
    with app_mod.db() as cn:
        assert cn.execute("SELECT COUNT(*) n FROM snooze").fetchone()["n"] == 2


def test_snooze_tomorrow_after_midnight_and_late_warning():
    from datetime import datetime, timedelta
    tz = app_mod.TZ
    # 凌晨 1 点说「明天」= 睡醒后的今早 9 点；白天说才是明早
    assert app_mod.snooze_until("tomorrow", datetime(2026, 10, 10, 1, 0, tzinfo=tz)) == datetime(2026, 10, 10, 9, 0, tzinfo=tz)
    assert app_mod.snooze_until("tomorrow", datetime(2026, 10, 9, 22, 0, tzinfo=tz)) == datetime(2026, 10, 10, 9, 0, tzinfo=tz)
    assert app_mod.snooze_until(3, datetime(2026, 10, 9, 22, 0, tzinfo=tz)) == datetime(2026, 10, 10, 1, 0, tzinfo=tz)
    # 提醒时间晚于截止：明说，不假装设好了
    soon = (datetime.now(tz) + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M")
    r = c.post("/api/todo/snooze", headers=AUTH, json={"key": "晚了|群", "title": "晚了", "hours": 3, "due": soon}).json()
    assert r["late"] and "已过截止" in r["msg"]
    r = c.post("/api/todo/snooze", headers=AUTH, json={"key": "来得及|群", "title": "来得及", "hours": 1, "due": ""}).json()
    assert not r["late"] and "1 小时后" in r["msg"]


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
    assert r == ("关键词「截止、DDL」", "passive")  # 关键词命中：不响不震


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
        m = re.search(r"- (?:待办|通知)：(.+?)（", user)  # 像真模型一样：头条说的是要点里的一件事
        return f"测试头条：{m.group(1)}" if m else "测试头条"

    def chats(self):
        return [c for k, c, _ in self.calls if k == "chat"]


def _ingest(chat, text, sender="甲"):
    c.post("/ingest?token=tok", json={"chat": chat, "sender": sender, "text": text})


def test_noise_filter():
    s = dict(app_mod.settings(), keywords=["奖学金"], vip=["王老师"], modes={"QQ|重要群": "focus", "QQ|静群": "atonly"})
    R = lambda text, chat="普通群", at=0, sender="甲": {"text": text, "chat": chat, "at_me": at, "sender": sender, "source": "QQ"}
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
    assert cl("收到", chat="静群", at=1) == "drop"           # 0.31：只看@我 的群不送模型（@我 在首页直接看）
    assert cl("【神价】京东抽纸券后 ¥29.9 今晚 12 点截止 https://u.jd.com/x") == "drop"  # 线报广告
    assert cl("砍一刀 https://mobile.yangkeduo.com/x") == "drop"


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
        assert (st["pending"]["msgs"], st["pending"]["chats"]) == (2, 2)       # 噪音不算
        d = c.post("/api/digest", headers=AUTH, json={"hours": 24}).json()
        assert sorted(fake.chats()) == ["增量A", "增量B"] and d["headline"].startswith("测试头条")
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


def _auto_reset():
    A = app_mod.AUTO
    A["running"].clear(); A["last_run"].clear(); A["fail"].clear(); A["fatal"] = None
    A.get("last_end", {}).clear()
    app_mod.ARRIVE.clear()


def _arrive(chat, offset):
    """把某群所有待整理消息的「收到时间」设成 now+offset。"""
    with app_mod.db() as x:
        for r in x.execute("SELECT id FROM msgs WHERE chat=?", (chat,)):
            app_mod.ARRIVE[r["id"]] = time.time() + offset


def test_auto_trigger_rules():
    """0.34 智能触发：要紧 ≈3 秒、闲聊安静 5 秒 / 攒 20 条 / 最多 15 秒、30 秒兜底、闲聊同群间隔 15 秒。"""
    _fresh(); _auto_reset()
    s = dict(app_mod.settings(), auto_on=True, vip=["王老师"], modes={"微信|吵闹群": "off", "微信|只看群": "atonly", "微信|重要群": "focus"})
    plan = lambda now=None: {k[1]: why for k, _, why in app_mod.auto_plan(s, now)[0]}
    T = time.time()
    assert plan() == {}                                          # 没有新消息
    _ingest("吵闹群", "今天下午 3 点开会"); _ingest("只看群", "明天交表"); _ingest("普通群", "哈哈哈")
    assert plan(T + 999) == {}                                   # 不看 / 只看@我 / 噪音都不触发
    _ingest("普通群", "周末一起去爬山吗大家")
    assert "普通群" not in plan(T + 3)                            # 才 3 秒，还在等安静
    assert plan(T + 5.5)["普通群"] == "quiet"                     # 安静 5 秒
    assert app_mod.auto_plan(dict(s, auto_on=False), T + 999)[0] == []  # 开关关了不整理
    # 持续刷屏：每条都把安静计时往后推，但最多 15 秒
    _arrive("普通群", -14)
    _ingest("普通群", "有人带水吗我带两瓶")
    with app_mod.db() as x:
        mid = x.execute("SELECT MAX(id) i FROM msgs WHERE chat='普通群'").fetchone()["i"]
    app_mod.ARRIVE[mid] = T  # 最后一条刚到
    assert "普通群" not in plan(T + 0.5) and plan(T + 1.5)["普通群"] == "maxwait"
    # 攒满 20 条：不等安静
    for i in range(20):
        _ingest("刷屏群", f"刷屏消息第{i}条内容比较长")
    assert plan(time.time() + 1)["刷屏群"] == "burst"
    # 要紧：重点群任何消息 / @全体 / VIP / 紧急词 / 规则事件（查寝+时间）≈ 3 秒内
    for chat, text, who in (("重要群", "这个周末大家随便聊聊", "甲"), ("普通群2", "@全体成员 明早交材料", "甲"),
                            ("普通群3", "大家看一下这个", "王老师"), ("普通群4", "紧急，有人在实验室吗", "甲"),
                            ("普通群5", "今晚导员会来查寝", "乙"), ("普通群6", "明天下午三点班会", "乙"),
                            ("普通群7", "周五 18:00 前交实验报告", "乙"), ("普通群8", "缴费链接今晚截止 https://pay.example.com/x", "乙")):
        _ingest(chat, text, who)
        p = plan(time.time() + 2.1)
        assert p.get(chat) == "urgent", (chat, p.get(chat))
    # 同群最小间隔：只管闲聊（15 秒），要紧消息不受限
    _arrive("普通群", 0)
    k = ("微信", "普通群")
    app_mod.AUTO["last_run"][k] = app_mod.AUTO["last_end"][k] = time.time() - 1
    assert "普通群" not in plan(time.time() + 10) and "普通群" in plan(time.time() + 15)
    app_mod.AUTO["last_run"][("微信", "重要群")] = time.time()
    assert plan(time.time() + 2.1)["重要群"] == "urgent"
    # 正在整理的群不重复启动
    app_mod.AUTO["running"][k] = time.time()
    assert "普通群" not in plan(time.time() + 99)
    _auto_reset(); _fresh()


def test_auto_urgent_never_decides_model_and_force_30s():
    """「要紧 / 普通」只影响快慢：普通群里看似闲聊的「今晚查寝」照样 30 秒内进模型；任何非噪音消息最迟 30 秒必进模型。"""
    _fresh(); _auto_reset()
    s = dict(app_mod.settings(), auto_on=True)
    _ingest("普通群", "今晚查寝哈哈哈，你们收拾了没")        # 带「哈哈」的闲聊口吻
    _ingest("普通群", "随便聊聊今天食堂的菜")
    k = ("微信", "普通群")
    rows = [r for r in app_mod.pending_info(s)[0] if r["chat"] == "普通群"]
    assert len(rows) == 2                                          # 都算待整理（会送模型）
    # 最坏情况：刚整理过（闲聊间隔）+ 全局调用到上限：仍然 30 秒兜底
    app_mod.AUTO["last_run"][k] = app_mod.AUTO["last_end"][k] = time.time() + 20
    now = time.time()
    app_mod._CALL_TS.extend([now] * (app_mod.AUTO_RPM + 5))
    try:
        _arrive("普通群", -31)
        ready = app_mod.auto_plan(s, time.time())[0]
        assert ready and ready[0][0] == k and ready[0][2] == "force"
        # 纯闲聊消息也一样：最迟 30 秒
        _fresh(); _auto_reset()
        _ingest("闲聊群", "这部电影真不错推荐给大家")
        app_mod.AUTO["last_run"][("微信", "闲聊群")] = time.time() + 999
        _arrive("闲聊群", -29)
        assert "闲聊群" not in {x[1] for x, _, _ in app_mod.auto_plan(s, time.time())[0]}
        _, nxt = app_mod.auto_plan(s, time.time())
        assert nxt is not None and nxt - time.time() <= 1.5          # 调度器会在 30 秒点醒来
        _arrive("闲聊群", -30.5)
        assert app_mod.auto_plan(s, time.time())[0][0][2] == "force"
    finally:
        app_mod._CALL_TS.clear(); _auto_reset(); _fresh()


def test_noise_conservative_ads_and_notice():
    """噪音极保守：像广告其实是通知的不跳过；高置信度广告才跳过；[文件]/[聊天记录] 照样送模型。"""
    s = dict(app_mod.settings(), modes={"QQ|只看群": "atonly", "QQ|不看群": "off"})
    cl = lambda t, chat="普通群", at=0, who="甲": app_mod.classify({"text": t, "chat": chat, "at_me": at, "sender": who, "source": "QQ"}, s)
    keep = ["缴费链接今晚截止 https://pay.example.com/x ¥50", "班费 30 元，拼多多买的扫把，链接 https://mobile.yangkeduo.com/x",
            "教材费在淘宝下单，券后 ¥29.9，班长统一订", "[文件]", "[聊天记录]", "[语音]", "[红包]", "可以", "行", "对", "在", "几点？", "改了",
            "[图片] 明天 8 点集合", "今晚查寝", "拍照的同学注意", "有人要出闲置的教材吗"]
    for t in keep:
        assert cl(t) != "drop", t
    drop = ["[图片]", "[表情][表情]", "[动画表情]", "👍👍", "[微笑][微笑]", "张三撤回了一条消息", "李四加入了群聊", "王五拍了拍赵六",
            "收到", "好的", "哈哈哈哈", "666", "+1",
            "【神价】京东抽纸券后 ¥29.9 今晚 12 点截止 https://u.jd.com/x", "砍一刀 https://mobile.yangkeduo.com/x",
            "兼职日结 300 元，私聊下单"]
    for t in drop:
        assert cl(t) == "drop", t
    s["vip"] = ["王老师"]
    assert cl("【神价】京东抽纸券后 ¥29.9 https://u.jd.com/x", who="王老师") != "drop"   # 重要的人发的不当广告跳过
    assert cl("收到", at=1) == "key" and cl("[图片]", at=1) == "key"   # @我 永远送
    assert cl("明天交表", chat="只看群") == "drop" and cl("明天交表", chat="不看群") == "drop"


def test_auto_noise_never_calls_model():
    """只有噪音的群：一次模型都不调，水位直接前进，不计入待整理。"""
    import asyncio
    _fresh(); _auto_reset()
    s = dict(app_mod.settings(), auto_on=True, modes={"微信|不看群": "off"})
    calls = []

    async def spy(msgs, as_json=False):
        calls.append(msgs[0]["content"][:20]); return json.dumps({"summary": "x"})
    old = app_mod.llm; app_mod.llm = spy
    try:
        for t in ("[图片]", "收到", "哈哈哈", "[表情]", "张三撤回了一条消息", "【神价】抽纸券后 ¥9.9 https://u.jd.com/x 速冲"):
            _ingest("噪音群", t)
        _ingest("不看群", "明天下午三点开会")
        assert app_mod.pending_info(s)[1]["msgs"] == 0
        asyncio.run(app_mod.auto_tick(s, time.time() + 999))
        assert calls == [] and not app_mod.AUTO_TASKS
        with app_mod.db() as x:
            top = x.execute("SELECT MAX(id) i FROM msgs").fetchone()["i"]
            wm = {r["chat"]: r["last_msg_id"] for r in x.execute("SELECT * FROM chat_state")}
        assert wm.get("噪音群") == top - 1 and wm.get("不看群") == top    # 水位前进
    finally:
        app_mod.llm = old; _auto_reset(); _fresh()


def test_auto_follow_up_after_running():
    """整理进行中来的新消息：这次一结束马上接着跑（要紧的立即；闲聊受 15 秒间隔，从上次开始算）。"""
    import asyncio
    _fresh(); _auto_reset()
    s = dict(app_mod.settings(), auto_on=True)
    seen = []

    async def slow(msgs, as_json=False):
        if not msgs[0]["content"].startswith("【群更新】"):
            return "头条"
        seen.append(msgs[1]["content"].split("新消息（", 1)[1])
        if len(seen) == 1:
            _ingest("接续群", "@全体成员 改到明天 9 点")             # 整理进行中来的要紧消息
        await asyncio.sleep(0.3)
        return json.dumps({"summary": "x"})
    old = app_mod.llm; app_mod.llm = slow
    try:
        _ingest("接续群", "周五 18:00 交报告")
        _arrive("接续群", -5)

        async def go():
            t0 = time.time()
            await app_mod.auto_tick(s, time.time())
            while app_mod.AUTO_TASKS:
                await asyncio.gather(*list(app_mod.AUTO_TASKS))
            ready = app_mod.auto_plan(s, time.time())[0]
            assert ready and ready[0][0][1] == "接续群" and ready[0][2] in ("urgent", "follow")
            await app_mod.auto_tick(s, time.time())
            while app_mod.AUTO_TASKS:
                await asyncio.gather(*list(app_mod.AUTO_TASKS))
            return time.time() - t0
        took = asyncio.run(go())
        assert len(seen) == 2 and "改到明天 9 点" in seen[1] and "交报告" not in seen[1]   # 第二轮只送增量
        assert took < 2 and app_mod.pending_info(s)[1]["msgs"] == 0
        # 闲聊的接续：结束后受 15 秒间隔（从上次开始算），且 ≤ 30 秒兜底
        _ingest("接续群2", "随便聊聊今天食堂")
        k = ("微信", "接续群2")
        t = time.time()
        app_mod.AUTO["last_run"][k] = t - 10; app_mod.AUTO["last_end"][k] = t - 1
        _arrive("接续群2", -6)
        assert "接续群2" not in {x[1] for x, _, _ in app_mod.auto_plan(s, t)[0]}
        assert app_mod.auto_plan(s, t + 5.1)[0][0][2] in ("follow", "quiet", "maxwait")
        assert app_mod.AUTO_LOG and app_mod.AUTO_LOG[-1]["chat"] == "接续群" and app_mod.AUTO_LOG[-1]["calls"] >= 1
        assert c.get("/api/auto/log", headers=AUTH).json()["hour"]["runs"] >= 2
    finally:
        app_mod.llm = old; _auto_reset(); _fresh()


def test_auto_429_cooldown_holds_new_runs():
    _fresh(); _auto_reset()
    s = dict(app_mod.settings(), auto_on=True)
    _ingest("限流群", "@全体成员 明早交材料")
    app_mod.LLM_COOL[0] = time.time() + 20
    try:
        r, nxt = app_mod.auto_plan(s, time.time() + 5)
        assert r == [] and abs(nxt - app_mod.LLM_COOL[0]) < 0.01
    finally:
        app_mod.LLM_COOL[0] = 0; _auto_reset(); _fresh()


def test_auto_parallel_backoff_and_fatal():
    import asyncio
    _fresh(); _auto_reset()
    s = dict(app_mod.settings(), auto_on=True)
    for i in range(7):
        _ingest(f"并发群{i}", f"周五 18:00 前交第{i}份材料")
    active, peak = [0], [0]

    async def slow_llm(msgs, as_json=False):
        if not msgs[0]["content"].startswith("【群更新】"):
            return "测试头条"
        active[0] += 1; peak[0] = max(peak[0], active[0])
        await asyncio.sleep(0.2)
        active[0] -= 1
        chat = msgs[0]["content"].split("「", 1)[1].split("」", 1)[0]
        return json.dumps({"summary": "材料", "new": [{"kind": "todo", "title": f"交{chat}材料", "due": "周五 18:00", "urgency": "high"}]})
    old = app_mod.llm; app_mod.llm = slow_llm
    try:
        async def go():
            far = time.time() + 999
            await app_mod.auto_tick(s, far)
            assert len(app_mod.AUTO["running"]) == app_mod.AUTO_PARALLEL == 6      # 全局最多 6 个群
            await app_mod.auto_tick(s, far)
            assert len(app_mod.AUTO["running"]) == 6                              # 不超额、不重复
            while app_mod.AUTO_TASKS:
                await asyncio.gather(*list(app_mod.AUTO_TASKS))
            await app_mod.auto_tick(s, far)
            while app_mod.AUTO_TASKS:
                await asyncio.gather(*list(app_mod.AUTO_TASKS))
        t0 = time.time()
        asyncio.run(go())
        assert peak[0] == 6 and time.time() - t0 < 1.5                            # 并发，不是 7×0.2 串行
        st = c.get("/api/state", headers=AUTH).json()
        titles = [t["title"] for t in st["digest"]["todos"]]
        assert all(f"交并发群{i}材料" in titles for i in range(7))                 # 局部更新进首页
        assert st["digest"]["live"] and st["auto"]["state"] == "idle" and st["pending"]["msgs"] == 0
    finally:
        app_mod.llm = old
    # 失败指数退避：10 → 20 → 40 → ≤ 60（模型一直没成功过：不跳过，一直重试）
    _fresh(); _auto_reset(); app_mod.LLM_LAST_OK[0] = 0
    _ingest("坏群", "周五 18:00 交表")

    async def bad(msgs, as_json=False):
        raise app_mod.HTTPException(502, "大模型服务商出故障（503）：稍后会自动重试", headers={"x-llm-code": "503"})
    app_mod.llm = bad
    try:
        rows = [r for r in app_mod.pending_info(s)[0] if r["chat"] == "坏群"]
        k = ("微信", "坏群")
        waits = []
        for n in range(7):
            now = time.time()
            asyncio.run(app_mod.auto_run_chat(k, rows, s, now))
            waits.append(round(app_mod.AUTO["fail"][k]["until"] - now))
        assert waits == [10, 20, 40, 60, 60, 60, 60]
        assert "坏群" not in {x[1] for x, _, _ in app_mod.auto_plan(s, time.time() + 30)[0]}
        assert app_mod.pending_info(s)[1]["failing"] == 1 and app_mod.pending_info(s)[1]["retry_in"] > 0
        assert app_mod.auto_status(s)["state"] == "idle"                    # 刚失败：后台退避重试，不打扰
        app_mod.AUTO["fail"][k]["since"] = time.time() - 400                # 连续失败 5 分钟以上才提示
        assert app_mod.auto_status(s)["state"] == "failed" and app_mod.auto_status(s)["reason"] == "服务商故障"
        # 余额不足：不重试，直到设置变更
        app_mod.AUTO["fail"].clear()

        async def broke(msgs, as_json=False):
            raise app_mod.HTTPException(502, app_mod.llm_err_text(402, "Insufficient Balance"), headers={"x-llm-code": "402"})
        app_mod.llm = broke
        asyncio.run(app_mod.auto_run_chat(k, rows, s, time.time()))
        st = app_mod.auto_status(s)
        assert st["state"] == "failed" and st["reason"] == "余额不足" and st["fatal"]
        assert app_mod.auto_plan(s, time.time() + 99999)[0] == []                 # 一天后也不重试
        s2 = dict(s, profile="我充值了")                                           # 设置变了：解除
        assert app_mod.auto_plan(s2, time.time() + 46)[0] and app_mod.AUTO["fatal"] is None
    finally:
        app_mod.llm = old
        _auto_reset(); _fresh()


def test_headline_only_rewritten_for_new_urgent():
    import asyncio
    _fresh(); _auto_reset()
    s = dict(app_mod.settings(), auto_on=True)
    nm = lambda u: u.split("新消息（", 1)[1]
    fake = FakeLLM({"头条群": lambda u: {"new": [{"kind": "todo", "title": "交报告A", "due": "明天 12:00", "urgency": "high"}]} if "报告A" in nm(u)
                    else {"new": [{"kind": "notice", "title": "图书馆换了开放时间", "urgency": "low"}]} if "图书馆" in nm(u)
                    else {"new": [{"kind": "todo", "title": "交班费", "due": "明天 18:00", "urgency": "high"}]}})
    old = app_mod.llm; app_mod.llm = fake
    app_mod.kv_set("head_ts", 0)
    try:
        def run(text):
            _ingest("头条群", text)
            rows = [r for r in app_mod.pending_info(s)[0] if r["chat"] == "头条群"]
            async def go():
                await app_mod.auto_run_chat(("微信", "头条群"), rows, s, time.time())
                while app_mod.AUTO_TASKS:
                    await asyncio.gather(*list(app_mod.AUTO_TASKS))
            asyncio.run(go())
            return c.get("/api/state", headers=AUTH).json()
        st = run("明天 12:00 前交报告A")
        heads = [x for x in fake.calls if x[0] == "head"]
        assert len(heads) == 1 and st["digest"]["headline"] == "测试头条：交报告A"   # 新的要紧事：模型写一次头条
        r1 = st["rev"]
        st = run("图书馆通知：开放时间改了")
        assert len([x for x in fake.calls if x[0] == "head"]) == 1 and st["digest"]["headline"] == "测试头条：交报告A"  # 不要紧：头条不动
        assert st["rev"] != r1                                                     # 事项变了 rev 就变
        st = run("明天 18:00 前交班费 50 元")                                           # 10 分钟内又来要紧事：规则拼，不调模型
        assert len([x for x in fake.calls if x[0] == "head"]) == 1 and "交班费" in st["digest"]["headline"]
        assert c.get("/api/rev", headers=AUTH).json()["rev"] == st["rev"]          # 没变化 rev 不变
        ids = {d["id"] for d in c.get("/api/digests", headers=AUTH).json()[:3]}
        assert len(ids) >= 1 and st["digest"]["id"] == max(ids)                    # 原地更新同一期
        n = len(c.get("/api/digests", headers=AUTH).json())
        run("明天 12:00 前交报告A 补充：要打印")
        assert len(c.get("/api/digests", headers=AUTH).json()) == n                 # 不会每次整理都新建一期
    finally:
        app_mod.llm = old
        _auto_reset(); _fresh()


def test_rev_changes_on_done_and_pin():
    _fresh()
    with app_mod.db() as x:
        x.execute("INSERT INTO items(source,chat,kind,title,status,first_ts,updated_ts) VALUES('QQ','rev群','todo','交表','open',1,1)")
        iid = x.execute("SELECT MAX(id) i FROM items").fetchone()["i"]
    r0 = c.get("/api/rev", headers=AUTH).json()
    assert set(r0) >= {"rev", "auto", "pending", "updated_ts", "mrev"}
    c.post("/api/todo", headers=AUTH, json={"key": f"item:{iid}", "pin": True})
    r1 = c.get("/api/rev", headers=AUTH).json()["rev"]
    c.post("/api/todo", headers=AUTH, json={"key": f"item:{iid}", "done": True})
    r2 = c.get("/api/rev", headers=AUTH).json()["rev"]
    assert len({r0["rev"], r1, r2}) == 3
    _ingest("rev群", "随便聊聊天气不错")
    assert c.get("/api/rev", headers=AUTH).json()["mrev"] > r0["mrev"]
    _fresh()


def test_auto_interval_migrates_to_switch():
    import json as _j
    keep = app_mod.kv_get("settings")
    try:
        for iv, on in ((30, True), (15, True), (120, True), (0, False)):
            st = _j.loads(keep); st.pop("auto_on", None); st["auto_interval"] = iv
            app_mod.kv_set("settings", _j.dumps(st))
            s = app_mod.settings()
            assert s["auto_on"] is on and "auto_interval" not in s
        assert app_mod.save_settings({"auto_interval": 60})["auto_on"] is True   # 旧客户端
        assert app_mod.save_settings({"auto_on": False})["auto_on"] is False
    finally:
        app_mod.kv_set("settings", keep)


def test_settings_groups_keep_fields():
    st = c.get("/api/settings", headers=AUTH).json()
    assert set(app_mod.DEFAULTS) <= set(st) and st["auto_on"] is True and "auto_interval" not in st
    before = dict(st)
    r = c.post("/api/settings", headers=AUTH, json={"auto_on": False}).json()
    assert r["auto_on"] is False and all(r[k] == before[k] for k in before if k != "auto_on")
    c.post("/api/settings", headers=AUTH, json={"auto_on": True})
    html = c.get("/", headers=AUTH).text
    # 每个设置项都还在页面上有入口（重组后不丢字段）
    for k in app_mod.DEFAULTS:
        if k in ("only_mode", "allowed", "muted", "levels", "modes", "modes_tip_done"):  # 旧字段已迁移；modes 走 /api/chat_mode
            continue
        assert f"SET.{k}" in html or f'data-k="{k}"' in html, k
    for sub in ("me", "groups", "kw", "remind", "conn", "data", "acct", "about"):
        assert f'id="sub-{sub}"' in html and f'data-sub="{sub}"' in html
    assert "refreshQuiet" in html and "/api/rev" in html and "visibilitychange" in html and "morph(" in html
    assert "稍后自动整理" not in html and 'id="s-auto"' in html and "<select id=\"s-auto\"" not in html   # 0.32：间隔下拉换成开关


def test_tidy_headline_never_cuts_mid_word():
    import importlib, sys
    app = sys.modules.get("app") or importlib.import_module("app")
    h = app.tidy_headline("Claude Max 5.5使用异常，Pro Api中转群反馈渠道、生图问题，待处理事项较多需要关注")
    assert h == "Claude Max 5.5使用异常，Pro Api中转群反馈渠道" or not h.endswith("待")
    assert not h.endswith("，") and len(h) <= 30
    assert app.tidy_headline("「周五前交实验报告」") == "周五前交实验报告"
    long = app.tidy_headline("一" * 50)
    assert long.endswith("…") and len(long) <= 30


def test_hotfix_0304_layout_css():
    """0.30.4：长群名把「各群在聊」撑出屏幕；body overflow-x:hidden 让 sticky 标题栏失效。"""
    import re as _re
    h = open(os.path.join(os.path.dirname(__file__), "..", "digest", "index.html"), encoding="utf-8").read()
    assert "repeat(2,minmax(0,1fr))" in h and "repeat(3,minmax(0,1fr))" in h
    assert not _re.search(r"grid-template-columns:[^;}]*(?<!,)\b1fr", h.replace("minmax(0,1fr)", ""))
    assert ".g .gn{flex:1;min-width:0" in h
    assert "body{overflow-x:clip}" in h and "html,body{max-width:100%;overflow-x:hidden" not in h
    assert "--dockh" in h and "imgGone" in h and "this.remove()" not in h
    sw = open(os.path.join(os.path.dirname(__file__), "..", "digest", "sw.js"), encoding="utf-8").read()
    assert f'qunbao-v{app_mod.VERSION}"' in sw


def test_messages_after_poll():
    for i in range(3):
        c.post("/ingest?token=tok", json={"chat": "轮询群", "sender": "a", "text": f"第{i}条消息内容"})
    ms = c.get("/api/messages?chat=轮询群", headers=AUTH).json()
    first = ms[0]["id"]
    new = c.get(f"/api/messages?chat=轮询群&after={first}", headers=AUTH).json()
    assert [m["text"] for m in new] == ["第1条消息内容", "第2条消息内容"]
    assert c.get(f"/api/messages?chat=轮询群&after={ms[-1]['id']}", headers=AUTH).json() == []


# ================= 0.31.0 =================
def _llm_spy(reply):
    calls = []

    async def fake(msgs, as_json=False):
        calls.append(msgs)
        return reply(msgs) if callable(reply) else reply
    return calls, fake


def test_mode_off_and_atonly_never_call_llm():
    _fresh()
    app_mod.set_modes([("微信", "水群A")], "off")
    app_mod.set_modes([("微信", "线报群B")], "atonly")
    _ingest("水群A", "明天 18:00 交材料，务必")
    _ingest("线报群B", "今晚 12 点截止 速冲")
    _ingest("线报群B", "@我 你的快递到了", sender="乙")
    calls, fake = _llm_spy(lambda m: '{"summary":"x","new":[]}')
    ol = app_mod.llm
    app_mod.llm = fake
    try:
        import asyncio
        st, _ = asyncio.run(app_mod.run_update())
    finally:
        app_mod.llm = ol
    assert not calls, "不看/只看@我 的群一律不送模型"
    assert st["skipped"] == 2 and st["calls"] == 0


def test_atonly_push_only_when_at():
    s = dict(app_mod.DEFAULTS, keywords=["截止"], modes={"QQ|吵群": "atonly", "QQ|关群": "off", "QQ|正常群": "normal", "QQ|重点群": "focus"})
    assert app_mod.hit_reason("吵群", "a", "报名截止明天", False, s, "QQ") is None
    assert app_mod.hit_reason("吵群", "a", "@全体成员 明天开会", True, s, "QQ") == ("@全体", "timeSensitive")
    assert app_mod.hit_reason("吵群", "a", "@我 看下", True, s, "QQ") == ("@你", "timeSensitive")
    assert app_mod.hit_reason("关群", "a", "@我 看下", True, s, "QQ") is None          # 不看：@我 也不推
    assert app_mod.hit_reason("正常群", "a", "【神价】券后 ¥9.9 今晚截止", False, s, "QQ") is None  # 广告不推
    assert app_mod.hit_reason("正常群", "a", "报名今晚截止", False, s, "QQ")[1] == "passive"  # 关键词：不响不震
    assert app_mod.hit_reason("重点群", "a", "周六 9:00 集合", False, s, "QQ") == ("重点群", "active")
    assert app_mod.hit_reason("重点群", "a", "哈哈哈", False, s, "QQ") is None


def test_new_chat_default_mode_and_same_name_split():
    app_mod.save_settings({"default_mode": "atonly"})
    try:
        _ingest("全新的群X", "大家好")
        assert app_mod.settings()["modes"]["微信|全新的群X"] == "atonly"
        app_mod.save_settings({"default_mode": "normal"})
        assert app_mod.chat_mode("微信", "全新的群X", app_mod.settings()) == "atonly"   # 改默认不影响已有的群
    finally:
        app_mod.save_settings({"default_mode": "normal"})
    # QQ 和微信同名群互不影响
    app_mod.set_modes([("QQ", "同名群")], "off")
    s = app_mod.settings()
    assert app_mod.chat_mode("QQ", "同名群", s) == "off" and app_mod.chat_mode("微信", "同名群", s) == "normal"
    r = c.post("/api/chat_mode", headers=AUTH, json={"chats": [{"source": "微信", "chat": "同名群"}], "mode": "focus"}).json()
    assert r["ok"] and r["name"] == "重点盯"
    assert c.post("/api/chat_mode", headers=AUTH, json={"chats": [{"chat": "x"}], "mode": "bad"}).status_code == 400
    ch = {(x["source"], x["chat"]): x for x in c.get("/api/chats", headers=AUTH).json()}
    assert ch[("微信", "全新的群X")]["mode"] == "atonly" and "today" in ch[("微信", "全新的群X")]


def test_legacy_settings_migrate_to_modes():
    s0 = app_mod.settings()
    app_mod.save_settings({"muted": ["老屏蔽群"], "levels": {"老重要群": "important"}})
    with app_mod.db() as d:
        d.execute("INSERT INTO msgs(ts,source,chat,sender,text) VALUES(?,?,?,?,?)", (int(time.time()), "QQ", "老屏蔽群", "a", "x"))
        d.execute("INSERT INTO msgs(ts,source,chat,sender,text) VALUES(?,?,?,?,?)", (int(time.time()), "QQ", "老重要群", "a", "x"))
        st = json.loads(d.execute("SELECT v FROM kv WHERE k='settings'").fetchone()["v"]); st.pop("modes_migrated", None)
        st["modes"] = {k: v for k, v in st["modes"].items() if not k.startswith("QQ|老")}
        d.execute("UPDATE kv SET v=? WHERE k='settings'", (json.dumps(st, ensure_ascii=False),))
    app_mod.migrate_modes()
    s = app_mod.settings()
    assert s["modes"]["QQ|老屏蔽群"] == "off" and s["modes"]["QQ|老重要群"] == "focus"
    assert s["muted"] == [] and s["levels"] == {} and not s["only_mode"]


def test_title_tidy_ads_and_headline():
    t = app_mod.tidy_title
    assert t("需要本学期班费每人 50，并且数据库实验报告 10月1，还要确认群公告") == "本学期班费每人 50"
    assert t("请于10月12日 17:00 前在学工系统完成奖学金申报 https://x.cn/a?b=1", due="10月12日 17:00") == "在学工系统完成奖学金申报"
    assert "https" not in t("字节内推 10月20日截止 https", due="10月20日")
    assert t("关于班会改线上的通知事项") == "班会改线上"
    assert len(t("这是一个非常非常长的标题而且中间没有任何标点符号可以截断的那种情况")) <= 22
    assert t("交实验报告") == "交实验报告"
    h = app_mod.tidy_headline("今天要交数据库实验报告、缴纳班费 50 元、参加周一组会汇报")
    assert h == "今天要交数据库实验报告"
    assert app_mod.tidy_headline("周五 23:59 前交数据库实验报告") == "周五 23:59 前交数据库实验报告"
    assert app_mod.same_text("交实验报告", "@全体成员 实验报告周五前交到学习通")
    assert not app_mod.same_text("交实验报告", "按模板命名，逾期不收")
    # 模型把广告当事项：直接丢掉
    _fresh()
    app_mod.apply_changes("QQ", "线报群", {"new": [{"kind": "todo", "title": "【神价】京东抽纸券后 ¥29.9", "quote": "速冲 https://u.jd.com/x"},
                                                  {"kind": "todo", "title": "交实验报告", "detail": "交实验报告"}]})
    with app_mod.db() as d:
        rows = [dict(r) for r in d.execute("SELECT title, detail FROM items WHERE chat='线报群'")]
    assert rows == [{"title": "交实验报告", "detail": ""}]


def test_parse_due_weekdays():
    from datetime import datetime
    now = datetime(2026, 10, 8, 22, 0, tzinfo=app_mod.TZ)  # 星期四
    P = lambda x: app_mod.parse_due(x, now).strftime("%m-%d %H:%M")
    assert P("本周五") == "10-09 23:59" and P("周五") == "10-09 23:59"
    assert P("周一上午 9:00") == "10-12 09:00" and P("下周三 18:00") == "10-14 18:00"
    assert P("月底") == "10-31 23:59" and P("周六晚 7 点") == "10-10 19:00" and P("明天 8 点半") == "10-09 08:30"
    assert P("星期日") == "10-11 23:59" and P("明天上午") == "10-09 12:00"


def test_invalid_json_retried_immediately():
    _fresh()
    _ingest("重试群", "明天 18:00 交材料")
    n = {"i": 0}

    def reply(m):
        n["i"] += 1
        return '好的：{"summary":"x","new":[{"kind":"todo","ti' if n["i"] == 1 else '{"summary":"交材料","new":[{"kind":"todo","title":"交材料","due":"明天 18:00"}]}'
    calls, fake = _llm_spy(reply)
    ol = app_mod.llm
    app_mod.llm = fake
    try:
        import asyncio
        st, _ = asyncio.run(app_mod.run_update())
    finally:
        app_mod.llm = ol
    assert st["errors"] == 0 and st["calls"] == 2 and len(calls) == 2
    assert "不是合法 JSON" in calls[1][-1]["content"]


def test_llm_error_text():
    assert "余额" in app_mod.llm_err_text(402, "Insufficient Balance")
    assert "Key" in app_mod.llm_err_text(401, "invalid api key")
    assert "限流" in app_mod.llm_err_text(429, "")
    assert "服务商" in app_mod.llm_err_text(503, "")


def test_held_push_survives_restart(monkeypatch):
    import asyncio
    app_mod.kv_set("held", "[]")
    from datetime import datetime
    h = datetime.now(app_mod.TZ).hour
    app_mod.save_settings({"bark_url": "http://x/k", "quiet_start": h, "quiet_end": (h + 1) % 24})
    try:
        assert asyncio.run(app_mod.push("a", "b")) is False
        assert json.loads(app_mod.kv_get("held")) == [["a", "b"]]   # 存在数据库里，重启不丢
    finally:
        app_mod.save_settings({"bark_url": "", "quiet_start": -1})
        app_mod.kv_set("held", "[]")


def _seed_chat(chat, texts, source="QQ"):
    ids = []
    with app_mod.db() as d:
        for t in texts:
            ids.append(d.execute("INSERT INTO msgs(ts,source,chat,sender,text) VALUES(?,?,?,?,?)",
                                 (int(time.time()) - 60, source, chat, "王老师", t)).lastrowid)
    return ids


def test_ask_scoped_with_valid_citations():
    ids = _seed_chat("问答群", ["周五 23:59 前交实验报告到学习通", "下周三班会改线上", "哈哈哈"])
    other = _seed_chat("别的群", ["周五要交的是别的东西"])[0]
    app_mod.set_modes([("QQ", "问答群")], "off")       # 不看的群也能问
    seen = {}

    def reply(m):
        seen["ctx"] = m[1]["content"]
        return f"要交实验报告 [#{ids[0]}]，班会改线上了 [#{ids[1]}]，还有个编的 [#99999999] 和别的群的 [#{other}]"
    calls, fake = _llm_spy(reply)
    ol = app_mod.llm
    app_mod.llm = fake
    try:
        r = c.post("/api/ask", headers=AUTH, json={"q": "有要我做的吗", "chat": "问答群", "source": "QQ"}).json()
    finally:
        app_mod.llm = ol
    assert "别的群" not in seen["ctx"] and f"#{other} " not in seen["ctx"]   # 只检索这个群
    assert "哈哈哈" not in seen["ctx"]                                     # 去噪
    assert [x["msg_id"] for x in r["citations"]] == ids[:2]                 # 非法 / 范围外引用丢弃
    assert "[1]" in r["a"] and "[2]" in r["a"] and "99999999" not in r["a"] and str(other) not in r["a"]
    # 跳转：按 id 取上下文
    ms = c.get(f"/api/messages?chat=问答群&source=QQ&around={ids[1]}&limit=10", headers=AUTH).json()
    assert ids[1] in [m["id"] for m in ms] and all(m["chat"] == "问答群" for m in ms)


def test_ask_cite_parser():
    rows = {5: {"ts": 1, "sender": "a", "chat": "c", "source": "QQ", "text": "x"}, 9: {"ts": 2, "sender": "b", "chat": "c", "source": "QQ", "text": "y"}}
    t, cs = app_mod.cite("一 [#9] 二【#5】三 [#9] 四 [#7]", rows)
    assert t == "一 [1] 二[2]三 [1] 四" and [x["msg_id"] for x in cs] == [9, 5]


def test_chat_brief_cached_no_new_no_llm():
    ids = _seed_chat("简报群", ["明天 9:00 东门集合", "记得带水"])
    calls, fake = _llm_spy(lambda m: f"- 明天 9 点东门集合 [#{ids[0]}]")
    ol = app_mod.llm
    app_mod.llm = fake
    try:
        r1 = c.get("/api/chat_brief?chat=简报群&source=QQ", headers=AUTH).json()
        r2 = c.get("/api/chat_brief?chat=简报群&source=QQ", headers=AUTH).json()
        r3 = c.get(f"/api/chat_brief?chat=简报群&source=QQ&since_id={ids[-1]}", headers=AUTH).json()
    finally:
        app_mod.llm = ol
    assert len(calls) == 1 and r2["cached"] and r3["nothing_new"] and r3["a"] == r1["a"]
    assert r1["citations"][0]["msg_id"] == ids[0]
    assert "【群简报】" in calls[0][0]["content"] and "口语" in calls[0][0]["content"]


def test_state_at_me_covered_and_mode_filter():
    _fresh()
    _ingest("覆盖群", "@我 明天交表", sender="班长")
    with app_mod.db() as d:
        mid = d.execute("SELECT MAX(id) i FROM msgs").fetchone()["i"]
    app_mod.apply_changes("微信", "覆盖群", {"new": [{"kind": "todo", "title": "交表", "msg_ids": [mid]}]}, {mid})
    import asyncio
    calls, fake = _llm_spy(lambda m: '{"summary":"交表","new":[]}' if m[0]["content"].startswith("【群更新】") else "交表")
    ol = app_mod.llm
    app_mod.llm = fake
    try:
        asyncio.run(app_mod.make_digest(24))
    finally:
        app_mod.llm = ol
    st = c.get("/api/state", headers=AUTH).json()
    a = [x for x in st["at_me"] if x["id"] == mid][0]
    assert a["covered"]
    assert any(t["title"] == "交表" for t in st["digest"]["todos"])
    app_mod.set_modes([("微信", "覆盖群")], "off")      # 改成「不看」立刻从首页拿掉
    st = c.get("/api/state", headers=AUTH).json()
    assert not any(t["chat"] == "覆盖群" for t in st["digest"]["todos"]) and not any(x["chat"] == "覆盖群" for x in st["at_me"])
    app_mod.set_modes([("微信", "覆盖群")], "normal")


def test_suggest_quiet_groups():
    with app_mod.db() as d:
        for i in range(20):
            d.execute("INSERT INTO msgs(ts,source,chat,sender,text) VALUES(?,?,?,?,?)",
                      (int(time.time()) - 60, "QQ", "神价线报群", "a", f"【神价】券后 ¥{i}.9 https://u.jd.com/{i}"))
    sg = {x["chat"]: x for x in c.get("/api/chat_suggest", headers=AUTH).json()}
    assert "神价线报群" in sg and "广告" in sg["神价线报群"]["why"]
    assert app_mod.chat_mode("QQ", "神价线报群", app_mod.settings()) != "atonly"   # 只建议，不自动改


def test_ui_has_modes_and_chat_assistant():
    h = c.get("/", headers=AUTH).text
    for k in ("modeSheet", "/api/chat_mode", "swipeRow", "longPress", 'id="cmode"', 'id="g-q"', 'id="g-batch"', "/api/chat_suggest",
              'id="chatask"', "问问这个群", "我没看的这段讲了啥", "/api/chat_brief", "button class=\"cite\"", "jumpTo", "visualViewport", "qb_ask:", "askclr"):
        assert k in h, k


def test_chats_expose_at_ids():
    app_mod.save_sync = None
    import asyncio
    asyncio.run(app_mod.save("QQ", "角标群", "老师", "@我 交表", None, True))
    ch = [x for x in c.get("/api/chats", headers=AUTH).json() if x["chat"] == "角标群"][0]
    assert ch["at_ids"] and ch["ats"] == 1


def test_tidy_bullets_cuts_at_punctuation():
    from digest import app as m
    a = "- 群里通知10月10日 23:00 前必须提交报名表格并且转发给班主任确认，别忘了 [#12]\n- 短句 [#3]"
    out = m.tidy_bullets(a)
    first = out.splitlines()[0]
    assert first.endswith("[#12]") and "23:00 前必须" in first and "确认" not in first
    assert out.splitlines()[1] == "- 短句 [#3]"


def test_nav_stack_and_edge_back_ui():
    html = c.get("/", headers=AUTH).text
    # 群聊 / 弹层 / 设置子页 / 待办详情 都进历史栈，popstate 关最上面一层；按 hash 恢复群；左边缘右滑返回
    for k in ("function navPush", "function navClose", '"popstate"', "#chat/", "chatHash(", 't:"chat"', 't:"sheet"', 't:"sub"', 't:"todo"',
              "H2SRC", "clientX>24", "innerWidth/3", "edgeShade"):
        assert k in html, k
    assert "pushState({sub:n}" not in html


def test_push_link_opens_chat(monkeypatch):
    import asyncio
    sent = []

    async def fake_post(self, url, json=None):
        sent.append(json)
        class R: status_code = 200
        return R()
    monkeypatch.setattr(app_mod.httpx.AsyncClient, "post", fake_post)
    app_mod.save_settings({"bark_url": "http://x/k", "site_url": "https://qb.example.com", "quiet_start": -1})
    try:
        assert asyncio.run(app_mod.push("@你 · 计科2201", "x", key="QQ|计科 2201/班"))
        assert sent[-1]["url"] == "https://qb.example.com/#chat/qq/%E8%AE%A1%E7%A7%91%202201%2F%E7%8F%AD"
    finally:
        app_mod.save_settings({"bark_url": "", "site_url": ""})


def test_censored_messages_skipped_not_stuck():
    """服务商内容审核（451 censorship_blocked）拦下某几条：拆块重试，只跳过被拦的那条，其余照常整理，水位前进。"""
    import asyncio
    from fastapi import HTTPException
    _fresh(); _auto_reset()
    s = dict(app_mod.settings(), auto_on=True)
    for i in range(6):
        _ingest("审核群", f"第{i}条：周五 18:00 前交材料{i}" if i != 3 else "敏感词BAD")
    calls = [0]

    async def fake(msgs, as_json=False):
        if not msgs[0]["content"].startswith("【群更新】"):
            return "头条"
        calls[0] += 1
        if "BAD" in msgs[1]["content"]:
            raise HTTPException(502, "blocked", headers={"x-llm-code": "451"})
        return json.dumps({"summary": "交材料", "new": [{"kind": "todo", "title": "交材料", "due": "周五 18:00", "urgency": "high"}]})
    old = app_mod.llm; app_mod.llm = fake
    try:
        async def go():
            await app_mod.auto_tick(s, time.time() + 999)
            while app_mod.AUTO_TASKS:
                await asyncio.gather(*list(app_mod.AUTO_TASKS))
        asyncio.run(go())
        st = c.get("/api/state", headers=AUTH).json()
        assert any(t["title"] == "交材料" for t in st["digest"]["todos"])
        assert st["pending"]["msgs"] == 0 and calls[0] <= 6
        assert not app_mod.AUTO["fail"]
    finally:
        app_mod.llm = old


def test_429_global_cooldown_and_censor_detect():
    assert app_mod.is_censored(451, "") and app_mod.is_censored(400, '{"type":"censorship_blocked"}')
    assert not app_mod.is_censored(400, "bad request")
    import asyncio, httpx
    hits = []

    def handler(req):
        hits.append(time.time())
        if len(hits) < 3:
            return httpx.Response(429, headers={"retry-after": "0"}, text="rate limit")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    real = httpx.AsyncClient
    old_key, old_waits = app_mod.LLM_KEY, app_mod.LLM_429_WAITS
    app_mod.LLM_KEY = "k"; app_mod.LLM_429_WAITS = (0, 0, 0, 0); app_mod.LLM_RETRY_WAIT_OLD = app_mod.LLM_RETRY_WAIT
    app_mod.LLM_RETRY_WAIT = 0
    httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    try:
        assert asyncio.run(REAL_LLM([{"role": "system", "content": "x"}])) == "ok"   # 两次 429 后成功，不报错
        assert len(hits) == 3
    finally:
        httpx.AsyncClient = real
        app_mod.LLM_KEY, app_mod.LLM_429_WAITS = old_key, old_waits
        app_mod.LLM_RETRY_WAIT = app_mod.LLM_RETRY_WAIT_OLD


def test_history_one_per_day():
    import json as _j
    _fresh()
    now = int(time.time())
    with app_mod.db() as x:
        x.execute("DELETE FROM digests")
        for i in range(6):  # 旧版：同一天每半小时存一期
            x.execute("INSERT INTO digests(ts,hours,body) VALUES(?,?,?)", (now - 600 * i, 24, _j.dumps({"headline": f"h{i}"})))
        x.execute("INSERT INTO digests(ts,hours,body) VALUES(?,?,?)", (now - 86400 * 2, 24, _j.dumps({"headline": "old"})))
    app_mod.kv_set("rule_backfill", app_mod.VERSION)  # 启动时的规则补建会原地更新最新一期，这里只测往期列表
    with TestClient(app_mod.app) as c:
        hs = [d["headline"] for d in c.get("/api/digests", headers=AUTH).json()]
    assert hs[0] == "h0" and hs.count("old") == 1 and not any(h in hs for h in ("h1", "h2", "h5"))
    _fresh()


def test_range_switch_is_pure_view():
    import json as _j
    _fresh()
    with app_mod.db() as x:
        x.execute("DELETE FROM digests")
        x.execute("INSERT INTO digests(ts,hours,body) VALUES(?,?,?)", (int(time.time()), 24, _j.dumps({"headline": "今日头条", "todos": [], "notices": []})))
    calls = int(app_mod.kv_get("llm_calls", 0) or 0)
    with TestClient(app_mod.app) as cc:
        for h in (72, 168, 24):
            st = cc.get(f"/api/state?hours={h}", headers=AUTH).json()
            assert st["hours"] == h and st["digest"]["headline"]
        n = len(cc.get("/api/digests", headers=AUTH).json())
    with app_mod.db() as x:
        assert x.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 1   # 不新建一期
    assert int(app_mod.kv_get("llm_calls", 0) or 0) == calls                 # 不调模型
    assert n == 1
    _fresh()


def test_backlog_cap_and_checkpoint():
    """大积压：一个群一次 700 条 → 只整理最近 240 条 + 少量旧的 @我，调用不超过 3 次，水位一次到位；
    中途某块失败时，前面已处理的块水位保留，重试不从头再来。"""
    import asyncio
    from fastapi import HTTPException
    _fresh(); _auto_reset()
    s = dict(app_mod.settings(), auto_on=True)
    for i in range(700):
        _ingest("积压群", f"第{i}条消息内容比较长一点点用来不被噪音过滤{i}")
    with app_mod.db() as x:
        top = x.execute("SELECT MAX(id) i FROM msgs").fetchone()["i"]
        x.execute("UPDATE msgs SET at_me=1 WHERE chat='积压群' AND text LIKE '第5条%' OR text LIKE '第6条%'")
    sent = []

    async def ok(msgs, as_json=False):
        if msgs[0]["content"].startswith("【群更新】"):
            sent.append(msgs[1]["content"])
        return json.dumps({"summary": "积压", "new": []})
    old = app_mod.llm; app_mod.llm = ok
    try:
        async def go():
            await app_mod.auto_tick(s, time.time() + 999)
            while app_mod.AUTO_TASKS:
                await asyncio.gather(*list(app_mod.AUTO_TASKS))
        asyncio.run(go())
        assert 1 <= len(sent) <= 3, len(sent)
        body = "\n".join(sent)
        assert f"第699条" in body and "第5条" in body and "第300条" not in body
        with app_mod.db() as x:
            wm = x.execute("SELECT last_msg_id FROM chat_state WHERE chat='积压群'").fetchone()["last_msg_id"]
        assert wm == top
        assert app_mod.cap_backlog([({"at_me": 0, "id": i}, "n") for i in range(10)])[1] == 0
        # 检查点：第二块失败，第一块的水位保留
        _fresh(); _auto_reset()
        for i in range(300):
            _ingest("检查点群", f"检查点消息{i}号需要处理一下不要被过滤掉")
        n = [0]

        async def flaky(msgs, as_json=False):
            if not msgs[0]["content"].startswith("【群更新】"):
                return "头条"
            n[0] += 1
            if n[0] == 2:
                raise HTTPException(502, "boom")
            return json.dumps({"summary": "x", "new": []})
        app_mod.llm = flaky
        rows = [dict(r) for r in app_mod.pending_info(s)[0] if r["chat"] == "检查点群"]
        stats = {"new": 0, "sent": 0, "calls": 0, "chats": 1, "changed": 0, "secs": 0.0, "errors": 0, "skipped": 0}
        try:
            asyncio.run(app_mod.update_chat("QQ", "检查点群", rows, s, stats))
        except HTTPException:
            pass
        with app_mod.db() as x:
            st = x.execute("SELECT last_msg_id FROM chat_state WHERE chat='检查点群'").fetchone()
        assert st and st["last_msg_id"] > 0 and st["last_msg_id"] < max(r["id"] for r in rows)
    finally:
        app_mod.llm = old
        _fresh()


def test_img_rkey_swap():
    k = {"group": "NEWG", "private": "NEWP"}
    u = "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=ABC&spec=0&rkey=OLD"
    assert app_mod.swap_rkey(u, k) == "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=ABC&spec=0&rkey=NEWG"
    assert app_mod.swap_rkey(u.replace("1407", "1406"), k).endswith("rkey=NEWP")
    assert app_mod.swap_rkey("https://evil.com/download?fileid=1&rkey=x", k) == ""
    assert app_mod.swap_rkey("https://gchat.qpic.cn/gchatpic_new/1/2-3/0", k) == ""


def test_clean_qq_cache(tmp_path):
    import os as _o
    pic = tmp_path / "nt_qq_x" / "nt_data" / "Pic" / "2026-10" / "Ori"; pic.mkdir(parents=True)
    keep = tmp_path / "nt_qq_x" / "nt_db"; keep.mkdir(parents=True)
    old, new, db_ = pic / "a.jpg", pic / "b.jpg", keep / "msg.db"
    for f in (old, new, db_):
        f.write_bytes(b"x" * 100)
    t = time.time()
    _o.utime(old, (t - 86400, t - 86400)); _o.utime(db_, (t - 86400, t - 86400))
    n, freed = app_mod.clean_qq_cache(str(tmp_path), now=t)
    assert n == 1 and freed == 100 and not old.exists() and new.exists() and db_.exists()
    assert (tmp_path / "nt_qq_x" / "nt_data" / "Pic").is_dir()


def test_headline_drops_done_item():
    import json as _j
    _fresh()
    now = int(time.time())
    with app_mod.db() as x:
        x.execute("DELETE FROM digests")
        x.execute("INSERT INTO items(kind,title,status,first_ts,updated_ts,source,chat) VALUES('todo','前往大活3405参加紧急会议','done',?,?,'QQ','g1')", (now, now))
        x.execute("INSERT INTO items(kind,title,due,status,first_ts,updated_ts,source,chat) VALUES('todo','今晚按时打卡学习','10月09日 今晚','open',?,?,'QQ','g2')", (now, now))
        x.execute("INSERT INTO digests(ts,hours,body) VALUES(?,?,?)", (now, 24, _j.dumps({"headline": "请即刻前往大活3405参加紧急会议",
                  "todos": [{"title": "今晚按时打卡学习", "due": "10月09日 今晚", "done": False}], "notices": []}, ensure_ascii=False)))
    with TestClient(app_mod.app) as cc:
        h = cc.get("/api/state", headers=AUTH).json()["digest"]["headline"]
    assert "3405" not in h and "打卡" in h
    with app_mod.db() as x:
        assert "打卡" in _j.loads(x.execute("SELECT body FROM digests").fetchone()["body"])["headline"]
    _fresh()


def _run_auto_once():
    import asyncio
    s = dict(app_mod.settings(), auto_on=True)

    async def go():
        await app_mod.auto_tick(s, time.time() + 999)
        while app_mod.AUTO_TASKS:
            await asyncio.gather(*list(app_mod.AUTO_TASKS))
    asyncio.run(go())


def test_rejected_chunk_bisected_not_stuck_400_and_garbled():
    """模型对某几条一直回 400，或一直吐不出合法 JSON：拆块跳过，其余照常整理，不让待整理永远清不掉。"""
    from fastapi import HTTPException
    _fresh(); _auto_reset()
    for i in range(6):
        _ingest("卡壳群", f"第{i}条：周五 18:00 前交材料{i}" if i not in (2, 4) else f"毒消息POISON{i}")
    app_mod.LLM_LAST_OK[0] = time.time()  # 模型最近是好的

    async def fake(msgs, as_json=False):
        if not msgs[0]["content"].startswith("【群更新】"):
            return "头条"
        body = msgs[1]["content"]
        if "POISON2" in body:
            raise HTTPException(502, "bad request", headers={"x-llm-code": "400"})
        if "POISON4" in body:
            return "这不是 JSON"
        return json.dumps({"summary": "交材料", "new": [{"kind": "todo", "title": "交材料", "due": "周五 18:00", "urgency": "high"}]})
    old = app_mod.llm; app_mod.llm = fake
    try:
        _run_auto_once()
        st = c.get("/api/state", headers=AUTH).json()
        assert any(t["title"] == "交材料" for t in st["digest"]["todos"])
        assert st["pending"]["msgs"] == 0
        assert not app_mod.AUTO["fail"]
    finally:
        app_mod.llm = old


def test_400_with_no_recent_success_is_config_error_not_skipped():
    """模型从没成功过就回 400（多半是模型名/参数配错）：不能把消息当毒消息全跳过，要留着等修好。"""
    from fastapi import HTTPException
    _fresh(); _auto_reset()
    for i in range(3):
        _ingest("配置错群", f"周五 18:00 前交材料{i}")
    app_mod.LLM_LAST_OK[0] = 0.0

    async def fake(msgs, as_json=False):
        raise HTTPException(502, "model not found", headers={"x-llm-code": "400"})
    old = app_mod.llm; app_mod.llm = fake
    try:
        _run_auto_once()
        assert app_mod.AUTO["fail"]
        assert c.get("/api/state", headers=AUTH).json()["pending"]["msgs"] == 3
    finally:
        app_mod.llm = old


def test_at_by_and_day_start_in_state():
    """@我 的消息被哪条待办认领、今天从几点算：前端靠它们在勾选后实时重算「没处理的 @我」「今天新增」。"""
    _fresh(); _auto_reset()
    now = int(time.time())
    with app_mod.db() as x:
        mid = x.execute("INSERT INTO msgs(ts,source,chat,sender,text,at_me) VALUES(?,?,?,?,?,1)",
                        (now, "QQ", "认领群", "老师", "@我 周五 18:00 前交材料", )).lastrowid
        x.execute("INSERT INTO items(source,chat,kind,title,due,urgency,status,first_ts,updated_ts,msg_ids,at_me) "
                  "VALUES('QQ','认领群','todo','交材料','周五 18:00','high','open',?,?,?,1)", (now, now, str(mid)))
        k = "item:%d" % x.execute("SELECT MAX(id) i FROM items").fetchone()["i"]
        x.execute("INSERT INTO chat_state(source,chat,last_msg_id,summary,updated_ts) VALUES('QQ','认领群',?,'交材料',?)", (mid, now))
    app_mod.kv_set("scan_id", mid)
    import asyncio
    asyncio.run(app_mod.refresh_live(dict(app_mod.settings()), {}, now, model_head=False))
    st = c.get("/api/state?hours=24", headers=AUTH).json()
    assert isinstance(st["day_start"], int) and st["day_start"] <= time.time() < st["day_start"] + 86400
    a = [m for m in st["at_me"] if m["id"] == mid][0]
    assert a["covered"] and a["by"] == k
    t = [t for t in st["digest"]["todos"] if t["key"] == k][0]
    assert t["first_ts"] >= st["day_start"]


def test_due_change_rearms_reminder_and_reopens_expired():
    """会议改期：截止时间变了要重新提醒；过期的事项群里又给了个还没到的新时间，要重新打开（沿用原 id）。"""
    _fresh()
    from datetime import datetime, timedelta
    now = int(time.time())
    fmt = lambda d: d.strftime("%Y-%m-%d %H:%M")
    t1 = fmt(datetime.fromtimestamp(now, app_mod.TZ) + timedelta(days=2))
    t2 = fmt(datetime.fromtimestamp(now, app_mod.TZ) + timedelta(days=3))
    with app_mod.db() as x:
        i = x.execute("INSERT INTO items(source,chat,kind,title,due,urgency,status,first_ts,updated_ts,reminded) "
                      "VALUES('QQ','改期群','todo','开组会',?,'mid','open',?,?,1)", (t1, now, now)).lastrowid
    app_mod.apply_changes("QQ", "改期群", {"update": [{"id": i, "due": t2}]})
    with app_mod.db() as x:
        r = x.execute("SELECT due, reminded FROM items WHERE id=?", (i,)).fetchone()
    assert r["due"] == t2 and r["reminded"] == 0
    # 过期的事项：模型把改期后的同一件事当新事项报
    old = fmt(datetime.fromtimestamp(now, app_mod.TZ) - timedelta(days=2))
    with app_mod.db() as x:
        j = x.execute("INSERT INTO items(source,chat,kind,title,due,urgency,status,first_ts,updated_ts,reminded) "
                      "VALUES('QQ','改期群','todo','交实验报告',?,'mid','expired',?,?,1)", (old, now - 86400 * 3, now - 3600)).lastrowid
    app_mod.apply_changes("QQ", "改期群", {"new": [{"kind": "todo", "title": "交实验报告", "due": t1}]})
    with app_mod.db() as x:
        r = x.execute("SELECT status, due, reminded FROM items WHERE id=?", (j,)).fetchone()
        n = x.execute("SELECT COUNT(*) n FROM items WHERE title='交实验报告'").fetchone()["n"]
    assert (r["status"], r["due"], r["reminded"], n) == ("open", t1, 0, 1)
    # 用户亲手勾了完成的不复活
    with app_mod.db() as x:
        k = x.execute("INSERT INTO items(source,chat,kind,title,due,urgency,status,first_ts,updated_ts) "
                      "VALUES('QQ','改期群','todo','报名比赛',?,'mid','done',?,?)", (old, now - 86400 * 3, now - 3600)).lastrowid
    app_mod.apply_changes("QQ", "改期群", {"new": [{"kind": "todo", "title": "报名比赛", "due": t1}]})
    with app_mod.db() as x:
        assert x.execute("SELECT status FROM items WHERE id=?", (k,)).fetchone()["status"] == "done"


def test_norm_ts_garbage_and_order():
    """0.33.3：消息时间容错（毫秒/字符串/未来/垃圾值不再 500 或把消息挤出时间窗），群聊页按发送时间排。"""
    n = int(time.time())
    f = app_mod.norm_ts
    assert f(n - 60) == n - 60 and f(str(n - 60)) == n - 60
    assert f((n - 60) * 1000) == n - 60               # 毫秒
    assert f(None) == f("") == f("乱写") == f(0) == f(-5) == f([]) in range(n, n + 3)
    assert f(n + 86400) in range(n, n + 3)            # 对方手机时间在未来
    assert f(n - 90 * 86400) in range(n, n + 3)       # 早于保留期
    iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(n - 3600))
    assert abs(f(iso) - (n - 3600)) < 3600 * 9 + 5    # ISO 字符串按服务器时区解析，不抛错
    chat = "时序群"
    for ts, txt in ((n - 100, "第二条先到"), (n - 300, "第一条迟到"), (n - 10, "第三条")):
        assert c.post("/ingest?token=tok", json={"chat": chat, "sender": "甲", "text": txt, "ts": ts}).status_code == 200
    assert c.post("/ingest?token=tok", json={"chat": chat, "sender": "甲", "text": "毫秒时间", "ts": (n - 5) * 1000}).status_code == 200
    assert c.post("/ingest?token=tok", json={"chat": chat, "sender": "甲", "text": "垃圾时间", "ts": "abc"}).status_code == 200
    ms = c.get(f"/api/messages?chat={chat}", headers=AUTH).json()
    assert [m["text"] for m in ms] == ["第一条迟到", "第二条先到", "第三条", "毫秒时间", "垃圾时间"]
    assert all(abs(m["ts"] - n) < 400 for m in ms)
    # 翻页游标仍是 id：before=最小 id 取到更早的，after=最大 id 只取更新的
    ids = [m["id"] for m in ms]
    assert c.get(f"/api/messages?chat={chat}&after={max(ids)}", headers=AUTH).json() == []
    assert len(c.get(f"/api/messages?chat={chat}&before={max(ids)}", headers=AUTH).json()) == 4


def test_followup_ask_keeps_topic_terms_and_no_repeat_prompt(monkeypatch):
    """追问「那后来定了吗」自己没有检索词：消息多、被截取时，要带上上一个问题的词，话题消息才不会漏。"""
    monkeypatch.setattr(app_mod, "ASK_LINES", 6)
    texts = ["团建地点改到莲花山公园", "团建经费每人五十"] + [f"闲聊第{i}条今天吃什么好呢大家" for i in range(30)]
    ids = _seed_chat("追问群", texts)
    seen = {}

    def reply(m):
        seen["sys"] = m[0]["content"]; seen["ctx"] = m[1]["content"]
        return "好的"
    calls, fake = _llm_spy(reply)
    monkeypatch.setattr(app_mod, "llm", fake)
    hist = [{"role": "user", "content": "团建在哪里"}, {"role": "assistant", "content": "在公园"}]
    c.post("/api/ask", headers=AUTH, json={"q": "那后来定了吗", "chat": "追问群", "source": "QQ", "history": hist})
    assert "莲花山公园" in seen["ctx"]
    assert "不要再讲一遍" in seen["sys"]


def test_home_empty_todo_line_follows_pending():
    h = open(os.path.join(os.path.dirname(__file__), "..", "digest", "index.html"), encoding="utf-8").read()
    assert "emptySub(st)" in h and 'id="emptysub"' in h and "还有 ${n} 条新消息没整理进来" in h


def _fake_group_info(monkeypatch, names):
    class R:
        def __init__(s, n): s.n = n
        def json(s): return {"data": {"group_name": s.n} if s.n else {}}

    class Cl:
        def __init__(s, *a, **k): pass
        async def __aenter__(s): return s
        async def __aexit__(s, *a): return False
        async def post(s, url, json=None, headers=None): return R(names.get(json["group_id"]))
    monkeypatch.setattr(app_mod.httpx, "AsyncClient", Cl)


def test_group_rename_keeps_history_items_and_mode(monkeypatch):
    """QQ 群改名：消息、事项、群档位、已完成记录全部跟到新名字，群不会拆成两个。"""
    import asyncio
    gid = 9101
    ids = _seed_chat("旧群名", ["周五交表"])
    with app_mod.db() as d:
        d.execute("INSERT INTO items(source,chat,kind,title,status,first_ts,updated_ts) VALUES('QQ','旧群名','todo','交表','open',1,1)")
        d.execute("INSERT INTO chat_state(source,chat,last_msg_id,summary,updated_ts) VALUES('QQ','旧群名',?,'旧摘要',5)", (ids[0],))
        d.execute("INSERT OR REPLACE INTO todo_done(k,ts,title,chat) VALUES('k1',1,'交表','旧群名')")
    app_mod.set_modes([("QQ", "旧群名")], "focus")
    names = {gid: "旧群名"}
    _fake_group_info(monkeypatch, names)
    app_mod._group_names.pop(gid, None); app_mod._group_miss.pop(gid, None)
    assert asyncio.run(app_mod.group_name(gid)) == "旧群名"   # 第一次记下 群号→名字
    names[gid] = "新群名"
    app_mod._group_ts[gid] = time.time() - app_mod.GROUP_NAME_TTL - 5; app_mod._group_miss.pop(gid, None)   # 缓存过期
    assert asyncio.run(app_mod.group_name(gid)) == "新群名"
    with app_mod.db() as d:
        assert d.execute("SELECT COUNT(*) FROM msgs WHERE chat='旧群名'").fetchone()[0] == 0
        assert d.execute("SELECT COUNT(*) FROM msgs WHERE chat='新群名'").fetchone()[0] == 1
        assert d.execute("SELECT chat FROM items WHERE title='交表'").fetchone()[0] == "新群名"
        assert d.execute("SELECT summary FROM chat_state WHERE chat='新群名'").fetchone()[0] == "旧摘要"
        assert d.execute("SELECT chat FROM todo_done WHERE k='k1'").fetchone()[0] == "新群名"
    assert app_mod.chat_mode("QQ", "新群名", app_mod.settings()) == "focus"
    assert "QQ|旧群名" not in (app_mod.settings().get("modes") or {})


def test_group_rename_from_fallback_name_and_twin_guard(monkeypatch):
    """NapCat 一度没查到名字、消息存在「群9102」名下：查到真名后搬过去；两个群同名时不抢对方的历史。"""
    import asyncio
    _seed_chat("群9102", ["a", "b"])
    _fake_group_info(monkeypatch, {9102: "补上名字的群"})
    app_mod._group_names.pop(9102, None); app_mod._group_miss.pop(9102, None)
    assert asyncio.run(app_mod.group_name(9102)) == "补上名字的群"
    assert len(c.get("/api/messages?chat=补上名字的群", headers=AUTH).json()) == 2
    # 双胞胎：9103 和 9104 都叫「撞名群」，9104 改名后不能把 9103 的历史带走
    app_mod.kv_set("gname:9103", "撞名群"); app_mod.kv_set("gname:9104", "撞名群")
    _seed_chat("撞名群", ["x"])
    _fake_group_info(monkeypatch, {9104: "别的名"})
    app_mod._group_names.pop(9104, None); app_mod._group_miss.pop(9104, None)
    asyncio.run(app_mod.group_name(9104))
    with app_mod.db() as d:
        assert d.execute("SELECT COUNT(*) FROM msgs WHERE chat='撞名群'").fetchone()[0] == 1


def test_activity_hours_follow_app_tz():
    """活跃时段按 APP_TZ 分，不跟服务器（容器里通常是 UTC）的本地时间。"""
    from datetime import datetime
    now = int(time.time())
    ts = int(datetime.now(app_mod.TZ).replace(hour=3, minute=30, second=0, microsecond=0).timestamp())
    if ts > now:
        ts -= 86400
    before = app_mod.activity_stats(2, "活跃测试源", now)["hours"][3]
    _seed_chat("活跃群", ["夜里的消息"], source="活跃测试源")
    with app_mod.db() as d:
        d.execute("UPDATE msgs SET ts=? WHERE chat='活跃群'", (ts,))
    assert app_mod.activity_stats(2, "活跃测试源", now)["hours"][3] == before + 1


# ================= 0.33.7：待整理不再卡住 / 查寝点名变待办 / 头条不挂已完成的事 =================
def _drain(s, now=None):
    import asyncio

    async def go():
        await app_mod.auto_tick(s, now or time.time() + 999)
        while app_mod.AUTO_TASKS:
            await asyncio.gather(*list(app_mod.AUTO_TASKS))
    asyncio.run(go())


def _pend(s):
    return app_mod.pending_info(s)[1]["msgs"]


def test_pending_filtered_kinds_never_counted_and_mode_change_clears():
    """会被过滤的消息（噪音/表情/图片/广告/撤回/只看@我/不看）一条都不算待整理；整理中途改成「只看@我」，已计入的立刻清零、水位推进。"""
    _fresh(); _auto_reset()
    s = dict(app_mod.settings(), auto_on=True, modes={"微信|过滤群": "normal", "微信|不看群": "off", "微信|只看群": "atonly"})
    for t in ["收到", "[图片]", "[表情][动画表情]", "哈哈哈", "张三撤回了一条消息", "【神价】券后 9.9 包邮 今晚 8 点 https://u.jd.com/x"]:
        _ingest("过滤群", t)
    _ingest("不看群", "明天 8 点开会"); _ingest("只看群", "@全体成员 今晚查寝")
    assert _pend(s) == 0
    app_mod.auto_sweep(s)
    with app_mod.db() as x:
        top = x.execute("SELECT MAX(id) i FROM msgs").fetchone()["i"]
    assert app_mod._scan_rows() == [] and int(app_mod.kv_get("scan_id")) == top  # 水位直接推到最后
    # 两条真消息先算进待整理，然后用户把群改成「只看@我」：马上不算了，水位推进
    _ingest("过滤群", "周五 18:00 前交实验报告"); _ingest("过滤群", "地点改到东区 3 楼")
    assert _pend(s) == 2
    s2 = dict(s, modes={**s["modes"], "微信|过滤群": "atonly"})
    assert _pend(s2) == 0
    app_mod.auto_sweep(s2)
    assert app_mod._scan_rows() == []


def test_pending_late_ts_rename_and_stale_watermark_all_drain():
    """迟到消息（发送时间是两小时前）、群改名后旧名下的消息、整理状态里水位落后：都能在一次巡检里整理完，待整理归零。"""
    _fresh(); _auto_reset()
    s = dict(app_mod.settings(), auto_on=True)
    fake = FakeLLM({})
    old = app_mod.llm; app_mod.llm = fake
    try:
        c.post("/ingest?token=tok", json={"chat": "迟到群", "sender": "甲", "text": "明天 9 点集合去实习", "ts": int(time.time()) - 7200})
        c.post("/ingest?token=tok", json={"source": "QQ", "chat": "群777", "sender": "甲", "text": "周日下午 3 点开班会"})
        app_mod.rename_chat("QQ", "群777", "电信1班")   # NapCat 刚查到真名
        assert {r["chat"] for r in app_mod.pending_info(s)[0]} == {"迟到群", "电信1班"}
        with app_mod.db() as x:  # 有个群的整理状态落后很多（以前的版本留下的）
            x.execute("INSERT OR REPLACE INTO chat_state(source,chat,last_msg_id,summary,updated_ts) VALUES('微信','迟到群',0,'',0)")
        _drain(s, time.time())  # 不传未来时间：真实时钟下，迟到消息按收到时间防抖，最迟 60 秒
        _drain(s, time.time() + 61)
        assert _pend(s) == 0 and sorted(fake.chats()) == ["电信1班", "迟到群"]
    finally:
        app_mod.llm = old; _auto_reset(); _fresh()


def test_stuck_pending_two_msgs_real_repro_drains_within_minutes():
    """真实复现：首页「还有 2 条新消息没整理进来」8 分钟不变。别的群一直整理成功，只有这个群的模型调用一直失败（503/超时）。
    以前：退避 30→60→…→600 秒，状态永远不报错，「正在自动整理」挂着不动。现在：10/20/40 秒重试，连续 3 次失败就按规则提取待办后跳过，
    两分钟内待整理归零；查寝这种事规则照样建成待办。"""
    import asyncio
    _fresh(); _auto_reset()
    s = dict(app_mod.settings(), auto_on=True)

    async def flaky(msgs, as_json=False):
        sysm = msgs[0]["content"]
        if not sysm.startswith("【群更新】"):
            return "头条"
        if "「卡住群」" in sysm:
            raise app_mod.HTTPException(502, "大模型服务商出故障（503）", headers={"x-llm-code": "503"})
        app_mod.LLM_LAST_OK[0] = time.time()   # 别的群：模型是好的
        return json.dumps({"summary": "闲聊"})
    old = app_mod.llm; app_mod.llm = flaky
    try:
        _ingest("卡住群", "今晚导员会来查寝", "况弘铭"); _ingest("卡住群", "大家把桌面收一下")
        assert _pend(s) == 2
        T0 = time.time()
        t, seen = T0, []
        while t < T0 + 8 * 60:
            _ingest("热闹群", f"随便聊聊第{int(t)}条消息内容")   # 别的群一直有消息、一直整理成功
            _drain(s, t)
            seen.append((round(t - T0), _pend(s)))
            st = app_mod.pending_info(s)[1]
            if st["msgs"] == 0 or all(x[1] == 0 for x in seen[-1:]) and seen[-1][0] > 0 and not app_mod.pending_info(s)[0]:
                break
            t += 5
        stuck = [r for r in app_mod.pending_info(s)[0] if r["chat"] == "卡住群"]
        assert not stuck and t - T0 <= 150, seen                                     # 两分半以内一定清掉
        with app_mod.db() as x:
            its = x.execute("SELECT * FROM items WHERE chat='卡住群' AND kind='todo'").fetchall()
        assert [i["title"] for i in its] == ["今晚导员查寝：在寝室并收拾好"]           # 模型没整理成，规则照样给待办
        assert "卡住群" not in {k[1] for k in app_mod.AUTO["fail"]}
    finally:
        app_mod.llm = old; _auto_reset(); _fresh()


def test_hung_llm_bounded_and_status_not_forever_running(monkeypatch):
    """模型卡住不回：单群整理有上限（AUTO_RUN_LIMIT），到点算失败，pending 里写明「失败、多少秒后重试」，不再一直「正在整理」。"""
    import asyncio
    _fresh(); _auto_reset()
    s = dict(app_mod.settings(), auto_on=True)
    monkeypatch.setattr(app_mod, "AUTO_RUN_LIMIT", 0.3)

    async def hang(msgs, as_json=False):
        await asyncio.sleep(30)
    old = app_mod.llm; app_mod.llm = hang
    try:
        _ingest("卡死群", "周五 18:00 前交表")
        _arrive("卡死群", -70)
        t0 = time.time()
        _drain(s, time.time())
        assert time.time() - t0 < 3 and not app_mod.AUTO["running"]
        p = app_mod.pending_info(s)[1]
        assert p["msgs"] == 1 and p["failing"] == 1 and p["running"] == 0 and 0 < p["retry_in"] <= 10
        assert "超时" in p["reason"] or p["reason"]
        assert c.get("/api/rev", headers=AUTH).json()["pending"]["failing"] == 1
    finally:
        app_mod.llm = old; _auto_reset(); _fresh()


def test_force_sweep_beats_urgent_queue():
    """忙的时候要紧群一直排前面，普通群的几条以前可能一直轮不上：等了 60 秒的排最前，最小间隔也不挡。"""
    _fresh(); _auto_reset()
    s = dict(app_mod.settings(), auto_on=True)
    _ingest("老实群", "周末大家一起去图书馆自习吧")
    _arrive("老实群", -35)
    app_mod.AUTO["last_run"][("微信", "老实群")] = time.time() - 5   # 刚整理过也不挡
    for i in range(8):
        _ingest(f"吵群{i}", f"@全体成员 第{i}个通知明天交")
    ready = app_mod.auto_plan(s, time.time() + 6)[0]
    assert ready[0][0][1] == "老实群" and ready[0][2] == "force"
    _auto_reset(); _fresh()


def test_frontend_pend_text_not_forever_auto():
    h = open(os.path.join(os.path.dirname(__file__), "..", "digest", "index.html"), encoding="utf-8").read()
    assert "function pendText" in h and "等着整理" in h and "自动重试" in h and "age<75" in h
    assert "swapHead(" in h and "forceQuiet()" in h


def test_rule_event_titles_and_negatives():
    now = time.time()
    nd = app_mod.datetime.fromtimestamp(now, app_mod.TZ)
    today = f"{nd.month}月{nd.day:02d}日"
    R = lambda t, ago=60, at=0: {"text": t, "ts": int(now - ago), "at_me": at, "sender": "甲", "chat": "g", "source": "QQ"}
    ev = app_mod.rule_event(R("今晚导员会来查寝"), now)
    assert ev and ev[0] == "今晚导员查寝：在寝室并收拾好" and ev[1] == f"{today} 今晚" and ev[3] == "high"
    assert app_mod.parse_due(ev[1], nd).hour == 23
    ev = app_mod.rule_event(R("下课杨导要点到"), now)
    assert ev and ev[0] == "下课后杨导点到：按时到场"
    assert app_mod.rule_event(R("下课杨导要点到", ago=5 * 3600), now) is None         # 早上说的「下课」，晚上就不算了
    ev = app_mod.rule_event(R("明天下午 3 点在 B201 开班会"), now)
    assert ev and ev[0].startswith("明天") and "班会" in ev[0] and "15:00" in ev[1] or "3:00" in ev[1]
    ev = app_mod.rule_event(R("今晚 7 点班会，地点 B201"), now)
    assert ev and ev[0] == "今晚班会：按时参加" and ev[1].endswith("晚上 7:00")
    for t in ["今天考试好难啊", "今晚 8 点直播抽奖签到领红包", "今天的会议纪要我发群里了",
              "昨天查寝好严", "今晚查寝吗？", "考试加油", "【神价】今晚 8 点秒杀考试资料 券后 9.9", "哈哈哈", "查寝的时候记得关灯了吗", "已经点过名了"]:
        assert app_mod.rule_event(R(t), now) is None, t


def test_inspection_becomes_todo_even_if_model_only_summarizes():
    """用户真实场景：24电信1班「今晚导员会来查寝」只进了群要点。现在模型只写要点也会补成待办；模型只建成通知的改成待办；
    模型已经建了的不重复；勾完成后再有人说一遍不复活；广告群和只看@我的群不生成。"""
    _fresh(); _auto_reset()
    s = dict(app_mod.settings(), auto_on=True, modes={"QQ|24电信1班": "normal", "QQ|线报群": "normal", "QQ|静音班群": "atonly"})
    app_mod.save_settings({"modes": {**(app_mod.settings().get("modes") or {}), **s["modes"]}})
    s = dict(app_mod.settings(), auto_on=True)
    fake = FakeLLM({"24电信1班": lambda u: {"summary": "今晚导员会来查寝"},
                    "通知群": lambda u: {"summary": "明早点名", "new": [{"kind": "notice", "title": "明早 8 点辅导员点名", "urgency": "mid"}]},
                    "好模型群": lambda u: {"summary": "查寝", "new": [{"kind": "todo", "title": "收拾寝室迎接查寝", "due": "今晚", "msg_ids": [int(re.search(r"#(\d+)", u.split("新消息（", 1)[1]).group(1))]}]}})
    old = app_mod.llm; app_mod.llm = fake
    try:
        q = lambda chat, text, sender="况弘铭": c.post("/ingest?token=tok", json={"source": "QQ", "chat": chat, "sender": sender, "text": text})
        q("24电信1班", "今晚导员会来查寝")
        q("通知群", "明早 8 点辅导员点名，都别迟到")
        q("好模型群", "今晚宿管查寝")
        q("线报群", "@全体成员 今晚 8 点秒杀，考试资料包邮")
        q("静音班群", "@全体成员 今晚查寝")
        _drain(s)
        with app_mod.db() as x:
            its = {(r["chat"], r["kind"], r["title"]) for r in x.execute("SELECT * FROM items WHERE status='open'")}
        assert ("24电信1班", "todo", "今晚导员查寝：在寝室并收拾好") in its
        assert any(ch == "通知群" and k == "todo" and "点名" in t for ch, k, t in its)            # 通知改成了待办
        assert [t for ch, k, t in its if ch == "好模型群"] == ["收拾寝室迎接查寝"]                # 模型建了就不重复
        assert not any(ch in ("线报群", "静音班群") for ch, _, _ in its)                          # 广告 / 只看@我 不生成
        st = c.get("/api/state", headers=AUTH).json()
        t = next(t for t in st["digest"]["todos"] if t["chat"] == "24电信1班")
        assert t["urgency"] == "high" and t["due"].endswith("今晚")
        c.post("/api/todo", headers=AUTH, json={"key": t["key"], "done": True})
        q("24电信1班", "今晚查寝别忘了", "班长")
        _drain(s)
        with app_mod.db() as x:
            assert x.execute("SELECT COUNT(*) FROM items WHERE chat='24电信1班'").fetchone()[0] == 1   # 勾完成的不复活
        # @全体成员 单独一条：算在同一个人刚发的通知上
        q("24电信1班", "下课杨导要点到", "吴声武"); q("24电信1班", "@全体成员", "吴声武")
        _drain(s)
        with app_mod.db() as x:
            r = x.execute("SELECT * FROM items WHERE chat='24电信1班' AND title LIKE '%点到%'").fetchone()
        assert r and r["urgency"] == "high" and r["title"] == "下课后杨导点到：按时到场"
        assert "查寝" in app_mod.chat_prompt(s, "QQ", "x") and "陈述句" in app_mod.chat_prompt(s, "QQ", "x")
    finally:
        app_mod.llm = old; _auto_reset(); _fresh()


def test_rule_backfill_recent_inspection():
    _fresh(); _auto_reset()
    with app_mod.db() as x:
        x.execute("INSERT INTO msgs(ts,source,chat,sender,text,at_me) VALUES(?,?,?,?,?,0)", (int(time.time()) - 1800, "QQ", "补建班群", "况", "今晚导员会来查寝"))
        x.execute("INSERT INTO msgs(ts,source,chat,sender,text,at_me) VALUES(?,?,?,?,?,0)", (int(time.time()) - 4 * 3600, "QQ", "补建班群", "吴", "下课杨导要点到"))
    assert app_mod.rule_backfill() >= 1
    with app_mod.db() as x:
        assert [r["title"] for r in x.execute("SELECT title FROM items WHERE chat='补建班群'")] == ["今晚导员查寝：在寝室并收拾好"]
    assert app_mod.rule_backfill() == 0
    _fresh()


def test_headline_done_item_real_path_never_comes_back():
    """真实复现：模型头条「learning群通知紧急开会，需立刻前往大活3405」，用户勾完成那件待办。
    以前：整句相似度只有 0.44（< 0.6），头条一直挂着；模型下次照着群要点又写回来。现在：勾完成的响应里就带新头条；
    再来新消息触发自动整理、模型又写回同一句，头条也不会再说这件事；手动「整理」也一样。"""
    import asyncio
    _fresh(); _auto_reset()
    s = dict(app_mod.settings(), auto_on=True)
    app_mod.kv_set("head_ts", 0)
    BAD = "learning群通知紧急开会，需立刻前往大活3405"

    class F(FakeLLM):
        async def __call__(self, msgs, as_json=False):
            if not msgs[0]["content"].startswith("【群更新】"):
                self.calls.append(("head", "", msgs[-1]["content"]))
                return BAD   # 模型照着群要点（「紧急开会，需立刻前往大活3405」）一直写这句
            return await super().__call__(msgs, as_json)
    fake = F({"learning": {"summary": "紧急开会，需立刻前往大活3405", "new": [{"kind": "todo", "title": "前往大活3405开会", "urgency": "high"}]},
              "27年蓝桥杯电子竞赛群": {"summary": "打卡", "new": [{"kind": "todo", "title": "今晚按时打卡学习", "due": "今晚", "urgency": "high"}]},
              "闲聊群": {"summary": "闲聊"}})
    old = app_mod.llm; app_mod.llm = fake
    try:
        q = lambda chat, text: c.post("/ingest?token=tok", json={"source": "QQ", "chat": chat, "sender": "甲", "text": text})
        q("learning", "@全体成员 紧急开会，立刻到大活3405")
        q("27年蓝桥杯电子竞赛群", "今晚按时打卡学习，优先扫脸打卡")
        _drain(s)
        st = c.get("/api/state", headers=AUTH).json()
        assert st["digest"]["headline"] == BAD                                    # 事还没做：模型头条照常显示
        t = next(t for t in st["digest"]["todos"] if "3405" in t["title"])
        r = c.post("/api/todo", headers=AUTH, json={"key": t["key"], "done": True}).json()
        assert r["headline"] and "3405" not in r["headline"] and "打卡" in r["headline"]   # 勾完成的响应里就是新头条
        assert "3405" not in c.get("/api/state", headers=AUTH).json()["digest"]["headline"]
        with app_mod.db() as x:
            assert "3405" not in json.loads(x.execute("SELECT body FROM digests ORDER BY id DESC LIMIT 1").fetchone()["body"])["headline"]
        # 再来新消息触发自动整理：模型头条又写回那句（群要点里还有），也不会显示
        app_mod.kv_set("head_ts", 0)
        q("27年蓝桥杯电子竞赛群", "@全体成员 明天 18:00 前交周报")
        fake.per_chat["27年蓝桥杯电子竞赛群"] = {"summary": "打卡、周报", "new": [{"kind": "todo", "title": "交周报", "due": "明天 18:00", "urgency": "high"}]}
        _drain(s)
        while app_mod.AUTO_TASKS:
            asyncio.run(asyncio.sleep(0.05))
        assert any(x[0] == "head" for x in fake.calls[-3:])                        # 模型确实又写了一次头条
        h = c.get("/api/state", headers=AUTH).json()["digest"]["headline"]
        assert "3405" not in h and h
        with app_mod.db() as x:
            assert "3405" not in json.loads(x.execute("SELECT body FROM digests ORDER BY id DESC LIMIT 1").fetchone()["body"])["headline"]
        # 手动「整理」（新建一期、模型写头条）也一样
        q("闲聊群", "周末去哪玩呢大家想想")
        d = c.post("/api/digest", headers=AUTH, json={"hours": 24}).json()
        assert "3405" not in d["headline"]
        assert "3405" not in c.get("/api/state", headers=AUTH).json()["digest"]["headline"]
    finally:
        app_mod.llm = old; _auto_reset(); _fresh()


def test_immediate_items_expire_after_hours():
    """「立刻/马上前往开会」这种没写具体截止的即时事项，6 小时后自动算过期，不再挂在首页和头条上。"""
    _fresh()
    now = int(time.time())
    with app_mod.db() as x:
        x.execute("INSERT INTO items(kind,title,detail,due,status,first_ts,updated_ts,source,chat,quote) VALUES('todo','前往大活3405开会','需立刻前往','','open',?,?,'QQ','learning','紧急开会，立刻到大活3405')", (now - 7 * 3600, now - 60))
        x.execute("INSERT INTO items(kind,title,due,status,first_ts,updated_ts,source,chat,quote) VALUES('todo','马上交签到表','10月09日 马上','open',?,?,'QQ','g','马上交签到表')", (now - 7 * 3600, now))
        x.execute("INSERT INTO items(kind,title,due,status,first_ts,updated_ts,source,chat,quote) VALUES('todo','前往会场','','open',?,?,'QQ','g','立刻到会场')", (now - 3600, now))
        x.execute("INSERT INTO items(kind,title,due,status,first_ts,updated_ts,source,chat,quote) VALUES('todo','交实验报告','','open',?,?,'QQ','g','尽量周末前交')", (now - 7 * 3600, now))
    app_mod.expire_items(now)
    with app_mod.db() as x:
        st = {r["title"]: r["status"] for r in x.execute("SELECT title,status FROM items")}
    assert st == {"前往大活3405开会": "expired", "马上交签到表": "expired", "前往会场": "open", "交实验报告": "open"}
    _fresh()


def test_valid_headline_rules():
    opens = [{"title": "今晚按时打卡学习", "due": "今晚"}]
    assert app_mod.valid_headline("learning群通知紧急开会，需立刻前往大活3405", opens, ["前往大活3405开会"]).startswith("今晚按时打卡学习")
    assert app_mod.valid_headline("今晚记得打卡学习", opens, []) == "今晚记得打卡学习"   # 说的是未完成的事：保留模型的说法
    assert app_mod.valid_headline("班长说周末聚餐", opens, []).startswith("今晚按时打卡学习")  # 只在群要点里的事不当头条
    assert app_mod.valid_headline("learning群通知紧急开会", [], ["前往大活3405开会"]) == "群里没什么要你管的"


def test_pending_hard_cap_even_when_model_down():
    """模型整体故障（别的群也没成功过）：照常重试，pending 里写明失败和重试时间；但任何消息最多挂 10 分钟，到点规则兜底后跳过。"""
    import asyncio
    _fresh(); _auto_reset(); app_mod.LLM_LAST_OK[0] = 0
    s = dict(app_mod.settings(), auto_on=True)

    async def down(msgs, as_json=False):
        raise app_mod.HTTPException(502, "大模型服务商出故障（503）", headers={"x-llm-code": "503"})
    old = app_mod.llm; app_mod.llm = down
    try:
        _ingest("全挂群", "今晚 7 点班会，地点 B201")
        rows = app_mod.pending_info(s)[0]
        k = ("微信", "全挂群")
        for _ in range(4):
            asyncio.run(app_mod.auto_run_chat(k, rows, s, time.time()))
        assert _pend(s) == 1 and app_mod.pending_info(s)[1]["failing"] == 1      # 没证据是这几条的问题：不跳过
        _arrive("全挂群", -601)
        asyncio.run(app_mod.auto_run_chat(k, rows, s, time.time()))
        assert _pend(s) == 0
        with app_mod.db() as x:
            assert [r["title"] for r in x.execute("SELECT title FROM items WHERE chat='全挂群'")] == ["今晚班会：按时参加"]
    finally:
        app_mod.llm = old; _auto_reset(); _fresh()


def test_digest_markdown_export():
    r = c.get("/api/digest.md", headers=AUTH)
    assert r.status_code == 200 and r.text.startswith("# 群报") and "## 待办" in r.text
    assert c.get("/api/digest.md").status_code == 401


def test_db_wal_and_busy_timeout():
    c = app_mod.db() if 'app_mod' in globals() else None
    if c is None:
        import digest.app as app_mod2
        c = app_mod2.db()
    assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] >= 30000


def test_prune_db():
    with app_mod.db() as c:
        c.execute("INSERT INTO digests(ts,hours,body) VALUES(?,?,?)", (1000, 24, "{}"))
        c.execute("INSERT INTO sessions(h,ts,exp,pw,ua) VALUES('old',1,2,'','')")
        c.execute("INSERT OR REPLACE INTO reminded(k,ts) VALUES('x',1000)")
    app_mod.prune_db()
    with app_mod.db() as c:
        assert c.execute("SELECT COUNT(*) FROM digests WHERE ts=1000").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM sessions WHERE h='old'").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM reminded WHERE k='x'").fetchone()[0] == 0


def test_onebot_token(monkeypatch):
    from fastapi.testclient import TestClient
    A = app_mod
    monkeypatch.setattr(A, "ONEBOT_TOKEN", "t0k")
    c = TestClient(A.app)
    assert c.post("/onebot", json={}).status_code == 401
    assert c.post("/onebot?token=t0k", json={}).status_code == 200
    assert c.post("/onebot", json={}, headers={"Authorization": "Bearer t0k"}).status_code == 200


def test_login_non_ascii_password_no_500():
    from fastapi.testclient import TestClient
    import digest.app as m
    c = TestClient(m.app)
    r = c.post("/api/login", json={"user": "me", "password": "密码é"})
    assert r.status_code in (401, 403)


def test_ingest_rejects_huge_body():
    from fastapi.testclient import TestClient
    import digest.app as m
    if not m.INGEST_TOKEN:
        return
    c = TestClient(m.app)
    r = c.post("/ingest", headers={"X-Token": m.INGEST_TOKEN}, content="x" * 300000)
    assert r.status_code == 413


def test_ics_fold():
    from digest.app import build_ics
    t = build_ics("很长的标题" * 30, "2026-10-10 18:00")
    for l in t.split("\r\n"):
        assert len(l.encode()) <= 75
    assert t.endswith("END:VCALENDAR\r\n")


def test_prune_old_closed_items():
    import time
    from digest import app as m
    now = int(time.time())
    with m.db() as c:
        c.execute("INSERT INTO items(source,chat,kind,title,status,first_ts,updated_ts,pinned) VALUES('qq','g','todo','旧完成','done',1,?,0)", (now - 100 * 86400,))
        c.execute("INSERT INTO items(source,chat,kind,title,status,first_ts,updated_ts,pinned) VALUES('qq','g','todo','旧置顶','done',1,?,1)", (now - 100 * 86400,))
    m.prune_db(now)
    with m.db() as c:
        ts = [r["title"] for r in c.execute("SELECT title FROM items")]
    assert "旧完成" not in ts and "旧置顶" in ts


def test_security_headers():
    from fastapi.testclient import TestClient
    import digest.app as a
    r = TestClient(a.app).get("/login")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"


def test_healthz_checks_db(monkeypatch):
    from fastapi.testclient import TestClient
    import app as A
    c = TestClient(A.app)
    assert c.get("/healthz").status_code == 200
    def boom(*a, **k): raise RuntimeError("x")
    monkeypatch.setattr(A, "db", boom)
    assert c.get("/healthz").status_code == 503


def test_export_has_no_secrets():
    app_mod.kv_set("vapid_priv", "SECRETPRIV")
    t = c.get("/api/export", headers=AUTH).text
    assert "SECRETPRIV" not in t


def test_design_tokens_0340():
    """0.34.0 布局重构：样式只从设计变量取值；引用的变量必须都有定义；按钮统一用 .btn。"""
    import re as _re
    h = open(os.path.join(os.path.dirname(__file__), "..", "digest", "index.html"), encoding="utf-8").read()
    css = h[h.index("<style>"):h.index("</style>")]
    for k in ("--s1:", "--s7:", "--t-xs:", "--t-2xl:", "--r-lg:", "--hit:", "--danger:"):
        assert k in css, k
    used = set(_re.findall(r"var\((--[\w-]+)", h))
    assert not [v for v in used if v + ":" not in h], "引用了未定义的 CSS 变量"
    assert len(set(_re.findall(r"font-size:([\d.]+px)", css))) <= 10           # 字号走 --t-*，裸像素只留少数特例
    for old in (".more-btn{display:block", ".acts2 button", ".brief .bf button", ".snzmenu button", ".suggest button", ".srcs button", ".sfchip button"):
        assert old not in css, old                                                 # 旧的各自为政的按钮样式不再回来
    assert 'class="btn line sm more-btn"' in h and 'class="btn line lg go"' in h
    assert '[hidden]{display:none!important}' in css
