"""Bark 锁屏分组名：从 bark.py 拆出，只管把群名规整成稳定的分组键。"""
import re
import unicodedata

from bark_copy import clip


def _strip_md(t):
    from bark import strip_md  # 延迟导入，避免循环
    return strip_md(t)


_OPEN, _CLOSE = r"[(（\[【〔]", r"[)）\]】〕]"
_TAG_WORDS = r"群聊?|group|免打扰|已静音|置顶|全员禁言|全体禁言|已屏蔽|消息免打扰|已折叠|折叠|特别关注|已关注|仅@我|已置顶|已免打扰|有人@我|已星标|星标|已隐藏"  # 群名里的状态标注词，头尾共用一份
_COUNT = r"(?:共|在线|total|online|未读|unread)?\s*\d{1,5}\s*(?:人|名|位|members?|条未读|条消息|条|unread)?"  # 人数会变，不能让同一个群锁屏上裂成几组
MEMBER_COUNT = re.compile(rf"\s*(?:{_OPEN}\s*(?:{_COUNT}|{_TAG_WORDS})\s*{_CLOSE}|\s\d{{1,5}}\s*(?:人|名|members?))\s*$", re.I)


LEAD_TAG = re.compile(rf"^{_OPEN}\s*(?:{_TAG_WORDS}|{_COUNT})\s*{_CLOSE}\s*", re.I)


def group_label(group: str) -> str:
    """锁屏分组名：去标记、全角转半角、并空白、截 30 字、去首尾标点、英文统一小写，空了回退「群报」。"""
    group = "".join(c for c in unicodedata.normalize("NFKC", _strip_md(group)) if unicodedata.category(c) not in ("So", "Cs", "Cf"))  # 表情符号：群名带🔥/🎓与否不再拆成两组
    group = re.sub(r"\s+", " ", group).strip()
    while (nxt := LEAD_TAG.sub("", group).strip()) and nxt != group:  # 开头也可能叠几层（「[置顶][358人]群名」）
        group = nxt
    while (nxt := MEMBER_COUNT.sub("", group).strip()) != group:  # 尾部可能叠两层（「群名(358)【在线12】」），剥到不变为止
        group = nxt
    return clip(group, 30).strip(" -_.,，。·|~—–:：;；!！、《》「」【】()（）…").lower() or "群报"
