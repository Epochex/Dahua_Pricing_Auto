# Cloudflare Tunnel 远程访问部署与运维

本文用于把 `.23` 上的自动定价网页通过 HTTPS 提供给远程同事。该方案不要求远程电脑安装 Tailscale，也不要求 `.23` 有公网 IP 或开放路由器入站端口。

```text
远程浏览器 -- HTTPS/443 --> Cloudflare Edge
                                |
                                | 已建立的 outbound Tunnel
                                v
.23 cloudflared --> 127.0.0.1:18083 专用 Nginx --> 127.0.0.1:8000 FastAPI
```

Cloudflare 在公网边缘终止 HTTPS。`cloudflared` 从 `.23` 主动建立到 Cloudflare 的连接，origin 侧在同一主机内使用 HTTP 不会经过局域网。

## 边界与选择

- 本方案默认允许知道网址的人直接打开平台，不强制 Microsoft 登录、邮箱 OTP 或 Cloudflare Access。
- Google Cloud 在此架构下不是必需组件。平台、runtime 数据和计算仍在 `.23`，所以 `.23` 关机、办公室断网或 Cloudflare 出站连接中断时，远程平台也会不可用。
- 公共入口只指向 `127.0.0.1:18083` 的专用 Nginx，不能指向现有的 `:80` 站点。专用配置明确拒绝 `/api/agent/*`、`/api/evolution/*` 和所有未列入白名单的未来 API。
- 所有 `/api/admin/*` 的 POST/PUT 写操作都由后端使用管理员 token 鉴权；数据源发布写接口 `/api/admin/price-data/*` 不因未启用 Cloudflare Access 而跳过这层保护。浏览器请求使用 `X-Price-Data-Admin-Token`，服务端从 `DAHUA_PRICE_DATA_ADMIN_TOKEN` 读取期望值。token 不放在 URL、Cloudflare 配置、Nginx 配置或仓库中。
- 不登录意味着公网匿名用户能看到网页，并调用已公开的查询、批量上传、任务下载及 `/api/admin/*` GET 读取接口，但不能匿名执行管理 POST/PUT。任务 ID 不能代替授权；敏感报价、上传文件和导出结果仍存在被滥用或泄露的风险。生产使用强烈建议至少对管理路径启用 Cloudflare Access，或最终给全站启用 Access。

## 网络前提

远程浏览器只需访问 Cloudflare HTTPS 443。`.23` 必须能对 Cloudflare Tunnel 端点发起出站连接：

- 当前模板固定 `protocol: http2`，需要出站 **TCP 7844**；
- 如果改用 QUIC，需要出站 **UDP 7844**；
- 建议 IT 同时允许 TCP/UDP 7844，受限网络至少允许 TCP 7844；
- 不需要从互联网或局域网向 `.23` 开放 80、18083、8000、7844 等入站端口。

如果 TCP 和 UDP 7844 都被拦截，Tunnel 无法建立。先让 IT 按 Cloudflare 官方 Tunnel 防火墙清单放行，不应通过把 `.23:80` 端口映射到公网来规避。

## 仓库内模板

| 文件 | 用途 |
|---|---|
| `deploy/nginx/dahua-cloudflare-origin.conf` | loopback-only、API 默认拒绝的 Tunnel origin |
| `deploy/cloudflared/dahua-auto-pricing.yml.example` | 不含真实 UUID、域名和凭据的 Tunnel 配置模板 |
| `deploy/systemd/dahua-cloudflared.service` | 独立低权限用户运行的可审计 systemd unit |
| `deploy/scripts/validate_cloudflare_tunnel.sh` | 非破坏性静态检查与已安装配置验证 |

模板不会创建 Cloudflare 账户、Tunnel 或 DNS，也不会写入真实 secret。

## 一、部署前检查

在 `.23` 上确认本地应用健康：

```bash
curl -fsS http://127.0.0.1:8000/api/meta
curl -fsSI http://127.0.0.1/
ss -lntp | grep -E ':(80|8000)\b'
```

确认当前 Nginx 的 `client_max_body_size` 是 100 MiB。专用 origin 同样设置为 100 MiB；Cloudflare 账户级上传限制也必须不低于业务文件大小。

部署前记录状态，便于回滚：

```bash
systemctl is-active nginx dahua-pricing-backend
nginx -T > /tmp/nginx-before-cloudflare.txt
```

## 二、安装 cloudflared 和创建 Tunnel

按公司软件安装规范安装 Cloudflare 官方 `cloudflared`，并确认二进制位置：

```bash
command -v cloudflared
cloudflared version
```

仓库的 unit 默认使用 `/usr/local/bin/cloudflared`。如果实际路径不同，应在复制 unit 后只修改 `ExecStart`。

下面命令会联网并改变 Cloudflare 账户，只能由有权限的管理员人工执行；仓库脚本不会自动执行：

```bash
cloudflared tunnel login
cloudflared tunnel create dahua-auto-pricing
cloudflared tunnel route dns dahua-auto-pricing pricing.example.com
```

记录命令返回的 Tunnel UUID 和凭据 JSON 路径。`pricing.example.com` 只是示例，必须换成 Cloudflare 托管区域内的正式 hostname。

## 三、安装专用 Nginx origin

先检查目的文件不存在；不要覆盖其他人维护的配置：

```bash
test ! -e /etc/nginx/conf.d/dahua-cloudflare-origin.conf
sudo install -o root -g root -m 0644 \
  /data/Dahua_Pricing_Auto/deploy/nginx/dahua-cloudflare-origin.conf \
  /etc/nginx/conf.d/dahua-cloudflare-origin.conf
sudo nginx -t
sudo systemctl reload nginx
```

验证 origin 只监听 loopback，并验证白名单/拒绝规则：

```bash
ss -lntp | grep '127.0.0.1:18083'
curl -fsS http://127.0.0.1:18083/api/meta
curl -o /dev/null -sS -w '%{http_code}\n' http://127.0.0.1:18083/api/agent/config
curl -o /dev/null -sS -w '%{http_code}\n' http://127.0.0.1:18083/api/evolution/status
curl -o /dev/null -sS -w '%{http_code}\n' http://127.0.0.1:18083/api/not-allowlisted
```

后三个请求都应返回 `404`。专用 origin 将 `CF-Connecting-IP` 还原为真实客户端 IP，并向后端固定传递 `X-Forwarded-Proto: https`；访问日志写入 `/var/log/nginx/dahua-cloudflare-access.log`。

## 四、配置价格数据管理员 token

在 `.23` 本机生成高强度 token，不要复制示例值：

```bash
openssl rand -hex 32
```

将生成值放进只允许 root 读取的环境文件，例如：

```text
DAHUA_PRICE_DATA_ADMIN_TOKEN=<生成的随机值>
```

推荐路径为 `/etc/dahua-pricing/backend.env`，权限 `root:root 0600`。使用 `sudo systemctl edit dahua-pricing-backend` 添加 drop-in：

```ini
[Service]
EnvironmentFile=/etc/dahua-pricing/backend.env
```

然后：

```bash
sudo systemctl daemon-reload
sudo systemctl restart dahua-pricing-backend
sudo systemctl show dahua-pricing-backend -p EnvironmentFiles
```

不要用 `systemctl show ... -p Environment` 打印 token。没有配置 token 时，后端应拒绝所有 `/api/admin/*` POST/PUT 管理写操作；读取规则和版本状态的 GET 可以不携带 token。管理员在管理页面输入 token 后，前端通过 `X-Price-Data-Admin-Token` 请求头发送。不要把 token 发到群聊、写入 URL 或提交到 Git。

## 五、安装 Tunnel 配置和 systemd unit

创建独立系统用户和配置目录：

```bash
id cloudflared >/dev/null 2>&1 || \
  sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin cloudflared
sudo install -d -o root -g cloudflared -m 0750 /etc/cloudflared
```

把 `deploy/cloudflared/dahua-auto-pricing.yml.example` 复制为 `/etc/cloudflared/dahua-auto-pricing.yml`，人工替换：

- `<TUNNEL-UUID>`：真实 Tunnel UUID；
- `<PRICING-HOSTNAME>`：正式公开 hostname；
- `credentials-file`：实际凭据 JSON 的绝对路径。

将创建 Tunnel 时返回的凭据文件复制进 `/etc/cloudflared`。下面两个变量必须人工改成真实 UUID 和命令返回的绝对路径；目标已存在时停止并人工比较，不覆盖：

```bash
pricing_tunnel_uuid='replace-with-real-uuid'
pricing_credential_source='/absolute/path/returned/by-cloudflared.json'
test ! -e "/etc/cloudflared/${pricing_tunnel_uuid}.json"
sudo install -o root -g cloudflared -m 0640 \
  "${pricing_credential_source}" "/etc/cloudflared/${pricing_tunnel_uuid}.json"
sudo chown root:cloudflared /etc/cloudflared/dahua-auto-pricing.yml
sudo chmod 0640 /etc/cloudflared/dahua-auto-pricing.yml
```

先验证，再安装 unit；目的文件已存在时先人工比较，不直接覆盖：

```bash
bash /data/Dahua_Pricing_Auto/deploy/scripts/validate_cloudflare_tunnel.sh \
  /etc/cloudflared/dahua-auto-pricing.yml
test ! -e /etc/systemd/system/dahua-cloudflared.service
sudo install -o root -g root -m 0644 \
  /data/Dahua_Pricing_Auto/deploy/systemd/dahua-cloudflared.service \
  /etc/systemd/system/dahua-cloudflared.service
sudo systemctl daemon-reload
sudo systemctl enable --now dahua-cloudflared
```

## 六、上线验证

本机观察连接状态，日志中不应出现持续 reconnect：

```bash
systemctl status dahua-cloudflared --no-pager
journalctl -u dahua-cloudflared --since '-10 minutes' --no-pager
```

从公司网络之外的设备验证：

```bash
curl -fsSI https://pricing.example.com/
curl -fsS https://pricing.example.com/api/meta
curl -o /dev/null -sS -w '%{http_code}\n' https://pricing.example.com/api/agent/config
curl -o /dev/null -sS -w '%{http_code}\n' https://pricing.example.com/api/evolution/status
```

内部接口应为 `404`。随后用浏览器完成一次小文件批量上传、任务查询和结果下载；数据管理员再完成“上传候选文件 → 校验 → 发布 → 回滚演练”。不要首次上线就直接发布未验证的真实价格源。

检查真实 IP 与 HTTPS 代理头：

```bash
sudo tail -n 20 /var/log/nginx/dahua-cloudflare-access.log
```

日志首列应为远程客户端 IP，而不是 `127.0.0.1`。后端若生成绝对 URL，应使用 HTTPS；不要信任来自公网的任意 `X-Forwarded-*` 头，专用 origin 已覆盖这些头。

## 七、日常运维与审计

```bash
# Tunnel 状态与最近日志
systemctl status dahua-cloudflared --no-pager
journalctl -u dahua-cloudflared --since today --no-pager

# Nginx 公网入口访问/错误日志
tail -n 100 /var/log/nginx/dahua-cloudflare-access.log
tail -n 100 /var/log/nginx/dahua-cloudflare-error.log

# 配置改动后验证并平滑加载
nginx -t && systemctl reload nginx
bash /data/Dahua_Pricing_Auto/deploy/scripts/validate_cloudflare_tunnel.sh \
  /etc/cloudflared/dahua-auto-pricing.yml
```

建议在 Cloudflare 配置基础限速、Bot/WAF 规则和上传大小限制。最小化的可选 Access 做法是只保护 `/api/admin/*` 或管理 hostname，普通查询页面继续匿名；若价格和客户数据敏感，则全站启用 Access。Access 是安全增强项，不是 Tunnel 工作的技术前提。

定期轮换 `DAHUA_PRICE_DATA_ADMIN_TOKEN`：更新 root-only 环境文件、重启后端、通知获授权的管理员更新浏览器中的 token，然后确认旧 token 返回 `401/403`。Tunnel 凭据泄露时应在 Cloudflare 控制台吊销并创建新 Tunnel 凭据，而不仅是修改本地文件。

## 八、故障排查

| 现象 | 检查 |
|---|---|
| Cloudflare 502/1033 | `dahua-cloudflared` 状态、凭据 UUID、Tunnel DNS route、`curl 127.0.0.1:18083/api/meta` |
| 持续连接超时 | 公司出口是否允许 TCP 7844；代理是否阻止 cloudflared 直连 |
| 上传 413 | 专用 Nginx、Cloudflare 账户和应用三层的大小限制 |
| `/api/agent` 可访问 | 立即停 Tunnel；确认 service 指向 `127.0.0.1:18083` 而不是 `:80` |
| 数据发布 401/403 | 后端环境变量是否加载、请求头是否为 `X-Price-Data-Admin-Token` |
| 日志 IP 为 127.0.0.1 | 确认请求确实经过 Cloudflare，检查 real-IP module 和 `CF-Connecting-IP` |

## 九、回滚

回滚 Tunnel 不影响原有局域网站点：

```bash
sudo systemctl disable --now dahua-cloudflared
sudo mv /etc/nginx/conf.d/dahua-cloudflare-origin.conf \
  /etc/nginx/dahua-cloudflare-origin.conf.disabled
sudo nginx -t
sudo systemctl reload nginx
```

Nginx 配置被移出自动加载目录但仍可恢复。真实 Tunnel 配置和凭据也保留在 `/etc/cloudflared`；若确定永久退役，再由 Cloudflare 管理员人工删除 DNS route 和 Tunnel，并按公司 secret 销毁流程处理凭据。仓库模板和验证脚本不执行这些删除操作。

如需恢复：重新安装专用 Nginx 配置、`nginx -t`、reload，然后 `systemctl enable --now dahua-cloudflared`。
