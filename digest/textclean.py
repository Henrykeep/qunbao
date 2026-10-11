"""广告判定与事项标题整理（纯函数，不依赖 app.py）：is_ad_sure / tidy_title / same_text。"""
import json
from fastapi import HTTPException
import re
from difflib import SequenceMatcher

from todo_match import norm_title

AD_CATS = (("promo", re.compile(r"券后|优惠券|领券|返利|返现|包邮|秒杀|神价|线报|速度冲|速冲|到手价|历史低价|好价|白菜价|限时抢", re.I)),
           ("shop", re.compile(r"淘口令|￥[A-Za-z0-9]{6,}￥|复制这条|打开手淘|打开淘宝|m\.tb\.cn|u\.jd\.com|s\.click\.taobao|uland\.taobao|yangkeduo|pinduoduo|拼多多|item\.jd\.com|tb\.cn/", re.I)),
           ("chop", re.compile(r"砍一刀|帮我砍|助力一下|帮忙助力|点一下助力")),
           ("gig", re.compile(r"代取快递|代拿|跑腿|可小刀|出闲置|低价出|私聊下单|招代理|兼职日结|日结|刷单|宝妈")),
           ("price", re.compile(r"[¥￥]\s?\d+(?:\.\d+)?|\d+(?:\.\d+)?\s?元")))
AD_VETO = re.compile(r"缴费|交费|班费|学费|报名费|资料费|书费|通知|老师|辅导员|导员|班主任|班长|学委|团支书|学院|作业|考试|开会|班会|签到|查寝|点名|@全体|@所有人|截止前|务必|学校")


def is_ad_sure(text: str) -> bool:
    t = text or ""
    if AD_VETO.search(t):
        return False
    cats = {n for n, rx in AD_CATS if rx.search(t)}
    return len(cats) >= 2 and bool(cats & {"promo", "chop", "gig"})


URL_ONLY_RE = re.compile(r"https?://\S+")


DATE_IN_TITLE = re.compile(r"(?:(?:\d{4}[年/-])?\d{1,2}[月/-]\d{1,2}[日号]?|(?:本|这|下)?(?:周|星期)[一二三四五六日天](?:晚上?|早上?|上午|下午|中午)?|(?:今|明|后)(?:天|晚|早)|"
                           r"(?:凌晨|早上|上午|中午|下午|晚上|晚)?\s*\d{1,2}[:：]\d{2}|(?:凌晨|早上|上午|中午|下午|晚上|晚)?\s*\d{1,2}\s*点(?:半|\d{1,2}分?)?|"
                           r"凌晨|早上|上午|中午|下午|晚上)(?:\s*(?:之前|以前|前|截止))?")


TAIL_FILLERS = (
    r"(?:[，,]?\s*(?:谢谢|感谢|多谢|辛苦了?|拜托了?)[大家各位！!]*|[哦噢啦呀哈喔]+|~+|～+)+$",  # 结尾客套/语气词
    r"[，,]\s*(?:请)?(?:大家|各位|同学们?)?(?:注意|知悉|留意|周知|查收|查看|关注|悉知)(?:一下|下)?[！!。]*$",  # 结尾「请大家注意」类
    r"\s*[（(](?:详见|详情见|见|附)[^）)]{0,12}[）)]$",  # 结尾「（详见群公告）」类指引
    r"[，,。；;]\s*(?:望周知|特此通知|特此提醒|以上)[！!。]*$",  # 结尾「望周知/特此通知」
    r"[，,：:]\s*(?:具体|详情|详细)?(?:如下|见下|见群公告|见通知|见附件|请看(?:群公告|附件)|请见(?:群公告|附件))[：:。！!]*$",  # 结尾「具体如下」类
    r"[，,。]\s*(?:不要|别|千万别?)(?:忘了|忘记|迟到|错过|漏了)[哦噢啊呀！!。]*$",  # 结尾「不要忘了」类
    r"[，,。]\s*(?:收到|看到|有问题|有疑问)(?:的|后)?(?:请|麻烦)?(?:大家|同学)?(?:回复|私聊我?|联系我?|在群里说)[一下！!。]*$",  # 结尾「收到请回复」类
    r"[，,。]\s*(?:请|麻烦)?(?:大家|各位|同学们?)?(?:准时|按时|积极|踊跃)(?:参加|到场|参与|出席|完成|提交)[哦噢啊呀！!。]*$",  # 结尾「请准时参加」类
    r"[，,。]\s*(?:请|麻烦)?(?:大家|各位|同学们?)?(?:积极)?(?:配合|支持|理解)(?:一下|下)?(?:工作)?[哦噢啊呀！!。]*$",  # 结尾「请大家配合」类
    r"[，,。]\s*(?:逾期|过时|过期|迟到)(?:不候|不补|不再受理|视为放弃)[！!。]*$",  # 结尾「逾期不候」类
    r"[，,。]\s*(?:名额有限|先到先得|额满为止|先报先得)[！!。]*$",  # 结尾「名额有限」类
    r"[，,。]\s*(?:有(?:事|问题)|不能(?:来|参加)|来不了)(?:的|请)?(?:提前|先)?(?:请假|说一声|告知|跟我说)[哦噢啊呀！!。]*$",  # 结尾「有事提前请假」类
    r"[，,。]\s*(?:请|麻烦)?(?:大家|各位|同学们?)?(?:带好|带上|携带)(?:好)?(?:学生证|身份证|证件|笔|电脑|资料)[哦噢啊呀！!。]*$",  # 结尾「请带好证件」类
)

LEAD_FILLERS = (
    r"^(?:(?:温馨)?提醒|通知|紧急通知|重要通知|注意)[：:]\s*",
    r"^(?:麻烦|请)?(?:各位|大家|全体)(?:同学|小伙伴|老师)?(?:们)?[，,\s]*",
    r"^(?:请于|请在|请|麻烦|需要|记得|别忘了|不要忘记)\s*",
    r"^(?:转发|转|再次提醒|再提醒|再强调|强调)(?:一下)?[：:，,]\s*",
    r"^[（(](?:紧急|重要|急|必看|置顶)[）)]\s*",
    r"^(?:(?:务必|尽快|及时|抓紧|赶紧|千万别?)\s*)+",
)


def tidy_title(t: str, limit: int = 22, due: str = "") -> str:
    """模型给的事项标题兜底：去链接/@/套话，有截止时间时把日期从标题里拿掉（卡片上另有截止标签），
    罗列多件事只留第一件，太长在标点或空格处截断。"""
    t = re.sub(r"\s*https?(?::\S*)?", "", URL_ONLY_RE.sub("", t or ""))  # 含被截断的半截链接
    if due and len(t) > 10:
        t2 = re.sub(r"\s*(?:之?前|以前|截止)?\s*(?=$|[，,。])", "", DATE_IN_TITLE.sub(" ", t)).strip()
        t2 = re.sub(r"^\s*(?:前|之前|以前)\s*", "", re.sub(r"\s{2,}", " ", t2)).strip(" ，,")
        t2 = re.sub(r"^[\s\-–—~～·]+|[\s\-–—~～·]+$", "", re.sub(r"\s+[\-–—~～]+\s+", " ", t2))
        if len(re.sub(r"\W", "", t2)) >= 4:
            t = t2
    t = re.sub(r"@全体成员|@所有人|@我|【[^】]{0,8}】", "", t).strip(" ，,。；;:：!！")
    t = re.sub(r"^关于(.+?)的?(通知事项|通知|事项|事宜)$", r"\1", t)
    for _ in range(3):  # 前缀套话（通知：/各位同学/请务必…）可叠加，一处循环剥干净
        t0 = t
        for pat in LEAD_FILLERS:
            t = re.sub(pat, "", t).strip() or t
        if t == t0:
            break
    for pat in TAIL_FILLERS:  # 结尾客套/提醒/公文尾巴，表驱动
        t = re.sub(pat, "", t).strip() or t
    parts = re.split(r"[，,；;]\s*(?:并且|并|还要|另外|以及|同时|还有)|；|;", t)
    t = parts[0].strip(" ，,。") if parts and len(parts[0].strip()) >= 4 else t
    if len(t) <= limit:
        return t.rstrip("，、；,;。 ")
    cut = max((m.end() for m in re.finditer(r"[，。；！？、,;!? ]", t[:limit + 1])), default=0)
    if cut >= 8:
        return t[:cut].rstrip("，。；、,; ").strip()
    return t[:limit - 1].rstrip("，、；,; ") + "…"


def same_text(a: str, b: str) -> bool:
    """详情是不是只把标题换个说法（卡片上就不重复显示）：标题的 2 字片段六成以上出现在详情里。"""
    x, y = re.sub(r"\d+", "", norm_title(a)), re.sub(r"\d+", "", norm_title(b))
    if not x or not y:
        return False
    if x in y or y in x or SequenceMatcher(None, x, y).ratio() >= 0.7:
        return True
    g = [x[i:i + 2] for i in range(len(x) - 1)]
    return bool(g) and sum(1 for t in g if t in y) / len(g) >= 0.6


def _jparse(out: str) -> dict:
    m = re.search(r"\{.*\}", out or "", re.S)
    try:
        d = json.loads(m.group(0) if m else out)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(502, "大模型返回的不是合法 JSON，再试一次")
    return d if isinstance(d, dict) else {}


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None
