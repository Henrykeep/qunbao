"""问答文本整形：检索词、引用角标、要点截断。0.34.38 从 app.py 拆出（纯函数，无依赖）。"""
import re

CITE_RE = re.compile(r"\[#?(\d{1,10})\]|【#?(\d{1,10})】|#(\d{2,10})\b")


def _q_terms(q: str) -> list:
    """问题里的检索词：英文/数字词 + 中文 2 字片段（去掉疑问套话）。"""
    q = re.sub(r"什么|怎么|有没有|哪些|哪个|是不是|吗|呢|吧|了|的|我|你|谁|说|今天|这周|最近|一下|这个群|群里", " ", q or "")
    terms = re.findall(r"[A-Za-z0-9]{2,}", q)
    for seg in re.findall(r"[\u4e00-\u9fff]{2,}", q):
        terms += [seg] if len(seg) <= 4 else [seg[i:i + 2] for i in range(len(seg) - 1)]
    return list(dict.fromkeys(t.lower() for t in terms))[:12]


def cite(answer: str, valid: dict):
    """把回答里的 [#消息id] 换成 [1][2] 角标；不在给定消息里的 id 一律丢掉（防止模型编造引用）。"""
    order, out = [], []

    def rep(m):
        i = int(m.group(1) or m.group(2) or m.group(3))
        if i not in valid:
            return ""
        if i not in order:
            order.append(i)
        return f"[{order.index(i) + 1}]"
    text = CITE_RE.sub(rep, answer or "")
    text = re.sub(r"(\[\d+\])(\1)+", r"\1", text)
    text = re.sub(r"[ \t]+\n", "\n", text).strip()
    for k, i in enumerate(order):
        r = valid[i]
        out.append({"n": k + 1, "msg_id": i, "ts": r["ts"], "sender": r["sender"], "chat": r["chat"], "source": r["source"],
                    "snippet": re.sub(r"\s+", " ", r["text"] or "")[:80]})
    return text, out


def tidy_bullets(a: str, limit: int = 34) -> str:
    """要点必须是完整短句：太长只在标点处截断，引用角标留在句末。"""
    out = []
    for ln in (a or "").splitlines():
        m = re.match(r"^(\s*[-·•]\s*)(.*?)((?:\s*(?:\[#?\d{1,10}\]|【#?\d{1,10}】))*)\s*$", ln)
        if not m or not m.group(1).strip():
            out.append(ln)
            continue
        body = m.group(2)
        if len(body) > limit:
            cut = max((x.start() for x in re.finditer(r"[，。；！？,;!?]", body[:limit + 1])), default=0)
            body = body[:cut] if cut >= 8 else body[:limit].rstrip("，、；,;（(") + "…"
        out.append(m.group(1) + body.rstrip("，、；,;") + m.group(3))
    return "\n".join(out)
