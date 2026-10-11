"""自动整理：单个群「什么时候该整理、为什么」的纯函数（不碰全局状态，便于单测）。"""


def chat_due(arr, urg, start, end, fail_until, busy, now, *, quiet, max_wait, burst,
             urgent_quiet, urgent_wait, min_gap, force_after):
    """arr=该群待整理消息的到达时间（升序）；urg=其中紧急消息的到达时间；start/end=上次整理起止；
    fail_until=失败退避到期（None=没失败）。返回 (due, why)。"""
    first, last = arr[0], arr[-1]
    cands = [(last + quiet, "quiet"), (first + max_wait, "maxwait")]
    if len(arr) >= burst:
        cands.append((arr[burst - 1], "burst"))
    if end >= start > 0 and any(start < a <= end for a in arr):  # 上次整理进行中来的消息：一结束就接着整理
        cands.append((end, "follow"))
    if urg:
        cands.append((min(last + urgent_quiet, min(urg) + urgent_wait), "urgent"))
    due, why = min(cands)
    if not urg:  # 同群最小间隔只管闲聊
        due = max(due, start + min_gap)
        if busy:  # 全局调用快到上限：闲聊暂缓（30 秒兜底照常）
            due = max(due, now + 2)
    if fail_until:
        due = max(due, fail_until)
    force = first + force_after
    if force <= now and not (fail_until and fail_until > now):  # 兜底：等太久不管间隔，立刻排最前
        due, why = min(due, force), "force"
    elif not fail_until:
        due = min(due, force)
    return due, why


def plan_chats(by, now, auto, arrive, is_urg, busy, **cfg):
    """by={(来源,群): 待整理消息}；返回 (ready, next_due)。等太久的排最前，再要紧的，再按到点先后。"""
    ready, nxt = [], None
    for k, rs in by.items():
        if k in auto["running"]:
            continue
        arr = sorted(min(arrive.get(r["id"], r["ts"]), now) for r in rs)
        start, end = auto["last_run"].get(k, 0), auto["last_end"].get(k, 0)
        urg = [min(arrive.get(r["id"], r["ts"]), now) for r in rs if is_urg(r)]
        f = auto["fail"].get(k)
        due, why = chat_due(arr, urg, start, end, f["until"] if f else None, busy, now, **cfg)
        if due <= now:
            ready.append((why != "force", 0 if urg else 1, due, k, rs, why))
        else:
            nxt = due if nxt is None else min(nxt, due)
    ready.sort(key=lambda x: (x[0], x[1], x[2]))
    return [(k, rs, why) for _, _, _, k, rs, why in ready], nxt


def fail_record(prev, now, code, err, *, backoff0, backoff_max, wall=None):
    """某群整理失败后的记录：连续次数、首次失败时间、退避到期（指数退避，封顶）。"""
    n = prev.get("n", 0) + 1
    return {"n": n, "since": prev.get("since", wall or now), "until": now + min(backoff_max, backoff0 * 2 ** (n - 1)),
            "err": err, "code": code, "ts": wall or now}


def should_give_up(n, age, model_ok, fatal, *, giveup_n, giveup_secs, giveup_hard):
    """是否放弃这个群这几条：模型是好的但这个群反复失败，或挂得太久（要用户处理的致命错误除外）。"""
    return bool((model_ok and (n >= giveup_n or (n >= 2 and age >= giveup_secs))) or (not fatal and age >= giveup_hard))
