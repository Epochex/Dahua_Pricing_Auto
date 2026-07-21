# 简历技术栈闭环审计（2026-07-16）

## 结论

Google Sheet 可以作为商务协作界面、事件入口和状态回写面，但不能让简历中的全部
技术栈自动成立。当前代码已经形成一条**默认无 GSP 写副作用**的定价业务链；它仍
需要 Google 账号授权、稳定 HTTPS、Windows 真机部署和生产 GSP 只读契约验收，
才能称为外部环境闭环。MCP、语义技能检索、自扩展技能库、消息队列/三级缓存/
120 QPS、RBAC/沙箱、Langfuse 等简历描述目前没有由本项目实现或证明。

## 本轮已实现并验证

| 能力 | 当前状态 | 代码证据 | 安全边界 |
|---|---|---|---|
| Google Sheet 编辑/定时同步 | 代码完成，待账号安装 | `script/google_sheets_agent_bridge.gs`、`script/appsscript.json` | 15 秒防抖、5 分钟补偿、快照哈希与幂等键；无钉钉 webhook |
| Sheet 快照入站 | 代码完成 | `backend/app/agent_ops.py::receive_sheet_push` | 相同快照复用原 push，不重复 event/trace |
| Sheet 新行创建定价工作流 | 代码完成，默认关闭 | `backend/app/sheet_workflow_ingest.py`、`backend/app/main.py::agent_sheet_push` | 首次只建基线；显式开启后才建单；强制 `submission_authorized=false`、空 GSP payload、无通知 |
| 定价状态机与未知提交恢复 | 已实现 | `backend/app/pricing_workflow.py` | 提交结果未知先进入 verifying，不能直接再次 submit |
| GSP Under Approval 扫描 | 两端代码完成，待真机只读验收 | `backend/app/agent_ops.py`、`desktop_agent/gsp_status_agent.py` | Windows 仅允许 list/detail 两个只读路径；本地再次过滤状态 |
| 扫描回调与超时恢复 | 已实现 | `desktop_agent/gsp_status_agent.py::UnderApprovalReportLedger`、后端 result callback | 稳定 report id、durable outbox、后端幂等重放 |
| 审批摘要与 trace/session | 已实现 | `backend/app/agent_ops.py` | 后端重算审批人、当前节点、状态和最老申请摘要 |
| Sheet 状态回写 | 代码完成，默认关闭 | Apps Script `applyPendingStatusUpdates_` | 开启前核对 sheet、row、允许状态及当前 PLA；逐条 ACK |
| 钉钉 Stream 入站及回复策略 | 基础链路已实现 | `backend/app/dingtalk_stream_worker.py`、`backend/app/agent_ops.py` | 当前配置、原始 reply policy、sender、@、dry-run 多重门禁；只允许 HTTPS `*.dingtalk.com` |

验证结果：Linux/后端 `91 passed, 8 subtests passed`；Windows Desktop Agent
`19 tests` 中 `17 passed`、`2 skipped`（本机未安装 Playwright）；Python 编译、
Apps Script JavaScript 语法、manifest JSON 和 `git diff --check` 均通过。所有 GSP
操作使用 mock；真实 GSP 提交 0，真实群消息 0。

## 简历中定价项目的诚实状态

| 简历描述 | 状态 | 判断 |
|---|---|---|
| IM + Sheet + Linux + Windows GSP 组合闭环 | 部分闭环 | 代码链已贯通；外部授权、部署和只读验收未完成 |
| 规则快路径 + LLM 歧义解析 | 部分实现 | 两个白名单意图可用；不是完整级联技能路由 |
| 语义检索召回技能、复合请求技能链 | 未实现 | 没有可验证的 embedding 检索或通用 planner |
| MCP 异步工具执行 | 未实现 | 当前是自定义 HTTP/control contract，不应称 MCP |
| 自动诱导技能、评审并晋升 | 未实现 | 有 reflection candidate/replay 基础，不等于自扩展技能库 |
| 前置/后置/不变量、终端回读 | 部分实现 | 定价状态机和 GSP 验证具备关键约束，不是通用过程验证框架 |
| 消息队列、指数退避、并发池、令牌桶 | 部分/未实现 | 有持久化文件队列与幂等恢复；无 Kafka/Redis 队列和完整限流实现 |
| 三级缓存、61% 命中、P99 480ms、120 QPS | 未证明 | 本项目没有对应实现与可复现实验档案 |
| RBAC、注入拦截、容器无网沙箱、输出白名单 | 部分/未实现 | 有 token、意图白名单、GSP 只读路径和出站域名门禁；不等于上述完整体系 |
| Langfuse trace | 未实现 | 有本地 trace/session，但未接 Langfuse |
| 320 产品线、1172 批次、2568 条、单批 3.6s | 未充分证明 | 现有统计口径和原始评测档案不足，不能直接作为生产事实 |
| 异常率 9.3% → 0.6% | 仅有受控实验 | 可描述为受控实验的 contract/action anomaly，不能偷换成生产价格字段事实 |

## NetOps 项目不能由 Google Sheet 代替的部分

简历中的 planner-executor-critic、混合 RAG、BM25/向量/图路径/RRF、reranker、
CRAG、引用核验、三层记忆、LongMemEval、GRPO/DPO 属于独立 NetOps 项目。当前
NetOps 仓库可以验证 Kafka、ClickHouse、Docker/Kubernetes、拓扑证据门控、
review/runbook 和基础回放/消融；完整混合 RAG、记忆演化、LongMemEval、GRPO/DPO
以及相应效果数字仍未实现或缺乏可复现证据。把这些塞进定价平台只为覆盖关键词，
会破坏项目边界，也不能形成可信的面试证据。

## 外部闭环前必须处理的问题

1. 目标 Google 账号需手工授权绑定 Apps Script，并安装 `onEdit` 与 5 分钟触发器；
   服务器需要稳定 HTTPS 地址。当前环境没有可复用的 `clasp`/Google Cloud 登录态。
2. `sheet_workflow_auto_create_enabled` 当前保持 `false`；完成基线核对后才能开启。
   `DAHUA_ENABLE_WRITEBACK` 也应先保持 `false`，抽样核对 PLA 行定位后再开启。
3. Windows 代码需要部署到 `DESKTOP-E1MKHQR` 并重启 Agent，先用 `--no-push`
   对真实账号验证 list API 的状态字段、分页、国家可见范围，再做一次真实回调验收。
4. 自动 GSP 提交仍需四类真实 payload 模板、字段 contract、受控测试账号和明确
   写授权；Google Sheet 不能解决这个阻塞。
5. 本轮按要求没有发送群消息。若要验收群回复，必须使用专用测试群和测试账号，
   不能拿当前生产群直接试发。
6. 用户曾在会话中展示过静态钉钉 webhook token；即使代码未保存它，也应在钉钉
   控制台重置后再做任何出站验收。
