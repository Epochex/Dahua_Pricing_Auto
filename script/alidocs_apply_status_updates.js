// DingTalk/Alidocs script: apply approved PLA status updates back to column L.
// 1) Replace AGENT_TOKEN with /data/dahua_pricing_runtime/agent/sheet_push_token.txt.
// 2) Run after alidocs_push_all_sheets.js, or add both scripts to a scheduled trigger.

const PENDING_URL = "https://current-ampland-biography-taxi.trycloudflare.com/api/agent/sheet/status-updates/pending";
const ACK_URL = "https://current-ampland-biography-taxi.trycloudflare.com/api/agent/sheet/status-updates/ack";
const AGENT_TOKEN = "PASTE_TOKEN_HERE";
const LIMIT = 500;

function normalizeSheetCollection(collection) {
  if (!collection) return [];
  if (Array.isArray(collection)) return collection;

  if (typeof collection.length === "number") {
    const out = [];
    for (let i = 0; i < collection.length; i += 1) out.push(collection[i]);
    return out.filter(Boolean);
  }

  if (typeof collection.getCount === "function") {
    const out = [];
    const count = collection.getCount();
    for (let i = 0; i < count; i += 1) {
      if (typeof collection.get === "function") out.push(collection.get(i));
      else if (typeof collection.getItem === "function") out.push(collection.getItem(i));
    }
    return out.filter(Boolean);
  }

  return [];
}

function getAllSheetsCompat() {
  const methodNames = [
    "getSheets",
    "getWorksheets",
    "getAllSheets",
    "getSheetList",
  ];

  for (const name of methodNames) {
    try {
      if (typeof Workbook[name] !== "function") continue;
      const sheets = normalizeSheetCollection(Workbook[name]());
      if (sheets.length > 0) return sheets;
    } catch (err) {
      Output.log("Skip " + name + ": " + String(err && err.message ? err.message : err));
    }
  }

  return [Workbook.getActiveSheet()];
}

function getSheetName(sheet, index) {
  try {
    if (sheet && typeof sheet.getName === "function") return sheet.getName();
  } catch (err) {
    Output.log("getName failed: " + String(err && err.message ? err.message : err));
  }
  return "sheet_" + (index + 1);
}

function findSheetByName(name) {
  const sheets = getAllSheetsCompat();
  for (let i = 0; i < sheets.length; i += 1) {
    if (getSheetName(sheets[i], i) === name) return sheets[i];
  }
  return null;
}

function setRangeValue(range, value) {
  if (range && typeof range.setValue === "function") return range.setValue(value);
  if (range && typeof range.setValues === "function") return range.setValues([[value]]);
  if (range && typeof range.setText === "function") return range.setText(value);
  throw new Error("range does not support setValue/setValues/setText");
}

async function ack(updateId, ok, error) {
  const resp = await fetch(ACK_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      token: AGENT_TOKEN,
      update_ids: [updateId],
      ok,
      error: error || "",
      applied_by: "alidocs-apply-status-updates",
    }),
  });
  const text = await resp.text();
  Output.log("ACK " + updateId + " HTTP " + resp.status + " " + text);
}

try {
  const resp = await fetch(PENDING_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      token: AGENT_TOKEN,
      limit: LIMIT,
    }),
  });
  const text = await resp.text();
  Output.log("PENDING HTTP " + resp.status);
  Output.log(text);
  if (!resp.ok) throw new Error("pending request failed: HTTP " + resp.status);

  const body = JSON.parse(text || "{}");
  const updates = Array.isArray(body.updates) ? body.updates : [];
  Output.log("Pending updates: " + updates.length);

  for (const update of updates) {
    const updateId = String(update.update_id || "");
    const sheetName = String(update.sheet || "");
    const cell = String(update.cell || ("L" + update.row_index));
    const value = String(update.new_status || "已完成");
    if (!updateId || !sheetName || !cell) {
      Output.log("Skip invalid update: " + JSON.stringify(update));
      continue;
    }

    try {
      const sheet = findSheetByName(sheetName);
      if (!sheet) throw new Error("sheet not found: " + sheetName);
      const range = sheet.getRange(cell);
      setRangeValue(range, value);
      Output.log("Updated " + sheetName + "!" + cell + " -> " + value + " (" + update.pla_no + ")");
      await ack(updateId, true, "");
    } catch (err) {
      const msg = String(err && err.message ? err.message : err);
      Output.log("Update failed " + updateId + ": " + msg);
      await ack(updateId, false, msg);
    }
  }
} catch (err) {
  Output.log("FETCH_OR_SCRIPT_ERROR");
  Output.log(String(err && err.message ? err.message : err));
}
