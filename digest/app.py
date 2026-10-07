"""群报：收集 QQ（NapCat / OneBot 11）和微信（通知转发）群消息，用大模型挑出重要的事和待办。"""
import asyncio, hashlib, json, os, re, secrets, sqlite3, time
from datetime import datetime, timedelta
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

HERE = os.path.dirname(__file__)
VERSION = "0.7.0"  # 和仓库根目录 VERSION 保持一致
TZ = ZoneInfo("Asia/Shanghai")
DB = os.getenv("DB_PATH", "/data/qunbao.db")
LLM_BASE = os.getenv("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
LLM_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
NAPCAT_HTTP = os.getenv("NAPCAT_HTTP", "http://napcat:3000").rstrip("/")
NAPCAT_TOKEN = os.getenv("NAPCAT_TOKEN", "")
WEB_USER = os.getenv("WEB_USER", "me")
WEB_PASS = os.getenv("WEB_PASS", "")
INGEST_TOKEN = os.getenv("INGEST_TOKEN", "")
MAX_CHARS = int(os.getenv("MAX_CHARS", "60000"))
KEEP_DAYS = int(os.getenv("KEEP_DAYS", "30"))
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "30"))  # 网页登录保持天数
COOKIE = "qb_session"

# 网页「设置」里可以改的项；.env 里的值只作为第一次启动时的默认值
DEFAULTS = {
    "profile": os.getenv("MY_PROFILE", ""),
    "vip": [],                 # 重要的人（昵称/群名片），他们说话一律值得看
    "keywords": ["截止", "ddl", "提交", "开会", "考试", "缴费", "通知", "报名", "@全体成员"],
    "muted": [],               # 屏蔽的群：不进总结、不推送
    "digest_hour": int(os.getenv("DIGEST_HOUR", "21")),
    "bark_url": os.getenv("BARK_URL", ""),   # 例：https://api.day.app/你的key
    "site_url": os.getenv("SITE_URL", ""),   # 推送点开后跳转的群报地址
    "push_at": True,           # @我 / 重要的人 / 关键词 实时推送
    "push_digest": True,       # 每日总结推送
    "remind_hours": 3,         # 待办截止前几小时推送提醒；0 = 关闭
    "quiet_start": -1,         # 免打扰开始（小时 0-23）；-1 = 关闭
    "quiet_end": 7,            # 免打扰结束（小时）
}

app = FastAPI(docs_url=None, redoc_url=None)
_group_names: dict[int, str] = {}
_last_push: dict[str, float] = {}


# ---------------- 存储 ----------------
def db():
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


with db() as c:
    c.executescript("""
    CREATE TABLE IF NOT EXISTS msgs(id INTEGER PRIMARY KEY, ts INTEGER, source TEXT, chat TEXT,
        sender TEXT, text TEXT, at_me INTEGER DEFAULT 0);
    CREATE INDEX IF NOT EXISTS i_ts ON msgs(ts);
    CREATE INDEX IF NOT EXISTS i_chat ON msgs(chat, ts);
    CREATE TABLE IF NOT EXISTS digests(id INTEGER PRIMARY KEY, ts INTEGER, hours INTEGER, body TEXT);
    CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS todo_done(k TEXT PRIMARY KEY, ts INTEGER);
    CREATE TABLE IF NOT EXISTS reminded(k TEXT PRIMARY KEY, ts INTEGER);
    CREATE TABLE IF NOT EXISTS sessions(h TEXT PRIMARY KEY, ts INTEGER, exp INTEGER, pw TEXT, ua TEXT);
    """)


def settings() -> dict:
    with db() as c:
        r = c.execute("SELECT v FROM kv WHERE k='settings'").fetchone()
    s = dict(DEFAULTS)
    if r:
        s.update(json.loads(r["v"]))
    return s


def save_settings(new: dict):
    s = settings()
    for k, v in new.items():
        if k in DEFAULTS:
            s[k] = v
    with db() as c:
        c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('settings',?)", (json.dumps(s, ensure_ascii=False),))
    return s


# ---------------- 登录（网页登录页 + Cookie 会话；也兼容 Basic Auth 给脚本用）----------------
_fails: dict[str, list] = {}


def _h(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


def _pw_tag() -> str:  # 改了 WEB_PASS 之后，旧会话全部失效
    return _h("pw:" + WEB_USER + ":" + WEB_PASS)[:16]


def _basic_ok(req: Request) -> bool:
    a = req.headers.get("authorization", "")
    if not a.lower().startswith("basic "):
        return False
    try:
        import base64
        u, _, p = base64.b64decode(a[6:]).decode().partition(":")
    except Exception:
        return False
    return secrets.compare_digest(u, WEB_USER) and secrets.compare_digest(p, WEB_PASS)


def _session_ok(req: Request, resp: Response | None = None) -> bool:
    tok = req.cookies.get(COOKIE, "")
    if not tok:
        return False
    now = int(time.time())
    with db() as c:
        r = c.execute("SELECT exp, pw FROM sessions WHERE h=?", (_h(tok),)).fetchone()
        if not r or r["exp"] < now or r["pw"] != _pw_tag():
            return False
        # 滑动续期：一直在用就一直保持登录，超过一天没续才写一次库
        if r["exp"] - now < SESSION_DAYS * 86400 - 86400 and resp is not None:
            c.execute("UPDATE sessions SET exp=? WHERE h=?", (now + SESSION_DAYS * 86400, _h(tok)))
            _set_cookie(resp, tok, req)
    return True


def _set_cookie(resp: Response, tok: str, req: Request):
    secure = req.url.scheme == "https" or req.headers.get("x-forwarded-proto") == "https"
    resp.set_cookie(COOKIE, tok, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=secure, path="/")


def auth(req: Request, resp: Response):
    if not WEB_PASS:
        raise HTTPException(500, "请先在 .env 里设置 WEB_PASS")
    if _session_ok(req, resp) or _basic_ok(req):
        return
    raise HTTPException(401, "未登录")


def _client_ip(req: Request) -> str:
    return (req.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (req.client.host if req.client else "?"))


# ---------------- 推送（Bark，iPhone 上收通知）----------------
_held: list = []


def in_quiet(s, now=None) -> bool:
    a, b = int(s.get("quiet_start", -1)), int(s.get("quiet_end", 7))
    if a < 0 or a == b:
        return False
    h = (now or datetime.now(TZ)).hour
    return (a <= h < b) if a < b else (h >= a or h < b)


async def flush_held():
    """免打扰结束后，把攒着的推送合并成一条发出。"""
    if not _held or in_quiet(settings()):
        return 0
    items = _held[:]
    _held.clear()
    body = "\n".join(f"· {t}：{b}" for t, b in items)[:300]
    await push(f"免打扰期间 {len(items)} 条消息", body, force=True, test=True)
    return len(items)


async def push(title: str, body: str, key: str = "", force=False, test=False):
    s = settings()
    if not test and s.get("bark_url") and in_quiet(s):
        if len(_held) < 50:
            _held.append((title, body))
        return False
    url = (s.get("bark_url") or "").rstrip("/")
    if not url:
        return False
    if key and not force and time.time() - _last_push.get(key, 0) < 60:  # 同一个群一分钟最多推一次
        return False
    _last_push[key] = time.time()
    payload = {"title": title[:60], "body": body[:300], "group": "群报"}
    if s.get("site_url"):
        payload["url"] = s["site_url"]
        payload["icon"] = s["site_url"].rstrip("/") + "/icon.png"
    try:
        async with httpx.AsyncClient(timeout=8) as cl:
            r = await cl.post(url, json=payload)
            return r.status_code < 300
    except Exception as ex:
        print("推送失败:", ex)
        return False


def hit_reason(chat, sender, text, at_me, s):
    if chat in s["muted"]:
        return None
    if at_me:
        return "@了你"
    if sender and any(v and v in sender for v in s["vip"]):
        return "重要的人"
    for k in s["keywords"]:
        if k and k.lower() in text.lower():
            return f"关键词「{k}」"
    return None


async def save(source, chat, sender, text, ts=None, at_me=False):
    text = (text or "").strip()
    if not text:
        return
    with db() as c:
        c.execute("INSERT INTO msgs(ts,source,chat,sender,text,at_me) VALUES(?,?,?,?,?,?)",
                  (int(ts or time.time()), source, chat, sender, text[:4000], int(at_me)))
    s = settings()
    why = hit_reason(chat, sender, text, at_me, s)
    if why and s.get("push_at"):
        asyncio.create_task(push(f"{chat} · {why}", f"{sender}：{text}", key=chat))


# ---------------- 收消息 ----------------
CQ = re.compile(r"\[CQ:(\w+)([^\]]*)\]")
CQ_NAME = {"image": "[图片]", "face": "", "record": "[语音]", "video": "[视频]", "file": "[文件]",
           "reply": "", "forward": "[聊天记录]", "json": "[卡片]", "xml": "[卡片]", "mface": "[表情]"}


def clean_cq(raw: str, self_id: str) -> str:
    def rep(m):
        kind, args = m.group(1), m.group(2)
        if kind == "at":
            q = re.search(r"qq=(\w+)", args)
            if q and q.group(1) == "all":
                return "@全体成员"
            if q and q.group(1) == self_id:
                return "@我"
            n = re.search(r"name=([^,\]]+)", args)
            return f"@{n.group(1)}" if n else "@某人"
        return CQ_NAME.get(kind, "")
    return CQ.sub(rep, raw).replace("&#91;", "[").replace("&#93;", "]").replace("&#44;", ",").replace("&amp;", "&").strip()


async def group_name(gid: int) -> str:
    if gid in _group_names:
        return _group_names[gid]
    name = f"群{gid}"
    try:
        h = {"Authorization": f"Bearer {NAPCAT_TOKEN}"} if NAPCAT_TOKEN else {}
        async with httpx.AsyncClient(timeout=5) as cl:
            r = await cl.post(f"{NAPCAT_HTTP}/get_group_info", json={"group_id": gid}, headers=h)
            name = r.json().get("data", {}).get("group_name") or name
    except Exception:
        pass
    _group_names[gid] = name
    return name


@app.post("/onebot")
async def onebot(req: Request):
    e = await req.json()
    if e.get("post_type") == "meta_event":
        with db() as c:
            c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('heartbeat',?)", (str(int(time.time())),))
        return {}
    if e.get("post_type") != "message":
        return {}
    raw = e.get("raw_message", "") or ""
    self_id = str(e.get("self_id", ""))
    at_me = f"qq={self_id}" in raw or "qq=all" in raw
    s = e.get("sender", {}) or {}
    sender = s.get("card") or s.get("nickname") or str(e.get("user_id"))
    if e.get("message_type") == "group":
        chat = await group_name(int(e["group_id"]))
    else:
        chat, at_me = f"私聊·{sender}", True
    await save("QQ", chat, sender, clean_cq(raw, self_id), e.get("time"), at_me)
    return {}


WX_COUNT = re.compile(r"^\[\d+条\]\s*")
WX_SKIP = ("你收到了一条消息", "收到一条新消息", "正在运行", "条新消息")


def _pick(d, *keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def parse_wx(d: dict):
    """把各种通知转发 App 的格式统一成 (chat, sender, text, at_me)。
    支持：{chat,sender,text} 原生格式；{title,content|text|msg} 通知格式；只有一个 content/msg 字段时第一行当标题。"""
    if d.get("chat") and d.get("text"):
        t = str(d["text"])
        return str(d["chat"]), str(d.get("sender") or ""), t, bool(d.get("at_me")) or "@我" in t or "[有人@我]" in t
    title = _pick(d, "title", "android.title", "name")
    body = _pick(d, "text", "content", "msg", "message", "body", "android.text")
    if not title and "\n" in body:
        title, body = body.split("\n", 1)
    title, body = title.strip(), body.strip()
    if not body or title in ("微信", "WeChat") or any(k in body for k in WX_SKIP):
        return None
    body = WX_COUNT.sub("", body)
    at_me = "[有人@我]" in body or "@所有人" in body
    body = body.replace("[有人@我]", "").strip()
    m = re.match(r"^([^:：\n]{1,32})[:：]\s?(.+)$", body, re.S)
    if m:  # 群消息通知：标题是群名，内容是「发送人: 内容」
        return title or "未知群", m.group(1).strip(), m.group(2).strip(), at_me
    return f"私聊·{title or '未知'}", title, body, True  # 私聊通知：标题是好友名


def mark_seen(key):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (key, str(int(time.time()))))


@app.post("/ingest")
async def ingest(req: Request):
    """通用入口：微信通知转发等。口令放 Header X-Token，或网址 ?token=。JSON 或表单都行。"""
    tok = req.headers.get("X-Token") or req.query_params.get("token") or ""
    if not INGEST_TOKEN or not secrets.compare_digest(tok, INGEST_TOKEN):
        raise HTTPException(401, "口令不对")
    ct = req.headers.get("content-type", "")
    raw = (await req.body()).decode("utf-8", "ignore")
    if "urlencoded" in ct:
        d = {k: v[0] for k, v in parse_qs(raw).items()}
    else:
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            d = {"content": raw}
    if not isinstance(d, dict):
        raise HTTPException(400, "需要 JSON 对象")
    source = str(d.get("source") or "微信")
    mark_seen("seen_wx" if source == "微信" else f"seen_{source}")
    p = parse_wx(d)
    if not p:
        return {"ok": True, "skipped": True}
    chat, sender, text, at_me = p
    with db() as c:  # 通知转发常会重复推同一条，2 分钟内同内容去重
        dup = c.execute("SELECT 1 FROM msgs WHERE source=? AND chat=? AND sender=? AND text=? AND ts>=?",
                        (source, chat, sender, text[:4000], int(time.time()) - 120)).fetchone()
    if dup:
        return {"ok": True, "dup": True}
    await save(source, chat, sender, text, d.get("ts"), at_me)
    return {"ok": True, "chat": chat, "sender": sender}


@app.get("/api/ingest", dependencies=[Depends(auth)])
def ingest_info():
    return {"token": INGEST_TOKEN, "ready": bool(INGEST_TOKEN)}


# ---------------- 大模型 ----------------
async def llm(messages, as_json=False):
    if not LLM_KEY:
        raise HTTPException(500, "请先在 .env 里设置 LLM_API_KEY")
    body = {"model": LLM_MODEL, "messages": messages, "temperature": 0.2}
    if as_json:
        body["response_format"] = {"type": "json_object"}
    try:
        async with httpx.AsyncClient(timeout=180) as cl:
            r = await cl.post(f"{LLM_BASE}/chat/completions", json=body,
                              headers={"Authorization": f"Bearer {LLM_KEY}"})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
    except httpx.HTTPStatusError as ex:
        raise HTTPException(502, f"大模型接口报错 {ex.response.status_code}：{ex.response.text[:200]}")
    except httpx.HTTPError as ex:
        raise HTTPException(502, f"连不上大模型接口：{ex}")


def transcript(hours: int):
    s = settings()
    since = int(time.time()) - hours * 3600
    with db() as c:
        rows = c.execute("SELECT * FROM msgs WHERE ts>=? ORDER BY ts", (since,)).fetchall()
    rows = [r for r in rows if r["chat"] not in s["muted"]]
    lines = [f"[{datetime.fromtimestamp(r['ts'], TZ):%m-%d %H:%M}][{r['source']}·{r['chat']}]"
             f"{' (@我)' if r['at_me'] else ''} {r['sender']}: {r['text']}" for r in rows]
    text = "\n".join(lines)
    if len(text) > MAX_CHARS:  # 太长时保留最新的部分
        text = text[-MAX_CHARS:]
    return rows, text


def about_me(s):
    parts = [f"关于用户：{s['profile'] or '（未提供）'}"]
    if s["vip"]:
        parts.append("对用户重要的人：" + "、".join(s["vip"]))
    if s["keywords"]:
        parts.append("用户关心的关键词：" + "、".join(s["keywords"]))
    return "\n".join(parts)


def digest_prompt(s):
    now = datetime.now(TZ)
    return f"""你是用户的群消息秘书。用户不看群，只看你的整理。现在是 {now:%Y-%m-%d %H:%M} 星期{"一二三四五六日"[now.weekday()]}。
{about_me(s)}
从聊天记录里挑出真正和用户相关的东西。闲聊、表情、广告、和用户无关的讨论一律忽略，宁缺毋滥。
重要的人说的话、@用户或@全体成员的内容、命中关键词的内容优先考虑。
只输出 JSON：
{{"headline":"一句话概括最要紧的事，25字内（没有就写：群里没什么要你管的）",
"todos":[{{"title":"要做的事，动词开头，15字内","detail":"补充信息，40字内","due":"截止时间，尽量写成具体日期时间，如 10月10日 23:59；没有就空","chat":"来源群","sender":"谁说的","quote":"原话摘录，50字内","urgency":"high|mid|low"}}],
"notices":[{{"title":"值得知道的通知或变化，15字内","detail":"40字内","chat":"来源群"}}],
"groups":[{{"chat":"群名","gist":"这个群主要在聊什么，20字内"}}]}}
todos 按紧急程度排序；urgency=high 只给 48 小时内截止或老师/领导点名要求的事。"""


async def make_digest(hours=24):
    s = settings()
    rows, text = transcript(hours)
    if not rows:
        body = {"headline": "这段时间群里很安静", "todos": [], "notices": [], "groups": []}
    else:
        out = await llm([{"role": "system", "content": digest_prompt(s)},
                         {"role": "user", "content": f"最近 {hours} 小时的消息：\n{text}"}], as_json=True)
        m = re.search(r"\{.*\}", out, re.S)
        try:
            body = json.loads(m.group(0) if m else out)
        except json.JSONDecodeError:
            raise HTTPException(502, "大模型返回的不是合法 JSON，再试一次")
    body["count"] = len(rows)
    body["chats"] = len({(r["source"], r["chat"]) for r in rows})
    with db() as c:
        cur = c.execute("INSERT INTO digests(ts,hours,body) VALUES(?,?,?)",
                        (int(time.time()), hours, json.dumps(body, ensure_ascii=False)))
        body["id"] = cur.lastrowid
    return body


def parse_due(text: str, now: datetime) -> datetime | None:
    """把「10月10日 23:59」「明天 18:00」「2026-10-10」之类的自由文本解析成时间；解析不了返回 None。"""
    t = (text or "").strip()
    if not t:
        return None
    day = None
    m = re.search(r"(?:(\d{4})[年/-])?(\d{1,2})[月/-](\d{1,2})[日号]?", t)
    if m:
        y = int(m.group(1) or now.year)
        try:
            day = datetime(y, int(m.group(2)), int(m.group(3)), tzinfo=now.tzinfo)
        except ValueError:
            return None
        if not m.group(1) and day.date() < now.date() - timedelta(days=30):
            day = day.replace(year=y + 1)
    else:
        for w, n in (("大后天", 3), ("后天", 2), ("明天", 1), ("明早", 1), ("今天", 0), ("今晚", 0)):
            if w in t:
                day = (now + timedelta(days=n)).replace(hour=0, minute=0, second=0, microsecond=0)
                break
    if day is None:
        return None
    hh, mm = 23, 59
    m = re.search(r"(\d{1,2})[:：点时](\d{1,2})?", t)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2) or 0)
        if re.search(r"下午|晚|PM|pm", t) and hh < 12:
            hh += 12
        if hh > 23 or mm > 59:
            return None
    return day.replace(hour=hh, minute=mm)


async def check_reminders():
    s = settings()
    n = int(s.get("remind_hours") or 0)
    if n <= 0 or not s.get("bark_url"):
        return 0
    with db() as c:
        d = c.execute("SELECT * FROM digests ORDER BY id DESC LIMIT 1").fetchone()
        done = {r["k"] for r in c.execute("SELECT k FROM todo_done")}
        seen = {r["k"] for r in c.execute("SELECT k FROM reminded")}
    if not d:
        return 0
    now = datetime.now(TZ)
    sent = 0
    for t in json.loads(d["body"]).get("todos", []):
        k = (t.get("title") or "") + "|" + (t.get("chat") or "")
        due = parse_due(t.get("due", ""), now)
        if not due or k in done or k in seen or not (timedelta(0) <= due - now <= timedelta(hours=n)):
            continue
        left = int((due - now).total_seconds() // 60)
        when = f"{left // 60} 小时 {left % 60} 分钟" if left >= 60 else f"{left} 分钟"
        await push("快截止了：" + t.get("title", ""), f"还剩 {when}（{t.get('due')}）· {t.get('chat', '')}", force=True)
        with db() as c:
            c.execute("INSERT OR REPLACE INTO reminded(k,ts) VALUES(?,?)", (k, int(time.time())))
        sent += 1
    return sent


@app.on_event("startup")
async def scheduler():
    async def loop():
        last = None
        while True:
            now = datetime.now(TZ)
            s = settings()
            if now.hour == int(s["digest_hour"]) and last != now.date():
                last = now.date()
                try:
                    d = await make_digest(24)
                    if s.get("push_digest"):
                        n = len(d.get("todos", []))
                        await push("今日群报" + (f" · {n} 件待办" if n else ""), d.get("headline", ""), force=True)
                except Exception as ex:
                    print("自动总结失败:", ex)
            try:
                await flush_held()
                await check_reminders()
            except Exception as ex:
                print("截止提醒失败:", ex)
            if now.hour == 4 and now.minute == 0:  # 每天凌晨清理过期消息
                with db() as c:
                    c.execute("DELETE FROM msgs WHERE ts<?", (int(time.time()) - KEEP_DAYS * 86400,))
            await asyncio.sleep(60)
    asyncio.create_task(loop())


# ---------------- 网页接口 ----------------
def digest_row(d):
    body = json.loads(d["body"])
    body["id"] = d["id"]
    return body


@app.get("/api/state", dependencies=[Depends(auth)])
def state(id: int | None = None):
    now = int(time.time())
    with db() as c:
        d = (c.execute("SELECT * FROM digests WHERE id=?", (id,)).fetchone() if id else
             c.execute("SELECT * FROM digests ORDER BY id DESC LIMIT 1").fetchone())
        span = (d["hours"] if d else 24) * 3600
        latest_id = c.execute("SELECT MAX(id) i FROM digests").fetchone()["i"]
        ref = now if (not d or d["id"] == latest_id) else d["ts"]
        ats = c.execute("SELECT * FROM msgs WHERE at_me=1 AND ts>=? AND ts<=? ORDER BY ts DESC LIMIT 30",
                        (ref - span, ref + 3600 * 24)).fetchall()
        per = c.execute("SELECT chat, COUNT(*) n FROM msgs WHERE ts>=? AND ts<=? GROUP BY chat",
                        (ref - span, ref)).fetchall()
        today = c.execute("SELECT COUNT(*) n FROM msgs WHERE ts>=?", (now - 86400,)).fetchone()["n"]
        last = c.execute("SELECT MAX(ts) t FROM msgs").fetchone()["t"]
        hb = c.execute("SELECT v FROM kv WHERE k='heartbeat'").fetchone()
        wx = c.execute("SELECT v FROM kv WHERE k='seen_wx'").fetchone()
        last_qq = c.execute("SELECT MAX(ts) t FROM msgs WHERE source='QQ'").fetchone()["t"]
        last_wx = c.execute("SELECT MAX(ts) t FROM msgs WHERE source='微信'").fetchone()["t"]
        srcs = {r["chat"]: r["source"] for r in c.execute("SELECT chat, source FROM msgs WHERE ts>=? GROUP BY chat",
                                                            (ref - span,)).fetchall()}
        done = [r["k"] for r in c.execute("SELECT k FROM todo_done").fetchall()]
        latest = latest_id
    hb_ts = int(hb["v"]) if hb else None
    wx_ts = max(int(wx["v"]) if wx else 0, last_wx or 0) or None
    qq_on = bool(hb_ts and now - hb_ts < 180) or bool(last_qq and now - last_qq < 1800)
    wx_on = bool(wx_ts and now - wx_ts < 6 * 3600)
    return {
        "digest": digest_row(d) if d else None,
        "digest_ts": d["ts"] if d else None,
        "hours": d["hours"] if d else 24,
        "is_latest": (not d) or d["id"] == latest,
        "per_chat": {r["chat"]: r["n"] for r in per},
        "chat_src": srcs,
        "at_me": [{"ts": r["ts"], "chat": r["chat"], "sender": r["sender"], "text": r["text"],
                   "source": r["source"]} for r in ats],
        "today": today,
        "done": done,
        "status": {"last_msg": last, "heartbeat": hb_ts, "online": qq_on or wx_on,
                   "qq": {"online": qq_on, "seen": hb_ts or last_qq, "last": last_qq},
                   "wx": {"online": wx_on, "seen": wx_ts, "last": last_wx, "ready": bool(INGEST_TOKEN)},
                   "llm": bool(LLM_KEY), "version": VERSION},
    }


@app.post("/api/digest", dependencies=[Depends(auth)])
async def digest_now(req: Request):
    hours = int((await req.json()).get("hours", 24))
    return await make_digest(max(1, min(hours, 168)))


@app.get("/api/digests", dependencies=[Depends(auth)])
def digests(limit: int = 30):
    with db() as c:
        rows = c.execute("SELECT * FROM digests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        b = json.loads(r["body"])
        out.append({"id": r["id"], "ts": r["ts"], "hours": r["hours"], "headline": b.get("headline", ""),
                    "todos": len(b.get("todos", [])), "count": b.get("count", 0)})
    return out


@app.post("/api/todo", dependencies=[Depends(auth)])
async def todo(req: Request):
    d = await req.json()
    with db() as c:
        if d.get("done"):
            c.execute("INSERT OR REPLACE INTO todo_done(k,ts) VALUES(?,?)", (d["key"], int(time.time())))
        else:
            c.execute("DELETE FROM todo_done WHERE k=?", (d["key"],))
    return {"ok": True}


@app.get("/api/chats", dependencies=[Depends(auth)])
def chats(hours: int = 168, source: str = ""):
    s = settings()
    with db() as c:
        rows = c.execute("""SELECT chat, source, COUNT(*) n, MAX(ts) last_ts, SUM(at_me) ats FROM msgs
                            WHERE ts>=? AND (?='' OR source=?) GROUP BY chat, source ORDER BY last_ts DESC""",
                         (int(time.time()) - hours * 3600, source, source)).fetchall()
        out = []
        for r in rows:
            m = c.execute("SELECT sender, text FROM msgs WHERE chat=? AND source=? ORDER BY id DESC LIMIT 1",
                          (r["chat"], r["source"])).fetchone()
            out.append({"chat": r["chat"], "source": r["source"], "n": r["n"], "last_ts": r["last_ts"],
                        "ats": r["ats"] or 0, "last": f"{m['sender']}：{m['text']}" if m else "",
                        "muted": r["chat"] in s["muted"]})
    return out


@app.get("/api/messages", dependencies=[Depends(auth)])
def messages(chat: str = "", q: str = "", before: int = 0, limit: int = 60, source: str = ""):
    sql, args = "SELECT * FROM msgs WHERE 1=1", []
    if chat:
        sql += " AND chat=?"; args.append(chat)
    if source:
        sql += " AND source=?"; args.append(source)
    if q:
        sql += " AND (text LIKE ? OR sender LIKE ?)"; args += [f"%{q}%", f"%{q}%"]
    if before:
        sql += " AND id<?"; args.append(before)
    sql += " ORDER BY id DESC LIMIT ?"; args.append(min(limit, 200))
    with db() as c:
        rows = c.execute(sql, args).fetchall()
    return [{"id": r["id"], "ts": r["ts"], "chat": r["chat"], "sender": r["sender"], "text": r["text"],
             "source": r["source"], "at_me": bool(r["at_me"])} for r in reversed(rows)]


@app.get("/api/settings", dependencies=[Depends(auth)])
def get_settings():
    return settings()


@app.post("/api/settings", dependencies=[Depends(auth)])
async def post_settings(req: Request):
    return save_settings(await req.json())


@app.post("/api/push/test", dependencies=[Depends(auth)])
async def push_test():
    if not settings().get("bark_url"):
        raise HTTPException(400, "先填 Bark 推送地址")
    ok = await push("群报", "推送通了。之后有人 @你 或说到关键词，会第一时间提醒你。", force=True, test=True)
    if not ok:
        raise HTTPException(502, "推送失败，检查 Bark 地址是否正确")
    return {"ok": True}


@app.post("/api/ask", dependencies=[Depends(auth)])
async def ask(req: Request):
    body = await req.json()
    q = body.get("q", "").strip()
    if not q:
        return {"a": ""}
    hist = [{"role": m["role"], "content": str(m["content"])[:2000]} for m in body.get("history", [])[-6:]
            if m.get("role") in ("user", "assistant")]
    s = settings()
    _, text = transcript(72)
    now = datetime.now(TZ)
    a = await llm([
        {"role": "system", "content": f"你是用户的群消息秘书。现在是 {now:%Y-%m-%d %H:%M}。{about_me(s)}\n"
                                      "只根据下面的聊天记录回答，简洁直接，说清楚来自哪个群、谁说的、什么时候；记录里没有就直说没有。"
                                      "可以用 **加粗** 标重点、用「- 」开头列条目，不要用标题和表格。"},
        {"role": "user", "content": f"最近 72 小时的消息：\n{text or '（没有消息）'}"},
        {"role": "assistant", "content": "好的，我看完了，请问。"}, *hist,
        {"role": "user", "content": q}])
    return {"a": a}


@app.get("/", response_class=HTMLResponse)
def index(req: Request, resp: Response):
    if not WEB_PASS:
        return HTMLResponse("请先在 .env 里设置 WEB_PASS", 500)
    if not (_session_ok(req, resp) or _basic_ok(req)):
        return RedirectResponse("/login", 303)
    with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
        return f.read()


@app.get("/login", response_class=HTMLResponse)
def login_page():
    with open(os.path.join(HERE, "login.html"), encoding="utf-8") as f:
        return f.read()


@app.post("/api/login")
async def login(req: Request):
    if not WEB_PASS:
        raise HTTPException(500, "请先在 .env 里设置 WEB_PASS")
    ip, now = _client_ip(req), time.time()
    fails = [t for t in _fails.get(ip, []) if now - t < 600]
    if len(fails) >= 8:  # 10 分钟内错 8 次就先锁住
        raise HTTPException(429, "错太多次了，10 分钟后再试")
    d = await req.json()
    u, p = str(d.get("user") or WEB_USER), str(d.get("password") or "")
    if not (secrets.compare_digest(u, WEB_USER) and secrets.compare_digest(p, WEB_PASS)):
        _fails[ip] = fails + [now]
        await asyncio.sleep(0.6)
        raise HTTPException(401, "用户名或密码不对")
    _fails.pop(ip, None)
    tok = secrets.token_urlsafe(32)
    with db() as c:
        c.execute("DELETE FROM sessions WHERE exp<?", (int(now),))
        c.execute("INSERT INTO sessions VALUES(?,?,?,?,?)", (_h(tok), int(now), int(now) + SESSION_DAYS * 86400,
                                                             _pw_tag(), req.headers.get("user-agent", "")[:200]))
    resp = JSONResponse({"ok": True, "days": SESSION_DAYS})
    _set_cookie(resp, tok, req)
    return resp


@app.post("/api/logout")
def logout(req: Request):
    tok = req.cookies.get(COOKIE, "")
    if tok:
        with db() as c:
            c.execute("DELETE FROM sessions WHERE h=?", (_h(tok),))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@app.get("/icon.png")
def icon():
    return FileResponse(os.path.join(HERE, "icon.png"), media_type="image/png")


@app.get("/manifest.json")
def manifest():
    return JSONResponse({"name": "群报", "short_name": "群报", "display": "standalone", "start_url": "/",
                         "background_color": "#F5F3EE", "theme_color": "#F5F3EE",
                         "icons": [{"src": "/icon.png", "sizes": "512x512", "type": "image/png"}]})


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": VERSION}
