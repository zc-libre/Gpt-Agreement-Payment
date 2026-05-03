# deploy/ — 本地服务器部署文件

本目录是把这个仓库跑在自己服务器（裸 systemd + nginx + Cloudflare Worker）时需要的额外配置和小工具。**与上游 main 分支隔离**，不要回流到 main。

## 部署位置一览

| 仓库内文件 | 部署到 |
|---|---|
| `deploy/systemd/gpt-pp-webui.service` | `/etc/systemd/system/gpt-pp-webui.service` |
| `deploy/nginx/ai2api.zclibre.org.location-webui.conf` | 把 `location /webui/ { ... }` 段插入到现有 nginx site conf 的 server 块内（不是替换整个文件） |
| `deploy/secrets.example.json` | 拷成 `output/secrets.json` 后填真实值 |
| `deploy/scripts/cf_revert_catchall.sh` | 仓库内即可执行，操作 CF Email Routing catch-all |

## 一次性部署步骤

```bash
# 1. 系统依赖（Debian 12）
sudo apt install -y python3-venv xvfb sqlite3 jq nginx
# gost 二进制要自己装：https://github.com/go-gost/gost/releases

# 2. clone + venv + 装依赖（按上游 README 走）
git clone https://github.com/DanOps-1/Gpt-Agreement-Payment.git /root/docker/Gpt-Agreement-Payment
cd /root/docker/Gpt-Agreement-Payment
git checkout feat/cf-kv-deploy   # 切到本地部署分支
python3 -m venv .venv
.venv/bin/pip install -r webui/requirements.txt
.venv/bin/pip install requests curl_cffi 'camoufox[geoip]' browserforge mitmproxy pybase64 socksio
.venv/bin/playwright install firefox
.venv/bin/camoufox fetch
# webui 前端
cd webui/frontend && pnpm i && pnpm build && cd ../..

# 3. systemd webui 服务
sudo cp deploy/systemd/gpt-pp-webui.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gpt-pp-webui
# 默认 listen 127.0.0.1:8765

# 4. nginx 反代（前提：你已有 ai2api.zclibre.org 的 server 块 + SSL 证书）
sudo cp /etc/nginx/conf.d/ai2api.zclibre.org.conf{,.bak.$(date +%s)}
# 把 deploy/nginx/ai2api.zclibre.org.location-webui.conf 里的 location 段
# 插入到 server { listen 443 ssl; ... } 块内
sudo nginx -t && sudo nginx -s reload

# 5. Cloudflare Email Worker + KV（自动）
# 先去 dashboard 创一个 token，权限：
#   Account → Workers Scripts:Edit
#   Account → Workers KV Storage:Edit
#   Zone → Email Routing Rules:Edit
#   Zone → Zone:Read
CF_API_TOKEN=cfut_xxx CF_ACCOUNT_ID=xxx \
  .venv/bin/python scripts/setup_cf_email_worker.py \
    --zones zone1.com,zone2.com,zone3.com
# 脚本会输出 otp_kv_namespace_id，写到 output/secrets.json

# 6. secrets.json
cp deploy/secrets.example.json output/secrets.json
# 编辑填真实 cloudflare.api_token / account_id / otp_kv_namespace_id

# 7. gost 中继（webshare auth socks5 → 本地无 auth socks5）
# Camoufox 不支持带 auth 的 SOCKS5，必须中继。pipeline 跑时 browser_register / card 期望 :18899
setsid nohup gost \
  -L=socks5://:18899 \
  -F=socks5://USER:PASS@WEBSHARE_HOST:PORT \
  > /tmp/gost-18899.log 2>&1 < /dev/null & disown
```

## 改动的代码（vs main）

| 文件 | 原因 |
|---|---|
| `CTF-reg/cf_kv_otp_provider.py` | grace 3s 太严，OpenAI 邮件比 pipeline 调 wait_for_otp 早 30-40s 到 KV，全部被当旧值忽略。改成默认 60s + `CF_KV_GRACE_S` env 可调 |
| `scripts/otp_email_worker.js` | 原 regex 直接对 raw RFC822 字符串扫，被 OpenAI 邮件模板里的 CSS 颜色 `color:#353740` 当 OTP 误抽。改成先 decode quoted-printable + strip `<style>` 和 HTML + 排除 hex 颜色，再 regex；同时把 raw body 备一份到 KV `<email>:raw` 供以后诊断 |
| `.gitignore` | 新增 `CTF-pay/config.auto.json`：webui 自动生成、含 secrets，不该 track |

## CF Email Routing catch-all 切换

服务器上 `libre-mail` Worker 还在但不接 catch-all（otp-relay 接管了）。要切回去：

```bash
# 切回 libre-mail
CF_API_TOKEN=cfut_xxx bash deploy/scripts/cf_revert_catchall.sh

# 切到 otp-relay
TARGET_WORKER=otp-relay CF_API_TOKEN=cfut_xxx bash deploy/scripts/cf_revert_catchall.sh
```

## 关于上游 OAuth client_id

`cpa.oauth_client_id` 填 OpenAI Codex CLI 的公开 client_id：

```
app_EMoamEEZ73f0CkXaXp7hrann
```

从 `https://auth.openai.com/oauth/authorize?client_id=...&codex_cli_simplified_flow=true&...` URL 里 `client_id` 参数抓的。

## 已知 race / 待修

- `card.py:5354+` 的 consent click 后立刻 `query_selector` 有 race，约 30% 概率挂 `Execution context was destroyed`。重跑就好；想根治要把循环里 query_selector 包 try/except 吞掉这个 navigation 异常。
