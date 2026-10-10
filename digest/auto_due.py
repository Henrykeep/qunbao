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
