"""Bark 锁屏分组名：从 bark.py 拆出，只管把群名规整成稳定的分组键。"""
import re
import unicodedata
from functools import lru_cache

from bark_copy import clip


_OPEN, _CLOSE = r"[(（\[【〔]", r"[)）\]】〕]"
# 群名里的状态标注词，头尾共用一份；新增只往这里加
_TAG_LIST = (
    '仅接收不提醒', '有新消息', '有新公告', '有人@所有人', '@all', '@everyone', '有人@全体成员', '@所有人',
    '@全体成员', '已开启免打扰', 'group', 'muted', 'pinned', 'mute', '仅显示红点', '已关闭通知',
    '已屏蔽消息', '接收不提醒', '消息不提醒', '消息免打扰', '通知已关闭', '免打扰中', '全体禁言', '全员禁言',
    '已免打扰', '智能提醒', '有人@我', '有人@你', '@我', '@你', '有人回复我', '有人回复你',
    '有人回复', '群公告', '特别关注', '红点提醒', '不提醒', '仅@我', '免打扰', '已关注',
    '已屏蔽', '已折叠', '已星标', '已置顶', '已隐藏', '已静音', '新消息', '群聊',
    '已读', '折叠', '星标', '未读', '置顶', '草稿',
)
_TAG_WORDS = "|".join(sorted(set(_TAG_LIST), key=lambda w: (-len(w), w)))  # 长词优先、去重
_BR_ONE = f"(?:{_TAG_WORDS}|群)"
_BR_WORDS = rf"{_BR_ONE}(?:\s*[,，、|/·+]\s*|\s+){_BR_ONE}(?:(?:\s*[,，、|/·+]\s*|\s+){_BR_ONE})*|{_TAG_WORDS}|群"  # 括号里叠多个标注「(置顶 免打扰)」「[置顶|免打扰]」也整组剥  # 单独一个「群」只在括号里算标注；裸的「群/名」「群-名」是群名本身
_COUNT = r"(?:共|在线|total|online|未读|unread|群号|id)?[:：]?\s*\d{1,10}\+?\s*(?:人|名|位|members?|条未读|条新消息|条消息|条|unread)?"  # 人数会变，不能让同一个群锁屏上裂成几组
MEMBER_COUNT = re.compile(rf"\s*(?:{_OPEN}\s*(?:{_COUNT}|{_BR_WORDS})\s*{_CLOSE}|(?:\s|\s*[-|·_—–/]\s*)(?:(?:共|在线|未读|unread|online)\s*\d{{1,5}}\s*(?:人|名|条|unread)?|\d{{1,5}}\s*(?:人在线|人|名|members?|条未读|条新消息|unread))|\s*[-|·_—–/]\s*(?:{_TAG_WORDS}))\s*$", re.I)  # 尾部「群名 - 免打扰」也剥


LEAD_TAG = re.compile(rf"^(?:{_OPEN}\s*(?:{_BR_WORDS}|{_COUNT})\s*{_CLOSE}|(?:{_TAG_WORDS}|(?:共|在线|未读|unread|online)\s*\d{{1,5}}\s*(?:人|名|条)?|\d{{1,5}}\s*(?:人|名|条|members?|unread))\s*[-|·_—–/:：]\s*(?=\S))\s*", re.I)  # 开头「免打扰 - 群名」也剥


@lru_cache(maxsize=512)  # 同一个群每条推送都算一遍，结果只取决于群名，缓存即可
def group_label(group: str) -> str:
    """锁屏分组名：去标记、全角转半角、并空白、截 30 字、去首尾标点、英文统一小写，空了回退「群报」。"""
    group = "".join(c for c in unicodedata.normalize("NFKC", str(group or "")) if unicodedata.category(c) not in ("So", "Cs", "Cf", "Sk") and c not in "\ufe0e\ufe0f")  # 群名不过 strip_md（否则「[草稿]群名」被占位规则改成「[文件]群名」）；表情符号：群名带🔥/🎓与否不再拆成两组
    group = re.sub(r"\s+", " ", group[:300]).strip()  # 先截断再跑正则：异常长群名不拖慢推送
    while (nxt := LEAD_TAG.sub("", group).strip()) and nxt != group:  # 开头也可能叠几层（「[置顶][358人]群名」）
        group = nxt
    while (nxt := MEMBER_COUNT.sub("", group).strip()) != group:  # 尾部可能叠两层（「群名(358)【在线12】」），剥到不变为止
        group = nxt
    group = re.sub(r"[・•‧∙●]", "·", group)  # 各种中点写法统一成「·」，「计科・2201」与「计科·2201」同一个群
    group = re.sub(r"(?<=[\u4e00-\u9fff]) | (?=[\u4e00-\u9fff])", "", group)  # 汉字两侧的空格都不算区分（「计科 2201」「Python 学习群」「Python学习群」同一个群）
    group = re.sub(r" ?([-_/·|—–~])(?: ?\1)* ?", r"\1", group)  # 分隔符：两侧空格不算区分、连写折成一个（「计科 - 2201」「计科--2201」都等于「计科-2201」）
    group = clip(group, 30).strip(_EDGE + "!！、《》「」…")
    return _trim_brackets(group).lower() or "群报"


_EDGE = " -_.,，。·|~—–:：;；"  # 首尾要剥的标点，两处共用
_PAIRS = {"(": ")", "[": "]", "【": "】", "〔": "〕", "「": "」", "《": "》"}  # NFKC 后（）已是()


def _trim_brackets(g: str) -> str:
    """只去掉没有配对的首尾括号（截断或剥标注留下的残片）；成对的「【通知】群名」「群名(官方群)」原样保留。"""
    closers = set(_PAIRS.values())
    while g and g[-1] in _PAIRS:  # 结尾孤立的开括号
        g = g[:-1].rstrip(_EDGE)
    while g and g[0] in closers:  # 开头孤立的闭括号
        g = g[1:].lstrip(_EDGE)
    for o, c in _PAIRS.items():
        if g.count(o) < g.count(c):
            g = g.replace(c, "", g.count(c) - g.count(o)) if g.startswith(c) or g.endswith(c) else g
        elif g.count(o) > g.count(c):
            i = g.rfind(o)
            g = (g[:i] + g[i + 1:]).strip()
    return g.strip()
