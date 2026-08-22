import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiGetJson, apiPostJson } from "./api.js";
import { safeStr } from "./format.js";

/* =====================================================================
 * 端到端演示（answer: 真实链路可视化）
 *
 * 接口契约见 demo_pipeline_contract.md：
 *   GET  /api/demo/config
 *   POST /api/demo/run
 *   GET  /api/demo/runs/{run_id}/stream   (SSE)
 *   GET  /api/demo/runs/{run_id}          (快照，SSE 兜底 + 回放共用)
 *   GET  /api/demo/runs                   (历史列表)
 *   GET  /api/demo/runs/{run_id}/artifact  (xlsx 下载)
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
    layer: "local"
  },
  {
    key: "ingress",
    index: 2,
    title: "Cloud Run 入口",
    subtitle: "鉴权 · 契约校验 · 派发",
    layer: "cloud"
  },
  {
    key: "workflow",
    index: 3,
    title: "Google Workflows 编排",
    subtitle: "execution id · 执行状态",
    layer: "cloud"
  },
  {
    key: "sheet",
    index: 4,
    title: "Google Sheets 任务行",
    subtitle: "updatedRange · 去重判定",
    layer: "cloud"
  },
  {
    key: "ingest",
    index: 5,
    title: "内网摄取与任务建立",
    subtitle: "任务 id · 幂等键",
    layer: "local"
  },
  {
    key: "pricing",
    index: 6,
    title: "确定性算价",
    subtitle: "匹配路径 · 规则/数据版本",
    layer: "local"
  },
  {
    key: "artifact",
    index: 7,
    title: "GSP 模板制品",
    subtitle: "xlsx · 人工复核边界",
    layer: "local"
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
    where: "位置：Cloud Run 入口与 Workflows 之间的消息缓冲",
    why: "当前入口直接同步派发 Workflows，未接消息队列"
  },
  {
    key: "eventarc",
    title: "Eventarc",
    tag: "规划中",
    where: "位置：由事件触发 Workflows",
    why: "当前由入口显式调用 Workflows executions.create"
  },
  {
    key: "windows",
    title: "Windows 执行端",
    tag: "已退役",
    where: "位置：原 GSP 桌面自动化执行机",
    why: "已由 Cloud Run + Workflows + 内网服务取代，不再参与链路"
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

const SCENARIOS = [
  { key: "normal", label: "正常链路", hint: "七跳依次点亮，末跳给出 xlsx 与人工复核边界" },
  { key: "duplicate", label: "重发同一条消息", hint: "复用上一次 message_id，Sheets 不追加第二行" },
  { key: "blocked", label: "越权消息被拦截", hint: "不满足契约，入口 422 拦截，未触达外部系统" }
];

const TERMINAL_STATES = new Set(["ok", "duplicate", "blocked", "skipped", "error"]);

/* ---------------------------------------------------------------------
 * 防御性归一化：后端可能少字段 / 字段形状不同 / 时间戳单位不同
 * ------------------------------------------------------------------- */
function normState(v) {
  const s = String(v || "").toLowerCase().trim();
  return STATE_META[s] ? s : "pending";
}

function toEpochMs(v) {
  const n = Number(v);
  if (!Number.isFinite(n) || n <= 0) return null;
  /* 契约给的是 epoch 秒（浮点），但容忍后端直接给毫秒 */
  return n > 1e11 ? n : n * 1000;
}

function toMs(v) {
  const n = Number(v);
  if (!Number.isFinite(n) || n < 0) return null;
  return n;
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
    startedAt: toEpochMs(src.started_at) ?? base.startedAt ?? null,
    endedAt: toEpochMs(src.ended_at) ?? null,
    totalMs: toMs(src.total_ms),
    finalState: safeStr(src.final_state || ""),
    environment: src.environment && typeof src.environment === "object" ? src.environment : null,
    source: safeStr(src.source || base.source || ""),
    label: safeStr(src.label || base.label || ""),
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
    data_version: "v-20260817T130034Z-5c46304ba8",
    last_message_id: "demo-20260822-001",
    replay_count: 3
  };
}

function mockSteps(scenario) {
  const t0 = Date.now() / 1000;
  const mk = (key, offsetS, durS, state, extra) => ({
    key,
    index: BLUEPRINT_BY_KEY[key].index,
    title: BLUEPRINT_BY_KEY[key].title,
    subtitle: BLUEPRINT_BY_KEY[key].subtitle,
    layer: BLUEPRINT_BY_KEY[key].layer,
    state,
    started_at: t0 + offsetS,
    ended_at: t0 + offsetS + durS,
    duration_ms: Math.round(durS * 1000),
    ...extra
  });
  const skipped = (key) => ({
    key,
    index: BLUEPRINT_BY_KEY[key].index,
    title: BLUEPRINT_BY_KEY[key].title,
    subtitle: BLUEPRINT_BY_KEY[key].subtitle,
    layer: BLUEPRINT_BY_KEY[key].layer,
    state: "skipped",
    duration_ms: 0,
    facts: [{ label: "原因", value: "上游未通过，未触达外部系统" }]
  });

  const message = mk("message", 0, 0.03, "ok", {
    facts: [
      { label: "message_id", value: "demo-20260822-001" },
      { label: "客户", value: "Demo Customer" },
      { label: "PN", value: "DEMO-PN-002, DEMO-PN-003" },
      { label: "层级 / 产品线", value: "Country / Video" }
    ],
    request: { channel: "huachat", text: "PN: DEMO-PN-002, DEMO-PN-003 客户: Demo Customer" },
    response: { parsed_pns: ["DEMO-PN-002", "DEMO-PN-003"], tier: "Country" }
  });

  if (scenario === "blocked") {
    return [
      { ...message, facts: message.facts.filter((f) => f.label !== "PN") },
      mk("ingress", 0.05, 0.42, "blocked", {
        facts: [
          { label: "服务", value: "pricing-huachat-ingress" },
          { label: "HTTP", value: "422 Unprocessable Entity" },
          { label: "拒绝原因", value: "missing field: pns" }
        ],
        request: { method: "POST", url: "https://pricing-huachat-ingress.../events/huachat", body: { customer: "Demo Customer" } },
        response: { status: 422, body: { detail: [{ loc: ["body", "pns"], msg: "field required" }] } },
        note: "拦截发生在入口，Workflows / Sheets 均未被调用"
      }),
      skipped("workflow"),
      skipped("sheet"),
      skipped("ingest"),
      skipped("pricing"),
      skipped("artifact")
    ];
  }

  const ingress = mk("ingress", 0.05, 2.97, "ok", {
    facts: [
      { label: "服务", value: "pricing-huachat-ingress" },
      { label: "区域", value: "europe-west1" },
      { label: "revision", value: "pricing-huachat-ingress-00007-x4k" },
      {
        label: "执行名",
        value:
          "projects/339841524852/locations/europe-west1/workflows/pricing-request-sandbox/executions/f152de47-9d0a-4a2e-9a1f-77c1"
      }
    ],
    request: { method: "POST", url: ".../events/huachat", body: { message_id: "demo-20260822-001" } },
    response: { status: 200, body: { dispatched: true, execution: "…/executions/f152de47" } },
    note: "冷启动约 3 秒属正常现象"
  });

  const workflow = mk("workflow", 3.05, 4.1, "ok", {
    facts: [
      { label: "工作流", value: "pricing-request-sandbox" },
      { label: "execution id", value: "f152de47-9d0a-4a2e-9a1f-77c1" },
      { label: "状态", value: "SUCCEEDED" }
    ],
    response: { state: "SUCCEEDED", result: { updatedRange: "'2026.07'!A118:N118" } },
    links: [
      {
        label: "GCP 控制台执行详情",
        url: "https://console.cloud.google.com/workflows/workflow/europe-west1/pricing-request-sandbox"
      }
    ]
  });

  if (scenario === "duplicate") {
    return [
      message,
      { ...ingress, facts: ingress.facts.concat([{ label: "复用 message_id", value: "demo-20260822-001" }]) },
      { ...workflow, state: "duplicate", facts: workflow.facts.concat([{ label: "duplicate", value: "true" }]) },
      mk("sheet", 7.2, 0.6, "duplicate", {
        facts: [
          { label: "目标标签页", value: "2026.07" },
          { label: "已存在行", value: "'2026.07'!A118:N118" },
          { label: "本次写入", value: "未追加第二行" },
          { label: "幂等键", value: "sha256:9f2c…（与上次一致）" }
        ],
        response: { duplicate: true, existing_range: "'2026.07'!A118:N118", appended: false },
        note: "同一 message_id 重发不会产生第二行任务，这是幂等保证"
      }),
      mk("ingest", 7.9, 0.12, "duplicate", {
        facts: [
          { label: "任务 id", value: "wf-20260822-0117（复用）" },
          { label: "动作", value: "未新建任务" }
        ]
      }),
      skipped("pricing"),
      skipped("artifact")
    ];
  }

  return [
    message,
    ingress,
    workflow,
    mk("sheet", 7.2, 0.85, "ok", {
      facts: [
        { label: "工作簿", value: "1g96wjgMSGMhXrzLGwCAIQic_mYkS8xsThxJryfFHJ2A" },
        { label: "标签页", value: "2026.07" },
        { label: "updatedRange", value: "'2026.07'!A118:N118" },
        { label: "写入列数", value: "14" }
      ],
      response: { updates: { updatedRange: "'2026.07'!A118:N118", updatedColumns: 14 } },
      note: "只显示本次写入的一行，不展示整张工作簿"
    }),
    mk("ingest", 8.1, 0.2, "ok", {
      facts: [
        { label: "任务 id", value: "wf-20260822-0118" },
        { label: "幂等键", value: "sha256:9f2c1b…" },
        { label: "状态机初态", value: "created" }
      ]
    }),
    mk("pricing", 8.35, 0.9, "ok", {
      facts: [
        { label: "匹配路径", value: "exact → country_table" },
        { label: "命中编号", value: "DEMO-PN-002 / DEMO-PN-003" },
        { label: "价格层级", value: "Country" },
        { label: "规则版本", value: "rules-2026.07.1" },
        { label: "数据版本", value: "v-20260817T130034Z-5c46304ba8" }
      ]
    }),
    mk("artifact", 9.3, 0.55, "ok", {
      facts: [
        { label: "文件名", value: "Demo_Customer_Country_import_upload_Model.xlsx" },
        { label: "submission_authorized", value: "false" },
        { label: "人工复核", value: "必须由人工确认后才可提交 GSP" }
      ],
      note: "系统只生成制品，不代替人工提交"
    })
  ];
}

function mockRunSnapshot(scenario) {
  const steps = mockSteps(scenario);
  const finalState =
    scenario === "blocked" ? "blocked" : scenario === "duplicate" ? "duplicate" : "ok";
  const total = steps.reduce((acc, s) => acc + (Number(s.duration_ms) || 0), 0);
  return {
    run_id: `mock-${scenario}`,
    scenario,
    started_at: Date.now() / 1000,
    total_ms: total,
    final_state: finalState,
    source: "mock",
    steps
  };
}

/* =====================================================================
 * 子组件
 * ===================================================================== */
function EnvBar({ cfg, loading }) {
  const dash = loading ? "读取中…" : "—";
  const items = [
    { label: "GCP 项目", value: safeStr(cfg?.project_id) || dash },
    { label: "区域", value: safeStr(cfg?.region) || dash },
    { label: "Cloud Run 服务", value: cfg ? deriveServiceName(cfg) : dash },
    { label: "目标标签页", value: safeStr(cfg?.sheet_name) || dash },
    { label: "数据版本", value: safeStr(cfg?.data_version) || dash }
  ];
  const health = cfg?.cloud_run_health;
  return (
    <div className="demoEnvBar">
      {items.map((it) => (
        <div className="demoEnvItem" key={it.label}>
          <div className="demoEnvLabel">{it.label}</div>
          <div className="demoEnvValue mono" title={it.value}>
            {it.value}
          </div>
        </div>
      ))}
      <div className="demoEnvItem">
        <div className="demoEnvLabel">入口健康</div>
        <div className="demoEnvValue mono">
          {health && typeof health === "object"
            ? `${health.ok ? "ok" : "down"} · ${safeStr(health.dispatch_mode) || "—"}`
            : dash}
        </div>
      </div>
    </div>
  );
}

function PipelineNode({ step, selected, onSelect }) {
  const meta = STATE_META[step.state] || STATE_META.pending;
  return (
    <button
      type="button"
      className={`demoNode st-${meta.cls}${selected ? " sel" : ""}`}
      onClick={() => onSelect(step.key)}
      title={step.title}
    >
      <div className="demoNodeTop">
        <span className="demoNodeIdx">{step.index}</span>
        <span className={`demoStateChip st-${meta.cls}`}>{meta.label}</span>
      </div>
      <div className="demoNodeTitle">{step.title}</div>
      <div className="demoNodeSub">{step.subtitle || " "}</div>
      <div className="demoNodeFoot mono">{step.durationMs === null ? "—" : fmtMs(step.durationMs)}</div>
    </button>
  );
}

function Connector({ small }) {
  return <div className={`demoConn${small ? " small" : ""}`} aria-hidden="true" />;
}

function Waterfall({ steps }) {
  const timed = steps.filter((s) => s.durationMs !== null || s.startedAt !== null);
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
    /* 没有 wall-clock 时退化为顺序堆叠，仍按真实 duration 取宽 */
    const total = Math.max(
      steps.reduce((acc, s) => acc + (s.durationMs || 0), 0),
      1
    );
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

  if (!timed.length) {
    return <div className="small">尚无耗时数据。发起一次运行或载入历史快照后显示真实 wall-clock 耗时。</div>;
  }

  return (
    <div className="demoWaterfall">
      <div className="demoWfScale small mono">
        <span>0 ms</span>
        <span>总跨度 {fmtMs(model.span)}</span>
      </div>
      {model.rows.map((row) => {
        const meta = STATE_META[row.step.state] || STATE_META.pending;
        return (
          <div className="demoWfRow" key={row.step.key}>
            <div className="demoWfLabel mono">
              {row.step.index}. {row.step.title}
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

function StepDetail({ step, runId, mode, placeholder }) {
  if (!step) return <div className="small">点选上方任一节点查看该跳的真实字段与报文。</div>;
  const meta = STATE_META[step.state] || STATE_META.pending;
  const showArtifactLink =
    step.key === "artifact" && step.state === "ok" && runId && mode !== "mock" && !placeholder;
  return (
    <div className="demoDetail">
      <div className="demoDetailHead">
        <div className="demoDetailTitle mono">
          {step.index}. {step.title}
        </div>
        <div className="row wrap">
          <span className={`demoStateChip st-${meta.cls}`}>{meta.label}</span>
          <span className="pill">{step.layer === "cloud" ? "GOOGLE CLOUD" : "内网 r230"}</span>
          <span className="pill mono">耗时 {step.durationMs === null ? "—" : fmtMs(step.durationMs)}</span>
          <span className="pill mono">起 {fmtClock(step.startedAt)}</span>
        </div>
      </div>

      {step.note ? <div className="demoNote">{step.note}</div> : null}

      {step.facts.length ? (
        <div className="tableWrap" style={{ marginTop: 10 }}>
          <table className="table dense">
            <thead>
              <tr>
                <th style={{ width: 220 }}>字段</th>
                <th>{placeholder ? "占位值（非实测）" : "真实值"}</th>
              </tr>
            </thead>
            <tbody>
              {step.facts.map((f, i) => (
                <tr key={`${f.label}-${i}`}>
                  <td className="mono">{f.label}</td>
                  <td className="mono" style={{ wordBreak: "break-all" }}>
                    {f.value}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="small" style={{ marginTop: 10 }}>
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
        <div className="row wrap" style={{ marginTop: 10 }}>
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
              下载本次 xlsx 制品
            </a>
          ) : null}
        </div>
      ) : null}
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
  const [transport, setTransport] = useState(""); /* sse | poll | replay | mock */
  const [selectedKey, setSelectedKey] = useState("");

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
        ? { runs: ["normal", "duplicate", "blocked"].map((s) => ({ run_id: `mock-${s}`, scenario: s, source: "mock", final_state: s === "normal" ? "ok" : s })) }
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
  const selectedStep = useMemo(() => {
    if (!steps.length) return null;
    const hit = steps.find((s) => s.key === selectedKey);
    return hit || null;
  }, [steps, selectedKey]);

  const bands = useMemo(() => {
    const out = [];
    steps.forEach((s) => {
      const last = out[out.length - 1];
      if (last && last.layer === s.layer) last.steps.push(s);
      else out.push({ layer: s.layer, steps: [s] });
    });
    return out;
  }, [steps]);

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
      setSelectedKey("");
      setBusy(true);

      for (let i = 0; i < full.steps.length; i += 1) {
        if (token !== replayTokenRef.current) return;
        const target = full.steps[i];
        if (target.state !== "skipped" && target.state !== "pending") {
          setRun((prev) =>
            prev ? { ...prev, steps: prev.steps.map((s) => (s.key === target.key ? { ...s, state: "running" } : s)) } : prev
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
        setSelectedKey(target.key);
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
    setSelectedKey("");
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
      const resp = await apiPostJson("/api/demo/run", body);
      const runId = safeStr(resp?.run_id || "");
      if (!runId) throw new Error("后端未返回 run_id");
      setRun(normalizeRun(resp, { run_id: runId, scenario }));
      startStream(runId);
    } catch (e) {
      setErr(String(e?.message || e));
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
  const finalMeta = run?.finalState ? STATE_META[normState(run.finalState)] : null;

  return (
    <div className="stack demoRoot">
      {mode === "replay" ? <div className="demoReplayBadge mono">回放 · 非实时</div> : null}
      {USE_MOCK ? (
        <div className="demoMockBanner mono">
          MOCK 模式已开启：下方全部为本地假数据，不是真实链路结果。演示前务必把 USE_MOCK 改回 false。
        </div>
      ) : null}
      {run?.placeholder ? (
        <div className="demoMockBanner mono">
          当前载入的是仓库内置占位快照：字段值为 PLACEHOLDER 占位符、耗时为占位数字，
          不是任何一次真实运行的结果，只用于验证页面渲染。
        </div>
      ) : null}

      <div className="card">
        <div className="cardHeader">
          <h2>端到端演示 · 真实链路</h2>
          <div className="small">
            聊天消息 → Cloud Run → Workflows → Sheets → 内网摄取 → 确定性算价 → GSP 制品
          </div>
        </div>
        <div className="cardBody">
          {cfgErr ? <div className="small err" style={{ marginBottom: 10 }}>配置读取失败：{cfgErr}</div> : null}
          <EnvBar cfg={cfg} loading={cfgLoading} />

          <div className="hr" />

          <div className="demoControls">
            <div className="demoScenarioRow">
              {SCENARIOS.map((s) => (
                <button
                  key={s.key}
                  type="button"
                  className={`demoScenarioBtn${scenario === s.key ? " active" : ""}`}
                  onClick={() => setScenario(s.key)}
                  disabled={busy}
                >
                  <span className="demoScenarioName">{s.label}</span>
                  <span className="demoScenarioHint">{s.hint}</span>
                </button>
              ))}
            </div>

            <div className="row wrap demoModeRow">
              <div className="demoToggle">
                <button
                  type="button"
                  className={`demoToggleBtn${mode === "live" ? " active" : ""}`}
                  onClick={() => switchMode("live")}
                >
                  实时
                </button>
                <button
                  type="button"
                  className={`demoToggleBtn${mode === "replay" ? " active" : ""}`}
                  onClick={() => switchMode("replay")}
                >
                  回放
                </button>
              </div>

              {mode === "live" ? (
                <>
                  <button className="btn primary" onClick={onRunLive} disabled={busy || liveDisabled}>
                    {cfgLoading ? "读取配置中…" : busy ? "运行中…" : "发起真实调用"}
                  </button>
                  {busy ? (
                    <button className="btn" onClick={onStop}>
                      中止观察
                    </button>
                  ) : null}
                  {transport ? (
                    <span className="pill mono">
                      传输：
                      {transport === "sse"
                        ? "SSE 实时流"
                        : transport === "poll"
                        ? "轮询兜底"
                        : transport === "mock"
                        ? "本地 MOCK"
                        : transport}
                    </span>
                  ) : null}
                </>
              ) : (
                <>
                  <select
                    className="input mono"
                    style={{ width: 360 }}
                    value={replayRunId}
                    onChange={(e) => setReplayRunId(e.target.value)}
                    disabled={busy}
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
                  <button className="btn primary" onClick={onReplay} disabled={busy || !replayRunId}>
                    {busy ? "回放中…" : "开始回放"}
                  </button>
                  {busy ? (
                    <button className="btn" onClick={onStop}>
                      停止回放
                    </button>
                  ) : null}
                  <span className="pill">倍速</span>
                  <div className="demoToggle">
                    {[1, 2, 4].map((sp) => (
                      <button
                        key={sp}
                        type="button"
                        className={`demoToggleBtn${replaySpeed === sp ? " active" : ""}`}
                        onClick={() => setReplaySpeed(sp)}
                      >
                        {sp}x
                      </button>
                    ))}
                  </div>
                  <button className="btn" onClick={loadRuns} disabled={busy}>
                    刷新列表
                  </button>
                </>
              )}
            </div>

            {mode === "replay" && autoReplayReason ? (
              <div className="demoNotice">自动进入回放：{autoReplayReason}</div>
            ) : null}
            {mode === "replay" && runsErr ? <div className="small err">历史列表读取失败：{runsErr}</div> : null}
            {mode === "live" && cfgLoading ? (
              <div className="demoNotice">正在读取 /api/demo/config，确认实时链路是否可用…</div>
            ) : null}
            {mode === "live" && liveDisabled && !cfgLoading ? (
              <div className="demoNotice">
                实时链路不可用{autoReplayReason ? `：${autoReplayReason}` : ""}，请切到回放模式。
              </div>
            ) : null}

            <details className="demoParams">
              <summary className="mono">演示消息参数（发送给 Cloud Run 入口的真实字段）</summary>
              <div className="demoParamGrid">
                <label className="demoParam">
                  <span className="demoEnvLabel">客户</span>
                  <input className="input mono" value={customer} onChange={(e) => setCustomer(e.target.value)} />
                </label>
                <label className="demoParam">
                  <span className="demoEnvLabel">PN（空格/逗号分隔）</span>
                  <input className="input mono" value={pnText} onChange={(e) => setPnText(e.target.value)} />
                </label>
                <label className="demoParam">
                  <span className="demoEnvLabel">产品线</span>
                  <input className="input mono" value={productLine} onChange={(e) => setProductLine(e.target.value)} />
                </label>
                <label className="demoParam">
                  <span className="demoEnvLabel">价格层级</span>
                  <input className="input mono" value={tier} onChange={(e) => setTier(e.target.value)} />
                </label>
                {scenario === "duplicate" ? (
                  <label className="demoParam demoParamWide">
                    <span className="demoEnvLabel">
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

            {err ? <div className="small err">{err}</div> : null}
          </div>
        </div>
      </div>

      <div className="card">
        <div className="cardHeader">
          <h2>链路视图</h2>
          <div className="row wrap">
            {mode === "replay" ? <span className="demoStateChip demoReplayChip">回放 · 非实时</span> : null}
            {run?.run_id ? <span className="pill mono">run {run.run_id}</span> : null}
            {run?.scenario ? <span className="pill">场景 {scenarioLabel(run.scenario)}</span> : null}
            {run?.source === "fixture" ? <span className="pill">内置快照</span> : null}
            {run?.placeholder ? <span className="demoStateChip st-duplicate">占位数据 · 非实测</span> : null}
            {finalMeta ? <span className={`demoStateChip st-${finalMeta.cls}`}>终态 {finalMeta.label}</span> : null}
            {run?.totalMs !== null && run?.totalMs !== undefined ? (
              <span className="pill mono">总耗时 {fmtMs(run.totalMs)}</span>
            ) : null}
          </div>
        </div>
        <div className="cardBody">
          <div className="demoPipeScroll">
            <div className="demoPipe">
              {bands.map((band, bi) => (
                <React.Fragment key={`${band.layer}-${bi}`}>
                  {bi > 0 ? <Connector /> : null}
                  <div className={`demoBand ${band.layer}`} style={{ flexGrow: band.steps.length }}>
                    <div className="demoBandLabel mono">
                      {band.layer === "cloud"
                        ? `GOOGLE CLOUD${cfg?.region ? ` · ${safeStr(cfg.region)}` : ""}`
                        : "内网 · r230"}
                    </div>
                    <div className="demoBandNodes">
                      {band.steps.map((s, si) => (
                        <React.Fragment key={s.key}>
                          {si > 0 ? <Connector small /> : null}
                          <PipelineNode step={s} selected={s.key === selectedKey} onSelect={setSelectedKey} />
                        </React.Fragment>
                      ))}
                    </div>
                  </div>
                </React.Fragment>
              ))}
            </div>
          </div>

          <div className="demoGhostStrip">
            <div className="demoGhostCaption mono">未接线通路 · 仅作架构说明，本演示中永不点亮</div>
            <div className="demoGhostRow">
              {GHOST_NODES.map((g) => (
                <div className="demoGhostNode" key={g.key}>
                  <div className="demoGhostTop">
                    <span className="demoGhostTitle mono">{g.title}</span>
                    <span className="demoGhostTag">{g.tag}</span>
                  </div>
                  <div className="demoGhostWhere">{g.where}</div>
                  <div className="demoGhostWhy">{g.why}</div>
                </div>
              ))}
            </div>
          </div>

          <div className="demoLegend">
            {Object.keys(STATE_META).map((k) => (
              <span className="demoLegendItem" key={k}>
                <span className={`demoLegendSwatch st-${STATE_META[k].cls}`} />
                {STATE_META[k].label}
              </span>
            ))}
          </div>
        </div>
      </div>

      <div className="card">
        <div className="cardHeader">
          <h2>{run?.placeholder ? "耗时瀑布（占位数据）" : "真实耗时瀑布"}</h2>
          <div className="small">
            {run?.placeholder
              ? "宽度按占位快照的 duration_ms 绘制，非实测耗时"
              : "宽度按各跳 duration_ms 真值绘制，无补间动画"}
          </div>
        </div>
        <div className="cardBody">
          <Waterfall steps={steps} />
        </div>
      </div>

      <div className="card">
        <div className="cardHeader">
          <h2>节点详情</h2>
          <div className="small">
            {selectedStep ? `${selectedStep.key} · ${selectedStep.layer === "cloud" ? "Google Cloud" : "内网"}` : "未选中"}
          </div>
        </div>
        <div className="cardBody">
          <StepDetail
            step={selectedStep}
            runId={run?.run_id}
            mode={USE_MOCK ? "mock" : mode}
            placeholder={!!run?.placeholder}
          />
        </div>
      </div>
    </div>
  );
}
