"""群报：收集 QQ（NapCat / OneBot 11）和微信（通知转发）群消息，用大模型挑出重要的事和待办。"""
import asyncio, collections, contextlib, hashlib, json, os, re, secrets, sqlite3, time
from difflib import SequenceMatcher
from datetime import datetime, timedelta
from urllib.parse import parse_qs, quote
from zoneinfo import ZoneInfo
from inbound import norm_ts, cq_images, clean_cq, parse_wx, CQ, CQ_NAME, WX_COUNT, WX_SKIP, _pick  # noqa: F401 (0.34.37 拆出)

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse, FileResponse, HTMLResponse, JSONResponse, RedirectResponse

HERE = os.path.dirname(__file__)
VERSION = "0.34.51"
from authutil import ceq as _ceq, tok_hash as _h, basic_creds, client_ip as _client_ip, COOKIE, set_session_cookie  # 0.34.51 拆出


TZ = ZoneInfo(os.getenv("APP_TZ") or "Asia/Shanghai")   # 时间解析/免打扰/每日整理都按这个时区
LLM_BASE = os.getenv("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
LLM_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
NAPCAT_HTTP = os.getenv("NAPCAT_HTTP", "http://napcat:3000").rstrip("/")
NAPCAT_TOKEN = os.getenv("NAPCAT_TOKEN", "")
WEB_USER = os.getenv("WEB_USER", "me")
WEB_PASS = os.getenv("WEB_PASS", "")
INGEST_TOKEN = os.getenv("INGEST_TOKEN", "")
ONEBOT_TOKEN = os.getenv("ONEBOT_TOKEN", "")
MAX_CHARS = int(os.getenv("MAX_CHARS", "60000"))
KEEP_DAYS = int(os.getenv("KEEP_DAYS", "30"))
NAPCAT_DATA = os.getenv("NAPCAT_DATA", "/napcat_qq")   # NapCat 的 QQ 数据目录（docker-compose 挂进来），用来清图片缓存
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "30"))  # 网页登录保持天数

# 网页「设置」里可以改的项；.env 里的值只作为第一次启动时的默认值
DEFAULTS = {
    "weekly_digest": True,
    "keep_days": KEEP_DAYS,    # 消息保留天数
    "profile": os.getenv("MY_PROFILE", ""),
    "my_names": [],            # 我的昵称/群名片：文本里出现 @昵称 就算 @我（微信通知常识别不出）
    "vip": [],                 # 重要的人（昵称/群名片），他们说话一律值得看
    "keywords": ["截止", "ddl", "提交", "开会", "考试", "缴费", "通知", "报名", "@全体成员"],
    "modes": {},               # 每个群怎么盯，键 "来源|群名"：focus 重点盯 / normal 正常 / atonly 只看@我 / off 不看
    "default_mode": "normal",  # 新出现的群默认档位
    "chat_pins": {},           # 手动置顶的群，键 "来源|群名" → 置顶时间；只管列表位置，和档位互相独立
    "modes_tip_done": False,   # 首页「给群分档」引导已处理
    # 以下为 0.30 及以前的旧字段，启动时迁移进 modes 后清空
    "only_mode": False, "allowed": [], "muted": [], "levels": {},
    "digest_hour": int(os.getenv("DIGEST_HOUR", "21")),
    "digest_hour2": -1,        # 第二次整理时间（早报+晚报）；-1 = 关闭
    "bark_url": os.getenv("BARK_URL", ""),   # 例：https://api.day.app/你的key
    "site_url": os.getenv("SITE_URL", ""),   # 推送点开后跳转的群报地址
    "push_at": True,           # @我 / 重要的人 / 关键词 实时推送
    "push_digest": True,       # 每日总结推送
    "remind_hours": 3,         # 待办截止前几小时推送提醒；0 = 关闭
    "quiet_start": -1,         # 免打扰开始（小时 0-23）；-1 = 关闭
    "quiet_end": 7,            # 免打扰结束（小时）
    "auto_on": True,           # 有新消息自动整理（0.32 起按群近实时触发；旧的 auto_interval>0 迁移为开，0 为关）
}
MODES = ("focus", "normal", "atonly", "off")
MODE_NAME = {"focus": "重点盯", "normal": "正常", "atonly": "只看@我", "off": "不看"}

@contextlib.asynccontextmanager
async def lifespan(_app):
    task = await scheduler()
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(docs_url=None, redoc_url=None, lifespan=lifespan)


@app.middleware("http")
async def _sec_headers(request, call_next):
    r = await call_next(request)
    r.headers.setdefault("X-Content-Type-Options", "nosniff")
    r.headers.setdefault("Referrer-Policy", "no-referrer")
    r.headers.setdefault("X-Frame-Options", "DENY")
    return r
ARRIVE: dict[int, float] = {}   # 消息 id → 收到的时间（自动整理防抖用；重启后丢失则退回消息 ts）
_WAKE: list = []                # 调度器的 asyncio.Event（收到消息 / 改设置时唤醒）


def wake_auto():
    for ev in _WAKE:
        ev.set()
_group_names: dict[int, str] = {}
_group_miss: dict[int, float] = {}
_group_ts: dict[int, float] = {}
_last_push: dict[str, float] = {}


# ---------------- 存储 ----------------
from store import DB, db  # noqa: F401  存储层见 store.py（建表在 import 时执行）


def settings() -> dict:
    with db() as c:
        r = c.execute("SELECT v FROM kv WHERE k='settings'").fetchone()
    s = dict(DEFAULTS)
    if r:
        old = json.loads(r["v"])
        if "auto_interval" in old and "auto_on" not in old:  # 0.31 及以前：自动整理间隔 → 开关
            old["auto_on"] = _int(old.get("auto_interval")) != 0
        old.pop("auto_interval", None)
        s.update(old)
    return s


def save_settings(new: dict):
    s = settings()
    new = dict(new)
    if "auto_interval" in new and "auto_on" not in new:  # 旧客户端
        new["auto_on"] = _int(new.pop("auto_interval")) != 0
    for k, v in new.items():
        if k in DEFAULTS:
            s[k] = v
    s["auto_on"] = bool(s.get("auto_on"))
    if s.get("default_mode") not in MODES:
        s["default_mode"] = "normal"
    s["modes"] = {k: v for k, v in (s.get("modes") or {}).items() if v in MODES and "|" in k}
    s["chat_pins"] = {k: v for k, v in (s.get("chat_pins") or {}).items() if "|" in k and isinstance(v, (int, float))}
    with db() as c:
        c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('settings',?)", (json.dumps(s, ensure_ascii=False),))
    wake_auto()  # 设置变了：解除「余额不足 / 密钥错误」的暂停，马上看一眼
    return s


# ---------------- 登录（网页登录页 + Cookie 会话；也兼容 Basic Auth 给脚本用）----------------
_fails: dict[str, list] = {}


def _pw_tag() -> str:  # 改了 WEB_PASS 之后，旧会话全部失效
    return _h("pw:" + WEB_USER + ":" + WEB_PASS)[:16]


def _basic_ok(req: Request) -> bool:
    cr = basic_creds(req.headers.get("authorization", ""))
    return bool(cr) and _ceq(cr[0], WEB_USER) and _ceq(cr[1], WEB_PASS)


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
    set_session_cookie(resp, tok, req, SESSION_DAYS)


def auth(req: Request, resp: Response):
    if not WEB_PASS:
        raise HTTPException(500, "请先在 .env 里设置 WEB_PASS")
    if _session_ok(req, resp) or _basic_ok(req):
        return
    raise HTTPException(401, "未登录")


# ---------------- 推送（Bark，iPhone 上收通知）----------------
def ckey(source, chat) -> str:
    return f"{source or 'QQ'}|{chat}"


def _legacy_mode(chat, s):
    if s.get("only_mode"):
        if chat not in (s.get("allowed") or []):
            return "off"
    elif chat in (s.get("muted") or []):
        return "off"
    return {"important": "focus", "atonly": "atonly"}.get((s.get("levels") or {}).get(chat), None)


def chat_mode(source, chat, s) -> str:
    """这个群怎么盯。按 (来源, 群名) 区分：QQ 和微信的同名群互不影响。"""
    m = (s.get("modes") or {}).get(ckey(source, chat))
    if m in MODES:
        return m
    return _legacy_mode(chat, s) or s.get("default_mode") or "normal"


def in_digest(source, chat, s) -> bool:
    """进不进整理（送不送模型）：只看@我 和 不看 都不进。"""
    return chat_mode(source, chat, s) in ("focus", "normal")


def migrate_modes():
    """0.30 → 0.31：旧的屏蔽/白名单/级别（按群名）迁移成每个 (来源, 群) 一档；已有的群全部写明档位，
    之后「新群默认档位」只影响真正新出现的群。"""
    s = settings()
    if s.get("modes_migrated"):
        return 0
    with db() as c:
        pairs = c.execute("SELECT DISTINCT source, chat FROM msgs").fetchall()
    modes = dict(s.get("modes") or {})
    for r in pairs:
        k = ckey(r["source"], r["chat"])
        if k not in modes:
            modes[k] = _legacy_mode(r["chat"], s) or "normal"
    s.update(modes=modes, only_mode=False, allowed=[], muted=[], levels={}, modes_migrated=True)
    with db() as c:
        c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('settings',?)", (json.dumps(s, ensure_ascii=False),))
    return len(pairs)


def set_modes(pairs, mode):
    s = settings()
    modes = dict(s.get("modes") or {})
    for src, chat in pairs:
        modes[ckey(src, chat)] = mode
    s["modes"] = modes
    with db() as c:
        c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('settings',?)", (json.dumps(s, ensure_ascii=False),))
    return s


def _held_get() -> list:
    try:
        return json.loads(kv_get("held", "[]") or "[]")
    except json.JSONDecodeError:
        return []


def in_quiet(s, now=None) -> bool:
    a, b = int(s.get("quiet_start", -1)), int(s.get("quiet_end", 7))
    if a < 0 or a == b:
        return False
    h = (now or datetime.now(TZ)).hour
    return (a <= h < b) if a < b else (h >= a or h < b)


def remind_start(due, n, s):
    """提醒窗口起点：默认截止前 n 小时；若这一刻落在免打扰里（凌晨 7 点的事 3 小时前是半夜 4 点，
    推了也只会被压到早上），就提前到免打扰开始前 1 小时（前一晚），睡前就知道明早有事。"""
    st = due - timedelta(hours=n)
    a = int(s.get("quiet_start", -1))
    if a >= 0 and in_quiet(s, st):
        q = due.replace(hour=a, minute=0, second=0, microsecond=0)
        if q > due:
            q -= timedelta(days=1)
        st = min(st, q - timedelta(hours=1))
    return st


async def flush_held():
    """免打扰结束后，把攒着的推送合并成一条发出。"""
    items = _held_get()
    if not items or in_quiet(settings()):
        return 0
    kv_set("held", "[]")
    items = [tuple(x[:2]) for x in items]
    body = "\n".join(f"· {t}：{b}" for t, b in items)[:300]
    await push(f"免打扰期间 {len(items)} 条消息", body, force=True, test=True)
    return len(items)


def has_push(s=None) -> bool:
    """有没有任何推送渠道：Bark 地址，或至少一台设备开了网页通知（Web Push）。"""
    s = s or settings()
    return bool(s.get("bark_url")) or push_sub_count() > 0


def chat_path(key: str) -> str:
    """某个群的推送点开后进这个群：/#chat/来源/群名"""
    if "|" not in (key or ""):
        return "/"
    src, ch = key.split("|", 1)
    return "/#chat/" + {"QQ": "qq", "微信": "wx"}.get(src, quote(src, safe="")) + "/" + quote(ch, safe="")


async def push(title: str, body: str, key: str = "", force=False, test=False, level: str = "active", gap: int = 60,
               url: str = "", tag: str = "", web: bool = True, bark: bool = True):
    """同时发 Bark 和 Web Push（主屏幕网页 App 的原生通知），哪个配了发哪个。
    level：timeSensitive（@我、快截止，专注模式也能响）/ active / passive（只进通知中心，不响不震；Web Push 不发）。
    url：点开后打开的群报内地址（/#todo/12、/#chat/qq/群名）；不给就按 key 跳到群。
    免打扰期间攒进数据库（重启不丢），到点合并成一条。"""
    s = settings()
    if not has_push(s):
        return False
    if not test and in_quiet(s):
        held = _held_get()
        if len(held) < 50:
            held.append([title, body])
            kv_set("held", json.dumps(held, ensure_ascii=False))
        return False
    if key and not force and time.time() - _last_push.get(key, 0) < gap:  # 同一个群限频
        return False
    _last_push[key] = time.time()
    path = url or chat_path(key)
    ok_b = ok_w = False
    if bark:
        ok_b = await send_bark(s.get("bark_url"), title, body, level, s.get("site_url") or "", path)
    if web and level != "passive":
        ok_w = await webpush_all({"title": title[:60], "body": body[:300], "url": path, "tag": tag or key or "",
                                  "badge": open_todo_count(s)}) > 0
    return ok_b or ok_w


# 传输层见 webpush.py / bark.py
from bark import send_bark
import webpush as _wp
_wp.bind(lambda: db(), lambda k: kv_get(k))
from webpush import vapid_keys, webpush_all, VAPID_SUB  # noqa: F401


def push_sub_count() -> int:
    with db() as c:
        return c.execute("SELECT COUNT(*) FROM push_subs").fetchone()[0]


def open_todo_count(s=None) -> int:
    """主屏幕图标角标：还没完成的待办数（不看 / 只看@我 的群不算）。"""
    s = s or settings()
    with db() as c:
        rows = c.execute("SELECT source, chat FROM items WHERE kind='todo' AND status='open'").fetchall()
    return sum(1 for r in rows if in_digest(r["source"], r["chat"], s))


def was_pushed(keys) -> bool:
    keys = [k for k in keys if k]
    if not keys:
        return False
    with db() as c:
        return c.execute(f"SELECT 1 FROM pushed WHERE k IN ({','.join('?' * len(keys))}) LIMIT 1", keys).fetchone() is not None


def mark_pushed(keys):
    now = int(time.time())
    with db() as c:
        c.executemany("INSERT OR REPLACE INTO pushed(k,ts) VALUES(?,?)", [(k, now) for k in keys if k])


async def push_msg(mid: int, *a, **k):
    """即时推送一条消息（@我 / 重点群…）。推了（或免打扰攒着了）就记下消息 id，之后整理成待办时不再推第二遍。"""
    ok = await push(*a, **k)
    if ok or in_quiet(settings()):
        mark_pushed([f"msg:{mid}"])
    return ok


async def push_new_todos(todos, s=None) -> bool:
    """自动整理出新待办：每件事只推一次；它来自的那条消息已经即时推过（@我）就不再推。
    一件：标题 = 群名，正文 = 事项一句话，点开定位到这件待办；多件合并成一条。"""
    s = s or settings()
    fresh = []
    for t in todos:
        if not t.get("id") or was_pushed([f"item:{t['id']}"]):
            continue
        mids = [f"msg:{m}" for m in str(t.get("msg_ids") or "").split() if m.isdigit()]
        mark_pushed([f"item:{t['id']}"])
        if mids and was_pushed(mids):
            continue
        fresh.append(t)
    if not fresh:
        return False
    one = lambda t: t.get("title", "") + (f"（{t['due']}）" if t.get("due") else "")
    chats = {(t.get("source") or "QQ", t.get("chat") or "") for t in fresh}
    if len(fresh) == 1:
        t = fresh[0]
        title, body, url = (t.get("chat") or "新待办"), "新待办：" + one(t), f"/#todo/{t['id']}"
    elif len(chats) == 1:
        title, body, url = (fresh[0].get("chat") or "新待办"), f"{len(fresh)} 件新待办：" + "；".join(one(t) for t in fresh[:3]), "/"
    else:
        title, body, url = "新待办", "；".join(f"{one(t)} · {t.get('chat', '')}" for t in fresh[:3]) + (
            f" 等 {len(fresh)} 件" if len(fresh) > 3 else ""), "/"
    lvl = "timeSensitive" if any(t.get("at_me") or t.get("urgency") == "high" for t in fresh) else "active"
    return await push(title, body, force=True, level=lvl, url=url, tag=f"todo-{fresh[0]['id']}")


def hit_reason(chat, sender, text, at_me, s, source="QQ"):
    """要不要即时推送，返回 (原因, Bark 级别) 或 None。不看的群一律不推；只看@我 只推 @我/@全体；广告不推。"""
    mode = chat_mode(source, chat, s)
    if mode == "off":
        return None
    if at_me:
        return ("@全体" if re.search(r"@全体成员|@所有人", text) else "@你"), "timeSensitive"
    if mode == "atonly" or AD_RE.search(text):
        return None
    if sender and any(v and v in sender for v in s["vip"]):
        return "重要的人", "active"
    hits = [k for k in s["keywords"] if k and k.lower() in text.lower()]
    if hits:
        return "关键词「" + "、".join(hits[:2]) + "」", "passive"  # 关键词命中只进通知中心，不响不震
    if mode == "focus" and KEY_RE.search(text) and not PLACEHOLDER_RE.match(text):
        return "重点群", "active"
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
    if ckey(source, chat) not in (s.get("modes") or {}):  # 新出现的群：记下默认档位
        s = set_modes([(source, chat)], chat_mode(source, chat, s))
    with db() as c:
        cur = c.execute("INSERT INTO msgs(ts,source,chat,sender,text,at_me,img) VALUES(?,?,?,?,?,?,?)",
                        (norm_ts(ts), source, chat, sender, text[:4000], int(at_me), imgs[:2000]))
    ARRIVE[cur.lastrowid] = time.time()  # 防抖按「收到的时间」算（消息自带的 ts 可能是对方手机时间）
    wake_auto()
    hit = hit_reason(chat, sender, text, at_me, s, source)
    if hit and s.get("push_at"):
        why, level = hit
        # 标题直接说是什么事：「@你 · 计科2201」+ 原话；非 @我 的每群 10 分钟最多一条
        brief = re.sub(r"\s+", " ", text)[:120]
        asyncio.create_task(push_msg(cur.lastrowid, f"{why} · {chat}", f"{sender}：{brief}", key=ckey(source, chat),
                                     level=level, gap=60 if level == "timeSensitive" else 600))


# ---------------- 收消息 ----------------
GROUP_NAME_TTL = 3600  # 群名缓存 1 小时：群主改了名，最迟一小时后跟上


def rename_chat(source: str, old: str, new: str) -> int:
    """群改名：把旧名下的消息、整理状态、事项、完成/置顶/稍后记录和群档位都改到新名下，
    不然改名后群会拆成两个（旧的一半历史 + 新的一半），待办和档位也对不上。"""
    if not old or not new or old == new:
        return 0
    with db() as c:
        n = c.execute("UPDATE msgs SET chat=? WHERE source=? AND chat=?", (new, source, old)).rowcount
        c.execute("UPDATE items SET chat=? WHERE source=? AND chat=?", (new, source, old))
        for tb in ("todo_done", "pins", "reminded", "snooze"):
            c.execute(f"UPDATE {tb} SET chat=? WHERE chat=?", (new, old))
        o = c.execute("SELECT * FROM chat_state WHERE source=? AND chat=?", (source, old)).fetchone()
        if o:
            t = c.execute("SELECT * FROM chat_state WHERE source=? AND chat=?", (source, new)).fetchone()
            if not t:
                c.execute("UPDATE chat_state SET chat=? WHERE source=? AND chat=?", (new, source, old))
            else:  # 新名下已经有状态（改名前后消息交错到过）：水位取小的，宁可多看一遍也不漏
                keep = o if o["updated_ts"] > t["updated_ts"] else t
                c.execute("UPDATE chat_state SET last_msg_id=?, summary=?, updated_ts=? WHERE source=? AND chat=?",
                          (min(o["last_msg_id"], t["last_msg_id"]), keep["summary"], keep["updated_ts"], source, new))
                c.execute("DELETE FROM chat_state WHERE source=? AND chat=?", (source, old))
        c.execute("DELETE FROM chat_brief WHERE source=? AND chat=?", (source, old))  # 速览缓存，重算即可
    s = settings()
    modes = dict(s.get("modes") or {})
    if ckey(source, old) in modes:
        m = modes.pop(ckey(source, old))
        modes.setdefault(ckey(source, new), m)
        s["modes"] = modes
    pins = dict(s.get("chat_pins") or {})
    if ckey(source, old) in pins:
        pins.setdefault(ckey(source, new), pins.pop(ckey(source, old)))
        s["chat_pins"] = pins
    with db() as c:
        c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('settings',?)", (json.dumps(s, ensure_ascii=False),))
    return n


def note_group_name(gid: int, got: str):
    """记下 群号→群名；和上次记的不一样就是改名了，历史跟着搬过去。"""
    k = f"gname:{gid}"
    old = kv_get(k) or f"群{gid}"  # 没记过：可能是以前 NapCat 没查到、消息存在「群123」名下
    if old != got:
        with db() as c:  # 别的群号现在还叫旧名（两个群同名）：不搬，免得把人家的历史抢走
            twin = c.execute("SELECT 1 FROM kv WHERE k LIKE 'gname:%' AND k!=? AND v=?", (k, old)).fetchone()
        if not twin:
            rename_chat("QQ", old, got)
    kv_set(k, got)


async def group_name(gid: int) -> str:
    fresh = time.time() - _group_ts.get(gid, time.time()) < GROUP_NAME_TTL
    if gid in _group_names and fresh:
        return _group_names[gid]
    name = _group_names.get(gid) or f"群{gid}"
    if time.time() - _group_miss.get(gid, 0) < 600:  # 查不到的 10 分钟后再试
        return name
    _group_miss[gid] = time.time()
    try:
        h = {"Authorization": f"Bearer {NAPCAT_TOKEN}"} if NAPCAT_TOKEN else {}
        async with httpx.AsyncClient(timeout=5) as cl:
            r = await cl.post(f"{NAPCAT_HTTP}/get_group_info", json={"group_id": gid}, headers=h)
            got = (r.json().get("data") or {}).get("group_name")
        if got:  # 只缓存查到的真名；查不到时下次再查，不要永远显示成「群123」
            got = str(got).strip() or got
            note_group_name(gid, got)
            _group_names[gid] = name = got
            _group_ts[gid] = time.time()
    except Exception:
        pass
    return name


# ---------------- 旧图片换新链接（不存图） ----------------
# QQ 新版图片链接 multimedia.nt.qq.com.cn/download?appid=..&fileid=..&rkey=.. 里的 rkey 几小时就过期，
# 图片本身还在 QQ 服务器上。向 NapCat 要一个当前有效的 rkey 换进去就能再看，服务器一张图都不用存。
_RKEY = {"ts": 0.0, "group": "", "private": ""}


async def fresh_rkeys(force=False):
    if not force and time.time() - _RKEY["ts"] < 1800 and (_RKEY["group"] or _RKEY["private"]):
        return _RKEY
    h = {"Authorization": f"Bearer {NAPCAT_TOKEN}"} if NAPCAT_TOKEN else {}
    async with httpx.AsyncClient(timeout=6) as cl:
        for api in ("nc_get_rkey", "get_rkey"):
            try:
                r = await cl.post(f"{NAPCAT_HTTP}/{api}", json={}, headers=h)
                data = r.json().get("data") or []
            except Exception:
                continue
            got = {}
            for x in data if isinstance(data, list) else []:
                k = str(x.get("rkey") or "").replace("&rkey=", "").lstrip("&")
                if k and x.get("type") in ("group", "private"):
                    got[x["type"]] = k
            if got:
                _RKEY.update(ts=time.time(), **got)
                return _RKEY
    return _RKEY


def swap_rkey(url: str, keys: dict) -> str:
    m = re.match(r"^https://multimedia\.nt\.qq\.com\.cn/download\?([^#\s]+)$", url or "")
    if not m:
        return ""
    q = parse_qs(m.group(1))
    key = keys.get("private" if (q.get("appid") or [""])[0] == "1406" else "group") or keys.get("group") or keys.get("private")
    if not key or not q.get("fileid"):
        return ""
    return re.sub(r"([?&])rkey=[^&]*", lambda mm: mm.group(1) + "rkey=" + key, url) if "rkey=" in url else url + "&rkey=" + key


@app.get("/api/img_fresh", dependencies=[Depends(auth)])
async def img_fresh(u: str, retry: int = 0):
    """图片链接过期时前端来要新链接；只换 QQ 官方图片域名的 rkey，换不了返回空（前端显示「图片已失效」）。"""
    keys = await fresh_rkeys(force=bool(retry))
    return {"url": swap_rkey(u, keys)}


@app.post("/onebot")
async def onebot(req: Request):
    if ONEBOT_TOKEN:
        auth = req.headers.get("Authorization", "")
        tok = auth[7:] if auth.lower().startswith("bearer ") else (req.query_params.get("token") or "")
        if not _ceq(tok, ONEBOT_TOKEN):
            raise HTTPException(401, "口令不对")
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


def mark_seen(key):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (key, str(int(time.time()))))


@app.post("/ingest")
async def ingest(req: Request):
    """通用入口：微信通知转发等。口令放 Header X-Token，或网址 ?token=。JSON 或表单都行。"""
    tok = req.headers.get("X-Token") or req.query_params.get("token") or ""
    if not INGEST_TOKEN or not _ceq(tok, INGEST_TOKEN):
        raise HTTPException(401, "口令不对")
    ct = req.headers.get("content-type", "")
    body = await req.body()
    if len(body) > 256 * 1024:
        raise HTTPException(413, "内容太大")
    raw = body.decode("utf-8", "ignore")
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
LLM_STATE = {"ok": True, "err": "", "ts": 0, "code": 0}


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
from ics import build_ics as ics_build  # noqa: E402
from llm_errors import is_censored, llm_short, REJECT_CODES, LLM_FATAL_CODES  # noqa: E402
from llm_errors import llm_err_text as _llm_err_text  # noqa: E402


def llm_err_text(code: int, text: str) -> str:
    return _llm_err_text(code, text, LLM_MODEL)




LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "60"))  # 单次调用最多等这么久（以前 180 秒，超时再重试一次，一个群能「正在整理」6 分钟）
LLM_TRIES = 5                 # 429 最多等 4 轮（3/8/20/45 秒，或按服务商的 Retry-After）
LLM_429_WAITS = (3, 8, 20, 45)
LLM_LAST_OK = [0.0]            # 最近一次调用成功的时间：判断「模型是好的，只是拒收这一块」还是「配置错了」
_CALL_TS: collections.deque = collections.deque(maxlen=2000)   # 最近的模型请求时间
LLM_COOL = [0.0]              # 全局冷却到这个时间点：被限流时所有调用一起等，而不是各自继续撞


def llm_err_shown() -> str:
    """首页黄条：要用户动手的错误（余额/密钥/地址）立刻显示；限流、超时这类偶发错误后台自己重试，连续 5 分钟都失败才显示。"""
    if LLM_STATE.get("ok", True) or not LLM_STATE.get("err"):
        return ""
    if LLM_STATE.get("code") in LLM_FATAL_CODES or time.time() - (LLM_STATE.get("fail_since") or time.time()) >= 300:
        return LLM_STATE["err"]
    return ""


def llm_log(kind: str):
    now = int(time.time())
    with db() as c:
        c.execute("INSERT INTO llm_log(ts,kind) VALUES(?,?)", (now, kind))


def llm_calls_since(sec: int = 3600) -> dict:
    with db() as c:
        rows = c.execute("SELECT kind, COUNT(*) n FROM llm_log WHERE ts>=? GROUP BY kind", (int(time.time()) - sec,)).fetchall()
    d = {r["kind"]: r["n"] for r in rows}
    return {"total": sum(d.values()), **d}


async def llm(messages, as_json=False):
    if not LLM_KEY:
        LLM_STATE.update(ok=False, err="还没配置大模型：在 .env 里设置 LLM_API_KEY", ts=int(time.time()), code=-1)
        raise HTTPException(500, "请先在 .env 里设置 LLM_API_KEY", headers={"x-llm-code": "-1"})
    sysm = str((messages or [{}])[0].get("content") or "")
    kind = next((v for k, v in (("【群更新】", "chat"), ("【头条】", "head"), ("【问答】", "ask"), ("【群简报】", "brief"))
                 if sysm.startswith(k)), "other")
    code = 0
    body = {"model": LLM_MODEL, "messages": messages, "temperature": 0.2}
    if as_json:
        body["response_format"] = {"type": "json_object"}
    err = None
    for attempt in range(LLM_TRIES):  # 429/5xx/超时 自动重试；429 时全局冷却，所有请求一起排队等，不再一窝蜂撞限流
        wait = LLM_COOL[0] - time.time()
        if wait > 0:
            await asyncio.sleep(min(wait, 120))
        _CALL_TS.append(time.time())  # 自动整理的全局限速看这个（含失败重试的请求）
        try:
            async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as cl:
                r = await cl.post(f"{LLM_BASE}/chat/completions", json=body,
                                  headers={"Authorization": f"Bearer {LLM_KEY}"})
                r.raise_for_status()
                out_j = r.json()
                out = out_j["choices"][0]["message"]["content"]
                LLM_STATE.update(ok=True, err="", ts=int(time.time()), code=0, fail_since=0)
                LLM_LAST_OK[0] = time.time()
                kv_add("llm_calls", 1)
                kv_add("llm_tokens", int((out_j.get("usage") or {}).get("total_tokens") or 0))
                llm_log(kind)
                return out
        except httpx.HTTPStatusError as ex:
            code = ex.response.status_code
            if is_censored(code, ex.response.text):
                code = 451  # 内容审核拦截：重试同样内容没用，交给调用方拆小块/跳过
                err = "大模型的内容审核拦下了部分消息：已自动跳过这几条，其余照常整理"
                break
            err = llm_err_text(code, ex.response.text)
            if code == 429:
                ra = _int(ex.response.headers.get("retry-after")) or LLM_429_WAITS[min(attempt, len(LLM_429_WAITS) - 1)]
                LLM_COOL[0] = max(LLM_COOL[0], time.time() + min(ra, 120))
                continue
            if code < 500:
                break
        except httpx.TimeoutException:
            code = 0
            err = "大模型接口超时：服务商可能拥堵，稍后会自动重试"
        except httpx.HTTPError as ex:
            err = f"连不上大模型接口（{LLM_BASE}）：检查服务器网络或 LLM_BASE_URL。{str(ex)[:80]}"
        if attempt < 1:
            await asyncio.sleep(LLM_RETRY_WAIT)
        elif code != 429:
            break
    if code != 451:  # 审核拦截由调用方拆块跳过，不算「大模型坏了」，不挂红色报错
        LLM_STATE.update(ok=False, err=err, ts=int(time.time()), code=code, fail_since=LLM_STATE.get("fail_since") or time.time())
    raise HTTPException(502, err, headers={"x-llm-code": str(code)})


def transcript(hours: int):
    s = settings()
    since = int(time.time()) - hours * 3600
    with db() as c:
        rows = c.execute("SELECT * FROM msgs WHERE ts>=? ORDER BY ts", (since,)).fetchall()
    rows = [r for r in rows if in_digest(r["source"], r["chat"], s) and classify(r, s) != "drop"]
    lines = [f"[{datetime.fromtimestamp(r['ts'], TZ):%m-%d %H:%M}][{r['source']}·{r['chat']}]"
             f"{'【重点群】' if chat_mode(r['source'], r['chat'], s) == 'focus' else ''}"
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
    imp = [k.split("|", 1)[1] for k, v in (s.get("modes") or {}).items() if v == "focus"]
    if imp:
        parts.append("重要的群（这些群里的事权重更高）：" + "、".join(imp))
    if s["keywords"]:
        parts.append("用户关心的关键词：" + "、".join(s["keywords"]))
    return "\n".join(parts)


from todo_match import (parse_due, todo_key, split_key, norm_title, same_todo, _PUNCT, _NUM_RE, _norm_chat, _norm_due, _day_tokens, _cn_hour, _grams)  # noqa: F401


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
    if isinstance(body.get("todos"), list):
        body["todos"] = merge_cross_chat(body["todos"])
    return body


def merge_cross_chat(todos: list) -> list:
    """多个群提到同一件事只留一条（先出现的），其余群名记入 also。已完成的不参与合并。"""
    out = []
    for t in todos:
        if t.get("done"):
            out.append(t)
            continue
        host = next((o for o in out if not o.get("done") and _norm_chat(o.get("chat")) != _norm_chat(t.get("chat"))
                     and same_todo(o, t, cross_chat=True)), None)
        if host is None:
            out.append(t)
            continue
        also = host.setdefault("also", [])
        for c in [t.get("chat")] + (t.get("also") or []):
            if c and c != host.get("chat") and c not in also:
                also.append(c)
        if t.get("pinned"):
            host["pinned"] = True
    return out


@app.post("/api/todo/snooze", dependencies=[Depends(auth)])
async def todo_snooze(req: Request):
    d = await req.json()
    h = d.get("hours")
    now = datetime.now(TZ)
    at = snooze_until(h, now)
    until = int(at.timestamp())
    t0, ch0 = split_key(d["key"])
    it = _item_from_key(d["key"])
    due_txt = (it["due"] if it is not None else d.get("due")) or ""
    with db() as c:
        c.execute("INSERT OR REPLACE INTO snooze(k,title,until,chat,due) VALUES(?,?,?,?,?)",
                  (d["key"], d.get("title") or t0, until, d.get("chat") or ch0, due_txt))
    when = ("今天" if at.date() == now.date() else "明天") + f" {at:%H:%M}" if h == "tomorrow" else f"{at:%H:%M}"
    msg = f"{when} 提醒你" if h == "tomorrow" else f"{int(float(h or 1))} 小时后（{when}）提醒你"
    due = parse_due(due_txt, now) if due_txt else None
    if due and due <= at:  # 提醒晚于截止：提醒就没意义了，说清楚
        msg = f"提醒设在 {when}，但那时已过截止（{due_txt}）"
    warn = "" if has_push() else "还没开启通知（设置 → 通知），到点发不出提醒"
    return {"ok": True, "until": until, "warn": warn, "msg": msg, "late": bool(due and due <= at)}


# ================= 整理引擎（0.30.0）：按群增量 + 事项稳定 ID + 噪音预过滤 =================
# 每个群一份状态（chat_state），每件事一条记录（items）。整理时只处理有新消息的群，
# 只把「该群旧要点 + 未完成事项（带 id）+ 新消息」交给模型，模型用 update/new/close 回答。
# 用户勾完成 = items.status='done'，任何重新整理都不会复活。
CHUNK_MSGS = int(os.getenv("CHUNK_MSGS", "120"))       # 单群新消息太多时，每块最多这么多条
CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "6000"))   # 每块最多这么多字（约 token 上限的保守估计）
LLM_PARALLEL = 4             # 手动/定时整理时同时整理的群数
from auto_stats import summarize  # noqa: E402
from auto_due import plan_chats  # noqa: E402
from headline import check_headline, live_head_plan, tidy_headline, rule_headline, QUIET_HEADS, _head_score, HEAD_MATCH  # noqa: E402,F401 (0.34.31 拆出)
from auto_due import chat_due, fail_record, should_give_up  # noqa: E402 (0.34.29 拆出)
from noise import is_noise, NOISE_WORDS, NOISE_PH, PLACEHOLDER_RE, RECALL_RE, SYS_RE  # noqa: E402 (0.34.28 拆出)


KEY_RE = re.compile(r"\d{1,2}[:：点时]\d{0,2}|\d{1,2}月\d{1,2}|\d{1,2}[/-]\d{1,2}|周[一二三四五六日天]|星期|今天|明天|后天|今晚|明早|下周|"
                    r"[¥￥]\s?\d|\d+(\.\d+)?\s?(元|块)|https?://|通知|截止|ddl|务必|提交|上交|缴费|交费|报名|考试|开会|会议|签到|作业|报告|"
                    r"取消|改到|推迟|提前|地点|集合", re.I)
ITEM_FIELDS = ("detail", "due", "urgency", "quote")
# 线报/返利/砍一刀/代取：不送模型、不推送、不进链接（@我 的除外）
# 0.34：只有「高置信度广告」才不送模型：至少两类强特征同时命中（其中一类是促销/拼团/代取兼职），且不带任何通知类字眼。
# 拿不准一律送模型（例：「缴费链接今晚截止 https://… ¥50」只命中 链接+金额，照样送）
from timing import snooze_until, digest_hours, weekly_title  # noqa: E402,F401 (0.34.35 拆出)
from atme import at_me_view  # noqa: E402 (0.34.50 拆出)
from textclean import _jparse, _int, AD_CATS, AD_VETO, DATE_IN_TITLE, URL_ONLY_RE, is_ad_sure, tidy_title, same_text  # noqa: E402,F401 (0.34.35 拆出)


def classify(r, s) -> str:
    """噪音预过滤：drop 不送模型（原文页照常显示）；key 重点（@我/重要群/时间金额链接/关键词/重要的人）；keep 普通。"""
    text = (r["text"] or "").strip()
    mode = chat_mode(r["source"] if "source" in r.keys() else "QQ", r["chat"], s)
    if mode in ("off", "atonly"):  # 不看 / 只看@我：都不送模型（@我 的消息在首页「@我」里直接看）
        return "drop"
    if r["at_me"]:
        return "key"
    vip = bool(r["sender"] and any(v and v in r["sender"] for v in s.get("vip") or []))
    if is_noise(text) or (not vip and is_ad_sure(text)):  # 0.34：噪音极保守，广告只跳高置信度的；其余一律送模型，由模型判断
        return "drop"
    if KEY_RE.search(text) or any(k and k.lower() in text.lower() for k in s.get("keywords") or []) or vip:
        return "key"
    return "key" if mode == "focus" else "keep"


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


_pend_cache = {"key": None, "cls": {}}


def pending_info(s=None):
    """待整理的新消息：已排除不进整理的群和噪音。首页每分钟轮询一次，分类结果按消息 id 缓存（设置变了才重算）。"""
    s = s or settings()
    key = hashlib.md5(json.dumps(s, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if _pend_cache["key"] != key or len(_pend_cache["cls"]) > 200000:
        _pend_cache.update(key=key, cls={})
    cc = _pend_cache["cls"]
    rows = []
    for r in _scan_rows():
        ck = (r["id"], r["source"], r["chat"])  # 带上群名：群改名后同一条消息按新群的档位重新分类
        k = cc.get(ck)
        if k is None:
            k = cc[ck] = classify(r, s)
        if k != "drop":
            rows.append(r)
    return rows, pend_detail(rows)


def pend_detail(rows, now=None) -> dict:
    """待整理消息的状态，给首页那一行用：等了多久、是在整理还是失败等重试，不再永远写「正在自动整理」。"""
    now = now or time.time()
    keys = {(r["source"], r["chat"]) for r in rows}
    out = {"msgs": len(rows), "chats": len(keys)}
    if not rows:
        return out
    oldest = min(min(ARRIVE.get(r["id"], r["ts"]), now) for r in rows)
    run = [now - AUTO["running"][k] for k in keys if k in AUTO["running"]]
    fails = [AUTO["fail"][k] for k in keys if k in AUTO["fail"] and k not in AUTO["running"]]
    out.update(oldest=int(now - oldest), running=len(run), run_secs=int(max(run)) if run else 0, failing=len(fails))
    if fails:
        f = max(fails, key=lambda v: v["ts"])
        out.update(retry_in=max(0, int(min(v["until"] for v in fails) - now)), reason=llm_short(f.get("code", 0), f.get("err", "")))
    return out


from backlog import BACKLOG_KEEP, BACKLOG_OLD_KEEP, _line, cap_backlog, chunked as _chunked  # noqa: F401 (0.34.44 拆出)


def chunked(pairs):
    return _chunked(pairs, CHUNK_MSGS, CHUNK_CHARS)


def chat_prompt(s, source, chat):
    now = datetime.now(TZ)
    imp = "，用户标为【重点盯】" if chat_mode(source, chat, s) == "focus" else ""
    return f"""【群更新】你是用户的群消息秘书，只负责维护「{chat}」（{source}{imp}）这一个群的状态。现在是 {now:%Y-%m-%d %H:%M} 星期{"一二三四五六日"[now.weekday()]}。
{about_me(s)}
输入：这个群的旧要点 summary、仍未完成的事项 open_items（每项有固定 id）、用户最近已完成的事 done_recent，以及之后的新消息（#数字 是消息编号，★ 是重点消息，(@我) 是 @用户的）。
只输出 JSON：
{{"summary":"这个群现在的要点，40字内（没变化就原样沿用）",
"update":[{{"id":已有事项的id,"detail":"40字内","due":"截止时间","urgency":"high|mid|low","quote":"原话50字内"}}],
"new":[{{"kind":"todo|notice","title":"动词开头，15字内，只写一件事，不写日期和链接（日期放 due）","detail":"40字内","due":"尽量写成具体日期时间，如 10月10日 23:59；没有就空","sender":"谁说的","quote":"原话摘录，50字内","urgency":"high|mid|low","msg_ids":[相关消息编号]}}],
"close":[{{"id":已有事项的id,"why":"过期|取消|已解决"}}]}}
规则：
- 已有事项只能通过 id 更新（只写变化的字段）或关闭；不要把已有事项换个说法再放进 new，不要改它的标题。
- done_recent 里是用户已经完成的事，不要再新建；只有群里提出了明确不同的新要求才 new，并在 title 里写清区别。
- todo 是用户要动手做的事；notice 是值得知道的通知或变化。闲聊、广告/线报/优惠券、和用户无关的讨论一律忽略，宁缺毋滥。
- 凡是会影响用户、需要用户到场 / 配合 / 准备的安排（查寝、查卫生、点名、点到、查课、检查、考试、开会、班会、集合、签到、上交、缴费等），哪怕只是陈述句（如「今晚导员会来查寝」「下课杨导要点到」），也要建成 todo，不要只写进 summary；
  title 写成用户要做的动作，可以带上什么时候（如「今晚导员查寝：在寝室并收拾好」「下课点到：按时到场」），due 写上时间（今晚就写今天的日期 + 今晚）；@全体成员 的通知 urgency=high。
  已经过去的、已取消的、纯闲聊（「昨天查寝好严」）不要建。
- 一件事一条，不要把几件事拼进一个标题；detail 写标题没说的补充（地点、要求、金额），不要重复标题。
- urgency=high 只给 48 小时内截止或老师/领导点名要求的事。没有变化就输出空数组。"""


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
                if "due" in f:
                    f["reminded"] = 0  # 截止时间改了（会议改期）：新的时间重新提醒一次
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
            raw_title = str(n.get("title") or "").strip()
            if AD_RE.search(raw_title + " " + str(n.get("quote") or "")) and not (set(_int(m) for m in (n.get("msg_ids") or [])) & set(at_ids)):
                continue  # 模型把线报/广告当成事了
            title = tidy_title(raw_title, due=str(n.get("due") or ""))[:60]
            if not title:
                continue
            if same_text(title, str(n.get("detail") or "")):
                n = {**n, "detail": ""}
            kind = "notice" if n.get("kind") == "notice" else "todo"
            cand = {"title": title, "chat": chat, "due": str(n.get("due") or "")}
            dup = next((it for it in recent if it["kind"] == kind and same_todo(cand, it)), None)
            if dup:  # 模型把已有事项又当新的报了：已完成/已关闭的不复活，未完成的顺手更新
                if dup["status"] == "rebuild":  # 完整重新整理：同一件事沿用原 id
                    c.execute("UPDATE items SET status='open' WHERE id=?", (dup["id"],))
                    dup["status"] = "open"; changed = True
                nd = parse_due(cand["due"], datetime.fromtimestamp(now, TZ)) if cand["due"] else None
                if (dup["status"] == "expired" and nd and nd > datetime.fromtimestamp(now, TZ)
                        and _norm_due(cand["due"]) != _norm_due(dup["due"])):
                    # 过期/被关闭的事项，群里又给了一个还没到的新时间（改期）：重新打开，沿用原 id，并重新提醒
                    c.execute("UPDATE items SET status='open', due=?, reminded=0, updated_ts=? WHERE id=?", (cand["due"][:60], now, dup["id"]))
                    dup.update(status="open", due=cand["due"][:60], reminded=0); changed = True
                    continue
                if dup["status"] == "open":
                    f = {k: str(n[k])[:200] for k in ITEM_FIELDS if n.get(k) and str(n[k]) != str(dup[k])}
                    if f.get("urgency") not in (None, "high", "mid", "low"):
                        f.pop("urgency")
                    if "due" in f:
                        f["reminded"] = 0
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


from rules import AD_RE, EVENT_RE, WHEN_RE, CLOCK_RE, SOON_WORDS, PAST_RE, WHO_RE, EVENT_ACT, EVENT_NAME, rule_event  # noqa: E402,F401 (0.34.41 拆出)


def rule_todos(source, chat, rows, s, now=None) -> bool:
    """规则兜底：模型整理完（或整理失败）后再扫一遍这批消息，查寝/点名/开会这类事没有对应待办就补一条。
    已有待办引用了这条消息、同群近期已有同类待办（包括你勾完成的）就不重复；模型只建成通知的改成待办。
    不看 / 只看@我 的群、广告、噪音不处理。"""
    now = int(now or time.time())
    hits = []
    for r in sorted(rows, key=lambda r: r["id"]):
        if classify(r, s) == "drop":
            continue
        ev = rule_event(r, now)
        if ev:
            hits.append((r, ev))
    if not hits:
        return False
    changed = False
    with db() as c:
        its = [dict(x) for x in c.execute("SELECT * FROM items WHERE source=? AND chat=? AND (status='open' OR updated_ts>=?)",
                                          (source, chat, now - 2 * 86400))]
        for r, (title, due, name, urg) in hits:
            # 同一个发送人前后 2 分钟里单独发了一条 @全体成员：算这条通知是 @全体 的
            allm = any(x["sender"] == r["sender"] and abs(x["ts"] - r["ts"]) <= 120 and ("@全体" in (x["text"] or "") or "@所有人" in (x["text"] or ""))
                       for x in rows)
            urg = "high" if allm else urg
            mine = [it for it in its if str(r["id"]) in str(it.get("msg_ids") or "").split()]
            same = [it for it in its if name in (it["title"] or "") + (it.get("detail") or "")
                    and abs((it["first_ts"] or 0) - r["ts"]) <= 18 * 3600]
            todo = next((it for it in mine + same if it["kind"] == "todo"), None)
            if todo:
                continue
            note = next((it for it in mine + same if it["kind"] == "notice" and it["status"] == "open"), None)
            if note:  # 模型只当成「通知」：改成待办，标题写成要做的动作
                c.execute("UPDATE items SET kind='todo', title=?, due=CASE WHEN due='' THEN ? ELSE due END, urgency=?, updated_ts=? WHERE id=?",
                          (title, due, urg, now, note["id"]))
                note.update(kind="todo"); changed = True
                continue
            vals = dict(source=source, chat=chat, kind="todo", title=title, detail="", due=due, sender=str(r["sender"] or "")[:40],
                        quote=(r["text"] or "")[:200], urgency=urg, status="open", first_ts=now, updated_ts=now, msg_ids=str(r["id"]),
                        at_me=int(bool(r["at_me"])), pinned=0, reminded=0)
            cr = c.execute(f"INSERT INTO items({','.join(vals)}) VALUES({','.join('?' * len(vals))})", tuple(vals.values()))
            its.append({**vals, "id": cr.lastrowid, "first_ts": r["ts"]}); changed = True
    return changed


def rule_backfill(hours: int = 6, now=None) -> int:
    """升级后补一遍：最近几小时里已经整理过、但当时没变成待办的查寝/点名/开会（每个版本只跑一次）。"""
    now = int(now or time.time())
    s = settings()
    with db() as c:
        rows = c.execute("SELECT * FROM msgs WHERE ts>=? ORDER BY id", (now - hours * 3600,)).fetchall()
    by = {}
    for r in rows:
        if EVENT_RE.search(r["text"] or "") or "@全体" in (r["text"] or ""):
            by.setdefault((r["source"], r["chat"]), []).append(r)
    n = 0
    for (src, chat), rs in by.items():
        if in_digest(src, chat, s):
            n += int(rule_todos(src, chat, rs, s, now))
    return n


_chat_locks: dict = {}


async def update_chat(source, chat, rows, s, stats) -> bool:
    """同一个群同一时间只整理一次（自动整理和手动「整理」可能撞上）：拿到锁后再按水位去掉已处理的消息。"""
    lk = _chat_locks.setdefault((source, chat), asyncio.Lock())
    async with lk:
        with db() as c:
            st = c.execute("SELECT last_msg_id FROM chat_state WHERE source=? AND chat=?", (source, chat)).fetchone()
        done_id = (st["last_msg_id"] or 0) if st else 0
        rows = [r for r in rows if r["id"] > done_id]
        if not rows:
            return False
        return await _update_chat(source, chat, rows, s, stats)


async def _update_chat(source, chat, rows, s, stats) -> bool:
    """处理一个群的新消息：噪音不送模型；分块逐块更新。返回这个群的事项或要点有没有变化。"""
    with db() as c:
        st = c.execute("SELECT * FROM chat_state WHERE source=? AND chat=?", (source, chat)).fetchone()
    old_summary = summary = st["summary"] if st else ""
    rows = sorted(rows, key=lambda r: r["id"])
    pairs = [(r, cls) for r in rows if (cls := classify(r, s)) != "drop"]
    pairs, cut = cap_backlog(pairs)
    stats["backlog_skipped"] = stats.get("backlog_skipped", 0) + cut
    stats["skipped"] += cut
    changed = False
    queue = chunked(pairs)
    while queue:
        part = queue.pop(0)
        with db() as c:
            opens = [dict(r) for r in c.execute("SELECT id,kind,title,detail,due,urgency FROM items WHERE source=? AND chat=? AND status='open' ORDER BY id",
                                                (source, chat))]
            done = [r["title"] for r in c.execute("SELECT title FROM items WHERE source=? AND chat=? AND status='done' AND updated_ts>=? ORDER BY updated_ts DESC LIMIT 15",
                                                  (source, chat, int(time.time()) - 7 * 86400))]
        state = json.dumps({"summary": summary, "open_items": opens, "done_recent": done}, ensure_ascii=False)
        msgs = [{"role": "system", "content": chat_prompt(s, source, chat)},
                {"role": "user", "content": f"旧状态：{state}\n新消息（{len(part)} 条）：\n" + "\n".join(x[2] for x in part)}]
        def bisect_or_skip(why):
            nonlocal changed
            # 这一块被模型「拒收」（内容审核 / 请求本身有问题 / 反复吐不出合法 JSON）：重试同样内容没用。
            # 拆成两半各自重来；拆到 2 条以内还不行就跳过这几条，水位照常前进，整个群不再卡死、待整理不再永远清不掉
            stats["calls"] += 1
            if len(part) > 2:
                h = len(part) // 2
                queue[:0] = [part[:h], part[h:]]
            else:
                stats["skipped"] += len(part)
                stats[why] = stats.get(why, 0) + len(part)
                changed |= rule_todos(source, chat, [x[0] for x in part], s)  # 跳过的几条里有查寝/点名，规则照样建待办
        try:
            out = await llm(msgs, as_json=True)
        except HTTPException as ex:
            code = _llm_code(ex)
            # 451 审核拦截一定是内容问题；400/413/422 也可能是模型名/参数配错了：只有最近 30 分钟模型成功响应过，才认定是这块内容的问题再拆小跳过
            if code not in REJECT_CODES or (code != 451 and time.time() - LLM_LAST_OK[0] > 1800):
                raise
            bisect_or_skip("blocked")
            continue
        stats["calls"] += 1
        stats["sent"] += len(part)
        try:
            d = _jparse(out)
        except HTTPException:  # 模型偶尔吐出半截/带说明文字的 JSON：马上重问一次，不让这个群拖到下一轮
            out = await llm(msgs + [{"role": "assistant", "content": (out or "")[:500]},
                                    {"role": "user", "content": "上面不是合法 JSON。只输出一个完整的 JSON 对象，不要任何解释。"}], as_json=True)
            stats["calls"] += 1
            try:
                d = _jparse(out)
            except HTTPException:  # 连问两次都不行：多半是这几条内容让模型犯迷糊，拆小再试，别永远卡在同一块
                bisect_or_skip("garbled")
                continue
        if isinstance(d.get("summary"), str) and d["summary"].strip():
            summary = d["summary"].strip()[:80]
        changed |= apply_changes(source, chat, d, {x[0]["id"] for x in part if x[0]["at_me"]})
        changed |= rule_todos(source, chat, [x[0] for x in part], s)  # 模型漏建的查寝/点名/开会，规则补上
        if queue:  # 检查点：每块处理完就推进水位，后面的块失败（限流/超时）也不用从头重来
            with db() as c:
                c.execute("INSERT INTO chat_state(source,chat,last_msg_id,summary,updated_ts) VALUES(?,?,?,?,?) "
                          "ON CONFLICT(source,chat) DO UPDATE SET last_msg_id=MAX(last_msg_id, excluded.last_msg_id), summary=excluded.summary, updated_ts=excluded.updated_ts",
                          (source, chat, max(x[0]["id"] for x in part), summary, int(time.time())))
    with db() as c:
        c.execute("INSERT OR REPLACE INTO chat_state(source,chat,last_msg_id,summary,updated_ts) VALUES(?,?,?,?,?)",
                  (source, chat, max(r["id"] for r in rows), summary,
                   int(time.time()) if (changed or summary != old_summary) else (st["updated_ts"] if st else int(time.time()))))
    return changed or summary != old_summary


IMMEDIATE_RE = re.compile(r"立刻|马上|立即|即刻|速来|速到|赶紧|赶快|现在就|现在到|紧急(?:开会|集合|会议)")
IMMEDIATE_HOURS = 6           # 「立刻 / 马上去开会」这种当下的事，没写具体截止：6 小时后自动算过期


def expire_items(now=None):
    """代码规则：截止已过 1 天的待办、3 天没更新的通知、6 小时前的「立刻/马上」类即时事项自动关闭（不花模型调用）。"""
    now = now or time.time()
    nd = datetime.fromtimestamp(now, TZ)
    n = 0
    with db() as c:
        for r in c.execute("SELECT id, title, detail, due, quote, first_ts FROM items WHERE status='open' AND pinned=0 AND first_ts<?",
                           (int(now) - IMMEDIATE_HOURS * 3600,)).fetchall():
            d = parse_due(r["due"], nd) if r["due"] and not IMMEDIATE_RE.search(r["due"]) else None  # 「10月09日 马上」不算具体截止
            if d and d > nd - timedelta(hours=IMMEDIATE_HOURS):
                continue  # 有具体截止且还没过多久：按截止算
            if IMMEDIATE_RE.search(" ".join(str(r[k] or "") for k in ("title", "detail", "due", "quote"))) and (not d or d < nd):
                c.execute("UPDATE items SET status='expired', updated_ts=? WHERE id=?", (int(now), r["id"])); n += 1
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
    stats = {"new": 0, "sent": 0, "calls": 0, "chats": 0, "changed": 0, "secs": 0.0, "errors": 0, "skipped": 0}
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
        skip = not in_digest(k[0], k[1], s)
        if skip or all(classify(r, s) == "drop" for r in rs):
            with db() as c:  # 不看 / 只看@我 / 全是噪音：零调用，只推进水位
                c.execute("INSERT INTO chat_state(source,chat,last_msg_id,updated_ts) VALUES(?,?,?,?) "
                          "ON CONFLICT(source,chat) DO UPDATE SET last_msg_id=excluded.last_msg_id", (*k, max(r["id"] for r in rs), int(time.time())))
            if skip:
                stats["skipped"] += 1
            else:
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
    far = datetime(2100, 1, 1, tzinfo=TZ)
    now = datetime.now(TZ)

    def k(t):
        d = parse_due(t.get("due", ""), now) or far
        return (not t["pinned"], not t["at_me"], chat_mode(t["source"], t["chat"], s) != "focus", d, -(t["first_ts"] or 0))
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
    its = [t for t in its if in_digest(t["source"], t["chat"], s)]
    key = _sort_key(s)
    pub = lambda t: {k: t[k] for k in ("id", "key", "title", "detail", "due", "chat", "sender", "quote", "urgency",
                                       "done", "pinned", "at_me", "source", "first_ts", "msg_ids")}
    todos = [pub(t) for t in sorted((t for t in its if t["kind"] == "todo"), key=key)]
    notices = [pub(t) for t in sorted((t for t in its if t["kind"] == "notice" and t["status"] == "open"
                                       and (t["updated_ts"] or 0) >= since), key=key)][:12]
    cnt = [r for r in cnt if chat_mode(r["source"], r["chat"], s) != "off"]
    nopen = {}
    for t in its:
        if t["status"] == "open":
            nopen[(t["source"], t["chat"])] = nopen.get((t["source"], t["chat"]), 0) + 1
    return {"todos": todos, "notices": notices,
            "groups": [{"chat": r["chat"], "source": r["source"], "gist": r["summary"], "open": nopen.get((r["source"], r["chat"]), 0),
                        "focus": chat_mode(r["source"], r["chat"], s) == "focus"}
                       for r in states if in_digest(r["source"], r["chat"], s)][:40],
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
    out = await llm([{"role": "system", "content": "【头条】你是用户的群消息秘书。根据下面的要点写一句话头条：只能说「待办」或「通知」里最要紧的一件事（群要点只作参考，不要拿群要点里的事当头条）（有截止就带上时间），20 字左右、不超过 28 字，必须是完整的一句话，不要罗列多件事。"
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
    return tidy_headline((out.splitlines() or [""])[0]) or "群里没什么要你管的"


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
    if not weekly:  # 模型可能照着群要点又写回已完成的事：头条只能说未完成事项里的一件
        body["headline"] = fresh_headline(body)
    stats["secs"] = round(time.time() - t0, 2)
    body.update(stats=stats, mode="full" if full else ("weekly" if weekly else "inc"), upto_id=int(kv_get("scan_id", 0) or 0))
    if auto:
        body["auto"] = True
    with db() as c:
        cur = c.execute("INSERT INTO digests(ts,hours,body) VALUES(?,?,?)", (int(time.time()), hours, json.dumps(body, ensure_ascii=False)))
        body["id"] = cur.lastrowid
    return body


# ================= 近实时自动整理（0.32 → 0.34 智能触发）：按群触发，整理完局部更新首页 =================
# 收到消息立刻唤醒调度器，做一次便宜的判断（不调模型）：
#   要紧消息（@我/@全体、重点群、规则认得的查寝/点名/考试/开会/交作业/缴费+时间、立刻/马上/紧急、重要的人、订阅关键词）
#     → 安静 1 秒或最多 2 秒就单独整理这个群，不受同群最小间隔限制；
#   普通消息 → 安静 5 秒 / 攒满 20 条 / 最多等 15 秒；同一个群两次「普通」整理至少隔 15 秒（从上次开始算）；
#   噪音（纯图片表情贴纸、撤回、系统提示、附和客套、高置信度广告、不看 / 只看@我 的群）→ 不调模型，水位直接前进；
#   正在整理时又来了新消息 → 这次一结束就接着整理（普通消息仍受 15 秒间隔）；
#   兜底：任何一条非噪音消息等了 30 秒，不管间隔、不管排队，排最前面整理（「要紧 / 普通」只决定快慢，绝不决定送不送模型）。
# 成本保护：每次只送这个群上次以来的增量；全局 1 分钟内模型调用达到 AUTO_RPM 次时，普通消息暂缓（要紧 / 30 秒兜底照常）；
# 被 429 限流时全局冷却期间不启动新的整理；每次整理的条数 / 调用次数 / 耗时 / 等待时间记在日志（/api/auto/log）。
AUTO_TICK = 5                 # 后台检查周期（秒）；收到消息时立刻唤醒，不等这 5 秒
AUTO_QUIET = 5                # 普通消息：群里安静 5 秒就整理（一段对话说完再整理，避免一句一调）
AUTO_BURST = 20               # 或攒满 20 条
AUTO_MAX_WAIT = 15            # 持续刷屏也最多等 15 秒
AUTO_FORCE = 30               # 兜底：任何一条非噪音消息等了 30 秒，不管最小间隔，立刻排上（排在最前）
AUTO_RUN_LIMIT = int(os.getenv("AUTO_RUN_LIMIT", "100"))  # 单个群一次自动整理最多跑这么久，超时算失败，不让「正在整理」挂着不动
AUTO_GIVEUP_N = 3             # 同一个群连续失败 3 次、而别的群期间整理成功过（模型是好的，是这几条有问题）：
AUTO_GIVEUP_SECS = 180        # 或待整理消息已等 3 分钟且失败过 2 次：按规则兜底提取待办后跳过，水位前进，待整理清零
AUTO_GIVEUP_HARD = 600        # 硬上限：任何待整理消息最多挂 10 分钟（模型整体故障也一样；余额不足/密钥错这类要你处理的除外）
AUTO_URGENT_QUIET = 1         # 要紧消息：安静 1 秒（同一个人连发的两三句凑一批）
AUTO_URGENT_WAIT = 2          # 最多等 2 秒；加上唤醒延迟约 2–3 秒开始整理
AUTO_MIN_GAP = 15             # 同一个群两次「普通」整理至少隔 15 秒（只管闲聊；要紧消息和 30 秒兜底不受限）
AUTO_PARALLEL = int(os.getenv("AUTO_PARALLEL", "6"))   # 全局最多同时整理 6 个群
AUTO_RPM = int(os.getenv("AUTO_RPM", "30"))            # 全局 1 分钟内模型调用到这个数，普通消息暂缓（防限流）；要紧和兜底不受限
AUTO_BACKOFF0, AUTO_BACKOFF_MAX = 10, 60    # 失败退避 10s → 20s → 40s → 最多 60 秒
AUTO_FAIL_SHOW = 300          # 连续失败 5 分钟才在页面提示
HEAD_GAP = 600                # 头条最多 10 分钟用模型重写一次；其间出现新的要紧事项用规则拼
AUTO = {"running": {}, "last_run": {}, "last_end": {}, "fail": {}, "fatal": None, "ok_ts": 0.0, "done_ts": 0}
AUTO_TASKS: set = set()
AUTO_LOG: collections.deque = collections.deque(maxlen=500)   # 每次自动整理：群、原因、条数、送模型条数、调用次数、耗时、最久等待
_BUMP = [0]


def bump():
    _BUMP[0] += 1


def _rpm_now(now: float) -> int:
    while _CALL_TS and _CALL_TS[0] < now - 60:
        _CALL_TS.popleft()
    return sum(1 for x in _CALL_TS if x <= now)


def auto_blocked(s: dict):
    """余额不足 / 密钥错误这类错误：不重试，直到设置变更（或手动整理成功）。返回挡着的错误或 None。"""
    f = AUTO["fatal"]
    if not f:
        return None
    if f["hash"] != _shash(s) or (LLM_STATE["ok"] and LLM_STATE["ts"] > f["ts"]):
        AUTO["fatal"] = None
        AUTO["fail"].clear()
        return None
    return f


def auto_plan(s: dict, now: float | None = None):
    """返回 (ready, next_due)。ready = [(群, 待整理消息, 原因)]，等太久的排最前；next_due = 最近一个还没到点的群的到期时间。"""
    now = now or time.time()
    if not s.get("auto_on") or auto_blocked(s):
        return [], None
    if LLM_COOL[0] > now:  # 被限流：冷却期间不启动新整理（启动了也只是在 llm() 里干等，白占单次 100 秒上限）
        return [], LLM_COOL[0]
    rows, _ = pending_info(s)
    by = {}
    for r in rows:
        by.setdefault((r["source"], r["chat"]), []).append(r)
    return plan_chats(by, now, AUTO, ARRIVE, lambda r: is_urgent(r, s), _rpm_now(now) >= AUTO_RPM,
                      quiet=AUTO_QUIET, max_wait=AUTO_MAX_WAIT, burst=AUTO_BURST, urgent_quiet=AUTO_URGENT_QUIET,
                      urgent_wait=AUTO_URGENT_WAIT, min_gap=AUTO_MIN_GAP, force_after=AUTO_FORCE)


def auto_sweep(s: dict):
    """便宜的收尾：只有噪音/不看/只看@我 消息的群直接推进水位（零调用）；全局水位停在最早一条待整理消息前。"""
    with db() as c:
        top = c.execute("SELECT MAX(id) i FROM msgs").fetchone()["i"] or 0
    allrows = [r for r in _scan_rows() if r["id"] <= top]
    pend, _ = pending_info(s)
    pkeys = {(r["source"], r["chat"]) for r in pend} | set(AUTO["running"])
    adv = {}
    for r in allrows:
        k = (r["source"], r["chat"])
        if k not in pkeys:
            adv[k] = max(adv.get(k, 0), r["id"])
    if adv:
        with db() as c:
            c.executemany("INSERT INTO chat_state(source,chat,last_msg_id,updated_ts) VALUES(?,?,?,?) "
                          "ON CONFLICT(source,chat) DO UPDATE SET last_msg_id=MAX(last_msg_id, excluded.last_msg_id)",
                          [(k[0], k[1], i, int(time.time())) for k, i in adv.items()])
        for r in allrows:
            if (r["source"], r["chat"]) in adv:
                ARRIVE.pop(r["id"], None)
    pids = [r["id"] for r in pend if r["id"] <= top]
    kv_set("scan_id", (min(pids) - 1) if pids else max(top, int(kv_get("scan_id", 0) or 0)))
    if len(ARRIVE) > 20000:  # 兜底：别无限长
        for i in sorted(ARRIVE)[:10000]:
            ARRIVE.pop(i, None)


def _llm_code(ex) -> int:
    h = getattr(ex, "headers", None) or {}
    return _int(h.get("x-llm-code")) or 0


async def auto_run_chat(k, rows, s, now: float | None = None, why: str = ""):
    """只整理一个群；成功后局部刷新首页那一期（不整篇重写）。"""
    now = now or time.time()
    AUTO["running"][k] = now
    AUTO["last_run"][k] = now
    bump()
    stats = {"new": len(rows), "sent": 0, "calls": 0, "chats": 1, "changed": 0, "secs": 0.0, "errors": 0, "skipped": 0}
    t0 = time.time()
    waited = max(0.0, now - min((min(ARRIVE.get(r["id"], r["ts"]), now) for r in rows), default=now))
    ok = False
    try:
        try:
            changed = await asyncio.wait_for(update_chat(k[0], k[1], rows, s, stats), AUTO_RUN_LIMIT)
        except asyncio.TimeoutError:
            raise HTTPException(502, f"大模型 {AUTO_RUN_LIMIT} 秒还没整理完这个群：稍后自动重试", headers={"x-llm-code": "0"})
        AUTO["fail"].pop(k, None)
        AUTO["ok_ts"] = time.time()
        for r in rows:
            ARRIVE.pop(r["id"], None)
        stats["changed"] = int(bool(changed))
        stats["secs"] = round(time.time() - t0, 2)
        if changed:
            await refresh_live(s, stats, now, bg_head=True)
        AUTO["done_ts"] = int(time.time())
        kv_set("checked_ts", AUTO["done_ts"])
        ok = True
        return changed
    except Exception as ex:
        code = _llm_code(ex)
        err = str(getattr(ex, "detail", "") or ex)[:300]
        AUTO["fail"][k] = fail_record(AUTO["fail"].get(k, {}), now, code, err, backoff0=AUTO_BACKOFF0, backoff_max=AUTO_BACKOFF_MAX, wall=time.time())
        n = AUTO["fail"][k]["n"]
        if code in LLM_FATAL_CODES or code == -1:
            AUTO["fatal"] = {"err": err, "short": llm_short(code, err), "hash": _shash(s), "ts": time.time()}
        print(f"自动整理失败 {k[1]}（第 {n} 次）:", err)
        # 模型整理不了的时候，查寝/点名/开会这类「要你到场」的事先按规则变成待办，不等模型
        with contextlib.suppress(Exception):
            if rule_todos(k[0], k[1], rows, s):
                await refresh_live(s, stats, now, model_head=False)
        # 模型是好的（别的群在这个群失败以后成功过），就是这个群这几条一直不行：跳过，水位前进，待整理不再挂着
        oldest = min((min(ARRIVE.get(r["id"], r["ts"]), now) for r in rows), default=now)
        model_ok = LLM_LAST_OK[0] > AUTO["fail"][k]["since"] and code not in LLM_FATAL_CODES and code != -1
        fatal = code in LLM_FATAL_CODES or code == -1
        age = time.time() - oldest
        if should_give_up(n, age, model_ok, fatal, giveup_n=AUTO_GIVEUP_N, giveup_secs=AUTO_GIVEUP_SECS, giveup_hard=AUTO_GIVEUP_HARD):
            give_up_chat(k, rows, err)
        return None
    finally:
        if AUTO["running"].get(k) == now:  # 占位已被超时释放并被新一轮接手时，别把别人的占位清掉
            AUTO["running"].pop(k, None)
        AUTO["last_end"][k] = time.time()
        secs = round(time.time() - t0, 2)
        AUTO_LOG.append({"ts": int(now), "chat": k[1], "source": k[0], "why": why, "n": len(rows), "sent": stats["sent"],
                         "calls": stats["calls"], "secs": secs, "wait": round(waited, 1), "ok": ok})
        print(f"自动整理 {k[1]}（{why or '-'}）：{len(rows)} 条，送模型 {stats['sent']} 条，调用 {stats['calls']} 次，"
              f"用时 {secs} 秒，最早一条等了 {waited:.1f} 秒{'' if ok else '，失败'}")
        bump()
        wake_auto()  # 整理期间又来的消息：马上排下一轮


def auto_log_summary(sec: int = 3600) -> dict:
    return summarize(AUTO_LOG, sec)


def give_up_chat(k, rows, why=""):
    """这个群的这几条消息模型反复整理不了：推进水位（规则兜底已经提取过待办），清掉失败记录，不再算进待整理。"""
    if not rows:
        return
    top = max(r["id"] for r in rows)
    with db() as c:
        c.execute("INSERT INTO chat_state(source,chat,last_msg_id,updated_ts) VALUES(?,?,?,?) "
                  "ON CONFLICT(source,chat) DO UPDATE SET last_msg_id=MAX(last_msg_id, excluded.last_msg_id)", (k[0], k[1], top, int(time.time())))
    for r in rows:
        ARRIVE.pop(r["id"], None)
    AUTO["fail"].pop(k, None)
    kv_add("skipped_msgs", len(rows))
    print(f"自动整理跳过 {k[1]} 的 {len(rows)} 条（反复失败：{why[:80]}）")


_live_lock = asyncio.Lock()
async def refresh_live(s: dict | None = None, stats: dict | None = None, now: float | None = None, model_head: bool = True,
                       bg_head: bool = False):
    """某个群整理完：把首页那一期按事项表重新拼一遍（不调模型），原地更新；
    只有出现新的要紧事项（48 小时内截止 / @我）时才重写头条：先用规则拼一句马上写进去（不让头条拖慢事项上首页），
    模型头条每 10 分钟最多一次，写好后再换上。"""
    s = s or settings()
    now = now or time.time()
    async with _live_lock:
        with db() as c:
            latest = c.execute("SELECT * FROM digests ORDER BY id DESC LIMIT 1").fetchone()
        prev = json.loads(latest["body"]) if latest else {}
        hours = latest["hours"] if latest and latest["hours"] in (24, 72) else 24
        body = build_body(hours, s)
        new, head, opens, want_model = live_head_plan(prev, body, bool(latest), kv_get("head_ts", 0), now, HEAD_GAP, model_head)
        if want_model:
            kv_set("head_ts", int(now))
        body["headline"] = valid_headline(head, opens)  # 旧头条说的事已勾完成/过期：这里就换掉，不等首页来修
        today = datetime.fromtimestamp(now, TZ).strftime("%Y-%m-%d")
        body.update(stats=stats or {}, mode="live", auto=True, live=True, live_day=today, upto_id=int(kv_get("scan_id", 0) or 0))
        with db() as c:
            if latest and prev.get("live") and latest["hours"] == hours and prev.get("live_day") == today:
                c.execute("UPDATE digests SET ts=?, body=? WHERE id=?", (int(time.time()), json.dumps(body, ensure_ascii=False), latest["id"]))
                body["id"] = latest["id"]
            else:  # 定时群报 / 手动整理之后、或跨天：开新的一期，之后原地更新
                body["id"] = c.execute("INSERT INTO digests(ts,hours,body) VALUES(?,?,?)",
                                       (int(time.time()), hours, json.dumps(body, ensure_ascii=False))).lastrowid
    newtodo = [t for t in new if t in body["todos"]]
    if newtodo and s.get("push_digest"):  # 免打扰时 push 会攒着，到点合并成一条
        await push_new_todos(newtodo, s)
    if want_model and bg_head:  # 自动整理：模型头条放后台，不占这个群的「正在整理」时间
        t = asyncio.create_task(_model_head(body, s, stats))
        AUTO_TASKS.add(t)
        t.add_done_callback(AUTO_TASKS.discard)
    elif want_model:
        await _model_head(body, s, stats)
    return body


async def _model_head(body, s, stats):
    try:
        mh = await make_headline(body, s, stats=stats)
        async with _live_lock:
            with db() as c:
                r = c.execute("SELECT body FROM digests WHERE id=?", (body["id"],)).fetchone()
                if r:
                    cur = json.loads(r["body"])
                    mh = fresh_headline({**cur, "headline": mh})  # 模型写好时可能已经有事被勾完成：按当前状态校验
                    cur["headline"] = mh
                    c.execute("UPDATE digests SET ts=?, body=? WHERE id=?", (int(time.time()), json.dumps(cur, ensure_ascii=False), body["id"]))
                    body["headline"] = mh
    except Exception as ex:
        print("模型头条失败，保留规则头条:", ex)


async def auto_tick(s: dict | None = None, now: float | None = None) -> float | None:
    """一次调度：收尾水位 + 启动到点的群（不超过并发上限）。返回下一次该醒的时间。"""
    s = s or settings()
    stale = (now or time.time()) - (AUTO_RUN_LIMIT * 2 + 60)
    for k in [k for k, t0 in AUTO["running"].items() if t0 < stale]:  # 任务异常没清占位：释放，别让这个群永远卡在「整理中」
        AUTO["running"].pop(k, None)
        print("自动整理占位超时已释放:", k[1])
    auto_sweep(s)
    ready, nxt = auto_plan(s, now)
    room = AUTO_PARALLEL - len(AUTO["running"])
    for k, rs, why in ready[:max(0, room)]:
        AUTO["running"][k] = now or time.time()  # 先占位，避免下一轮重复启动
        t = asyncio.create_task(auto_run_chat(k, rs, s, now, why))
        AUTO_TASKS.add(t)
        t.add_done_callback(AUTO_TASKS.discard)
    if len(ready) > room:
        nxt = time.time() + 1
    return nxt


def auto_status(s: dict | None = None) -> dict:
    s = s or settings()
    if not s.get("auto_on"):
        return {"state": "off"}
    f = auto_blocked(s)
    if f:
        return {"state": "failed", "reason": f["short"], "err": f["err"], "fatal": True}
    if AUTO["running"]:
        return {"state": "running", "n": len(AUTO["running"]), "chats": [k[1] for k in AUTO["running"]][:6]}
    fails = list(AUTO["fail"].values())
    if fails:
        last = max(fails, key=lambda v: v["ts"])
        # 偶发失败（限流、超时）后台自己退避重试，用户无感；连续失败超过 5 分钟、期间没有任何群整理成功才提示
        since = min(v.get("since", v["ts"]) for v in fails)
        if last["ts"] > AUTO["ok_ts"] and time.time() - since >= AUTO_FAIL_SHOW:
            return {"state": "failed", "reason": llm_short(last["code"], last["err"]), "err": last["err"], "fatal": False,
                    "retry_in": max(0, int(min(v["until"] for v in fails) - time.time())), "n": len(fails)}
    return {"state": "idle"}


def data_rev(s: dict | None = None) -> str:
    """首页/群页数据的轻量版本号：事项、群要点、群报、@我、完成/置顶、设置任一变化就变。"""
    s = s or settings()
    with db() as c:
        a = tuple(c.execute("SELECT COUNT(*), COALESCE(MAX(updated_ts),0), COALESCE(SUM(pinned),0), "
                            "COALESCE(SUM((CASE status WHEN 'open' THEN 1 WHEN 'done' THEN 2 ELSE 3 END) * (id % 9973)),0) FROM items").fetchone())
        b = tuple(c.execute("SELECT COALESCE(MAX(updated_ts),0), COUNT(*) FROM chat_state").fetchone())
        d = c.execute("SELECT id, ts FROM digests ORDER BY id DESC LIMIT 1").fetchone()
        m = c.execute("SELECT COALESCE(MAX(id),0) FROM msgs WHERE at_me=1").fetchone()[0]
        x = (c.execute("SELECT COUNT(*) FROM todo_done").fetchone()[0], c.execute("SELECT COUNT(*) FROM pins").fetchone()[0])
    raw = repr((a, b, tuple(d) if d else None, m, x, _shash(s)))
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def updated_ts():
    with db() as c:
        d = c.execute("SELECT ts FROM digests ORDER BY id DESC LIMIT 1").fetchone()
    return max(d["ts"] if d else 0, int(kv_get("checked_ts", 0) or 0)) or None


async def auto_loop():
    ev = asyncio.Event()
    _WAKE.append(ev)
    while True:
        nxt = None
        try:
            nxt = await auto_tick()
        except Exception as ex:
            print("自动整理调度出错:", ex)
        wait = AUTO_TICK if nxt is None else min(AUTO_TICK, max(0.3, nxt - time.time() + 0.05))
        try:
            await asyncio.wait_for(ev.wait(), wait)
            await asyncio.sleep(0.3)  # 收到消息被唤醒：稍等一下，让同一批消息一起落库
        except asyncio.TimeoutError:
            pass
        ev.clear()


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
            t = {"title": it["title"], "chat": it["chat"], "id": it["id"]}
        else:
            t0, ch0 = split_key(r["k"])
            t = {"title": r["title"] or t0, "chat": r["chat"] or ch0, "due": r["due"] or ""}
            if find_match(t, done):
                continue
        await push("提醒：" + t["title"], t["chat"], force=True, level="timeSensitive", url=f"/#todo/{t['id']}" if t.get("id") else "")
        sent += 1
    return sent


async def check_reminders():
    await check_snoozed()
    s = settings()
    n = int(s.get("remind_hours") or 0)
    if n <= 0 or not has_push(s):
        return 0
    now = datetime.now(TZ)
    sent = 0

    async def fire(t):
        due = parse_due(t.get("due", ""), now)
        if not due or not (remind_start(due, n, s) <= now <= due):
            return False
        left = int((due - now).total_seconds() // 60)
        when = f"{left // 60} 小时 {left % 60} 分钟" if left >= 60 else f"{left} 分钟"
        await push(f"还剩 {when}：{t.get('title', '')}", f"截止 {t.get('due')} · {t.get('chat', '')}", force=True, level="timeSensitive",
                   url=f"/#todo/{t['id']}" if t.get("id") else "", tag=f"due-{t.get('id') or todo_key(t)}")
        return True
    with db() as c:  # 新：挂在事项 id 上，每件事只提醒一次，完成的不提醒
        its = c.execute("SELECT * FROM items WHERE kind='todo' AND status='open' AND reminded=0 AND due!=''").fetchall()
    for it in its:
        if in_digest(it["source"], it["chat"], s) and await fire(dict(it)):
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


async def scheduler():
    async def loop():
        last, cleaned, qq_cleaned = set(), None, 0.0
        if kv_get("rule_backfill") != VERSION:  # 升级后补一遍最近几小时漏掉的查寝/点名/开会待办（每个版本一次）
            kv_set("rule_backfill", VERSION)
            try:
                if rule_backfill():
                    await refresh_live(settings(), model_head=False)
            except Exception as ex:
                print("规则补建待办失败:", ex)
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
                        n = len([t for t in d.get("todos", []) if not t.get("done")])
                        await push(("群报 · " + (f"{n} 件待办" if n else "没什么要你管的")), d.get("headline", ""), force=True, web=False)
                except Exception as ex:
                    print("自动总结失败:", ex)
            if (s.get("weekly_digest") and now.weekday() == 6 and now.hour == 20
                    and ("wk", now.date()) not in last):
                last = last | {("wk", now.date())}
                ran = True
                try:
                    d = await make_digest(168)
                    if s.get("push_digest"):
                        await push(weekly_title(d), f"{d.get('count', 0)} 条消息 · {d.get('chats', 0)} 个群。" + d.get("headline", ""), force=True, web=False)
                except Exception as ex:
                    print("周报失败:", ex)
            if not ran and s.get("auto_on"):
                try:  # 截止过了 / 通知过期：代码规则关闭（不调模型），首页跟着更新
                    if expire_items():
                        await refresh_live(s)
                except Exception as ex:
                    print("过期事项整理失败:", ex)
            try:
                await flush_held()
                await check_reminders()
            except Exception as ex:
                print("截止提醒失败:", ex)
            if time.time() - qq_cleaned > 6 * 3600:  # 每 6 小时清一次 QQ 媒体缓存（启动后先清一次）
                qq_cleaned = time.time()
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(clean_qq_cache)
            if now.hour == 4 and cleaned != now.date():  # 每天凌晨清理过期消息（不依赖正好 4:00 这一分钟醒着）
                cleaned = now.date()
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(prune_db)
            await asyncio.sleep(60)
    return asyncio.gather(loop(), auto_loop())


# 保留期表：每天凌晨统一清理，新增要清的表只改这里。(表, 时间列, 保留天数, 额外条件)
RETENTION = [
    ("sessions", "exp", 0, ""),                  # 已过期的登录会话
    ("pushed", "ts", 30, ""),                    # 推送去重记录
    ("llm_log", "ts", 3, ""),                    # 大模型调用统计
    ("digests", "ts", 90, ""),                   # 历史整理
    ("reminded", "ts", 60, ""),                  # 提醒记录
    ("todo_done", "ts", 90, ""),                 # 勾掉记录
    ("snooze", "until", 30, ""),                 # 早已触发的稍后提醒
    ("items", "updated_ts", 90, "status!='open' AND pinned=0"),  # 已完成/过期的旧事项
]


def prune_db(now=None):
    """每天一次：按保留期表清旧记录和过期消息（keep_days 可设置），再刷新索引统计并截断 WAL。"""
    now = int(now or time.time())
    keep = max(1, int(settings().get("keep_days") or KEEP_DAYS))
    with db() as c:
        for table, col, days, cond in RETENTION:
            c.execute(f"DELETE FROM {table} WHERE {col}<?" + (f" AND {cond}" if cond else ""), (now - days * 86400,))
        c.execute("DELETE FROM msgs WHERE ts<?", (now - keep * 86400,))
    with contextlib.suppress(Exception):
        c = db(); c.execute("PRAGMA optimize"); c.execute("PRAGMA wal_checkpoint(TRUNCATE)"); c.close()


# ---------------- 清 NapCat 里 QQ 的媒体缓存 ----------------
# 群报显示图片直接从 QQ 服务器加载（过期换 rkey），不用 QQ 本地缓存；这些缓存只占空间。
# 只删 nt_data 下的 Pic / Video / Ptt / Thumb 目录里超过 6 小时的文件，登录数据、数据库一律不碰。
from urgent import is_urgent, shash as _shash, _hooks as _urgent_hooks, URGENT_WORDS, DUE_TASK_RE, DUE_WHEN_RE  # noqa: E402,F401 (0.34.37 拆出)
_urgent_hooks.update(chat_mode=lambda *a: chat_mode(*a), rule_event=lambda r: rule_event(r))
from qqcache import QQ_CACHE_DIRS, clean_qq_cache  # noqa: E402,F401 (0.34.35 拆出)


# ---------------- 网页接口 ----------------
URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&*+=%]+")


def extract_links(rows, limit=20, s=None):
    seen, out = set(), []
    for r in rows:
        if AD_RE.search(r["text"] or "") or (s is not None and "source" in r.keys() and not in_digest(r["source"], r["chat"], s)):
            continue
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


def valid_headline(head, opens, gone=None) -> str:
    """头条校验：规则见 headline.check_headline；这里只负责补上最近 7 天已完成/过期的标题。"""
    if gone is None:
        with db() as c:
            gone = [r["title"] for r in c.execute(
                "SELECT title FROM items WHERE status!='open' AND updated_ts>=? ORDER BY updated_ts DESC LIMIT 300",
                (int(time.time()) - 7 * 86400,))]
    return check_headline(head, opens, gone)


def fresh_headline(body):
    """头条说的那件事已经勾完成 / 过期了，就别再挂在最上面：换成还没做的最要紧的一件（不调模型）。周报不动。"""
    head = body.get("headline") or ""
    if body.get("mode") == "weekly":
        return head
    opens = [t for t in body.get("todos", []) if not t.get("done")] + (body.get("notices") or [])
    gone = [t.get("title", "") for t in body.get("todos", []) if t.get("done")]
    with db() as c:
        gone += [r["title"] for r in c.execute(
            "SELECT title FROM items WHERE status!='open' AND updated_ts>=? ORDER BY updated_ts DESC LIMIT 300",
            (int(time.time()) - 7 * 86400,))]
        # 这一期存的事项可能是旧的：以数据库里事项的当前状态为准（勾完成那一刻 items 已改，这一期还没重拼）
        st = {r["id"]: r["status"] for r in c.execute("SELECT id, status FROM items")}
    opens = [t for t in opens if not t.get("id") or st.get(t["id"], "open") == "open"]
    return valid_headline(head, opens, gone)


def fix_latest_headline():
    """勾完成 / 取消完成之后马上校验最新一期的头条并存回（不等首页轮询、不等下一次整理）。返回现在的头条。"""
    with db() as c:
        d = c.execute("SELECT * FROM digests ORDER BY id DESC LIMIT 1").fetchone()
    if not d:
        return None
    body = json.loads(d["body"])
    with db() as c:
        st = {r["id"]: r["status"] for r in c.execute("SELECT id, status FROM items")}
    for t in body.get("todos", []):  # 这一期存的 done 是旧的：按事项当前状态
        if t.get("id") in st:
            t["done"] = st[t["id"]] == "done"
    body["notices"] = [n for n in body.get("notices") or [] if st.get(n.get("id"), "open") == "open"]
    fixed = fresh_headline(body)
    if fixed != body.get("headline"):
        with db() as c:
            cur = json.loads(c.execute("SELECT body FROM digests WHERE id=?", (d["id"],)).fetchone()["body"])
            cur["headline"] = fixed
            c.execute("UPDATE digests SET body=? WHERE id=?", (json.dumps(cur, ensure_ascii=False), d["id"]))
    return fixed


def view_headline(hours, body):
    """3 天 / 7 天视图的头条：今天已为这个范围写过就沿用，否则用规则从未完成事项里挑一句（不调模型）。"""
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    try:
        c = json.loads(kv_get(f"head_{hours}", "") or "{}")
    except Exception:
        c = {}
    if c.get("day") == today and c.get("h"):
        return c["h"]
    opens = [t for t in body.get("todos", []) if not t.get("done")] + body.get("notices", [])
    return rule_headline(opens) or "这段时间群里没什么要你管的"


@app.get("/api/digest.md", dependencies=[Depends(auth)])
def digest_md(hours: int = 24):
    """把当前群报导出成 Markdown 文字（待办 / 通知 / 各群要点），方便分享或存进备忘录。只含已整理的事项，不含原文。"""
    b = build_body(hours if hours in (24, 72, 168) else 24, settings())
    out = [f"# 群报 {time.strftime('%Y-%m-%d')}", ""]
    opens = [t for t in b["todos"] if not t["done"]]
    dones = [t for t in b["todos"] if t["done"]]
    out.append(f"## 待办（{len(opens)}）")
    out += [f"- [ ] {t['title']}（{t['chat']}{'，' + t['due'] if t['due'] else ''}）" for t in opens] or ["- 暂无"]
    if dones:
        out += ["", f"## 已完成（{len(dones)}）"] + [f"- [x] {t['title']}（{t['chat']}）" for t in dones]
    if b["notices"]:
        out += ["", "## 通知"] + [f"- {n['title']}（{n['chat']}）" for n in b["notices"]]
    if b["groups"]:
        out += ["", "## 各群要点"] + [f"- {g['chat']}：{g['gist']}" for g in b["groups"]]
    return PlainTextResponse("\n".join(out) + "\n", media_type="text/markdown; charset=utf-8")


@app.get("/api/state", dependencies=[Depends(auth)])
def state(id: int | None = None, hours: int | None = None):
    now = int(time.time())
    with db() as c:
        d = (c.execute("SELECT * FROM digests WHERE id=?", (id,)).fetchone() if id else
             c.execute("SELECT * FROM digests ORDER BY id DESC LIMIT 1").fetchone())
        # 切「今天/3天/7天」只是换个时间范围看已经整理好的事项：现拼，不调模型、不重新整理、不新建一期
        H = hours if (hours in (24, 72, 168) and not id and d) else (d["hours"] if d else 24)
        span = H * 3600
        latest_id = c.execute("SELECT MAX(id) i FROM digests").fetchone()["i"]
        pv = c.execute("SELECT body FROM digests WHERE id<? ORDER BY id DESC LIMIT 1", (d["id"],)).fetchone() if d else None
        ref = now if (not d or d["id"] == latest_id) else d["ts"]
        ats = c.execute("SELECT * FROM msgs WHERE at_me=1 AND ts>=? AND ts<=? ORDER BY ts DESC LIMIT 30",
                        (ref - span, ref + 3600 * 24)).fetchall()
        per = c.execute("SELECT source, chat, COUNT(*) n FROM msgs WHERE ts>=? AND ts<=? GROUP BY source, chat",
                        (ref - span, ref)).fetchall()
        link_rows = c.execute("SELECT ts, source, chat, sender, text FROM msgs WHERE text LIKE '%http%' AND ts>=? AND ts<=? ORDER BY ts DESC LIMIT 300",
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
    if d and H != d["hours"]:
        vb = build_body(H, settings())
        vb["headline"] = view_headline(H, vb) or json.loads(d["body"]).get("headline", "")
        vb.update(id=d["id"], view=True)
        body = annotate_todos(vb)
    else:
        body = annotate_todos(digest_row(d)) if d else None
    if body and body.get("headline") and (not d or d["id"] == latest_id):
        fixed = fresh_headline(body)
        if fixed != body["headline"]:
            body["headline"] = fixed
            if not body.get("view"):  # 存回去：头条说的事已经勾完成/过期了，换成还没做的最要紧的一件
                with contextlib.suppress(Exception), db() as c:
                    cur = json.loads(c.execute("SELECT body FROM digests WHERE id=?", (d["id"],)).fetchone()["body"])
                    cur["headline"] = fixed
                    c.execute("UPDATE digests SET body=? WHERE id=?", (json.dumps(cur, ensure_ascii=False), d["id"]))
    # done / pins：存着的原始 key + 当前这期里被模糊匹配上的 key（前端两者都认）
    with db() as c:
        done = [r["k"] for r in c.execute("SELECT k FROM todo_done ORDER BY ts DESC LIMIT 500")]
        pins = [r["k"] for r in c.execute("SELECT k FROM pins")]
    done += [t["key"] for t in (body or {}).get("todos", []) if t.get("done") and t["key"] not in done]
    pins += [t["key"] for t in (body or {}).get("todos", []) if t.get("pinned") and t["key"] not in pins]
    s = settings()
    _, pend = pending_info(s)
    ats = [r for r in ats if chat_mode(r["source"], r["chat"], s) != "off"]
    if body:  # 刚改了档位：「不看/只看@我」的群立刻从首页拿掉，不用等下次整理
        sf = lambda x: in_digest(x.get("source") or srcs.get(x.get("chat"), "QQ"), x.get("chat") or "", s)
        for k in ("todos", "notices", "groups"):
            body[k] = [x for x in body.get(k) or [] if sf(x)]
    nowdt = datetime.now(TZ)
    for t in (body or {}).get("todos", []):
        dd = parse_due(t.get("due", ""), nowdt)
        t["due_ts"] = int(dd.timestamp()) if dd else None
    nchats = len({(r["source"], r["chat"]) for r in per})
    return {
        "digest": body,
        "auto_on": bool(s.get("auto_on")),
        "auto": auto_status(s),
        "rev": data_rev(s),
        "updated_ts": updated_ts(),
        "pending": pend,
        "checked_ts": int(kv_get("checked_ts", 0) or 0) or None,
        "llm_calls": int(kv_get("llm_calls", 0) or 0),
        "llm_tokens": int(kv_get("llm_tokens", 0) or 0),
        "llm_hour": llm_calls_since(3600),
        "diff": todo_diff(digest_row(d), json.loads(pv["body"])) if d and pv else None,
        "day_start": int(datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()),
        "digest_ts": d["ts"] if d else None,
        "hours": H,
        "is_latest": (not d) or d["id"] == latest,
        "per_chat": {r["chat"]: r["n"] for r in per},
        "per_src": {ckey(r["source"], r["chat"]): r["n"] for r in per},
        "modes_tip": (not s.get("modes_tip_done")) and nchats >= 8,
        "nchats": nchats,
        "chat_src": srcs,
        "at_me": at_me_view(ats, body),
        "today": today,
        "links": extract_links(link_rows, s=s),
        "done": done,
        "pins": pins,
        "chat_pins": settings().get("chat_pins") or {},
        "status": {"last_msg": last, "heartbeat": hb_ts, "online": qq_on or wx_on,
                   "qq": {"online": qq_on, "seen": hb_ts or last_qq, "last": last_qq},
                   "wx": {"online": wx_on, "seen": wx_ts, "last": last_wx, "ready": bool(INGEST_TOKEN)},
                   "llm": bool(LLM_KEY), "llm_err": llm_err_shown(),
                   "llm_err_ts": LLM_STATE["ts"], "version": VERSION},
    }


@app.get("/api/rev", dependencies=[Depends(auth)])
def rev():
    """前端每 8 秒轮询的极轻接口：数据版本号 + 自动整理状态 + 待整理条数 + 最新消息 id（群页追加新消息用）。"""
    s = settings()
    _, pend = pending_info(s)
    with db() as c:
        mrev = c.execute("SELECT COALESCE(MAX(id),0) FROM msgs").fetchone()[0]
    return {"rev": data_rev(s), "auto": auto_status(s), "pending": pend, "updated_ts": updated_ts(), "mrev": mrev}


@app.get("/api/auto/log", dependencies=[Depends(auth)])
def auto_log(limit: int = 100):
    """自动整理的真实成本：最近每次整理的群 / 原因 / 条数 / 送模型条数 / 调用次数 / 耗时 / 等待，及最近 1 小时、24 小时汇总。"""
    return {"recent": list(AUTO_LOG)[-max(1, min(limit, 500)):][::-1], "hour": auto_log_summary(3600), "day": auto_log_summary(86400),
            "params": {"quiet": AUTO_QUIET, "burst": AUTO_BURST, "max_wait": AUTO_MAX_WAIT, "force": AUTO_FORCE, "urgent_quiet": AUTO_URGENT_QUIET,
                       "urgent_wait": AUTO_URGENT_WAIT, "min_gap": AUTO_MIN_GAP, "parallel": AUTO_PARALLEL, "rpm": AUTO_RPM},
            "llm_hour": llm_calls_since(3600)}


@app.post("/api/digest", dependencies=[Depends(auth)])
async def digest_now(req: Request):
    d = await req.json()
    hours = int(d.get("hours", 24))
    return await make_digest(max(1, min(hours, 168)), full=bool(d.get("full")))


@app.get("/api/digests", dependencies=[Depends(auth)])
def digests(limit: int = 30):
    with db() as c:
        rows = c.execute("SELECT * FROM digests ORDER BY ts DESC, id DESC LIMIT ?", (max(limit, 1) * 40,)).fetchall()
    out, seen = [], set()
    for r in rows:
        # 往期每天只留一期（当天最新那期；周报单独一期）：旧版每半小时整理一次会存一堆几乎一样的
        k = (datetime.fromtimestamp(r["ts"], TZ).strftime("%Y-%m-%d"), r["hours"] >= 168)
        if k in seen:
            continue
        seen.add(k)
        if len(out) >= limit:
            break
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
        head = None
        if "pin" not in d:
            with contextlib.suppress(Exception):
                head = fix_latest_headline()
        return {"ok": True, "id": it["id"], "headline": head}
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


def build_ics(title: str, due: str, detail: str = "", chat: str = "") -> str:
    now = datetime.now(TZ)
    return ics_build(title, due, parse_due(due, now), now, detail, chat)


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
            hours[datetime.fromtimestamp(r["ts"], TZ).hour] += 1
            groups[r["chat"]] = groups.get(r["chat"], 0) + 1
    top = sorted(groups.items(), key=lambda x: -x[1])[:5]
    return {"days": days, "hours": hours, "total": sum(hours), "top": [{"chat": k, "n": v} for k, v in top]}


@app.get("/api/activity", dependencies=[Depends(auth)])
def activity(days: int = 7, source: str = ""):
    return activity_stats(max(1, min(days, 90)), source)


def set_chat_pin(source, chat, on):
    s = settings()
    pins = dict(s.get("chat_pins") or {})
    if on:
        pins[ckey(source, chat)] = time.time()
    else:
        pins.pop(ckey(source, chat), None)
    s["chat_pins"] = pins
    with db() as c:
        c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('settings',?)", (json.dumps(s, ensure_ascii=False),))


@app.post("/api/chat_pin", dependencies=[Depends(auth)])
async def chat_pin_api(req: Request):
    """置顶 / 取消置顶一个群：{"source":"QQ","chat":"群名","pin":true}"""
    d = await req.json()
    if not d.get("chat"):
        raise HTTPException(400, "没有选群")
    set_chat_pin(str(d.get("source") or "QQ"), str(d["chat"]), bool(d.get("pin")))
    return {"ok": True, "pin": bool(d.get("pin"))}


@app.get("/api/chats", dependencies=[Depends(auth)])
def chats(hours: int = 168, source: str = ""):
    s = settings()
    day0 = int(datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    with db() as c:
        today = {(r["source"], r["chat"]): r["n"] for r in c.execute(
            "SELECT source, chat, COUNT(*) n FROM msgs WHERE ts>=? GROUP BY source, chat", (day0,))}
        rows = c.execute("""SELECT chat, source, COUNT(*) n, MAX(ts) last_ts, SUM(at_me) ats FROM msgs
                            WHERE ts>=? AND (?='' OR source=?) GROUP BY chat, source ORDER BY last_ts DESC""",
                         (int(time.time()) - hours * 3600, source, source)).fetchall()
        nopen = {(r["source"], r["chat"]): r["n"] for r in c.execute(
            "SELECT source, chat, COUNT(*) n FROM items WHERE status='open' AND kind='todo' GROUP BY source, chat")}
        out = []
        for r in rows:
            m = c.execute("SELECT sender, text FROM msgs WHERE chat=? AND source=? ORDER BY id DESC LIMIT 1",
                          (r["chat"], r["source"])).fetchone()
            at_ids = [x["id"] for x in c.execute(
                "SELECT id FROM msgs WHERE chat=? AND source=? AND at_me=1 AND ts>=? ORDER BY id DESC LIMIT 50",
                (r["chat"], r["source"], int(time.time()) - hours * 3600))]
            out.append({"chat": r["chat"], "source": r["source"], "n": r["n"], "last_ts": r["last_ts"],
                        "ats": r["ats"] or 0, "at_ids": at_ids, "last": f"{m['sender']}：{m['text']}" if m else "",
                        "open": nopen.get((r["source"], r["chat"]), 0),
                        "pin": (s.get("chat_pins") or {}).get(ckey(r["source"], r["chat"]), 0),
                        "mode": chat_mode(r["source"], r["chat"], s), "today": today.get((r["source"], r["chat"]), 0),
                        "muted": chat_mode(r["source"], r["chat"], s) == "off"})
    return out


@app.get("/api/messages", dependencies=[Depends(auth)])
def messages(chat: str = "", q: str = "", before: int = 0, limit: int = 60, source: str = "",
             sender: str = "", since: int = 0, until: int = 0, after: int = 0, around: int = 0):
    if around and chat:  # 跳到某条消息：取它前后各一段上下文
        older = messages(chat=chat, source=source, before=around + 1, limit=min(limit, 100) // 2 + 1)
        newer = messages(chat=chat, source=source, after=around, limit=min(limit, 100) // 2)
        return sorted(older + newer, key=lambda m: (m["ts"], m["id"]))
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
    if after:  # 群聊轮询：只要比这条新的
        sql += " AND id>?"; args.append(after)
    sql += (" ORDER BY id ASC LIMIT ?" if after else " ORDER BY id DESC LIMIT ?"); args.append(min(limit, 200))
    with db() as c:
        rows = c.execute(sql, args).fetchall()
    out = [{"id": r["id"], "ts": r["ts"], "chat": r["chat"], "sender": r["sender"], "text": r["text"],
            "source": r["source"], "at_me": bool(r["at_me"]),
            "imgs": r["img"].split() if r["img"] else []} for r in rows]
    if not (q or sender or since or until):  # 群聊页按发送时间排（迟到的消息放回它该在的位置）；翻页游标仍按收到顺序（id）
        out.sort(key=lambda m: (m["ts"], m["id"]))
    elif not after:
        out.reverse()
    return out


@app.post("/api/chat_mode", dependencies=[Depends(auth)])
async def chat_mode_api(req: Request):
    """设某个（或一批）群怎么盯：{"chats":[{"source":"QQ","chat":"群名"}], "mode":"atonly"}"""
    d = await req.json()
    mode = d.get("mode")
    if mode not in MODES:
        raise HTTPException(400, "档位只能是 focus / normal / atonly / off")
    pairs = [(str(x.get("source") or "QQ"), str(x.get("chat") or "")) for x in (d.get("chats") or []) if x.get("chat")]
    if not pairs:
        raise HTTPException(400, "没有选群")
    set_modes(pairs, mode)
    return {"ok": True, "n": len(pairs), "mode": mode, "name": MODE_NAME[mode]}


def suggest_quiet(s=None, days: int = 3):
    """很吵但没要紧事的群：近几天消息多、从没产生过待办/通知，或者一大半是线报广告/表情水。只建议，不自动改。"""
    s = s or settings()
    since = int(time.time()) - days * 86400
    with db() as c:
        rows = c.execute("SELECT * FROM msgs WHERE ts>=?", (since,)).fetchall()
        has_items = {(r["source"], r["chat"]) for r in c.execute("SELECT DISTINCT source, chat FROM items")}
    by = {}
    for r in rows:
        by.setdefault((r["source"], r["chat"]), []).append(r)
    out = []
    for (src, chat), rs in by.items():
        if chat_mode(src, chat, s) not in ("normal", "focus") or chat.startswith("私聊") or len(rs) < 15:
            continue
        if any(r["at_me"] for r in rs):  # 有人 @ 过我的群不建议静音，免得漏掉点名
            continue
        ad = sum(1 for r in rs if AD_RE.search(r["text"] or "")) / len(rs)
        noise = sum(1 for r in rs if not r["at_me"] and classify(r, {**s, "modes": {}, "default_mode": "normal"}) == "drop") / len(rs)
        if ad >= 0.25:
            why = "多是线报/广告"
        elif noise >= 0.6:
            why = "多是表情和水聊"
        elif (src, chat) not in has_items and len(rs) >= 30:
            why = "消息多但没整理出过事"
        else:
            continue
        out.append({"source": src, "chat": chat, "n": len(rs), "why": why})
    return sorted(out, key=lambda x: -x["n"])


@app.get("/api/chat_suggest", dependencies=[Depends(auth)])
def chat_suggest():
    return suggest_quiet()


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


@app.get("/api/push/key", dependencies=[Depends(auth)])
def push_key(endpoint: str = ""):
    """网页通知的公钥（浏览器订阅时用），以及这台设备的订阅服务器上还在不在。"""
    _, pub = vapid_keys()
    known = False
    if endpoint:
        with db() as c:
            known = c.execute("SELECT 1 FROM push_subs WHERE endpoint=?", (endpoint,)).fetchone() is not None
    return {"key": pub, "subs": push_sub_count(), "known": known}


MAX_SUBS = 20


@app.post("/api/push/sub", dependencies=[Depends(auth)])
async def push_sub(req: Request):
    """存一台设备的订阅（PushSubscription.toJSON()）；同一 endpoint 覆盖。带 remove:true 等同删除。"""
    d = await req.json()
    sub = d.get("sub") or d
    ep = str(sub.get("endpoint") or "")
    if d.get("remove"):
        return push_unsub_ep(ep)
    keys = sub.get("keys") or {}
    if not ep.startswith("https://") or len(ep) > 1000 or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(400, "订阅信息不完整")
    now = int(time.time())
    with db() as c:
        c.execute("INSERT OR REPLACE INTO push_subs(endpoint,p256dh,auth,ua,ts,ok_ts,fails) VALUES(?,?,?,?,?,0,0)",
                  (ep, str(keys["p256dh"])[:200], str(keys["auth"])[:100], (req.headers.get("user-agent") or "")[:200], now))
        extra = c.execute("SELECT endpoint FROM push_subs ORDER BY ts DESC LIMIT -1 OFFSET ?", (MAX_SUBS,)).fetchall()
        for r in extra:
            c.execute("DELETE FROM push_subs WHERE endpoint=?", (r["endpoint"],))
    return {"ok": True, "subs": push_sub_count()}


def push_unsub_ep(ep: str):
    with db() as c:
        n = c.execute("DELETE FROM push_subs WHERE endpoint=?", (ep,)).rowcount
    return {"ok": True, "removed": n, "subs": push_sub_count()}


@app.delete("/api/push/sub", dependencies=[Depends(auth)])
async def push_unsub(req: Request):
    d = await req.json()
    return push_unsub_ep(str((d.get("sub") or d).get("endpoint") or ""))


@app.post("/api/push/test", dependencies=[Depends(auth)])
async def push_test(req: Request):
    try:
        d = await req.json()
    except Exception:
        d = {}
    if (d or {}).get("channel") == "web":
        ep = str(d.get("endpoint") or "")
        n = await webpush_all({"title": "群报", "body": "通知开好了。之后有新待办、有人 @你，会直接提醒你。", "url": "/",
                               "tag": "test", "badge": open_todo_count()}, endpoint=ep)
        if not n:
            raise HTTPException(502, "没发出去：这台设备的订阅失效了，关掉再开一次通知试试" if ep else "还没有设备开启通知")
        return {"ok": True, "sent": n}
    if not settings().get("bark_url"):
        raise HTTPException(400, "先填 Bark 推送地址")
    ok = await push("群报", "推送通了。之后有人 @你 或说到关键词，会第一时间提醒你。", force=True, test=True, web=False)
    if not ok:
        raise HTTPException(502, "推送失败，检查 Bark 地址是否正确")
    return {"ok": True}


ASK_LINES = int(os.getenv("ASK_LINES", "220"))      # 一次问答最多送多少条原文
ASK_CHARS = int(os.getenv("ASK_CHARS", "16000"))
from askfmt import CITE_RE, _q_terms, cite, tidy_bullets  # noqa: E402,F401 (0.34.41 拆出)


def ask_context(q: str, s: dict, chat: str = "", source: str = "", since_id: int = 0, hours: int = 72, prior: str = ""):
    """问答检索：限定群（任何档位都能问）或全局（排除「不看」的群）；先去噪，消息太多时按关键词 + 重点 + 最新挑一部分。"""
    since = int(time.time()) - hours * 3600
    sql, args = "SELECT * FROM msgs WHERE ts>=?", [since]
    if chat:
        sql += " AND chat=?"; args.append(chat)
        if source:
            sql += " AND source=?"; args.append(source)
    if since_id:
        sql += " AND id>?"; args.append(since_id)
    with db() as c:
        rows = c.execute(sql + " ORDER BY id", args).fetchall()
    s2 = {**s, "modes": {}, "default_mode": "normal"}  # 问答时不按档位过滤，只去噪
    if not chat:
        rows = [r for r in rows if chat_mode(r["source"], r["chat"], s) != "off"]
    cls = {r["id"]: classify(r, s2) for r in rows}
    rows = [r for r in rows if cls[r["id"]] != "drop" or r["at_me"]]
    # 追问（「那后来定了吗」）自己没几个检索词，把上一个问题的词也带上，消息多时才不会漏掉话题
    terms = _q_terms(q)
    terms += [t for t in _q_terms(prior) if t not in terms][:8]
    line = lambda r: (f"#{r['id']} [{datetime.fromtimestamp(r['ts'], TZ):%m-%d %H:%M}]"
                      f"{'' if chat else '[' + r['source'] + '·' + r['chat'] + ']'} {r['sender']}"
                      f"{' (@我)' if r['at_me'] else ''}: {(r['text'] or '')[:300]}")
    if len(rows) > ASK_LINES or sum(len(r["text"] or "") for r in rows) > ASK_CHARS:
        def score(r):
            t = (r["text"] or "").lower()
            return (sum(3 for k in terms if k in t or k in (r["sender"] or "").lower()) + (2 if r["at_me"] else 0)
                    + (1 if cls[r["id"]] == "key" else 0))
        hit = sorted(rows, key=lambda r: (-score(r), -r["id"]))[: ASK_LINES * 2 // 3]
        recent = rows[-(ASK_LINES // 3):]
        keep = {r["id"] for r in hit} | {r["id"] for r in recent}
        rows = [r for r in rows if r["id"] in keep]
    lines, n = [], 0
    for r in reversed(rows):  # 超字数时保留最新的
        ln = line(r)
        if n + len(ln) > ASK_CHARS:
            break
        lines.append(ln); n += len(ln) + 1
    lines.reverse()
    return rows, lines


_ASK_CACHE: dict = {}
ASK_CACHE_TTL = 600


@app.post("/api/ask", dependencies=[Depends(auth)])
async def ask(req: Request):
    """问答。可限定一个群（chat + source；「不看」的群也能问），也可从上次已读位置起（since_id）。
    回答里的引用只能是给定消息的 id，返回 citations 供前端跳到原文。"""
    body = await req.json()
    q = str(body.get("q", "")).strip()
    if not q:
        return {"a": "", "citations": []}
    chat, source = str(body.get("chat") or ""), str(body.get("source") or "")
    since_id = int(body.get("since_id") or 0)
    hist = [{"role": m["role"], "content": str(m["content"])[:2000]} for m in body.get("history", [])[-6:]
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    s = settings()
    prior = " ".join(m["content"] for m in hist if m["role"] == "user")[-200:]
    rows, lines = ask_context(q, s, chat, source, since_id, hours=168 if since_id else 72, prior=prior)
    now = datetime.now(TZ)
    scope = f"「{chat}」这个群（{source or '群聊'}）" if chat else "用户所有的群"
    extra = ""
    if not chat:  # 全局问答先给整理好的事项和各群要点，原文作为依据
        b = build_body(72, s)
        extra = "\n已整理的事项：\n" + "\n".join(f"- {t['title']}（{t['chat']}{'，截止 ' + t['due'] if t['due'] else ''}{'，已完成' if t['done'] else ''}）"
                                            for t in b["todos"][:20] + b["notices"][:10]) or ""
    if not lines:
        return {"a": ("这段时间你没看的部分没有新消息。" if since_id else f"最近{'一周' if since_id else ' 3 天'}{scope}里没有相关消息。"), "citations": []}
    key = (re.sub(r"\s+", "", q), chat, source, since_id, json.dumps(hist, ensure_ascii=False), hash("\n".join(lines)), bool(extra))
    hit = _ASK_CACHE.get(key)
    if hit and time.time() - hit[0] < ASK_CACHE_TTL:  # 同一问题、同样的聊天记录：直接复用，不再花一次模型调用
        return {**hit[1], "reused": True}
    a = await llm([
        {"role": "system", "content": f"【问答】你是用户的群消息秘书。现在是 {now:%Y-%m-%d %H:%M}。{about_me(s)}\n"
                                      f"只根据下面{scope}的聊天记录回答。像朋友帮忙转述一样说人话、口语、简短，不要报告腔，不要「综上所述」「以下是」：先一句话直接回答，需要时再用「- 」列 2–4 个要点，每点不超过 30 字。"
                                      "用户追问（如「那后来定了吗」）时接着上文回答：只答新问的那部分，上文已经说过的不要再讲一遍，没有新信息就一句话说「没有新的进展」。"
                                      "每个要点末尾用 [#消息编号] 标出依据，编号只能用记录里出现过的 #数字，没有依据就不要编。"
                                      "记录里没有就直说没有。可以用 **加粗** 标重点，不要标题和表格。"},
        {"role": "user", "content": f"聊天记录（#数字 是消息编号）：\n" + "\n".join(lines) + extra},
        {"role": "assistant", "content": "好的，我看完了，请问。"}, *hist,
        {"role": "user", "content": q}])
    ids = {int(ln[1:].split(" ", 1)[0]) for ln in lines}
    valid = {r["id"]: r for r in rows if r["id"] in ids}
    text, cites = cite(tidy_bullets(a), valid)
    out = {"a": text, "citations": cites, "scope": {"chat": chat, "source": source}, "used": len(lines)}
    if len(_ASK_CACHE) > 100:
        _ASK_CACHE.clear()
    _ASK_CACHE[key] = (time.time(), out)
    return out


@app.get("/api/chat_brief", dependencies=[Depends(auth)])
async def chat_brief(chat: str, source: str = "", since_id: int = 0, refresh: int = 0):
    """点开群时最上面那段「这个群最近聊了啥」：从上次已读起（没有就最近 24 小时），3–5 条口语要点带引用。
    结果按 (群, 起点, 截至消息) 缓存；没有新消息就不调模型。任何档位的群都能看。"""
    with db() as c:
        q = "SELECT MAX(id) m FROM msgs WHERE chat=?" + (" AND source=?" if source else "")
        maxid = c.execute(q, (chat, source) if source else (chat,)).fetchone()["m"] or 0
        cached = c.execute("SELECT * FROM chat_brief WHERE source=? AND chat=?", (source, chat)).fetchone()
    old = json.loads(cached["body"]) if cached else None
    if since_id and since_id >= maxid:  # 上次看过之后没有新消息：给上一段要点，不调模型
        return {**(old or {"a": "", "citations": []}), "nothing_new": True, "upto_id": maxid, "cached": bool(old)}
    if cached and not refresh and cached["since_id"] == since_id and cached["upto_id"] == maxid:
        return {**old, "cached": True, "upto_id": maxid}
    s = settings()
    rows, lines = ask_context("这个群聊了啥", s, chat, source, since_id, hours=(7 * 24 if since_id else 24))
    if not lines and not since_id:
        rows, lines = ask_context("这个群聊了啥", s, chat, source, 0, hours=7 * 24)
    if not lines:
        out = {"a": "这段时间都是表情和闲聊，没啥要紧的。", "citations": [], "n": 0}
    else:
        now = datetime.now(TZ)
        a = await llm([
            {"role": "system", "content": f"【群简报】你是用户的群消息秘书，现在是 {now:%Y-%m-%d %H:%M}。{about_me(s)}\n"
                                          f"用户没空看「{chat}」这个群，你用口语告诉他{'他上次看过之后' if since_id else '最近'}群里聊了啥："
                                          "3–5 条「- 」开头的要点，每条一句话、不超过 30 字，末尾用 [#消息编号] 标依据（只能用记录里的编号）。"
                                          "有要他做的事或 @他 的放第一条；闲聊一笔带过；全是闲聊就只说一句「都是闲聊，没啥要紧的」。不要报告腔，不要开场白。"},
            {"role": "user", "content": "聊天记录（#数字 是消息编号）：\n" + "\n".join(lines)}])
        ids = {int(ln[1:].split(" ", 1)[0]) for ln in lines}
        text, cites = cite(tidy_bullets(a), {r["id"]: r for r in rows if r["id"] in ids})
        out = {"a": text, "citations": cites, "n": len(lines)}
    out.update(since_id=since_id, ts=int(time.time()))
    with db() as c:
        c.execute("INSERT OR REPLACE INTO chat_brief(source,chat,since_id,upto_id,body,ts) VALUES(?,?,?,?,?,?)",
                  (source, chat, since_id, maxid, json.dumps(out, ensure_ascii=False), int(time.time())))
    return {**out, "cached": False, "upto_id": maxid}


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
    if not (_ceq(u, WEB_USER) and _ceq(p, WEB_PASS)):
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
    try:  # 数据库打不开/损坏时返回 503，docker 会标 unhealthy
        with db() as c:
            c.execute("SELECT 1").fetchone()
    except Exception:
        return JSONResponse({"ok": False, "version": VERSION}, status_code=503)
    return {"ok": True, "version": VERSION}


migrate_items()
migrate_modes()
with contextlib.suppress(Exception):  # 第一次启动就生成网页通知的密钥，之后不变
    vapid_keys()
