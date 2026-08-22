# HuaChat 到 Google Sheets 沙箱入口

这个目录实现简历方案中的最小公共入口：接收一条已标准化的聊天事件，抽取显式定价字段，触发 Google Workflows，并由工作流追加到现有询价工作簿 `2026.07!A:N`。它只负责任务接入，不读取价格表，不计算价格，也不提交企业内部定价审批系统。

## 保留现有内网能力

该入口是旁路增量能力，不替换任何现有内网入口：

- 现有内网页面、单 PN 查询、批量导出和规则管理保持原路径；
- 现有在线表格桥、审批查询和 Windows 执行端保持原配置；
- Cloud Run、HuaChat 或 Google Sheet 不可用时，内网定价仍可独立使用；
- 沙箱接入由独立服务和环境变量控制，默认不会随内网后端启动；
- 自动创建的任务强制停在人工复核，无法被 Windows 提交任务领取。

当前提交只新增 `cloud_gateway/` 和对应测试，没有修改定价引擎、内网接口、系统服务或生产数据目录。

## 当前边界

HuaChat 官方事件结构和签名算法尚未接入。`POST /events/huachat` 当前接受一个用于联调的标准包络，并用共享 token 保护入口。拿到官方文档和测试应用后，只需替换事件验签与字段映射，`parse_pricing_event()` 之后的表格协议保持不变。

标准包络示例：

```json
{
  "message_id": "hc-demo-001",
  "created_at": "2026-08-03T14:00:00Z",
  "sender": {"name": "Sandbox User"},
  "text": "定价\nPN: DEMO-PN-002, DEMO-PN-003\n层级: Country\n客户: Demo Customer\n产品线: Video"
}
```

服务返回 `accepted=true` 后，任务行状态为 `尚未开始`、阶段为 `收集需求`。现有 Google Sheets 桥的 5 分钟对账会把表格快照送到 Linux 后端；安全创建逻辑只生成定价任务，并强制 `submission_authorized=false`，最终停在 `manual_review`。

当前目标工作簿：

- Spreadsheet ID：`1g96wjgMSGMhXrzLGwCAIQic_mYkS8xsThxJryfFHJ2A`
- 标签页：`2026.07`
- 业务表头：第 4 行
- 业务数据：第 5 行开始

写入列严格沿用现有表头：请求人、描述、产品线、PN、内部型号、价格层级、客户、是否发布、截止日期、PLA、执行人、状态、阶段、备注。HuaChat 消息 ID 和时间只写入备注列，格式为 `[HUACHAT:<message_id>] <created_at>`，用于重试去重。

## Cloud Run 配置

环境变量：

- `HUACHAT_INGRESS_TOKEN`：沙箱入口共享 token，放在 Secret Manager，不写进镜像。
- `GOOGLE_SPREADSHEET_ID`：目标工作簿 ID。
- `GOOGLE_SHEET_NAME`：月份标签页，当前为 `2026.07`。
- `WORKFLOW_EXECUTION_URL`：Google Workflow Executions API 的工作流执行集合地址。配置后，入口只排队任务，不直接写表。

目标工作簿只需要共享给 Workflows 服务账号并授予编辑权限。工作流在 `N5:N1000` 扫描 HuaChat 消息标记，再向 `A:N` 追加任务。正式大规模并发接入时应把有界扫描换成 Firestore 事务型幂等表。

当前 Google Cloud 部署对象：

- 项目：`gen-lang-client-0394580582`
- 区域：`europe-west1`
- Cloud Run：`pricing-huachat-ingress`
- Workflows：`pricing-request-sandbox`
- 入口服务账号：`pricing-ingress@gen-lang-client-0394580582.iam.gserviceaccount.com`
- 工作流服务账号：`pricing-workflow@gen-lang-client-0394580582.iam.gserviceaccount.com`
- 月度告警预算：`Pricing Cloud Sandbox EUR 5 Alert`，阈值为 50%、80% 和 100%

Cloud Run 同时设置服务级与修订级最大实例数为 1，最小实例数为 0。Google Cloud 的普通预算负责告警。Cloud Run 预览版 Spend Cap 需要在结算控制台按单一项目和单一服务创建，建议把 Cloud Run 强制暂停阈值设为 EUR 4，为 Workflows、构建和镜像存储保留缓冲。

工作流定义位于 `workflows/pricing_request.yaml`，控制台将显示以下步骤：事件校验、安全边界校验、消息 ID 去重、写入任务行、人工复核边界和返回结果。详细执行历史只用于脱敏演示任务，常规入口使用基础执行历史，避免在步骤变量中保存完整客户信息。

一键部署脚本位于 `deploy/deploy_gcp.sh`。部署前必须为项目启用有效结算；脚本会启用所需 API、创建独立 Secret Manager 密钥、部署工作流和 Cloud Run，并输出服务地址与 Workflows 控制台地址。

预期控制台地址：

```text
https://console.cloud.google.com/workflows/workflow/europe-west1/pricing-request-sandbox?project=gen-lang-client-0394580582
```

## 2026-08-03 云端联调结果

- Cloud Run 状态：Ready，修订 `pricing-huachat-ingress-00001-rzj`
- 公开健康地址：`https://pricing-huachat-ingress-339841524852.europe-west1.run.app/health`
- Workflows 状态：Active，修订 `000001-2f3`
- 脱敏消息 ID：`cloud-demo-20260803-001`
- 首次执行：Succeeded，写入 `'2026.07'!A148:N148`
- 重复执行：Succeeded，返回 `duplicate=true`，没有追加第二行
- 两次执行均返回 `submission_authorized=false`

首次执行详情：

```text
https://console.cloud.google.com/workflows/workflow/europe-west1/pricing-request-sandbox/execution/f152de47-d1c6-47e9-ae03-2a7d8821a81a?project=gen-lang-client-0394580582
```

示例启动命令：

```bash
HUACHAT_INGRESS_TOKEN=test-only \
GOOGLE_SPREADSHEET_ID=1g96wjgMSGMhXrzLGwCAIQic_mYkS8xsThxJryfFHJ2A \
GOOGLE_SHEET_NAME=2026.07 \
WORKFLOW_EXECUTION_URL=https://workflowexecutions.googleapis.com/v1/projects/gen-lang-client-0394580582/locations/europe-west1/workflows/pricing-request-sandbox/executions \
uvicorn cloud_gateway.app:app --host 127.0.0.1 --port 8080
```
