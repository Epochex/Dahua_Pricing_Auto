# Google Sheets 自动同步桥

`script/google_sheets_agent_bridge.gs` 是绑定到询价工作簿的 Apps Script。它只连接 Google Sheets 和 Linux 定价后端，不包含任何钉钉 webhook，也没有群消息发送能力。

## 数据流

1. 商务编辑 `YYYY.MM` 标签页第 5 行以后的业务单元格。
2. 安装型 `onEdit` 触发器把所有月份页推送到 `/api/agent/sheet/push`。
3. 5 分钟定时触发器执行一次全量对账，并读取 `/api/agent/sheet/status-updates/pending`。
4. 后端确认 GSP 已审批后，脚本按服务端返回的精确标签页和单元格回写状态，再调用 `/ack` 幂等确认。

`onEdit` 提供秒级触发，5 分钟任务负责补偿漏事件、临时网络失败和多人并发编辑，因此两者不是重复机制，而是实时链路加最终一致性对账。

## 安装

1. 在目标 Google Sheet 中打开“扩展程序 → Apps Script”，粘贴 `google_sheets_agent_bridge.gs`。
2. 在“项目设置 → 脚本属性”中配置：
   - `DAHUA_AGENT_BASE_URL`：稳定的 HTTPS 后端地址。
   - `DAHUA_AGENT_TOKEN`：服务器 `sheet_push_token.txt` 的值。
   - `DAHUA_ENABLE_WRITEBACK`：首次联调设为 `false`；确认 PLA 行定位正确后再改为 `true`。
3. 手工运行一次 `installPricingAgentTriggers` 并完成 Google 授权。
4. 返回值必须同时满足 `sheetPushAccepted=true`、`pendingReadAccepted=true`、`groupMessageSent=false`。

项目不提供带静态口令的远程 bootstrap。首次配置必须由工作簿所有者在
Apps Script 编辑器中调用 `configurePricingAgent`，令牌只写入 Script
Properties，不能出现在源码、部署包或聊天记录中。

密钥只放 Script Properties，禁止写进源码、单元格或执行日志。当前临时 Cloudflare quick tunnel 地址不适合作为长期触发器地址，应换成稳定域名或受控的 Tailscale/反向代理入口。

每次快照携带 `snapshotHash` 和稳定的 `idempotencyKey`。编辑触发器有 15 秒防抖，5 分钟对账负责补偿被合并的连续编辑。状态回写前会同时核对标签页、目标行、允许状态以及该行当前 PLA，避免排序或插行后把“已完成”写到另一张申请。

## 消息安全边界

Google Sheets 触发器不会调用钉钉。钉钉 Stream 的入站消息由独立 worker 处理；worker 只有同时满足下列条件才允许回复：

- Stream 配置允许回复；
- 后端确认机器人被 `@`；
- 后端 `group_reply_enabled=true`；
- 后端 `dry_run=false`；
- 发送者在允许列表内（若配置了允许列表）。

任何条件缺失时，只记录意图、生成回复预览和 trace，不执行群发送。
