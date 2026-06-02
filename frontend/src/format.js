const PRICE_DECIMAL_THRESHOLD = 10;

export function formatPricePiecewise(v) {
  if (v === null || v === undefined) return "";
  const f = Number(v);
  if (!Number.isFinite(f)) return String(v);

  if (f < PRICE_DECIMAL_THRESHOLD) {
    return (Math.round((f + Number.EPSILON) * 10) / 10).toFixed(1);
  }
  return String(Math.round(f));
}

export function fmtBool(v) {
  return v ? "YES" : "";
}

export function safeStr(v) {
  if (v === null || v === undefined) return "";
  if (typeof v === "number" && Number.isNaN(v)) return "";
  return String(v);
}
