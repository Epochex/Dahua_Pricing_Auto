from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests

if TYPE_CHECKING:
    from playwright.sync_api import Page


def playwright_timeout_error() -> type[Exception]:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        return PlaywrightTimeoutError
    except Exception:
        return TimeoutError


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_sheet_filter(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.fullmatch(r"(\d{4})\.(\d{1,2})", text)
    if match:
        return f"{match.group(1)}.{int(match.group(2)):02d}"
    return text


def load_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    base_dir = path.resolve().parent
    data["server_url"] = str(data.get("server_url") or "").rstrip("/")
    data["agent_token"] = str(data.get("agent_token") or "")
    data["gsp_url"] = str(data.get("gsp_url") or "https://gsp.dahuasecurity.com/#/pricing/application/list")
    data["gsp_base_url"] = str(data.get("gsp_base_url") or "https://gsp.dahuasecurity.com").rstrip("/")
    data["gsp_query_mode"] = str(data.get("gsp_query_mode") or "api").strip().lower()
    data["backend_id"] = str(data.get("backend_id") or "windows-gsp-agent").strip() or "windows-gsp-agent"
    data["backend_display_name"] = (
        str(data.get("backend_display_name") or "Windows GSP Status Agent").strip()
        or "Windows GSP Status Agent"
    )
    data["gsp_username"] = str(data.get("gsp_username") or "")
    data["gsp_password"] = str(data.get("gsp_password") or "")
    data["gsp_country_code"] = str(data.get("gsp_country_code") or "FR").strip() or "FR"
    country_codes = data.get("gsp_country_codes") or data["gsp_country_code"]
    if isinstance(country_codes, str):
        data["gsp_country_codes"] = [x.strip() for x in country_codes.split(",") if x.strip()]
    else:
        data["gsp_country_codes"] = [str(x).strip() for x in country_codes if str(x).strip()]
    if not data["gsp_country_codes"]:
        data["gsp_country_codes"] = ["FR"]
    auth_path = str(data.get("gsp_auth_path") or "gsp_auth.local.json")
    data["gsp_auth_path"] = str((base_dir / auth_path).resolve() if not Path(auth_path).is_absolute() else Path(auth_path))
    data["edge_channel"] = str(data.get("edge_channel") or "msedge")
    data["user_data_dir"] = str(data.get("user_data_dir") or "C:/DahuaPricingAgent/edge-profile")
    data["headless"] = bool(data.get("headless", True))
    data["max_tasks"] = max(1, min(500, int(data.get("max_tasks") or 5)))
    data["sheet"] = normalize_sheet_filter(data.get("sheet"))
    data["poll_interval_seconds"] = max(0, int(data.get("poll_interval_seconds") or 0))
    data["listen_host"] = str(data.get("listen_host") or "0.0.0.0")
    data["listen_port"] = max(1, min(65535, int(data.get("listen_port") or 8765)))
    data["control_url"] = str(data.get("control_url") or "").strip()
    data["slow_mo_ms"] = max(0, int(data.get("slow_mo_ms") or 0))
    data["page_timeout_ms"] = max(10000, int(data.get("page_timeout_ms") or 45000))
    data["dry_run"] = bool(data.get("dry_run", False))
    data["under_approval_page_size"] = max(1, min(200, int(data.get("under_approval_page_size") or 100)))
    data["under_approval_max_pages"] = max(1, min(100, int(data.get("under_approval_max_pages") or 20)))
    report_path = Path(str(data.get("under_approval_report_path") or "under_approval_reports.local.json"))
    if not report_path.is_absolute():
        report_path = base_dir / report_path
    data["under_approval_report_path"] = str(report_path.resolve())
    if not data["server_url"]:
        raise ValueError("server_url is empty")
    if not data["agent_token"] or "PASTE_" in data["agent_token"]:
        raise ValueError("agent_token is empty")
    return data


def api_post(cfg: dict[str, Any], path: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = cfg["server_url"] + path
    resp = requests.post(url, json=payload, timeout=30)
    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text}
    if resp.status_code >= 400:
        raise RuntimeError(f"POST {path} failed HTTP {resp.status_code}: {body}")
    return body


def send_heartbeat(cfg: dict[str, Any], *, status: str = "online", load: float = 0.0) -> dict[str, Any]:
    capabilities = [
        "gsp.status_check",
        "gsp.status_api" if cfg.get("gsp_query_mode") == "api" else "gsp.browser_status_checker",
    ]
    if cfg.get("gsp_query_mode") == "api":
        capabilities.append("gsp.under_approval_scan")
    payload = {
        "token": cfg["agent_token"],
        "backend_id": cfg["backend_id"],
        "display_name": cfg["backend_display_name"],
        "status": status,
        "load": max(0.0, min(1.0, float(load))),
        "capabilities": capabilities,
        "metadata": {
            "query_mode": cfg.get("gsp_query_mode"),
            "headless": bool(cfg.get("headless")),
            "edge_channel": cfg.get("edge_channel"),
            "gsp_base_url": cfg.get("gsp_base_url"),
            "control_url": cfg.get("control_url"),
            "listen_host": cfg.get("listen_host"),
            "listen_port": cfg.get("listen_port"),
        },
    }
    return api_post(cfg, "/api/agent/tool-backends/heartbeat", payload)


def fetch_queue(cfg: dict[str, Any], *, limit: int | None = None, sheet: str | None = None) -> list[dict[str, Any]]:
    payload = {"token": cfg["agent_token"], "limit": limit or cfg["max_tasks"]}
    sheet_filter = normalize_sheet_filter(sheet if sheet is not None else cfg.get("sheet"))
    if sheet_filter:
        payload["sheet"] = sheet_filter
    body = api_post(
        cfg,
        "/api/agent/gsp/status-queue",
        payload,
    )
    return list(body.get("tasks") or [])


def push_result(cfg: dict[str, Any], task: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "token": cfg["agent_token"],
        "run_id": task.get("run_id"),
        "queue_id": task.get("queue_id"),
        "session_id": task.get("session_id"),
        "context_id": task.get("context_id"),
        "skill_call_id": task.get("skill_call_id"),
        "dispatch_id": task.get("dispatch_id"),
        "pla_no": task.get("pla_no") or result.get("pla_no") or "",
        "status": result.get("status") or "",
        "ok": bool(result.get("ok")),
        "sheet": task.get("sheet"),
        "row_index": task.get("row_index"),
        "pn": task.get("pn"),
        "internal_model": task.get("internal_model"),
        "requester": task.get("requester"),
        "sheet_status": task.get("sheet_status"),
        "approval_current_step": result.get("approval_current_step"),
        "approval_taskers": result.get("approval_taskers"),
        "related_rows": task.get("related_rows") or [],
        "checked_at": result.get("checked_at") or utc_now_iso(),
        "source": "windows-desktop-agent",
        "detail": result.get("detail"),
        "error": result.get("error"),
        "raw": result.get("raw"),
    }
    return api_post(cfg, "/api/agent/gsp/status-result", payload)


UNDER_APPROVAL_RESULT_PATH = "/api/agent/gsp/under-approval-result"
GSP_LIST_PATH = "/dahua-b-pricing/priceListApplication/pageByEntity"
GSP_DETAIL_PATH = "/dahua-b-pricing/priceListApplication/getApplicationDetailAndCategory"
UNDER_APPROVAL_STATUSES = {"under approval", "underapproval", "under_approval", "审批中", "待审批"}


def normalize_under_approval_status(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def under_approval_report_id(scan_id: str) -> str:
    """Return a stable opaque id for every replay of the same scan request."""
    normalized = str(scan_id or "").strip()
    if not normalized:
        raise ValueError("scan_id is required")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"gsp-under-approval:{digest}"


class UnderApprovalReportLedger:
    """Small durable outbox; it stores scan evidence, never auth material."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if isinstance(value, dict):
                value.setdefault("schema_version", 1)
                value.setdefault("reports", {})
                return value
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass
        return {"schema_version": 1, "reports": {}}

    def _write(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
        tmp.replace(self.path)

    def get(self, report_id: str) -> dict[str, Any] | None:
        report = (self._read().get("reports") or {}).get(report_id)
        return copy.deepcopy(report) if isinstance(report, dict) else None

    def put_pending(self, report_id: str, payload: dict[str, Any]) -> None:
        data = self._read()
        reports = data.setdefault("reports", {})
        prior = reports.get(report_id) if isinstance(reports.get(report_id), dict) else {}
        reports[report_id] = {
            "status": "pending",
            "created_at": prior.get("created_at") or utc_now_iso(),
            "updated_at": utc_now_iso(),
            "payload": copy.deepcopy(payload),
        }
        self._write(data)

    def mark_acked(self, report_id: str, response: dict[str, Any]) -> None:
        data = self._read()
        report = (data.setdefault("reports", {})).get(report_id)
        if not isinstance(report, dict):
            raise KeyError(report_id)
        report.update(
            {
                "status": "acked",
                "updated_at": utc_now_iso(),
                "acked_at": utc_now_iso(),
                "response": copy.deepcopy(response),
            }
        )
        self._write(data)


class GspStatusChecker:
    def __init__(self, cfg: dict[str, Any], *, login_mode: bool = False):
        self.cfg = cfg
        self.login_mode = login_mode
        self.playwright = None
        self.context = None
        self.page: Page | None = None

    def __enter__(self) -> "GspStatusChecker":
        from playwright.sync_api import sync_playwright

        self.playwright = sync_playwright().start()
        user_data_dir = Path(self.cfg["user_data_dir"])
        user_data_dir.mkdir(parents=True, exist_ok=True)
        self.context = self.playwright.chromium.launch_persistent_context(
            str(user_data_dir),
            channel=self.cfg["edge_channel"],
            headless=False if self.login_mode else self.cfg["headless"],
            slow_mo=self.cfg["slow_mo_ms"],
            args=["--start-minimized"] if not self.cfg["headless"] and not self.login_mode else [],
        )
        self.context.set_default_timeout(self.cfg["page_timeout_ms"])
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.context:
            self.context.close()
        if self.playwright:
            self.playwright.stop()

    def login(self) -> None:
        assert self.page is not None
        self.page.goto(self.cfg["gsp_url"], wait_until="domcontentloaded")
        print("A dedicated Edge profile is open. Log in to GSP there, then press Enter here.")
        input()
        self.page.reload(wait_until="domcontentloaded")
        print(f"Saved profile at: {self.cfg['user_data_dir']}")

    def query_status(self, pla_no: str) -> dict[str, Any]:
        assert self.page is not None
        page = self.page
        pla_no = str(pla_no or "").strip()
        if not pla_no:
            return {"ok": False, "pla_no": pla_no, "status": "", "error": "empty PLA"}

        try:
            page.goto(self.cfg["gsp_url"], wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except playwright_timeout_error():
                pass
            page.wait_for_timeout(1500)
            if self._looks_like_login_page(page):
                return {
                    "ok": False,
                    "pla_no": pla_no,
                    "status": "LOGIN_REQUIRED",
                    "error": "GSP login is required in the dedicated Edge profile",
                    "checked_at": utc_now_iso(),
                }

            self._fill_pla_no(page, pla_no)
            self._click_search(page)
            page.wait_for_timeout(2500)
            extracted = self._extract_status(page, pla_no)
            extracted["checked_at"] = utc_now_iso()
            return extracted
        except Exception as err:
            return {
                "ok": False,
                "pla_no": pla_no,
                "status": "ERROR",
                "error": f"{type(err).__name__}: {err}",
                "checked_at": utc_now_iso(),
            }

    def _looks_like_login_page(self, page: Page) -> bool:
        url = page.url.lower()
        if any(x in url for x in ("login", "sso", "auth")):
            return True
        text = page.locator("body").inner_text(timeout=5000).lower()
        return any(x in text for x in ("login", "sign in", "password", "登录"))

    def _fill_pla_no(self, page: Page, pla_no: str) -> None:
        found = page.evaluate(
            """
            () => {
              const visible = (el) => {
                const style = window.getComputedStyle(el);
                const box = el.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && box.width > 10 && box.height > 10;
              };
              const inputs = Array.from(document.querySelectorAll('input')).filter(visible);
              for (const input of inputs) {
                const text = [
                  input.placeholder,
                  input.name,
                  input.id,
                  input.getAttribute('aria-label')
                ].join(' ').toLowerCase();
                if (text.includes('pla')) {
                  input.setAttribute('data-dahua-agent-pla-input', '1');
                  return true;
                }
              }

              const labels = Array.from(document.querySelectorAll('label,span,div,p'))
                .filter((el) => /PLA\\s*NO\\.?/i.test(el.textContent || '') && visible(el));
              for (const label of labels) {
                const lb = label.getBoundingClientRect();
                let best = null;
                let bestScore = Infinity;
                for (const input of inputs) {
                  const ib = input.getBoundingClientRect();
                  const dx = Math.max(0, ib.left - lb.right);
                  const dy = Math.abs((ib.top + ib.bottom) / 2 - (lb.top + lb.bottom) / 2);
                  const score = dx + dy * 3;
                  if (ib.left >= lb.left && dy < 80 && score < bestScore) {
                    best = input;
                    bestScore = score;
                  }
                }
                if (best) {
                  best.setAttribute('data-dahua-agent-pla-input', '1');
                  return true;
                }
              }
              return false;
            }
            """
        )
        if not found:
            raise RuntimeError("PLA NO input was not found")
        field = page.locator("[data-dahua-agent-pla-input='1']").first()
        field.click()
        field.fill(pla_no)

    def _click_search(self, page: Page) -> None:
        candidates = [
            page.get_by_role("button", name=re.compile(r"Search|Query|查询|搜索", re.I)),
            page.locator("button:has-text('Search')"),
            page.locator("button:has-text('Query')"),
            page.locator("button:has-text('查询')"),
            page.locator("button:has-text('搜索')"),
            page.locator("button[type='submit']"),
        ]
        for locator in candidates:
            try:
                if locator.count() > 0:
                    locator.first.click()
                    return
            except Exception:
                continue
        clicked = page.evaluate(
            """
            () => {
              const nodes = Array.from(document.querySelectorAll('button,span,a'));
              const el = nodes.find((x) => /Search|Query|查询|搜索/i.test(x.textContent || ''));
              if (!el) return false;
              el.click();
              return true;
            }
            """
        )
        if not clicked:
            raise RuntimeError("Search button was not found")

    def _extract_status(self, page: Page, pla_no: str) -> dict[str, Any]:
        try:
            page.locator(f"text={pla_no}").first.wait_for(timeout=15000)
        except playwright_timeout_error():
            pass

        result = page.evaluate(
            """
            (plaNo) => {
              const clean = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
              const normalize = (s) => clean(s).toLowerCase();
              const statusHeader = (x) => {
                const n = normalize(x);
                return n === 'status' || n.includes('status') || clean(x).includes('状态');
              };
              const statusValue = (x) => {
                const text = clean(x);
                const match = text.match(/(Approved|Unapproved|Rejected|Pending|Draft|Submitted|Submit|Processing|Completed|Closed|Canceled|Cancelled|In Approval|审批中|未审批|已审批|审批通过|已批准|驳回|待处理|待审批|草稿|已提交|进行中|已完成|已关闭|已取消)/i);
                return match ? match[0] : '';
              };
              const readRows = (root, rowSelector, cellSelector, headerSelector) => {
                const headerCells = Array.from(root.querySelectorAll(headerSelector));
                const headers = headerCells.map((x) => clean(x.textContent));
                const statusIndex = headers.findIndex(statusHeader);
                const rows = Array.from(root.querySelectorAll(rowSelector));
                for (const row of rows) {
                  const text = clean(row.textContent);
                  if (!text.includes(plaNo)) continue;
                  const cells = Array.from(row.querySelectorAll(cellSelector)).map((x) => clean(x.textContent));
                  let status = '';
                  if (statusIndex >= 0 && statusIndex < cells.length) status = cells[statusIndex];
                  if (!status) status = cells.map(statusValue).find(Boolean) || '';
                  return { found: true, status, headers, cells, rowText: text };
                }
                return null;
              };
              const tableEls = Array.from(document.querySelectorAll('table'));
              for (const table of tableEls) {
                const found = readRows(table, 'tbody tr, tr', 'td,th', 'thead th, thead td, tr:first-child th, tr:first-child td');
                if (found) return found;
              }
              const grids = Array.from(document.querySelectorAll('[role="table"],[role="grid"],.ant-table,.el-table,.vxe-table'));
              for (const grid of grids) {
                const found = readRows(grid, '[role="row"],.ant-table-row,.el-table__row,.vxe-body--row', '[role="cell"],[role="gridcell"],td,th,.ant-table-cell,.el-table__cell,.vxe-body--column', '[role="columnheader"],th,.ant-table-thead .ant-table-cell,.el-table__header th,.vxe-header--column');
                if (found) return found;
              }
              const plaNodes = Array.from(document.querySelectorAll('td,div,span,p')).filter((x) => clean(x.textContent).includes(plaNo));
              for (const node of plaNodes) {
                let cur = node;
                for (let depth = 0; cur && depth < 8; depth += 1, cur = cur.parentElement) {
                  const text = clean(cur.textContent);
                  if (!text.includes(plaNo)) continue;
                  const status = statusValue(text);
                  if (status) {
                    return { found: true, status, headers: [], cells: [], rowText: text };
                  }
                }
              }
              return { found: false, status: '', headers: [], cells: [], rowText: '' };
            }
            """,
            pla_no,
        )
        if not result.get("found"):
            return {
                "ok": False,
                "pla_no": pla_no,
                "status": "NOT_FOUND",
                "detail": "No result row found after search",
                "raw": result,
            }
        return {
            "ok": True,
            "pla_no": pla_no,
            "status": result.get("status") or "UNKNOWN",
            "detail": result.get("rowText") or "",
            "raw": result,
        }


class GspApiStatusChecker:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json;charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            }
        )

    def __enter__(self) -> "GspApiStatusChecker":
        self._ensure_auth()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.session.close()

    def login(self) -> dict[str, Any]:
        username = str(self.cfg.get("gsp_username") or "").strip()
        password = str(self.cfg.get("gsp_password") or "")
        if not username or not password:
            raise ValueError("gsp_username/gsp_password are required for API login")
        resp = self.session.post(
            self.cfg["gsp_base_url"] + "/dahua-b-usercenter/oauth/token",
            params={
                "grant_type": "password",
                "username": username,
                "password": password,
                "client_id": "client_1",
                "client_secret": "B0123456!AbC",
            },
            timeout=30,
        )
        body = self._json_response(resp)
        token = body.get("access_token")
        if resp.status_code >= 400 or not token:
            msg = body.get("error_description") or body.get("message") or body.get("msg") or body.get("error") or body
            raise RuntimeError(f"GSP API login failed HTTP {resp.status_code}: {msg}")
        auth = {
            "access_token": token,
            "refresh_token": body.get("refresh_token"),
            "token_type": body.get("token_type") or "bearer",
            "expires_in": body.get("expires_in"),
            "saved_at": utc_now_iso(),
        }
        auth_path = Path(self.cfg["gsp_auth_path"])
        auth_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = auth_path.with_suffix(auth_path.suffix + ".tmp")
        tmp.write_text(json.dumps(auth, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(auth_path)
        self._apply_auth(auth)
        return {"ok": True, "auth_path": str(auth_path), "refresh_token_set": bool(auth.get("refresh_token"))}

    def query_status(self, pla_no: str) -> dict[str, Any]:
        pla_no = str(pla_no or "").strip()
        if not pla_no:
            return {"ok": False, "pla_no": pla_no, "status": "", "error": "empty PLA"}
        try:
            return self._query_status(pla_no)
        except RuntimeError as err:
            if "HTTP 401" not in str(err) and "unauthorized" not in str(err).lower():
                raise
            self.login()
            return self._query_status(pla_no)

    def _query_status(self, pla_no: str) -> dict[str, Any]:
        checked_at = utc_now_iso()
        last_body: Any = None
        for country_code in self.cfg["gsp_country_codes"]:
            payload = {
                "pageNum": 1,
                "pageSize": 10,
                "countryCode": country_code,
                "priceListApplicationId": pla_no,
            }
            resp = self._post_readonly_authed(GSP_LIST_PATH, payload)
            body = self._json_response(resp)
            last_body = body
            if resp.status_code == 401:
                raise RuntimeError(f"GSP status query failed HTTP 401: {body}")
            if resp.status_code >= 400:
                continue
            records = self._records_from_body(body)
            matched = [row for row in records if str(row.get("priceListApplicationId") or "").strip() == pla_no]
            if matched:
                row = matched[0]
                detail_body = self._fetch_application_detail(pla_no)
                app_detail = self._application_from_detail(detail_body) or {}
                merged = dict(row)
                merged.update({k: v for k, v in app_detail.items() if v not in (None, "")})
                status = str(merged.get("status") or "UNKNOWN").strip() or "UNKNOWN"
                current_step = str(merged.get("currentStep") or "").strip()
                taskers = str(merged.get("taskers") or "").strip()
                detail_parts = [
                    str(merged.get("priceListApplicationId") or pla_no),
                    str(merged.get("countryName") or country_code),
                    str(merged.get("priceListType") or ""),
                    str(merged.get("clientName") or ""),
                ]
                if current_step:
                    detail_parts.append(f"current_step={current_step}")
                if taskers:
                    detail_parts.append(f"taskers={taskers}")
                return {
                    "ok": True,
                    "pla_no": pla_no,
                    "status": status,
                    "detail": " | ".join([x for x in detail_parts if x]),
                    "approval_current_step": current_step,
                    "approval_taskers": taskers,
                    "checked_at": checked_at,
                    "raw": {
                        "country_code": country_code,
                        "record": row,
                        "detail": detail_body,
                    },
                }
        return {
            "ok": False,
            "pla_no": pla_no,
            "status": "NOT_FOUND",
            "detail": f"No GSP API row found in countries: {', '.join(self.cfg['gsp_country_codes'])}",
            "checked_at": checked_at,
            "raw": last_body,
        }

    def _ensure_auth(self) -> None:
        auth_path = Path(self.cfg["gsp_auth_path"])
        if auth_path.exists():
            try:
                auth = json.loads(auth_path.read_text(encoding="utf-8-sig"))
                if auth.get("access_token"):
                    self._apply_auth(auth)
                    return
            except Exception:
                pass
        self.login()

    def _fetch_application_detail(self, pla_no: str) -> dict[str, Any]:
        resp = self._post_readonly_authed(GSP_DETAIL_PATH, {"priceListApplicationId": pla_no})
        body = self._json_response(resp)
        if resp.status_code == 401:
            raise RuntimeError(f"GSP detail query failed HTTP 401: {body}")
        if resp.status_code >= 400:
            return {"http_status": resp.status_code, "body": body}
        return body

    def _post_readonly(self, path: str, payload: dict[str, Any]) -> requests.Response:
        """Hard gate for status operations: no GSP create/save/workflow endpoint is callable here."""
        if path not in {GSP_LIST_PATH, GSP_DETAIL_PATH}:
            raise PermissionError(f"GSP write/non-status endpoint is forbidden: {path}")
        return self.session.post(self.cfg["gsp_base_url"] + path, json=payload, timeout=30)

    def _post_readonly_authed(self, path: str, payload: dict[str, Any]) -> requests.Response:
        response = self._post_readonly(path, payload)
        if response.status_code == 401:
            self.login()
            response = self._post_readonly(path, payload)
        return response

    def scan_under_approval(self, *, limit: int = 200, country_code: str = "") -> dict[str, Any]:
        """Read current Under Approval applications and enrich approval ownership.

        The list API filter is sent as an optimization, but every returned row is
        filtered again locally. This prevents a server that ignores ``status``
        from leaking unrelated applications into a scan report.
        """
        limit = max(1, min(500, int(limit or 200)))
        page_size = max(1, min(limit, int(self.cfg.get("under_approval_page_size") or 100)))
        max_pages = max(1, min(100, int(self.cfg.get("under_approval_max_pages") or 20)))
        countries = [str(country_code).strip()] if str(country_code or "").strip() else list(self.cfg["gsp_country_codes"])
        items_by_pla: dict[str, dict[str, Any]] = {}
        pages_scanned = 0
        checked_at = utc_now_iso()

        for country in countries:
            country_records_seen = 0
            for page_num in range(1, max_pages + 1):
                payload = {
                    "pageNum": page_num,
                    "pageSize": page_size,
                    "countryCode": country,
                    "status": "Under Approval",
                }
                resp = self._post_readonly_authed(GSP_LIST_PATH, payload)
                body = self._json_response(resp)
                if resp.status_code == 401:
                    raise RuntimeError(f"GSP under-approval scan failed HTTP 401: {body}")
                if resp.status_code >= 400:
                    raise RuntimeError(f"GSP under-approval scan failed HTTP {resp.status_code}: {body}")
                pages_scanned += 1
                records = self._records_from_body(body)
                country_records_seen += len(records)
                total_records = self._total_from_body(body)
                for row in records:
                    if normalize_under_approval_status(row.get("status")) not in UNDER_APPROVAL_STATUSES:
                        continue
                    pla_no = str(row.get("priceListApplicationId") or "").strip()
                    if not pla_no or pla_no in items_by_pla:
                        continue
                    detail_body = self._fetch_application_detail(pla_no)
                    detail = self._application_from_detail(detail_body) or {}
                    merged = dict(row)
                    merged.update({key: value for key, value in detail.items() if value not in (None, "")})
                    # Detail is authoritative; refuse a row that changed state during the scan.
                    if normalize_under_approval_status(merged.get("status")) not in UNDER_APPROVAL_STATUSES:
                        continue
                    items_by_pla[pla_no] = self._under_approval_item(merged, country, checked_at)
                    if len(items_by_pla) >= limit:
                        break
                if len(items_by_pla) >= limit:
                    break
                if not records:
                    break
                if total_records is not None and country_records_seen >= total_records:
                    break
                if total_records is None and len(records) < page_size:
                    break
            if len(items_by_pla) >= limit:
                break

        items = sorted(items_by_pla.values(), key=lambda item: str(item.get("pla_no") or ""))
        return {
            "ok": True,
            "items": items,
            "total": len(items),
            "checked_at": checked_at,
            "raw": {"countries": countries, "pages_scanned": pages_scanned, "limit": limit},
        }

    def _under_approval_item(self, record: dict[str, Any], country: str, checked_at: str) -> dict[str, Any]:
        return {
            "pla_no": str(record.get("priceListApplicationId") or "").strip(),
            "status": str(record.get("status") or "Under Approval").strip(),
            "approval_current_step": str(record.get("currentStep") or "").strip(),
            "approval_taskers": str(record.get("taskers") or "").strip(),
            "requester": str(
                record.get("requester")
                or record.get("applicantName")
                or record.get("creatorName")
                or ""
            ).strip(),
            "pn": str(record.get("partNum") or record.get("pn") or "").strip(),
            "created_at": str(record.get("createTime") or record.get("createdAt") or "").strip(),
            "country": str(record.get("countryCode") or record.get("countryName") or country).strip(),
            "checked_at": checked_at,
        }

    def _application_from_detail(self, body: dict[str, Any]) -> dict[str, Any] | None:
        data = body.get("data")
        if not isinstance(data, dict):
            return None
        app = data.get("priceListApplication")
        return app if isinstance(app, dict) else None

    def _apply_auth(self, auth: dict[str, Any]) -> None:
        self.session.headers.update({"Authorization": "Bearer " + str(auth.get("access_token") or "")})
        username = str(self.cfg.get("gsp_username") or "").strip()
        if username:
            self.session.headers.update({"apm-user-no": username})

    def _json_response(self, resp: requests.Response) -> dict[str, Any]:
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text}
        return body if isinstance(body, dict) else {"data": body}

    def _records_from_body(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        data = body.get("data")
        if isinstance(data, dict):
            records = data.get("records") or data.get("list") or data.get("rows") or data.get("data") or []
        elif isinstance(data, list):
            records = data
        else:
            records = []
        return [x for x in records if isinstance(x, dict)]

    def _total_from_body(self, body: dict[str, Any]) -> int | None:
        data = body.get("data")
        if not isinstance(data, dict):
            return None
        for key in ("total", "totalCount", "count"):
            value = data.get(key)
            try:
                return max(0, int(value)) if value is not None else None
            except (TypeError, ValueError):
                continue
        return None


def push_under_approval_report(cfg: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    transport = copy.deepcopy(payload)
    transport["token"] = cfg["agent_token"]
    return api_post(cfg, UNDER_APPROVAL_RESULT_PATH, transport)


def _build_under_approval_report(
    request_payload: dict[str, Any],
    scan_result: dict[str, Any],
) -> dict[str, Any]:
    scan_id = str(request_payload.get("scan_id") or "").strip()
    report_id = under_approval_report_id(scan_id)
    items = list(scan_result.get("items") or [])
    return {
        "scan_id": scan_id,
        "run_id": str(request_payload.get("run_id") or "").strip() or None,
        "session_id": str(request_payload.get("session_id") or "").strip() or None,
        "report_id": report_id,
        "ok": bool(scan_result.get("ok")),
        "items": items,
        "total": len(items),
        "checked_at": scan_result.get("checked_at") or utc_now_iso(),
        "source": "windows-desktop-agent",
        "error": scan_result.get("error"),
        "raw": scan_result.get("raw") or {},
    }


def run_under_approval_scan(
    cfg: dict[str, Any],
    request_payload: dict[str, Any],
    *,
    no_push: bool = False,
    checker_factory: Any = GspApiStatusChecker,
) -> dict[str, Any]:
    """Run or replay one server-issued scan using a durable idempotent outbox."""
    scan_id = str(request_payload.get("scan_id") or "").strip()
    if not scan_id:
        raise ValueError("scan_id is required for scan_under_approval")
    report_id = under_approval_report_id(scan_id)
    ledger = UnderApprovalReportLedger(Path(cfg["under_approval_report_path"]))
    prior = ledger.get(report_id)
    if prior and isinstance(prior.get("payload"), dict):
        report = copy.deepcopy(prior["payload"])
        if prior.get("status") == "acked":
            return {
                "ok": bool(report.get("ok")),
                "replayed": True,
                "already_acked": True,
                "report": report,
                "callback": prior.get("response") or {},
            }
    else:
        limit = max(1, min(500, int(request_payload.get("limit") or cfg.get("max_tasks") or 200)))
        country = str(request_payload.get("country") or "").strip()
        if cfg.get("gsp_query_mode") != "api":
            raise ValueError("scan_under_approval requires gsp_query_mode=api")
        try:
            with checker_factory(cfg) as checker:
                scan_result = checker.scan_under_approval(limit=limit, country_code=country)
        except Exception as exc:
            scan_result = {
                "ok": False,
                "items": [],
                "checked_at": utc_now_iso(),
                "error": f"{type(exc).__name__}: {str(exc)[:1000]}",
                "raw": {"country": country, "limit": limit},
            }
        report = _build_under_approval_report(request_payload, scan_result)

    if no_push or cfg.get("dry_run"):
        # A dry-run must never leave an outbox entry that a later process could send.
        return {
            "ok": bool(report.get("ok")),
            "report": report,
            "callback": {"ok": True, "skipped": True, "reason": "dry_run/no_push"},
        }

    if not prior:
        ledger.put_pending(report_id, report)
    response = push_under_approval_report(cfg, report)
    ledger.mark_acked(report_id, response)
    return {
        "ok": bool(report.get("ok")),
        "replayed": bool(prior),
        "report": report,
        "callback": response,
    }


def print_queue(tasks: list[dict[str, Any]]) -> None:
    print(json.dumps({"event": "queue", "count": len(tasks)}, ensure_ascii=False))
    for task in tasks:
        print(
            json.dumps(
                {
                    "event": "queue_item",
                    "pla_no": task.get("pla_no"),
                    "sheet": task.get("sheet"),
                    "row_index": task.get("row_index"),
                    "pn": task.get("pn"),
                    "sheet_status": task.get("sheet_status"),
                    "requester": task.get("requester"),
                },
                ensure_ascii=False,
            )
        )


def run_once(cfg: dict[str, Any], *, no_push: bool = False, sheet: str | None = None) -> None:
    try:
        heartbeat = send_heartbeat(cfg, load=0.1)
        print(json.dumps({"event": "heartbeat", "ok": heartbeat.get("ok")}, ensure_ascii=False))
    except Exception as err:
        print(json.dumps({"event": "heartbeat_failed", "error": f"{type(err).__name__}: {err}"}, ensure_ascii=False))
    sheet_filter = normalize_sheet_filter(sheet if sheet is not None else cfg.get("sheet"))
    tasks = fetch_queue(cfg, sheet=sheet_filter)
    print(json.dumps({"event": "queue", "count": len(tasks), "sheet": sheet_filter or None}, ensure_ascii=False))
    if not tasks:
        return
    checker_cls = GspApiStatusChecker if cfg.get("gsp_query_mode") == "api" else GspStatusChecker
    with checker_cls(cfg) as checker:
        for task in tasks:
            pla_no = task.get("pla_no") or ""
            result = checker.query_status(pla_no)
            if no_push or cfg.get("dry_run"):
                saved = {"ok": True, "skipped": True, "reason": "dry_run/no_push"}
            else:
                saved = push_result(cfg, task, result)
            print(
                json.dumps(
                    {
                        "event": "checked",
                        "pla_no": pla_no,
                        "status": result.get("status"),
                        "approval_current_step": result.get("approval_current_step"),
                        "approval_taskers": result.get("approval_taskers"),
                        "ok": result.get("ok"),
                        "saved": saved,
                    },
                    ensure_ascii=False,
                )
            )


def query_one_pla(cfg: dict[str, Any], pla_no: str) -> None:
    checker_cls = GspApiStatusChecker if cfg.get("gsp_query_mode") == "api" else GspStatusChecker
    with checker_cls(cfg) as checker:
        result = checker.query_status(pla_no)
        print(
            json.dumps(
                {
                    "event": "pla_checked",
                    "pla_no": pla_no,
                    "status": result.get("status"),
                    "approval_current_step": result.get("approval_current_step"),
                    "approval_taskers": result.get("approval_taskers"),
                    "ok": result.get("ok"),
                    "detail": result.get("detail"),
                },
                ensure_ascii=False,
            )
        )


def _control_auth_ok(cfg: dict[str, Any], headers: Any) -> bool:
    token = str(cfg.get("agent_token") or "")
    candidates = [
        str(headers.get("X-Agent-Token") or ""),
        str(headers.get("X-Dahua-Agent-Token") or ""),
        str(headers.get("Authorization") or ""),
    ]
    for value in candidates:
        value = value.strip()
        if value == token:
            return True
        if value.lower().startswith("bearer ") and value[7:].strip() == token:
            return True
    return False


def serve_control(cfg: dict[str, Any]) -> None:
    state: dict[str, Any] = {
        "running": False,
        "started_at": "",
        "finished_at": "",
        "last_request": {},
        "last_operation": "",
        "last_report_id": "",
        "last_result_count": 0,
        "last_error": "",
    }
    lock = threading.Lock()

    def write_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def worker(request_payload: dict[str, Any]) -> None:
        run_cfg = dict(cfg)
        if request_payload.get("max_tasks") is not None:
            try:
                run_cfg["max_tasks"] = max(1, min(500, int(request_payload.get("max_tasks"))))
            except Exception:
                pass
        sheet = normalize_sheet_filter(request_payload.get("sheet"))
        no_push = bool(request_payload.get("no_push", False))
        operation = str(request_payload.get("operation") or "check_sheet_pla").strip().lower()
        with lock:
            state.update(
                {
                    "running": True,
                    "started_at": utc_now_iso(),
                    "finished_at": "",
                    "last_request": request_payload,
                    "last_operation": operation,
                    "last_report_id": "",
                    "last_result_count": 0,
                    "last_error": "",
                }
            )
        try:
            try:
                send_heartbeat(run_cfg, load=1.0)
            except Exception as err:
                print(json.dumps({"event": "control_heartbeat_failed", "error": f"{type(err).__name__}: {err}"}, ensure_ascii=False))
            if operation == "scan_under_approval":
                result = run_under_approval_scan(run_cfg, request_payload, no_push=no_push)
                report = result.get("report") if isinstance(result, dict) else {}
                with lock:
                    state["last_report_id"] = str((report or {}).get("report_id") or "")
                    state["last_result_count"] = int((report or {}).get("total") or 0)
                print(
                    json.dumps(
                        {
                            "event": "under_approval_scan_completed",
                            "scan_id": request_payload.get("scan_id"),
                            "report_id": (report or {}).get("report_id"),
                            "ok": result.get("ok"),
                            "total": (report or {}).get("total"),
                            "replayed": bool(result.get("replayed")),
                            "callback": result.get("callback"),
                        },
                        ensure_ascii=False,
                    )
                )
            elif operation == "check_sheet_pla":
                run_once(run_cfg, no_push=no_push, sheet=sheet)
            else:
                raise ValueError(f"unsupported operation: {operation}")
        except Exception as err:
            with lock:
                state["last_error"] = f"{type(err).__name__}: {err}"
            print(json.dumps({"event": "control_run_failed", "error": state["last_error"]}, ensure_ascii=False))
        finally:
            try:
                send_heartbeat(run_cfg, load=0.0)
            except Exception as err:
                print(json.dumps({"event": "control_heartbeat_failed", "error": f"{type(err).__name__}: {err}"}, ensure_ascii=False))
            with lock:
                state["running"] = False
                state["finished_at"] = utc_now_iso()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            print(json.dumps({"event": "control_http", "message": format % args}, ensure_ascii=False))

        def do_GET(self) -> None:
            if self.path.rstrip("/") != "/health":
                write_json(self, 404, {"ok": False, "error": "not found"})
                return
            with lock:
                snapshot = dict(state)
            write_json(self, 200, {"ok": True, **snapshot})

        def do_POST(self) -> None:
            if self.path.rstrip("/") != "/run-once":
                write_json(self, 404, {"ok": False, "error": "not found"})
                return
            if not _control_auth_ok(cfg, self.headers):
                write_json(self, 403, {"ok": False, "error": "invalid token"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                write_json(self, 400, {"ok": False, "error": "invalid json"})
                return
            if not isinstance(payload, dict):
                write_json(self, 400, {"ok": False, "error": "payload must be object"})
                return
            with lock:
                if state.get("running"):
                    write_json(self, 409, {"ok": False, "error": "agent is already running", **state})
                    return
                state["last_request"] = payload
            threading.Thread(target=worker, args=(payload,), daemon=True, name="gsp-control-run-once").start()
            write_json(self, 202, {"ok": True, "accepted": True, "running": True})

    try:
        heartbeat = send_heartbeat(cfg, load=0.0)
        print(json.dumps({"event": "control_heartbeat", "ok": heartbeat.get("ok")}, ensure_ascii=False))
    except Exception as err:
        print(json.dumps({"event": "control_heartbeat_failed", "error": f"{type(err).__name__}: {err}"}, ensure_ascii=False))

    server = ThreadingHTTPServer((cfg["listen_host"], int(cfg["listen_port"])), Handler)
    print(
        json.dumps(
            {
                "event": "control_server_started",
                "listen_host": cfg["listen_host"],
                "listen_port": cfg["listen_port"],
                "control_url": cfg.get("control_url"),
            },
            ensure_ascii=False,
        )
    )
    server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--serve", action="store_true", help="run a local HTTP control service for backend-triggered checks")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--login", action="store_true")
    parser.add_argument("--queue-only", action="store_true", help="fetch and print backend queue without opening GSP")
    parser.add_argument("--no-push", action="store_true", help="query GSP but do not POST status results")
    parser.add_argument("--max-tasks", type=int, default=None, help="override config max_tasks for this run")
    parser.add_argument("--sheet", default=None, help="only fetch pending PLA rows from this sheet, e.g. 2026.07")
    parser.add_argument("--pla", default="", help="query one PLA directly without reading backend queue")
    parser.add_argument("--scan-under-approval", action="store_true", help="read current GSP Under Approval rows")
    parser.add_argument("--scan-id", default="", help="stable server scan id; generated locally when omitted")
    parser.add_argument("--country", default="", help="optional GSP country code for an Under Approval scan")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    if args.max_tasks is not None:
        cfg["max_tasks"] = max(1, min(500, int(args.max_tasks)))
    if args.sheet is not None:
        cfg["sheet"] = normalize_sheet_filter(args.sheet)
    if args.login:
        if cfg.get("gsp_query_mode") == "api":
            with GspApiStatusChecker(cfg) as checker:
                print(json.dumps({"event": "api_login", **checker.login()}, ensure_ascii=False))
        else:
            with GspStatusChecker(cfg, login_mode=True) as checker:
                checker.login()
        return 0
    if args.serve:
        serve_control(cfg)
        return 0
    if args.queue_only:
        try:
            send_heartbeat(cfg, load=0.0)
        except Exception:
            pass
        print_queue(fetch_queue(cfg, sheet=cfg.get("sheet")))
        return 0
    if args.pla:
        try:
            send_heartbeat(cfg, load=0.2)
        except Exception:
            pass
        query_one_pla(cfg, args.pla)
        return 0
    if args.scan_under_approval:
        result = run_under_approval_scan(
            cfg,
            {
                "operation": "scan_under_approval",
                "scan_id": args.scan_id or f"manual-{uuid.uuid4().hex}",
                "limit": cfg["max_tasks"],
                "country": args.country,
            },
            no_push=bool(args.no_push),
        )
        report = result.get("report") or {}
        print(
            json.dumps(
                {
                    "event": "under_approval_scan_completed",
                    "scan_id": report.get("scan_id"),
                    "report_id": report.get("report_id"),
                    "ok": report.get("ok"),
                    "total": report.get("total"),
                    "error": report.get("error"),
                    "callback": result.get("callback"),
                },
                ensure_ascii=False,
            )
        )
        return 0 if report.get("ok") else 1

    while True:
        run_once(cfg, no_push=bool(args.no_push), sheet=cfg.get("sheet"))
        if args.once or cfg["poll_interval_seconds"] <= 0:
            return 0
        time.sleep(cfg["poll_interval_seconds"])


if __name__ == "__main__":
    sys.exit(main())
