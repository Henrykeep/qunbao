"""Bark 锁屏分组名：从 bark.py 拆出，只管把群名规整成稳定的分组键。"""
import re
import unicodedata

from bark_copy import clip


_OPEN, _CLOSE = r"[(（\[【〔]", r"[)）\]】〕]"
_TAG_WORDS = "|".join(sorted(set(['仅接收不提醒', '已开启免打扰', 'group', 'muted', 'pinned', 'mute', '仅显示红点', '已关闭通知', '已屏蔽消息', '接收不提醒', '消息不提醒', '消息免打扰', '通知已关闭', '免打扰中', '全体禁言', '全员禁言', '已免打扰', '智能提醒', '有人@我', '特别关注', '红点提醒', '不提醒', '仅@我', '免打扰', '已关注', '已屏蔽', '已折叠', '已星标', '已置顶', '已隐藏', '已静音', '新消息', '群聊', '已读', '折叠', '星标', '未读', '置顶', '草稿']), key=lambda w: (-len(w), w)))  # 群名里的状态标注词（长词优先，去重），头尾共用一份
_BR_WORDS = _TAG_WORDS + "|群"  # 单独一个「群」只在括号里算标注；裸的「群/名」「群-名」是群名本身
_COUNT = r"(?:共|在线|total|online|未读|unread|群号|id)?[:：]?\s*\d{1,10}\+?\s*(?:人|名|位|members?|条未读|条新消息|条消息|条|unread)?"  # 人数会变，不能让同一个群锁屏上裂成几组
MEMBER_COUNT = re.compile(rf"\s*(?:{_OPEN}\s*(?:{_COUNT}|{_BR_WORDS})\s*{_CLOSE}|(?:\s|\s*[-|·_—–/]\s*)(?:(?:共|在线|未读|unread|online)\s*\d{{1,5}}\s*(?:人|名|条|unread)?|\d{{1,5}}\s*(?:人在线|人|名|members?))|\s*[-|·_—–/]\s*(?:{_TAG_WORDS}))\s*$", re.I)  # 尾部「群名 - 免打扰」也剥


LEAD_TAG = re.compile(rf"^(?:{_OPEN}\s*(?:{_BR_WORDS}|{_COUNT})\s*{_CLOSE}|(?:{_TAG_WORDS}|(?:共|在线|未读|unread|online)\s*\d{{1,5}}\s*(?:人|名|条)?|\d{{1,5}}\s*(?:人|名|条|members?|unread))\s*[-|·_—–/:：]\s*(?=\S))\s*", re.I)  # 开头「免打扰 - 群名」也剥


def group_label(group: str) -> str:
    """锁屏分组名：去标记、全角转半角、并空白、截 30 字、去首尾标点、英文统一小写，空了回退「群报」。"""
    group = "".join(c for c in unicodedata.normalize("NFKC", group) if unicodedata.category(c) not in ("So", "Cs", "Cf"))  # 群名不过 strip_md（否则「[草稿]群名」被占位规则改成「[文件]群名」）；表情符号：群名带🔥/🎓与否不再拆成两组
    group = re.sub(r"\s+", " ", group).strip()
    while (nxt := LEAD_TAG.sub("", group).strip()) and nxt != group:  # 开头也可能叠几层（「[置顶][358人]群名」）
        group = nxt
    while (nxt := MEMBER_COUNT.sub("", group).strip()) != group:  # 尾部可能叠两层（「群名(358)【在线12】」），剥到不变为止
        group = nxt
    group = clip(group, 30).strip(" -_.,，。·|~—–:：;；!！、《》「」…")
    return _trim_brackets(group).lower() or "群报"


_PAIRS = {"(": ")", "[": "]", "【": "】", "〔": "〕", "「": "」", "《": "》"}  # NFKC 后（）已是()


def _trim_brackets(g: str) -> str:
    """只去掉没有配对的首尾括号（截断或剥标注留下的残片）；成对的「【通知】群名」「群名(官方群)」原样保留。"""
    closers = set(_PAIRS.values())
    while g and g[-1] in _PAIRS:  # 结尾孤立的开括号
        g = g[:-1].rstrip(" -_.,，。·|~—–:：;；")
    while g and g[0] in closers:  # 开头孤立的闭括号
        g = g[1:].lstrip(" -_.,，。·|~—–:：;；")
    for o, c in _PAIRS.items():
        if g.count(o) < g.count(c):
            g = g.replace(c, "", g.count(c) - g.count(o)) if g.startswith(c) or g.endswith(c) else g
        elif g.count(o) > g.count(c):
            i = g.rfind(o)
            g = (g[:i] + g[i + 1:]).strip()
    return g.strip()
