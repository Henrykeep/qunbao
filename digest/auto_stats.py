"""自动整理成本统计（纯函数，不依赖 app.py）：从 AUTO_LOG 记录算次数、调用数、等待时间。"""
import time


def summarize(log, sec: int = 3600, now: float | None = None) -> dict:
    cut = (now or time.time()) - sec
    rs = [x for x in log if x["ts"] >= cut]
    w = sorted(x["wait"] + x["secs"] for x in rs if x["ok"])
    return {"runs": len(rs), "calls": sum(x["calls"] for x in rs), "msgs": sum(x["n"] for x in rs), "sent": sum(x["sent"] for x in rs),
            "fails": sum(1 for x in rs if not x["ok"]), "avg_secs": round(sum(x["secs"] for x in rs) / len(rs), 1) if rs else 0,
            "p50_latency": w[len(w) // 2] if w else 0, "max_latency": w[-1] if w else 0,
            "by_why": {k: sum(1 for x in rs if x["why"] == k) for k in sorted({x["why"] for x in rs})}}
