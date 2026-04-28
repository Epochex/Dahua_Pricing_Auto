import React, { useEffect, useMemo, useRef, useState } from "react";
import { apiGetJson, apiPostForm, apiPostJson, apiPutJson } from "./api.js";
import { formatPricePiecewise, safeStr } from "./format.js";

/* =========================
 * Common UI helpers
 * ========================= */

function Badge({ status }) {
  const s = (status || "").toLowerCase();
  const cls =
    s === "done" || s === "ok"
      ? "ok"
      : s === "running" || s === "queued"
      ? "run"
      : "fail";
  return <span className={`badge ${cls}`}>{status || "unknown"}</span>;
}

function Card({ title, right, children }) {
  return (
    <div className="card">
      <div className="cardHeader">
        <h2>{title}</h2>
        {right ? <div className="small">{right}</div> : null}
      </div>
      <div className="cardBody">{children}</div>
    </div>
  );
}

function KV({ obj }) {
  if (!obj) return null;
  const keys = Object.keys(obj);
  return (
    <div className="kv">
      {keys.map((k) => (
        <React.Fragment key={k}>
          <div className="k">{k}</div>
          <div className="v">
            {typeof obj[k] === "object" ? JSON.stringify(obj[k]) : safeStr(obj[k])}
          </div>
        </React.Fragment>
      ))}
    </div>
  );
}

function Hr() {
  return <div className="hr" />;
}

/* =========================
 * Query source / rendering logic
 * ========================= */

const PRICE_KEYS = [
  "FOB C(EUR)",
  "DDP A(EUR)",
  "Suggested Reseller(EUR)",
  "Gold(EUR)",
  "Silver(EUR)",
  "Ivory(EUR)",
  "MSRP(EUR)",
];

function getPriceSourceLabel(field, resp) {
  const meta = resp?.meta || {};
  const calculated = new Set(resp?.calculated_fields || []);
  const fv = resp?.final_values || {};
  const hasValue = fv[field] !== null && fv[field] !== undefined && String(fv[field]) !== "";

  if (!hasValue) return "";

  if (calculated.has(field)) {
    if (meta.used_sys) return "Calculated(Sys)";
    return "Calculated";
  }

  if (!meta.used_sys) return "Original(FR)";

  if (meta.used_sys && !calculated.has(field)) {
    if (meta.used_fr_fallback === true) return "Original(FR-fallback)";
    return "Original(Sys)";
  }

  return "";
}

function renderFieldValue(v) {
  if (v === null || v === undefined) return "";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function normalizeManualPriceInput(v) {
  const s = String(v ?? "").trim();
  if (!s) return null;
  const n = Number(s);
  if (!Number.isFinite(n) || n <= 0) {
    throw new Error("手动输入价格必须是大于 0 的数字");
  }
  return n;
}

function formatMatchMode(mode, matchedPn) {
  const m = String(mode || "").toLowerCase();
  const hit = safeStr(matchedPn);
  if (m === "exact") {
    return hit ? `精准匹配（${hit}）` : "精准匹配";
  }
  if (m === "base") {
    return hit ? `回退查找（去国际化后缀，命中 ${hit}）` : "回退查找（去国际化后缀）";
  }
  if (m === "none") return "未匹配";
  return safeStr(mode);
}

function parseFilenameFromContentDisposition(value) {
  const s = String(value || "");
  const utf8 = s.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8 && utf8[1]) {
    try {
      return decodeURIComponent(utf8[1]);
    } catch {
      return utf8[1];
    }
  }
  const basic = s.match(/filename=\"?([^\";]+)\"?/i);
  return basic && basic[1] ? basic[1] : "";
}

function formatMetaTime(iso, epoch) {
  let d = null;
  if (iso) {
    const t = new Date(iso);
    if (!Number.isNaN(t.getTime())) d = t;
  }
  if (!d && epoch !== null && epoch !== undefined) {
    const n = Number(epoch);
    if (Number.isFinite(n)) {
      const t = new Date(n * 1000);
      if (!Number.isNaN(t.getTime())) d = t;
    }
  }
  if (!d) return "-";
  const p2 = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}.${p2(d.getMonth() + 1)}.${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}`;
}

function QueryPriceTable({ resp }) {
  const fv = resp?.final_values || {};
  const calculated = new Set(resp?.calculated_fields || []);
  const meta = resp?.meta || {};
  const sysSalesType = safeStr(meta?.sys_sales_type) || "-";
  const sysBasisField = safeStr(meta?.sys_basis_field) || "-";
  const usedBasisRaw = meta?.sys_basis_price_used;
  const selectedBasisRaw = meta?.sys_basis_price;
  const hasUsedBasis = usedBasisRaw !== null && usedBasisRaw !== undefined && String(usedBasisRaw) !== "";
  const hasSelectedBasis =
    selectedBasisRaw !== null && selectedBasisRaw !== undefined && String(selectedBasisRaw) !== "";
  const calcLayerValue =
    sysBasisField && sysBasisField !== "-" ? `${sysSalesType} (${sysBasisField})` : sysSalesType;
  const basisValue = hasUsedBasis
    ? formatPricePiecewise(usedBasisRaw)
    : hasSelectedBasis
      ? `${formatPricePiecewise(selectedBasisRaw)} (not used)`
      : "-";
  const basisRaw = hasUsedBasis
    ? safeStr(usedBasisRaw)
    : hasSelectedBasis
      ? `${safeStr(selectedBasisRaw)} [${sysBasisField}]`
      : "-";

  return (
    <table className="table">
      <thead>
        <tr>
          <th style={{ width: 300 }}>Field</th>
          <th style={{ width: 220 }}>Value</th>
          <th>Raw</th>
        </tr>
      </thead>
      <tbody>
        {PRICE_KEYS.map((k) => {
          const raw = fv[k];
          const display = formatPricePiecewise(raw);
          const source = getPriceSourceLabel(k, resp);
          return (
            <tr key={k}>
              <td className="mono">{k}</td>
              <td className="priceCell">
                <span className="priceVal">{display}</span>
                {source ? (
                  <span className={`inlineTag ${calculated.has(k) ? "calc" : "orig"}`}>
                    {source}
                  </span>
                ) : null}
              </td>
              <td className="mono">{safeStr(raw)}</td>
            </tr>
          );
        })}
        <tr key="__calc_layer__">
          <td className="mono">Calc Layer (Sys)</td>
          <td className="priceCell">
            <span className="priceVal">{calcLayerValue}</span>
          </td>
          <td className="mono">{`${sysSalesType} / ${sysBasisField}`}</td>
        </tr>
        <tr key="__sys_basis_price_used__">
          <td className="mono">Sys Basis Price Used</td>
          <td className="priceCell">
            <span className="priceVal">{basisValue}</span>
            <span className={`inlineTag ${hasUsedBasis ? "calc" : "orig"}`}>
              {hasUsedBasis ? "Used" : "Not Used"}
            </span>
          </td>
          <td className="mono">{basisRaw}</td>
        </tr>
      </tbody>
    </table>
  );
}

function QueryInfoTable({ resp }) {
  const fv = resp?.final_values || {};
  const fields = [
    "Part No.",
    "Series",
    "External Model",
    "Internal Model",
    "Sales Status",
    "Description",
  ];

  return (
    <table className="table">
      <thead>
        <tr>
          <th style={{ width: 260 }}>Field</th>
          <th>Value</th>
        </tr>
      </thead>
      <tbody>
        {fields.map((k) => (
          <tr key={k}>
            <td className="mono">{k}</td>
            <td>{renderFieldValue(fv[k])}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function QueryDiagnosticBlock({ resp }) {
  if (!resp) return null;
  const meta = resp?.meta || {};
  const statusOk = String(resp?.status || "").toLowerCase() === "ok";
  const manualOverride = Boolean(meta?.manual_override);
  const frMatchText = formatMatchMode(meta.fr_match_mode, meta.fr_matched_pn);
  const sysMatchText = formatMatchMode(meta.sys_match_mode, meta.sys_matched_pn);

  const calcStatusText = statusOk
    ? `自动化计算成功（产品线：${safeStr(meta.category)} / 子线：${safeStr(
        meta.series_key
      )} | 系列：${safeStr(meta.series_display)} | 定价公式：${safeStr(meta.pricing_rule_name)}）`
    : safeStr(resp?.status);

  const calcLevelText = `${safeStr(meta.sys_sales_type)}（Sys 基准字段：${safeStr(
    meta.sys_basis_field
  )} | Sys adjust key：${safeStr(meta.sys_uplift_key)} | 关键词叠加：${safeStr(
    meta.sys_keyword_uplift_hits
  )} (+${safeStr(meta.sys_keyword_uplift_pct)}) | 定价公式：${safeStr(meta.pricing_rule_name)}）`;

  return (
    <div className="diagBlock">
      <div className="diagHeader">
        <div className="diagTitle">QUERY SUMMARY</div>
        <Badge status={resp.status} />
      </div>

      <div className="diagGrid">

        <div className="diagItem">
          <div className="diagLabel">法国国家侧匹配</div>
          <div className="diagValue">{frMatchText}</div>
        </div>

        <div className="diagItem">
          <div className="diagLabel">系统侧匹配</div>
          <div className="diagValue">{sysMatchText}</div>
        </div>

        <div className="diagItem diagSpan2">
          <div className="diagLabel">display rule</div>
          <div className="diagValue">
            <span className="bigPill monoInline">&lt;10 → 2 decimals</span>
            <span className="bigPill monoInline">≥10 → integer</span>
          </div>
        </div>
      </div>

      <div className="diagTextRow">
        <div className="diagTextLabel">计算状态</div>
        <div className="diagTextValue">
          <div>{calcStatusText}</div>
          <div className="diagInlineWarn">
            务必严格核对产品描述与识别产品线是否符合
          </div>
          <div className="small" style={{ marginTop: 6 }}>
            取价策略：国家侧优先；缺失或层级不全时，系统侧按底价补算。
          </div>
        </div>
      </div>

      <div className="diagTextRow">
        <div className="diagTextLabel">计算层级</div>
        <div className="diagTextValue">{calcLevelText}</div>
      </div>

      <div className="diagTextRow">
        <div className="diagTextLabel">重算模式</div>
        <div className="diagTextValue">
          {manualOverride ? (
            <>
              <span className="bigPill monoInline">
                MANUAL OVERRIDE · category={safeStr(meta?.forced_category)} · price_group={safeStr(
                  meta?.forced_price_group
                )} · series_key={safeStr(meta?.forced_series_key)}
              </span>
              {meta?.manual_override_field === "sys_basis_price_used" ? (
                <span className="bigPill monoInline">
                  manual Sys Basis Price Used={safeStr(
                    formatPricePiecewise(meta?.manual_sys_basis_price_input)
                  )}
                </span>
              ) : null}
              {meta?.manual_override_field === "fob" ? (
                <span className="bigPill monoInline">
                  manual FOB C(EUR)={safeStr(formatPricePiecewise(meta?.manual_fob_input))}
                </span>
              ) : null}
            </>
          ) : (
            <span className="bigPill monoInline">AUTO</span>
          )}
        </div>
      </div>
    </div>
  );
}

function ExternalModelClusterBlock({ cluster, loading, onExportAll, exporting }) {
  if (!loading && !cluster) return null;
  const rows = Array.isArray(cluster?.rows) ? cluster.rows : [];
  return (
    <div className="diagBlock">
      <div className="diagHeader">
        <div className="diagTitle">EXTERNAL MODEL INDEX</div>
        <span className="small monoInline">
          {loading
            ? "loading..."
            : `external_model=${safeStr(cluster?.external_model)} · rows=${safeStr(cluster?.count)} · anchor=${
                cluster?.anchor_applied ? `yes(${safeStr(cluster?.anchor_pn)})` : "no"
              } · changed=${safeStr(cluster?.anchor_changed_count)}`}
        </span>
      </div>

      <div className="row" style={{ marginBottom: 10 }}>
        <button className="btn" onClick={onExportAll} disabled={loading || exporting || rows.length === 0}>
          {exporting ? "EXPORTING..." : "EXPORT EXT ALL"}
        </button>
      </div>

      {loading ? (
        <div className="small">Loading same external model cluster...</div>
      ) : (
        <div className="tableWrap">
          <table className="table dense">
            <thead>
              <tr>
                <th style={{ minWidth: 220 }}>PN</th>
                <th style={{ minWidth: 260 }}>Internal Model</th>
                <th style={{ minWidth: 160 }}>Release Status</th>
                <th style={{ minWidth: 105 }}>FOB</th>
                <th style={{ minWidth: 105 }}>DDP</th>
                <th style={{ minWidth: 105 }}>Reseller</th>
                <th style={{ minWidth: 105 }}>Gold</th>
                <th style={{ minWidth: 105 }}>Silver</th>
                <th style={{ minWidth: 105 }}>Ivory</th>
                <th style={{ minWidth: 105 }}>MSRP</th>
                <th style={{ minWidth: 180 }}>Price Source</th>
                <th style={{ minWidth: 90 }}>Changed</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const fv = r?.final_values || {};
                const m = r?.meta || {};
                return (
                  <tr key={safeStr(r?.pn)}>
                    <td className="mono">{safeStr(r?.pn)}</td>
                    <td className="mono">{safeStr(fv["Internal Model"])}</td>
                    <td className="mono">{safeStr(fv["Sales Status"])}</td>
                    <td className="mono">{safeStr(formatPricePiecewise(fv["FOB C(EUR)"]))}</td>
                    <td className="mono">{safeStr(formatPricePiecewise(fv["DDP A(EUR)"]))}</td>
                    <td className="mono">{safeStr(formatPricePiecewise(fv["Suggested Reseller(EUR)"]))}</td>
                    <td className="mono">{safeStr(formatPricePiecewise(fv["Gold(EUR)"]))}</td>
                    <td className="mono">{safeStr(formatPricePiecewise(fv["Silver(EUR)"]))}</td>
                    <td className="mono">{safeStr(formatPricePiecewise(fv["Ivory(EUR)"]))}</td>
                    <td className="mono">{safeStr(formatPricePiecewise(fv["MSRP(EUR)"]))}</td>
                    <td className="mono">
                      {m?.external_model_anchor_applied
                        ? `FR anchor ${safeStr(m?.external_model_anchor_pn)}`
                        : "normal"}
                    </td>
                    <td className="mono">{m?.external_model_anchor_changed ? "yes" : "no"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function SingleQuery() {
  const [pn, setPn] = useState("");
  const [loading, setLoading] = useState(false);
  const [recalcLoading, setRecalcLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [extMode, setExtMode] = useState(false);
  const [extLoading, setExtLoading] = useState(false);
  const [extExporting, setExtExporting] = useState(false);
  const [extResp, setExtResp] = useState(null);
  const [resp, setResp] = useState(null);
  const [err, setErr] = useState("");
  const [optionsErr, setOptionsErr] = useState("");
  const [optionsLoading, setOptionsLoading] = useState(false);
  const [queryOptions, setQueryOptions] = useState({
    categories: [],
    price_groups: [],
    category_price_groups: {},
    group_rule_keys: {},
  });
  const [manualCategory, setManualCategory] = useState("");
  const [manualPriceGroup, setManualPriceGroup] = useState("");
  const [manualSeriesKey, setManualSeriesKey] = useState("_default_");
  const [manualSysBasisPriceUsed, setManualSysBasisPriceUsed] = useState("");
  const [manualFob, setManualFob] = useState("");

  async function loadQueryOptions({ silent = false } = {}) {
    if (!silent) setOptionsErr("");
    setOptionsLoading(true);
    let lastErr = "";
    for (let i = 0; i < 3; i += 1) {
      try {
        const r = await apiGetJson("/api/query/options");
        setQueryOptions({
          categories: Array.isArray(r?.categories) ? r.categories : [],
          price_groups: Array.isArray(r?.price_groups) ? r.price_groups : [],
          category_price_groups:
            r?.category_price_groups && typeof r.category_price_groups === "object"
              ? r.category_price_groups
              : {},
          group_rule_keys:
            r?.group_rule_keys && typeof r.group_rule_keys === "object" ? r.group_rule_keys : {},
        });
        setOptionsErr("");
        setOptionsLoading(false);
        return true;
      } catch (e) {
        lastErr = String(e.message || e);
        await new Promise((resolve) => setTimeout(resolve, 250 * (i + 1)));
      }
    }
    if (!silent) setOptionsErr(lastErr);
    setOptionsLoading(false);
    return false;
  }

  useEffect(() => {
    void loadQueryOptions({ silent: false });
  }, []);

  const categoryOptions = useMemo(() => {
    const set = new Set([
      ...(Array.isArray(queryOptions.categories) ? queryOptions.categories : []),
      safeStr(resp?.meta?.category),
    ]);
    return Array.from(set).filter((x) => x).sort();
  }, [queryOptions, resp]);

  const priceGroupOptions = useMemo(() => {
    const set = new Set([
      ...(Array.isArray(queryOptions.price_groups) ? queryOptions.price_groups : []),
      safeStr(resp?.meta?.price_group),
    ]);
    return Array.from(set).filter((x) => x).sort();
  }, [queryOptions, resp]);

  const categoryPriceGroups = useMemo(() => {
    const map = queryOptions?.category_price_groups || {};
    const arr = Array.isArray(map?.[manualCategory]) ? map[manualCategory] : [];
    if (arr.length > 0) return arr;
    if (manualCategory && priceGroupOptions.includes(manualCategory)) return [manualCategory];
    return manualPriceGroup ? [manualPriceGroup] : [];
  }, [queryOptions, manualCategory, manualPriceGroup, priceGroupOptions]);

  const seriesKeyOptions = useMemo(() => {
    const map = queryOptions?.group_rule_keys || {};
    const arr = Array.isArray(map?.[manualPriceGroup]) ? map[manualPriceGroup] : [];
    if (arr.length === 0) return ["_default_"];
    return arr.includes("_default_") ? arr : ["_default_", ...arr];
  }, [queryOptions, manualPriceGroup]);

  useEffect(() => {
    if (!manualCategory) return;
    if (categoryPriceGroups.length === 0) return;
    if (!categoryPriceGroups.includes(manualPriceGroup)) {
      setManualPriceGroup(categoryPriceGroups[0]);
    }
  }, [manualCategory, manualPriceGroup, categoryPriceGroups]);

  useEffect(() => {
    if (seriesKeyOptions.length === 0) return;
    if (!seriesKeyOptions.includes(manualSeriesKey)) {
      setManualSeriesKey(seriesKeyOptions[0]);
    }
  }, [seriesKeyOptions, manualSeriesKey]);

  function syncManualInputsFromResponse(r) {
    const meta = r?.meta || {};
    if (meta?.manual_override_field === "sys_basis_price_used") {
      setManualSysBasisPriceUsed(safeStr(meta?.manual_sys_basis_price_input));
      setManualFob("");
      return;
    }
    if (meta?.manual_override_field === "fob") {
      setManualFob(safeStr(meta?.manual_fob_input));
      setManualSysBasisPriceUsed("");
      return;
    }
    setManualSysBasisPriceUsed("");
    setManualFob("");
  }

  async function run() {
    setErr("");
    setResp(null);
    setExtResp(null);

    const s = pn.trim();
    if (!s) return;

    setLoading(true);
    try {
      const r = await apiPostJson("/api/query", { pn: s });
      setResp(r);
      const nextCategory = safeStr(r?.meta?.category);
      const nextPriceGroup = safeStr(r?.meta?.price_group);
      const nextSeriesKey = safeStr(r?.meta?.series_key) || "_default_";
      setManualCategory(nextCategory);
      setManualPriceGroup(nextPriceGroup);
      setManualSeriesKey(nextSeriesKey);
      syncManualInputsFromResponse(r);
      void loadQueryOptions({ silent: true });
      if (extMode) {
        setExtLoading(true);
        try {
          const ext = await apiPostJson("/api/query/external-model-index", {
            pn: s,
            apply_france_anchor: true,
          });
          setExtResp(ext);
        } finally {
          setExtLoading(false);
        }
      }
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }

  async function exportExternalModelAll() {
    const s = pn.trim();
    if (!s) return;
    setErr("");
    setExtExporting(true);
    try {
      const r = await fetch("/api/query/external-model-export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pn: s, apply_france_anchor: true }),
      });
      if (!r.ok) {
        const txt = await r.text().catch(() => "");
        throw new Error(`POST /api/query/external-model-export -> ${r.status} ${txt}`);
      }
      const blob = await r.blob();
      const cd = r.headers.get("Content-Disposition") || r.headers.get("content-disposition");
      const fromServer = parseFilenameFromContentDisposition(cd);
      const fallback = `${s.replace(/[^a-zA-Z0-9._-]+/g, "_")}_external_model_all.xlsx`;
      const filename = fromServer || fallback;
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setExtExporting(false);
    }
  }

  async function recalc() {
    if (!resp) return;
    const s = (pn || safeStr(resp?.pn)).trim();
    if (!s) return;

    if (!manualCategory) {
      setErr("请先选择产品线大类");
      return;
    }

    let nextManualBasis = null;
    let nextManualFob = null;
    try {
      nextManualBasis = normalizeManualPriceInput(manualSysBasisPriceUsed);
      nextManualFob = normalizeManualPriceInput(manualFob);
    } catch (e) {
      setErr(String(e.message || e));
      return;
    }
    if (nextManualBasis !== null && nextManualFob !== null) {
      setErr("Sys Basis Price Used 和 FOB C(EUR) 只能同时修改其中一个");
      return;
    }

    setErr("");
    setRecalcLoading(true);
    try {
      const r = await apiPostJson("/api/query/recompute", {
        pn: s,
        force_category: manualCategory || null,
        force_price_group: manualPriceGroup || null,
        force_series_key: manualSeriesKey || null,
        manual_sys_basis_price_used: nextManualBasis,
        manual_fob: nextManualFob,
      });
      setResp(r);
      syncManualInputsFromResponse(r);
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setRecalcLoading(false);
    }
  }

  async function exportOne() {
    if (!resp) return;
    if (extMode) {
      await exportExternalModelAll();
      return;
    }

    const s = (pn || safeStr(resp?.pn)).trim();
    if (!s) return;

    const meta = resp?.meta || {};
    const manualOverride = Boolean(meta?.manual_override);

    const payload = {
      pn: s,
      force_category: null,
      force_price_group: null,
      force_series_key: null,
      force_full_recalc: false,
    };

    if (manualOverride) {
      payload.force_category = safeStr(meta?.forced_category) || null;
      payload.force_price_group = safeStr(meta?.forced_price_group) || null;
      payload.force_series_key = safeStr(meta?.forced_series_key) || null;
      payload.force_full_recalc = Boolean(meta?.force_full_recalc);
      if (meta?.manual_override_field === "sys_basis_price_used" && meta?.manual_sys_basis_price_input != null) {
        payload.manual_sys_basis_price_used = meta.manual_sys_basis_price_input;
      }
      if (meta?.manual_override_field === "fob" && meta?.manual_fob_input != null) {
        payload.manual_fob = meta.manual_fob_input;
      }
    }

    setErr("");
    setExporting(true);
    try {
      const r = await fetch("/api/query/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!r.ok) {
        const txt = await r.text().catch(() => "");
        throw new Error(`POST /api/query/export -> ${r.status} ${txt}`);
      }
      const blob = await r.blob();
      const cd = r.headers.get("Content-Disposition") || r.headers.get("content-disposition");
      const fromServer = parseFilenameFromContentDisposition(cd);
      const fallback = `${s.replace(/[^a-zA-Z0-9._-]+/g, "_")}_Country_import_upload_Model.xlsx`;
      const filename = fromServer || fallback;

      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setExporting(false);
    }
  }

  return (
    <Card
      title="SINGLE QUERY"
      right={
        <div className="row">
          <span className="small monoInline">POST /api/query</span>
          {resp ? <Badge status={resp.status} /> : null}
        </div>
      }
    >
      <div className="row">
        <input
          className="input mono"
          value={pn}
          onChange={(e) => setPn(e.target.value)}
          placeholder="Part No. (e.g. 2.3.01.01.10597)"
          onKeyDown={(e) => {
            if (e.key === "Enter") run();
          }}
        />
        <button className="btn primary" onClick={run} disabled={loading}>
          {loading ? "RUNNING..." : "RUN"}
        </button>
        <button className="btn" onClick={exportOne} disabled={!resp || exporting || extExporting || loading}>
          {exporting || extExporting ? "EXPORTING..." : extMode ? "EXPORT EXT ALL" : "EXPORT"}
        </button>
        <label className="small" style={{ marginLeft: 8, display: "flex", alignItems: "center", gap: 6 }}>
          <input
            type="checkbox"
            checked={extMode}
            onChange={(e) => {
              const checked = Boolean(e.target.checked);
              setExtMode(checked);
              if (!checked) setExtResp(null);
            }}
          />
          扩展索引模式（External Model）
        </label>
      </div>

      {err ? (
        <>
          <Hr />
          <div className="small err">{err}</div>
        </>
      ) : null}

      {resp ? (
        <>
          <Hr />

          <QueryDiagnosticBlock resp={resp} />

          <Hr />
          <div className="sectionTitle">BASE FIELDS</div>
          <QueryInfoTable resp={resp} />

          <Hr />
          <div className="sectionTitle">PRICE FIELDS</div>
          <QueryPriceTable resp={resp} />

          {extMode ? (
            <>
              <Hr />
              <div className="sectionTitle">EXTERNAL MODEL CLUSTER</div>
              <ExternalModelClusterBlock
                cluster={extResp}
                loading={extLoading}
                onExportAll={exportExternalModelAll}
                exporting={extExporting}
              />
            </>
          ) : null}

          <Hr />
          <div className="sectionTitle">MANUAL RECALCULATE</div>
          <div className="diagBlock">
            <div className="diagHeader">
              <div className="diagTitle">RE-CALCULATE WITH MANUAL PRODUCT LINE</div>
              <div className="row">
                <span className="small monoInline">POST /api/query/recompute</span>
                <button className="btn" onClick={() => void loadQueryOptions({ silent: false })} disabled={optionsLoading}>
                  {optionsLoading ? "LOADING OPTIONS..." : "RELOAD OPTIONS"}
                </button>
              </div>
            </div>

            <div className="diagTextRow">
              <div className="diagTextLabel">当前自动识别</div>
              <div className="diagTextValue">
                <span className="bigPill monoInline">category: {safeStr(resp?.meta?.category)}</span>
                <span className="bigPill monoInline">price_group: {safeStr(resp?.meta?.price_group)}</span>
                <span className="bigPill monoInline">series_key: {safeStr(resp?.meta?.series_key)}</span>
              </div>
            </div>

            <div className="diagTextRow">
              <div className="diagTextLabel">手动重算</div>
              <div className="diagTextValue">
                <div className="row wrap">
                  <select
                    className="input mono"
                    style={{ maxWidth: 320 }}
                    value={manualCategory}
                    onChange={(e) => setManualCategory(e.target.value)}
                  >
                    <option value="">(选择产品线大类)</option>
                    {categoryOptions.map((c) => (
                      <option key={c} value={c}>
                        {c}
                      </option>
                    ))}
                  </select>

                  <select
                    className="input mono"
                    style={{ maxWidth: 360 }}
                    value={manualSeriesKey}
                    onChange={(e) => setManualSeriesKey(e.target.value)}
                  >
                    {seriesKeyOptions.map((k) => (
                      <option key={k} value={k}>
                        {k}
                      </option>
                    ))}
                  </select>

                  <button
                    className="btn primary"
                    onClick={recalc}
                    disabled={recalcLoading}
                  >
                    {recalcLoading ? "RECALCULATING..." : "RECALCULATE"}
                  </button>
                </div>

                <div className="small" style={{ marginTop: 8 }}>
                  逻辑：先选产品线大类，再选子产品线（无细分时使用 `_default_`），再全量重算。
                </div>
                <div className="small monoInline" style={{ marginTop: 6 }}>
                  price_group(auto): {safeStr(manualPriceGroup || "(auto unresolved)")}
                </div>

                <div className="row wrap" style={{ marginTop: 10 }}>
                  <input
                    className="input mono"
                    style={{ maxWidth: 240 }}
                    type="number"
                    step="0.01"
                    value={manualSysBasisPriceUsed}
                    onChange={(e) => setManualSysBasisPriceUsed(e.target.value)}
                    placeholder={`Sys Basis Price Used（当前 ${safeStr(
                      formatPricePiecewise(resp?.meta?.sys_basis_price_used ?? resp?.meta?.sys_basis_price)
                    )}）`}
                  />
                  <input
                    className="input mono"
                    style={{ maxWidth: 220 }}
                    type="number"
                    step="0.01"
                    value={manualFob}
                    onChange={(e) => setManualFob(e.target.value)}
                    placeholder={`FOB C(EUR)（当前 ${safeStr(
                      formatPricePiecewise(resp?.final_values?.["FOB C(EUR)"])
                    )}）`}
                  />
                </div>

                <div className="small" style={{ marginTop: 6 }}>
                  价格手动项只允许填写一个。改 `Sys Basis Price Used` 时会按当前产品线公式自动重算 `FOB`；
                  改 `FOB C(EUR)` 时只重算其余价格层级，不反推 `Sys Basis Price Used`。
                </div>

                {categoryPriceGroups.length > 1 ? (
                  <div className="row wrap" style={{ marginTop: 8 }}>
                    <span className="small monoInline">高级：切换 price_group</span>
                    <select
                      className="input mono"
                      style={{ maxWidth: 360 }}
                      value={manualPriceGroup}
                      onChange={(e) => setManualPriceGroup(e.target.value)}
                    >
                      {categoryPriceGroups.map((g) => (
                        <option key={g} value={g}>
                          {g}
                        </option>
                      ))}
                    </select>
                  </div>
                ) : null}

                {optionsErr ? <div className="small err">{optionsErr}</div> : null}
              </div>
            </div>
          </div>

          {/* <Hr />
          <div className="sectionTitle">RAW DEBUG</div>
          <KV obj={resp.meta ? { meta: resp.meta, calculated_fields: resp.calculated_fields } : resp} /> */}
        </>
      ) : null}
    </Card>
  );
}

function ModelSearchPage() {
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [resp, setResp] = useState(null);

  async function runSearch() {
    const s = query.trim();
    if (!s) {
      setErr("请输入内部或外部型号关键字");
      return;
    }
    setErr("");
    setLoading(true);
    try {
      const r = await apiPostJson("/api/models/search", {
        query: s,
        limit: 100,
      });
      setResp(r);
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }

  const items = Array.isArray(resp?.items) ? resp.items : [];

  return (
    <Card
      title="MODEL SEARCH"
      right={<span className="small monoInline">POST /api/models/search</span>}
    >
      <div className="row wrap">
        <input
          className="input mono"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Internal / External Model (supports exact, prefix, contains)"
          onKeyDown={(e) => {
            if (e.key === "Enter") void runSearch();
          }}
        />
        <button className="btn primary" onClick={() => void runSearch()} disabled={loading}>
          {loading ? "SEARCHING..." : "SEARCH"}
        </button>
      </div>

      {err ? (
        <>
          <Hr />
          <div className="small err">{err}</div>
        </>
      ) : null}

      {resp ? (
        <>
          <Hr />
          <div className="diagBlock">
            <div className="diagHeader">
              <div className="diagTitle">MATCH SUMMARY</div>
              <span className="small monoInline">
                query={safeStr(resp?.query)} · shown={safeStr(resp?.count)} · total_hits={safeStr(resp?.total_hits)}
              </span>
            </div>
            <div className="small">
              排序优先级：`exact` → `prefix` → `contains`。价格信息为当前平台自动计算结果。
            </div>
          </div>

          <Hr />
          <div className="tableWrap">
            <table className="table dense">
              <thead>
                <tr>
                  <th style={{ minWidth: 220 }}>PN</th>
                  <th style={{ minWidth: 220 }}>Internal Model</th>
                  <th style={{ minWidth: 220 }}>External Model</th>
                  <th style={{ minWidth: 110 }}>Match</th>
                  <th style={{ minWidth: 160 }}>Matched Fields</th>
                  <th style={{ minWidth: 120 }}>Source</th>
                  <th style={{ minWidth: 100 }}>FOB</th>
                  <th style={{ minWidth: 100 }}>DDP</th>
                  <th style={{ minWidth: 110 }}>Reseller</th>
                  <th style={{ minWidth: 100 }}>Gold</th>
                  <th style={{ minWidth: 100 }}>Silver</th>
                  <th style={{ minWidth: 100 }}>Ivory</th>
                  <th style={{ minWidth: 100 }}>MSRP</th>
                </tr>
              </thead>
              <tbody>
                {items.length === 0 ? (
                  <tr>
                    <td colSpan={13} className="small">
                      no matched devices
                    </td>
                  </tr>
                ) : (
                  items.map((item) => (
                    <tr key={safeStr(item?.pn)}>
                      <td className="mono">{safeStr(item?.pn)}</td>
                      <td className="mono">{safeStr(item?.internal_model || item?.internal_model_raw)}</td>
                      <td className="mono">{safeStr(item?.external_model || item?.external_model_raw)}</td>
                      <td className="mono">{safeStr(item?.match_type)}</td>
                      <td className="mono">{safeStr((item?.matched_fields || []).join(", "))}</td>
                      <td className="mono">{safeStr((item?.sources || []).join(", "))}</td>
                      <td className="mono">{safeStr(formatPricePiecewise(item?.fob))}</td>
                      <td className="mono">{safeStr(formatPricePiecewise(item?.ddp))}</td>
                      <td className="mono">{safeStr(formatPricePiecewise(item?.reseller))}</td>
                      <td className="mono">{safeStr(formatPricePiecewise(item?.gold))}</td>
                      <td className="mono">{safeStr(formatPricePiecewise(item?.silver))}</td>
                      <td className="mono">{safeStr(formatPricePiecewise(item?.ivory))}</td>
                      <td className="mono">{safeStr(formatPricePiecewise(item?.msrp))}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </>
      ) : null}
    </Card>
  );
}

/* =========================
 * Batch Export
 * ========================= */

function BatchJobBlock({ job }) {
  if (!job) return null;

  const status = safeStr(job.status);
  const report = job.report || {};
  const outputFiles = Array.isArray(job.output_files) ? job.output_files : [];
  const total = Number(job.progress_total ?? report.count_total ?? 0) || 0;
  const done = Number(job.progress_done ?? 0) || 0;
  const pctRaw = Number(job.progress_percent ?? (total > 0 ? (done * 100) / total : 0));
  const pct = Number.isFinite(pctRaw) ? Math.max(0, Math.min(100, pctRaw)) : 0;

  return (
    <div className="diagBlock">
      <div className="diagHeader">
        <div className="diagTitle">BATCH JOB SUMMARY</div>
        <Badge status={status} />
      </div>

      <div className="diagGrid">
        <div className="diagItem">
          <div className="diagLabel">job_id</div>
          <div className="diagValue mono">{safeStr(job.job_id)}</div>
        </div>

        <div className="diagItem">
          <div className="diagLabel">status</div>
          <div className="diagValue">{status}</div>
        </div>

        <div className="diagItem">
          <div className="diagLabel">level_input</div>
          <div className="diagValue">{safeStr(job.level_input || job.level)}</div>
        </div>

        <div className="diagItem">
          <div className="diagLabel">export_layout</div>
          <div className="diagValue">{safeStr(job.export_layout)}</div>
        </div>

        <div className="diagItem diagSpan2">
          <div className="diagLabel">input file</div>
          <div className="diagValue mono">{safeStr(job.input_name)}</div>
        </div>
      </div>

      <div className="diagTextRow">
        <div className="diagTextLabel">统计</div>
        <div className="diagTextValue">
          <span className="bigPill monoInline">total: {safeStr(report.count_total)}</span>
          <span className="bigPill monoInline">not_found: {safeStr(report.count_not_found)}</span>
          <span className="bigPill monoInline">anchor_applied: {safeStr(report.count_anchor_applied)}</span>
          <span className="bigPill monoInline">anchor_changed: {safeStr(report.count_anchor_changed)}</span>
          <span className="bigPill monoInline">outputs: {outputFiles.length}</span>
        </div>
      </div>

      <div className="diagTextRow">
        <div className="diagTextLabel">实时进度</div>
        <div className="diagTextValue">
          <div className="progressTrack">
            <div className="progressFill" style={{ width: `${pct}%` }} />
          </div>
          <div className="small monoInline" style={{ marginTop: 6 }}>
            {safeStr(done)} / {safeStr(total)} ({pct.toFixed(1)}%) · current_pn=
            {safeStr(job.progress_current_pn || "-")}
          </div>
        </div>
      </div>

      {job.error ? (
        <div className="diagTextRow">
          <div className="diagTextLabel">error</div>
          <div className="diagTextValue mono err">{safeStr(job.error)}</div>
        </div>
      ) : null}
    </div>
  );
}

function BatchReviewTable({ job }) {
  const items = Array.isArray(job?.report?.items) ? job.report.items : [];
  if (items.length === 0) return null;

  return (
    <div className="diagBlock">
      <div className="diagHeader">
        <div className="diagTitle">BATCH REVIEW ROWS</div>
        <span className="small monoInline">rows={items.length}</span>
      </div>
      <div className="tableWrap">
        <table className="table dense">
          <thead>
            <tr>
              <th style={{ minWidth: 56 }}>#</th>
              <th style={{ minWidth: 220 }}>PN</th>
              <th style={{ minWidth: 90 }}>status</th>
              <th style={{ minWidth: 160 }}>category</th>
              <th style={{ minWidth: 260 }}>Internal Model Info</th>
              <th style={{ minWidth: 220 }}>series/rule</th>
              <th style={{ minWidth: 180 }}>match</th>
              <th style={{ minWidth: 180 }}>Price Source</th>
              <th style={{ minWidth: 100 }}>FOB</th>
              <th style={{ minWidth: 100 }}>DDP</th>
              <th style={{ minWidth: 100 }}>Reseller</th>
              <th style={{ minWidth: 100 }}>Gold</th>
              <th style={{ minWidth: 100 }}>Silver</th>
              <th style={{ minWidth: 100 }}>Ivory</th>
              <th style={{ minWidth: 100 }}>MSRP</th>
              <th style={{ minWidth: 260 }}>warnings</th>
            </tr>
          </thead>
          <tbody>
            {items.map((r) => (
              <tr key={`${safeStr(r.idx)}_${safeStr(r.pn)}`}>
                <td className="mono">{safeStr(r.idx)}</td>
                <td className="mono">{safeStr(r.pn)}</td>
                <td className="mono">{safeStr(r.status)}</td>
                <td className="mono">{safeStr(r.category)}</td>
                <td className="mono">
                  {safeStr(r.internal_model)}
                  <br />
                  <span className="small">external: {safeStr(r.external_model)}</span>
                </td>
                <td className="mono">
                  {safeStr(r.series_display)}
                  <br />
                  <span className="small">{safeStr(r.pricing_rule_name)}</span>
                </td>
                <td className="mono">
                  FR {safeStr(r.fr_match_mode)} ({safeStr(r.fr_matched_pn)})
                  <br />
                  SYS {safeStr(r.sys_match_mode)} ({safeStr(r.sys_matched_pn)})
                </td>
                <td className="mono">{safeStr(r.price_source)}</td>
                <td className="mono">{safeStr(formatPricePiecewise(r.fob))}</td>
                <td className="mono">{safeStr(formatPricePiecewise(r.ddp))}</td>
                <td className="mono">{safeStr(formatPricePiecewise(r.reseller))}</td>
                <td className="mono">{safeStr(formatPricePiecewise(r.gold))}</td>
                <td className="mono">{safeStr(formatPricePiecewise(r.silver))}</td>
                <td className="mono">{safeStr(formatPricePiecewise(r.ivory))}</td>
                <td className="mono">{safeStr(formatPricePiecewise(r.msrp))}</td>
                <td className="mono">{safeStr((r.warnings || []).join(" | "))}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function BatchExport() {
  const [file, setFile] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState("");
  const [jobId, setJobId] = useState("");
  const [job, setJob] = useState(null);

  // 新增：后端 /api/batch 要求必填 level
  const [level, setLevel] = useState("country");

  async function submit() {
    if (!file) return;
    setErr("");
    setSubmitting(true);
    setJob(null);

    try {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("level", level); // 修复 422: missing body.level

      const r = await apiPostForm("/api/batch", fd);
      setJob(r);
      if (r?.job_id) {
        const id = String(r.job_id);
        setJobId(id);
        await refresh(id);
      }
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setSubmitting(false);
    }
  }

  async function refresh(idParam) {
    const id = String(idParam || jobId || "").trim();
    if (!id) return;
    setErr("");
    try {
      const r = await apiGetJson(`/api/jobs/${encodeURIComponent(id)}`);
      setJob(r);
      if (!jobId) setJobId(id);
    } catch (e) {
      setErr(String(e.message || e));
    }
  }

  useEffect(() => {
    const st = String(job?.status || "").toLowerCase();
    const shouldPoll = Boolean(jobId) && (!job || st === "queued" || st === "running");
    if (!shouldPoll) return undefined;
    const tid = setInterval(() => {
      refresh(jobId);
    }, 1200);
    return () => clearInterval(tid);
  }, [jobId, job]);

  const downloadUrl = jobId ? `/api/jobs/${encodeURIComponent(jobId)}/download` : "#";
  const canDownload =
    job &&
    (String(job.status || "").toLowerCase() === "done" ||
      String(job.status || "").toLowerCase() === "ok");

  return (
    <Card
      title="BATCH EXPORT"
      right={<span className="small monoInline">async upload → poll → download xlsx</span>}
    >
      <div className="row wrap">
        <input
          className="input"
          type="file"
          accept=".txt,.csv,.xlsx,.xls"
          onChange={(e) => setFile(e.target.files?.[0] || null)}
        />

        <select
          className="input mono"
          style={{ maxWidth: 260 }}
          value={level}
          onChange={(e) => setLevel(e.target.value)}
        >
          <option value="country">country</option>
          <option value="country_customer">country_customer</option>
        </select>

        <button className="btn primary" onClick={submit} disabled={!file || submitting}>
          {submitting ? "UPLOADING..." : "SUBMIT"}
        </button>

        <input
          className="input mono"
          style={{ maxWidth: 260 }}
          value={jobId}
          onChange={(e) => setJobId(e.target.value)}
          placeholder="job_id"
        />

        <button className="btn" onClick={() => refresh(jobId)} disabled={!jobId}>
          REFRESH
        </button>

        <a
          className={`btn ${canDownload ? "secondary" : "disabled"}`}
          href={canDownload ? downloadUrl : "#"}
          onClick={(e) => {
            if (!canDownload) e.preventDefault();
          }}
        >
          DOWNLOAD
        </a>
      </div>

      <div className="small" style={{ marginTop: 10 }}>
        txt/csv: one PN per line · xlsx/xls: backend parser handles file format
      </div>

      {err ? (
        <>
          <Hr />
          <div className="small err">{err}</div>
        </>
      ) : null}

      {job ? (
        <>
          <Hr />
          <BatchJobBlock job={job} />

          <Hr />
          <BatchReviewTable job={job} />

          <Hr />
          <details>
            <summary className="small monoInline">Raw Job JSON</summary>
            <div style={{ marginTop: 10 }} />
            <KV obj={job} />
          </details>
        </>
      ) : (
        <div className="small" style={{ marginTop: 10 }}>
          upload PN list / excel → async job → poll status → download xlsx (country layout)
        </div>
      )}
    </Card>
  );
}

/* =========================
 * ADMIN (DDP + price rules + Sys FOB adjust)
 * ========================= */

const PRICE_RULE_FIELDS = [
  { key: "reseller", label: "Reseller" },
  { key: "gold", label: "Gold" },
  { key: "silver", label: "Silver" },
  { key: "ivory", label: "Ivory" },
  { key: "msrp_on_installer", label: "MSRP" },
];

const DDP_RULE_FIELDS = [
  { key: "p1", label: "M1" },
  { key: "p2", label: "M2" },
  { key: "p3", label: "M3" },
  { key: "p4", label: "M4" },
];

function normNum(v, fallback = 0) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function flattenDdpRules(ddpRules) {
  const rows = [];
  const keys = Object.keys(ddpRules || {}).sort();
  for (const k of keys) {
    const v = Array.isArray(ddpRules[k]) ? ddpRules[k] : [];
    rows.push({
      category: k,
      p1: safeStr(v[0]),
      p2: safeStr(v[1]),
      p3: safeStr(v[2]),
      p4: safeStr(v[3]),
    });
  }
  return rows;
}

function rebuildDdpRules(rows) {
  const out = {};
  for (const r of rows) {
    out[r.category] = [
      normNum(r.p1, 0),
      normNum(r.p2, 0),
      normNum(r.p3, 0),
      normNum(r.p4, 0),
    ];
  }
  return out;
}

function flattenPricingRules(pricingRules) {
  const rows = [];
  const groups = Object.keys(pricingRules || {}).sort();
  for (const g of groups) {
    const groupObj = pricingRules[g] || {};
    const ruleNames = Object.keys(groupObj).sort();
    for (const rn of ruleNames) {
      const item = groupObj[rn] || {};
      rows.push({
        group: g,
        ruleName: rn,
        reseller: safeStr(item.reseller),
        gold: safeStr(item.gold),
        silver: safeStr(item.silver),
        ivory: safeStr(item.ivory),
        msrp_on_installer: safeStr(item.msrp_on_installer),
      });
    }
  }
  return rows;
}

function rebuildPricingRules(rows) {
  const out = {};
  for (const r of rows) {
    if (!out[r.group]) out[r.group] = {};
    out[r.group][r.ruleName] = {
      reseller: normNum(r.reseller, 0),
      gold: normNum(r.gold, 0),
      silver: normNum(r.silver, 0),
      ivory: normNum(r.ivory, 0),
      msrp_on_installer: normNum(r.msrp_on_installer, 0),
    };
  }
  return out;
}

function parseUpliftAnyShape(raw) {
  const upliftFlat = {};
  const src = raw || {};

  for (const k of Object.keys(src)) {
    const v = src[k];

    if (Array.isArray(v)) {
      upliftFlat[`${k}#Tier1`] = v[0] ?? "";
      upliftFlat[`${k}#Tier2`] = v[1] ?? "";
      upliftFlat[`${k}#Tier3`] = v[2] ?? "";
      upliftFlat[`${k}#Tier4`] = v[3] ?? "";
      continue;
    }

    upliftFlat[k] = v;
  }

  return upliftFlat;
}

function buildUpliftPayloadLikeInput(originalRaw, upliftFlat) {
  const src = originalRaw || {};
  const out = {};

  for (const k of Object.keys(src)) {
    const v = src[k];
    if (Array.isArray(v)) {
      out[k] = [
        normNum(upliftFlat[`${k}#Tier1`], 0),
        normNum(upliftFlat[`${k}#Tier2`], 0),
        normNum(upliftFlat[`${k}#Tier3`], 0),
        normNum(upliftFlat[`${k}#Tier4`], 0),
      ];
    } else {
      out[k] = normNum(upliftFlat[k], 0);
    }
  }

  for (const k of Object.keys(upliftFlat)) {
    if (k.includes("#")) continue;
    if (!(k in out)) out[k] = normNum(upliftFlat[k], 0);
  }

  const grouped = {};
  for (const k of Object.keys(upliftFlat)) {
    const idx = k.indexOf("#");
    if (idx < 0) continue;
    const cat = k.slice(0, idx);
    const tier = k.slice(idx + 1);
    if (!grouped[cat]) grouped[cat] = {};
    grouped[cat][tier] = upliftFlat[k];
  }
  for (const cat of Object.keys(grouped)) {
    if (cat in out) continue;
    out[cat] = [
      normNum(grouped[cat].Tier1, 0),
      normNum(grouped[cat].Tier2, 0),
      normNum(grouped[cat].Tier3, 0),
      normNum(grouped[cat].Tier4, 0),
    ];
  }

  return out;
}

function flattenKeywordUpliftRows(rawRows) {
  const src = Array.isArray(rawRows) ? rawRows : [];
  return src.map((r, idx) => ({
    id: `kw_${idx}_${Date.now()}`,
    keyword: safeStr(r?.keyword),
    pct: safeStr(r?.pct),
    enabled: r?.enabled !== false,
  }));
}

function rebuildKeywordUpliftRows(rows) {
  const out = [];
  for (const r of rows || []) {
    const kw = safeStr(r?.keyword).trim();
    if (!kw) continue;
    out.push({
      keyword: kw,
      pct: normNum(r?.pct, 0),
      enabled: r?.enabled !== false,
    });
  }
  return out;
}

function AdminUnifiedRules() {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [autoSaving, setAutoSaving] = useState(false);
  const [err, setErr] = useState("");
  const [info, setInfo] = useState("");

  const [upliftRaw, setUpliftRaw] = useState(null);
  const [ddpRows, setDdpRows] = useState([]);
  const [rows, setRows] = useState([]);
  const [upliftFlat, setUpliftFlat] = useState({});
  const [filter, setFilter] = useState("");
  const skipAutoSaveRef = useRef(true);
  const dirtyRef = useRef(false);
  const rowsRef = useRef(rows);
  const ddpRowsRef = useRef(ddpRows);
  const upliftRawRef = useRef(upliftRaw);
  const upliftFlatRef = useRef(upliftFlat);

  useEffect(() => {
    rowsRef.current = rows;
    ddpRowsRef.current = ddpRows;
    upliftRawRef.current = upliftRaw;
    upliftFlatRef.current = upliftFlat;
  }, [rows, ddpRows, upliftRaw, upliftFlat]);

  async function loadAll() {
    setErr("");
    setInfo("");
    setLoading(true);
    skipAutoSaveRef.current = true;
    try {
      const [pr, uf, dr] = await Promise.all([
        apiGetJson("/api/admin/pricing-rules"),
        apiGetJson("/api/admin/uplift"),
        apiGetJson("/api/admin/ddp-rules"),
      ]);

      setUpliftRaw(uf || {});
      setDdpRows(flattenDdpRules(dr || {}));
      setRows(flattenPricingRules(pr || {}));
      setUpliftFlat(parseUpliftAnyShape(uf || {}));
      setInfo("Loaded");
      dirtyRef.current = false;
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setLoading(false);
      setTimeout(() => {
        skipAutoSaveRef.current = false;
      }, 0);
    }
  }

  async function persistAll({ reload = false, silent = false, forceReloadRules = false, snapshot = null } = {}) {
    const src = snapshot || {
      rows: rowsRef.current,
      ddpRows: ddpRowsRef.current,
      upliftRaw: upliftRawRef.current,
      upliftFlat: upliftFlatRef.current,
    };
    const pricingPayload = rebuildPricingRules(src.rows || []);
    const ddpPayload = rebuildDdpRules(src.ddpRows || []);
    const upliftPayload = buildUpliftPayloadLikeInput(src.upliftRaw, src.upliftFlat || {});

    await apiPutJson("/api/admin/pricing-rules", pricingPayload);
    await apiPutJson("/api/admin/ddp-rules", ddpPayload);
    await apiPutJson("/api/admin/uplift", upliftPayload);
    if (forceReloadRules) {
      await apiPostJson("/api/admin/reload-rules", {});
    }

    if (!silent) {
      setInfo("Saved pricing-rules + ddp-rules + sys-fob-adjust");
    }
    if (reload) {
      await loadAll();
    } else {
      dirtyRef.current = false;
    }
  }

  async function saveAll() {
    setErr("");
    setInfo("");
    setSaving(true);
    try {
      await persistAll({ reload: true, silent: false, forceReloadRules: true });
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setSaving(false);
    }
  }

  useEffect(() => {
    loadAll();
  }, []);

  function setRuleValue(rowIdx, field, value) {
    dirtyRef.current = true;
    setRows((prev) => {
      const next = prev.slice();
      next[rowIdx] = { ...next[rowIdx], [field]: value };
      return next;
    });
  }

  function setDdpValue(rowIdx, field, value) {
    dirtyRef.current = true;
    setDdpRows((prev) => {
      const next = prev.slice();
      next[rowIdx] = { ...next[rowIdx], [field]: value };
      return next;
    });
  }

  function setUpliftValue(key, value) {
    dirtyRef.current = true;
    setUpliftFlat((prev) => ({ ...prev, [key]: value }));
  }

  function resolveAutoUpliftKeyForRow(row) {
    const rn = String(row?.ruleName || "").trim();
    if (rn && rn !== "_default_") return rn;
    return String(row?.group || "").trim();
  }

  function jumpToTop() {
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function jumpToBottom() {
    window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
  }

  useEffect(() => {
    if (skipAutoSaveRef.current) return;
    if (loading || saving || autoSaving) return;
    const t = setTimeout(async () => {
      setErr("");
      setAutoSaving(true);
      try {
        await persistAll({ reload: false, silent: true });
        setInfo("Auto-saved");
      } catch (e) {
        setErr(String(e.message || e));
      } finally {
        setAutoSaving(false);
      }
    }, 700);
    return () => clearTimeout(t);
  }, [rows, ddpRows, upliftFlat, loading, saving]);

  useEffect(() => {
    return () => {
      if (!dirtyRef.current) return;
      const snapshot = {
        rows: rowsRef.current,
        ddpRows: ddpRowsRef.current,
        upliftRaw: upliftRawRef.current,
        upliftFlat: upliftFlatRef.current,
      };
      void persistAll({ reload: false, silent: true, forceReloadRules: true, snapshot }).catch(() => {});
    };
  }, []);

  const visibleRows = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return rows;
    return rows.filter((r) => {
      return (
        String(r.group).toLowerCase().includes(q) ||
        String(r.ruleName).toLowerCase().includes(q)
      );
    });
  }, [rows, filter]);

  return (
    <Card
      title="RULES · DDP + PRICE + SYS FOB ADJUST"
      right={
        <span className="small monoInline">
          /api/admin/ddp-rules + /api/admin/pricing-rules + /api/admin/uplift
        </span>
      }
    >
      <div className="row wrap">
        <button className="btn" onClick={loadAll} disabled={loading}>
          {loading ? "LOADING..." : "RELOAD"}
        </button>
        <button className="btn primary" onClick={saveAll} disabled={saving || autoSaving}>
          {saving ? "SAVING..." : "SAVE+RELOAD"}
        </button>
        <span className="small monoInline">{autoSaving ? "AUTO-SAVING..." : "AUTO-SAVE ON"}</span>

        <input
          className="input"
          style={{ maxWidth: 360 }}
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="filter by product group / rule name"
        />
        <button className="btn" onClick={jumpToTop}>
          TOP
        </button>
        <button className="btn" onClick={jumpToBottom}>
          BOTTOM
        </button>

      </div>

      {err ? (
        <>
          <Hr />
          <div className="small err">{err}</div>
        </>
      ) : null}

      {info ? (
        <>
          <Hr />
          <div className="small okc">{info}</div>
        </>
      ) : null}

      <Hr />

      <div className="small monoInline">DDP Rules (FOB * (1+M1) * (1+M2) * (1+M3) * (1+M4))</div>
      <div className="tableWrap">
        <table className="table dense">
          <thead>
            <tr>
              <th style={{ minWidth: 180 }}>Category</th>
              {DDP_RULE_FIELDS.map((f) => (
                <th key={f.key} style={{ minWidth: 110 }}>
                  {f.label}
                </th>
              ))}
              <th style={{ minWidth: 380 }}>Formula Preview</th>
            </tr>
          </thead>
          <tbody>
            {ddpRows.map((r) => {
              const actualIdx = ddpRows.indexOf(r);
              return (
                <tr key={r.category}>
                  <td className="mono">{r.category}</td>
                  {DDP_RULE_FIELDS.map((f) => (
                    <td key={f.key}>
                      <input
                        className="input mono cellInput"
                        value={safeStr(r[f.key])}
                        onChange={(e) => setDdpValue(actualIdx, f.key, e.target.value)}
                      />
                    </td>
                  ))}
                  <td className="mono small">
                    {`FOB*(1+${safeStr(r.p1 || 0)})*(1+${safeStr(r.p2 || 0)})*(1+${safeStr(r.p3 || 0)})*(1+${safeStr(r.p4 || 0)})`}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <Hr />

      <div className="small monoInline">Price Rules + Sys FOB Adjust (France FOB missing + Sys fallback only)</div>
      <div className="tableWrap">
        <table className="table dense">
          <thead>
            <tr>
              <th style={{ minWidth: 140 }}>Product Group</th>
              <th style={{ minWidth: 220 }}>Rule Name</th>
              {PRICE_RULE_FIELDS.map((f) => (
                <th key={f.key} style={{ minWidth: 120 }}>
                  {f.label}
                </th>
              ))}
              <th style={{ minWidth: 180 }}>Adjust</th>
            </tr>
          </thead>
          <tbody>
            {visibleRows.map((r) => {
              const actualIdx = rows.indexOf(r);
              const autoUpliftKey = resolveAutoUpliftKeyForRow(r);
              return (
                <tr key={`${r.group}__${r.ruleName}`}>
                  <td className="mono">{r.group}</td>
                  <td className="mono">{r.ruleName}</td>

                  {PRICE_RULE_FIELDS.map((f) => (
                    <td key={f.key}>
                      <input
                        className="input mono cellInput"
                        value={safeStr(r[f.key])}
                        onChange={(e) => setRuleValue(actualIdx, f.key, e.target.value)}
                      />
                    </td>
                  ))}
                  <td>
                    <input
                      className="input mono cellInput"
                      title={`uplift_key = ${autoUpliftKey}`}
                      value={safeStr(upliftFlat[autoUpliftKey])}
                      onChange={(e) => setUpliftValue(autoUpliftKey, e.target.value)}
                      placeholder="0 / 0.05 / 0.15"
                    />
                  </td>
                </tr>
              );
            })}

            {visibleRows.length === 0 ? (
              <tr>
                <td colSpan={2 + PRICE_RULE_FIELDS.length + 1}>
                  <div className="small">No rows matched filter.</div>
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      <Hr />

      <details>
        <summary className="small monoInline">All Sys FOB Adjust Keys (advanced / direct edit)</summary>
        <div style={{ marginTop: 10 }} />
        <div className="tableWrap">
          <table className="table dense">
            <thead>
              <tr>
                <th style={{ minWidth: 260 }}>uplift_key</th>
                <th style={{ minWidth: 180 }}>value</th>
              </tr>
            </thead>
            <tbody>
              {Object.keys(upliftFlat)
                .sort()
                .map((k) => (
                  <tr key={k}>
                    <td className="mono">{k}</td>
                    <td>
                      <input
                        className="input mono cellInput"
                        value={safeStr(upliftFlat[k])}
                        onChange={(e) => setUpliftValue(k, e.target.value)}
                      />
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      </details>

      <div className="row wrap" style={{ marginTop: 10 }}>
        <button className="btn" onClick={jumpToTop}>
          TOP
        </button>
        <button className="btn" onClick={jumpToBottom}>
          BOTTOM
        </button>
      </div>
    </Card>
  );
}

function KeywordAdjustConsole() {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");
  const [info, setInfo] = useState("");
  const [keywordRows, setKeywordRows] = useState([]);
  const [keywordPreviewById, setKeywordPreviewById] = useState({});
  const [keywordPreviewLoadingId, setKeywordPreviewLoadingId] = useState("");

  async function loadAll() {
    setErr("");
    setInfo("");
    setLoading(true);
    try {
      const kw = await apiGetJson("/api/admin/keyword-uplift");
      setKeywordRows(flattenKeywordUpliftRows(kw || []));
      setKeywordPreviewById({});
      setInfo("Loaded");
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }

  async function saveAll() {
    setErr("");
    setInfo("");
    setSaving(true);
    try {
      const payload = rebuildKeywordUpliftRows(keywordRows || []);
      await apiPutJson("/api/admin/keyword-uplift", payload);
      await apiPostJson("/api/admin/reload-rules", {});
      setInfo("Saved keyword adjust rules + reloaded");
      await loadAll();
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setSaving(false);
    }
  }

  useEffect(() => {
    loadAll();
  }, []);

  function setKeywordValue(rowIdx, field, value) {
    setKeywordRows((prev) => {
      const next = prev.slice();
      next[rowIdx] = { ...next[rowIdx], [field]: value };
      return next;
    });
  }

  function addKeywordRow() {
    setKeywordRows((prev) => [
      ...prev,
      { id: `kw_new_${Date.now()}`, keyword: "", pct: "0", enabled: true },
    ]);
  }

  function removeKeywordRow(rowIdx) {
    setKeywordRows((prev) => prev.filter((_, i) => i !== rowIdx));
  }

  async function previewKeywordRow(row) {
    const keyword = safeStr(row?.keyword).trim();
    if (!keyword) return;
    const id = safeStr(row?.id);
    setKeywordPreviewLoadingId(id);
    try {
      const resp = await apiPostJson("/api/admin/keyword-uplift/preview", {
        keyword,
        pct: normNum(row?.pct, 0),
        enabled: row?.enabled !== false,
      });
      setKeywordPreviewById((prev) => ({ ...prev, [id]: resp }));
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setKeywordPreviewLoadingId("");
    }
  }

  function jumpToTop() {
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function jumpToBottom() {
    window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
  }

  return (
    <Card
      title="KEYWORD ADJUST · GLOBAL MATCH + IMPACT PREVIEW"
      right={<span className="small monoInline">/api/admin/keyword-uplift + /api/admin/keyword-uplift/preview</span>}
    >
      <div className="row wrap">
        <button className="btn" onClick={loadAll} disabled={loading}>
          {loading ? "LOADING..." : "RELOAD"}
        </button>
        <button className="btn primary" onClick={saveAll} disabled={saving}>
          {saving ? "SAVING..." : "SAVE+RELOAD"}
        </button>
        <button className="btn" onClick={jumpToTop}>
          TOP
        </button>
        <button className="btn" onClick={jumpToBottom}>
          BOTTOM
        </button>
      </div>

      {err ? (
        <>
          <Hr />
          <div className="small err">{err}</div>
        </>
      ) : null}
      {info ? (
        <>
          <Hr />
          <div className="small okc">{info}</div>
        </>
      ) : null}

      <Hr />
        <div className="row wrap" style={{ marginBottom: 8 }}>
          <div className="small monoInline">
            关键词命中范围：France + Sys 的 Internal Model / External Model；Preview 会重算并仅展示价格实际变化的 PN。
          </div>
          <button className="btn" onClick={addKeywordRow}>
            ADD KEYWORD RULE
        </button>
      </div>

      <div className="tableWrap">
        <table className="table dense">
          <thead>
            <tr>
              <th style={{ minWidth: 200 }}>Keyword</th>
              <th style={{ minWidth: 120 }}>Increase %</th>
              <th style={{ minWidth: 110 }}>Enabled</th>
              <th style={{ minWidth: 260 }}>Impact Summary</th>
              <th style={{ minWidth: 260 }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {keywordRows.map((r, idx) => {
              const pv = keywordPreviewById[safeStr(r.id)];
              const affected = pv?.affected_count ?? pv?.total ?? "-";
              const candidates = pv?.candidate_count ?? pv?.total_candidates ?? "-";
              const displayed = pv?.displayed_count ?? pv?.shown ?? "-";
              return (
                <tr key={safeStr(r.id)}>
                  <td>
                    <input
                      className="input mono cellInput"
                      value={safeStr(r.keyword)}
                      onChange={(e) => setKeywordValue(idx, "keyword", e.target.value)}
                      placeholder="ASC / ASI / VTO / KIT"
                    />
                  </td>
                  <td>
                    <input
                      className="input mono cellInput"
                      value={safeStr(r.pct)}
                      onChange={(e) => setKeywordValue(idx, "pct", e.target.value)}
                      placeholder="0.15"
                    />
                  </td>
                  <td>
                    <select
                      className="input mono cellInput"
                      value={r.enabled !== false ? "1" : "0"}
                      onChange={(e) => setKeywordValue(idx, "enabled", e.target.value === "1")}
                    >
                      <option value="1">yes</option>
                      <option value="0">no</option>
                    </select>
                  </td>
                  <td className="mono">
                    {pv ? `受影响=${safeStr(affected)} · 候选=${safeStr(candidates)} · 展示=${safeStr(displayed)}` : "-"}
                  </td>
                  <td className="row wrap">
                    <button
                      className="btn"
                      onClick={() => previewKeywordRow(r)}
                      disabled={keywordPreviewLoadingId === safeStr(r.id) || !safeStr(r.keyword).trim()}
                    >
                      {keywordPreviewLoadingId === safeStr(r.id) ? "PREVIEWING..." : "PREVIEW"}
                    </button>
                    <button className="btn" onClick={() => removeKeywordRow(idx)}>
                      DELETE
                    </button>
                  </td>
                </tr>
              );
            })}
            {keywordRows.length === 0 ? (
              <tr>
                <td colSpan={5}>
                  <div className="small">No keyword rules. Click ADD KEYWORD RULE.</div>
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>

      {keywordRows
        .filter((r) => keywordPreviewById[safeStr(r.id)])
        .map((r) => {
          const pv = keywordPreviewById[safeStr(r.id)];
          const list = Array.isArray(pv?.rows) ? pv.rows : [];
          const affected = pv?.affected_count ?? pv?.total ?? 0;
          const candidates = pv?.candidate_count ?? pv?.total_candidates ?? 0;
          const displayed = pv?.displayed_count ?? pv?.shown ?? 0;
          return (
            <details key={`preview_${safeStr(r.id)}`} style={{ marginTop: 8 }} open>
              <summary className="small monoInline">
                Preview {safeStr(r.keyword)}：候选 {safeStr(candidates)}，受影响 {safeStr(affected)}，展示 {safeStr(displayed)}（已展示全部受影响项）
              </summary>
              <div style={{ marginTop: 8 }} />
              <div className="tableWrap">
                <table className="table dense">
                  <thead>
                    <tr>
                      <th style={{ minWidth: 220 }}>PN</th>
                      <th style={{ minWidth: 220 }}>Internal Model</th>
                      <th style={{ minWidth: 220 }}>External Model</th>
                      <th style={{ minWidth: 120 }}>FOB Before</th>
                      <th style={{ minWidth: 120 }}>FOB After</th>
                      <th style={{ minWidth: 120 }}>DDP After</th>
                      <th style={{ minWidth: 120 }}>Reseller</th>
                      <th style={{ minWidth: 120 }}>Gold</th>
                      <th style={{ minWidth: 120 }}>Silver</th>
                      <th style={{ minWidth: 120 }}>Ivory</th>
                      <th style={{ minWidth: 120 }}>MSRP</th>
                      <th style={{ minWidth: 120 }}>Delta %</th>
                    </tr>
                  </thead>
                  <tbody>
                    {list.map((x, i) => (
                      <tr key={`${safeStr(r.id)}_${i}`}>
                        <td className="mono">{safeStr(x.pn)}</td>
                        <td className="mono">{safeStr(x.internal_model)}</td>
                        <td className="mono">{safeStr(x.external_model)}</td>
                        <td className="mono">{safeStr(formatPricePiecewise(x.fob_before))}</td>
                        <td className="mono">{safeStr(formatPricePiecewise(x.fob_after))}</td>
                        <td className="mono">{safeStr(formatPricePiecewise(x.ddp_after))}</td>
                        <td className="mono">{safeStr(formatPricePiecewise(x.reseller_after))}</td>
                        <td className="mono">{safeStr(formatPricePiecewise(x.gold_after))}</td>
                        <td className="mono">{safeStr(formatPricePiecewise(x.silver_after))}</td>
                        <td className="mono">{safeStr(formatPricePiecewise(x.ivory_after))}</td>
                        <td className="mono">{safeStr(formatPricePiecewise(x.msrp_after))}</td>
                        <td className="mono">{safeStr(x.delta_pct)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          );
        })}
    </Card>
  );
}

function AdminConsole() {
  return (
    <div className="stack">

      <AdminUnifiedRules />
    </div>
  );
}

/* =========================
 * Meta
 * ========================= */

function MetaPanel({ meta, metaErr }) {
  return (
    <Card
      title="ENGINE META"
      right={meta ? <span className="pill">loaded {safeStr(meta.loaded)}</span> : null}
    >
      {metaErr ? <div className="small err">{metaErr}</div> : null}
      {meta ? <KV obj={meta} /> : <div className="small">/api/meta</div>}
    </Card>
  );
}

/* =========================
 * App root
 * ========================= */

export default function App() {
  const [tab, setTab] = useState("query");
  const [meta, setMeta] = useState(null);
  const [metaErr, setMetaErr] = useState("");

  useEffect(() => {
    (async () => {
      try {
        const m = await apiGetJson("/api/meta");
        setMeta(m);
      } catch (e) {
        setMetaErr(String(e.message || e));
      }
    })();
  }, []);

  return (
    <div className="container">
      <div className="topbar">
        <div className="brand">
          <h1 className="title">
            <span className="dot" />
            大华法国驻地专用产品定价自动化平台
          </h1>
          <div className="sub">
            <span className="pill">
              国家侧数据更新时间:
              {formatMetaTime(meta?.country_data_updated_at_iso, meta?.country_data_updated_at_epoch)}
            </span>
            <span className="pill">
              系统侧数据更新时间:
              {formatMetaTime(meta?.sys_data_updated_at_iso, meta?.sys_data_updated_at_epoch)}
            </span>
            <span className="pill">遇到问题请联系 开发人员:林建克 Jianke LIN | Wechat: Epochex404 </span>
          </div>
        </div>

        <div className="tabs">
          <button
            className={`tab ${tab === "query" ? "active" : ""}`}
            onClick={() => setTab("query")}
          >
            QUERY
          </button>
          <button
            className={`tab ${tab === "search" ? "active" : ""}`}
            onClick={() => setTab("search")}
          >
            MODEL SEARCH
          </button>
          <button
            className={`tab ${tab === "batch" ? "active" : ""}`}
            onClick={() => setTab("batch")}
          >
            BATCH
          </button>
          <button
            className={`tab ${tab === "rules" ? "active" : ""}`}
            onClick={() => setTab("rules")}
          >
            RULES
          </button>
          <button
            className={`tab ${tab === "keyword" ? "active" : ""}`}
            onClick={() => setTab("keyword")}
          >
            KEYWORD
          </button>
          <button
            className={`tab ${tab === "meta" ? "active" : ""}`}
            onClick={() => setTab("meta")}
          >
            META
          </button>
        </div>
      </div>

      <div className="grid">
        {tab === "query" ? <SingleQuery /> : null}
        {tab === "search" ? <ModelSearchPage /> : null}
        {tab === "batch" ? <BatchExport /> : null}
        {tab === "rules" ? <AdminConsole /> : null}
        {tab === "keyword" ? <KeywordAdjustConsole /> : null}
        {tab === "meta" ? <MetaPanel meta={meta} metaErr={metaErr} /> : null}
      </div>
    </div>
  );
}
