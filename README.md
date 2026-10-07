# 群报

替你看 QQ / 微信群：每天一份「头条 + 待办 + 值得知道」，有人 @你、重要的人说话、命中关键词时推送到 iPhone，随时可以问「我今天要做什么」。

## 功能
- **今日**：一句话头条、按紧急程度排序的待办（打勾自动沉底，多设备同步）、点开看原话并跳到原文上下文、值得知道的通知、@我的消息、各群在聊什么。可切今天 / 3 天 / 7 天，可回看往期。
- **原文**：所有群的原始聊天记录，全文搜索。
- **问群报**：对话式提问，可连续追问。
- **设置**：关于我、重要的人、关键词、屏蔽的群、每日整理时间、Bark 推送、运行状态。
- 手机 Safari「添加到主屏幕」后像原生 App，自动跟随深色模式。

## 部署
    cp .env.example .env      # 至少改 WEB_PASS、LLM_API_KEY
    mkdir -p napcat/qq napcat/config data
    docker compose up -d --build
安全组放行 8000（群报）和 6099（NapCat 后台，登录后可关）。

## 登录 QQ
1. `docker logs napcat`：日志里有登录二维码和 WebUI token
2. 手机 QQ 扫码（建议用小号）
3. NapCat 后台 → 网络配置 → 新建
   - HTTP 客户端：`http://qunbao:8000/onebot`，消息格式 string，启用
   - HTTP 服务器：端口 3000，启用（用来显示群名）

## 推送到 iPhone
App Store 装 **Bark**，打开后复制它给的推送地址（形如 `https://api.day.app/xxxx`），填进群报「设置 → 推送到 iPhone」，点「测试推送」。

## 接微信（可选）
旧安卓机登微信小号，用通知转发类 App 把通知 POST 到：
    POST http://服务器IP:8000/ingest
    Header  X-Token: <INGEST_TOKEN>
    Body    {"source":"微信","chat":"群名","sender":"发送人","text":"内容"}

## 更新
    git pull && docker compose up -d --build qunbao
数据都在 `./data/qunbao.db`，更新不会丢。
