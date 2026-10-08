"""群报：收集 QQ（NapCat / OneBot 11）和微信（通知转发）群消息，用大模型挑出重要的事和待办。"""
import asyncio, contextlib, hashlib, json, os, re, secrets, sqlite3, time
from difflib import SequenceMatcher
from datetime import datetime, timedelta
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

HERE = os.path.dirname(__file__)
VERSION = "0.30.2"
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
    "weekly_digest": True,
    "keep_days": KEEP_DAYS,    # 消息保留天数
    "profile": os.getenv("MY_PROFILE", ""),
    "my_names": [],            # 我的昵称/群名片：文本里出现 @昵称 就算 @我（微信通知常识别不出）
    "vip": [],                 # 重要的人（昵称/群名片），他们说话一律值得看
    "keywords": ["截止", "ddl", "提交", "开会", "考试", "缴费", "通知", "报名", "@全体成员"],
    "only_mode": False,        # 白名单模式：只总结 allowed 里的群
    "allowed": [],
    "muted": [],               # 屏蔽的群：不进总结、不推送
    "levels": {},              # 每个群的级别：important 重要 / atonly 只看@我；缺省 = 普通
    "digest_hour": int(os.getenv("DIGEST_HOUR", "21")),
    "digest_hour2": -1,        # 第二次整理时间（早报+晚报）；-1 = 关闭
    "bark_url": os.getenv("BARK_URL", ""),   # 例：https://api.day.app/你的key
    "site_url": os.getenv("SITE_URL", ""),   # 推送点开后跳转的群报地址
    "push_at": True,           # @我 / 重要的人 / 关键词 实时推送
    "push_digest": True,       # 每日总结推送
    "remind_hours": 3,         # 待办截止前几小时推送提醒；0 = 关闭
    "quiet_start": -1,         # 免打扰开始（小时 0-23）；-1 = 关闭
    "quiet_end": 7,            # 免打扰结束（小时）
    "auto_interval": 30,       # 有新消息时自动整理的间隔（分钟）；0 = 关闭
}
AUTO_CHOICES = (0, 15, 30, 60, 120)
AUTO_URGENT_GAP = 300          # 重要群 / @我 的新消息最少隔 5 分钟就可以提前整理

@contextlib.asynccontextmanager
async def lifespan(_app):
    task = await scheduler()
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(docs_url=None, redoc_url=None, lifespan=lifespan)
_group_names: dict[int, str] = {}
_group_miss: dict[int, float] = {}
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
    CREATE TABLE IF NOT EXISTS pins(k TEXT PRIMARY KEY, ts INTEGER);
    CREATE TABLE IF NOT EXISTS snooze(k TEXT PRIMARY KEY, title TEXT, until INTEGER);
    CREATE TABLE IF NOT EXISTS sessions(h TEXT PRIMARY KEY, ts INTEGER, exp INTEGER, pw TEXT, ua TEXT);
    CREATE TABLE IF NOT EXISTS chat_state(source TEXT, chat TEXT, last_msg_id INTEGER DEFAULT 0, summary TEXT DEFAULT '',
        updated_ts INTEGER DEFAULT 0, PRIMARY KEY(source, chat));
    CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, source TEXT, chat TEXT, kind TEXT, title TEXT,
        detail TEXT DEFAULT '', due TEXT DEFAULT '', sender TEXT DEFAULT '', quote TEXT DEFAULT '', urgency TEXT DEFAULT 'mid',
        status TEXT DEFAULT 'open', first_ts INTEGER, updated_ts INTEGER, msg_ids TEXT DEFAULT '',
        pinned INTEGER DEFAULT 0, reminded INTEGER DEFAULT 0, at_me INTEGER DEFAULT 0);
    CREATE INDEX IF NOT EXISTS i_items ON items(source, chat, status);
    """)
    for tb, col in [("msgs", "img"), ("todo_done", "title"), ("todo_done", "chat"), ("todo_done", "due"),
                    ("pins", "title"), ("pins", "chat"), ("pins", "due"), ("reminded", "title"), ("reminded", "chat"),
                    ("reminded", "due"), ("snooze", "chat"), ("snooze", "due")]:
        try:
            c.execute(f"ALTER TABLE {tb} ADD COLUMN {col} TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass


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
    try:
        s["auto_interval"] = int(s.get("auto_interval") or 0)
    except (TypeError, ValueError):
        s["auto_interval"] = DEFAULTS["auto_interval"]
    if s["auto_interval"] not in AUTO_CHOICES:
        s["auto_interval"] = DEFAULTS["auto_interval"]
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


def is_muted(chat, s) -> bool:
    if s.get("only_mode"):
        return chat not in (s.get("allowed") or [])
    return chat in s["muted"]


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
    if is_muted(chat, s):
        return None
    if at_me:
        return "@了你"
    if (s.get("levels") or {}).get(chat) == "atonly":
        return None
    if sender and any(v and v in sender for v in s["vip"]):
        return "重要的人"
    hits = [k for k in s["keywords"] if k and k.lower() in text.lower()]
    if hits:
        return "关键词" + "、".join(f"「{k}」" for k in hits[:3])
    return None


def mentions_me(text, s) -> bool:
    t = text.replace("＠", "@").replace("\u2005", " ").replace("@ ", "@")
    return any(n and ("@" + n) in t for n in (s.get("my_names") or []))


async def save(source, chat, sender, text, ts=None, at_me=False, imgs=""):
    text = (text or "").strip()
    if not text:
        return
    s = settings()
    at_me = bool(at_me) or mentions_me(text, s)
    with db() as c:
        c.execute("INSERT INTO msgs(ts,source,chat,sender,text,at_me,img) VALUES(?,?,?,?,?,?,?)",
                  (int(ts or time.time()), source, chat, sender, text[:4000], int(at_me), imgs[:2000]))
    why = hit_reason(chat, sender, text, at_me, s)
    if why and s.get("push_at"):
        asyncio.create_task(push(f"{chat} · {why}", f"{sender}：{text}", key=chat))


# ---------------- 收消息 ----------------
CQ = re.compile(r"\[CQ:(\w+)([^\]]*)\]")
CQ_NAME = {"image": "[图片]", "face": "", "record": "[语音]", "video": "[视频]", "file": "[文件]",
           "reply": "", "forward": "[聊天记录]", "json": "[卡片]", "xml": "[卡片]", "mface": "[表情]"}


def cq_images(raw: str) -> str:
    """提取 CQ 图片的 http(s) URL，空格分隔，最多 4 张。"""
    out = []
    for m in CQ.finditer(raw or ""):
        if m.group(1) == "image":
            u = re.search(r"url=(https?://[^,\]]+)", m.group(2))
            if u:
                out.append(u.group(1).replace("&amp;", "&"))
    return " ".join(out[:4])


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
    if time.time() - _group_miss.get(gid, 0) < 600:  # 查不到的 10 分钟后再试
        return name
    _group_miss[gid] = time.time()
    try:
        h = {"Authorization": f"Bearer {NAPCAT_TOKEN}"} if NAPCAT_TOKEN else {}
        async with httpx.AsyncClient(timeout=5) as cl:
            r = await cl.post(f"{NAPCAT_HTTP}/get_group_info", json={"group_id": gid}, headers=h)
            got = (r.json().get("data") or {}).get("group_name")
        if got:  # 只缓存查到的真名；查不到时下次再查，不要永远显示成「群123」
            _group_names[gid] = name = got
    except Exception:
        pass
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
    await save("QQ", chat, sender, clean_cq(raw, self_id), e.get("time"), at_me, cq_images(raw))
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
LLM_STATE = {"ok": True, "err": "", "ts": 0}


def kv_get(k, default=None):
    with db() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def kv_set(k, v):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, str(v)))


def kv_add(k, n=1):
    with db() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, str(int(float(r["v"]) if r else 0) + n)))
LLM_RETRY_WAIT = 2


async def llm(messages, as_json=False):
    if not LLM_KEY:
        raise HTTPException(500, "请先在 .env 里设置 LLM_API_KEY")
    body = {"model": LLM_MODEL, "messages": messages, "temperature": 0.2}
    if as_json:
        body["response_format"] = {"type": "json_object"}
    err = None
    for attempt in range(2):  # 失败自动重试 1 次
        try:
            async with httpx.AsyncClient(timeout=180) as cl:
                r = await cl.post(f"{LLM_BASE}/chat/completions", json=body,
                                  headers={"Authorization": f"Bearer {LLM_KEY}"})
                r.raise_for_status()
                out = r.json()["choices"][0]["message"]["content"]
                LLM_STATE.update(ok=True, err="", ts=int(time.time()))
                kv_add("llm_calls", 1)
                return out
        except httpx.HTTPStatusError as ex:
            err = f"大模型接口报错 {ex.response.status_code}：{ex.response.text[:200]}"
            if ex.response.status_code < 500 and ex.response.status_code != 429:
                break
        except httpx.HTTPError as ex:
            err = f"连不上大模型接口：{ex}"
        if attempt == 0:
            await asyncio.sleep(LLM_RETRY_WAIT)
    LLM_STATE.update(ok=False, err=err, ts=int(time.time()))
    raise HTTPException(502, err)


def transcript(hours: int):
    s = settings()
    since = int(time.time()) - hours * 3600
    with db() as c:
        rows = c.execute("SELECT * FROM msgs WHERE ts>=? ORDER BY ts", (since,)).fetchall()
    lv = s.get("levels") or {}
    rows = [r for r in rows if not is_muted(r["chat"], s)
            and (lv.get(r["chat"]) != "atonly" or r["at_me"]) and classify(r, s) != "drop"]
    lines = [f"[{datetime.fromtimestamp(r['ts'], TZ):%m-%d %H:%M}][{r['source']}·{r['chat']}]"
             f"{'【重要群】' if lv.get(r['chat']) == 'important' else ''}"
             f"{' (@我)' if r['at_me'] else ''} {r['sender']}: {r['text']}" for r in rows]
    text = "\n".join(lines)
    if len(text) > MAX_CHARS:  # 太长时保留最新的部分
        text = text[-MAX_CHARS:]
    return rows, text


def about_me(s):
    parts = [f"关于用户：{s['profile'] or '（未提供）'}"]
    if s.get("my_names"):
        parts.append("用户在群里的昵称：" + "、".join(s["my_names"]))
    if s["vip"]:
        parts.append("对用户重要的人：" + "、".join(s["vip"]))
    imp = [k for k, v in (s.get("levels") or {}).items() if v == "important"]
    if imp:
        parts.append("重要的群（这些群里的事权重更高）：" + "、".join(imp))
    if s["keywords"]:
        parts.append("用户关心的关键词：" + "、".join(s["keywords"]))
    return "\n".join(parts)


def weekly_title(d: dict) -> str:
    n = len([t for t in d.get("todos", []) if not (isinstance(t, dict) and t.get("done"))])
    return "本周群报" + (f" · {n} 件待办" if n else "")


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


# ---------------- 待办同一件事的判断（LLM 每次整理都会改写标题，不能只认 "标题|群"）----------------
_PUNCT = re.compile(r"[\s\W_]+", re.U)
_NUM_RE = re.compile(r"\d+|第[一二三四五六七八九十百]+|[一二三四五六七八九十]+[章节次课题号期周]")
_STOP = ("请", "记得", "需要", "务必", "按时", "尽快", "一下", "及时", "之前")
_SYN = (("提交", "交"), ("上交", "交"), ("缴纳", "交"), ("缴", "交"), ("参加", "去"), ("参与", "去"))


def todo_key(t: dict) -> str:
    return (t.get("title") or "") + "|" + (t.get("chat") or "")


def split_key(k: str):
    title, sep, chat = (k or "").rpartition("|")
    return (title, chat) if sep else (k or "", "")


def norm_title(x: str) -> str:
    x = _PUNCT.sub("", (x or "").lower())
    for a, b in _SYN:
        x = x.replace(a, b)
    for w in _STOP:
        x = x.replace(w, "")
    return x


def _norm_chat(x: str) -> str:
    return _PUNCT.sub("", (x or "").lower())


def _norm_due(x: str) -> str:
    x = (x or "").strip()
    if not x:
        return ""
    d = parse_due(x, datetime.now(TZ))
    return d.strftime("%Y-%m-%d %H:%M") if d else _PUNCT.sub("", x)


def _grams(x: str) -> set:
    return {x[i:i + 2] for i in range(len(x) - 1)} or ({x} if x else set())


def same_todo(a: dict, b: dict) -> bool:
    """同一个群里、归一化后标题足够像（或截止时间相同且有共同词）就算同一件事。"""
    ca, cb = _norm_chat(a.get("chat")), _norm_chat(b.get("chat"))
    if ca and cb and ca != cb and ca not in cb and cb not in ca:
        return False
    ta, tb = norm_title(a.get("title")), norm_title(b.get("title"))
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    na, nb = _NUM_RE.findall(ta), _NUM_RE.findall(tb)
    if na and nb and na != nb:  # 「交第一章作业」和「交第二章作业」不是一件事
        return False
    if min(len(ta), len(tb)) >= 3 and (ta in tb or tb in ta):
        return True
    r = SequenceMatcher(None, ta, tb).ratio()
    ga, gb = _grams(ta), _grams(tb)
    ov = len(ga & gb) / max(1, min(len(ga), len(gb)))
    if r >= 0.6 or ov >= 0.5:
        return True
    m = SequenceMatcher(None, ta, tb).find_longest_match(0, len(ta), 0, len(tb))
    if m.size >= 4 and m.size / min(len(ta), len(tb)) >= 0.5:  # 共同的长关键词，如「实验报告」
        return True
    da, db_ = _norm_due(a.get("due")), _norm_due(b.get("due"))
    return bool(da and da == db_ and (r >= 0.3 or ov >= 0.25))


def _records(c, table: str, since: int = 0) -> list:
    out = []
    for r in c.execute(f"SELECT * FROM {table}" + (" WHERE ts>=?" if since else ""), (since,) if since else ()):
        t0, ch0 = split_key(r["k"])
        out.append({"k": r["k"], "title": r["title"] or t0, "chat": r["chat"] or ch0, "due": r["due"] or "",
                    "ts": r["ts"] if "ts" in r.keys() else 0})
    return out


def find_match(t: dict, recs: list):
    k = todo_key(t)
    for r in recs:
        if r["k"] == k:
            return r
    for r in recs:
        if same_todo(t, r):
            return r
    return None


def done_records(since: int = 0) -> list:
    with db() as c:
        return _records(c, "todo_done", since)


def annotate_todos(body: dict) -> dict:
    """给每条待办打上 key / done / pinned（模糊匹配已完成和置顶）。"""
    with db() as c:
        done, pins = _records(c, "todo_done"), _records(c, "pins")
        ids = [t["id"] for t in (body.get("todos", []) or []) if isinstance(t, dict) and t.get("id")]
        live = {r["id"]: r for r in c.execute(f"SELECT id,status,pinned FROM items WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
    for t in body.get("todos", []) or []:
        if t.get("id"):  # 新版：以 items 表为准（用户勾完成后立刻生效，任何整理都不会复活）
            r = live.get(t["id"])
            t["key"] = f"item:{t['id']}"
            t["done"] = bool(r and r["status"] == "done")
            t["pinned"] = bool(r and r["pinned"])
            continue
        t["key"] = todo_key(t)
        t["done"] = bool(find_match(t, done))
        t["pinned"] = bool(find_match(t, pins))
    return body


@app.post("/api/todo/snooze", dependencies=[Depends(auth)])
async def todo_snooze(req: Request):
    d = await req.json()
    h = d.get("hours")
    if h == "tomorrow":
        t = datetime.now(TZ).replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=1)
        until = int(t.timestamp())
    else:
        until = int(time.time() + float(h or 1) * 3600)
    t0, ch0 = split_key(d["key"])
    with db() as c:
        c.execute("INSERT OR REPLACE INTO snooze(k,title,until,chat,due) VALUES(?,?,?,?,?)",
                  (d["key"], d.get("title") or t0, until, d.get("chat") or ch0, d.get("due") or ""))
    warn = "" if settings().get("bark_url") else "还没填 Bark 推送地址，到点发不出提醒"
    return {"ok": True, "until": until, "warn": warn}


# ================= 整理引擎（0.30.0）：按群增量 + 事项稳定 ID + 噪音预过滤 =================
# 每个群一份状态（chat_state），每件事一条记录（items）。整理时只处理有新消息的群，
# 只把「该群旧要点 + 未完成事项（带 id）+ 新消息」交给模型，模型用 update/new/close 回答。
# 用户勾完成 = items.status='done'，任何重新整理都不会复活。
CHUNK_MSGS = int(os.getenv("CHUNK_MSGS", "300"))       # 单群新消息太多时，每块最多这么多条
CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "12000"))   # 每块最多这么多字（约 token 上限的保守估计）
LLM_PARALLEL = 3
DEBOUNCE_QUIET = 120          # 新消息安静 2 分钟后再整理
DEBOUNCE_BURST = 50           # 或者累计 50 条
NOISE_WORDS = {"收到", "好的", "好", "好滴", "好哒", "嗯", "嗯嗯", "ok", "okk", "okay", "谢谢", "谢谢老师", "多谢", "感谢",
               "哈", "哈哈", "哈哈哈", "哈哈哈哈", "哈哈哈哈哈", "1", "11", "111", "6", "66", "666", "6666", "牛", "牛啊",
               "赞", "对", "对的", "是的", "可以", "行", "知道了", "明白", "了解", "晚安", "早", "早安", "在", "在吗", "嘿嘿",
               "嗯呢", "好嘞", "收到收到", "好的收到", "收到谢谢", "辛苦了", "辛苦", "确实", "真的", "笑死", "啊这", "草"}
PLACEHOLDER_RE = re.compile(r"^(\s*\[(图片|表情|动画表情|语音|视频|文件|卡片|聊天记录|红包|位置|名片)\]\s*)+$")
RECALL_RE = re.compile(r"撤回了一条消息|撤回一条消息|recalled a message")
KEY_RE = re.compile(r"\d{1,2}[:：点时]\d{0,2}|\d{1,2}月\d{1,2}|\d{1,2}[/-]\d{1,2}|周[一二三四五六日天]|星期|今天|明天|后天|今晚|明早|下周|"
                    r"[¥￥]\s?\d|\d+(\.\d+)?\s?(元|块)|https?://|通知|截止|ddl|务必|提交|上交|缴费|交费|报名|考试|开会|会议|签到|作业|报告|"
                    r"取消|改到|推迟|提前|地点|集合", re.I)
ITEM_FIELDS = ("detail", "due", "urgency", "quote")


def classify(r, s) -> str:
    """噪音预过滤：drop 不送模型（原文页照常显示）；key 重点（@我/重要群/时间金额链接/关键词/重要的人）；keep 普通。"""
    text = (r["text"] or "").strip()
    lv = (s.get("levels") or {}).get(r["chat"])
    if r["at_me"]:
        return "key"
    if lv == "atonly":
        return "drop"
    if not text or RECALL_RE.search(text) or PLACEHOLDER_RE.match(text):
        return "drop"
    if (KEY_RE.search(text) or any(k and k.lower() in text.lower() for k in s.get("keywords") or [])
            or (r["sender"] and any(v and v in r["sender"] for v in s.get("vip") or []))):
        return "key"
    core = re.sub(r"[\s\W_]+", "", text.lower())
    if core in NOISE_WORDS or len(core) <= 2 or re.fullmatch(r"(哈|呵|嘿|啊|哦|噢|嗯|6|1|\+)+", core or "x"):
        return "drop"
    return "key" if lv == "important" else "keep"


def _scan_rows(full=False):
    """自上次整理以来的新消息（按全局水位 + 每群 last_msg_id）。没有水位时只看最近 24 小时。"""
    wm = None if full else kv_get("scan_id")
    with db() as c:
        if wm is None:
            rows = c.execute("SELECT * FROM msgs WHERE ts>=? ORDER BY id", (int(time.time()) - 86400,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM msgs WHERE id>? ORDER BY id", (int(wm),)).fetchall()
        last = {} if full else {(r["source"], r["chat"]): r["last_msg_id"] for r in c.execute("SELECT * FROM chat_state")}
    return [r for r in rows if r["id"] > (last.get((r["source"], r["chat"])) or 0)]


def pending_info(s=None):
    """待整理的新消息：已排除屏蔽群和噪音。"""
    s = s or settings()
    rows = [r for r in _scan_rows() if not is_muted(r["chat"], s) and classify(r, s) != "drop"]
    return rows, {"msgs": len(rows), "chats": len({(r["source"], r["chat"]) for r in rows})}


def _line(r, cls) -> str:
    return (f"#{r['id']} [{datetime.fromtimestamp(r['ts'], TZ):%m-%d %H:%M}] {r['sender']}"
            f"{' (@我)' if r['at_me'] else ''}{' ★' if cls == 'key' else ''}: {(r['text'] or '')[:800]}")


def chunked(pairs):
    out, cur, n = [], [], 0
    for r, cls in pairs:
        ln = _line(r, cls)
        if cur and (len(cur) >= CHUNK_MSGS or n + len(ln) > CHUNK_CHARS):
            out.append(cur); cur, n = [], 0
        cur.append((r, cls, ln)); n += len(ln) + 1
    if cur:
        out.append(cur)
    return out


def chat_prompt(s, source, chat):
    now = datetime.now(TZ)
    imp = "，用户标为【重要群】" if (s.get("levels") or {}).get(chat) == "important" else ""
    return f"""【群更新】你是用户的群消息秘书，只负责维护「{chat}」（{source}{imp}）这一个群的状态。现在是 {now:%Y-%m-%d %H:%M} 星期{"一二三四五六日"[now.weekday()]}。
{about_me(s)}
输入：这个群的旧要点 summary、仍未完成的事项 open_items（每项有固定 id）、用户最近已完成的事 done_recent，以及之后的新消息（#数字 是消息编号，★ 是重点消息，(@我) 是 @用户的）。
只输出 JSON：
{{"summary":"这个群现在的要点，40字内（没变化就原样沿用）",
"update":[{{"id":已有事项的id,"detail":"40字内","due":"截止时间","urgency":"high|mid|low","quote":"原话50字内"}}],
"new":[{{"kind":"todo|notice","title":"动词开头，15字内","detail":"40字内","due":"尽量写成具体日期时间，如 10月10日 23:59；没有就空","sender":"谁说的","quote":"原话摘录，50字内","urgency":"high|mid|low","msg_ids":[相关消息编号]}}],
"close":[{{"id":已有事项的id,"why":"过期|取消|已解决"}}]}}
规则：
- 已有事项只能通过 id 更新（只写变化的字段）或关闭；不要把已有事项换个说法再放进 new，不要改它的标题。
- done_recent 里是用户已经完成的事，不要再新建；只有群里提出了明确不同的新要求才 new，并在 title 里写清区别。
- todo 是用户要动手做的事；notice 是值得知道的通知或变化。闲聊、广告、和用户无关的讨论一律忽略，宁缺毋滥。
- urgency=high 只给 48 小时内截止或老师/领导点名要求的事。没有变化就输出空数组。"""


def _jparse(out: str) -> dict:
    m = re.search(r"\{.*\}", out or "", re.S)
    try:
        d = json.loads(m.group(0) if m else out)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(502, "大模型返回的不是合法 JSON，再试一次")
    return d if isinstance(d, dict) else {}


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def item_dict(r) -> dict:
    d = dict(r)
    d["key"] = f"item:{d['id']}"
    d["done"] = d["status"] == "done"
    d["pinned"] = bool(d["pinned"])
    return d


def apply_changes(source, chat, d: dict, at_ids=frozenset(), now=None) -> bool:
    """把模型的 update/new/close 落到 items 表。身份只认 id；模型漏传 id 时用标题模糊匹配兜底去重。"""
    now = int(now or time.time())
    changed = False
    with db() as c:
        cur = {r["id"]: dict(r) for r in c.execute("SELECT * FROM items WHERE source=? AND chat=?", (source, chat))}
        for u in d.get("update") or []:
            it = cur.get(_int(u.get("id"))) if isinstance(u, dict) else None
            if not it or it["status"] != "open":
                continue
            f = {k: str(u[k])[:200] for k in ITEM_FIELDS if u.get(k) not in (None, "") and str(u[k]) != str(it[k])}
            if f.get("urgency") not in (None, "high", "mid", "low"):
                f.pop("urgency")
            if f:
                c.execute(f"UPDATE items SET {', '.join(k + '=?' for k in f)}, updated_ts=? WHERE id=?", (*f.values(), now, it["id"]))
                it.update(f); changed = True
        for x in d.get("close") or []:
            it = cur.get(_int(x.get("id") if isinstance(x, dict) else x))
            if it and it["status"] == "open" and not it["pinned"]:
                c.execute("UPDATE items SET status='expired', updated_ts=? WHERE id=?", (now, it["id"]))
                it["status"] = "expired"; changed = True
        recent = [it for it in cur.values() if it["status"] in ("open", "rebuild") or (it["updated_ts"] or 0) >= now - 7 * 86400]
        for n in d.get("new") or []:
            if not isinstance(n, dict):
                continue
            title = str(n.get("title") or "").strip()[:60]
            if not title:
                continue
            kind = "notice" if n.get("kind") == "notice" else "todo"
            cand = {"title": title, "chat": chat, "due": str(n.get("due") or "")}
            dup = next((it for it in recent if it["kind"] == kind and same_todo(cand, it)), None)
            if dup:  # 模型把已有事项又当新的报了：已完成/已关闭的不复活，未完成的顺手更新
                if dup["status"] == "rebuild":  # 完整重新整理：同一件事沿用原 id
                    c.execute("UPDATE items SET status='open' WHERE id=?", (dup["id"],))
                    dup["status"] = "open"; changed = True
                if dup["status"] == "open":
                    f = {k: str(n[k])[:200] for k in ITEM_FIELDS if n.get(k) and str(n[k]) != str(dup[k])}
                    if f.get("urgency") not in (None, "high", "mid", "low"):
                        f.pop("urgency")
                    if f:
                        c.execute(f"UPDATE items SET {', '.join(k + '=?' for k in f)}, updated_ts=? WHERE id=?", (*f.values(), now, dup["id"]))
                        dup.update(f); changed = True
                continue
            mids = [i for i in (_int(m) for m in (n.get("msg_ids") or [])) if i][:20]
            urg = n.get("urgency") if n.get("urgency") in ("high", "mid", "low") else "mid"
            vals = dict(source=source, chat=chat, kind=kind, title=title, detail=str(n.get("detail") or "")[:200],
                        due=cand["due"][:60], sender=str(n.get("sender") or "")[:40], quote=str(n.get("quote") or "")[:200],
                        urgency=urg, status="open", first_ts=now, updated_ts=now, msg_ids=" ".join(map(str, mids)),
                        at_me=int(bool(set(mids) & set(at_ids))), pinned=0, reminded=0)
            cr = c.execute(f"INSERT INTO items({','.join(vals)}) VALUES({','.join('?' * len(vals))})", tuple(vals.values()))
            recent.append({**vals, "id": cr.lastrowid}); changed = True
    return changed


async def update_chat(source, chat, rows, s, stats) -> bool:
    """处理一个群的新消息：噪音不送模型；分块逐块更新。返回这个群的事项或要点有没有变化。"""
    with db() as c:
        st = c.execute("SELECT * FROM chat_state WHERE source=? AND chat=?", (source, chat)).fetchone()
    old_summary = summary = st["summary"] if st else ""
    pairs = [(r, cls) for r in rows if (cls := classify(r, s)) != "drop"]
    changed = False
    for part in chunked(pairs):
        with db() as c:
            opens = [dict(r) for r in c.execute("SELECT id,kind,title,detail,due,urgency FROM items WHERE source=? AND chat=? AND status='open' ORDER BY id",
                                                (source, chat))]
            done = [r["title"] for r in c.execute("SELECT title FROM items WHERE source=? AND chat=? AND status='done' AND updated_ts>=? ORDER BY updated_ts DESC LIMIT 15",
                                                  (source, chat, int(time.time()) - 7 * 86400))]
        state = json.dumps({"summary": summary, "open_items": opens, "done_recent": done}, ensure_ascii=False)
        out = await llm([{"role": "system", "content": chat_prompt(s, source, chat)},
                         {"role": "user", "content": f"旧状态：{state}\n新消息（{len(part)} 条）：\n" + "\n".join(x[2] for x in part)}], as_json=True)
        stats["calls"] += 1
        stats["sent"] += len(part)
        d = _jparse(out)
        if isinstance(d.get("summary"), str) and d["summary"].strip():
            summary = d["summary"].strip()[:80]
        changed |= apply_changes(source, chat, d, {x[0]["id"] for x in part if x[0]["at_me"]})
    with db() as c:
        c.execute("INSERT OR REPLACE INTO chat_state(source,chat,last_msg_id,summary,updated_ts) VALUES(?,?,?,?,?)",
                  (source, chat, max(r["id"] for r in rows), summary,
                   int(time.time()) if (changed or summary != old_summary) else (st["updated_ts"] if st else int(time.time()))))
    return changed or summary != old_summary


def expire_items(now=None):
    """代码规则：截止已过 1 天的待办、3 天没更新的通知自动关闭（不花模型调用）。"""
    now = now or time.time()
    nd = datetime.fromtimestamp(now, TZ)
    n = 0
    with db() as c:
        for r in c.execute("SELECT id, due FROM items WHERE status='open' AND kind='todo' AND due!='' AND pinned=0").fetchall():
            d = parse_due(r["due"], nd)
            if d and (nd - d).total_seconds() > 86400:
                c.execute("UPDATE items SET status='expired', updated_ts=? WHERE id=?", (int(now), r["id"])); n += 1
        n += c.execute("UPDATE items SET status='expired', updated_ts=? WHERE status='open' AND kind='notice' AND pinned=0 AND updated_ts<?",
                       (int(now), int(now) - 3 * 86400)).rowcount
    return n


async def run_update(full=False):
    """增量整理：只处理有新消息的群。返回 (stats, changed)。"""
    s = settings()
    t0 = time.time()
    stats = {"new": 0, "sent": 0, "calls": 0, "chats": 0, "changed": 0, "secs": 0.0, "errors": 0}
    kv_set("checked_ts", int(t0))
    if full:
        with db() as c:  # 完整重建：保留已完成 / 置顶，其余从最近 24 小时的消息重新来
            c.execute("UPDATE items SET status='rebuild' WHERE status='open' AND pinned=0")  # 重读后再报出来的沿用原 id
            c.execute("DELETE FROM chat_state")
    rows = _scan_rows(full)
    changed = bool(expire_items()) or full
    if not rows:
        stats["secs"] = round(time.time() - t0, 2)
        return stats, changed
    by = {}
    for r in rows:
        by.setdefault((r["source"], r["chat"]), []).append(r)
    todo = {}
    for k, rs in by.items():
        if is_muted(k[1], s) or all(classify(r, s) == "drop" for r in rs):
            with db() as c:  # 屏蔽群 / 全是噪音：零调用，只推进水位
                c.execute("INSERT INTO chat_state(source,chat,last_msg_id,updated_ts) VALUES(?,?,?,?) "
                          "ON CONFLICT(source,chat) DO UPDATE SET last_msg_id=excluded.last_msg_id", (*k, max(r["id"] for r in rs), int(time.time())))
            if not is_muted(k[1], s):
                stats["new"] += len(rs)
            continue
        stats["new"] += len(rs)
        todo[k] = rs
    stats["chats"] = len(todo)
    sem = asyncio.Semaphore(LLM_PARALLEL)
    failed, err = [], None

    async def one(k, rs):
        nonlocal err
        async with sem:
            try:
                if await update_chat(k[0], k[1], rs, s, stats):
                    stats["changed"] += 1
            except Exception as ex:
                failed.append(min(r["id"] for r in rs)); err = ex
    await asyncio.gather(*(one(k, rs) for k, rs in todo.items()))
    if full:
        with db() as c:
            c.execute("UPDATE items SET status='expired', updated_ts=? WHERE status='rebuild'", (int(time.time()),))
    # 全局水位：有群失败就停在它前面，下次重试（成功的群靠各自 last_msg_id 跳过已处理的消息）
    kv_set("scan_id", (min(failed) - 1) if failed else max(r["id"] for r in rows))
    stats["errors"] = len(failed)
    stats["secs"] = round(time.time() - t0, 2)
    if failed and not stats["changed"] and len(failed) == len(todo):
        raise err if isinstance(err, HTTPException) else HTTPException(502, str(err))
    return stats, changed or stats["changed"] > 0


def _sort_key(s):
    lv = s.get("levels") or {}
    far = datetime(2100, 1, 1, tzinfo=TZ)
    now = datetime.now(TZ)

    def k(t):
        d = parse_due(t.get("due", ""), now) or far
        return (not t["pinned"], not t["at_me"], lv.get(t["chat"]) != "important", d, -(t["first_ts"] or 0))
    return k


def build_body(hours, s) -> dict:
    """首页群报 = 各群状态 + 未完成事项（+ 这段时间里完成的，折叠显示）。不读原始消息内容。"""
    now = int(time.time())
    since = now - hours * 3600
    with db() as c:
        its = [item_dict(r) for r in c.execute(
            "SELECT * FROM items WHERE status='open' OR (status='done' AND updated_ts>=?) ORDER BY id", (since,))]
        states = c.execute("SELECT * FROM chat_state WHERE updated_ts>=? AND summary!='' ORDER BY updated_ts DESC", (since,)).fetchall()
        cnt = c.execute("SELECT source, chat, COUNT(*) n FROM msgs WHERE ts>=? GROUP BY source, chat", (since,)).fetchall()
    its = [t for t in its if not is_muted(t["chat"], s)]
    key = _sort_key(s)
    pub = lambda t: {k: t[k] for k in ("id", "key", "title", "detail", "due", "chat", "sender", "quote", "urgency",
                                       "done", "pinned", "at_me", "source", "first_ts")}
    todos = [pub(t) for t in sorted((t for t in its if t["kind"] == "todo"), key=key)]
    notices = [pub(t) for t in sorted((t for t in its if t["kind"] == "notice" and t["status"] == "open"
                                       and (t["updated_ts"] or 0) >= since), key=key)][:12]
    cnt = [r for r in cnt if not is_muted(r["chat"], s)]
    return {"todos": todos, "notices": notices,
            "groups": [{"chat": r["chat"], "gist": r["summary"]} for r in states if not is_muted(r["chat"], s)][:16],
            "count": sum(r["n"] for r in cnt), "chats": len(cnt)}


async def make_headline(body, s, weekly=False, stats=None) -> str:
    """一句话头条：输入是各群要点和事项标题（不是原始消息），一次很短的调用。"""
    opens = [t for t in body["todos"] if not t["done"]]
    if not weekly and not opens and not body["notices"] and not body["groups"]:
        return "这段时间群里很安静" if not body["count"] else "群里没什么要你管的"
    lines = [f"- 待办：{t['title']}（{t['chat']}{'，' + t['due'] if t['due'] else ''}）" for t in opens[:12]]
    lines += [f"- 通知：{n['title']}（{n['chat']}）" for n in body["notices"][:6]]
    lines += [f"- 群「{g['chat']}」：{g['gist']}" for g in body["groups"][:10]]
    if weekly:
        done = [t["title"] for t in body["todos"] if t["done"]]
        lines.append(f"- 本周已完成 {len(done)} 件：" + "、".join(done[:8]))
    out = await llm([{"role": "system", "content": "【头条】你是用户的群消息秘书。根据下面的要点写一句话头条，指出最要紧的事，25 字内。"
                                                   "只输出这句话，不要引号。没有要紧事就写：群里没什么要你管的"},
                     {"role": "user", "content": ("这是一周汇总，" if weekly else "") + "要点：\n" + ("\n".join(lines) or "（没有）")}])
    if stats is not None:
        stats["calls"] += 1
    out = (out or "").strip()
    if out.startswith("{"):
        try:
            out = str(json.loads(out).get("headline") or "")
        except (json.JSONDecodeError, AttributeError):
            pass
    return (out.splitlines() or [""])[0].strip().strip("\"“”「」")[:40] or "群里没什么要你管的"


_digest_lock = asyncio.Lock()


async def make_digest(hours=24, auto=False, full=False):
    async with _digest_lock:
        return await _make_digest(hours, auto, full)


async def _make_digest(hours=24, auto=False, full=False):
    s = settings()
    t0 = time.time()
    stats, changed = await run_update(full)
    with db() as c:
        latest = c.execute("SELECT * FROM digests ORDER BY id DESC LIMIT 1").fetchone()
    weekly = hours >= 168
    if latest and not changed and not weekly and latest["hours"] == hours:
        b = digest_row(latest)  # 没有新东西：直接返回上一期，不调模型、不新建一期
        b.update(unchanged=True, stats=stats)
        return b
    body = build_body(hours, s)
    if changed or weekly or not latest:
        body["headline"] = await make_headline(body, s, weekly, stats)
    else:  # 只是换了时间范围：沿用上一期头条
        body["headline"] = json.loads(latest["body"]).get("headline", "")
    stats["secs"] = round(time.time() - t0, 2)
    body.update(stats=stats, mode="full" if full else ("weekly" if weekly else "inc"), upto_id=int(kv_get("scan_id", 0) or 0))
    if auto:
        body["auto"] = True
    with db() as c:
        cur = c.execute("INSERT INTO digests(ts,hours,body) VALUES(?,?,?)", (int(time.time()), hours, json.dumps(body, ensure_ascii=False)))
        body["id"] = cur.lastrowid
    return body


def auto_check(s: dict, now: float | None = None):
    """该不该自动整理：返回 (原因, 时间窗口小时数) 或 None。
    有待整理的新消息（已排除屏蔽群和噪音），并且
    - 距上次整理 ≥ 间隔，且新消息已安静 2 分钟或累计 ≥ 50 条（防抖）；或
    - 新消息里有 @我 / 重要群，且距上次 ≥ 5 分钟。"""
    iv = int(s.get("auto_interval") or 0)
    if iv <= 0:
        return None
    now = now or time.time()
    tried = kv_get("auto_try")
    if tried and now - int(tried) < AUTO_URGENT_GAP:  # 刚试过（可能失败了），别每分钟都打大模型
        return None
    rows, _ = pending_info(s)
    if not rows:
        return None
    with db() as c:
        last = c.execute("SELECT ts, hours FROM digests ORDER BY id DESC LIMIT 1").fetchone()
    last_ts = last["ts"] if last else 0
    last_h = last["hours"] if last else 24
    if last_h >= 168 and now - last_ts < 3600:  # 刚出周报，先让用户看一会儿
        return None
    hours = last_h if last_h in (24, 72) else 24
    gap = now - last_ts
    quiet = now - max(r["ts"] for r in rows)
    lv = s.get("levels") or {}
    if gap >= iv * 60 and (quiet >= DEBOUNCE_QUIET or len(rows) >= DEBOUNCE_BURST):
        return "interval", hours
    if gap >= AUTO_URGENT_GAP and any(r["at_me"] or lv.get(r["chat"]) == "important" for r in rows):
        return "urgent", hours
    return None


async def auto_digest(s: dict | None = None):
    s = s or settings()
    chk = auto_check(s)
    if not chk:
        return None
    kv_set("auto_try", int(time.time()))
    with db() as c:
        before = {r["id"] for r in c.execute("SELECT id FROM items")}
    d = await make_digest(chk[1], auto=True)
    # 只在有「新」待办时推一条；免打扰时段只更新不推送
    if s.get("push_digest") and s.get("bark_url") and not in_quiet(s) and not d.get("unchanged"):
        new = [t for t in d.get("todos", []) if t.get("id") and t["id"] not in before and not t.get("done")]
        if new:
            await push(f"群报更新 · 新增 {len(new)} 件待办", "；".join(t.get("title", "") for t in new[:3]), key="auto")
    return d


def _item_from_key(k: str):
    if isinstance(k, str) and k.startswith("item:") and k[5:].isdigit():
        with db() as c:
            return c.execute("SELECT * FROM items WHERE id=?", (int(k[5:]),)).fetchone()
    return None


async def check_snoozed():
    now = int(time.time())
    with db() as c:
        rows = c.execute("SELECT * FROM snooze WHERE until<=?", (now,)).fetchall()
    done = done_records()
    sent = 0
    for r in rows:
        with db() as c:
            c.execute("DELETE FROM snooze WHERE k=?", (r["k"],))
        it = _item_from_key(r["k"])
        if it is not None:  # 挂在事项 id 上：完成了 / 已关闭就不提醒
            if it["status"] != "open":
                continue
            t = {"title": it["title"], "chat": it["chat"]}
        else:
            t0, ch0 = split_key(r["k"])
            t = {"title": r["title"] or t0, "chat": r["chat"] or ch0, "due": r["due"] or ""}
            if find_match(t, done):
                continue
        await push("稍后提醒：" + t["title"], t["chat"], force=True)
        sent += 1
    return sent


async def check_reminders():
    await check_snoozed()
    s = settings()
    n = int(s.get("remind_hours") or 0)
    if n <= 0 or not s.get("bark_url"):
        return 0
    now = datetime.now(TZ)
    sent = 0

    async def fire(t):
        due = parse_due(t.get("due", ""), now)
        if not due or not (timedelta(0) <= due - now <= timedelta(hours=n)):
            return False
        left = int((due - now).total_seconds() // 60)
        when = f"{left // 60} 小时 {left % 60} 分钟" if left >= 60 else f"{left} 分钟"
        await push("快截止了：" + t.get("title", ""), f"还剩 {when}（{t.get('due')}）· {t.get('chat', '')}", force=True)
        return True
    with db() as c:  # 新：挂在事项 id 上，每件事只提醒一次，完成的不提醒
        its = c.execute("SELECT * FROM items WHERE kind='todo' AND status='open' AND reminded=0 AND due!=''").fetchall()
    for it in its:
        if not is_muted(it["chat"], s) and await fire(dict(it)):
            with db() as c:
                c.execute("UPDATE items SET reminded=1 WHERE id=?", (it["id"],))
            sent += 1
    # 兼容：旧版群报（没有事项 id）里的待办
    with db() as c:
        d = c.execute("SELECT * FROM digests ORDER BY id DESC LIMIT 1").fetchone()
        done = _records(c, "todo_done")
        seen = _records(c, "reminded")
    for t in (json.loads(d["body"]).get("todos", []) if d else []):
        if t.get("id") or find_match(t, done) or find_match(t, seen):
            continue
        if await fire(t):
            with db() as c:
                c.execute("INSERT OR REPLACE INTO reminded(k,ts,title,chat,due) VALUES(?,?,?,?,?)",
                          (todo_key(t), int(time.time()), t.get("title") or "", t.get("chat") or "", t.get("due") or ""))
            seen.append({"k": todo_key(t), "title": t.get("title") or "", "chat": t.get("chat") or "", "due": t.get("due") or ""})
            sent += 1
    return sent


def migrate_items():
    """旧数据迁移：items 表为空而有旧群报时，把最近一期的待办/通知导入（保留已完成、置顶），
    各群要点导入 chat_state，水位设到当前，避免第一次就把所有旧消息重新整理一遍。"""
    with db() as c:
        if c.execute("SELECT 1 FROM items LIMIT 1").fetchone() or kv_get("migrated_items"):
            return 0
        d = c.execute("SELECT * FROM digests WHERE hours<168 ORDER BY id DESC LIMIT 1").fetchone()
        if not d:
            return 0
        body = json.loads(d["body"])
        done, pins = _records(c, "todo_done"), _records(c, "pins")
        srcs = {r["chat"]: r["source"] for r in c.execute("SELECT chat, source FROM msgs GROUP BY chat")}
        top = c.execute("SELECT MAX(id) i FROM msgs").fetchone()["i"] or 0
        n = 0
        for kind, lst in (("todo", body.get("todos") or []), ("notice", body.get("notices") or [])):
            for t in lst:
                if not t.get("title"):
                    continue
                st = "done" if kind == "todo" and find_match(t, done) else "open"
                c.execute("INSERT INTO items(source,chat,kind,title,detail,due,sender,quote,urgency,status,first_ts,updated_ts,pinned) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (srcs.get(t.get("chat"), "QQ"), t.get("chat") or "", kind, t["title"], t.get("detail") or "", t.get("due") or "",
                           t.get("sender") or "", t.get("quote") or "", t.get("urgency") if t.get("urgency") in ("high", "mid", "low") else "mid",
                           st, d["ts"], d["ts"], int(bool(find_match(t, pins)))))
                n += 1
        for g in body.get("groups") or []:
            if g.get("chat"):
                c.execute("INSERT OR REPLACE INTO chat_state(source,chat,last_msg_id,summary,updated_ts) VALUES(?,?,?,?,?)",
                          (srcs.get(g["chat"], "QQ"), g["chat"], top, g.get("gist") or "", d["ts"]))
        c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('scan_id',?)", (str(body.get("upto_id") or top),))
        c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('migrated_items','1')")
    return n


def digest_hours(s: dict) -> set:
    hs = {int(s.get("digest_hour", 21))}
    h2 = int(s.get("digest_hour2", -1))
    if 0 <= h2 <= 23:
        hs.add(h2)
    return hs


async def scheduler():
    async def loop():
        last, cleaned = set(), None
        while True:
            now = datetime.now(TZ)
            s = settings()
            ran = False
            if now.hour in digest_hours(s) and (now.date(), now.hour) not in last:
                ran = True
                last = {(now.date(), now.hour)}
                try:
                    d = await make_digest(24)
                    if s.get("push_digest"):
                        n = len(d.get("todos", []))
                        await push("今日群报" + (f" · {n} 件待办" if n else ""), d.get("headline", ""), force=True)
                except Exception as ex:
                    print("自动总结失败:", ex)
            if (s.get("weekly_digest") and now.weekday() == 6 and now.hour == 20
                    and ("wk", now.date()) not in last):
                last = last | {("wk", now.date())}
                ran = True
                try:
                    d = await make_digest(168)
                    if s.get("push_digest"):
                        await push(weekly_title(d), f"{d.get('count', 0)} 条消息 · {d.get('chats', 0)} 个群。" + d.get("headline", ""), force=True)
                except Exception as ex:
                    print("周报失败:", ex)
            if not ran:
                try:
                    await auto_digest(s)
                except Exception as ex:
                    print("自动整理失败:", ex)
            try:
                await flush_held()
                await check_reminders()
            except Exception as ex:
                print("截止提醒失败:", ex)
            if now.hour == 4 and cleaned != now.date():  # 每天凌晨清理过期消息（不依赖正好 4:00 这一分钟醒着）
                cleaned = now.date()
                with db() as c:
                    c.execute("DELETE FROM msgs WHERE ts<?", (int(time.time()) - max(1, int(settings().get("keep_days") or KEEP_DAYS)) * 86400,))
            await asyncio.sleep(60)
    return asyncio.create_task(loop())


# ---------------- 网页接口 ----------------
URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&*+=%]+")


def extract_links(rows, limit=20):
    seen, out = set(), []
    for r in rows:
        for u in URL_RE.findall(r["text"] or ""):
            u = u.rstrip(".,;!?，。；！？、")
            if u in seen:
                continue
            seen.add(u)
            out.append({"url": u, "chat": r["chat"], "sender": r["sender"], "ts": r["ts"]})
            if len(out) >= limit:
                return out
    return out


def todo_diff(cur: dict, prev: dict | None) -> dict | None:
    if prev is None:
        return None
    old, new = prev.get("todos", []) or [], cur.get("todos", []) or []
    if any(t.get("id") for t in new):  # 新版：按事项 id 对比
        oi = {t.get("id") for t in old if t.get("id")}
        ni = {t.get("id") for t in new if t.get("id")}
        return {"new": [t["title"] for t in new if t.get("id") and t["id"] not in oi and not t.get("done")],
                "gone": len(oi - ni), "kept": len(ni & oi)}
    added = [t.get("title", "") for t in new if not find_match(t, [{"k": todo_key(p), **p} for p in old])]
    gone = sum(1 for p in old if not find_match(p, [{"k": todo_key(t), **t} for t in new]))
    return {"new": added, "gone": gone, "kept": len(new) - len(added)}


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
        pv = c.execute("SELECT body FROM digests WHERE id<? ORDER BY id DESC LIMIT 1", (d["id"],)).fetchone() if d else None
        ref = now if (not d or d["id"] == latest_id) else d["ts"]
        ats = c.execute("SELECT * FROM msgs WHERE at_me=1 AND ts>=? AND ts<=? ORDER BY ts DESC LIMIT 30",
                        (ref - span, ref + 3600 * 24)).fetchall()
        per = c.execute("SELECT chat, COUNT(*) n FROM msgs WHERE ts>=? AND ts<=? GROUP BY chat",
                        (ref - span, ref)).fetchall()
        link_rows = c.execute("SELECT ts, chat, sender, text FROM msgs WHERE text LIKE '%http%' AND ts>=? AND ts<=? ORDER BY ts DESC LIMIT 200",
                              (ref - span, ref)).fetchall()
        today = c.execute("SELECT COUNT(*) n FROM msgs WHERE ts>=?", (now - 86400,)).fetchone()["n"]
        last = c.execute("SELECT MAX(ts) t FROM msgs").fetchone()["t"]
        hb = c.execute("SELECT v FROM kv WHERE k='heartbeat'").fetchone()
        wx = c.execute("SELECT v FROM kv WHERE k='seen_wx'").fetchone()
        last_qq = c.execute("SELECT MAX(ts) t FROM msgs WHERE source='QQ'").fetchone()["t"]
        last_wx = c.execute("SELECT MAX(ts) t FROM msgs WHERE source='微信'").fetchone()["t"]
        srcs = {r["chat"]: r["source"] for r in c.execute("SELECT chat, source FROM msgs WHERE ts>=? GROUP BY chat",
                                                            (ref - span,)).fetchall()}
        latest = latest_id
    hb_ts = int(hb["v"]) if hb else None
    wx_ts = max(int(wx["v"]) if wx else 0, last_wx or 0) or None
    qq_on = bool(hb_ts and now - hb_ts < 180) or bool(last_qq and now - last_qq < 1800)
    wx_on = bool(wx_ts and now - wx_ts < 6 * 3600)
    body = annotate_todos(digest_row(d)) if d else None
    # done / pins：存着的原始 key + 当前这期里被模糊匹配上的 key（前端两者都认）
    with db() as c:
        done = [r["k"] for r in c.execute("SELECT k FROM todo_done ORDER BY ts DESC LIMIT 500")]
        pins = [r["k"] for r in c.execute("SELECT k FROM pins")]
    done += [t["key"] for t in (body or {}).get("todos", []) if t.get("done") and t["key"] not in done]
    pins += [t["key"] for t in (body or {}).get("todos", []) if t.get("pinned") and t["key"] not in pins]
    s = settings()
    _, pend = pending_info(s)
    return {
        "digest": body,
        "auto_interval": int(s.get("auto_interval") or 0),
        "pending": pend,
        "checked_ts": int(kv_get("checked_ts", 0) or 0) or None,
        "llm_calls": int(kv_get("llm_calls", 0) or 0),
        "diff": todo_diff(digest_row(d), json.loads(pv["body"])) if d and pv else None,
        "digest_ts": d["ts"] if d else None,
        "hours": d["hours"] if d else 24,
        "is_latest": (not d) or d["id"] == latest,
        "per_chat": {r["chat"]: r["n"] for r in per},
        "chat_src": srcs,
        "at_me": [{"ts": r["ts"], "chat": r["chat"], "sender": r["sender"], "text": r["text"],
                   "source": r["source"]} for r in ats],
        "today": today,
        "links": extract_links(link_rows),
        "done": done,
        "pins": pins,
        "status": {"last_msg": last, "heartbeat": hb_ts, "online": qq_on or wx_on,
                   "qq": {"online": qq_on, "seen": hb_ts or last_qq, "last": last_qq},
                   "wx": {"online": wx_on, "seen": wx_ts, "last": last_wx, "ready": bool(INGEST_TOKEN)},
                   "llm": bool(LLM_KEY), "llm_err": (LLM_STATE["err"] if not LLM_STATE["ok"] else ""),
                   "llm_err_ts": LLM_STATE["ts"], "version": VERSION},
    }


@app.post("/api/digest", dependencies=[Depends(auth)])
async def digest_now(req: Request):
    d = await req.json()
    hours = int(d.get("hours", 24))
    return await make_digest(max(1, min(hours, 168)), full=bool(d.get("full")))


@app.get("/api/digests", dependencies=[Depends(auth)])
def digests(limit: int = 30):
    with db() as c:
        rows = c.execute("SELECT * FROM digests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        b = json.loads(r["body"])
        out.append({"id": r["id"], "ts": r["ts"], "hours": r["hours"], "headline": b.get("headline", ""),
                    "todos": len([t for t in b.get("todos", []) if not t.get("done")]), "count": b.get("count", 0), "auto": bool(b.get("auto"))})
    return out


@app.post("/api/todo", dependencies=[Depends(auth)])
async def todo(req: Request):
    d = await req.json()
    it = _item_from_key(d.get("key"))
    if it is not None:  # 新版：直接改事项状态
        with db() as c:
            if "pin" in d:
                c.execute("UPDATE items SET pinned=? WHERE id=?", (int(bool(d["pin"])), it["id"]))
            else:
                c.execute("UPDATE items SET status=?, updated_ts=? WHERE id=?",
                          ("done" if d.get("done") else "open", int(time.time()), it["id"]))
        return {"ok": True, "id": it["id"]}
    t0, ch0 = split_key(d["key"])
    t = {"title": d.get("title") or t0, "chat": d.get("chat") or ch0, "due": d.get("due") or ""}
    table, on = ("pins", d["pin"]) if "pin" in d else ("todo_done", d.get("done"))
    with db() as c:
        if on:
            c.execute(f"INSERT OR REPLACE INTO {table}(k,ts,title,chat,due) VALUES(?,?,?,?,?)",
                      (d["key"], int(time.time()), t["title"], t["chat"], t["due"]))
        else:  # 取消时把模糊匹配到的旧记录一起删掉，否则下次整理又会被认成已完成
            ks = [r["k"] for r in _records(c, table) if r["k"] == d["key"] or same_todo(t, r)]
            c.executemany(f"DELETE FROM {table} WHERE k=?", [(k,) for k in ks])
    return {"ok": True}


def _ics_esc(t: str) -> str:
    return (t or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def build_ics(title: str, due: str, detail: str = "", chat: str = "") -> str:
    now = datetime.now(TZ)
    dt = parse_due(due, now)
    stamp = now.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    uid = hashlib.md5(f"{title}|{due}".encode()).hexdigest() + "@qunbao"
    if dt:
        s = dt.astimezone(ZoneInfo("UTC"))
        e = s + timedelta(hours=1)
        when = f"DTSTART:{s:%Y%m%dT%H%M%SZ}\r\nDTEND:{e:%Y%m%dT%H%M%SZ}"
    else:
        d0 = now.date()
        when = f"DTSTART;VALUE=DATE:{d0:%Y%m%d}\r\nDTEND;VALUE=DATE:{d0 + timedelta(days=1):%Y%m%d}"
    desc = "\n".join(x for x in [detail, f"来自群：{chat}" if chat else "", f"原定：{due}" if due else ""] if x)
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//qunbao//CN\r\nBEGIN:VEVENT\r\n"
            f"UID:{uid}\r\nDTSTAMP:{stamp}\r\n{when}\r\nSUMMARY:{_ics_esc(title)}\r\n"
            f"DESCRIPTION:{_ics_esc(desc)}\r\nBEGIN:VALARM\r\nTRIGGER:-PT1H\r\nACTION:DISPLAY\r\n"
            "DESCRIPTION:待办提醒\r\nEND:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")


@app.get("/api/ics", dependencies=[Depends(auth)])
def ics(title: str = "", due: str = "", detail: str = "", chat: str = ""):
    if not title.strip():
        raise HTTPException(400, "缺少标题")
    return Response(build_ics(title[:200], due[:60], detail[:500], chat[:80]), media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": "inline; filename=todo.ics"})


def activity_stats(days: int = 7, source: str = "", now: int = 0):
    now = now or int(time.time())
    since = now - days * 86400
    hours = [0] * 24
    groups = {}
    with db() as c:
        for r in c.execute("SELECT ts, chat FROM msgs WHERE ts>=? AND (?='' OR source=?)", (since, source, source)):
            hours[time.localtime(r["ts"]).tm_hour] += 1
            groups[r["chat"]] = groups.get(r["chat"], 0) + 1
    top = sorted(groups.items(), key=lambda x: -x[1])[:5]
    return {"days": days, "hours": hours, "total": sum(hours), "top": [{"chat": k, "n": v} for k, v in top]}


@app.get("/api/activity", dependencies=[Depends(auth)])
def activity(days: int = 7, source: str = ""):
    return activity_stats(max(1, min(days, 90)), source)


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
                        "muted": is_muted(r["chat"], s),
                        "level": (s.get("levels") or {}).get(r["chat"], "normal")})
    return out


@app.get("/api/messages", dependencies=[Depends(auth)])
def messages(chat: str = "", q: str = "", before: int = 0, limit: int = 60, source: str = "",
             sender: str = "", since: int = 0, until: int = 0):
    sql, args = "SELECT * FROM msgs WHERE 1=1", []
    if chat:
        sql += " AND chat=?"; args.append(chat)
    if source:
        sql += " AND source=?"; args.append(source)
    if q:
        sql += " AND (text LIKE ? OR sender LIKE ?)"; args += [f"%{q}%", f"%{q}%"]
    if sender:
        sql += " AND sender LIKE ?"; args.append(f"%{sender}%")
    if since:
        sql += " AND ts>=?"; args.append(since)
    if until:
        sql += " AND ts<?"; args.append(until)
    if before:
        sql += " AND id<?"; args.append(before)
    sql += " ORDER BY id DESC LIMIT ?"; args.append(min(limit, 200))
    with db() as c:
        rows = c.execute(sql, args).fetchall()
    return [{"id": r["id"], "ts": r["ts"], "chat": r["chat"], "sender": r["sender"], "text": r["text"],
             "source": r["source"], "at_me": bool(r["at_me"]),
             "imgs": r["img"].split() if r["img"] else []} for r in reversed(rows)]


@app.get("/api/settings", dependencies=[Depends(auth)])
def get_settings():
    return settings()


@app.get("/api/export", dependencies=[Depends(auth)])
def export_all():
    with db() as c:
        msgs = [dict(r) for r in c.execute("SELECT * FROM msgs ORDER BY id")]
        dg = [dict(r) for r in c.execute("SELECT * FROM digests ORDER BY id")]
    st = settings()
    st.pop("bark_url", None)
    data = {"version": VERSION, "exported": int(time.time()), "settings": st, "digests": dg, "messages": msgs}
    return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="qunbao-{datetime.now(TZ):%Y%m%d}.json"'})


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


@app.get("/sw.js")
def sw():
    return FileResponse(os.path.join(HERE, "sw.js"), media_type="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/manifest.json")
def manifest():
    return JSONResponse({"name": "群报", "short_name": "群报", "display": "standalone", "start_url": "/",
                         "background_color": "#F5F3EE", "theme_color": "#F5F3EE",
                         "icons": [{"src": "/icon.png", "sizes": "512x512", "type": "image/png"}]})


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": VERSION}


migrate_items()
