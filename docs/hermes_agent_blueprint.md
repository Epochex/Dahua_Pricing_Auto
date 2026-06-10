# Dahua Pricing Agent: Hermes-Native Upgrade Blueprint

## 1. Positioning

This project should not be framed as "a bot that checks PLA status".

The stronger framing is:

- a persistent enterprise agent that grows with the business workflow
- with a messaging gateway, scheduled automations, and tool dispatch
- with Hermes-style prompt/context assembly before action
- with bounded curated memory plus external workflow memory
- with reusable procedural skills for GSP, Alidocs, and notification tasks
- with a closed learning loop: observe, reflect, consolidate, improve

The current repository already contains the seed of that system:

- Alidocs push script as a business-event adapter
- Linux backend as the Hermes-inspired agent core
- Windows desktop agent as a tool backend with GSP capability
- GSP as a protected enterprise tool environment
- DingTalk/Huachat as the messaging gateway and notification surface

This is already beyond a toy MVP. The upgrade path is to formalize it into a Hermes-style architecture.

## 2. Current State

Current implemented flow:

1. `script/alidocs_push_all_sheets.js`
   pushes all sheet rows to `/api/agent/sheet/push`
2. backend parses rows into structured tasks
3. backend filters rows where PLA exists and sheet status is not completed
4. Windows agent fetches queue from `/api/agent/gsp/status-queue`
5. Windows agent checks GSP via API-first mode and browser fallback mode
6. Windows agent posts result to `/api/agent/gsp/status-result`
7. backend queues online sheet write-back
8. `script/alidocs_apply_status_updates.js`
   writes `L{row}=已完成` and ACKs the update

This means the system already has:

- event ingestion
- task parsing
- execution routing
- result persistence
- feedback write-back
- partial observability

Hermes-native entities now implemented in the backend:

- trace store: `GET /api/agent/traces`, `GET /api/agent/traces/{run_id}`
- PLA timeline memory: `GET /api/agent/memory/pla/{pla_no}`
- compact PLA memory: stored inside each PLA timeline as `compact_memory`
- versioned skills with plan graphs: `GET /api/agent/skills`, `GET /api/agent/skills/{skill_name}`
- tool backend registry: `GET /api/agent/tool-backends`, `POST /api/agent/tool-backends/heartbeat`
- reflection candidates: `GET /api/agent/reflections`
- replay evaluation: `POST /api/agent/evals/replay`

These are intentionally file-backed first, because the current deployment already uses filesystem-backed agent state. The schema is shaped so it can later move to Postgres and Langfuse without changing the agent contract.

## 3. Why Hermes Fits

Hermes is a better conceptual match than a thin "chat bot" because this project is:

- long-horizon
- multi-system
- stateful
- repetitive
- failure-prone in the real world
- improved by persistent context and memory

The key value is not the loop itself. Plenty of agent systems have loops.

The value is:

- bounded but durable memory
- context assembly before execution
- post-run learning and strategy refinement
- a persistent execution identity across tasks

That maps very naturally to the PLA / inquiry / GSP workflow.

## 4. Hermes-Native Target Architecture

The design should map directly to Hermes concepts instead of becoming a generic workflow backend.

```text
Messaging Gateway / Cron / Alidocs Adapter
  -> Business MessageEvent
  -> Agent Session
  -> Prompt + Context Assembly
  -> Memory Snapshot + Relevant Workflow Records
  -> Skill Selection
  -> Tool Dispatch
  -> Observation
  -> Reflection
  -> Memory / Skill Update
  -> Delivery / Write-back
```

### 4.1 Gateway Layer

This is the project equivalent of Hermes Gateway plus Cron.

Sources:

- Alidocs push
- DingTalk incoming callback
- scheduled poll runs
- manual backend trigger
- future email / ticket / CRM events

Every source should be normalized into a business message event:

```json
{
  "event_id": "evt_xxx",
  "source": "alidocs|dingtalk|scheduler|manual",
  "source_ref": "sheet_push_id|conversation_id|job_id",
  "intent": "check_approval_status",
  "payload": {},
  "received_at": "2026-06-10T00:00:00Z"
}
```

### 4.2 Agent Session Layer

This is where the system stops being a webhook handler.

Each trigger opens or resumes an agent session:

- sheet-scoped session: for one Alidocs push
- PLA-scoped session: for one approval lifecycle
- user/group-scoped session: for incoming chat commands
- scheduled session: for cron-like unattended audits

The session carries lineage, so follow-up checks, write-backs, and manual reviews are tied to the same history.

### 4.3 Prompt And Context Assembly

This is the Hermes heart: context is deliberately assembled before tools run.

Inputs:

- latest parsed sheet snapshot
- row-level task info
- relevant PLA memory
- relevant PN/product memory
- latest GSP result
- notification policy
- operator hints
- enabled skills
- allowed tools and risk policy

Output:

```json
{
  "run_id": "run_xxx",
  "intent": "check_approval_status",
  "sheet": "2026.06",
  "row_index": 23,
  "pla_no": "PLA20260605143508433",
  "pn": "1.0.01.15.12158",
  "requester": "xiyan",
  "current_sheet_status": "进行中",
  "last_known_gsp_status": "Approved",
  "execution_policy": {
    "query_mode": "api_first",
    "retry_limit": 2,
    "allow_browser_fallback": true,
    "notify": false
  }
}
```

The important point: this context is not a large dump of logs. It is a compact, curated working set, similar in spirit to Hermes prompt tiers and context compression.

### 4.4 Memory Manager

Memory should be explicit and layered, not just "lots of logs".

Hermes has bounded curated memory. This project should keep the same discipline:

- small always-active memory for durable facts and conventions
- larger external memory for structured workflow history
- consolidation rules that decide what graduates from trace to memory

#### Core Memory

Small curated facts that should affect every future run:

- GSP is the source of truth for approval status
- Alidocs can write back only through sheet-side scripts unless official API permission exists
- high-risk GSP write actions require approval
- current robot notification policy

This is the project equivalent of `MEMORY.md`: compact, opinionated, and always injected.

#### Episodic Memory

Per-PLA / per-task history:

- when first seen
- which sheet rows it appeared in
- status timeline
- who triggered checks
- how it was resolved

This is not prompt bloat. It is retrieved and compressed into the session only when relevant.

#### Procedural Memory / Skills

Reusable business procedures:

- `check_gsp_approval_status`
- `apply_alidocs_status_update`
- `official_release_product_in_gsp`
- `summarize_blocked_approval`
- `escalate_manual_review`

Each Skill carries:

- active version
- risk level
- allowed tools
- preconditions and postconditions
- evaluation criteria
- plan graph

The plan graph is the answer to "how do you decompose complex tasks?" It makes each business procedure inspectable before it is executed.

#### Compact Memory

PLA timeline memory keeps the raw event stream, but the session should normally consume the compact view:

- latest sheet / row / PN / requester
- latest GSP status
- write-back state
- run count
- status event count
- approved / failed counts
- reflection count
- attention flag

This prevents the memory layer from becoming prompt bloat or an unbounded log dump.

Each skill should contain:

- when to use it
- required context
- allowed tools
- risk level
- preconditions
- postconditions
- eval criteria

This is the Hermes "skills evolve during use" angle. A failed official-release run should improve the skill notes, not just leave an error log.

#### Operational Memory

System behavior memory:

- GSP API failure patterns
- browser fallback success rate
- frequent approval bottlenecks by owner / country / product line
- recurring parsing ambiguities

#### Policy Memory / Soul

Human-curated memory:

- allowed groups
- escalation rules
- notification style
- quiet hours
- preferred execution strategy per environment

This is closer to Hermes `USER.md` / policy context than a generic config table: it shapes the agent's behavior across sessions.

### 4.5 Tool Registry And Dispatch

Executors should be exposed as tools with capability metadata:

- `sheet.push_parse`
- `alidocs.status_writeback`
- `gsp.status_check`
- `gsp.status_api`
- `gsp.browser_status_checker`
- `gsp.product_release_browser`
- `dingtalk.notify`
- `human.approval_gate`

Each tool needs:

- schema
- capability tags
- risk level
- idempotency key rules
- observable result schema
- failure classification

Tool backends register themselves separately from Skills:

- backend ID
- display name
- capabilities
- online/degraded/offline status
- load
- last heartbeat
- environment metadata

This is the multi-agent switching layer. A Skill chooses required capability; the Hermes core selects the currently healthy backend and records that choice in the trace.

### 4.6 Reflection And Learning Loop

After each run, the agent should ask:

- what worked?
- what failed?
- which skill note should be updated?
- which memory should be consolidated?
- should executor preference change?
- should this case become an eval dataset item?

That is the Hermes flavor. The system learns from work without pretending every action needs an LLM.

## 5. Agent Control Loop

This is the real heart of the system.

Hermes-style run state:

```text
ingested
  -> session_resolved
  -> context_assembled
  -> skill_selected
  -> tool_plan_ready
  -> policy_checked
  -> executing
  -> observed
  -> reflected
  -> memory_consolidated
  -> writeback_pending
  -> completed
  -> or failed / manual_review
```

Each run should have:

- stable `run_id`
- parent trigger reference
- selected strategy
- execution trace
- result artifact
- evaluation result

This is where Hermes-style control pays off:

- idempotency
- retries
- fallbacks
- branch decisions
- post-run summarization

## 6. Tool Backend Layer

Windows agents, API clients, and Alidocs scripts are not "the agent". They are tool backends.

### Current Tool Backends

- `sheet_push_parser`
- `gsp_api_status_checker`
- `gsp_browser_status_checker`
- `sheet_writeback_script`
- `dingtalk_notify_webhook`

### Future Tool Backends

- `dingtalk_incoming_gateway`
- `manual_review_gate`
- `gsp_product_release_browser`
- `pricing_executor` when re-enabled
- `crm_sync_tool`
- `approval_escalation_tool`

Execution strategy should be dynamic:

- default `api_first`
- fallback `browser`
- final fallback `manual_review`

## 7. Evaluation Layer

This is where the system becomes "not just MVP".

Every run should be scored on:

- trigger understanding correctness
- row selection correctness
- PLA extraction correctness
- GSP query success
- returned status correctness
- sheet write-back correctness
- notification correctness
- time to completion

Two eval modes should exist:

### Offline Eval

Replay historical sheet pushes and historical PLA outcomes as datasets.

Questions:

- Did we select the right rows?
- Did we mark the right rows complete?
- Did we avoid false positives?
- Did API-first reduce failures vs browser-first?

### Online Eval

Capture production traces and compute:

- queue success rate
- GSP lookup success rate
- write-back success rate
- manual intervention rate
- mean end-to-end latency
- false completion rate

## 8. Langfuse Fit

Langfuse fits this project very well, even if most of the current chain is not LLM-driven yet.

Why it still fits:

- traces are still useful for agent runs
- evals are useful for workflow correctness
- datasets are useful for regression testing
- future incoming natural-language commands will likely use LLM interpretation

Recommended Langfuse usage:

1. trace each run as one top-level trace
2. create spans for:
   - sheet_push_parse
   - task_filter
   - gsp_queue_fetch
   - gsp_api_query
   - gsp_browser_fallback
   - result_persist
   - sheet_writeback
   - notification
3. attach scores:
   - `row_selection_correct`
   - `status_match`
   - `writeback_applied`
   - `manual_intervention_required`
4. later, when incoming chat is enabled:
   - log user prompt
   - log intent classification
   - log tool routing decision

## 9. Loop Agent vs Hermes

If by "Loop Agent" you mean the current wave of loop-centric agents, then the overlap is real:

- both rely on perceive / reason / act / observe cycles
- both can persist state across steps
- both can route tools conditionally

But in practice the difference is usually one of emphasis:

- loop agents emphasize the control cycle
- Hermes emphasizes memory, continuity, and post-run improvement

For this project, a plain loop is not enough.

Why:

- the system must remember prior PLA runs
- the system must learn which executor path is more reliable
- the system must avoid duplicate writes and duplicate notifications
- the system must accumulate operational knowledge over time

So the right conclusion is:

- loop mechanics are necessary
- Hermes-style memory and evaluation are what make the project resume-worthy

## 10. Resume Narrative

Weak narrative:

- built a bot to query approval status

Strong narrative:

- designed and implemented a context-aware enterprise agent system spanning online sheets, internal approval systems, Linux orchestration services, and Windows execution agents
- built adaptive API-first / browser-fallback execution for approval-state automation in restricted intranet environments
- introduced structured run memory, evaluation hooks, and traceable write-back loops to improve workflow reliability and reduce manual follow-up

## 11. Recommended Next Build Stages

### Stage A: Operational Backbone

- formalize run IDs and trace records
- add run history listing APIs
- add executor decision records
- add manual review queue

### Stage B: Memory

- per-PLA timeline store
- per-run artifact store
- environment reliability memory
- notification policy memory

### Stage C: Eval

- historical replay dataset
- automated regression checks
- write-back correctness evaluation
- latency / failure dashboards

### Stage D: Incoming

- DingTalk event callback
- intent classification
- safe command gating
- group / user authorization

### Stage E: Intelligence

- LLM-based incoming command understanding
- approval anomaly summarization
- operator-facing explanations
- policy suggestions from historical traces

## 12. Final Judgment

This project is absolutely capable of becoming a serious Hermes-style system.

The winning move is not to turn everything into LLM calls.

The winning move is:

- keep deterministic automation where it is reliable
- add context assembly where state matters
- add memory where repeated work accumulates
- add eval where trust matters
- add LLM reasoning only where interpretation or adaptive decision-making truly helps

That yields a system that is more real, more defensible, and much stronger on a resume than a generic "AI bot".
