async function readTextSafe(r) {
  try {
    return await r.text();
  } catch {
    return "";
  }
}

export const ADMIN_TOKEN_SESSION_KEY = "dahua_price_data_admin_token";

function adminWriteHeaders(url) {
  if (!String(url || "").startsWith("/api/admin")) return {};
  try {
    const token = window.sessionStorage.getItem(ADMIN_TOKEN_SESSION_KEY) || "";
    return token ? { "X-Price-Data-Admin-Token": token } : {};
  } catch {
    return {};
  }
}

async function throwRequestError(method, url, response) {
  const detail = await readTextSafe(response);
  const authHint =
    String(url || "").startsWith("/api/admin") && (response.status === 401 || response.status === 403)
      ? "。请先到“价格数据源”页面输入有效的管理员令牌"
      : "";
  throw new Error(`${method} ${url} -> ${response.status} ${detail}${authHint}`);
}

export async function apiGetJson(url) {
  const r = await fetch(url, { method: "GET" });
  if (!r.ok) await throwRequestError("GET", url, r);
  return await r.json();
}

export async function apiPostJson(url, body, options = {}) {
  const r = await fetch(url, {
    method: "POST",
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...adminWriteHeaders(url),
      ...(options.headers || {}),
    },
    body: JSON.stringify(body)
  });
  if (!r.ok) await throwRequestError("POST", url, r);
  return await r.json();
}

export async function apiPutJson(url, body, options = {}) {
  const r = await fetch(url, {
    method: "PUT",
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...adminWriteHeaders(url),
      ...(options.headers || {}),
    },
    body: JSON.stringify(body)
  });
  if (!r.ok) await throwRequestError("PUT", url, r);
  return await r.json().catch(() => ({}));
}

export async function apiPostForm(url, formData, options = {}) {
  const r = await fetch(url, {
    method: "POST",
    ...options,
    headers: { ...adminWriteHeaders(url), ...(options.headers || {}) },
    body: formData,
  });
  if (!r.ok) await throwRequestError("POST", url, r);
  return await r.json();
}
