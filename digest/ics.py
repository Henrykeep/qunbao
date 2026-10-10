"""待办导出 .ics（RFC5545），纯函数，不依赖 app.py。"""
import hashlib
from datetime import timedelta
from zoneinfo import ZoneInfo


def _ics_esc(t: str) -> str:
    return (t or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def _ics_fold(line: str) -> str:
    """RFC5545：每行不超过 75 字节，超出折行（按字符边界，不切断中文）。"""
    out, cur, n = [], "", 0
    for ch in line:
        b = len(ch.encode())
        if n + b > (75 if not out else 74):
            out.append(cur); cur, n = "", 0
        cur += ch; n += b
    out.append(cur)
    return "\r\n ".join(out)


def _ics_fold_all(t: str) -> str:
    return "\r\n".join(_ics_fold(l) for l in t.split("\r\n")) 


def build_ics(title: str, due: str, dt, now, detail: str = "", chat: str = "") -> str:
    """dt：已解析的到期时间（None 则整天事件）；now：带时区的当前时间。"""
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
    return _ics_fold_all("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//qunbao//CN\r\nBEGIN:VEVENT\r\n"
            f"UID:{uid}\r\nDTSTAMP:{stamp}\r\n{when}\r\nSUMMARY:{_ics_esc(title)}\r\n"
            f"DESCRIPTION:{_ics_esc(desc)}\r\nBEGIN:VALARM\r\nTRIGGER:-PT1H\r\nACTION:DISPLAY\r\n"
            "DESCRIPTION:待办提醒\r\nEND:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")


