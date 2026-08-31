# 产品归类异常调查 Agent

## 目标

确定性分类和算价继续处理正常路径。独立验证器发现未知映射、规则冲突、强型号证据冲突、多源价格表冲突和型号族离群。人工只完成一次分流，选择深入调查后由 Agent 收集证据并给出结果。

## 处理链

```text
产品分类器
→ MappingVerifier
  ├─ PASS：继续确定性算价
  └─ WARN/BLOCK：创建调查案例
                    ↓
                人工一次分流
  ┌─────────────────┼─────────────────┐
  ↓                 ↓                 ↓
确认当前结果      直接修正结果       深入调查
  ↓                 ↓                 ↓
保存案例          保存案例          Agent 调查
                                      ↓
                              输出候选、证据和冲突
                                      ↓
                         查看链路、检查点回正或重跑
                                      ↓
                                记录写回动作回执
                                      ↓
                                  案例记忆
                                      ↓
                              限定范围的规则候选
                                      ↓
                         历史回放、影子验证、发布与回滚
```

Agent 调查完成后进入 `investigation_complete`。该状态没有第二次强制人工确认。写回操作单独记录目标和动作回执。已经写回的案例从旧检查点回正或重跑时会标记 `reconciliation_required`，直到新的写回回执完成对账。

## 已实现模块

### 规则命中溯源

`collect_mapping_matches` 返回所有命中规则、优先级、条件、分类和价格组。现有定价路径继续选择优先级最高的结果，验证器可以看到被早期规则遮挡的其他候选。

### 独立验证器

`MappingVerifier` 输出：

- `PASS`：继续确定性算价。
- `WARN`：等待人工快速分流。
- `BLOCK`：停止当前型号进入算价。

当前信号包括：

- `missing_mapping`
- `intra_source_rule_conflict`
- `cross_source_mapping_conflict`
- `strong_model_evidence_conflict`
- `selected_category_has_no_supporting_rule`
- `unverifiable_mapping`
- `family_category_outlier`

### 持久化调查案例

`MappingInvestigationStore` 提供：

- 幂等创建案例。
- 乐观并发版本控制。
- 一次人工分流。
- 不可变工具检查点。
- 检查点回正。
- 从指定检查点重跑。
- Agent 结果保存。
- 写回动作回执。
- 写回后回正的对账标记。

### 有界 Agent 调查

`MappingInvestigationAgent` 只允许调用白名单中的只读领域工具，并限制工具调用次数和调查时间。每次工具调用保存输入哈希、输出证据引用、状态和幂等键。

工具失败会形成失败检查点。工具预算耗尽时输出 `retain_current_hold`，当前型号继续保持暂停状态。

规划器每一轮只接收有长度上限的上下文。压缩过程保留候选分类、异常信号、证据引用和检索命中，并记录规划次数、工具次数、上下文字符数、工具结果字符数、模型输入输出 token 数和调查耗时。`ReferenceEvidencePlanner` 提供可复现的基线策略；`OpenAICompatibleMappingPlanner` 提供严格 JSON、只读工具校验和用量回传的模型接入边界。

### 分层证据检索

`TieredEvidenceRetriever` 按以下顺序检索：

1. 精确产品对象。
2. 型号族。
3. 关键词和属性。
4. 可插拔语义检索。

结构化映射、产品目录和人工确认案例优先使用精确检索。产品说明书和历史文字材料可以接入关键词与向量混合检索。

`EvidenceContextAssembler` 限制证据数量和文本预算，同时保留支持证据、反对证据、中立证据、来源版本和缺失证据。

### 案例记忆

`MappingCaseMemory` 只接收以下案例：

- 人工确认当前分类。
- 人工直接修正分类。
- 已记录写回动作回执的 Agent 调查结果。

案例默认作用于具体产品对象。型号族范围需要显式指定 `model_family`。记忆记录可以设置有效期，所有规则建议均保持 `automatic_publish=false`。

### 版本化文档证据库

`MappingDocumentStore` 保存产品说明书、产品目录和其他受控材料的版本、权威级别、适用产品、型号族、内容哈希和结构化属性。同一 `evidence_id` 的内容保持不可变，重复写入同一内容按幂等请求处理。检索时同时合并受控文档和已批准案例记忆。

## 领域工具

当前调查链已经接入以下项目内类型化 Python 工具：

- `mapping.get_candidates`
- `catalog.get_product_attributes`
- `catalog.search_similar_products`
- `documents.search_product_documents`
- `history.search_approved_cases`
- `price_data.compare_sources`

工具需要返回稳定的 `evidence_refs`。正式价格提交和规则发布不进入 Agent 工具白名单。

当工具跨服务器部署、被多个 Agent 复用或需要独立认证和限流时，可以把同一组 Python 适配器封装成自定义 MCP Server。当前单体后端阶段使用直接函数或内部 HTTP 接口。

人工分流选择深入调查后，可以调用 `POST /api/mapping/investigations/{case_id}/run-reference` 执行确定性基线链路。企业批准的模型服务通过 `DAHUA_MAPPING_MODEL_ENDPOINT`、`DAHUA_MAPPING_MODEL_NAME` 和密钥环境变量配置，再由 `POST /api/mapping/investigations/{case_id}/run-model` 调用相同的上下文预算和只读工具。未配置模型服务时模型入口保持关闭。受控文档通过 `POST /api/mapping/evidence/documents` 登记，通过对应 GET 接口检索。案例调查、检查点回正、重跑、写回回执和案例记忆沿用同一组受保护接口。

## Benchmark

版本化清单位于 `benchmarks/mapping_investigation_v1.jsonl`，覆盖六类样本：

- `normal_baseline`
- `historical_bad_case`
- `challenge_conflict`
- `unseen_variant`
- `should_refuse`
- `resource_budget`

数据划分包含 `baseline`、`regression`、`holdout` 和 `shadow`。评测门禁包括：

- 验证状态准确率。
- 必要异常信号召回率。
- 人工分流动作准确率。
- Agent 建议动作准确率。
- 候选分类准确率。
- 错误自动路由数量为零。
- 无证据引用数量为零。
- 工具预算超限数量为零。
- 延迟预算超限数量为零。

有限数据集覆盖已知风险、组合挑战和设计阶段准备的未见变体。影子请求、人工纠正和新故障持续进入版本化案例库，用于补充后续数据版本。

`benchmarks/mapping_investigation_scenarios_v1.jsonl` 提供 12 个可执行场景。执行 `python3 script/run_mapping_benchmark.py` 会真实运行调查状态机、只读工具、分层检索、案例记忆、上下文预算和结果评估。当前确定性基线结果为：

- 12 个场景通过率 100%。
- 必要异常信号召回率 100%。
- 错误自动路由、无来源证据、工具预算超限和延迟预算超限均为 0。
- 已知 bad case 命中案例记忆后使用 3 次工具调用完成调查。
- 未见后缀型号命中文档证据后使用 4 次工具调用完成调查。
- 无证据场景使用 5 次工具调用后保持挂起。
- 多源冲突场景使用 6 次工具调用后保持挂起。

消融测试关闭案例记忆后，`bad-broad-rule-003` 从正确处理退化；关闭文档检索后，`unseen-suffix-007` 从正确处理退化。两个配置的场景通过率均降至 91.67%，严格发布门禁失败。这组结果证明两类上下文在当前版本化场景中的独立贡献。

上述数字的适用范围是当前提交中的 12 个内部合成场景和确定性规划器。benchmark 的临时案例库优先使用内存文件系统，执行序列化、版本控制、检查点和原子替换，并关闭磁盘 `fsync`，延迟数据只用于发现明显预算超限。生产调查库保持持久化写入。真实业务效果将使用脱敏历史异常、正常请求、边界请求和后续影子流量继续测量，指标包括分类准确率、拒答准确率、平均工具调用数、模型 token、调查耗时和同类问题复用率。

## 后续生产验证

1. 从脱敏历史工单和人工纠正记录构建独立标注集，替换合成场景中的业务标签。
2. 对产品说明书建立检索召回标注；关键词和属性检索召回不足的文档范围再加入向量索引。
3. 在企业批准的模型服务上评估模型规划器，对比确定性基线的准确率、拒答、token、耗时和失败恢复。
4. 在管理页面实现异常列表、证据链路、检查点回正和重跑操作。
5. 对规则候选接入现有配对回放、影子验证、人工批准、发布监控和回滚控制。
