# Langfuse Eval Plan For Dahua Pricing Agent

## 1. Goal

Use Langfuse as the evaluation and observability layer for the agent system, not only for future LLM prompts, but also for current workflow runs.

The purpose is to make the system:

- debuggable
- regression-testable
- measurable
- presentable as a real agent platform

## 2. What To Trace

One top-level trace per run:

```text
trace = one approval-check run
```

Suggested spans:

1. `sheet_push_ingest`
2. `sheet_parse`
3. `task_filter`
4. `queue_create`
5. `executor_select`
6. `gsp_api_query`
7. `gsp_browser_fallback` if used
8. `result_persist`
9. `sheet_update_queue`
10. `sheet_writeback`
11. `notification_send`

Trace metadata:

- `run_id`
- `event_id`
- `push_id`
- `sheet`
- `row_index`
- `pla_no`
- `pn`
- `requester`
- `executor_mode`
- `result_status`

## 3. What To Score

Recommended score fields:

### Selection Scores

- `score.row_selected_correctly`
- `score.pla_extracted_correctly`
- `score.sheet_target_correctly`

### Execution Scores

- `score.gsp_query_success`
- `score.gsp_status_valid`
- `score.browser_fallback_used`
- `score.result_persisted`

### Outcome Scores

- `score.writeback_applied`
- `score.notification_expected`
- `score.notification_sent`
- `score.false_completion`

### Operational Scores

- `score.requires_manual_review`
- `score.retry_count`
- `score.end_to_end_latency_ms`

## 4. Offline Dataset Design

Create replay datasets from historical pushes and known outcomes.

Dataset item shape:

```json
{
  "dataset_id": "approval_replay_v1",
  "push_id": "eb90eea9976b4e33",
  "sheet": "2026.06",
  "row_index": 23,
  "pla_no": "PLA20260605143508433",
  "expected_should_queue": true,
  "expected_status": "Approved",
  "expected_writeback_cell": "L23",
  "expected_writeback_value": "已完成"
}
```

Useful offline eval suites:

1. row filtering regression
2. PLA extraction regression
3. GSP status normalization regression
4. write-back correctness regression
5. duplicate suppression regression

## 5. Online Eval Questions

Production-facing questions:

1. Did we choose the right rows from the latest sheet snapshot?
2. Did the executor return a plausible status?
3. Did we mark rows complete only when they should be complete?
4. How often did we need browser fallback?
5. Where do failures cluster?

## 6. Immediate Integration Plan

### Phase 1: Trace-Only

No LLM dependency required.

- instrument backend runs with trace events
- emit JSON logs with stable IDs
- mirror those to Langfuse traces later

### Phase 2: Workflow Evals

- build offline replay set from saved `sheet_push/` and `gsp_status/`
- compute scores and attach to traces

### Phase 3: Incoming Command Evals

After DingTalk incoming is available:

- log incoming message text
- evaluate intent match
- evaluate target sheet extraction
- evaluate safe trigger gating

### Phase 4: LLM Evals

Only once LLM-based interpretation is added:

- prompt correctness
- tool selection correctness
- summarization quality
- false-trigger rate

## 7. Why This Matters For Resume Story

Without eval:

- this looks like an automation script bundle

With eval:

- this becomes an agent platform with measurable correctness and operational quality

That is much stronger in interviews because you can talk about:

- reliability engineering
- traceability
- agent observability
- production feedback loops
- data-driven iteration

## 8. Suggested Resume Line

- integrated structured traces and evaluation workflows for a cross-system enterprise agent, enabling replay-based regression checks and production observability for approval-status automation

## 9. Suggested Next Implementation

1. add stable `run_id` creation for every inbound trigger
2. persist per-run trace JSON on the backend
3. expose a small trace listing API
4. map trace schema to Langfuse spans and scores
5. build one offline replay script over historical pushes
