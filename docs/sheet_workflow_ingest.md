# Google Sheet 行到定价工作流的安全建单层

`backend/app/sheet_workflow_ingest.py` 把 `AgentAutomation.parse_sheet_payload()`
产生的 `tasks` 转换为 `PricingWorkflowCreateReq` 候选。规划函数只做确定性判断和
数据转换；`SheetWorkflowIngestCoordinator` 已接到 `/api/agent/sheet/push`，负责保存
基线，并只把通过规则的候选交给现有 `PricingWorkflowStore`。

## 安全规则

- 首次快照 (`previous_rows=None`) 只建立基线，不把历史行批量建单。
- 后续必须显式传 `allow_new_rows=True`，否则仍不产生建单候选。
- 只有带有效 PN、没有 PLA 且处于待处理状态的全新物理行才是候选。
- 空 PN、占位 PN、已完成、进行中、待决策/待确认和已有 PLA 的行全部跳过。
- 已知行的业务字段改变时进入 `manual_review` 决策，不用新内容自动重建任务。
- 请求固定为 `submission_authorized=False`、`notify=False`、`gsp_payload={}`，
  因此最多运行定价并停在人工审核，不能进入 GSP 提交队列或发送群通知。
- 幂等键由 spreadsheet、sheet、row 和业务字段哈希共同决定；重复交付同一候选
  给 `PricingWorkflowStore.create()` 只会返回同一个工作流。

## 运行方式

后端配置 `sheet_workflow_auto_create_enabled` 默认为 `false`。无论这个开关是什么，
某个 spreadsheet 的第一次同步都只建立基线。基线建立后，只有把开关明确改为
`true`，后续出现的新行才会创建定价工作流；生成的任务仍强制停在 `manual_review`。
若收到另一个 spreadsheet ID，协调器会返回 `spreadsheet_identity_changed`，不会
自动建立第二套基线或创建任务。

内部顺序如下：

1. Sheet push 解析完成后读取上次已接受的 `locator -> business_hash` 快照。
2. 调用 `plan_sheet_workflow_ingest(parsed, spreadsheet_id=..., previous_rows=...,
   allow_new_rows=...)`。
3. 首次运行保存 `plan.observed_rows` 作为基线，不调用工作流 store。
4. 对后续 `action=create` 的 decision，把 `decision.request` 交给
   `PricingWorkflowStore.create()`；只有它持久化返回后才确认该 locator/hash。
5. `manual_review` 决策写入审计/人工队列，不覆盖旧 hash。处理人明确接受修改后，
   再更新快照；这样网络重放和失败重试不会丢单，旧行改 PN 也不会静默建新单。

`observed_rows` 是观测结果，不是自动确认动作。协调器把快照原子写入 runtime 的
`agent/sheet_workflow_ingest/state.json`；不要把它放到 Google Sheet 单元格或
Apps Script 日志中。

## 存量 pending 行 backfill

接入前已经存在的 pending 行不能通过打开自动创建开关绕过首次快照保护。调用
`POST /api/agent/sheet/workflow-backfill`，先用 `apply=false` 生成候选清单和
`manifest_hash`；人工核对后，再以完全相同的 source、push、limit 和
`expected_manifest_hash` 执行 `apply=true`。

backfill 只在 `dry_run=true` 且 `group_reply_enabled=false` 时工作。生成请求继续固定
为 `submission_authorized=false`、`notify=false`、`gsp_payload={}`，最多运行定价并停在
`manual_review`，不会调用 Windows Agent。
