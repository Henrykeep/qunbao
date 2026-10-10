"""存储层（0.34.25 从 app.py 拆出）：连接、建表、迁移。不 import app。"""
import os, sqlite3

DB = os.getenv("DB_PATH", "/data/qunbao.db")


def db():
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass
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
    CREATE TABLE IF NOT EXISTS llm_log(ts INTEGER, kind TEXT);
    CREATE INDEX IF NOT EXISTS i_llm_log ON llm_log(ts);
    CREATE TABLE IF NOT EXISTS push_subs(endpoint TEXT PRIMARY KEY, p256dh TEXT, auth TEXT, ua TEXT DEFAULT '',
        ts INTEGER, ok_ts INTEGER DEFAULT 0, fails INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS pushed(k TEXT PRIMARY KEY, ts INTEGER);
    CREATE TABLE IF NOT EXISTS chat_brief(source TEXT, chat TEXT, since_id INTEGER, upto_id INTEGER, body TEXT, ts INTEGER,
        PRIMARY KEY(source, chat));
    """)
    for tb, col in [("msgs", "img"), ("todo_done", "title"), ("todo_done", "chat"), ("todo_done", "due"),
                    ("pins", "title"), ("pins", "chat"), ("pins", "due"), ("reminded", "title"), ("reminded", "chat"),
                    ("reminded", "due"), ("snooze", "chat"), ("snooze", "due")]:
        try:
            c.execute(f"ALTER TABLE {tb} ADD COLUMN {col} TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
