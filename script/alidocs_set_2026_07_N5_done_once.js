// One-off smoke test: update only 2026.07!N5 to 已完成.
// This script does not call the backend, ACK anything, or send notifications.

const TARGET_SHEET = "2026.07";
const TARGET_CELL = "N5";
const TARGET_VALUE = "已完成";

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

try {
  const sheet = findSheetByName(TARGET_SHEET);
  if (!sheet) throw new Error("sheet not found: " + TARGET_SHEET);
  const range = sheet.getRange(TARGET_CELL);
  setRangeValue(range, TARGET_VALUE);
  Output.log("Updated " + TARGET_SHEET + "!" + TARGET_CELL + " -> " + TARGET_VALUE);
} catch (err) {
  Output.log("UPDATE_FAILED");
  Output.log(String(err && err.message ? err.message : err));
}
