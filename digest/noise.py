"""噪音预过滤：极保守，只跳过空/撤回/群系统提示/纯图片表情/整条附和客套。纯函数，不依赖 app.py。"""
import re

# 0.34：噪音只跳过「跟一个表情一样没内容」的：整条就是附和/客套/笑（可能是回答的「可以」「行」「对」「在」不算噪音，照样送模型）
NOISE_WORDS = {"收到", "收到收到", "好的收到", "收到谢谢", "好的", "好滴", "好哒", "好嘞", "嗯嗯", "嗯呢", "嗯", "ok", "okk", "okay",
               "谢谢", "谢谢老师", "多谢", "感谢", "哈", "哈哈", "哈哈哈", "哈哈哈哈", "哈哈哈哈哈", "1", "11", "111", "6", "66", "666", "6666",
               "牛", "牛啊", "赞", "知道了", "明白", "了解", "晚安", "早安", "嘿嘿", "辛苦了", "辛苦", "笑死", "啊这", "草", "好呀", "好的呢", "好吧", "拜拜", "再见", "不客气", "没事", "没问题的", "对对", "对的", "是的", "哦哦", "哦", "额", "呃", "厉害", "太强了", "nb", "绝了", "好棒", "加油", "恭喜", "太棒了", "好耶", "冲", "笑死我了", "离谱", "太好了", "真好", "羡慕", "优秀", "可爱", "爱了", "泪目", "破防了", "蚌埠住了", "无语", "沙发", "打卡", "路过", "围观", "吃瓜", "顶", "顶顶", "+1", "同感", "我也是", "我也一样", "收到了", "明白了", "了解了", "知道啦", "谢谢大家", "感谢分享", "感谢大佬", "谢谢分享", "okok", "嗯好", "好的好的", "好的谢谢", "谢谢啦", "谢啦", "多谢多谢", "感谢感谢", "了解收到", "明白收到", "好的明白", "好的知道了", "懂了", "get", "嗷", "哇", "哇塞", "nice", "yyds"}
PLACEHOLDER_RE = re.compile(r"^(\s*\[(图片|表情|动画表情|语音|视频|文件|卡片|聊天记录|红包|位置|名片)\]\s*)+$")
# 只有这几种占位符算噪音（纯图片 / 表情 / 贴纸，没有文字）；[文件][聊天记录][卡片][语音][红包] 可能就是通知，照样送模型
NOISE_PH = {"图片", "表情", "动画表情", "贴纸", "动画", "emoji"}
RECALL_RE = re.compile(r"撤回了一条消息|撤回一条消息|recalled a message")
SYS_RE = re.compile(r"^\s*\S{0,24}?(?:邀请\S{0,40}?加入了群聊|加入了群聊|加入本群|退出了群聊|被移出群聊|被移出了群聊|修改群名(?:称)?为\S{0,40}|"
                    r"拍了拍\S{0,30}|开启了全员禁言|关闭了全员禁言|被设置为管理员|被取消了管理员|成为新群主)\s*[。.]?\s*$")


def is_noise(text: str) -> bool:
    """极保守的噪音：空 / 撤回 / 群系统提示 / 只有图片表情贴纸没有文字 / 整条只是附和客套或笑。拿不准一律不算。"""
    t = (text or "").strip()
    if not t or RECALL_RE.search(t) or (len(t) <= 80 and SYS_RE.match(t)):
        return True
    toks = re.findall(r"\[([^\[\]]{1,6})\]", t)
    rest = re.sub(r"\[[^\[\]]{1,6}\]", "", t)
    core = re.sub(r"[\s\W_]+", "", rest.lower())
    if toks and not core:  # 只有 [xx] 占位符 / 微信小表情：全是图片表情贴纸类才算噪音，有 [文件] 之类的照样送
        return all(x in NOISE_PH or (x not in ("文件", "卡片", "聊天记录", "红包", "位置", "名片", "语音", "视频", "链接", "小程序", "转账", "群公告", "公告")
                                     and len(x) <= 4 and not re.search(r"\d", x)) for x in toks)
    if not core:  # 纯 emoji / 标点
        return True
    base = re.sub(r"[啦呀哟噢喔啊哇~]+$", "", core) if len(core) > 2 else core  # 「收到啦」「好的呀」语气尾巴不改变含义
    base = re.sub(r"(老师|同学|学长|学姐|师兄|师姐|哥|姐)$", "", base) if len(base) > 3 else base  # 「收到老师」「好的学姐」称呼尾巴
    return core in NOISE_WORDS or _all_noise_words(core) or (len(base) >= 2 and (base in NOISE_WORDS or _all_noise_words(base))) or bool(re.fullmatch(r"(?:哈|呵|嘿|嘻)+|6+|1+|\+1|(?:(好|嗯|对|哦|噢|是|行)\1+)", core))


_WORDS_DESC = sorted((w for w in NOISE_WORDS if not w.isascii() or len(w) > 1), key=len, reverse=True)


def _all_noise_words(core: str) -> bool:
    """整条由噪音词连着拼成（「好的好的」「收到谢谢」「谢谢老师辛苦了」）也算噪音；只切中文词，拼不满就不算。"""
    if len(core) > 16 or len(core) < 4:
        return False
    i = 0
    while i < len(core):
        for w in _WORDS_DESC:
            if core.startswith(w, i):
                i += len(w)
                break
        else:
            return False
    return True
