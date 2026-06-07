// DingTalk/Alidocs script: push all inquiry sheets to Dahua Pricing Agent.
// 1) Replace PUSH_TOKEN with /data/dahua_pricing_runtime/agent/sheet_push_token.txt.
// 2) Run manually first. If OK, add it to an Alidocs scheduled/script trigger.

const PUSH_URL = "https://current-ampland-biography-taxi.trycloudflare.com/api/agent/sheet/push";
const PUSH_TOKEN = "PASTE_TOKEN_HERE";
const FILE_NAME = "询价任务状态表_demandeDePrix.xlsx";
const RANGE_ADDRESS = "A1:Z1000";

function isBlankCell(value) {
  return value === null || value === undefined || String(value).trim() === "";
}

function trimTrailingEmptyRows(rows) {
  let end = rows.length;
  while (end > 0) {
    const row = rows[end - 1] || [];
    if (row.some((cell) => !isBlankCell(cell))) break;
    end -= 1;
  }
  return rows.slice(0, end);
}

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
      if (sheets.length > 0) {
        return { sheets, method: name, fallback: false };
      }
    } catch (err) {
      Output.log("Skip " + name + ": " + String(err && err.message ? err.message : err));
    }
  }

  return {
    sheets: [Workbook.getActiveSheet()],
    method: "Workbook.getActiveSheet",
    fallback: true,
  };
}

function getSheetName(sheet, index) {
  try {
    if (sheet && typeof sheet.getName === "function") return sheet.getName();
  } catch (err) {
    Output.log("getName failed: " + String(err && err.message ? err.message : err));
  }
  return "sheet_" + (index + 1);
}

function readSheetRows(sheet) {
  const range = sheet.getRange(RANGE_ADDRESS);
  const rows = range.getValues();
  return trimTrailingEmptyRows(rows || []);
}

try {
  const startedAt = new Date().toISOString();
  const resolved = getAllSheetsCompat();
  const pushedSheets = [];

  for (let i = 0; i < resolved.sheets.length; i += 1) {
    const sheet = resolved.sheets[i];
    const name = getSheetName(sheet, i);
    try {
      const rows = readSheetRows(sheet);
      pushedSheets.push({
        name,
        range: RANGE_ADDRESS,
        rows,
        rowCount: rows.length,
      });
      Output.log("Read " + name + ": " + rows.length + " rows");
    } catch (err) {
      pushedSheets.push({
        name,
        range: RANGE_ADDRESS,
        rows: [],
        rowCount: 0,
        error: String(err && err.message ? err.message : err),
      });
      Output.log("Read failed " + name + ": " + String(err && err.message ? err.message : err));
    }
  }

  const resp = await fetch(PUSH_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      source: "alidocs-all-sheets",
      token: PUSH_TOKEN,
      payload: {
        file: FILE_NAME,
        range: RANGE_ADDRESS,
        pushedAt: new Date().toISOString(),
        startedAt,
        sheetReadMethod: resolved.method,
        fallbackToActiveSheet: resolved.fallback,
        sheets: pushedSheets,
      },
    }),
  });

  const text = await resp.text();
  Output.log("HTTP " + resp.status);
  Output.log(text);
} catch (err) {
  Output.log("FETCH_OR_SCRIPT_ERROR");
  Output.log(String(err && err.message ? err.message : err));
}
