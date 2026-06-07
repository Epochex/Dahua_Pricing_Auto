from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    data["server_url"] = str(data.get("server_url") or "").rstrip("/")
    data["agent_token"] = str(data.get("agent_token") or "")
    data["gsp_url"] = str(data.get("gsp_url") or "https://gsp.dahuasecurity.com/#/pricing/application/list")
    data["edge_channel"] = str(data.get("edge_channel") or "msedge")
    data["user_data_dir"] = str(data.get("user_data_dir") or "C:/DahuaPricingAgent/edge-profile")
    data["headless"] = bool(data.get("headless", True))
    data["max_tasks"] = max(1, min(500, int(data.get("max_tasks") or 5)))
    data["poll_interval_seconds"] = max(0, int(data.get("poll_interval_seconds") or 0))
    data["slow_mo_ms"] = max(0, int(data.get("slow_mo_ms") or 0))
    data["page_timeout_ms"] = max(10000, int(data.get("page_timeout_ms") or 45000))
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


def fetch_queue(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    body = api_post(
        cfg,
        "/api/agent/gsp/status-queue",
        {"token": cfg["agent_token"], "limit": cfg["max_tasks"]},
    )
    return list(body.get("tasks") or [])


def push_result(cfg: dict[str, Any], task: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "token": cfg["agent_token"],
        "pla_no": task.get("pla_no") or result.get("pla_no") or "",
        "status": result.get("status") or "",
        "ok": bool(result.get("ok")),
        "sheet": task.get("sheet"),
        "row_index": task.get("row_index"),
        "pn": task.get("pn"),
        "requester": task.get("requester"),
        "checked_at": result.get("checked_at") or utc_now_iso(),
        "source": "windows-desktop-agent",
        "detail": result.get("detail"),
        "error": result.get("error"),
        "raw": result.get("raw"),
    }
    return api_post(cfg, "/api/agent/gsp/status-result", payload)


class GspStatusChecker:
    def __init__(self, cfg: dict[str, Any], *, login_mode: bool = False):
        self.cfg = cfg
        self.login_mode = login_mode
        self.playwright = None
        self.context = None
        self.page: Page | None = None

    def __enter__(self) -> "GspStatusChecker":
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
            page.get_by_role("button", name=re.compile(r"Search|查询", re.I)),
            page.locator("button:has-text('Search')"),
            page.locator("button:has-text('查询')"),
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
              const el = nodes.find((x) => /Search|查询/i.test(x.textContent || ''));
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
        except PlaywrightTimeoutError:
            pass

        result = page.evaluate(
            """
            (plaNo) => {
              const clean = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
              const normalize = (s) => clean(s).toLowerCase();
              const tableEls = Array.from(document.querySelectorAll('table'));
              for (const table of tableEls) {
                const headerCells = Array.from(table.querySelectorAll('thead th, thead td, tr:first-child th, tr:first-child td'));
                const headers = headerCells.map((x) => clean(x.textContent));
                let statusIndex = headers.findIndex((x) => normalize(x) === 'status' || x.includes('状态'));
                const rows = Array.from(table.querySelectorAll('tbody tr, tr'));
                for (const row of rows) {
                  const text = clean(row.textContent);
                  if (!text.includes(plaNo)) continue;
                  const cells = Array.from(row.querySelectorAll('td,th')).map((x) => clean(x.textContent));
                  let status = '';
                  if (statusIndex >= 0 && statusIndex < cells.length) status = cells[statusIndex];
                  if (!status) {
                    status = cells.find((x) => /Approved|Unapproved|Rejected|Pending|Draft|Submit|审批|未审批|驳回|待/i.test(x)) || '';
                  }
                  return { found: true, status, headers, cells, rowText: text };
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


def run_once(cfg: dict[str, Any]) -> None:
    tasks = fetch_queue(cfg)
    print(json.dumps({"event": "queue", "count": len(tasks)}, ensure_ascii=False))
    if not tasks:
        return
    with GspStatusChecker(cfg) as checker:
        for task in tasks:
            pla_no = task.get("pla_no") or ""
            result = checker.query_status(pla_no)
            saved = push_result(cfg, task, result)
            print(
                json.dumps(
                    {
                        "event": "checked",
                        "pla_no": pla_no,
                        "status": result.get("status"),
                        "ok": result.get("ok"),
                        "saved": saved,
                    },
                    ensure_ascii=False,
                )
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--login", action="store_true")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    if args.login:
        with GspStatusChecker(cfg, login_mode=True) as checker:
            checker.login()
        return 0

    while True:
        run_once(cfg)
        if args.once or cfg["poll_interval_seconds"] <= 0:
            return 0
        time.sleep(cfg["poll_interval_seconds"])


if __name__ == "__main__":
    sys.exit(main())
