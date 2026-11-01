"""Bark 一键复制内容的挑选（0.38.34 从 bark.py 拆出）：口令/验证码 > 链接 > 账号 > 邮箱 > 手机号 > 标题。纯函数。"""
import re


def clip(t: str, n: int) -> str:
    """超长截断并补省略号，让人看出被截了而不是话没说完。"""
    t = t or ""
    if len(t) <= n:
        return t
    cut = n - 1
    k = max(t.rfind(c, 0, cut) for c in "。！？!?\n；;")  # 优先在句末收尾：锁屏上读到完整的一句，比半句加省略号清楚
    if k >= n * 0.6:
        return t[:k + 1].rstrip() + "…"
    m = re.search(r"[A-Za-z0-9\-]+$", t[:cut])
    if m and t[cut:cut + 1].isascii() and t[cut:cut + 1].isalnum() and m.start() >= 3:
        cut = m.start()  # 不把取件码/单号/英文单词拦腰截断
    o = t.rfind("[", 0, cut)
    if o >= 3 and "]" not in t[o:cut] and cut - o <= 6:
        cut = o  # 不把「[图片]」「[链接]」占位拦腰截成「[图…」
    return t[:cut].rstrip("，,、：:；;（([【“\"‘ ") + "…"  # 截断处不留悬空标点/半个括号


CODE_WORDS = r"(?!(?:群|学|手机|电话|工|QQ|微信|房间|门牌|身份证|邮政|订单)[码号])[\u4e00-\u9fff]{1,3}[码号]|快递单号|口令|暗号|密码|会议\s?(?:ID|id)|(?i:meeting)\s?(?:ID|number|no\.?)|腾讯会议|飞书会议|钉钉会议|(?i:pwd|passcode|code|otp|pin)(?![A-Za-z])"  # 任何「X码/X号」都算口令，不再一个个枚举（手机号等超 8 位的数字天然匹配不上）
CODE_PHRASE = re.compile(r"(?:口令|暗号|入群密码|进群密码)[\s:：是为]{0,4}[「“\"']?(?!红包|红包口令)(?![A-Za-z]+(?![A-Za-z0-9\u4e00-\u9fff]))([\u4e00-\u9fffA-Za-z0-9]{2,12})")  # 入群口令常是中文词「学习2024」「芝麻开门」，要整个复制
CODE_AFTER = re.compile(r"(?:" + CODE_WORDS + r")(?:\s+is)?[^A-Za-z0-9]{0,4}((?=[A-Za-z-]*\d)[A-Za-z0-9-]{3,12})(?![A-Za-z0-9])")  # 3 位也认但必须含数字：食堂「取餐号 A12」
CODE_SECRET = re.compile(r"(?:提取码|密码|口令|暗号|兑换码|激活码|邀请码|序列号|核销码|券码|兑换券|[?&]pwd)[^A-Za-z0-9]{0,4}([A-Za-z0-9]{4,6}(?:-[A-Za-z0-9]{4,6}){1,4}|[A-Za-z]{4,8})(?![A-Za-z0-9-]|[@#$%^&*_!+=][A-Za-z0-9])")  # 带符号的密码（campus@2024）交给 CODE_PWD 整串复制  # 含 ABCD-EFGH-IJKL 分段码  # 网盘提取码常是纯字母 abcd
SPLIT_DIGITS = re.compile(r"(?<!\d)(\d{3,4})((?:[ -]\d{3,4})+)(?!\d)|(?<![\d.])(\d{1,2})((?: \d{1,2}){3,7}|(?: \d{2}){2,3})(?![\d.])")  # 会议号「123 456 789」分组书写，复制要整串
CODE_BEFORE = re.compile(r"(?<![\dA-Za-z])(\d{4,8})(?![\dA-Za-z])\D{0,6}(?:是|为)?(?:你的|您的)?(?:" + CODE_WORDS + r")")  # “123456 为您的验证码”

CODE_EN_BEFORE = re.compile(r"(?<![\dA-Za-z])(?:[A-Z]-)?(\d{4,8})(?![\dA-Za-z])\s*(?:is\s+your\b[^.\n]{0,30}?(?:code|pin|otp|password)|to\s+(?:verify|log\s?in|sign\s?in|confirm|reset)|for\s+(?:verification|login|sign\s?in))", re.I)  # 英文短信「482913 is your code」「Use 482913 to verify」

CODE_PWD = re.compile(r"(?:wifi|WiFi|WIFI|无线网|热点|账号)?密码\s*(?:是|为)?\s*[:：]?\s*([A-Za-z0-9][\x21-\x7e]{5,19})(?![\x21-\x7e])")  # 含符号的密码「Ab@12345」，CODE_AFTER 只认字母数字
CODE_KEYPAD = re.compile(r"(?:密码|口令|开门码|门禁码)\s*(?:是|为)?\s*[:：]?\s*([*#]\d{3,8}[*#]?|\d{3,8}[*#])")  # 门禁/快递柜键盘码「*1234#」带星井号才能按出来
CODE_PINGMA = re.compile(r"[凭憑](?:码)?\s*([A-Za-z0-9-]{3,12})\s*(?:到|去)?(?:取|领|核销|兑换)")  # “凭 8832 取件”

LINK_RX = re.compile(r"(?:https?://|(?<![A-Za-z0-9_.@/])www\.)[^\s<>\"'\]\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]*[^\s<>\"'\]\u4e00-\u9fff\u3000-\u303f\uff00-\uffef.,;:!?)]")  # 链接末尾的英文标点/右括号不属于链接

BARE_LINK = re.compile(r"(?<![A-Za-z0-9_.@/-])(?:[A-Za-z0-9-]+\.)+(?:com|cn|net|org|edu\.cn|cc|link|ly|top|me|io)/[A-Za-z0-9_./?=&%#~+-]+[A-Za-z0-9_/=#-]")  # 无协议的短链「wj.qq.com/s/123」「pan.baidu.com/s/1abc」（必须带路径，避免把「qq.com」之类文字当链接）

CODE_SPACED = re.compile(r"(?:" + CODE_WORDS + r")[^A-Za-z0-9]{0,4}(\d(?: \d){3,7})(?!\d)")  # 防抓取的单个数字间隔「验证码 8 8 2 1 3 4」，复制要合成 882134


CODE_WRAPPED = re.compile(r"([￥¥$€₤][A-Za-z0-9]{8,20}[￥¥$€₤])")  # 淘宝/拼团式整段口令「￥AbC12dEf3￥」，两端符号也要复制才能被 App 识别


ID_NUMS = re.compile(r"(?:宿舍|寝室|教室|考场|座位|楼|班|组|床|车|房)[号码]\s*[:：]?\s*[A-Za-z0-9-]+")


EMAIL_RX = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")  # 邮箱：提交材料的群通知常只给邮箱


PHONE_RX = re.compile(r"(?<!\d)(?:[(（]?\+?86[)）]?[ -]?)?(1[3-9]\d)[ -]?(\d{4})[ -]?(\d{4})(?!\d)")  # 大陆手机号：联系人类通知


LANDLINE_RX = re.compile(r"(?<![\d-])0\d{2,3}-\d{7,8}(?:-\d{1,6})?(?![\d-])")  # 座机/办公室电话「010-12345678」「0571-8765432-801」：保留连字符，拨号可直接用


TRACK_RX = re.compile(r"(?<![A-Za-z0-9])(?:[A-Z]{2,5}[A-Z]?\d{10,15}(?!\d)|[A-Z]{2}\d{9}CN(?![A-Za-z]))")  # 快递单号：2~6 位大写字母前缀 + 10~15 位数字，通吃各家快递，不再逐家加前缀；也含 EMS 式 XX123456789CN


TRACK_AFTER = re.compile(r"(?:快递|运单|物流|单号)[^\dA-Za-z]{0,6}((?:[A-Za-z]{1,5})?\d{4}(?:[ -]?\d{1,5}){2,4})(?![\dA-Za-z])")  # 「快递单号：4301234567890」纯数字 13~15 位，CODE_AFTER 只取前 12 位会复制残缺


def _copy_link(u: str) -> str:
    """复制用的链接：去尾部标点；裸 www 域名补 http://，粘贴到浏览器才能直接打开。"""
    u = u.rstrip(".,;)，。；）")
    return u if u.startswith("http") else "http://" + u


ACCOUNT_WORDS = r"[\u4e00-\u9fffA-Za-z]{0,8}(?:号|账号|ID|id)|联系电话|电话|微信|vx|wx|tg|ins|wechat|telegram|discord|line|whatsapp|instagram|支付宝|steam|github|zoom|skype|teams|slack|facebook|twitter|signal|kakao(?:talk)?|[\u4e00-\u9fffA-Za-z]{1,6}群|QQ|会议号"  # 通用「xxx号/ID」+ 常见平台名，新平台一般无需再改
ACCOUNT_RX = re.compile(r"(?:" + ACCOUNT_WORDS + r")\s*[:：]\s*([A-Za-z][A-Za-z0-9_-]{5,19}|[1-9]\d{4,11}(?!\d)|\d{16,19}(?!\d)|[1-9]\d{2,3}(?: \d{3,4}){1,2}(?!\d))", re.I)  # 银行卡 16-19 位整串，不能只截 12 位  # 加好友/入群要抄的号
ACCOUNT_LOOSE = re.compile(r"(?:群号|银行卡号?|卡号|(?:QQ|微信|钉钉|飞书|班级|年级|交流|学习)群|微信号?|QQ号?|vx|wx|加\s*[vV]|[vV]\s*信|扣扣|账号|(?<![\u4e00-\u9fffA-Za-z])(?!(?:手机|电话|学|工|订单|身份证|邮政|门牌|单|序列|房|寝室|宿舍|教室|座位)[号码])[\u4e00-\u9fffA-Za-z]{1,6}(?:号|邀请码)|腾讯会议|钉钉群|飞书会议|钉钉会议|飞书群|Zoom|zoom|Webex|Teams)\s*(?:[:：]\s*)?([A-Za-z](?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{5,19}|[1-9]\d{4,11}(?!\d)|\d{16,19}(?!\d))", re.I)  # 「群号 123456789」不带冒号也常见；只限最明确的几个词，避免误配


FULLWIDTH = {c: c - 0xFEE0 for c in range(0xFF10, 0xFF5B) if chr(c).isalnum()}
FULLWIDTH.update({0xFF20: 64, 0xFF0E: 46})  # 全角＠、．也折半角：邮箱「abc＠qq．com」才复制得出
# 全角数字/字母「１２３４５６」折成半角，口令才认得出


def _whole_digits(txt, m):
    """分组书写的数字（123 456 789）取整串；其余原样。"""
    c = m.group(1)
    sp = SPLIT_DIGITS.match(txt, m.start(1)) if c.isdigit() else None
    if sp and sp.group(0).startswith(c) and len(sp.group(0)) > len(c):
        return re.sub(r"[ -]", "", sp.group(0))
    return c


MEETING_ID = re.compile(r"(?:会议\s?(?:号|ID|id)|(?i:meeting\s?(?:ID|number|no\.?)))\s*[:：是为]?\s*#?(\d{3}[ -]?\d{3,4}[ -]?\d{3,5})(?!\d)")  # 会议号和密码并存：先复制会议号（进会先输号，密码在群里再看）


def pick_copy(txt: str, link, title: str) -> str:
    """一键复制的内容，按优先级：口令/验证码 > 链接 > 微信/QQ号 > 邮箱 > 手机号 > 标题。"""
    txt = txt.translate(FULLWIDTH)
    track = TRACK_AFTER.search(txt)
    if track:
        return re.sub(r"[ -]", "", track.group(1))  # 「4301 2345 6789 0」分组写法复制整串
    wrapped = CODE_WRAPPED.search(txt)
    if wrapped:
        return wrapped.group(1)
    spaced = CODE_SPACED.search(txt)
    if spaced:
        return spaced.group(1).replace(" ", "")
    mt = MEETING_ID.search(txt)
    if mt:
        return re.sub(r"[ -]", "", mt.group(1))
    code = CODE_SECRET.search(txt) or CODE_KEYPAD.search(txt) or CODE_PHRASE.search(txt) or CODE_PWD.search(txt) or CODE_AFTER.search(txt) or CODE_PINGMA.search(txt) or CODE_BEFORE.search(txt) or CODE_EN_BEFORE.search(txt)
    if code:
        c = _whole_digits(txt, code)
        if c != code.group(1):
            return c  # 「会议号 123 456 789」分组书写：整串复制，别只剩 123
        return c.replace("-", "") if re.fullmatch(r"\d{3}-\d{3}|\d{3}-\d{3,4}-\d{3,4}", c) else c  # 会议号「123-456-789」粘贴到会议 App 要纯数字
    if link:
        return _copy_link(link.group(0))
    bare = BARE_LINK.search(txt)
    if bare:
        return _copy_link(bare.group(0))
    acc = ACCOUNT_RX.search(txt) or ACCOUNT_LOOSE.search(txt)
    if acc:
        return _whole_digits(txt, acc).replace(" ", "")  # 「群号：123 456 789」分组书写同样整串复制
    for rx in (EMAIL_RX, TRACK_RX, PHONE_RX, LANDLINE_RX):
        m = rx.search(txt)
        if m:
            return "".join(m.groups()) if rx is PHONE_RX else m.group(0)
    return clip(title, 60)
