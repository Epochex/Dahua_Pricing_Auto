async function readTextSafe(r) {
  try {
    return await r.text();
  } catch {
    return "";
  }
}

export async function apiGetJson(url) {
  const r = await fetch(url, { method: "GET" });
  if (!r.ok) throw new Error(`GET ${url} -> ${r.status} ${await readTextSafe(r)}`);
  return await r.json();
}

export async function apiPostJson(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  if (!r.ok) throw new Error(`POST ${url} -> ${r.status} ${await readTextSafe(r)}`);
  return await r.json();
}

export async function apiPutJson(url, body) {
  const r = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  if (!r.ok) throw new Error(`PUT ${url} -> ${r.status} ${await readTextSafe(r)}`);
  return await r.json().catch(() => ({}));
}

export async function apiPostForm(url, formData) {
  const r = await fetch(url, { method: "POST", body: formData });
  if (!r.ok) throw new Error(`POST ${url} -> ${r.status} ${await readTextSafe(r)}`);
  return await r.json();
}
