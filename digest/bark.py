"""Bark 传输层（0.34.24 从 app.py 拆出）：纯发送，不碰数据库、不 import app。"""
import asyncio
import html
import re
import time
import unicodedata

import httpx


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
    return t[:cut].rstrip("，,、：:；;（([【“\"‘ ") + "…"  # 截断处不留悬空标点/半个括号


def norm_bark_url(u: str) -> str:
    """用户常把 Bark App 里的示例整条粘进来（…/KEY/内容?group=x），只留 服务器/KEY。"""
    from urllib.parse import urlsplit
    u = (u or "").strip()
    if u and "://" not in u and "." in u.split("/")[0]:
        h = u.split("/")[0]
        # 漏写协议：域名补 https（api.day.app/KEY）；IP 或带端口的自建服务多半没证书，补 http
        selfhost = ":" in h or re.fullmatch(r"[\d.]+", h)
        u = ("http://" if selfhost else "https://") + u
    sp = urlsplit(u)
    if not sp.scheme or not sp.netloc:
        return u.rstrip("/")
    seg = [x for x in sp.path.split("/") if x]
    return f"{sp.scheme}://{sp.netloc}" + ("/" + seg[0] if seg else "")


CQ_LABEL = {"record": "[语音]", "video": "[视频]", "forward": "[卡片]", "json": "[卡片]", "xml": "[卡片]",
            "file": "[文件]", "music": "[音乐]", "share": "[链接]", "dice": "[骰子]", "rps": "[猜拳]", "image": "[图片]", "face": "[表情]", "mface": "[表情]", "bface": "[表情]"}


WX_EMOJI = "微笑|撇嘴|色|发呆|得意|流泪|害羞|闭嘴|睡|大哭|尴尬|发怒|调皮|呲牙|惊讶|难过|酷|冷汗|抓狂|吐|偷笑|愉快|白眼|傲慢|困|惊恐|流汗|憨笑|悠闲|奋斗|咒骂|疑问|嘘|晕|衰|骷髅|敲打|再见|擦汗|抠鼻|鼓掌|坏笑|左哼哼|右哼哼|哈欠|鄙视|委屈|快哭了|阴险|亲亲|可怜|笑脸|生病|脸红|破涕为笑|恐惧|失望|无语|嘿哈|捂脸|奸笑|机智|皱眉|耶|吃瓜|加油|汗|天啊|Emm|社会社会|旺柴|好的|打脸|哇|翻白眼|裂开|苦涩|让我看看|叹气|合十|强|握手|胜利|抱拳|OK|拳头|爱心|心碎|玫瑰|凋谢|赞|踩"


def _cq(m) -> str:
    return CQ_LABEL.get(m.group(1), "")  # QQ 码统一一张表：有可读含义的留占位，其余去掉


# 占位别名表：每行 = 规范占位: 别名正则（|分隔）。新占位只往这里加一行。
EN_ALIAS = {
    "[图片]": 'Photo|照片|Image|Picture|闪照|图片消息|Screenshot|截图|相册|Album|Live Photo|实况照片|群相册|Group Album|Photo Album',
    "[语音]": "(?:Voice(?: Message)?|语音|语音消息)(?:\\s*\\d+\\s*(?:''|'|″|′|秒|s))?|语音消息|Audio|语音通话|语音条|Voice Note|Voice Memo|语音留言",
    "[视频]": 'Video|Video Clip|Short Video|小视频|视频号|视频消息|短视频|视频通话|Screen Recording|录屏',
    "[文件]": 'File|Document|Attachment|文件消息|文档|压缩包|附件|Group File|群文件|Folder|文件夹|Spreadsheet|表格文件|PDF|Word|Excel|PPT|Presentation|Zip|Archive|压缩文件',
    "[位置]": 'Location|Live Location|Map|位置信息|地理位置|实时位置|位置共享|Navigation|导航分享|Shared Location',
    "[名片]": 'Contact|Business Card|Name Card|个人名片|联系人名片|Contact Card|Shared Contact|推荐好友',
    "[链接]": 'Link|Share|分享链接|网页链接|链接分享|Article|Shared Article|公众号文章|文章分享',
    "[表情]": "Sticker|Emoji|Animated Sticker|动画表情|动态表情|大表情|Meme|表情包|斗图|GIF|动图|Emoticon|贴纸|Dice|骰子|猜拳|Rock Paper Scissors|魔法表情|" + WX_EMOJI,
    "[小程序]": 'Mini[ -]?Program|Mini Game|小游戏|小程序消息|小程序卡片',
    "[音乐]": 'Music|Song|Music Card|音乐分享|音乐消息|歌曲分享',
    "[红包]": 'Red Packet|Lucky Money|Red Envelope|Hongbao|拼手气红包|口令红包|微信红包|QQ红包|红包消息|红包封面|Red Packet Cover',
    "[聊天记录]": 'Forwarded Message|Forwarded Messages|合并转发|Chat History|Chat Record|聊天记录转发|转发的聊天记录',
    "[转账]": 'Transfer|Money Transfer|微信转账|转账消息|Payment|付款|收款|Payment Request|AA收款|群收款',
    "[投票]": 'Poll|Vote|群投票|投票消息|Survey|问卷|Questionnaire|表单|Form|填表',
    "[接龙]": 'Solitaire|Group Note|群接龙|接龙消息',
    "[通话]": 'Call|Missed Call|Video Call|未接通话|未接来电|通话请求|Group Call|群通话|语音聊天|Voice Chat|Meeting|腾讯会议|会议邀请',
    "[直播]": 'Live|Livestream|Live Stream|直播间|直播分享|直播邀请',
    "[公告]": 'Announcement|Group Announcement|群公告|公告消息',
    "[日程]": 'Calendar Event|Group Schedule|群日程|日程提醒',
    "[笔记]": 'Note|Notes|Group Note Doc|笔记分享|群笔记|云笔记',
    "[二维码]": 'QR Code|QR|二维码图片|群二维码|收款码|Payment Code',
    "[礼物]": 'Gift|Group Gift|礼物|群礼物|礼物消息',
}
EN_ALIAS_RX = re.compile(r"\[(?:" + "|".join(f"(?P<g{i}>{v})" for i, v in enumerate(EN_ALIAS.values())) + r")\]")


def _en_alias(m) -> str:
    return list(EN_ALIAS)[int(m.lastgroup[1:])]


def _first(m) -> str:
    return m.group(1)


_PLACEHOLDERS = r"\[(?:" + "|".join(k[1:-1] for k in EN_ALIAS) + r"|卡片)\]"  # 连发折叠覆盖 EN_ALIAS 全部占位
# 顺序即语义：先还原/清标记，再折叠占位与重复，最后规整空白。新规则只往表里加一行。
STRIP_RULES = [(re.compile(p, f), r) for p, r, f in [
    (r"[\u200b-\u200d\u2060\ufeff\u00ad\u180e\u200e\u200f\u202a-\u202e]", "", 0),  # 零宽字符：锁屏是看不见的空档，还会让取件码匹配失败
    (r"\r\n?|[\u2028\u2029\x0b\x0c\x85]", "\n", 0),  # 换行统一（含 Unicode 行/段分隔符、垂直制表），免得重复行折叠和列表符匹配被 \r 挡住
    (r"(?i)<br\s*/?>", "\n", 0),  # 转发文本里的 <br> 换成真换行
    (r"(?i)</?(?:b|i|u|p|div|span|font|strong|em|a|h[1-6]|li|ul|ol|table|tr|td|th|img|code|pre)\b[^>]*>", "", 0),
    (r"\[CQ:(\w+)[^\]]*\]", _cq, 0),
    (r"(?s)^\s*「[^」]*」\s*\n\s*(?:-\s*){6,}\n?", "", 0),  # 微信引用回复自带“「昵称：原话」+ 虚线”
    (r"(?m)^\s*(?:#{1,6}|[-*>]|\d{1,2}[.、)])\s+", "", 0),  # 标题/列表/引用前缀
    (r"(?m)^\s*[①-⑩⓵-⓾•·▪●○◆◇■□▶►➤➡✔✅☑]\s*", "", 0),  # 圆圈数字/圆点/箭头/对勾列表符
    (r"!\[[^\]]*\]\([^)]*\)", "[图片]", 0),  # Markdown 图片 ![alt](url) 统一成占位，不留感叹号+alt
    (r"\[([^\]]+)\]\([^)]*\)", r"\1", 0),
    (r"https?://[\x21-\x7e]*[\x21-\x7e](?<![.,;:!?)\]])", "[链接]", 0),  # 只吃 ASCII，网址后紧跟的中文不被一起吞掉
    (r"(?<![A-Za-z0-9_.@/])www\.[\x21-\x7e]+", "[链接]", 0),
    (r"(\[链接\][\s,，、]*){2,}", "[链接] ", 0),  # 多个网址只留一个占位
    (r"(?m)^\s*(?:[-*_=]{3,}|\|?[\s:|-]{3,}\|)\s*$", "", 0),  # 分隔线/表格对齐行
    (r"(?m)^[ \t]*\|(.+?)\|?[ \t]*$", lambda m: re.sub(r"\s*\|\s*", " ", m.group(1)).strip(), 0),  # 表格行 | a | b | 变成「a b」，锁屏不露竖线
    (r"\*\*|__|~~|`", "", 0),  # 粗体/删除线/代码标记
    (r"(?<![\w*])\*([^\s*][^*\n]*?)\*(?![\w*])", r"\1", 0),  # 斜体 *x*（a*b 不动）
    (r"([!！?？。.~～])\1{2,}", r"\1\1", 0),  # 刷屏标点压成两个
    (r"([哈啊嘿呵])\1{3,}", r"\1\1\1", 0),
    (r"([\U0001F300-\U0001FAFF\u2600-\u27BF])\1{2,}", r"\1\1", 0),  # 刷屏表情压成两个
    (r"(?:…|\.){4,}", "……", 0),
    (EN_ALIAS_RX.pattern, _en_alias, 0),  # 英文/别名占位统一查 EN_ALIAS 表
    (r"(" + _PLACEHOLDERS + r")(?:\s*\1)+", _first, 0),  # 连发同类占位只留一个
    (r"(?m)^(.+)\n(?:\1\n?)+", lambda m: m.group(1) + "\n", 0),  # 复读连着几行一样的只留一行
    (r"@(?:全体成员|所有人)\s*", "", 0),  # 任意位置的 @全体成员/@所有人（紧急度已由级别表达）
    (r"^\s*(?:(?:[\[【(（]\s*(?:转发|Fwd?|自动回复|Re)\s*[\]】)）]\s*[:：]?|(?:转发|Fwd?|Re)\s*[:：])\s*)+", "", re.I),  # 转发/回复前缀
    (r"(?m)[\s—-]*(?:发自|来自|Sent from)\s*(?:我的)?\s*(?:iPhone|iPad|Android|微信|QQ|手机)\S*\s*$", "", 0),  # 签名尾巴
    (r"[【\[（(]\s*[】\]）)]", "", 0),  # 空括号（图片/表情被剥掉后留下的【】）锁屏上是噪点
    (r"[\u2002-\u200a\u00a0\u202f]", " ", 0),  # 特殊空格统一成普通空格
    (r"[ \u3000\t]{2,}", " ", 0),  # 连续空格折成一个
]]


def strip_md(t: str) -> str:
    """Bark 不渲染 Markdown，LLM/转发带的标记会原样露在锁屏上：按 STRIP_RULES 顺序清掉。"""
    t = t or ""
    for i, (rx, rep) in enumerate(STRIP_RULES):
        t = rx.sub(rep, t)
        if i == 1:
            t = html.unescape(t)  # 换行统一后再解 &amp; &lt;，其后的标签规则才看得见 &lt;b&gt; 还原出的标签
    return t.strip()


CODE_WORDS = r"(?!(?:群|学|手机|电话|工|QQ|微信|房间|门牌|身份证|邮政|订单)[码号])[\u4e00-\u9fff]{1,3}[码号]|快递单号|口令|暗号|密码|会议ID|(?i:pwd|passcode|code)"  # 任何「X码/X号」都算口令，不再一个个枚举（手机号等超 8 位的数字天然匹配不上）
CODE_AFTER = re.compile(r"(?:" + CODE_WORDS + r")[^A-Za-z0-9]{0,4}((?=[A-Za-z-]*\d)[A-Za-z0-9-]{3,12})")  # 3 位也认但必须含数字：食堂「取餐号 A12」
CODE_SECRET = re.compile(r"(?:提取码|密码|口令|暗号)[^A-Za-z0-9]{0,4}([A-Za-z]{4,8})(?![A-Za-z])")  # 网盘提取码常是纯字母 abcd
SPLIT_DIGITS = re.compile(r"(?<!\d)(\d{3,4})((?: \d{3,4})+)(?!\d)")  # 会议号「123 456 789」分组书写，复制要整串
CODE_BEFORE = re.compile(r"(?<![\dA-Za-z])(\d{4,8})(?![\dA-Za-z])\D{0,6}(?:是|为)?(?:你的|您的)?(?:" + CODE_WORDS + r")")  # “123456 为您的验证码”

CODE_PINGMA = re.compile(r"[凭憑](?:码)?\s*([A-Za-z0-9-]{3,12})\s*(?:到|去)?(?:取|领|核销|兑换)")  # “凭 8832 取件”

LINK_RX = re.compile(r"(?:https?://|(?<![A-Za-z0-9_.@/])www\.)[^\s<>\"'\]\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]*[^\s<>\"'\]\u4e00-\u9fff\u3000-\u303f\uff00-\uffef.,;:!?)]")  # 链接末尾的英文标点/右括号不属于链接

ID_NUMS = re.compile(r"(?:宿舍|寝室|教室|考场|座位|楼|班|组|床|车|房)[号码]\s*[:：]?\s*[A-Za-z0-9-]+")


EMAIL_RX = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")  # 邮箱：提交材料的群通知常只给邮箱


PHONE_RX = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")  # 大陆手机号：联系人类通知


TRACK_RX = re.compile(r"(?<![A-Za-z0-9])(?:SF|JD|YT|JT|ZT|YD)[A-Z]?\d{10,15}(?!\d)")  # 快递单号常不带「单号」二字：顺丰SF…/京东JD…


def _copy_link(u: str) -> str:
    """复制用的链接：去尾部标点；裸 www 域名补 http://，粘贴到浏览器才能直接打开。"""
    u = u.rstrip(".,;)，。；）")
    return u if u.startswith("http") else "http://" + u


ACCOUNT_WORDS = r"[\u4e00-\u9fffA-Za-z]{0,8}(?:号|账号|ID|id)|联系电话|电话|微信|vx|wx|tg|ins|wechat|telegram|discord|line|whatsapp|instagram|支付宝|steam|github|zoom|skype|teams|slack|facebook|twitter|signal|kakao(?:talk)?|[\u4e00-\u9fff]{2,6}群|QQ|会议号"  # 通用「xxx号/ID」+ 常见平台名，新平台一般无需再改
ACCOUNT_RX = re.compile(r"(?:" + ACCOUNT_WORDS + r")\s*[:：]\s*([A-Za-z][A-Za-z0-9_-]{5,19}|[1-9]\d{4,11})", re.I)  # 加好友/入群要抄的号


def pick_copy(txt: str, link, title: str) -> str:
    """一键复制的内容，按优先级：口令/验证码 > 链接 > 微信/QQ号 > 邮箱 > 手机号 > 标题。"""
    code = CODE_AFTER.search(txt) or CODE_SECRET.search(txt) or CODE_PINGMA.search(txt) or CODE_BEFORE.search(txt)
    if code:
        return code.group(1)
    if link:
        return _copy_link(link.group(0))
    acc = ACCOUNT_RX.search(txt)
    if acc:
        return acc.group(1)
    for rx in (EMAIL_RX, TRACK_RX, PHONE_RX):
        m = rx.search(txt)
        if m:
            return m.group(0)
    return clip(title, 60)


def build_payload(title: str, body: str, level: str = "active", site_url: str = "", path: str = "/", badge: int = -1, group: str = "") -> dict:
    """纯函数：把标题/正文整理成 Bark 请求体（清理、去重、级别、复制内容、点开地址）。发送与重试在 send_bark。"""
    link = LINK_RX.search(body or "")  # 正文里的链接锁屏上折成[链接]，没有口令时改为复制原链接
    title = strip_md(title).strip() or "群报"  # 空标题的推送在锁屏上像乱码，兜底成 App 名
    body = re.sub(r"\n\s*\n+", "\n", strip_md(body).strip()) or title  # 空行折掉
    if body != title and body.startswith(title) and len(body) > len(title) + 2:
        body = body[len(title):].lstrip("：:，,。 \n") or body  # 正文以标题开头时去掉重复
    payload = {"title": clip(title, 60), "body": clip(body, 300), "group": clip(group, 30) or "群报", "isArchive": "1"}  # 存进 Bark 历史
    if level in ("timeSensitive", "passive"):
        payload["level"] = level
    if level == "timeSensitive":
        payload.update(sound="alarm", volume=8, call="1")  # 紧急：不同铃声、拉高音量、循环响铃
    txt = SPLIT_DIGITS.sub(lambda m: m.group(1) + m.group(2).replace(" ", ""), unicodedata.normalize("NFKC", title + " " + body))  # 全角数字/冒号先归一，否则「１２３４５６」认不出
    txt = ID_NUMS.sub(" ", txt)  # 寝室号/考场号等位置类数字不是口令
    payload["copy"] = pick_copy(txt, link, title)  # 不开 autoCopy，用户长按复制
    if badge >= 0:
        payload["badge"] = badge  # 角标 = 未完成待办数
    if site_url:
        site = site_url.rstrip("/")
        payload["url"] = site + path if path != "/" else site_url
        payload["icon"] = site + "/icon.png"
    return payload


DEDUPE_SECS = 60  # 同一条推送的去重窗口
_RECENT: dict = {}  # (地址, 标题, 正文) → 上次成功时间；同一条 DEDUPE_SECS 秒内不重复推（重试/多路触发时手机不连震）


def _is_dup(key: tuple, now: float) -> bool:
    for k in [k for k, t in _RECENT.items() if now - t > DEDUPE_SECS]:
        del _RECENT[k]
    return key in _RECENT


LAST_ERR = ""  # 最近一次失败的原因（测试推送时直接告诉用户，不让人猜）


async def send_bark(burl: str, title: str, body: str, level: str = "active", site_url: str = "", path: str = "/", badge: int = -1, group: str = "", dedupe: bool = True) -> bool:
    """发一条 Bark。level：timeSensitive / active / passive。"""
    global LAST_ERR
    LAST_ERR = ""
    burl = norm_bark_url(burl)
    if not burl:
        LAST_ERR = "地址为空"
        return False
    payload = build_payload(title, body, level, site_url, path, badge, group)
    key = (burl, payload["title"], payload["body"])
    if dedupe and _is_dup(key, time.monotonic()):  # 测试推送不去重：连点两下也要真发
        return True  # 刚推过（或正在推）一模一样的，视为已送达
    if dedupe:
        _RECENT[key] = time.monotonic()  # 先占位：多路同时触发同一条时，后到的不再重复发
    # 大陆服务器连 Bark 偶发超时：失败（网络错/5xx）自动再试两次，4xx（地址错）不重试，429 限流除外
    async with httpx.AsyncClient(timeout=httpx.Timeout(8, connect=4)) as cl:  # 三次共用一个连接；连不上 4 秒就重试，不干等 8 秒
        wait = 0.0
        tries = 5 if level == "timeSensitive" else 3  # 紧急推送丢了代价最大，多试两次
        for i in range(tries):
            wait = (1.0 if level == "timeSensitive" else 1.5) * (i + 1)  # 紧急的间隔更短，5 次总等待也就 10 秒内
            try:
                r = await cl.post(burl, json=payload)
                if r.status_code < 300:
                    _RECENT[key] = time.monotonic()
                    return True
                LAST_ERR = f"Bark 返回 {r.status_code}：{r.text[:60]}"
                print(LAST_ERR)  # 写进 docker logs，地址/Key 填错一眼能看出
                if r.status_code < 500 and r.status_code != 429:  # 429 限流也值得再试
                    _RECENT.pop(key, None)
                    return False
                if r.status_code == 429 and str(r.headers.get('retry-after', '')).isdigit():
                    wait = min(int(r.headers['retry-after']), 5)  # 服务端说等几秒就等几秒，最多 5 秒
            except Exception as ex:
                LAST_ERR = f"连不上 Bark 服务器（{type(ex).__name__}）"
                print(f"Bark 推送失败（第{i+1}次）：{type(ex).__name__} {ex}")  # 超时异常 str 为空，带上类型才看得出原因
            if i < tries - 1:
                await asyncio.sleep(wait)
    _RECENT.pop(key, None)  # 没送达就释放占位，允许之后重试
    return False
