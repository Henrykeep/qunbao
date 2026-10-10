"""规则兜底的纯函数部分：识别「今晚查寝 / 明天点名 / 开会」这类要你到场的消息。0.34.39 从 app.py 拆出。
AD_RE（线报/广告特征）也放这里，app.py 与 rules 共用，避免循环依赖。"""
import os, re, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from todo_match import parse_due

TZ = ZoneInfo(os.getenv("APP_TZ") or "Asia/Shanghai")

AD_RE = re.compile(r"券后|优惠券|领券|返利|返现|包邮|秒杀|神价|线报|速度冲|速冲|复制这条|打开手淘|淘口令|￥[A-Za-z0-9]{6,}￥|"
                   r"砍一刀|帮我砍|助力一下|拼多多|yangkeduo|pinduoduo|m\.tb\.cn|u\.jd\.com|s\.click\.taobao|uland\.taobao|"
                   r"代取快递|代拿|跑腿|可小刀|出闲置|低价出|私聊下单|招代理|兼职日结|刷单", re.I)
EVENT_RE = re.compile(r"查寝|查宿舍?|查卫生|卫生检查|内务检查|查内务|点名|点到|查课|查早操|查晚自习|考试|测验|开会|班会|会议|集合|签到|上交|缴费|交费")
WHEN_RE = re.compile(r"今天晚上|今天下午|今天上午|今晚|今早|今天|今日|明天晚上|明天下午|明天上午|明晚|明早|明天|后天|下课后?|晚自习|待会儿?|一会儿?|等下|等会儿?|稍后|马上|立刻|立即|现在|中午|下午|晚上")
CLOCK_RE = re.compile(r"(\d{1,2})\s*[:：点]\s*(\d{1,2}|半)?")
SOON_WORDS = ("下课", "待会", "一会", "等下", "等会", "稍后", "马上", "立刻", "立即", "现在")
PAST_RE = re.compile(r"昨天|昨晚|前天|上周|已经|查过|查完|点过|签过|考完|开完|结束了|刚才|刚刚|取消|不查|不用|不点|没查|没点|好严|吓死|"
                     r"好难|太难|难死|好简单|怎么样|咋样|纪要|记录|总结|回放|"
                     r"抽奖|红包|直播|福利|领取|优惠|免费领")  # 过去的事、闲聊评论、直播抽奖都不算
WHO_RE = re.compile(r"辅导员|导员|班主任|宿管|学生会|班长|[\u4e00-\u9fa5]导(?=[要会来查点说今明下])|[\u4e00-\u9fa5]?老师")
EVENT_ACT = (("查寝|查宿|查卫生|卫生检查|内务", "在寝室并收拾好"), ("点名|点到|查课|查早操|查晚自习", "按时到场"), ("签到", "记得按时签到"),
             ("考试|测验", "按时参加，带好证件"), ("开会|班会|会议", "按时参加"), ("集合", "按时到集合点"), ("上交|缴费|交费", "按要求完成"))
EVENT_NAME = {"查宿": "查寝", "查宿舍": "查寝", "卫生检查": "查卫生", "内务检查": "查内务", "测验": "考试", "会议": "开会", "交费": "缴费"}


def rule_event(r, now=None):
    """一条消息是不是「今晚导员会来查寝」这类要你到场/准备的事：是就返回 (标题, 截止, 事件词, urgency)，否则 None。"""
    text = (r["text"] or "").strip()
    ev = EVENT_RE.search(text)
    if not ev or len(text) > 200 or AD_RE.search(text) or PAST_RE.search(text):
        return None
    when, clock = WHEN_RE.search(text), CLOCK_RE.search(text)
    if not when and not clock:
        return None
    if re.search(r"[吗嘛？?]\s*$", text) and "@全体" not in text:  # 「今晚查寝吗？」是问句，不是通知
        return None
    now = now or time.time()
    nd, td = datetime.fromtimestamp(now, TZ), datetime.fromtimestamp(r["ts"], TZ)
    g = timedelta(hours=5)  # 凌晨 5 点前仍算「昨天的今晚」：「天」按减 5 小时算
    nd, tdd = nd - g, td - g
    w = when.group(0) if when else ""
    shift = 2 if w.startswith("后天") else 1 if w.startswith("明") else 0
    day = (tdd + timedelta(days=shift)).date()
    dm = re.search(r"\d{1,2}月\d{1,2}[日号]?|(?:下下|下个?|本|这)?(?:周|星期|礼拜)[一二三四五六日天]", text)
    if dm:  # 写了具体日期 / 周几：按它算哪天
        pd = parse_due(dm.group(0), td)
        if pd:
            day = pd.date()
            if w.startswith(("今", "明", "后天")):
                w = ""
    if day < nd.date():
        return None  # 说的那天已经过去了
    if any(x in w for x in SOON_WORDS) and now - r["ts"] > 3 * 3600:
        return None  # 「下课」「待会」这种马上就发生的，三小时后就不算了
    hm = ""
    if clock:
        hh, mm = int(clock.group(1)), clock.group(2)
        mm = 30 if mm == "半" else int(mm or 0)
        if hh <= 23 and mm <= 59:
            pre = ("晚上 " if "晚" in w else "下午 " if "下午" in w else "") if hh < 12 else ""
            hm = f"{pre}{hh}:{mm:02d}"
    tag = hm or ({"今天晚上": "今晚", "明天晚上": "晚上", "明晚": "晚上", "明早": "上午", "明天上午": "上午", "明天下午": "下午",
                  "今天下午": "下午", "今天上午": "上午", "下课": "下课后"}.get(w, w))
    due = f"{day.month}月{day.day:02d}日 {tag}".strip()
    if day == nd.date():
        label = w if w and not w.startswith(("明", "后天")) else "今天"
    elif day == nd.date() + timedelta(days=1):
        label = "明天" + ("晚上" if "晚" in w else "")
    else:
        label = re.sub(r"^(?:星期|礼拜)", "周", dm.group(0)) if dm and not re.match(r"\d", dm.group(0)) else f"{day.month}月{day.day}日"
    label = {"今天晚上": "今晚", "下课": "下课后", "现在": "马上", "立即": "马上", "立刻": "马上"}.get(label, label)
    who = WHO_RE.search(text)
    name = EVENT_NAME.get(ev.group(0), ev.group(0))
    act = next(a for k, a in EVENT_ACT if re.search(k, ev.group(0)))
    title = f"{label}{who.group(0) if who else ''}{name}：{act}"
    allm = "@全体" in text or "@所有人" in text
    urg = "high" if (allm or r["at_me"] or day <= nd.date() + timedelta(days=1)) else "mid"
    return title, due, name, urg
