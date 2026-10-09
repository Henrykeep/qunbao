# 给部署代理（Codex 等）的说明

目标：在这台 Linux 服务器上用 Docker 部署群报（NapCat 收 QQ 群消息 + qunbao 网页总结）。

## 步骤
1. 确认已安装 Docker 和 docker compose 插件；没有就按 Docker 官方文档安装。
2. 在仓库根目录 `cp .env.example .env`，向用户索取并填写：
   - WEB_PASS（网页登录密码）
   - LLM_API_KEY / LLM_BASE_URL / LLM_MODEL（OpenAI 兼容接口）
   - MY_PROFILE（用户自我介绍、重要的人和关心的事）
   - INGEST_TOKEN（随机生成：`openssl rand -hex 16`）
   其余选项（关于我、重要的人、推送等）部署后在网页「设置」里改。
   不要把 .env 提交到 git。
3. `mkdir -p napcat/qq napcat/config data && docker compose up -d --build`
4. 防火墙/安全组放行 TCP 8000 和 6099。
5. `docker logs napcat`，把 WebUI token 和登录二维码交给用户，让用户用手机 QQ 扫码。
6. 用户登录后，在 NapCat WebUI「网络配置」新建：
   - HTTP 客户端：URL `http://qunbao:8000/onebot`，消息格式 string，启用
   - HTTP 服务器：端口 3000，启用
   也可以直接改 napcat/config 下的 onebot11_<QQ号>.json，然后 `docker restart napcat`。
7. 验证：`docker logs qunbao` 里能看到 `POST /onebot 200`；浏览器打开 `http://服务器IP:8000` 会跳到登录页，用 WEB_USER / WEB_PASS 能登录（Cookie 会话保持 30 天）。
8. 部署完成后建议关闭 6099 的公网访问。

## 更新（升级到最新版）
在部署目录执行：
```
git pull && docker compose up -d --build qunbao
```
- 不要删除数据卷、data 目录或 .env：消息、待办和设置都保留，旧设置会自动迁移（0.32 起「自动整理间隔」变成默认开启的开关）。
- 验证：`curl -s http://127.0.0.1:8000/healthz` 返回的 version 与仓库 VERSION 文件一致；`docker logs --tail 50 qunbao` 无报错。
- 手机上把群报从后台划掉再打开一次，确保拿到新页面。

## 接微信（可选）
不需要在服务器上额外部署。确认 .env 里 INGEST_TOKEN 已设置、8000 端口可从公网访问；
用户在网页「设置 → 接入微信」复制接收地址，填进安卓手机上的通知转发 App 即可。

## 自检
`pip install fastapi httpx pytest && python -m pytest tests -q`
`curl http://127.0.0.1:8000/healthz` 返回版本号。
