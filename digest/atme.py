"""首页「@我」列表的口径（纯函数，不依赖 app.py）：哪些 @我 已被整理成事项、由哪条待办认领。"""
from todo_match import norm_title


def at_me_view(rows, body) -> list[dict]:
    items = (body or {}).get("todos", []) + (body or {}).get("notices", [])
    todos = (body or {}).get("todos", [])
    covered = {int(m) for t in items for m in str(t.get("msg_ids") or "").split() if m.isdigit()}
    qs = [(norm_title(t.get("quote") or "")[:16], t.get("key") or "") for t in todos if t.get("quote")]
    mid_key: dict[int, str] = {}  # 哪条待办「认领」了这条 @我：待办勾完成后前端不再算它未处理
    for t in todos:
        for m in str(t.get("msg_ids") or "").split():
            if m.isdigit():
                mid_key.setdefault(int(m), t.get("key") or "")
    out = []
    for r in rows:
        nt = norm_title(r["text"])
        hit = next((k for q, k in qs if q and q in nt), "")
        out.append({"id": r["id"], "ts": r["ts"], "chat": r["chat"], "sender": r["sender"], "text": r["text"],
                    "source": r["source"], "covered": r["id"] in covered or any(q and q in nt for q, _ in qs),
                    "by": mid_key.get(r["id"]) or hit})
    return out
