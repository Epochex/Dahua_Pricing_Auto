# 企业定价工作流演化控制平面

本模块把“自演化”限制为可审计的软件发布过程，而不是允许模型直接修改生产逻辑。
当前部署默认 `shadow_only`，控制平面本身没有 GSP、Windows Agent、Google 或钉钉调用能力。

## 组件

- `event_store.py`：带校验和的 append-only 事件日志，事件 ID 幂等，按任务确定性回放；业务输入只存 SHA-256 与证据引用。
- `registry.py`：不可变 workflow/skill/prompt/policy 版本、环境绑定、任务版本 pin、审计哈希链以及版本束原子激活/回滚。
- `evaluation.py`：baseline/candidate 对相同案例的 paired replay；价格越界、非法迁移、重复副作用为零容忍门禁，LLM 自评分不能作为发布依据。
- `release.py`：`proposed → shadow → shadow_ready → canary → canary_ready → active` 发布状态机；支持四眼审批、指标窗口、kill switch 和回滚。
- `control_plane.py`：为报价任务固定版本束，捕获状态事件，把离线评测结果连接到发布控制器。
- `case_memory.py`：把人工介入/失败沉淀为待审核案例；只有人工接受后才能进入评测集，重复原因只形成候选建议，不会自动发布技能。

## 任务推进与版本固定

服务启动时注册并激活内置安全基线。新任务写入：

```json
{
  "execution_context": {
    "run_id": "run-...",
    "session_id": "session-...",
    "environment": "production",
    "bundle_hash": "sha256:...",
    "pins": {
      "workflow:enterprise-pricing": "1.0.0",
      "skill:price-validate": "1.0.0",
      "prompt:pricing-decision": "1.0.0",
      "policy:pricing-safety": "1.0.0"
    }
  }
}
```

后续重试继续使用任务中的 pins，不会因为线上版本更新而在执行中途换规则。

## 发布门禁

候选 artifact 先进入 `candidate`，然后执行：

1. 对不可变案例集运行 baseline/candidate paired replay。
2. 任一 price boundary、illegal transition 或 duplicate effect 立即拒绝。
3. 业务准确率、人工率、成功率和 P95 延迟不能超过配置的退化阈值。
4. 影子窗口只记录决策，不允许外部副作用。
5. 灰度需要开发者与审批者不同，并限制最大流量比例。
6. 灰度指标满足门禁后，原子激活完整版本束。
7. 线上硬约束异常触发 kill switch；回滚重新绑定此前的 immutable pins。

当前 `DAHUA_EVOLUTION_EXECUTION_ENABLED` 默认未开启；即使发布状态达到 canary/active，
`windows_agent_allowed` 和 `notifications_allowed` 仍固定为 `false`。

## Google/表格存量任务

普通 Sheet ingest 的第一份快照只建立基线，不创建任务。对于已经存在的 pending 行，使用
`POST /api/agent/sheet/workflow-backfill`：

1. `apply=false` 获取候选列表及 `manifest_hash`。
2. 人工核对候选数量和行定位。
3. 以相同 source、push 和 limit，携带 `expected_manifest_hash` 且 `apply=true`。

只有当服务器仍满足 `dry_run=true`、`group_reply_enabled=false` 时才能应用。创建的请求始终
满足 `submission_authorized=false`、`notify=false`、`gsp_payload={}`，因此只会完成定价并
停在 `manual_review`，不会被 Windows Agent 领取。

## API

- `GET /api/evolution/status`
- `GET /api/evolution/events`、`GET /api/evolution/replay/{task_id}`
- `GET|POST /api/evolution/artifacts`
- `GET /api/evolution/cases`、`POST /api/evolution/cases/{id}/review`
- `GET /api/evolution/cases/evaluation-dataset`、`GET /api/evolution/cases/suggestions`
- `POST /api/evolution/artifacts/{kind}/{name}/{version}/transition`
- `GET /api/evolution/bundles/{environment}`
- `POST|GET /api/evolution/evaluations`
- `POST|GET /api/evolution/releases`
- `POST /api/evolution/releases/{id}/shadow`
- `POST /api/evolution/releases/{id}/metric-windows`
- `POST /api/evolution/releases/{id}/canary`
- `POST /api/evolution/releases/{id}/promote`
- `POST /api/evolution/releases/{id}/rollback`

所有变更接口复用服务端 agent token；查询接口只返回版本定义、哈希、指标和引用，不返回
原始价格、客户数据或凭据。

## 证明稳定性的测试

测试覆盖事件重复/冲突、跨进程追加、日志尾部恢复、中段损坏、状态链断裂、不可变版本覆盖、
原子版本束回滚、硬门禁拒绝、分层退化、LLM 自评字段拒绝、四眼审批、灰度 kill switch，
以及 Sheet backfill 清单变化时拒绝执行。
