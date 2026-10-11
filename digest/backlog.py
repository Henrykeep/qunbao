"""大积压保护与分块的纯函数（0.34.44 从 app.py 拆出，不碰数据库/全局状态）。"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.getenv("APP_TZ") or "Asia/Shanghai")

def _line(r, cls) -> str:
    return (f"#{r['id']} [{datetime.fromtimestamp(r['ts'], TZ):%m-%d %H:%M}] {r['sender']}"
            f"{' (@我)' if r['at_me'] else ''}{' ★' if cls == 'key' else ''}: {(r['text'] or '')[:800]}")


BACKLOG_KEEP = int(os.getenv("BACKLOG_KEEP", "240"))   # 单群一次最多整理最近这么多条；更早的直接略过（刚升级/断了很久才会遇到）
BACKLOG_OLD_KEEP = 30                                  # 略过的旧消息里，@我 / 重点消息最多再保留这么多条


def cap_backlog(pairs):
    """大积压保护：一个群一次攒了几百条（服务器刚升级、断线很久），只整理最近 BACKLOG_KEEP 条，
    更早的只留 @我 / 重点消息，其余直接推进水位。这样几分钟内能消化完，不会连发十几次模型调用引发限流。返回 (保留的, 略过条数)。"""
    if len(pairs) <= BACKLOG_KEEP:
        return pairs, 0
    old, recent = pairs[:-BACKLOG_KEEP], pairs[-BACKLOG_KEEP:]
    keep = [(r, c) for r, c in old if r["at_me"] or c == "key"][-BACKLOG_OLD_KEEP:]
    return keep + recent, len(old) - len(keep)


def chunked(pairs, max_msgs=120, max_chars=6000):
    out, cur, n = [], [], 0
    for r, cls in pairs:
        ln = _line(r, cls)
        if cur and (len(cur) >= max_msgs or n + len(ln) > max_chars):
            out.append(cur); cur, n = [], 0
        cur.append((r, cls, ln)); n += len(ln) + 1
    if cur:
        out.append(cur)
    return out
