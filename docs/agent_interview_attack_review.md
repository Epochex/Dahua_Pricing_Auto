# Hermes Agent Interview Attack Review

This document stress-tests the Dahua Pricing Agent against common Agent / RAG / tool-calling interview questions.

Reference set:

- `https://xiaolinnote.com/ai/agent/`
- `https://xiaolinnote.com/ai/rag/`
- `https://xiaolinnote.com/ai/tools/1_function_calling.html`
- `https://xiaolinnote.com/ai/agent/13_handcode.html`

The point is not to force every interview topic into the project. The point is to identify which questions genuinely attack this system, then turn those questions into concrete engineering structure.

## 1. Agent vs LLM vs Workflow

Likely attack:

> Is this really an Agent, or just a scheduled workflow with API calls?

Strong answer:

This system is not defined by whether every step calls an LLM. It is an Agent system because it has persistent sessions, memory, skill selection, tool dispatch, observations, reflection candidates, and replay evaluation across a long-running business process.

Current evidence:

- Gateway: Alidocs push, DingTalk event endpoint, scheduler/manual triggers.
- Session/trace: `GET /api/agent/traces`, one `run_id` per business run.
- Memory: `GET /api/agent/memory/pla/{pla_no}`.
- Skills: `GET /api/agent/skills`, versioned business procedures.
- Tool Dispatch: Windows GSP agent and Alidocs script are external tool backends.
- Reflection/Eval: `GET /api/agent/reflections`, `POST /api/agent/evals/replay`.

The correct framing is:

> Deterministic workflow where the business rule is stable, Agent orchestration where context, tool choice, memory, and recovery matter, and LLM only where natural-language interpretation or adaptive reasoning is useful.

## 2. Tools, Function Calling, MCP, Skills

Likely attack:

> Does the model execute tools? How do Skills differ from tools?

Strong answer:

The model, if introduced, only decides. Host code executes. In this project, the current core does not depend on LLM calls for GSP status checks. Tool execution is explicit:

- `gsp.status_check`: performed by Windows agent.
- `alidocs.status_writeback`: performed inside Alidocs script.
- `trace.store`, `memory.pla_timeline`, `eval.replay`: performed by the Linux backend.

Skills are not the same thing as tools. A Skill is a versioned business procedure with risk level, allowed tools, preconditions, postconditions, eval criteria, and a plan graph.

Current evidence:

- `check_gsp_approval_status`
- `apply_alidocs_status_update`
- `official_release_product_in_gsp`

The high-risk release skill is intentionally `draft`, requires human approval, and includes a dry-run and postcondition verification plan.

## 3. ReAct, Plan-and-Execute, Reflection

Likely attack:

> Which Agent pattern did you actually use?

Strong answer:

This project is not a generic ReAct chat loop. It uses a Hermes-style plan-execute-observe-reflect structure:

- Plan: skill `plan_graph`.
- Execute: selected tool backend runs the step.
- Observe: GSP result / Alidocs ACK / failure evidence.
- Reflect: failed tool execution, write-back gaps, and duplicate suppression create reflection candidates.
- Evaluate: replay eval checks row selection and write-back correctness.

ReAct becomes useful later for incoming natural-language commands, but the core business process benefits more from explicit plans and strict postconditions.

## 4. Complex Task Decomposition

Likely attack:

> How do you decompose complex tasks instead of one giant prompt?

Strong answer:

Each business skill is decomposed into a plan graph. Example:

`check_gsp_approval_status`

1. context assembly
2. tool backend selection
3. GSP query
4. observation normalization
5. decision and memory update

`official_release_product_in_gsp`

1. context assembly
2. dry-run locate product
3. risk gate
4. official release
5. postcondition check

This makes high-risk GSP mutations inspectable before they become executable.

## 5. Memory Design

Likely attack:

> Where is memory stored? What is the granularity? How do you avoid prompt bloat?

Strong answer:

Memory is layered:

- Trace store: per-run execution history.
- PLA timeline memory: per-PLA business memory.
- Compact memory: compressed summary attached to each PLA timeline.
- Skill memory: versioned procedures and risk policy.
- Reflection candidates: proposed improvements, not automatically promoted.

Memory is not blindly injected. A session can retrieve compact PLA memory first, then drill into raw events only when needed.

Current compact memory fields include:

- latest sheet / row / PN / requester
- latest GSP status
- write-back state
- run count
- status event count
- approved count
- failure count
- reflection count
- attention flag

This directly answers memory compression and granularity questions.

## 6. Reflection And Self-Improvement

Likely attack:

> What prevents "self-evolution" from becoming hallucinated self-modification?

Strong answer:

Reflection does not directly mutate Skills. It creates `reflection_candidate` records with evidence, risk level, and proposed update.

Promotion path:

1. observe failure or policy gap
2. create candidate with evidence
3. evaluate against replay data
4. human approves high-risk changes
5. create a new Skill version
6. future runs point to the new active version

This keeps the self-improvement loop auditable and gated.

## 7. Multi-Agent Collaboration And Dynamic Switching

Likely attack:

> If there are multiple Windows agents, how does the backend choose?

Strong answer:

Windows agents are tool backends, not separate magical brains. Each backend sends heartbeat with:

- stable backend ID
- capabilities
- status
- load
- metadata
- last heartbeat time

The backend selects by capability and lowest freshness-adjusted load. The selected backend is recorded into trace metadata and each queued task.

Current evidence:

- `POST /api/agent/tool-backends/heartbeat`
- `GET /api/agent/tool-backends`
- `selected_tool_backend` in GSP queue traces and task items

This design naturally supports future backends:

- `gsp.status_check`
- `gsp.product_release_browser`
- `alidocs.status_writeback`
- `manual.review`
- `crm.sync`

## 8. Hand-Written Core vs Framework

Likely attack:

> Why not use LangChain / LangGraph directly?

Strong answer:

The core is deliberately hand-written because this is a production business process with strict audit, idempotency, and tool-side constraints. The project needs transparent control over:

- row identity checks
- GSP mutation gates
- write-back idempotency
- trace schema
- memory promotion
- replay eval

Frameworks can be used around the edges: Langfuse for traces/evals, vector stores for future knowledge retrieval, LLM SDKs for intent classification. The control loop and business state transitions stay explicit.

## 9. RAG Relevance

Many RAG questions are not directly relevant yet because the current system is not document-QA driven.

Relevant parts:

- hallucination prevention
- eval metrics
- dynamic knowledge update
- retrieval granularity if future GSP/SOP docs are added

Current answer:

The system avoids hallucination by grounding decisions in structured tool observations:

- GSP status is accepted only from GSP tool output.
- Alidocs write-back is accepted only after ACK.
- Reflection candidates are evidence-backed records.
- Replay eval checks false completion and missing write-back.

Future RAG should retrieve SOPs, approval policies, and GSP operation guides as supporting context, not as authority for final state. Final state still comes from GSP and Alidocs observations.

## 10. Questions To Ignore For This Project

These are not useful to force into the current project narrative:

- transformer training details
- embedding algorithm internals
- vector database benchmark minutiae
- LoRA / fine-tuning implementation
- WebRTC streaming unless the incoming message channel requires it

They may matter in a platform team interview, but they do not strengthen this specific enterprise workflow agent unless the system expands into knowledge-base QA or real-time multimodal chat.

## 11. Resume-Grade Claim

A defensible claim:

> Designed and implemented a Hermes-style enterprise workflow agent for pricing operations, with message/event gateway, session traces, PLA timeline memory with compression, versioned business skills, capability-based tool backend dispatch across Windows/GSP/Alidocs executors, reflection candidates, and replay evaluation for write-back correctness.

This claim is stronger than "built a bot" because every term maps to a concrete API, data entity, or execution path.
