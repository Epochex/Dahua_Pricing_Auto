/**
 * Bound Google Apps Script bridge for the pricing inquiry workbook.
 *
 * Required Script Properties:
 *   DAHUA_AGENT_BASE_URL=https://<stable-agent-domain>
 *   DAHUA_AGENT_TOKEN=<contents of sheet_push_token.txt>
 *
 * This bridge has no DingTalk webhook or group-send capability. It only:
 *   1. pushes business-sheet values to the Linux backend;
 *   2. polls approved status updates and writes them back to Google Sheets.
 */

const PRICING_AGENT = Object.freeze({
  bridgeVersion: "1.1.0",
  businessSheetPattern: /^\d{4}\.\d{2}(?:-\d{4}\.\d{2})?$/,
  maxColumns: 26,
  editDebounceMillis: 15000,
  pushPath: "/api/agent/sheet/push",
  pendingPath: "/api/agent/sheet/status-updates/pending",
  ackPath: "/api/agent/sheet/status-updates/ack",
  triggerHandlers: ["pricingAgentOnEdit", "pricingAgentReconcile"],
});

function installPricingAgentTriggers() {
  assertConfigured_();
  const spreadsheet = SpreadsheetApp.getActive();
  ScriptApp.getProjectTriggers().forEach((trigger) => {
    if (PRICING_AGENT.triggerHandlers.includes(trigger.getHandlerFunction())) {
      ScriptApp.deleteTrigger(trigger);
    }
  });

  ScriptApp.newTrigger("pricingAgentOnEdit")
    .forSpreadsheet(spreadsheet)
    .onEdit()
    .create();
  ScriptApp.newTrigger("pricingAgentReconcile")
    .timeBased()
    .everyMinutes(5)
    .create();

  return pricingAgentSafeTest();
}

function uninstallPricingAgentTriggers() {
  ScriptApp.getProjectTriggers().forEach((trigger) => {
    if (PRICING_AGENT.triggerHandlers.includes(trigger.getHandlerFunction())) {
      ScriptApp.deleteTrigger(trigger);
    }
  });
}

function configurePricingAgent(baseUrl, token, enableWriteback) {
  const normalizedUrl = String(baseUrl || "").replace(/\/$/, "");
  const normalizedToken = String(token || "");
  if (!/^https:\/\//.test(normalizedUrl)) throw new Error("baseUrl must use HTTPS");
  if (!normalizedToken) throw new Error("token is required");
  PropertiesService.getScriptProperties().setProperties({
    DAHUA_AGENT_BASE_URL: normalizedUrl,
    DAHUA_AGENT_TOKEN: normalizedToken,
    DAHUA_ENABLE_WRITEBACK: enableWriteback === true ? "true" : "false",
  });
  return inspectPricingAgentConfig();
}

function inspectPricingAgentConfig() {
  const cfg = config_();
  return {
    ok: true,
    baseUrl: cfg.baseUrl,
    tokenPresent: Boolean(cfg.token),
    writebackEnabled: cfg.writebackEnabled,
    triggerCount: ScriptApp.getProjectTriggers().filter((trigger) =>
      PRICING_AGENT.triggerHandlers.includes(trigger.getHandlerFunction())
    ).length,
  };
}

function pricingAgentOnEdit(event) {
  if (!event || !event.range) return;
  const sheet = event.range.getSheet();
  if (!isBusinessSheet_(sheet) || event.range.getRow() < 5) return;

  const documentProperties = PropertiesService.getDocumentProperties();
  documentProperties.setProperty("DAHUA_SHEET_DIRTY_AT", new Date().toISOString());
  const lastPushAt = Number(documentProperties.getProperty("DAHUA_LAST_PUSH_EPOCH_MS") || 0);
  if (Date.now() - lastPushAt < PRICING_AGENT.editDebounceMillis) return;

  const lock = LockService.getDocumentLock();
  if (!lock.tryLock(1000)) return;
  try {
    pushAllBusinessSheets_("google-sheets-on-edit");
    documentProperties.setProperty("DAHUA_LAST_PUSH_EPOCH_MS", String(Date.now()));
  } finally {
    lock.releaseLock();
  }
}

function pricingAgentReconcile() {
  const lock = LockService.getDocumentLock();
  if (!lock.tryLock(1000)) return { ok: true, skipped: "another sync is running" };
  try {
    const push = pushAllBusinessSheets_("google-sheets-reconcile");
    const writeback = applyPendingStatusUpdates_();
    PropertiesService.getDocumentProperties().setProperties({
      DAHUA_LAST_PUSH_EPOCH_MS: String(Date.now()),
      DAHUA_LAST_RECONCILE_AT: new Date().toISOString(),
    });
    return { ok: true, push, writeback };
  } finally {
    lock.releaseLock();
  }
}

function pricingAgentSafeTest() {
  const push = pushAllBusinessSheets_("google-sheets-safe-test");
  const pending = postJson_(PRICING_AGENT.pendingPath, {
    token: config_().token,
    limit: 1,
  });
  return {
    ok: true,
    sheetPushAccepted: Boolean(push && push.ok),
    pendingReadAccepted: Boolean(pending && pending.ok),
    writebackEnabled: config_().writebackEnabled,
    groupMessageSent: false,
  };
}

function pushAllBusinessSheets_(source) {
  assertConfigured_();
  const spreadsheet = SpreadsheetApp.getActive();
  const sheets = spreadsheet.getSheets()
    .filter(isBusinessSheet_)
    .map((sheet) => {
      const lastRow = Math.max(sheet.getLastRow(), 4);
      const lastColumn = Math.min(Math.max(sheet.getLastColumn(), 14), PRICING_AGENT.maxColumns);
      return {
        name: sheet.getName(),
        range: `A1:${columnLetter_(lastColumn)}${lastRow}`,
        rows: sheet.getRange(1, 1, lastRow, lastColumn).getDisplayValues(),
        rowCount: lastRow,
      };
    });

  const pushedAt = new Date().toISOString();
  const snapshotHash = sha256Hex_(JSON.stringify(sheets));
  return postJson_(PRICING_AGENT.pushPath, {
    source,
    token: config_().token,
    payload: {
      file: spreadsheet.getName(),
      spreadsheetId: spreadsheet.getId(),
      bridgeVersion: PRICING_AGENT.bridgeVersion,
      pushedAt,
      snapshotHash,
      idempotencyKey: `${spreadsheet.getId()}:${snapshotHash}`,
      sheets,
    },
  });
}

function applyPendingStatusUpdates_() {
  const cfg = config_();
  if (!cfg.writebackEnabled) {
    return { ok: true, disabled: true, count: 0, results: [] };
  }
  const pending = postJson_(PRICING_AGENT.pendingPath, { token: cfg.token, limit: 500 });
  const updates = Array.isArray(pending.updates) ? pending.updates : [];
  const spreadsheet = SpreadsheetApp.getActive();
  const results = [];

  updates.forEach((update) => {
    const updateId = String(update.update_id || "");
    let ok = false;
    let error = "";
    try {
      const sheet = spreadsheet.getSheetByName(String(update.sheet || ""));
      if (!sheet) throw new Error(`sheet not found: ${update.sheet}`);
      const cell = String(update.cell || `L${update.row_index}`);
      const value = String(update.new_status || "已完成");
      validateStatusUpdate_(sheet, cell, value, update);
      sheet.getRange(cell).setValue(value);
      ok = true;
    } catch (err) {
      error = String(err && err.message ? err.message : err);
    }

    postJson_(PRICING_AGENT.ackPath, {
      token: cfg.token,
      update_ids: [updateId],
      ok,
      error,
      applied_by: "google-sheets-agent-bridge",
    });
    results.push({ updateId, ok, error });
  });

  return { ok: true, count: results.length, results };
}

function postJson_(path, body) {
  const cfg = config_();
  const response = UrlFetchApp.fetch(cfg.baseUrl + path, {
    method: "post",
    contentType: "application/json; charset=utf-8",
    payload: JSON.stringify(body),
    muteHttpExceptions: true,
  });
  const status = response.getResponseCode();
  const text = response.getContentText();
  let parsed = {};
  try {
    parsed = text ? JSON.parse(text) : {};
  } catch (err) {
    parsed = { raw: text.slice(0, 1000) };
  }
  if (status < 200 || status >= 300) {
    throw new Error(`Agent HTTP ${status}: ${text.slice(0, 500)}`);
  }
  return parsed;
}

function config_() {
  const props = PropertiesService.getScriptProperties();
  return {
    baseUrl: String(props.getProperty("DAHUA_AGENT_BASE_URL") || "").replace(/\/$/, ""),
    token: String(props.getProperty("DAHUA_AGENT_TOKEN") || ""),
    writebackEnabled: String(props.getProperty("DAHUA_ENABLE_WRITEBACK") || "false").toLowerCase() === "true",
  };
}

function assertConfigured_() {
  const cfg = config_();
  if (!cfg.baseUrl || !/^https:\/\//.test(cfg.baseUrl)) {
    throw new Error("Set DAHUA_AGENT_BASE_URL to a stable HTTPS endpoint in Script Properties");
  }
  if (!cfg.token) throw new Error("Set DAHUA_AGENT_TOKEN in Script Properties");
}

function isBusinessSheet_(sheet) {
  return Boolean(sheet && PRICING_AGENT.businessSheetPattern.test(sheet.getName()));
}

function validateStatusUpdate_(sheet, cell, value, update) {
  const allowedStatuses = ["尚未开始", "进行中", "待决策", "已完成"];
  if (!allowedStatuses.includes(value)) throw new Error(`unsupported status writeback: ${value}`);

  const target = sheet.getRange(cell);
  const expectedRow = Number(update.row_index || target.getRow());
  if (target.getRow() !== expectedRow) {
    throw new Error(`row mismatch: update=${expectedRow}, cell=${target.getRow()}`);
  }

  const headers = sheet.getRange(4, 1, 1, Math.min(sheet.getLastColumn(), PRICING_AGENT.maxColumns)).getDisplayValues()[0];
  const plaColumn = headers.findIndex((header) => /PLA\s*(NO\.?|号)/i.test(String(header || ""))) + 1;
  const expectedPla = String(update.pla_no || "").trim();
  if (expectedPla && plaColumn > 0) {
    const actualPla = String(sheet.getRange(expectedRow, plaColumn).getDisplayValue() || "").trim();
    if (!actualPla.split(/[\s,;，；/]+/).includes(expectedPla)) {
      throw new Error(`PLA mismatch at ${sheet.getName()}!${cell}`);
    }
  }
}

function sha256Hex_(text) {
  const bytes = Utilities.computeDigest(
    Utilities.DigestAlgorithm.SHA_256,
    text,
    Utilities.Charset.UTF_8,
  );
  return bytes.map((value) => (`0${(value + 256).toString(16)}`).slice(-2)).join("");
}

function columnLetter_(column) {
  let value = Number(column);
  let result = "";
  while (value > 0) {
    value -= 1;
    result = String.fromCharCode(65 + (value % 26)) + result;
    value = Math.floor(value / 26);
  }
  return result;
}
