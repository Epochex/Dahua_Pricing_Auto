// frontend/src/cliFormat.js
import { formatPricePiecewise, safeStr } from "./format.js";

function padRight(s, n) {
  s = String(s ?? "");
  if (s.length >= n) return s;
  return s + " ".repeat(n - s.length);
}
function line(ch, n) {
  return ch.repeat(n);
}

export function formatQueryAsCli(resp) {
  const status = safeStr(resp?.status);
  const pn = safeStr(resp?.pn);
  const meta = resp?.meta || {};
  const fv = resp?.final_values || {};
  const calculated = new Set(resp?.calculated_fields || []);

  const head = [];
  head.push(`[Match] France: ${safeStr(meta.fr_match_mode)} | Sys: ${safeStr(meta.sys_match_mode)}`);
  head.push("");
  head.push("查询结果如下：");
  head.push(
    `计算状态：${status === "ok" ? "自动化计算成功" : status}（产品线：${safeStr(meta.category)} / 子线：${safeStr(meta.series_key)} | 系列：${safeStr(meta.series_display)} | 定价公式：${safeStr(meta.pricing_rule_name)}）`
  );
  head.push(
    `[计算层级]：${safeStr(meta.sys_sales_type)}（Sys 基准字段：${safeStr(meta.sys_basis_field)} | 定价公式：${safeStr(meta.pricing_rule_name)}）`
  );
  head.push("");

  // 终端表格（简化版，保持你本地风格的“Name | Value | Calculated”语义）
  const rows = [];
  rows.push(["Part No.", pn, ""]);
  rows.push(["Series", safeStr(meta.series_display), ""]);
  rows.push(["External Model", safeStr(resp?.external_model), ""]);
  rows.push(["Internal Model", safeStr(resp?.internal_model), ""]);
  rows.push(["Sales Status", safeStr(resp?.sales_status), ""]);
  rows.push(["Description", safeStr(resp?.description) || "Value Not Found", ""]);

  const priceKeys = [
    "FOB C(EUR)",
    "DDP A(EUR)",
    "Suggested Reseller(EUR)",
    "Gold(EUR)",
    "Silver(EUR)",
    "Ivory(EUR)",
    "MSRP(EUR)"
  ];

  for (const k of priceKeys) {
    const v = fv?.[k];
    const display = v === null || v === undefined ? "" : formatPricePiecewise(v);
    rows.push([k, display, calculated.has(k) ? "Calculated" : ""]);
  }

  const col1 = Math.max(...rows.map(r => String(r[0]).length), 10);
  const col2 = Math.max(...rows.map(r => String(r[1]).length), 10);
  const col3 = Math.max(...rows.map(r => String(r[2]).length), 10);

  const total = col1 + col2 + col3 + 10;
  const out = [];
  out.push(line("=", total));
  for (const r of rows) {
    out.push(
      `| ${padRight(r[0], col1)} | ${padRight(r[1], col2)} | ${padRight(r[2], col3)} |`
    );
  }
  out.push(line("=", total));
  out.push("");
  out.push("请输入 Part No.（输入 quit 退出，直接回车进入批量模式）：");

  return head.join("\n") + "\n" + out.join("\n");
}
