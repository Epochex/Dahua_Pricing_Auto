import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiGetJson, apiPostJson } from "./api.js";
import { safeStr } from "./format.js";

const DEMO_KEY_STORAGE = "dahuaPricingDemoKey";

function readDemoKey() {
  try {
    return window.localStorage.getItem(DEMO_KEY_STORAGE) || "";
  } catch {
    return "";
  }
}

function writeDemoKey(value) {
  try {
    if (value) window.localStorage.setItem(DEMO_KEY_STORAGE, value);
    else window.localStorage.removeItem(DEMO_KEY_STORAGE);
  } catch {
    /* 隐私模式下写不进去也不影响本次会话 */
  }
}

function demoKeyHeaders() {
  const key = readDemoKey();
  return key ? { "X-Demo-Key": key } : {};
}

/* =====================================================================
 * 端到端演示 · 执行视图
 *
 * 接口契约见 demo_pipeline_contract.md：
 *   GET  /api/demo/config
 *   POST /api/demo/run
 *   GET  /api/demo/runs/{run_id}/stream   (SSE)
 *   GET  /api/demo/runs/{run_id}          (快照，SSE 兜底 + 回放共用)
 *   GET  /api/demo/runs                   (历史列表)
 *   GET  /api/demo/runs/{run_id}/artifact  (xlsx 下载)
 *
 * 执行图的节点与分支是**静态**的（系统设计是静态的），运行时只用真实 step
 * 状态点亮走过的那条路径。所有分支条件都逐条核对过 backend/app/demo_pipeline.py，
 * 每条边在后端都有对应代码分支，见 BRANCHES 里的 source 字段。
 *
 * USE_MOCK 只用于后端未就绪时的本地渲染自测。默认必须为 false，
 * 打开后页面顶部会常驻醒目的 MOCK 警示条，避免假数据被当成真实结果。
 * ===================================================================== */
const USE_MOCK = false;

/* ---------------------------------------------------------------------
 * 静态骨架：即使后端少返回字段 / 少返回节点，7 个真实节点也必须画全
 * ------------------------------------------------------------------- */
const STEP_BLUEPRINT = [
  {
    key: "message",
    index: 1,
    title: "消息构造与字段解析",
    subtitle: "PN · 客户 · 层级 · 产品线",
    layer: "local",
    graphTitle: "消息构造与解析",
    graphSub: "脱敏消息 · 14 列"
  },
  {
    key: "ingress",
    index: 2,
    title: "Cloud Run 入口",
    subtitle: "鉴权 · 契约校验 · 派发",
    layer: "cloud",
    graphTitle: "Cloud Run 入口",
    graphSub: "鉴权 · 契约校验"
  },
  {
    key: "workflow",
    index: 3,
    title: "Google Workflows 编排",
    subtitle: "execution id · 执行状态",
    layer: "cloud",
    graphTitle: "Workflows 编排",
    graphSub: "执行状态 · 去重"
  },
  {
    key: "sheet",
    index: 4,
    title: "Google Sheets 任务行",
    subtitle: "updatedRange · 去重判定",
    layer: "cloud",
    graphTitle: "Sheets 任务行",
    graphSub: "updatedRange"
  },
  {
    key: "ingest",
    index: 5,
    title: "内网摄取与任务建立",
    subtitle: "任务 id · 幂等键",
    layer: "local",
    graphTitle: "内网摄取建任务",
    graphSub: "任务 id · 幂等键"
  },
  {
    key: "pricing",
    index: 6,
    title: "确定性算价",
    subtitle: "匹配路径 · 规则/数据版本",
    layer: "local",
    graphTitle: "确定性算价",
    graphSub: "匹配 · 版本"
  },
  {
    key: "artifact",
    index: 7,
    title: "GSP 模板制品",
    subtitle: "xlsx · 人工复核边界",
    layer: "local",
    graphTitle: "GSP 模板制品",
    graphSub: "xlsx 制品"
  }
];

const BLUEPRINT_BY_KEY = STEP_BLUEPRINT.reduce((acc, s) => {
  acc[s.key] = s;
  return acc;
}, {});

/* 永远不点亮的灰节点：这是刻意的诚实标注，不要改成可点亮状态 */
const GHOST_NODES = [
  {
    key: "pubsub",
    title: "Pub/Sub",
    tag: "规划中",
    why: "入口当前同步派发 Workflows，未接消息队列缓冲"
  },
  {
    key: "eventarc",
    title: "Eventarc",
    tag: "规划中",
    why: "当前由入口显式调用 Workflows executions.create"
  },
  {
    key: "windows",
    title: "Windows 执行端",
    tag: "已退役",
    why: "已由 Cloud Run + Workflows + 内网服务取代"
  }
];

const STATE_META = {
  pending: { label: "待执行", cls: "pending" },
  running: { label: "执行中", cls: "running" },
  ok: { label: "成功", cls: "ok" },
  duplicate: { label: "重复 · 未追加", cls: "duplicate" },
  blocked: { label: "已拦截", cls: "blocked" },
  skipped: { label: "已跳过", cls: "skipped" },
  error: { label: "失败", cls: "error" }
};

/* 色值全部取自 styles.css 既有调色板，不引入新色系 */
const PALETTE = {
  idle: "#cfc7b9",
  pending: "#b3aca0",
  running: "#7a5d18",
  ok: "#2f6b55",
  duplicate: "#c96a12",
  blocked: "#8c3a3a",
  error: "#6f2e2e",
  skipped: "#a9a294",
  ink: "#1b1b1b",
  muted: "#5a5a5a",
  line: "#d8d1c5",
  surface: "#fbf7f1"
};

const SCENARIOS = [
  { key: "normal", label: "正常链路", hint: "七跳依次点亮，末跳给出 xlsx 与人工复核边界" },
  { key: "duplicate", label: "重发同一条消息", hint: "复用上一次 message_id，Sheets 不追加第二行" },
  { key: "blocked", label: "越权消息被拦截", hint: "不满足契约，入口 422 拦截，未触达外部系统" }
];

const TERMINAL_STATES = new Set(["ok", "duplicate", "blocked", "skipped", "error"]);
const LIVE_STATES = new Set(["running", "ok", "duplicate", "blocked", "error"]);

/* ---------------------------------------------------------------------
 * 防御性归一化：后端可能少字段 / 字段形状不同 / 时间戳单位不同
 * ------------------------------------------------------------------- */
function normState(v) {
  const s = String(v || "").toLowerCase().trim();
  return STATE_META[s] ? s : "pending";
}

function toEpochMs(v) {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  if (!Number.isFinite(n) || n <= 0) return null;
  /* 契约给的是 epoch 秒（浮点），但容忍后端直接给毫秒 */
  return n > 1e11 ? n : n * 1000;
}

function toMs(v) {
  /* 后端对未执行的节点给的是 duration_ms: null，而 Number(null) === 0，
     不先挡一道的话待执行节点会显示成「0 ms」，看着像已经跑完且瞬时完成。 */
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  if (!Number.isFinite(n) || n < 0) return null;
  return n;
}

function renderScalar(v) {
  if (v === null || v === undefined) return "";
  if (typeof v === "object") {
    try {
      return JSON.stringify(v);
    } catch {
      return String(v);
    }
  }
  if (typeof v === "boolean") return v ? "true" : "false";
  return safeStr(v);
}

function normFacts(raw) {
  if (!raw) return [];
  const out = [];
  if (Array.isArray(raw)) {
    raw.forEach((item, i) => {
      if (item === null || item === undefined) return;
      if (Array.isArray(item)) {
        out.push({ label: safeStr(item[0]), value: renderScalar(item[1]) });
        return;
      }
      if (typeof item === "object") {
        const label = item.label ?? item.k ?? item.key ?? item.name ?? `#${i + 1}`;
        const value = "value" in item ? item.value : item.v ?? item.val;
        out.push({ label: safeStr(label), value: renderScalar(value) });
        return;
      }
      out.push({ label: `#${i + 1}`, value: renderScalar(item) });
    });
    return out;
  }
  if (typeof raw === "object") {
    Object.keys(raw).forEach((k) => out.push({ label: k, value: renderScalar(raw[k]) }));
    return out;
  }
  return [{ label: "值", value: renderScalar(raw) }];
}

function normLinks(raw) {
  if (!raw) return [];
  const arr = Array.isArray(raw) ? raw : [raw];
  const out = [];
  arr.forEach((item) => {
    if (!item) return;
    if (typeof item === "string") {
      if (/^https?:\/\//i.test(item)) out.push({ label: item, url: item });
      return;
    }
    if (typeof item === "object") {
      const url = safeStr(item.url ?? item.href ?? "");
      if (!/^https?:\/\//i.test(url)) return;
      out.push({ label: safeStr(item.label ?? item.title ?? url), url });
    }
  });
  return out;
}

function normStep(raw, fallbackIndex) {
  const src = raw && typeof raw === "object" ? raw : {};
  const key = safeStr(src.key || "").trim();
  const bp = BLUEPRINT_BY_KEY[key] || {};
  const startedAt = toEpochMs(src.started_at);
  const endedAt = toEpochMs(src.ended_at);
  let durationMs = toMs(src.duration_ms);
  if (durationMs === null && startedAt !== null && endedAt !== null && endedAt >= startedAt) {
    durationMs = endedAt - startedAt;
  }
  const layerRaw = String(src.layer || "").toLowerCase();
  return {
    key: key || bp.key || `step-${fallbackIndex}`,
    index: Number.isFinite(Number(src.index)) ? Number(src.index) : bp.index ?? fallbackIndex,
    title: safeStr(src.title || bp.title || key || "未命名节点"),
    subtitle: safeStr(src.subtitle ?? bp.subtitle ?? ""),
    layer: layerRaw === "cloud" || layerRaw === "local" ? layerRaw : bp.layer || "local",
    state: normState(src.state),
    startedAt,
    endedAt,
    durationMs,
    facts: normFacts(src.facts),
    request: src.request ?? null,
    response: src.response ?? null,
    links: normLinks(src.links),
    note: safeStr(src.note ?? ""),
    hasPayload: src.request !== undefined || src.response !== undefined
  };
}

/* 用骨架补齐，保证 7 个节点恒在；后端多返回的节点按 index 追加 */
function normalizeSteps(rawSteps) {
  const list = Array.isArray(rawSteps) ? rawSteps : [];
  const byKey = new Map();
  list.forEach((s, i) => {
    const n = normStep(s, i + 1);
    byKey.set(n.key, n);
  });
  const out = STEP_BLUEPRINT.map((bp) => {
    const got = byKey.get(bp.key);
    byKey.delete(bp.key);
    if (got) return got;
    return {
      key: bp.key,
      index: bp.index,
      title: bp.title,
      subtitle: bp.subtitle,
      layer: bp.layer,
      state: "pending",
      startedAt: null,
      endedAt: null,
      durationMs: null,
      facts: [],
      request: null,
      response: null,
      links: [],
      note: "",
      hasPayload: false
    };
  });
  const extras = Array.from(byKey.values()).sort((a, b) => a.index - b.index);
  return out.concat(extras);
}

function mergeStepPatch(steps, rawStep) {
  const src = rawStep && typeof rawStep === "object" ? rawStep : {};
  const patch = normStep(src, steps.length + 1);
  const hasState = STATE_META[String(src.state || "").toLowerCase().trim()] !== undefined;
  let found = false;
  const next = steps.map((s) => {
    if (s.key !== patch.key) return s;
    found = true;
    /* 只覆盖后端本次给出的内容，缺字段一律沿用已有值 */
    return {
      ...s,
      ...patch,
      state: hasState ? patch.state : s.state,
      title: patch.title || s.title,
      subtitle: patch.subtitle || s.subtitle,
      facts: patch.facts.length ? patch.facts : s.facts,
      links: patch.links.length ? patch.links : s.links,
      request: patch.request !== null ? patch.request : s.request,
      response: patch.response !== null ? patch.response : s.response,
      note: patch.note || s.note,
      startedAt: patch.startedAt !== null ? patch.startedAt : s.startedAt,
      endedAt: patch.endedAt !== null ? patch.endedAt : s.endedAt,
      durationMs: patch.durationMs !== null ? patch.durationMs : s.durationMs
    };
  });
  if (found) return next;
  return next.concat([patch]).sort((a, b) => a.index - b.index);
}

function normalizeRun(raw, fallback) {
  const src = raw && typeof raw === "object" ? raw : {};
  const base = fallback || {};
  return {
    run_id: safeStr(src.run_id || base.run_id || ""),
    scenario: safeStr(src.scenario || base.scenario || ""),
    messageId: safeStr(src.message_id || base.messageId || ""),
    startedAt: toEpochMs(src.started_at) ?? base.startedAt ?? null,
    endedAt: toEpochMs(src.ended_at) ?? null,
    totalMs: toMs(src.total_ms),
    finalState: safeStr(src.final_state || ""),
    environment: src.environment && typeof src.environment === "object" ? src.environment : null,
    source: safeStr(src.source || base.source || ""),
    label: safeStr(src.label || base.label || ""),
    /* 落盘失败：本次演示照常结束但不可回放，必须让人看见 */
    persistError: safeStr(src.persist_error || ""),
    /* 仓库内置的 fixture 是占位快照（字段是 PLACEHOLDER、耗时是占位数字），
       必须在界面上和真实运行结果区分开，禁止让占位数据冒充实测值 */
    placeholder: src.placeholder === true || base.placeholder === true,
    steps: normalizeSteps(src.steps)
  };
}

function normalizeRunList(raw) {
  const arr = Array.isArray(raw) ? raw : Array.isArray(raw?.runs) ? raw.runs : [];
  return arr
    .filter((r) => r && typeof r === "object" && r.run_id)
    .map((r) => ({
      run_id: safeStr(r.run_id),
      scenario: safeStr(r.scenario || ""),
      startedAt: toEpochMs(r.started_at),
      finalState: safeStr(r.final_state || ""),
      totalMs: toMs(r.total_ms),
      source: safeStr(r.source || ""),
      label: safeStr(r.label || ""),
      placeholder: r.placeholder === true || /占位/.test(safeStr(r.label || ""))
    }));
}

function isRunComplete(run) {
  if (!run) return false;
  if (run.finalState && run.finalState !== "running" && run.finalState !== "pending") return true;
  const steps = run.steps || [];
  if (!steps.length) return false;
  return steps.every((s) => TERMINAL_STATES.has(s.state));
}

/* ---------------------------------------------------------------------
 * 格式化
 * ------------------------------------------------------------------- */
function fmtMs(ms) {
  if (ms === null || ms === undefined || !Number.isFinite(Number(ms))) return "—";
  const v = Number(ms);
  if (v < 1000) return `${Math.round(v)} ms`;
  return `${(v / 1000).toFixed(2)} s`;
}

function pad2(n) {
  return String(n).padStart(2, "0");
}

function fmtClock(epochMs) {
  if (!Number.isFinite(Number(epochMs))) return "—";
  const d = new Date(Number(epochMs));
  if (Number.isNaN(d.getTime())) return "—";
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(
    d.getHours()
  )}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}

function pretty(v) {
  if (v === null || v === undefined) return "";
  if (typeof v === "string") return v;
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

function deriveServiceName(cfg) {
  const direct = safeStr(cfg?.cloud_run_service || cfg?.service_name || cfg?.service || "");
  if (direct) return direct;
  const url = safeStr(cfg?.cloud_run_url || "");
  if (!url) return "—";
  try {
    const host = new URL(url).hostname;
    const first = host.split(".")[0] || host;
    return first.replace(/-\d{6,}$/, "");
  } catch {
    return url;
  }
}

function scenarioLabel(key) {
  const found = SCENARIOS.find((s) => s.key === key);
  return found ? found.label : key || "—";
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, Math.max(0, ms)));
}

function parsePns(text) {
  return String(text || "")
    .split(/[\s,;，、]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

/* =====================================================================
 * 执行图：静态拓扑
 *
 * 每条分支边都对应 backend/app/demo_pipeline.py 里的一段真实代码，
 * source 字段写的是那段代码的位置。lit() 只看真实 step 数据，
 * 拿不到证据就不点亮。
 * ===================================================================== */
function factOf(step, label) {
  const hit = (step?.facts || []).find((f) => f.label === label);
  return hit ? String(hit.value) : null;
}

function noteOf(step) {
  return String(step?.note || "");
}

const BRANCHES = [
  /* ---- 1 消息构造与字段解析 ---- */
  {
    id: "t_msg_fail",
    from: "message",
    side: "right",
    kind: "terminal",
    tone: "error",
    edge: "解析失败",
    title: "消息不满足契约",
    code: "message_parse_failed",
    source: "_step_message：MessageParseError 且场景非 blocked",
    detail:
      "cloud_gateway.message_parser.parse_pricing_event 抛 MessageParseError。这一跳判 error 并置 halt=message_parse_failed，第 2–7 跳全部标 skipped，不向 Cloud Run 入口发送任何请求。",
    lit: (m) => m.message.state === "error"
  },
  {
    id: "y_msg_probe",
    from: "message",
    side: "left",
    kind: "bypass",
    tone: "duplicate",
    rejoin: "ingress",
    edge: "blocked 场景",
    title: "本地判定不阻断",
    code: "原样发往入口取权威裁决",
    source: "_step_message：MessageParseError 且 scenario == blocked",
    detail:
      "blocked 场景故意构造缺 PN 行的消息。本地解析已判定不满足契约，但这一跳仍判 ok 并把原文原样发往入口——契约的权威判定点在 Cloud Run 入口，不在本地。第 2 跳返回的 422 才是真实拦截，这样评委看到的不是本地自己演的一出戏。",
    lit: (m) =>
      m.message.state === "ok" && /不满足契约/.test(factOf(m.message, "解析结果") || "")
  },

  /* ---- 2 Cloud Run 入口 ---- */
  {
    id: "t_ing_blocked",
    from: "ingress",
    side: "right",
    kind: "terminal",
    tone: "blocked",
    edge: "HTTP 422",
    title: "入口契约拦截",
    code: "blocked · 未触达外部系统",
    source: "_step_ingress：status == 422",
    detail:
      "入口在写入任何外部系统之前拒绝了这条消息：Workflows 没有被创建执行，工作簿没有被追加行。这一跳判 blocked 并置 halt=blocked，第 3–7 跳标 skipped。这是唯一一条「失败但系统行为正确」的终态。",
    lit: (m) => m.ingress.state === "blocked"
  },
  {
    id: "t_ing_unreachable",
    from: "ingress",
    side: "right",
    kind: "terminal",
    tone: "error",
    edge: "连接失败",
    title: "入口不可达",
    code: "ingress_unreachable",
    source: "_step_ingress：_http_post_json 抛 DemoHttpError",
    detail:
      "DNS / TCP / TLS / 超时任一失败都会抛 DemoHttpError（入口超时窗口 30 秒）。失败原因原文写进 facts，链路在第 2 跳终止。",
    lit: (m) => m.ingress.state === "error" && /不可达/.test(noteOf(m.ingress))
  },
  {
    id: "t_ing_error",
    from: "ingress",
    side: "left",
    kind: "terminal",
    tone: "error",
    edge: "非 200",
    title: "入口返回非预期状态",
    code: "ingress_error",
    source: "_step_ingress：status 既不是 200 也不是 422",
    detail:
      "入口返回了 200 / 422 之外的状态码（401、500、503…）。原始状态码与响应体照原样留痕，不做归类美化，链路终止。",
    lit: (m) =>
      m.ingress.state === "error" &&
      !/不可达/.test(noteOf(m.ingress)) &&
      !/DEMO_INGRESS_TOKEN/.test(noteOf(m.ingress))
  },
  {
    id: "t_ing_token",
    from: "ingress",
    side: "left",
    kind: "terminal",
    tone: "error",
    edge: "未配置令牌",
    title: "鉴权未配置",
    code: "ingress_token_missing",
    source: "_step_ingress：ingress_token() 为空",
    detail:
      "后端环境变量 DEMO_INGRESS_TOKEN 缺失。这一跳不发起任何真实网络调用就直接判 error——宁可承认没配置，也不发一个没有鉴权头的请求去撞 401。注入 token 后重试即可实时演示。",
    lit: (m) => m.ingress.state === "error" && /DEMO_INGRESS_TOKEN/.test(noteOf(m.ingress))
  },

  /* ---- 3 Google Workflows 编排 ---- */
  {
    id: "t_wf_failed",
    from: "workflow",
    side: "right",
    kind: "terminal",
    tone: "error",
    edge: "FAILED",
    title: "编排执行失败",
    code: "workflow_failed",
    source: "_step_workflow：轮询拿不到 result 且 state ∈ (FAILED, CANCELLED)",
    detail:
      "触发条件是执行终态为 FAILED 或 CANCELLED、且没有拿到执行返回值。置 halt=workflow_failed，第 4–7 跳标 skipped。注意 CANCELLED 也走这条边。",
    lit: (m) => m.workflow.state === "error"
  },
  {
    id: "y_wf_degrade",
    from: "workflow",
    side: "left",
    kind: "bypass",
    tone: "duplicate",
    rejoin: "sheet",
    edge: "无凭证/超时",
    title: "只确认执行已创建",
    code: "不拉取执行结果",
    source: "_step_workflow：三处 finish(ok) + note",
    detail:
      "三种触发合并成同一条降级通路，因为后果完全一致（这一跳仍判 ok，但拿不到执行返回值）：① 未配置 DEMO_GCP_ACCESS_TOKEN 只读凭证，只能确认执行已创建；② 拉取执行结果时网络异常（DemoHttpError），失败原因写进 note；③ 25 秒轮询窗口内执行仍是 ACTIVE。后果传导到第 4 跳：拿不到 updatedRange，第 4 跳会标 skipped 但不阻断链路。",
    lit: (m) =>
      m.workflow.state === "ok" &&
      /未配置 DEMO_GCP_ACCESS_TOKEN|拉取执行结果失败|轮询窗口内未拿到/.test(noteOf(m.workflow))
  },
  {
    id: "y_wf_direct",
    from: "workflow",
    side: "left",
    kind: "bypass",
    tone: "skipped",
    rejoin: "sheet",
    edge: "无执行名",
    title: "未经过 Workflows",
    code: "direct_sheet 模式",
    source: "_step_workflow：execution_name 为空 → _skip_rest",
    detail:
      "入口回退到直接写表模式时响应里没有 execution_name，这一跳整体标 skipped、不设 halt。第 4 跳改从入口响应里取 updated_range，链路照常往下走。如实标 skipped 而不是伪造一个执行 id。",
    lit: (m) =>
      m.workflow.state === "skipped" && /未经过 Workflows|direct_sheet/.test(noteOf(m.workflow))
  },

  /* ---- 4 Google Sheets 任务行 ---- */
  {
    id: "t_dup",
    from: "sheet",
    side: "right",
    kind: "terminal",
    tone: "duplicate",
    edge: "去重命中",
    title: "去重收束 · 未追加第二行",
    code: "第 5–7 跳不执行",
    source: "_step_workflow / _step_sheet：result.duplicate == true",
    extraFrom: { node: "workflow", edge: "duplicate" },
    detail:
      "相同 message_id 已存在。判定点有两个：① 第 3 跳编排返回值里的 duplicate=true（编排扫描到相同 [HUACHAT:message_id] 标记后直接返回，未再写表）；② 第 4 跳写表结果里的 duplicate（direct_sheet 模式下来自入口响应）。任一命中都会把 context.duplicate 置位，第 5/6/7 跳整体标 skipped：不建立第二个任务、不重复算价、不生成第二份制品。这是全场最能说明幂等设计的一段。",
    lit: (m) => m.workflow.state === "duplicate" || m.sheet.state === "duplicate"
  },
  {
    id: "y_sheet_unobserved",
    from: "sheet",
    side: "left",
    kind: "bypass",
    tone: "skipped",
    rejoin: "ingest",
    edge: "未观测",
    title: "跳过 updatedRange",
    code: "标 skipped 但链路继续",
    source: "_step_sheet：result is None，或 result 里没有 updated_range",
    detail:
      "两种触发：① 没有取到编排执行结果（多半是第 3 跳降级的后果）；② 执行结果里没有 updated_range 字段。这一跳标 skipped 但不设 halt——写表由 Workflows 异步完成，未观测不等于未发生，所以第 5 跳照常执行。这是一条「降级但不撒谎」的边：宁可承认没观测到，也不编一个 updatedRange。",
    lit: (m) =>
      m.sheet.state === "skipped" &&
      /updatedRange|没有取到编排执行结果|执行结果里没有/.test(noteOf(m.sheet))
  },

  /* ---- 5 内网摄取与任务建立 ---- */
  {
    id: "t_ingest_failed",
    from: "ingest",
    side: "right",
    kind: "terminal",
    tone: "error",
    edge: "建立失败",
    title: "任务建立失败",
    code: "ingest_failed",
    source: "_step_ingest_and_pricing：PricingWorkflowStore.create 抛异常",
    detail:
      "建任务失败时置 halt=ingest_failed，同一段代码里第 6 跳立刻标 skipped，第 7 跳随后 skipped。异常类型与消息原样进 facts。",
    lit: (m) => m.ingest.state === "error"
  },
  {
    id: "y_ingest_replay",
    from: "ingest",
    side: "left",
    kind: "bypass",
    tone: "duplicate",
    rejoin: "pricing",
    edge: "幂等命中",
    title: "复用既有任务",
    code: "idempotent_replay",
    source: "PricingWorkflowStore.create：幂等键状态文件已存在",
    detail:
      "幂等键是 demo:{message_id}。该键的任务状态文件已存在时，create() 直接返回既有任务并置 idempotent_replay=true，不重建任务、不重复算价、不产生第二份副作用（effect_key 不变）。这一跳仍判 ok，facts 里的「幂等重放」字段是这条边点亮的唯一依据。",
    lit: (m) => m.ingest.state === "ok" && factOf(m.ingest, "幂等重放") === "是"
  },

  /* ---- 6 确定性算价 ---- */
  {
    id: "t_pricing_failed",
    from: "pricing",
    side: "right",
    kind: "terminal",
    tone: "error",
    edge: "无结果行",
    title: "算价无结果",
    code: "pricing_failed",
    source: "_step_ingest_and_pricing：pricing_result.rows 为空 或 task.state == failed",
    detail:
      "判定条件是「没有结果行 或 任务状态是 failed」——引擎未加载价格数据、PN 全部未命中、算价过程抛异常都会落到这里，不只是「全部 PN 未命中」一种。置 halt=pricing_failed，第 7 跳标 skipped。",
    lit: (m) => m.pricing.state === "error"
  },
  {
    id: "y_manual_review",
    from: "pricing",
    side: "left",
    kind: "bypass",
    tone: "duplicate",
    rejoin: "gate",
    edge: "校验未过",
    title: "转人工复核队列",
    code: "manual_review",
    source: "PricingWorkflowStore：validation_errors → transition(manual_review)",
    detail:
      "校验规则有两条：算价行数与 PN 数不符、存在未命中 PN。任一成立即 validation.ok=false，任务状态机 transition 到 manual_review（event=validation_failed）。注意这一跳仍判 ok、制品照常生成——转人工复核不是失败，是把不确定的结果挡在自动提交之外。",
    lit: (m) => {
      const v = factOf(m.pricing, "校验结果");
      return m.pricing.state === "ok" && !!v && v !== "通过";
    }
  },

  /* ---- 7 GSP 模板制品 ---- */
  {
    id: "t_art_skip",
    from: "artifact",
    side: "right",
    kind: "terminal",
    tone: "skipped",
    edge: "无结果行",
    title: "跳过制品生成",
    code: "skipped",
    source: "_step_artifact：rows 为空",
    detail: "没有算价结果行就不生成 xlsx，这一跳标 skipped。不产出空模板冒充制品。",
    lit: (m) => m.artifact.state === "skipped" && /没有算价结果行/.test(noteOf(m.artifact))
  },
  {
    id: "t_art_error",
    from: "artifact",
    side: "right",
    kind: "terminal",
    tone: "error",
    edge: "导出异常",
    title: "制品导出失败",
    code: "error",
    source: "_step_artifact：build_export_frames / write_export_xlsx 抛异常",
    detail:
      "导出失败时这一跳判 error 但不设 halt（已经是最后一跳）。异常原文进 facts，本次运行的 final_state 会是 error。",
    lit: (m) => m.artifact.state === "error"
  }
];

const BRANCH_BY_ID = BRANCHES.reduce((acc, b) => {
  acc[b.id] = b;
  return acc;
}, {});

const GATE_DEF = {
  id: "gate",
  title: "人工复核边界",
  sub: "submission_authorized = false",
  source: "第 7 跳 facts + PricingWorkflowStore 状态机",
  detail:
    "这不是一个后端 step，而是第 7 跳事实与任务状态机的交汇点，画成终点是因为它确实是这条链路的终点。演示管线建任务时恒传 submission_authorized=False，于是 PricingWorkflowStore 在两种情况下都停在 manual_review：校验通过时走 submission_authorization_required，校验未过时走 validation_failed。两条路都不会自动向 GSP 提交——制品只落到内网目录，提交动作必须由人来授权。实测正常链路的第 6/7 跳 facts 里「任务状态」就是 manual_review。"
};

/* ---- 图外容错：不进主图，但要说明 ---- */
const OUT_OF_GRAPH = [
  {
    key: "transport",
    title: "传输层降级",
    body: "SSE 断开或静默 15 秒 → 自动切轮询兜底（GET /api/demo/runs/{run_id}），页面不会卡在半途。"
  },
  {
    key: "persist",
    title: "留痕失败",
    body: "快照落盘异常时后端置 persist_error，本次演示照常结束但不可回放，页面必须显式提示。"
  },
  {
    key: "guard",
    title: "管线级兜底",
    body: "任一跳抛未捕获异常，_execute 的 except 会把所有 pending/running 节点整体判 error 并写明异常类型，演示进程不会挂死。"
  }
];

/* ---------------------------------------------------------------------
 * 执行图：静态坐标布局（手写 SVG，无第三方图库）
 * viewBox 固定，SVG 按容器宽度等比缩放，因此屏幕越大字越大，
 * 且永远不会横向溢出。
 * ------------------------------------------------------------------- */
const GRAPH_ROWS = [
  { key: "message", right: ["t_msg_fail"], left: ["y_msg_probe"] },
  { key: "ingress", right: ["t_ing_blocked", "t_ing_unreachable"], left: ["t_ing_error", "t_ing_token"] },
  { key: "workflow", right: ["t_wf_failed"], left: ["y_wf_degrade", "y_wf_direct"] },
  { key: "sheet", right: ["t_dup"], left: ["y_sheet_unobserved"] },
  { key: "ingest", right: ["t_ingest_failed"], left: ["y_ingest_replay"] },
  { key: "pricing", right: ["t_pricing_failed"], left: ["y_manual_review"] },
  { key: "artifact", right: ["t_art_skip", "t_art_error"], left: [] },
  { key: "gate", right: [], left: [] }
];

const LAYOUT = (() => {
  const W = 700;
  const NX = 236;
  const NW = 200;
  const NH = 44;
  const CX = NX + NW / 2; /* 336 */
  const ROW_GAP = 22;
  const R_BUS = 456;
  const R_CHIP_X = 520;
  const R_CHIP_W = 178;
  const L_BUS = 212;
  const L_CHIP_X = 2;
  const L_CHIP_W = 146;
  const REJOIN_BUS = 224;
  const CH = 26;
  const CGAP = 8;
  const PAD_T = 18;
  const PAD_B = 16;
  const GATE_W = 240;
  const GATE_X = CX - GATE_W / 2; /* 216 */
  const GATE_H = 50;

  const nodes = {};
  const chips = {};
  let y = PAD_T;

  GRAPH_ROWS.forEach((row) => {
    const isGate = row.key === "gate";
    const nh = isGate ? GATE_H : NH;
    const nw = isGate ? GATE_W : NW;
    const nx = isGate ? GATE_X : NX;
    const maxSide = Math.max(row.right.length, row.left.length);
    const chipsH = maxSide ? maxSide * CH + (maxSide - 1) * CGAP + 16 : 0;
    const rowH = Math.max(nh + ROW_GAP, chipsH);
    const cy = y + rowH / 2;
    nodes[row.key] = { key: row.key, x: nx, y: cy - nh / 2, w: nw, h: nh, cx: nx + nw / 2, cy, gate: isGate };
    ["right", "left"].forEach((side) => {
      const ids = row[side];
      const total = ids.length * CH + Math.max(0, ids.length - 1) * CGAP;
      ids.forEach((id, i) => {
        const top = cy - total / 2 + i * (CH + CGAP);
        chips[id] = {
          id,
          side,
          from: row.key,
          order: i,
          x: side === "right" ? R_CHIP_X : L_CHIP_X,
          w: side === "right" ? R_CHIP_W : L_CHIP_W,
          y: top,
          h: CH,
          cy: top + CH / 2
        };
      });
    });
    y += rowH;
  });

  const H = y + PAD_B;

  /* --- 主干边 --- */
  const trunk = [];
  for (let i = 0; i < GRAPH_ROWS.length - 1; i += 1) {
    const a = nodes[GRAPH_ROWS[i].key];
    const b = nodes[GRAPH_ROWS[i + 1].key];
    trunk.push({
      id: `trunk-${a.key}-${b.key}`,
      from: a.key,
      to: b.key,
      d: `M ${CX} ${a.y + a.h} L ${CX} ${b.y}`
    });
  }

  /* --- 分支边（先出后拐的正交折线） --- */
  const branchEdges = {};
  BRANCHES.forEach((b) => {
    const c = chips[b.id];
    const n = nodes[b.from];
    if (!c || !n) return;
    let d;
    let head;
    if (c.side === "right") {
      const yOff = b.extraFrom ? 0 : 0;
      d =
        Math.abs(n.cy - c.cy) < 0.6
          ? `M ${n.x + n.w} ${n.cy} L ${c.x} ${c.cy}`
          : `M ${n.x + n.w} ${n.cy + yOff} L ${R_BUS} ${n.cy + yOff} L ${R_BUS} ${c.cy} L ${c.x} ${c.cy}`;
      head = { x: c.x, y: c.cy, dir: 1 };
    } else {
      d =
        Math.abs(n.cy - c.cy) < 0.6
          ? `M ${n.x} ${n.cy} L ${c.x + c.w} ${c.cy}`
          : `M ${n.x} ${n.cy} L ${L_BUS} ${n.cy} L ${L_BUS} ${c.cy - 5} L ${c.x + c.w} ${c.cy - 5}`;
      head = { x: c.x + c.w, y: Math.abs(n.cy - c.cy) < 0.6 ? c.cy : c.cy - 5, dir: -1 };
    }
    const label =
      c.side === "right"
        ? { x: (R_BUS + c.x) / 2, y: c.cy - 5, anchor: "middle" }
        : { x: (c.x + c.w + L_BUS) / 2, y: (Math.abs(n.cy - c.cy) < 0.6 ? c.cy : c.cy - 5) - 5, anchor: "middle" };
    branchEdges[b.id] = { d, head, label };
  });

  /* --- 汇入边：第 3 跳的 duplicate 判定也收到同一个终态 --- */
  const extraEdges = [];
  BRANCHES.forEach((b) => {
    if (!b.extraFrom) return;
    const c = chips[b.id];
    const n = nodes[b.extraFrom.node];
    if (!c || !n) return;
    extraEdges.push({
      id: `${b.id}-from-${b.extraFrom.node}`,
      branchId: b.id,
      from: b.extraFrom.node,
      label: b.extraFrom.edge,
      labelPos: { x: n.x + n.w + 12, y: n.cy + 2, anchor: "start" },
      d: `M ${n.x + n.w} ${n.cy + 8} L ${R_BUS + 12} ${n.cy + 8} L ${R_BUS + 12} ${c.cy} L ${c.x} ${c.cy}`,
      head: { x: c.x, y: c.cy, dir: 1 }
    });
  });

  /* --- 旁路回主干 --- */
  const rejoinEdges = [];
  BRANCHES.forEach((b) => {
    if (!b.rejoin) return;
    const c = chips[b.id];
    const t = nodes[b.rejoin];
    if (!c || !t) return;
    const yOff = (c.order || 0) * 10 - 4;
    rejoinEdges.push({
      id: `${b.id}-rejoin`,
      branchId: b.id,
      d: `M ${c.x + c.w} ${c.cy + 6} L ${REJOIN_BUS} ${c.cy + 6} L ${REJOIN_BUS} ${t.cy + yOff} L ${t.x} ${t.cy + yOff}`,
      head: { x: t.x, y: t.cy + yOff, dir: 1 }
    });
  });

  return {
    W,
    H,
    CX,
    NX,
    NW,
    NH,
    nodes,
    chips,
    trunk,
    branchEdges,
    extraEdges,
    rejoinEdges
  };
})();

/* 节点是否被真实走到：skipped 分两种——链路终止后的 skipped（未走到），
   和旁路型 skipped（走到了但这一跳没做事，链路继续往下）。
   判据是后面还有没有节点真的被执行过。 */
function computeReached(steps) {
  const reached = steps.map((s) => LIVE_STATES.has(s.state));
  for (let i = steps.length - 1; i >= 0; i -= 1) {
    if (steps[i].state === "skipped") {
      reached[i] = steps.slice(i + 1).some((s) => LIVE_STATES.has(s.state));
    }
  }
  return reached;
}

function buildGraphModel(steps) {
  const byKey = {};
  steps.forEach((s) => {
    byKey[s.key] = s;
  });
  STEP_BLUEPRINT.forEach((bp) => {
    if (!byKey[bp.key]) byKey[bp.key] = { key: bp.key, state: "pending", facts: [], note: "" };
  });

  const ordered = STEP_BLUEPRINT.map((bp) => byKey[bp.key]);
  const reachedArr = computeReached(ordered);
  const reached = {};
  STEP_BLUEPRINT.forEach((bp, i) => {
    reached[bp.key] = reachedArr[i];
  });

  const gateLit = byKey.pricing.state === "ok" || byKey.artifact.state === "ok";
  reached.gate = gateLit;

  const litBranch = {};
  BRANCHES.forEach((b) => {
    let on = false;
    try {
      on = !!b.lit(byKey);
    } catch {
      on = false;
    }
    litBranch[b.id] = on;
  });

  /* 主干边：两端都被真实走到才算走过 */
  const litTrunk = {};
  LAYOUT.trunk.forEach((e) => {
    if (e.to === "gate") {
      litTrunk[e.id] =
        byKey.artifact.state === "ok" || (byKey.artifact.state === "skipped" && byKey.pricing.state === "ok");
    } else {
      litTrunk[e.id] = !!(reached[e.from] && reached[e.to]);
    }
  });

  return { byKey, reached, litBranch, litTrunk, gateLit };
}

/* =====================================================================
 * 开发期 mock（USE_MOCK=true 才会被使用）
 * ===================================================================== */
function mockConfig() {
  return {
    live_available: true,
    ingress_configured: true,
    cloud_run_url: "https://pricing-huachat-ingress-339841524852.europe-west1.run.app",
    cloud_run_health: { ok: true, dispatch_mode: "workflow", sheet_name: "2026.07" },
    workflow_console_url:
      "https://console.cloud.google.com/workflows/workflow/europe-west1/pricing-request-sandbox?project=gen-lang-client-0394580582",
    project_id: "gen-lang-client-0394580582",
    region: "europe-west1",
    spreadsheet_id: "1g96wjgMSGMhXrzLGwCAIQic_mYkS8xsThxJryfFHJ2A",
    sheet_name: "2026.07",
    data_version: "v-MOCK",
    last_message_id: "mock-message-id",
    replay_count: 3
  };
}

function mockRunSnapshot(scenario) {
  const t0 = Date.now() / 1000;
  const mk = (key, off, dur, state, extra) => ({
    key,
    index: BLUEPRINT_BY_KEY[key].index,
    title: BLUEPRINT_BY_KEY[key].title,
    subtitle: BLUEPRINT_BY_KEY[key].subtitle,
    layer: BLUEPRINT_BY_KEY[key].layer,
    state,
    started_at: t0 + off,
    ended_at: t0 + off + dur,
    duration_ms: Math.round(dur * 1000),
    facts: [{ label: "MOCK", value: "本地假数据" }],
    ...extra
  });
  const skip = (key, note) => mk(key, 0, 0, "skipped", { note, facts: [] });
  let steps;
  if (scenario === "blocked") {
    steps = [
      mk("message", 0, 0.03, "ok", { facts: [{ label: "解析结果", value: "不满足契约（MOCK）" }] }),
      mk("ingress", 0.05, 0.42, "blocked", { note: "入口在写入任何外部系统之前拒绝了这条消息" }),
      skip("workflow", "入口拦截，未触达外部系统"),
      skip("sheet", "入口拦截，未触达外部系统"),
      skip("ingest", "入口拦截，未触达外部系统"),
      skip("pricing", "入口拦截，未触达外部系统"),
      skip("artifact", "入口拦截，未触达外部系统")
    ];
  } else if (scenario === "duplicate") {
    steps = [
      mk("message", 0, 0.03, "ok"),
      mk("ingress", 0.05, 1.2, "ok"),
      mk("workflow", 1.3, 2.4, "duplicate", { note: "编排扫描到相同标记后直接返回" }),
      mk("sheet", 3.8, 0.4, "duplicate", { note: "相同 message_id 已存在，工作簿未追加第二行" }),
      skip("ingest", "去重命中，链路提前收束，未建立第二个任务"),
      skip("pricing", "去重命中，链路提前收束"),
      skip("artifact", "去重命中，链路提前收束，未生成制品")
    ];
  } else {
    steps = [
      mk("message", 0, 0.03, "ok"),
      mk("ingress", 0.05, 1.21, "ok"),
      mk("workflow", 1.3, 2.42, "ok"),
      mk("sheet", 3.75, 0.02, "ok"),
      mk("ingest", 3.8, 0.03, "ok", { facts: [{ label: "幂等重放", value: "否" }] }),
      mk("pricing", 3.85, 0.39, "ok", { facts: [{ label: "校验结果", value: "通过" }] }),
      mk("artifact", 4.25, 0.04, "ok")
    ];
  }
  const finalState = scenario === "blocked" ? "blocked" : scenario === "duplicate" ? "duplicate" : "ok";
  return {
    run_id: `mock-${scenario}`,
    scenario,
    message_id: "mock-message-id",
    started_at: t0,
    total_ms: steps.reduce((a, s) => a + (Number(s.duration_ms) || 0), 0),
    final_state: finalState,
    source: "mock",
    steps
  };
}

/* =====================================================================
 * SVG 图元
 * ===================================================================== */
function Glyph({ kind, cx, cy, r, color }) {
  const s = r * 0.56;
  const ring = <circle cx={cx} cy={cy} r={r} fill="none" stroke={color} strokeWidth="1.3" />;
  if (kind === "ok") {
    return (
      <g>
        {ring}
        <path
          d={`M ${cx - s} ${cy + 0.1} L ${cx - s * 0.2} ${cy + s * 0.75} L ${cx + s} ${cy - s * 0.75}`}
          fill="none"
          stroke={color}
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </g>
    );
  }
  if (kind === "blocked") {
    return (
      <g>
        {ring}
        <path
          d={`M ${cx - s} ${cy - s} L ${cx + s} ${cy + s} M ${cx + s} ${cy - s} L ${cx - s} ${cy + s}`}
          fill="none"
          stroke={color}
          strokeWidth="1.5"
          strokeLinecap="round"
        />
      </g>
    );
  }
  if (kind === "error") {
    return (
      <g>
        {ring}
        <path
          d={`M ${cx} ${cy - s * 1.05} L ${cx} ${cy + s * 0.15}`}
          fill="none"
          stroke={color}
          strokeWidth="1.6"
          strokeLinecap="round"
        />
        <circle cx={cx} cy={cy + s * 0.85} r={r * 0.13} fill={color} />
      </g>
    );
  }
  if (kind === "duplicate") {
    return (
      <g>
        {ring}
        <circle cx={cx} cy={cy} r={r * 0.45} fill="none" stroke={color} strokeWidth="1.3" />
      </g>
    );
  }
  if (kind === "skipped") {
    return (
      <g>
        <circle cx={cx} cy={cy} r={r} fill="none" stroke={color} strokeWidth="1.1" strokeDasharray="2.4 2" />
        <path
          d={`M ${cx - s * 0.95} ${cy + s * 0.95} L ${cx + s * 0.95} ${cy - s * 0.95}`}
          fill="none"
          stroke={color}
          strokeWidth="1.4"
          strokeLinecap="round"
        />
      </g>
    );
  }
  if (kind === "running") {
    return (
      <g>
        {ring}
        <circle className="demoGPulse" cx={cx} cy={cy} r={r * 0.42} fill={color} />
      </g>
    );
  }
  if (kind === "bypass") {
    return (
      <g>
        <circle cx={cx} cy={cy} r={r} fill="none" stroke={color} strokeWidth="1.1" strokeDasharray="2.4 2" />
        <path
          d={`M ${cx - s * 0.7} ${cy - s * 0.8} L ${cx + s * 0.5} ${cy} L ${cx - s * 0.7} ${cy + s * 0.8}`}
          fill="none"
          stroke={color}
          strokeWidth="1.4"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </g>
    );
  }
  if (kind === "gate") {
    return (
      <g>
        <circle cx={cx} cy={cy} r={r} fill="none" stroke={color} strokeWidth="1.6" />
        <path
          d={`M ${cx - s * 1.05} ${cy} L ${cx + s * 1.05} ${cy}`}
          fill="none"
          stroke={color}
          strokeWidth="1.7"
          strokeLinecap="round"
        />
      </g>
    );
  }
  /* pending */
  return <circle cx={cx} cy={cy} r={r} fill="none" stroke={color} strokeWidth="1.1" strokeDasharray="2.4 2" />;
}

function ArrowHead({ x, y, dir, color }) {
  const w = 4.6;
  const h = 3.1;
  const tipX = x + (dir > 0 ? 0 : 0);
  const backX = tipX - dir * w;
  return (
    <path
      d={`M ${tipX} ${y} L ${backX} ${y - h} L ${backX} ${y + h} Z`}
      fill={color}
      stroke="none"
    />
  );
}

/* =====================================================================
 * 执行图
 * ===================================================================== */
function ExecutionGraph({ steps, model, selected, onSelect, region }) {
  const { byKey, reached, litBranch, litTrunk, gateLit } = model;

  const nodeTone = (key) => {
    const st = byKey[key];
    if (!st) return "pending";
    if (st.state === "skipped") return reached[key] ? "skipped" : "pending";
    return st.state;
  };

  const glyphFor = (state) => {
    if (state === "ok") return "ok";
    if (state === "running") return "running";
    if (state === "duplicate") return "duplicate";
    if (state === "blocked") return "blocked";
    if (state === "error") return "error";
    if (state === "skipped") return "skipped";
    return "pending";
  };

  const isSel = (type, id) => selected && selected.type === type && selected.id === id;

  return (
    <svg
      className="demoGSvg"
      viewBox={`0 0 ${LAYOUT.W} ${LAYOUT.H}`}
      preserveAspectRatio="xMidYMid meet"
      role="img"
      aria-label="端到端演示执行图：主干七跳与各判定点的失败、降级、人工兜底分支"
    >
      <title>端到端演示执行图</title>

      {/* ---- 主干边（竖直向下，末端箭头） ---- */}
      {LAYOUT.trunk.map((e) => {
        const on = litTrunk[e.id];
        const color = on ? PALETTE.ok : PALETTE.idle;
        const ty = LAYOUT.nodes[e.to].y;
        return (
          <g key={e.id}>
            <path
              d={e.d}
              fill="none"
              stroke={color}
              strokeWidth={on ? 1.8 : 1.1}
              strokeDasharray={on ? undefined : "4 3"}
            />
            <path
              d={`M ${LAYOUT.CX} ${ty} L ${LAYOUT.CX - 3.1} ${ty - 4.6} L ${LAYOUT.CX + 3.1} ${ty - 4.6} Z`}
              fill={color}
            />
          </g>
        );
      })}

      {/* ---- 旁路回主干（画在节点下方，先画边后画节点） ---- */}
      {LAYOUT.rejoinEdges.map((e) => {
        const b = BRANCH_BY_ID[e.branchId];
        const on = litBranch[e.branchId];
        const color = on ? PALETTE[b.tone] || PALETTE.duplicate : PALETTE.idle;
        return (
          <g key={e.id}>
            <path d={e.d} fill="none" stroke={color} strokeWidth={on ? 1.5 : 1} strokeDasharray="4 3" />
            <ArrowHead x={e.head.x} y={e.head.y} dir={e.head.dir} color={color} />
          </g>
        );
      })}

      {/* ---- 汇入边 ---- */}
      {LAYOUT.extraEdges.map((e) => {
        const on = litBranch[e.branchId] && byKey[e.from]?.state === "duplicate";
        const color = on ? PALETTE.duplicate : PALETTE.idle;
        return (
          <g key={e.id}>
            <path d={e.d} fill="none" stroke={color} strokeWidth={on ? 1.6 : 1} strokeDasharray={on ? undefined : "4 3"} />
            <ArrowHead x={e.head.x} y={e.head.y} dir={e.head.dir} color={color} />
            <text
              className="demoGEdgeLabel"
              x={e.labelPos.x}
              y={e.labelPos.y}
              textAnchor={e.labelPos.anchor}
              fill={on ? PALETTE.duplicate : PALETTE.muted}
            >
              {e.label}
            </text>
          </g>
        );
      })}

      {/* ---- 分支边 ---- */}
      {BRANCHES.map((b) => {
        const e = LAYOUT.branchEdges[b.id];
        if (!e) return null;
        const on = litBranch[b.id];
        const color = on ? PALETTE[b.tone] || PALETTE.error : PALETTE.idle;
        return (
          <g key={`${b.id}-edge`}>
            <path
              d={e.d}
              fill="none"
              stroke={color}
              strokeWidth={on ? 1.6 : 1}
              strokeDasharray={on ? (b.kind === "bypass" ? "4 3" : undefined) : "4 3"}
            />
            <ArrowHead x={e.head.x} y={e.head.y} dir={e.head.dir} color={color} />
            <text
              className="demoGEdgeLabel"
              x={e.label.x}
              y={e.label.y}
              textAnchor={e.label.anchor}
              fill={on ? color : PALETTE.muted}
            >
              {b.edge}
            </text>
          </g>
        );
      })}

      {/* ---- 分支节点 ---- */}
      {BRANCHES.map((b) => {
        const c = LAYOUT.chips[b.id];
        if (!c) return null;
        const on = litBranch[b.id];
        const color = on ? PALETTE[b.tone] || PALETTE.error : PALETTE.idle;
        const sel = isSel("branch", b.id);
        const textX = c.x + 22;
        return (
          <g
            key={b.id}
            className={`demoGChip${on ? " on" : ""}${sel ? " sel" : ""}`}
            onClick={() => onSelect({ type: "branch", id: b.id })}
            role="button"
            tabIndex={0}
            onKeyDown={(ev) => {
              if (ev.key === "Enter" || ev.key === " ") {
                ev.preventDefault();
                onSelect({ type: "branch", id: b.id });
              }
            }}
            aria-label={`${b.kind === "terminal" ? "终态分支" : "旁路分支"} ${b.title}${on ? "（本次命中）" : "（本次未走到）"}`}
          >
            <rect
              x={c.x}
              y={c.y}
              width={c.w}
              height={c.h}
              rx="3"
              fill={on ? "#ffffff" : PALETTE.surface}
              stroke={sel ? PALETTE.ink : color}
              strokeWidth={sel ? 1.6 : on ? 1.2 : 0.9}
              strokeDasharray={on ? undefined : "3 2.4"}
            />
            <Glyph
              kind={b.kind === "terminal" ? glyphChipKind(b.tone) : "bypass"}
              cx={c.x + 12}
              cy={c.cy}
              r={6}
              color={color}
            />
            <text className="demoGChipTitle" x={textX} y={c.y + 11.5} fill={on ? PALETTE.ink : PALETTE.muted}>
              {b.title}
            </text>
            <text className="demoGChipCode" x={textX} y={c.y + 21.5} fill={PALETTE.muted}>
              {b.code}
            </text>
          </g>
        );
      })}

      {/* ---- 主干节点 ---- */}
      {STEP_BLUEPRINT.map((bp) => {
        const n = LAYOUT.nodes[bp.key];
        const st = byKey[bp.key];
        const tone = nodeTone(bp.key);
        const color = PALETTE[tone] || PALETTE.pending;
        const sel = isSel("step", bp.key);
        const dim = tone === "pending";
        return (
          <g
            key={bp.key}
            className={`demoGNode${sel ? " sel" : ""}`}
            onClick={() => onSelect({ type: "step", id: bp.key })}
            role="button"
            tabIndex={0}
            onKeyDown={(ev) => {
              if (ev.key === "Enter" || ev.key === " ") {
                ev.preventDefault();
                onSelect({ type: "step", id: bp.key });
              }
            }}
            aria-label={`第 ${bp.index} 跳 ${bp.title}，状态 ${(STATE_META[st.state] || STATE_META.pending).label}`}
          >
            <rect
              x={n.x}
              y={n.y}
              width={n.w}
              height={n.h}
              rx="3"
              fill={sel ? "#f2efe7" : "#ffffff"}
              stroke={sel ? PALETTE.ink : dim ? PALETTE.line : color}
              strokeWidth={sel ? 1.8 : dim ? 1 : 1.3}
              strokeDasharray={tone === "skipped" ? "4 3" : undefined}
            />
            <rect x={n.x} y={n.y} width={3} height={n.h} fill={dim ? PALETTE.line : color} />
            <text className="demoGIdx" x={n.x + 13} y={n.cy + 3.5} textAnchor="middle" fill={PALETTE.muted}>
              {bp.index}
            </text>
            <Glyph kind={glyphFor(st.state)} cx={n.x + 30} cy={n.cy} r={7.5} color={dim ? PALETTE.pending : color} />
            <text className="demoGNodeTitle" x={n.x + 44} y={n.y + 19} fill={dim ? PALETTE.muted : PALETTE.ink}>
              {bp.graphTitle}
            </text>
            <text className="demoGNodeSub" x={n.x + 44} y={n.y + 32.5} fill={PALETTE.muted}>
              {bp.graphSub}
            </text>
            <text className="demoGTag" x={n.x + n.w - 9} y={n.y + 15} textAnchor="end" fill={PALETTE.muted}>
              {bp.layer === "cloud" ? "GCP" : "内网"}
            </text>
            <text className="demoGDur" x={n.x + n.w - 9} y={n.y + 33} textAnchor="end" fill={dim ? PALETTE.pending : PALETTE.ink}>
              {st.durationMs === null || st.durationMs === undefined ? "—" : fmtMs(st.durationMs)}
            </text>
          </g>
        );
      })}

      {/* ---- 终点：人工复核门 ---- */}
      {(() => {
        const n = LAYOUT.nodes.gate;
        const sel = isSel("gate", "gate");
        const color = gateLit ? PALETTE.ok : PALETTE.idle;
        return (
          <g
            className={`demoGNode demoGGate${sel ? " sel" : ""}`}
            onClick={() => onSelect({ type: "gate", id: "gate" })}
            role="button"
            tabIndex={0}
            onKeyDown={(ev) => {
              if (ev.key === "Enter" || ev.key === " ") {
                ev.preventDefault();
                onSelect({ type: "gate", id: "gate" });
              }
            }}
            aria-label="终点：人工复核边界，submission_authorized 恒为 false"
          >
            <rect
              x={n.x}
              y={n.y}
              width={n.w}
              height={n.h}
              rx="3"
              fill={sel ? "#f2efe7" : "#ffffff"}
              stroke={sel ? PALETTE.ink : color}
              strokeWidth={sel ? 2 : gateLit ? 1.9 : 1.2}
              strokeDasharray={gateLit ? undefined : "4 3"}
            />
            <rect x={n.x} y={n.y} width={3} height={n.h} fill={color} />
            <text className="demoGIdx" x={n.x + 14} y={n.cy + 3.5} textAnchor="middle" fill={PALETTE.muted}>
              8
            </text>
            <Glyph kind="gate" cx={n.x + 32} cy={n.cy} r={8} color={color} />
            <text className="demoGNodeTitle" x={n.x + 47} y={n.y + 22} fill={PALETTE.ink}>
              {GATE_DEF.title}
            </text>
            <text className="demoGGateSub" x={n.x + 47} y={n.y + 36} fill={PALETTE.muted}>
              {GATE_DEF.sub}
            </text>
          </g>
        );
      })()}

      {/* ---- 云/内网分带说明 ---- */}
      <text className="demoGBandTag" x={2} y={11} fill={PALETTE.muted}>
        {`主干 8 节点 · 分支 ${BRANCHES.length} 条${region ? ` · ${region}` : ""}`}
      </text>
    </svg>
  );
}

function glyphChipKind(tone) {
  if (tone === "duplicate") return "duplicate";
  if (tone === "skipped") return "skipped";
  if (tone === "blocked") return "blocked";
  return "blocked";
}

/* =====================================================================
 * 右栏
 * ===================================================================== */
function FactsTable({ facts, placeholder }) {
  if (!facts.length) return null;
  return (
    <div className="tableWrap demoFactsWrap">
      <table className="table dense">
        <thead>
          <tr>
            <th style={{ width: 118 }}>字段</th>
            <th>{placeholder ? "占位值（非实测）" : "真实值"}</th>
          </tr>
        </thead>
        <tbody>
          {facts.map((f, i) => (
            <tr key={`${f.label}-${i}`}>
              <td className="mono">{f.label}</td>
              <td className="mono demoFactVal">{f.value}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function StepDetail({ step, runId, mode, placeholder }) {
  const meta = STATE_META[step.state] || STATE_META.pending;
  const showArtifactLink =
    step.key === "artifact" && step.state === "ok" && runId && mode !== "mock" && !placeholder;
  return (
    <div className="demoSide">
      <div className="demoSideHead">
        <div className="demoSideTitle">
          <span className="demoSideIdx mono">{step.index}</span>
          {step.title}
        </div>
        <div className="demoChipRow">
          <span className={`demoStateChip st-${meta.cls}`}>{meta.label}</span>
          <span className="pill">{step.layer === "cloud" ? "Google Cloud" : "内网 r230"}</span>
          <span className="pill mono">{step.durationMs === null ? "—" : fmtMs(step.durationMs)}</span>
        </div>
      </div>
      <div className="demoSideMeta mono">
        {step.subtitle ? <span>{step.subtitle}</span> : null}
        <span>起 {fmtClock(step.startedAt)}</span>
      </div>

      {step.note ? <div className="demoNote">{step.note}</div> : null}

      {step.facts.length ? (
        <FactsTable facts={step.facts} placeholder={placeholder} />
      ) : (
        <div className="small demoSideEmpty">
          {step.state === "pending" ? "该节点尚未执行。" : "该节点未返回字段明细。"}
        </div>
      )}

      <div className="demoPayloads">
        <details>
          <summary className="mono">{placeholder ? "request（占位报文）" : "request（真实请求）"}</summary>
          <pre className="codebox">{step.request === null ? "（无）" : pretty(step.request)}</pre>
        </details>
        <details>
          <summary className="mono">{placeholder ? "response（占位报文）" : "response（真实响应）"}</summary>
          <pre className="codebox">{step.response === null ? "（无）" : pretty(step.response)}</pre>
        </details>
      </div>

      {step.links.length || showArtifactLink ? (
        <div className="demoChipRow demoSideLinks">
          {step.links.map((l) => (
            <a key={l.url} className="btn" href={l.url} target="_blank" rel="noreferrer">
              {l.label}
            </a>
          ))}
          {showArtifactLink ? (
            <a
              className="btn secondary"
              href={`/api/demo/runs/${encodeURIComponent(runId)}/artifact`}
              target="_blank"
              rel="noreferrer"
            >
              下载本次 xlsx
            </a>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function BranchDetail({ branch, lit }) {
  const fromBp = BLUEPRINT_BY_KEY[branch.from];
  const rejoinBp = branch.rejoin === "gate" ? null : BLUEPRINT_BY_KEY[branch.rejoin];
  return (
    <div className="demoSide">
      <div className="demoSideHead">
        <div className="demoSideTitle">
          <span className={`demoSideIdx mono br-${branch.kind}`}>{branch.kind === "terminal" ? "✕" : "⇢"}</span>
          {branch.title}
        </div>
        <div className="demoChipRow">
          <span className={`demoStateChip ${lit ? `st-${branch.tone}` : "st-idle"}`}>
            {lit ? "本次命中" : "本次未走到"}
          </span>
          <span className="pill">{branch.kind === "terminal" ? "终态 · 链路在此终止" : "旁路 · 回到主干继续"}</span>
        </div>
      </div>
      <div className="demoSideMeta mono">
        <span>
          出自 {fromBp ? `第 ${fromBp.index} 跳 ${fromBp.title}` : branch.from}
        </span>
        <span>条件 {branch.edge}</span>
      </div>

      <div className="tableWrap demoFactsWrap">
        <table className="table dense">
          <tbody>
            <tr>
              <td className="mono" style={{ width: 92 }}>结果标识</td>
              <td className="mono demoFactVal">{branch.code}</td>
            </tr>
            <tr>
              <td className="mono">代码位置</td>
              <td className="mono demoFactVal">{branch.source}</td>
            </tr>
            <tr>
              <td className="mono">去向</td>
              <td className="mono demoFactVal">
                {branch.kind === "terminal"
                  ? "终态，后续节点不执行"
                  : branch.rejoin === "gate"
                  ? "汇入第 8 节点 人工复核边界"
                  : `回到主干第 ${rejoinBp ? rejoinBp.index : "?"} 跳${rejoinBp ? ` ${rejoinBp.title}` : ""}`}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div className="demoNote">{branch.detail}</div>

      {!lit ? (
        <div className="small demoSideEmpty">
          本次执行没有走到这条分支。它保持灰态可见，是因为它是系统设计的一部分——不是没画，是这次没触发。
        </div>
      ) : null}
    </div>
  );
}

function GateDetail({ lit, gateStep }) {
  const facts = (gateStep?.facts || []).filter((f) =>
    /submission_authorized|人工复核边界|任务状态/.test(f.label)
  );
  return (
    <div className="demoSide">
      <div className="demoSideHead">
        <div className="demoSideTitle">
          <span className="demoSideIdx mono">8</span>
          {GATE_DEF.title}
        </div>
        <div className="demoChipRow">
          <span className={`demoStateChip ${lit ? "st-ok" : "st-idle"}`}>{lit ? "本次到达" : "本次未到达"}</span>
          <span className="pill">终点 · 不是自动提交</span>
        </div>
      </div>
      <div className="demoSideMeta mono">
        <span>{GATE_DEF.sub}</span>
        <span>{GATE_DEF.source}</span>
      </div>
      <div className="demoNote">{GATE_DEF.detail}</div>
      {facts.length ? <FactsTable facts={facts} placeholder={false} /> : null}
    </div>
  );
}

function RunSummary({ run, cfg, steps, transport, mode }) {
  const settled = safeStr(run?.finalState) !== "" && safeStr(run?.finalState) !== "running";
  const finalMeta = settled ? STATE_META[normState(run.finalState)] : null;
  const timed = steps
    .filter((s) => s.durationMs !== null && s.durationMs > 0)
    .slice()
    .sort((a, b) => b.durationMs - a.durationMs);
  const env = run?.environment || {};
  const rows = [
    ["场景", run?.scenario ? scenarioLabel(run.scenario) : "—"],
    ["run_id", run?.run_id || "—"],
    ["message_id", run?.messageId || safeStr(cfg?.last_message_id) || "—"],
    [settled ? "终态" : "状态", finalMeta ? finalMeta.label : run ? "执行中" : "未开始"],
    ["总耗时", run?.totalMs === null || run?.totalMs === undefined ? "—" : fmtMs(run.totalMs)],
    ["开始时刻", fmtClock(run?.startedAt)],
    ["快照来源", run?.source === "fixture" ? "仓库内置快照" : run?.source === "mock" ? "本地 MOCK" : run?.source || "—"],
    ["GCP 项目", safeStr(env.project_id) || safeStr(cfg?.project_id) || "—"],
    ["区域", safeStr(env.region) || safeStr(cfg?.region) || "—"],
    ["Cloud Run", cfg ? deriveServiceName(cfg) : "—"],
    ["工作簿标签页", safeStr(env.sheet_name) || safeStr(cfg?.sheet_name) || "—"],
    ["数据版本", safeStr(env.data_version) || safeStr(cfg?.data_version) || "—"]
  ];
  return (
    <div className="demoSide">
      <div className="demoSideHead">
        <div className="demoSideTitle">本次执行摘要</div>
        <div className="demoChipRow">
          {finalMeta ? <span className={`demoStateChip st-${finalMeta.cls}`}>{finalMeta.label}</span> : null}
          {mode === "replay" ? <span className="demoStateChip demoReplayChip">回放</span> : null}
        </div>
      </div>
      <div className="demoSideMeta mono">
        <span>点选图上任一节点或分支查看细节</span>
      </div>

      <div className="tableWrap demoFactsWrap">
        <table className="table dense">
          <tbody>
            {rows.map((r) => (
              <tr key={r[0]}>
                <td className="mono" style={{ width: 104 }}>{r[0]}</td>
                <td className="mono demoFactVal">{r[1]}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {run?.persistError ? (
        <div className="demoNote demoNoteWarn">
          快照落盘失败（persist_error：{run.persistError}）。本次演示照常结束，但这一次不可回放。
        </div>
      ) : null}

      {timed.length ? (
        <>
          <div className="demoSideSub mono">各跳耗时排序</div>
          <div className="tableWrap demoFactsWrap">
            <table className="table dense">
              <tbody>
                {timed.map((s) => (
                  <tr key={s.key}>
                    <td className="mono" style={{ width: 104 }}>
                      {s.index}. {s.key}
                    </td>
                    <td className="mono demoFactVal">{fmtMs(s.durationMs)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : (
        <div className="small demoSideEmpty">
          尚无耗时数据。发起一次真实调用或载入历史快照后，这里显示真实 wall-clock 耗时。
        </div>
      )}

      <div className="demoSideSub mono">传输</div>
      <div className="small">
        {transport === "sse"
          ? "SSE 实时流"
          : transport === "poll"
          ? "轮询兜底（SSE 已断开，自动降级）"
          : transport === "replay"
          ? "本地回放，按快照 duration_ms 复现节奏"
          : transport === "mock"
          ? "本地 MOCK"
          : "未连接"}
      </div>
    </div>
  );
}

/* =====================================================================
 * 底部：真实耗时时间线
 * ===================================================================== */
function Timeline({ steps, placeholder }) {
  const model = useMemo(() => {
    const withClock = steps.filter((s) => s.startedAt !== null);
    if (withClock.length >= 2) {
      const base = Math.min(...withClock.map((s) => s.startedAt));
      const end = Math.max(
        ...withClock.map((s) => (s.endedAt !== null ? s.endedAt : s.startedAt + (s.durationMs || 0)))
      );
      const span = Math.max(end - base, 1);
      return {
        span,
        rows: steps.map((s) => {
          if (s.startedAt === null) return { step: s, offset: 0, width: 0 };
          const dur = s.durationMs !== null ? s.durationMs : (s.endedAt || s.startedAt) - s.startedAt;
          return {
            step: s,
            offset: ((s.startedAt - base) / span) * 100,
            width: Math.max((dur / span) * 100, dur > 0 ? 0.6 : 0)
          };
        })
      };
    }
    const total = Math.max(steps.reduce((acc, s) => acc + (s.durationMs || 0), 0), 1);
    let cursor = 0;
    return {
      span: total,
      rows: steps.map((s) => {
        const dur = s.durationMs || 0;
        const row = { step: s, offset: (cursor / total) * 100, width: (dur / total) * 100 };
        cursor += dur;
        return row;
      })
    };
  }, [steps]);

  const anyTimed = steps.some((s) => s.durationMs !== null || s.startedAt !== null);
  if (!anyTimed) {
    return (
      <div className="small">
        尚无耗时数据。发起一次运行或载入历史快照后，这里按真实 duration_ms 绘制横向时间线。
      </div>
    );
  }

  return (
    <div className="demoWf">
      <div className="demoWfScale small mono">
        <span>0 ms</span>
        <span>
          {placeholder ? "占位跨度" : "总跨度"} {fmtMs(model.span)}
        </span>
      </div>
      {model.rows.map((row) => {
        const meta = STATE_META[row.step.state] || STATE_META.pending;
        return (
          <div className="demoWfRow" key={row.step.key}>
            <div className="demoWfLabel mono">
              {row.step.index}. {BLUEPRINT_BY_KEY[row.step.key]?.graphTitle || row.step.title}
            </div>
            <div className="demoWfTrack">
              {row.width > 0 ? (
                <div
                  className={`demoWfBar st-${meta.cls}`}
                  style={{ marginLeft: `${row.offset}%`, width: `${Math.min(row.width, 100 - row.offset)}%` }}
                />
              ) : null}
            </div>
            <div className="demoWfMs mono">{row.step.durationMs === null ? "—" : fmtMs(row.step.durationMs)}</div>
          </div>
        );
      })}
    </div>
  );
}

/* =====================================================================
 * 主组件
 * ===================================================================== */
export default function DemoPipeline() {
  const [cfg, setCfg] = useState(null);
  const [cfgErr, setCfgErr] = useState("");
  const [cfgLoading, setCfgLoading] = useState(true);
  const [mode, setMode] = useState("live"); /* live | replay */
  const [autoReplayReason, setAutoReplayReason] = useState("");

  const [scenario, setScenario] = useState("normal");
  const [customer, setCustomer] = useState("Demo Customer");
  const [pnText, setPnText] = useState("DEMO-PN-002, DEMO-PN-003");
  const [productLine, setProductLine] = useState("Video");
  const [tier, setTier] = useState("Country");
  const [reuseMessageId, setReuseMessageId] = useState("");

  const [run, setRun] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [needKey, setNeedKey] = useState(false);
  const [keyInput, setKeyInput] = useState("");
  const [transport, setTransport] = useState(""); /* sse | poll | replay | mock */
  const [selected, setSelected] = useState(null); /* {type:'step'|'branch'|'gate', id} */

  const [runs, setRuns] = useState([]);
  const [runsErr, setRunsErr] = useState("");
  const [replayRunId, setReplayRunId] = useState("");
  const [replaySpeed, setReplaySpeed] = useState(1);

  const esRef = useRef(null);
  const pollRef = useRef(null);
  const watchdogRef = useRef(null);
  const lastEventRef = useRef(0);
  const replayTokenRef = useRef(0);
  const modeTouchedRef = useRef(false);
  const replaySpeedRef = useRef(1);

  useEffect(() => {
    replaySpeedRef.current = replaySpeed;
  }, [replaySpeed]);

  const stopAll = useCallback(() => {
    replayTokenRef.current += 1;
    if (esRef.current) {
      try {
        esRef.current.close();
      } catch {
        /* ignore */
      }
      esRef.current = null;
    }
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    if (watchdogRef.current) {
      clearInterval(watchdogRef.current);
      watchdogRef.current = null;
    }
  }, []);

  useEffect(() => () => stopAll(), [stopAll]);

  /* ---- config ---- */
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const data = USE_MOCK ? mockConfig() : await apiGetJson("/api/demo/config");
        if (!alive) return;
        setCfg(data || {});
        setCfgErr("");
        const liveOk = data?.live_available !== false;
        if (!liveOk && !modeTouchedRef.current) {
          setMode("replay");
          setAutoReplayReason(
            safeStr(data?.live_reason || data?.reason || "") ||
              (data?.ingress_configured === false
                ? "后端未配置入口 token（DEMO_INGRESS_TOKEN 缺失），无法发起真实调用。"
                : "后端报告 live_available=false，实时链路当前不可用。")
          );
        }
      } catch (e) {
        if (!alive) return;
        setCfg(null);
        setCfgErr(String(e?.message || e));
        if (!modeTouchedRef.current) {
          setMode("replay");
          setAutoReplayReason("无法读取 /api/demo/config，已自动切到回放模式。");
        }
      } finally {
        if (alive) setCfgLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  /* ---- 历史 run 列表 ---- */
  const loadRuns = useCallback(async () => {
    try {
      const data = USE_MOCK
        ? {
            runs: ["normal", "duplicate", "blocked"].map((s) => ({
              run_id: `mock-${s}`,
              scenario: s,
              source: "mock",
              final_state: s === "normal" ? "ok" : s
            }))
          }
        : await apiGetJson("/api/demo/runs");
      const list = normalizeRunList(data);
      setRuns(list);
      setRunsErr("");
      setReplayRunId((prev) => (prev && list.some((r) => r.run_id === prev) ? prev : list[0]?.run_id || ""));
    } catch (e) {
      setRuns([]);
      setRunsErr(String(e?.message || e));
    }
  }, []);

  useEffect(() => {
    if (mode === "replay") loadRuns();
  }, [mode, loadRuns]);

  const steps = run?.steps || normalizeSteps([]);
  const graphModel = useMemo(() => buildGraphModel(steps), [steps]);

  /* ---- 快照应用 ---- */
  const applySnapshot = useCallback((raw, fallback) => {
    setRun((prev) => normalizeRun(raw, fallback || prev || undefined));
  }, []);

  /* ---- 轮询兜底 ---- */
  const startPolling = useCallback(
    (runId) => {
      if (pollRef.current) return;
      setTransport("poll");
      let ticks = 0;
      pollRef.current = setInterval(async () => {
        ticks += 1;
        if (ticks > 200) {
          stopAll();
          setBusy(false);
          setErr("轮询超时：后端长时间未返回终态。");
          return;
        }
        try {
          const snap = await apiGetJson(`/api/demo/runs/${encodeURIComponent(runId)}`);
          const normalized = normalizeRun(snap, { run_id: runId });
          setRun(normalized);
          if (isRunComplete(normalized)) {
            stopAll();
            setBusy(false);
          }
        } catch (e) {
          /* 单次失败不中断轮询，只在界面上留错误提示 */
          setErr(String(e?.message || e));
        }
      }, 900);
    },
    [stopAll]
  );

  /* ---- SSE ---- */
  const startStream = useCallback(
    (runId) => {
      if (typeof window === "undefined" || typeof window.EventSource === "undefined") {
        startPolling(runId);
        return;
      }
      let es;
      try {
        es = new window.EventSource(`/api/demo/runs/${encodeURIComponent(runId)}/stream`);
      } catch {
        startPolling(runId);
        return;
      }
      esRef.current = es;
      setTransport("sse");
      lastEventRef.current = Date.now();

      const handleStep = (ev) => {
        lastEventRef.current = Date.now();
        let payload = null;
        try {
          payload = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (!payload || typeof payload !== "object") return;
        if (payload.run_id && runId && payload.run_id !== runId) return;
        const candidates = Array.isArray(payload.steps)
          ? payload.steps
          : payload.step
          ? [payload.step]
          : payload.key
          ? [payload]
          : [];
        if (!candidates.length) return;
        setRun((prev) => {
          const base = prev || { run_id: runId, scenario, steps: normalizeSteps([]) };
          let next = base.steps;
          candidates.forEach((c) => {
            next = mergeStepPatch(next, c);
          });
          return { ...base, steps: next };
        });
      };

      const handleDone = (ev) => {
        lastEventRef.current = Date.now();
        let payload = {};
        try {
          payload = JSON.parse(ev.data) || {};
        } catch {
          payload = {};
        }
        setRun((prev) =>
          prev
            ? {
                ...prev,
                finalState: safeStr(payload.final_state || prev.finalState),
                totalMs: toMs(payload.total_ms) ?? prev.totalMs
              }
            : prev
        );
        stopAll();
        setBusy(false);
        /* 结束后再取一次完整快照，补齐 SSE 期间可能漏掉的字段 */
        if (!USE_MOCK) {
          apiGetJson(`/api/demo/runs/${encodeURIComponent(runId)}`)
            .then((snap) => applySnapshot(snap, { run_id: runId }))
            .catch(() => {});
        }
      };

      es.addEventListener("step", handleStep);
      es.addEventListener("done", handleDone);
      es.onmessage = handleStep;
      es.onerror = () => {
        try {
          es.close();
        } catch {
          /* ignore */
        }
        if (esRef.current === es) esRef.current = null;
        if (watchdogRef.current) {
          clearInterval(watchdogRef.current);
          watchdogRef.current = null;
        }
        startPolling(runId);
      };

      /* 看门狗：SSE 静默 15 秒即切轮询，避免现场卡在半途 */
      watchdogRef.current = setInterval(() => {
        if (Date.now() - lastEventRef.current > 15000) {
          try {
            es.close();
          } catch {
            /* ignore */
          }
          if (esRef.current === es) esRef.current = null;
          clearInterval(watchdogRef.current);
          watchdogRef.current = null;
          startPolling(runId);
        }
      }, 3000);
    },
    [applySnapshot, scenario, startPolling, stopAll]
  );

  /* ---- 回放：按每步真实 duration_ms 复现节奏 ---- */
  const playback = useCallback(
    async (snapshot) => {
      stopAll();
      const token = ++replayTokenRef.current;
      const full = normalizeRun(snapshot, undefined);
      setTransport(USE_MOCK ? "mock" : "replay");
      setRun({
        ...full,
        finalState: "",
        steps: full.steps.map((s) => ({
          ...s,
          state: "pending",
          facts: [],
          request: null,
          response: null,
          links: [],
          note: "",
          durationMs: null,
          startedAt: null,
          endedAt: null
        }))
      });
      setSelected(null);
      setBusy(true);

      for (let i = 0; i < full.steps.length; i += 1) {
        if (token !== replayTokenRef.current) return;
        const target = full.steps[i];
        if (target.state !== "skipped" && target.state !== "pending") {
          setRun((prev) =>
            prev
              ? { ...prev, steps: prev.steps.map((s) => (s.key === target.key ? { ...s, state: "running" } : s)) }
              : prev
          );
          const raw = target.durationMs !== null ? target.durationMs : 400;
          const wait = Math.min(Math.max(raw, 150), 8000) / (replaySpeedRef.current || 1);
          await sleep(wait);
          if (token !== replayTokenRef.current) return;
        } else {
          await sleep(120 / (replaySpeedRef.current || 1));
          if (token !== replayTokenRef.current) return;
        }
        setRun((prev) => (prev ? { ...prev, steps: prev.steps.map((s) => (s.key === target.key ? target : s)) } : prev));
      }
      if (token !== replayTokenRef.current) return;
      setRun((prev) => (prev ? { ...prev, finalState: full.finalState, totalMs: full.totalMs } : prev));
      setBusy(false);
    },
    [stopAll]
  );

  /* ---- 动作 ---- */
  const onRunLive = useCallback(async () => {
    stopAll();
    setErr("");
    setSelected(null);
    setBusy(true);
    try {
      if (USE_MOCK) {
        await playback(mockRunSnapshot(scenario));
        return;
      }
      const body = {
        scenario,
        customer: customer.trim(),
        pns: parsePns(pnText),
        product_line: productLine.trim(),
        tier: tier.trim(),
        reuse_message_id: reuseMessageId.trim() ? reuseMessageId.trim() : null
      };
      const resp = await apiPostJson("/api/demo/run", body, demoKeyHeaders());
      const runId = safeStr(resp?.run_id || "");
      if (!runId) throw new Error("后端未返回 run_id");
      setRun(normalizeRun(resp, { run_id: runId, scenario }));
      startStream(runId);
    } catch (e) {
      if (e?.status === 403) {
        setNeedKey(true);
        setErr("演示口令无效或缺失。触发真实链路需要口令，只读回放不需要。");
      } else {
        setErr(String(e?.message || e));
      }
      setBusy(false);
      setTransport("");
    }
  }, [customer, playback, pnText, productLine, reuseMessageId, scenario, startStream, stopAll, tier]);

  const onReplay = useCallback(async () => {
    if (!replayRunId) {
      setErr("请先选择一个历史 run。");
      return;
    }
    setErr("");
    try {
      if (USE_MOCK) {
        const sc = replayRunId.replace(/^mock-/, "");
        await playback(mockRunSnapshot(SCENARIOS.some((s) => s.key === sc) ? sc : "normal"));
        return;
      }
      const snap = await apiGetJson(`/api/demo/runs/${encodeURIComponent(replayRunId)}`);
      await playback(snap);
    } catch (e) {
      setErr(String(e?.message || e));
      setBusy(false);
    }
  }, [playback, replayRunId]);

  const onStop = useCallback(() => {
    stopAll();
    setBusy(false);
  }, [stopAll]);

  const switchMode = useCallback(
    (next) => {
      modeTouchedRef.current = true;
      stopAll();
      setBusy(false);
      setErr("");
      setMode(next);
    },
    [stopAll]
  );

  const liveDisabled = cfgLoading || cfg?.live_available === false || !!cfgErr;
  /* running 不是终态：跑到一半时不能写「终态 执行中」 */
  const settled = safeStr(run?.finalState) !== "" && safeStr(run?.finalState) !== "running";
  const finalMeta = settled ? STATE_META[normState(run.finalState)] : null;
  const placeholder = !!run?.placeholder;

  const selectedStep =
    selected?.type === "step" ? steps.find((s) => s.key === selected.id) || null : null;
  const selectedBranch = selected?.type === "branch" ? BRANCH_BY_ID[selected.id] : null;

  return (
    <div className="stack demoRoot">
      {mode === "replay" ? <div className="demoReplayBadge mono">回放 · 非实时</div> : null}
      {USE_MOCK ? (
        <div className="demoMockBanner mono">
          MOCK 模式已开启：下方全部为本地假数据，不是真实链路结果。演示前务必把 USE_MOCK 改回 false。
        </div>
      ) : null}
      {placeholder ? (
        <div className="demoMockBanner mono">
          当前载入的是仓库内置占位快照：字段值为 PLACEHOLDER 占位符、耗时为占位数字，
          不是任何一次真实运行的结果，只用于验证页面渲染。
        </div>
      ) : null}

      {/* ================= 顶部工具条 ================= */}
      <div className="demoBar">
        <div className="demoBarMain">
          <div className="demoBarTitle">端到端演示</div>

          <div className="demoSeg">
            <button
              type="button"
              className={`demoSegBtn${mode === "live" ? " active" : ""}`}
              onClick={() => switchMode("live")}
            >
              实时
            </button>
            <button
              type="button"
              className={`demoSegBtn${mode === "replay" ? " active" : ""}`}
              onClick={() => switchMode("replay")}
            >
              回放
            </button>
          </div>

          {mode === "live" ? (
            <>
              <div className="demoSeg">
                {SCENARIOS.map((s) => (
                  <button
                    key={s.key}
                    type="button"
                    className={`demoSegBtn${scenario === s.key ? " active" : ""}`}
                    onClick={() => setScenario(s.key)}
                    disabled={busy}
                    title={s.hint}
                  >
                    {s.label}
                  </button>
                ))}
              </div>
              <button className="btn primary demoBarBtn" onClick={onRunLive} disabled={busy || liveDisabled}>
                {cfgLoading ? "读取配置中…" : busy ? "运行中…" : "发起真实调用"}
              </button>
              {busy ? (
                <button className="btn demoBarBtn" onClick={onStop}>
                  中止观察
                </button>
              ) : null}
            </>
          ) : (
            <>
              <select
                className="input mono demoRunSelect"
                value={replayRunId}
                onChange={(e) => setReplayRunId(e.target.value)}
                disabled={busy}
                aria-label="历史执行选择器"
              >
                {runs.length === 0 ? <option value="">（无历史 run）</option> : null}
                {runs.map((r) => (
                  <option key={r.run_id} value={r.run_id}>
                    {r.label || `${scenarioLabel(r.scenario)} · ${fmtClock(r.startedAt)}`}
                    {r.source === "fixture" ? " [内置快照]" : ""}
                    {r.placeholder ? " [占位数据]" : ""}
                    {r.finalState ? ` · ${r.finalState}` : ""}
                  </option>
                ))}
              </select>
              <button className="btn primary demoBarBtn" onClick={onReplay} disabled={busy || !replayRunId}>
                {busy ? "回放中…" : "开始回放"}
              </button>
              {busy ? (
                <button className="btn demoBarBtn" onClick={onStop}>
                  停止回放
                </button>
              ) : null}
              <div className="demoSeg">
                {[1, 2, 4].map((sp) => (
                  <button
                    key={sp}
                    type="button"
                    className={`demoSegBtn${replaySpeed === sp ? " active" : ""}`}
                    onClick={() => setReplaySpeed(sp)}
                  >
                    {sp}x
                  </button>
                ))}
              </div>
              <button className="btn demoBarBtn" onClick={loadRuns} disabled={busy}>
                刷新
              </button>
            </>
          )}

          <div className="demoBarRight">
            {run?.source === "fixture" ? <span className="pill">内置快照</span> : null}
            {placeholder ? <span className="demoStateChip st-duplicate">占位数据 · 非实测</span> : null}
            {finalMeta ? (
              <span className={`demoStateChip st-${finalMeta.cls}`}>终态 {finalMeta.label}</span>
            ) : busy ? (
              <span className="demoStateChip st-running">执行中</span>
            ) : null}
            <span className="demoBarTotal mono">
              总耗时 {run?.totalMs === null || run?.totalMs === undefined ? "—" : fmtMs(run.totalMs)}
            </span>
          </div>
        </div>

        {/* 元信息一行小字 */}
        <div className="demoMetaLine mono">
          <span>{safeStr(cfg?.project_id) || (cfgLoading ? "读取中…" : "—")}</span>
          <span>{safeStr(cfg?.region) || "—"}</span>
          <span>{cfg ? deriveServiceName(cfg) : "—"}</span>
          <span>标签页 {safeStr(cfg?.sheet_name) || "—"}</span>
          <span>数据版本 {safeStr(cfg?.data_version) || "—"}</span>
          <span>
            入口{" "}
            {cfg?.cloud_run_health && typeof cfg.cloud_run_health === "object"
              ? `${cfg.cloud_run_health.ok ? "ok" : "down"} · ${safeStr(cfg.cloud_run_health.dispatch_mode) || "—"}`
              : "—"}
          </span>
          {run?.run_id ? <span>run {run.run_id}</span> : null}
          <span className={`demoTransport t-${transport || "idle"}`}>
            传输{" "}
            {transport === "sse"
              ? "SSE"
              : transport === "poll"
              ? "轮询兜底"
              : transport === "replay"
              ? "回放"
              : transport === "mock"
              ? "MOCK"
              : "未连接"}
          </span>
        </div>

        {needKey ? (
          <div className="demoKeyRow">
            <span className="demoKeyLabel">演示口令</span>
            <input
              className="input demoKeyInput"
              type="password"
              value={keyInput}
              placeholder="触发真实链路需要，只读回放不需要"
              onChange={(e) => setKeyInput(e.target.value)}
            />
            <button
              type="button"
              className="btn"
              onClick={() => {
                writeDemoKey(keyInput.trim());
                setKeyInput("");
                setNeedKey(false);
                setErr("");
              }}
            >
              记住口令
            </button>
            <span className="small">保存在本机浏览器，换设备需重新输入</span>
          </div>
        ) : null}

        {cfgErr ? <div className="demoAlert err">配置读取失败：{cfgErr}</div> : null}
        {mode === "replay" && autoReplayReason ? (
          <div className="demoAlert">自动进入回放：{autoReplayReason}</div>
        ) : null}
        {mode === "replay" && runsErr ? <div className="demoAlert err">历史列表读取失败：{runsErr}</div> : null}
        {mode === "live" && cfgLoading ? (
          <div className="demoAlert">正在读取 /api/demo/config，确认实时链路是否可用…</div>
        ) : null}
        {mode === "live" && liveDisabled && !cfgLoading ? (
          <div className="demoAlert">
            实时链路不可用{autoReplayReason ? `：${autoReplayReason}` : ""}，请切到回放模式。
          </div>
        ) : null}
        {err ? <div className="demoAlert err">{err}</div> : null}

        <details className="demoParams">
          <summary className="mono">演示消息参数（发送给 Cloud Run 入口的真实字段）</summary>
          <div className="demoParamGrid">
            <label className="demoParam">
              <span className="demoParamLabel">客户</span>
              <input className="input mono" value={customer} onChange={(e) => setCustomer(e.target.value)} />
            </label>
            <label className="demoParam">
              <span className="demoParamLabel">PN（空格/逗号分隔）</span>
              <input className="input mono" value={pnText} onChange={(e) => setPnText(e.target.value)} />
            </label>
            <label className="demoParam">
              <span className="demoParamLabel">产品线</span>
              <input className="input mono" value={productLine} onChange={(e) => setProductLine(e.target.value)} />
            </label>
            <label className="demoParam">
              <span className="demoParamLabel">价格层级</span>
              <input className="input mono" value={tier} onChange={(e) => setTier(e.target.value)} />
            </label>
            {scenario === "duplicate" ? (
              <label className="demoParam demoParamWide">
                <span className="demoParamLabel">
                  复用 message_id（留空则由后端复用上一次成功的 id
                  {cfg?.last_message_id ? `，当前：${safeStr(cfg.last_message_id)}` : ""}）
                </span>
                <input
                  className="input mono"
                  value={reuseMessageId}
                  onChange={(e) => setReuseMessageId(e.target.value)}
                  placeholder={safeStr(cfg?.last_message_id) || "demo-YYYYMMDD-NNN"}
                />
              </label>
            ) : null}
          </div>
        </details>
      </div>

      {/* ================= 主区：执行图 + 详情 ================= */}
      <div className="demoMain">
        <div className="demoGraphPane">
          <div className="demoPaneHead">
            <span className="demoPaneTitle">执行图</span>
            <span className="demoPaneHint">
              实线主色 = 本次真实走过；浅灰虚线 = 系统设计里存在但本次未触发的分支
            </span>
          </div>
          <div className="demoGraphBody">
            <ExecutionGraph
              steps={steps}
              model={graphModel}
              selected={selected}
              onSelect={setSelected}
              region={safeStr(cfg?.region)}
            />
          </div>
          <div className="demoGraphFoot">
            <div className="demoLegend">
              <span className="demoLegendItem">
                <span className="demoLegendSwatch st-ok" />成功
              </span>
              <span className="demoLegendItem">
                <span className="demoLegendSwatch st-running" />执行中
              </span>
              <span className="demoLegendItem">
                <span className="demoLegendSwatch st-duplicate" />去重 / 降级
              </span>
              <span className="demoLegendItem">
                <span className="demoLegendSwatch st-blocked" />拦截
              </span>
              <span className="demoLegendItem">
                <span className="demoLegendSwatch st-error" />失败
              </span>
              <span className="demoLegendItem">
                <span className="demoLegendSwatch st-skipped" />跳过
              </span>
              <span className="demoLegendItem">
                <span className="demoLegendSwatch st-idle" />未走到的设计分支
              </span>
            </div>
            <div className="demoGhostStrip">
              <span className="demoGhostCaption mono">未接线通路 · 只画不跑，永不点亮</span>
              {GHOST_NODES.map((g) => (
                <span className="demoGhostNode" key={g.key} title={g.why}>
                  <span className="demoGhostTitle mono">{g.title}</span>
                  <span className="demoGhostTag">{g.tag}</span>
                </span>
              ))}
            </div>
          </div>
        </div>

        <div className="demoSidePane">
          <div className="demoPaneHead">
            <span className="demoPaneTitle">
              {selectedStep
                ? "节点详情"
                : selectedBranch
                ? "分支详情"
                : selected?.type === "gate"
                ? "终点详情"
                : "执行摘要"}
            </span>
            {selected ? (
              <button type="button" className="demoPaneClear" onClick={() => setSelected(null)}>
                返回摘要
              </button>
            ) : null}
          </div>
          <div className="demoSideBody">
            {selectedStep ? (
              <StepDetail
                step={selectedStep}
                runId={run?.run_id}
                mode={USE_MOCK ? "mock" : mode}
                placeholder={placeholder}
              />
            ) : selectedBranch ? (
              <BranchDetail branch={selectedBranch} lit={!!graphModel.litBranch[selectedBranch.id]} />
            ) : selected?.type === "gate" ? (
              <GateDetail lit={graphModel.gateLit} gateStep={graphModel.byKey.artifact} />
            ) : (
              <RunSummary run={run} cfg={cfg} steps={steps} transport={transport} mode={mode} />
            )}
          </div>
        </div>
      </div>

      {/* ================= 底部：时间线 + 图外容错 ================= */}
      <div className="demoBottom">
        <div className="demoTimelinePane">
          <div className="demoPaneHead">
            <span className="demoPaneTitle">步骤时间线</span>
            <span className="demoPaneHint">
              {placeholder ? "宽度按占位快照的 duration_ms 绘制，非实测耗时" : "宽度 = 真实 duration_ms，无补间动画"}
            </span>
          </div>
          <div className="demoTimelineBody">
            <Timeline steps={steps} placeholder={placeholder} />
          </div>
        </div>

        <div className="demoFaultPane">
          <div className="demoPaneHead">
            <span className="demoPaneTitle">图外容错</span>
            <span className="demoPaneHint">跨节点、不属于任何一跳</span>
          </div>
          <div className="demoFaultBody">
            {OUT_OF_GRAPH.map((f) => (
              <div className="demoFaultItem" key={f.key}>
                <div className="demoFaultTitle mono">{f.title}</div>
                <div className="demoFaultBodyText">{f.body}</div>
                {f.key === "transport" ? (
                  <div className={`demoFaultNow mono t-${transport || "idle"}`}>
                    当前：
                    {transport === "sse"
                      ? "SSE 实时流"
                      : transport === "poll"
                      ? "轮询兜底（已降级）"
                      : transport === "replay"
                      ? "本地回放"
                      : transport === "mock"
                      ? "MOCK"
                      : "未连接"}
                  </div>
                ) : null}
                {f.key === "persist" && run?.persistError ? (
                  <div className="demoFaultNow mono t-bad">当前：persist_error = {run.persistError}</div>
                ) : null}
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
