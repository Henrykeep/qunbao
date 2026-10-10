"""大模型报错的纯函数（0.34.26 从 app.py 拆出）：错误翻译、短原因、审核拦截判断。不碰数据库、不 import app。"""


def llm_err_text(code: int, text: str, model: str = "") -> str:
    """把接口报错翻译成用户看得懂、知道怎么修的话。"""
    t = (text or "")[:300]
    low = t.lower()
    if code == 402 or "insufficient" in low or "balance" in low or "余额" in t or "quota" in low:
        return "大模型余额不足：去服务商后台充值后，点「整理」即可恢复"
    if code in (401, 403):
        return "大模型 API Key 不对或已失效：在服务器 .env 里改 LLM_API_KEY，重启 qunbao 容器"
    if code == 404:
        return f"大模型接口地址或模型名不对：检查 .env 里的 LLM_BASE_URL 和 LLM_MODEL（现在是 {model}）"
    if code == 429:
        return "大模型被限流（请求太频繁）：几分钟后会自动重试"
    if code >= 500:
        return f"大模型服务商出故障（{code}）：稍后会自动重试"
    return f"大模型接口报错 {code}：{t[:160]}"


def is_censored(code: int, text: str) -> bool:
    low = (text or "").lower()
    return code == 451 or "censorship" in low or "content_filter" in low or "data_inspection_failed" in low \
        or "content you provided" in low or "敏感" in (text or "")


REJECT_CODES = (451, 400, 413, 422)  # 模型拒收这一块内容（审核拦截 / 请求有问题 / 太长）：拆小跳过，不整个群卡死
LLM_FATAL_CODES = (401, 402, 403, 404)  # 余额不足 / 密钥错误 / 地址或模型名错误：自动整理不重试，等设置变更或手动整理


def llm_short(code: int, err: str) -> str:
    """顶部状态用的短原因。"""
    e = err or ""
    if code == 402 or "余额" in e:
        return "余额不足"
    if code in (401, 403) or "Key" in e:
        return "API Key 不对"
    if code == 404:
        return "接口地址或模型名不对"
    if code == 429:
        return "被限流"
    if code == -1:
        return "没配置大模型"
    if "超时" in e:
        return "接口超时"
    if "连不上" in e:
        return "连不上大模型"
    if code >= 500:
        return "服务商故障"
    return "大模型报错"
