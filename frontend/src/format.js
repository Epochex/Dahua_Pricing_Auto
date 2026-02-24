export function formatPricePiecewise(v) {
  if (v === null || v === undefined) return "";
  const f = Number(v);
  if (!Number.isFinite(f)) return String(v);

  if (f < 30) return f.toFixed(2);
  return String(Math.round(f));
}

export function fmtBool(v) {
  return v ? "YES" : "";
}

export function safeStr(v) {
  if (v === null || v === undefined) return "";
  return String(v);
}
