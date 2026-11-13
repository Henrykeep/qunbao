"""Bark 传输层（0.34.24 从 app.py 拆出）：纯发送，不碰数据库、不 import app。"""
import asyncio
import html
import random
import re
import time
import unicodedata
from urllib.parse import quote  # noqa: F401

import httpx


from bark_url import norm_bark_url, has_key, open_url  # noqa: F401  地址清洗在 bark_url.py


CQ_LABEL = {"record": "[语音]", "video": "[视频]", "forward": "[卡片]", "json": "[卡片]", "xml": "[卡片]",
            "file": "[文件]", "music": "[音乐]", "share": "[链接]", "dice": "[骰子]", "rps": "[猜拳]", "image": "[图片]", "face": "[表情]", "mface": "[表情]", "bface": "[表情]"}


from bark_alias import EN_ALIAS, WX_EMOJI  # noqa: F401  别名表在 bark_alias.py

def _cq(m) -> str:
    return CQ_LABEL.get(m.group(1), "")  # QQ 码统一一张表：有可读含义的留占位，其余去掉


EN_ALIAS_RX = re.compile(r"\[(?:" + "|".join(f"(?P<g{i}>{v})" for i, v in enumerate(EN_ALIAS.values())) + r")\]")


def _en_alias(m) -> str:
    return list(EN_ALIAS)[int(m.lastgroup[1:])]


def _first(m) -> str:
    return m.group(1)


_PLACEHOLDERS = r"\[(?:" + "|".join(k[1:-1] for k in EN_ALIAS) + r"|卡片)\]"  # 连发折叠覆盖 EN_ALIAS 全部占位
# 顺序即语义：先还原/清标记，再折叠占位与重复，最后规整空白。新规则只往表里加一行。
# 消息末尾「（已X）」类修改标记的 X；以后只在这里加词，不再新增规则
EDIT_MARKS = ("编辑 修改 更新 更正 补充 补发 修订 重发 重新发送 置顶 转发 撤回 修正 确认 更改 调整 核对 完善 订正 补正 补全 优化 校对 勘误 补齐 追加 变更 增补 更换 改正 重编辑").split()

PLACEHOLDER_KEYWORDS = [
    ("[快递]", "快递|驿站|取件|包裹|物流|菜鸟|顺丰|中通|圆通|韵达|京东快递|丰巢|取货码"),
    ("[文件]", "文档|表格|文件|收集表|问卷|课件|PPT|PDF|Word|Excel|压缩包|腾讯文档|石墨|飞书文档|金山文档|幻灯片|讲义|论文|简历|报名表|表单|通知单|电子版|扫描件|任务书|议程|策划案|海报"),
    ("[图片]", "照片|相片|图片|截图|截屏|动图|海报|二维码"),
    ("[语音]", "语音|录音"),
    ("[视频]", "视频|录像|短片"),
    ("[链接]", "小程序|Mini ?Program|公众号|直播|音乐|歌曲|文章|推文|网页|新闻|视频号|小红书|抖音|B站|哔哩哔哩|微博|知乎|网盘|百度网盘|夸克|阿里云盘"),
    ("[位置]", "位置|定位|地点|地图|导航|实时共享"),
    ("[红包]", "红包|压岁钱"),
    ("[转账]", "转账|收款|付款|汇款"),
    ("[表情]", "表情|贴纸|骰子|猜拳|魔法"),
    ("[卡片]", "名片|联系人|群聊邀请|聊天记录|投票|接龙|群公告|会议邀请|腾讯会议|在线表格|分享|待办|打卡|相册"),
]

# 扩展名按「一行一类」登记成列表（mpe?g/jpe?g/tiff?/aiff? 是可选字母写法），生成正则与测试都读这张表
EXT_WORDS = {
    "[视频]": "mp4 mov avi mkv flv wmv m4v 3gp webm mpe?g rmvb f4v mts vob ogv rm".split(),
    "[语音]": "mp3 m4a wav flac aac amr silk ogg opus wma ape m4r mid midi aiff? mka m4b weba".split(),
    "[图片]": "jpe?g png gif webp heic bmp tiff? svg avif ico dng raw psd heif jfif jp2 heics tga apng jxl pcx".split(),
}
EXT_LABELS = [(label, "|".join(words)) for label, words in EXT_WORDS.items()]

def _weekday_run(t: str) -> str:
    """「周一、周二、周三」这种连续星期列举折成区间；不连续（周一、周三、周五）原样返回。"""
    ds = re.findall(r"周([一二三四五六日])", t)
    idx = ["一二三四五六日".index(d) for d in ds]
    if all(b - a == 1 for a, b in zip(idx, idx[1:])):
        return f"周{ds[0]}-周{ds[-1]}"
    return t


def _cn2n(t: str) -> int:
    """1–39 的汉字数 → 整数（十=10，二十五=25）。"""
    d = "一二三四五六七八九"
    if "十" in t:
        a, b = t.split("十")
        return (d.index(a) + 1 if a else 1) * 10 + (d.index(b) + 1 if b else 0)
    return d.index(t) + 1


def _this_year(m) -> str:
    import datetime
    return "" if int(m.group(1)) == datetime.date.today().year else m.group(0)


def _sep(m):
    """时段词被吃掉后，前面若紧挨数字（「11/13」+「15:00」）补个空格，免得粘成一串。"""
    return " " if m.start() and m.string[m.start() - 1].isdigit() else ""


def _hour24(period, h):
    """时段词+钟点 → 24 小时制钟点文字（时段词丢弃）。已是 13–23 的不再加 12；超范围原样返回。"""
    pm = period in ("下午", "晚上", "晚间", "夜里", "夜晚", "傍晚", "晚") or period in ("深夜", "半夜") and 6 <= h <= 11 or period == "中午" and 1 <= h <= 3  # 「深夜11点」=23点；「半夜3点」仍是凌晨；「中午1点」=13点（「中午11点」「中午12点」不加）
    if pm and 1 <= h <= 11:
        h += 12
    elif period in ("晚上", "晚间", "夜里", "夜晚", "晚") and h == 12 or period in ("凌晨", "深夜", "半夜") and h == 12:  # 「晚上12点」是午夜、「凌晨12点」同，24 小时制没有 12 点这个午夜：保留时段免得读成中午
        return period + str(h)
    return str(h)


def _range_end(period, h):
    """区间右端：「晚上10点到12点」的 12 是午夜，写成 24点，不要「22点-晚上12点」。"""
    return "24" if h == 12 and period in ("晚上", "晚间", "晚", "深夜") else _hour24(period, h)


def _en_period(ap, h):
    """英文 AM/PM → 中文时段词再走 _hour24：12am 是午夜（保留「凌晨12点」免得读成中午），12pm 是正午。"""
    return _hour24("下午" if ap in "Pp" else "凌晨" if h == 12 else "上午", h)


_PERIOD_RX = r"(上午|早上|早晨|清晨|凌晨|深夜|半夜|中午|下午|晚上|晚间|夜里|夜晚|傍晚|(?<![今明昨后前每])[早晚])"  # 时段词（单字「早/晚」放最后；前面是今明后等时「明早8点」保留，免得变成「明8点」）

STRIP_RULES = [(re.compile(p, f), r) for p, r, f in [
    (r"[\u200b-\u200d\u2060\ufeff\u00ad\u180e\u200e\u200f\u202a-\u202e]", "", 0),  # 零宽字符：锁屏是看不见的空档，还会让取件码匹配失败
    (r"\r\n?|[\u2028\u2029\x0b\x0c\x85]", "\n", 0),  # 换行统一（含 Unicode 行/段分隔符、垂直制表），免得重复行折叠和列表符匹配被 \r 挡住
    (r"(?i)<br\s*/?>", "\n", 0),  # 转发文本里的 <br> 换成真换行
    (r"(?i)</?(?:b|i|u|p|div|span|font|strong|em|a|h[1-6]|li|ul|ol|table|tr|td|th|img|code|pre)\b[^>]*>", "", 0),
    (r"\[CQ:(\w+)[^\]]*\]", _cq, 0),
    (r"(?s)^\s*「[^」]*」\s*\n\s*(?:-\s*){6,}\n?", "", 0),  # 微信引用回复自带“「昵称：原话」+ 虚线”
    (r"(?m)^\s*(?:(?:#{1,6}|[-*>]|\d{1,2}[.、)）])\s+|[(（]\d{1,2}[)）]\s*(?=\S))", "", 0),  # 标题/列表/引用前缀（含 （1）/(1) 全半角序号）
    (r"(?m)^\s*[①-⑩⓵-⓾•·▪●○◆◇■□▶►➤➡✔✅☑]\s*", "", 0),  # 圆圈数字/圆点/箭头/对勾列表符
    (r"!\[[^\]]*\]\([^)]*\)", "[图片]", 0),  # Markdown 图片 ![alt](url) 统一成占位，不留感叹号+alt
    (r"\[([^\]]+)\]\([^)]*\)", r"\1", 0),
    (r"<?https?://[^\s<>\"'\u0080-\uffff]*[^\s<>\"'\u0080-\uffff.,;:!?)\]]>?", "[链接]", 0),  # 只吃 ASCII，网址后紧跟的中文不被一起吞掉
    (r"(?<![A-Za-z0-9_.@/])www\.[\x21-\x7e]+", "[链接]", 0),
    (r"(\[链接\][\s,，、]*){2,}", "[链接] ", 0),  # 多个网址只留一个占位
    (r"(?:链接|网址|网页|地址)\s*[:：]\s*(?=\[链接\])", "", 0),  # 「链接：[链接]」只留占位
    (r"(?m)^\s*(?:[-*_=]{3,}|\|?[\s:|-]{3,}\|)\s*$", "", 0),  # 分隔线/表格对齐行
    (r"(?m)^[ \t]*\|(.+?)\|?[ \t]*$", lambda m: re.sub(r"\s*\|\s*", " ", m.group(1)).strip(), 0),  # 表格行 | a | b | 变成「a b」，锁屏不露竖线
    (r"\*\*|__|~~|`", "", 0),  # 粗体/删除线/代码标记
    (r"(?<![\w*])\*([^\s*][^*\n]*?)\*(?![\w*])", r"\1", 0),  # 斜体 *x*（a*b 不动）
    (r"([!！?？。.~～])\1{2,}", r"\1\1", 0),  # 刷屏标点压成两个
    (r"([，、；：,;])\1+", r"\1", 0),  # 去掉占位/标记后常留下「，，」「、、」
    (r"([哈啊嘿呵])\1{3,}", r"\1\1\1", 0),
    (r"([\U0001F300-\U0001FAFF\u2600-\u27BF]\uFE0F?)\1{2,}", r"\1\1", 0),  # 刷屏表情压成两个
    (r"(?:…|\.){4,}", "……", 0),
    (EN_ALIAS_RX.pattern, _en_alias, re.I),  # 英文/别名占位统一查 EN_ALIAS 表（大小写不敏感：[PHOTO]/[voice] 也认）
    (r"【([^【】\n]{1,80}\.[A-Za-z][A-Za-z0-9]{1,4})】", r"[\1]", 0),  # 全角【文件名.扩展名】先换成半角，后面的文件/图片/语音规则只写一份
    *[(r"\[[^\[\]\n]{1,80}\.(?:" + exts + r")\]", label, re.I) for label, exts in EXT_LABELS],  # 方括号文件名按扩展名归类，只需登记视频/语音/图片后缀，其余扩展名由下一条兜底为 [文件]
    (r"\[[^\[\]\n]{1,80}\.[A-Za-z][A-Za-z0-9]{1,4}\]", "[文件]", 0),  # 兜底：方括号内「名字.扩展名」一律 [文件]，不再逐个登记后缀
    *[(r"\[[^\[\]\n]{0,8}(?:" + kw + r")[^\[\]\n]{0,8}\]", label, re.I) for label, kw in PLACEHOLDER_KEYWORDS],  # 含关键词的未登记占位按语义归类，新增只改 PLACEHOLDER_KEYWORDS
    (r"\[[^\[\]\n]{1,20}(?:表|单|册|书|稿|纪要|安排|名单|报告|手册|通知|证|卷|资料|大纲|课件|讲义)\]", "[文件]", 0),  # 文书类后缀（课表/成绩单/任务书/实验报告…）一条规则兜住，不再逐词登记
    (r"(" + _PLACEHOLDERS + r")\s*[x×*]\s*\d{1,3}\b", _first, 0),  # 「[图片]×3」「[图片] x3」只留占位
    (r"(\[(?:语音|视频)\])\s*\d{1,3}(?:[\"″秒sS'′]|:\d{2}|分\d{0,2}秒?)", _first, 0),  # 「[语音] 3\"」「[视频] 0:12」时长不露在锁屏
    (r"(" + _PLACEHOLDERS + r")(?:\s*\1)+", _first, 0),  # 连发同类占位只留一个
    (r"(?m)^(.+)\n(?:\1\n?)+", lambda m: m.group(1) + "\n", 0),  # 复读连着几行一样的只留一行
    (r"(?i)[@＠](?:全体成员|所有人|everyone|all(?![a-z]))\s*", "", 0),  # 任意位置的 @全体成员/@所有人（紧急度已由级别表达）
    (r"^\s*(?:(?:[\[【(（]\s*(?:转发|Fwd?|自动回复|Re)\s*[\]】)）]\s*[:：]?|(?:转发|Fwd?|Re)\s*[:：])\s*)+", "", re.I),  # 转发/回复前缀
    (r"(?m)[\s—-]*(?:(?:发自|发送自|Sent from|Sent via)\s*(?:我的)?|(?:来自|Get)\s*我的|Get(?=\s+(?:Outlook|Gmail)))\s*(?:iPhone|iPad|Android|微信|QQ|手机|小米|华为|荣耀|OPPO|vivo|三星|Mac|Windows|网易邮箱|QQ邮箱|一加|真我|realme|iQOO|魅族|红米|Redmi|Pixel|Galaxy|Linux|iOS|客户端|Outlook|Gmail|WeChat|钉钉|飞书|Telegram|邮箱大师|Mail)[^\n]{0,20}$", "", 0),  # 签名尾巴（「来自小米的通知」这类正文不是签名：来自/Get 必须带「我的」或是 Outlook/Gmail）
    (r"(?m)(?:^\s*|\s*)[(（\[【]\s*(?:已(?:" + "|".join(EDIT_MARKS) + r")(?:版)?|编辑过|edited)\s*[)）\]】]\s*(?:$|[:：]?\s*)", "", re.I),  # 「（已编辑）」「【已更新】」改动标记放开头或末尾都是噪点，只留正文
    (r"[【\[（(「『《]\s*[】\]）)」』》]", "", 0),  # 空括号/空引号（图片/表情被剥掉后留下的【】）锁屏上是噪点
    (r"(?<=\S)\s*[(（]\s*\d+(?:\.\d+)?\s*(?:[KMG]B|字节)\s*[)）]", "", re.I),  # 文件消息「期末.pdf (2.3MB)」的大小括号对锁屏没用，文件名留着
    (r"(\.[A-Za-z0-9]{2,4})\s+\d+(?:\.\d+)?\s*[KMG]B\b", r"\1", re.I),  # 无括号的「期末.pdf 2.3MB」也只留文件名
    (r"[０-９Ａ-Ｚａ-ｚ]+", lambda m: m.group(0).translate({c: c - 0xFEE0 for c in range(0xFF01, 0xFF5F)}), 0),  # 全角数字/字母转半角：「验证码：１２３４５６」才匹配得上复制
    (r"(?<![\d.\-/])(\d{4})(?:年\s*|[-/.])(?=\d{1,2}(?:月|[-/.]\d{1,2}(?!\d)))", _this_year, 0),  # 本年日期的年份对锁屏是冗余：「2026年11月13日」「2026-11-13」只留月日（往年/明年保留）
    (r"(?<=\d)\s+(?=[点時时]|分(?!\d))|(?<=[点時时])\s+(?=\d{1,2}\s*分)", "", 0),  # 钟点里夹的空格：「3 点 30 分」→「3点30分」，后面的规则才认得
    (r"(?<=[点时])([二三四五])十([一二三四五六七八九])(?![分个人位名号块元钟秒天岁年次\d])", lambda m: str("二三四五".index(m.group(1))*10+20+"一二三四五六七八九".index(m.group(2))+1)+"分", 0),  # 口语省略「分」的汉字分钟：「八点三十五」→「八点35分」，否则会剩成「20:30五」
    (r"(?<=[点时]半)钟", "", 0),  # 「三点半钟」的「钟」是赘字：「3:30钟」→「3:30」
    (r"(?<=[点时])[零〇]?((?:[二三四五]?十)?)([一二三四五六七八九]?)分", lambda m: str(({"":0,"十":10,"二十":20,"三十":30,"四十":40,"五十":50}[m.group(1)]) + ("一二三四五六七八九".index(m.group(2)) + 1 if m.group(2) else 0)) + "分", 0),  # 汉字分钟转数字：「八点十五分」「八点四十五分」「八点零五分」→「八点15分」，后面的钟点规则统一成 20:15
    (r"(?<![一二三四五六七八九十两])(十[一二]?|[零〇一二两三四五六七八九])(?=[点时]差(?:一刻|[二三四五]?十分|五分|\d{1,2}分)|[点时][半整一三]刻|[点时][半整]|[点时][二三四五]?十分|[点时][二三四五]?十(?![个人位名号块元钟秒天岁年次\d])|[点时]\d{1,2}分|[点时](?!(?:(?![到至])[一-龥])*[一二三四五六七八九十]))", lambda m: str({"零":0,"〇":0,"一":1,"二":2,"两":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"十":10,"十一":11,"十二":12}[m.group(1)]), 0),  # 汉字钟点转数字：「晚上八点半」→「晚上8点半」，后面的规则才统一成 24 小时制
    (r"(?<!\d)([01]?\d|2[0-3])[点时]过?([二三四五]?)(?:十分|十(?![个人位名号块元钟秒天岁年次\d]))", lambda m: m.group(1)+":"+({"":"1","二":"2","三":"3","四":"4","五":"5"}[m.group(2)])+"0", 0),  # 汉字整十分钟：「3点十分」→「3:10」「晚上8点二十」→「晚上8:20」
    (r"(?<!\d)([01]?\d|2[0-4])[点時时](?:0{1,2}分?|整|钟)(?!\d)", r"\1点", 0),  # 整点不必写分/整：「18点00分」「18点00」「18点整」「18点钟」→「18点」
    (r"(?<!\d)([01]?\d|2[0-3])[点時时]([0-5]?\d)分(?![钟\d])", lambda m: f"{m.group(1)}:{int(m.group(2)):02d}", 0),  # 「18点30分」→「18:30」，「3点5分」→「3:05」，锁屏省字
    (r"(?<!\d)([01]?\d|2[0-3])[点時时][过零](0?[1-9]|[1-5]\d)(?:分(?![钟\d])|(?![分\d]))(?![:点])", lambda m: f"{m.group(1)}:{int(m.group(2)):02d}", 0),  # 「3点过5分」「3点零5分」是 3:05，不是 3 点
    (r"(?<!\d)([01]?\d|2[0-3])[点時时]([1-5]\d|0[1-9])(?![\d分:点])", r"\1:\2", 0),  # 口语省略「分」：「18点15」「9点05」→「18:15」「9:05」
    (r"(?<!\d)([01]?\d|2[0-4])[点时]差(?:一刻|15分)(?:钟)?", lambda m: f"{(int(m.group(1))-1)%24}:45", 0),  # 「15点差一刻」是 14:45，不是 15 点；放在「一刻」规则前
    (r"(?<!\d)([01]?\d|2[0-4])[点时]差([1-5]?\d|五|十|二十)分(?<!差0分)(?<!差00分)(?:钟)?", lambda m: f"{(int(m.group(1))-1)%24}:{60-({'五':5,'十':10,'二十':20}.get(m.group(2)) or int(m.group(2))):02d}", 0),  # 「15点差5分」「8点差十分」是 14:55 / 7:50，不是 15 点 / 8 点；差 0~59 分之外不会出现
    (r"(?<!\d)([01]?\d|2[0-3])[点时]([一三])刻", lambda m: m.group(1)+(":15" if m.group(2)=="一" else ":45"), 0),  # 「18点一刻」→「18:15」「三刻」→「:45」
    (r"(?<!\d)([01]?\d|2[0-3])[点时]半", r"\1:30", 0),
    (r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?:整|分(?!\d))", r"\1:\2", 0),  # 「18:00整」「18:30分」去掉多余的「整」「分」
    (r"(?:星期|礼拜|周)([一二三四五六日天])", lambda m: "周" + ("日" if m.group(1) == "天" else m.group(1)), 0),  # 锁屏省字：星期/礼拜/周 + 一…日/天 统一成「周X」（天→日），一条规则收口
    (r"(?<![一-龥])([今明昨后前])日(?![一-龥]{0,0}[报刊志])", lambda m: m.group(1) + "天", 0),  # 书面「明日/今日」统一成口语「明天/今天」，与后面的相对日规则一致
    (r"(?:本|这)周(?=[一二三四五六日])", "周", 0),  # 「本周五/这周五」即「周五」，锁屏省一字
    (r"(?:大约|大概|差不多)(?=[^，。；\n]{0,8}?(?:左右|上下|前后))", "", 0),  # 「大约3点左右」大约与左右重复，只留「左右」
    (r"(?:(?<=[天日\d点])|(?<=周[一二三四五六日]))(?:之|以)前", "前", 0),  # 截止语「周五之前/18点以前/11/13之前」→「前」，锁屏省一字
    (r"周[一二三四五六日](?:\s*[、,，]\s*周[一二三四五六日]){2,}", lambda m: _weekday_run(m.group(0)), 0),  # 连续三天以上的星期列举「周一、周二、周三」→「周一-周三」，非连续原样保留
    (r"周六\s*[、,，和及与]?\s*周日", "周末", 0),  # 「周六、周日」→「周末」，锁屏再省一字
    (r"(?<=周[一二三四五六日])\s*[至到~～]\s*(?=周[一二三四五六日])", "-", 0),  # 星期区间「周一至周五」→「周一-周五」，与日期/时间区间一致
    (r"周一-周日", "每天", 0),  # 「周一至周日」即每天，再省三字
    (r"周一-周五", "工作日", 0),  # 「周一至周五」即工作日，再省两字
    (r"每(?:个|周)?(?=工作日|周末|每天)|(?<=每周)(?=每天)", "", 0),  # 合并后的「每周工作日/每个周末/每周每天」里「每X」冗余，只留工作日/周末/每天
    (r"(?<![一二三四五六七八九十\d])(十[一二]?|[一二三四五六七八九])月\s*((?:[一二三]?十)?[一二三四五六七八九]?)(?=[日号])(?<=[十一二三四五六七八九])", lambda m: f"{_cn2n(m.group(1))}月{_cn2n(m.group(2))}", 0),  # 汉字月日「十二月一日」「三月二十五号」→数字，交给下一条统一成「12/1」
    (r"(?<![\d年])(1[0-2]|0?[1-9])月\s*(3[01]|[12]\d|0?[1-9])[日号](?=(\d)?)", lambda m: f"{m[1]}/{m[2]}" + (" " if m[3] else ""), 0),  # 锁屏省字：「11月13日」「11月13号」→「11/13」（年份已由上一条处理）
    (r"(?<![\d/])(\d{1,2}/\d{1,2})\s*[至到~～]\s*(?=\d{1,2}/\d{1,2}(?![\d/]))", r"\1-", 0),  # 日期区间「12/1至12/3」与时间区间一致用「-」
    (r"(?<![\d/])(\d{1,2}/\d{1,2})\s*[至到~～-]\s*(\d{1,2})[日号]?(?![\d/:点时])", r"\1-\2", 0),  # 同月日期区间「12/1至3日」→「12/1-3」
    (r"(?<=[\d日号])\s*[(（]\s*(?:含|包括)(?:当天|当日|本日)?\s*[)）]", "", 0),  # 日期后「（含）」对锁屏冗余
    (r"(?<![一-龥])(截止|报名|提交|上交|交|集合|上课|开会|面试|考试|活动|报到|签到|领取|取件|答辩|比赛|彩排|培训|讲座)(?:时间|日期|地点|地址)\s*[:：]\s*", r"\1 ", 0),  # 「截止时间：12/5」「集合地点：A楼」→「截止 12/5」「集合 A楼」，冒号和「时间/地点」对锁屏冗余（原两条合并）
    (r"(\d{1,2})/(\d{1,2})-\1/(\d{1,2})(?!\d)", r"\1/\2-\3", 0),  # 同月区间「12/1-12/5」→「12/1-5」
    (r"(?:(?<=\d/\d)|(?<=\d/\d{2})|(?<=[今明后]天))\s*[(（]\s*周[一二三四五六日]\s*[)）]", "", 0),  # 「11/13（周五）」「明天（周六）」括号星期对锁屏是冗余，日期/相对日已在
    (r"(?<!\d)([01]?\d|2[0-3])[：:]([0-5]\d)(?!\d)", r"\1:\2", 0),  # 全角冒号时间转半角：「18：00」→「18:00」
    (r"(?<![\d:])(\d{1,2}:[0-5]\d)\s*[~～至到—–]\s*(?=\d{1,2}:[0-5]\d(?![\d:]))", r"\1-", 0),  # 时间段「18:00~20:00」「18:00至20:00」统一成「18:00-20:00」
    (r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d):[0-5]\d(?![\d:])", r"\1:\2", 0),  # 时间的秒数对锁屏是噪点：「18:00:00」只留「18:00」
    (_PERIOD_RX + r"\s*(\d{1,2})(:[0-5]\d|[点時时])?\s*[~～至到–—-]\s*(\d{1,2})(?=[点時时:])", lambda m: _sep(m) + _hour24(m.group(1), int(m.group(2))) + (m.group(3) or "") + "-" + _range_end(m.group(1), int(m.group(4))), 0),  # 时段+钟点区间共用时段：「下午3点到5点」→「15点-17点」，否则 5点 会被读成凌晨
    (r"([今明后昨前每][早晚])\s*(\d{1,2}(?::[0-5]\d|[点時时]))\s*[~～至到]\s*(?=\d)", r"\1\2-", 0),  # 「今晚7点到9点」区间也统一成「-」（今明晚不转 24 小时制，只整理连接符）
    (_PERIOD_RX + r"\s*(\d{1,2})\s*(?=[点時时:：])", lambda m: _sep(m) + _hour24(m.group(1), int(m.group(2))), 0),  # 时段+钟点统一 24 小时制：「下午3点」→「15点」「上午9点」→「9点」「中午12点」→「12点」「晚上20:00」→「20:00」
    (r"(?<![A-Za-z\d:])(?:(?P<pre>[AaPp])\.?[Mm]\.?\s*(?P<h1>\d{1,2})(?P<m1>:[0-5]\d)?|(?P<h2>\d{1,2})(?P<m2>:[0-5]\d)?\s*(?P<post>[AaPp])\.?[Mm]\.?)(?![A-Za-z\d点時时])", lambda m: _en_period(m.group("pre") or m.group("post"), int(m.group("h1") or m.group("h2"))) + ((m.group("m1") or m.group("m2")) or "点"), 0),  # 英文 AM/PM：「PM 3:00」「3pm」→「15:00」「15点」，与中文时段统一 24 小时制
    (r"(?<![\d:])(\d{1,2}:[0-5]\d-)(\d{1,2})点(?![半整一二三四五六七八九十\d分刻])", r"\1\2:00", 0),  # 区间一头带分钟另一头只有「点」：「15:30-17点」→「15:30-17:00」，两端写法一致
    (r"(?<![\d:])(\d{1,2})点(-\d{1,2}:[0-5]\d)(?![\d:])", r"\1:00\2", 0),  # 反过来右端带分钟：「15点-17:30」→「15:00-17:30」
    (r"(?<![\d:])(\d{1,2}(?::[0-5]\d|点))\s*[至到]\s*(?=\d{1,2}(?::[0-5]\d|点))", r"\1-", 0),  # 各段已转24h后，剩下的「早上8点到中午12点」区间也统一成「-」
    (r"(今天|明天|后天|大后天|周[一二三四五六日]|\d{1,2}/\d{1,2})(\s*\d{1,2}(?::[0-5]\d|点))\s*[至到]\s*\1\s*(?=\d)", r"\1\2-", 0),  # 区间两端同一天只写一次：「明天15点到明天17点」→「明天15点-17点」
    (r"[\u2002-\u200a\u00a0\u202f]", " ", 0),  # 特殊空格统一成普通空格
    (r"[ \u3000\t]{2,}", " ", 0),  # 连续空格折成一个
    (r" +(?=[，。、；：！？）」』》】])", "", 0),  # 网址/代码被换成占位后留下的「[链接] ，」空格，中文标点前不该有空格
]]


def strip_md(t: str) -> str:
    """Bark 不渲染 Markdown，LLM/转发带的标记会原样露在锁屏上：按 STRIP_RULES 顺序清掉。"""
    t = t or ""
    for i, (rx, rep) in enumerate(STRIP_RULES):
        t = rx.sub(rep, t)
        if i == 1:
            t = html.unescape(t)  # 换行统一后再解 &amp; &lt;，其后的标签规则才看得见 &lt;b&gt; 还原出的标签
    return t.strip()


from bark_copy import *  # noqa: F401,F403  复制规则与 clip 在 bark_copy.py；仍可经 bark.xxx 访问
from bark_copy import clip, pick_copy, _copy_link  # noqa: F401


from bark_group import group_label, MEMBER_COUNT, LEAD_TAG  # noqa: F401  分组名规整在 bark_group.py


def _copy_for(title: str, full_body: str, raw_body: str, link) -> str:
    """长按可复制的内容：先归一全角数字，剔除位置类数字，再取原链接 pwd 或口令。"""
    txt = SPLIT_DIGITS.sub(lambda m: ((m.group(1) or m.group(3)) + (m.group(2) or m.group(4))).replace(" ", "").replace("-", ""), unicodedata.normalize("NFKC", title + " " + full_body))  # 全角数字/冒号先归一，否则「１２３４５６」认不出
    txt = ID_NUMS.sub(" ", txt)  # 寝室号/考场号等位置类数字不是口令
    pwd = re.search(r"[?&]pwd=([A-Za-z0-9]{4,8})", raw_body or "")  # 链接在正文里已折成[链接]，pwd 参数要从原文取
    return pwd.group(1) if pwd and not CODE_AFTER.search(txt) and not TRACK_AFTER.search(txt) else pick_copy(txt, link, title)


def _s(v) -> str:
    return str(v) if v else ""


def build_payload(title: str, body: str, level: str = "active", site_url: str = "", path: str = "/", badge: int = -1, group: str = "") -> dict:
    """纯函数：把标题/正文整理成 Bark 请求体（清理、去重、级别、复制内容、点开地址）。发送与重试在 send_bark。"""
    # 入参先统一成文字：空值/数字不抛错；站点地址去首尾空白（粘贴常带）
    title, body, site_url, path = _s(title), _s(body), _s(site_url).strip(), _s(path).strip() or "/"
    level = {"timesensitive": "timeSensitive", "passive": "passive"}.get(_s(level).strip().lower(), level)  # 级别大小写写错（timesensitive）也认，不再静默降成普通推送
    raw_body = body
    link = LINK_RX.search(body or "")  # 正文里的链接锁屏上折成[链接]，没有口令时改为复制原链接
    title = strip_md(title).strip() or "群报"  # 空标题的推送在锁屏上像乱码，兜底成 App 名
    body = re.sub(r"\n\s*\n+", "\n", strip_md(body).strip()) or title  # 空行折掉
    full_body = body  # 去标题前缀前的正文：复制内容要在完整文字里找（「取件」+「取件码：123」去重后只剩「码：123」会认不出）
    if len(title) <= 60 and body != title and body.startswith(title) and len(body) > len(title) + 2:
        body = body[len(title):].lstrip("：:，,。 \n") or body  # 正文以标题开头时去掉重复
    payload = {"title": clip(title, 60), "body": clip(body, 300), "group": group_label(group), "isArchive": "1"}  # 存进 Bark 历史
    if level in ("timeSensitive", "passive"):
        payload["level"] = level
    if level == "timeSensitive":
        payload.update(sound="alarm", volume=8, call="1")  # 紧急：不同铃声、拉高音量、循环响铃
    payload["copy"] = _copy_for(title, full_body, raw_body, link)  # 不开 autoCopy，用户长按复制
    if isinstance(badge, (int, float)) and not isinstance(badge, bool) and badge >= 0 and badge < 1e9:  # None/字符串不再抛 TypeError 拖垮整条推送
        payload["badge"] = min(int(badge), 999)  # 角标上限 999，积压再多也不让桌面图标显示一长串； 角标 = 未完成待办数
    url, icon = open_url(site_url, path)
    if url:
        payload["url"], payload["icon"] = url, icon
    return payload


DEDUPE_SECS = 60  # 同一条推送的去重窗口
_RECENT: dict = {}  # (地址, 标题, 正文) → 上次成功时间；同一条 DEDUPE_SECS 秒内不重复推（重试/多路触发时手机不连震）


def _is_dup(key: tuple, now: float) -> bool:
    for k in [k for k, t in _RECENT.items() if now - t > DEDUPE_SECS]:
        del _RECENT[k]
    return key in _RECENT


def retry_plan(level: str) -> tuple:
    """(最多尝试次数, 退避步长秒)：紧急推送丢了代价最大，多试两次且间隔更短（5 次总等待 10 秒内）。"""
    return (5, 1.0) if level == "timeSensitive" else (3, 1.5)


def redact(t: str, burl: str) -> str:
    """日志/错误提示里抹掉 Bark Key（地址最后一段路径），docker logs 和测试推送回显都不泄露设备密钥。"""
    key = burl.strip().split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]  # 地址若还带 ?query，Key 仍是路径最后一段
    return t.replace(key, "***") if len(key) >= 6 else t


TOO_BIG = (413, 414, 431)  # 内容过大类状态码：压短重发
SHRINK_TITLE_MAX = 60  # 压短时标题上限
SHRINK_LIMITS = (200, 80, 30, 10)  # 逐档压短：第一档不够再狠一点，宁可短也要送达


def shrink_payload(payload, limit=200) -> bool:
    """413/414/431 时就地压短：标题 >SHRINK_TITLE_MAX、正文 >limit 截断，超长跳转链接丢弃（414/431 多半是链接惹的祸）。返回是否真的改动。"""
    before = dict(payload)
    t = re.sub(r"\s+", " ", str(payload.get("title", ""))).strip()  # 标题里的换行/连续空白编码后白占长度
    tmax = min(SHRINK_TITLE_MAX, max(limit, 20))  # 标题上限随档位收紧，末档也别留一个 60 字长标题
    if len(t) > tmax:
        t = clip(t, tmax)
    payload["title"] = t or "群报"  # 标题只剩空白时别发空标题
    body = str(payload.get("body", ""))
    if limit <= SHRINK_LIMITS[2]:
        body = re.sub(r"\s*\n\s*", " ", body).strip()  # 末两档：换行并成空格，路径里换行会被编码成 %0A 白占长度
    body = body or payload["title"]  # 正文只剩空白时回退标题，不发空正文
    if len(body) > limit:
        payload["body"] = clip(body, limit)  # 与正常截断同一套（不拦腰切时间/小数/网址），≤ limit 保证再压一次必收敛
    else:
        payload["body"] = body
    if "group" in payload:
        payload["group"] = group_label(str(payload["group"]))  # 压短重发也走同一套分组名清理，空了回退「群报」
    for k in [k for k, v in payload.items() if k not in ("title", "body") and len(str(v)) > limit]:
        payload.pop(k)  # 其余任何超长字段（链接、图标、复制内容、分组…）同样会触发 414/431，一并丢弃
    return payload != before


def retry_after(v) -> float | None:
    """Retry-After 秒数或 HTTP 日期 → 等待秒数（最少 0.5、最多 5）；无法解析返回 None。"""
    v = str(v or '').strip()
    if not v:
        return None
    if re.fullmatch(r'\d+(\.\d+)?', v):
        sec = float(v)
    else:
        try:
            from email.utils import parsedate_to_datetime
            sec = parsedate_to_datetime(v).timestamp() - time.time()
        except Exception:
            return None
    return max(0.5, min(sec, 5))


RETRYABLE = (408, 429)  # 4xx 里值得再试的
_KEY_HINT = "（设备 Key 或地址可能填错，到 Bark App 首页重新复制）"
_AUTH_HINT = "（自建 Bark 服务器开了鉴权，地址里需带账号）"
# (状态码们, 提示)：一组状态码共用一句话，按分组一目了然
_HINT_GROUPS = (
    ((404,), _KEY_HINT),
    ((401, 403), _AUTH_HINT),
    ((301, 302, 307, 308), "（Bark 地址被重定向，请直接填最终地址，自建服务器多为 http 被跳转到 https）"),
    ((405,), "（Bark 地址路径不对，地址只填到服务器根，如 https://api.day.app/Key）"),
    ((407,), "（服务器出口代理要求鉴权，检查 HTTP_PROXY 设置）"),
    ((408, 504), "（Bark 服务器或其网关等待超时，会自动重试）"),
    ((502, 503), "（Bark 服务器网关暂不可用或在维护，会自动重试）"),
    ((410,), "（该设备已在 Bark 服务器注销，请在 App 重新注册并复制新 Key）"),
    (TOO_BIG, "（推送内容过大，已超出 Bark 服务器限制，已自动压短重发）"),
    ((400, 422), "（Bark 服务器认为请求不合法，多为设备 Key 不完整或填错，到 Bark App 首页重新复制）"),
    ((426,), "（Bark 服务器要求升级协议，地址请改用 https:// 开头）"),
    ((429,), "（Bark 服务器限流，稍后会自动重试）"),
    ((521, 522, 523, 524, 525, 526), "（Bark 服务器前面的 CDN 连不上源站或证书异常，会自动重试；自建的请检查源站是否在线）"),
)
STATUS_HINT = {c: h for cs, h in _HINT_GROUPS for c in cs}
_FALLBACK = {3: STATUS_HINT[302], 4: "（Bark 服务器拒绝了请求，请检查地址和设备 Key）", 5: "（Bark 服务器暂时出错，会自动重试）"}
for _c in range(300, 600):  # 各段其余状态码统一兜底，不留空白
    STATUS_HINT.setdefault(_c, _FALLBACK[_c // 100])
LAST_ERR = ""  # 最近一次失败的原因（测试推送时直接告诉用户，不让人猜）


# (异常类型, 错误文本关键词(小写), 提示)；按顺序首条命中，类型 None = 任意异常
NET_ERR_HINTS = (
    (None, ("ssl", "certificate"), "，证书/HTTPS 握手失败，自建服务器请改用 http://IP:端口"),
    (httpx.ConnectError, ("name or service", "resolution", "getaddrinfo"), "，域名解析失败，检查 Bark 地址拼写或服务器 DNS"),
    (httpx.ConnectError, ("refused",), "，对方拒绝连接，检查 Bark 服务是否在运行、端口是否写对"),
    (httpx.ConnectError, ("unreachable", "no route"), "，服务器当前没有可用网络路由，检查服务器网络/出口"),
    (httpx.ConnectError, (), "，检查服务器能否访问外网/防火墙端口"),
    (httpx.ConnectTimeout, (), "，连接超时（端口可能被防火墙丢包），检查安全组/防火墙，会自动重试"),
    (httpx.ReadTimeout, (), "，已连上但对方响应太慢，会自动重试"),
    (httpx.TimeoutException, (), "，连接超时，会自动重试"),
    (httpx.ReadError, (), "，连接被对方重置（Connection reset），多为防火墙拦截，会自动重试"),
    ((httpx.UnsupportedProtocol, httpx.InvalidURL), (), "，Bark 地址格式不对，应形如 https://api.day.app/你的Key，到设置里重新粘贴"),
    (httpx.ProxyError, (), "，经代理连接失败，检查服务器 HTTP(S)_PROXY 设置"),
    (httpx.WriteError, (), "，发送数据时连接断开，会自动重试"),
    (httpx.RemoteProtocolError, (), "，对方中途断开连接（常见于被网关/防火墙重置），会自动重试"),
)


def net_err_text(ex: Exception) -> str:
    """网络异常 → 给用户的一句话（原因 + 怎么修）"""
    msg = str(ex).lower()
    hint = next((h for t, kws, h in NET_ERR_HINTS
                 if (t is None or isinstance(ex, t)) and (not kws or any(k in msg for k in kws))), "")
    return f"连不上 Bark 服务器（{type(ex).__name__}）" + hint


async def send_bark(burl: str, title: str, body: str, level: str = "active", site_url: str = "", path: str = "/", badge: int = -1, group: str = "", dedupe: bool = True, once: bool = False) -> bool:
    """发一条 Bark。level：timeSensitive / active / passive。once=True（测试按钮）只试一次：地址填错时秒回原因，不让人干等重试。"""
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
        tries, step = retry_plan(level)
        if once:
            tries = 1
        i, shrunk, shrinks = -1, False, 0
        while i < tries - 1 or shrunk:  # 压短重发不占重试次数，最后一次遇到过大也能再送一次
            i += 1 if not shrunk else 0
            shrunk = False
            wait = step * (i + 1) * random.uniform(0.8, 1.25)  # 抖动：多条同时失败时不在同一刻齐刷刷重试
            try:
                r = await cl.post(burl, json=payload)
                if r.status_code < 300:
                    _RECENT[key] = time.monotonic()
                    return True
                LAST_ERR = redact(f"Bark 返回 {r.status_code}：{r.text[:60]}", burl)
                LAST_ERR += STATUS_HINT.get(r.status_code, "")
                print(LAST_ERR)  # 写进 docker logs，地址/Key 填错一眼能看出
                if r.status_code in TOO_BIG and shrinks < len(SHRINK_LIMITS) and shrink_payload(payload, SHRINK_LIMITS[shrinks]):
                    # 内容过大：压短正文、丢掉超长跳转链接再发，宁可短也要送达
                    shrunk, shrinks = True, shrinks + 1  # 最多压 len(SHRINK_LIMITS) 档，仍嫌大就按失败走，杜绝死循环
                    continue
                if r.status_code < 500 and r.status_code not in RETRYABLE:  # 408 超时/429 限流也值得再试
                    _RECENT.pop(key, None)
                    return False
                ra = retry_after(r.headers.get('retry-after'))  # 任何可重试状态：服务端说等多久就等多久
                if ra is not None:
                    wait = ra
            except Exception as ex:
                LAST_ERR = net_err_text(ex)
                print(redact(f"Bark 推送失败（第{i+1}次）：{type(ex).__name__} {ex}", burl))  # 超时异常 str 为空，带上类型才看得出原因
            if i < tries - 1:
                await asyncio.sleep(wait)
    _RECENT.pop(key, None)  # 没送达就释放占位，允许之后重试
    print(f"Bark 推送最终未送达：「{payload['title'][:20]}」{level}，已试 {tries} 次（{LAST_ERR}）")  # 一条汇总，docker logs 里看得出丢了哪条
    return False
