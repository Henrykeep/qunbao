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

## 配 HTTPS（iPhone 网页通知必需）
iPhone 只在 HTTPS 网址下给「添加到主屏幕」的网页发通知（Web Push）。没有 HTTPS 时群报照常能用，只是「开启通知」会提示需要 HTTPS。
1. 向用户要一个域名（或子域名），DNS 加 A 记录指向服务器公网 IP。没有域名可以先用 `服务器IP用横线连起来.sslip.io`（如 `1-2-3-4.sslip.io`）。
   注意：服务器在中国大陆时，域名走 80/443 需要 ICP 备案；没备案就用境外服务器，或用 Cloudflare Tunnel（`cloudflared`，不用开端口，自带 HTTPS）把 `http://127.0.0.1:8000` 映射出去。
2. 安装 Caddy（Debian/Ubuntu：按 https://caddyserver.com/docs/install 加官方源后 `apt install caddy`），`/etc/caddy/Caddyfile` 写：
   ```
   qunbao.example.com {
       reverse_proxy 127.0.0.1:8000
   }
   ```
   `systemctl reload caddy`。Caddy 会自动申请并续期 Let's Encrypt 证书（会自动带上 X-Forwarded-Proto，登录 Cookie 自动变 Secure）。
3. 防火墙/安全组放行 TCP 80 和 443（80 用于申请证书）。配好后 8000 可只对本机开放。
4. 验证：`curl -sI https://qunbao.example.com/healthz` 返回 200；手机 Safari 打开 `https://域名` 能登录。
5. 告诉用户：用 https 地址重新「添加到主屏幕」（旧的 http 图标删掉），从桌面打开 →「开启通知」；网页「设置 → 定时、提醒与免打扰 → 点通知打开」改成 https 地址（Bark 点开用）。
- 网页通知的 VAPID 密钥第一次启动自动生成，存在 data/qunbao.db 里，不用配置；**不要删 data 目录**，否则已开启的设备要重新开一次。
- 服务器需要能访问外网的推送服务（苹果是 `web.push.apple.com`）。

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
`pip install -r digest/requirements.txt pytest && python -m pytest tests -q`
`curl http://127.0.0.1:8000/healthz` 返回版本号。
