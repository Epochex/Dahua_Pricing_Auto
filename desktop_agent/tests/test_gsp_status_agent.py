from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - optional local dependency
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import gsp_status_agent as agent  # noqa: E402


class ConfigTests(unittest.TestCase):
    def test_load_config_normalizes_safe_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.json"
            cfg_path.write_text(
                """
                {
                  "server_url": "http://localhost:8000/",
                  "agent_token": "token",
                  "max_tasks": 9999,
                  "dry_run": true
                }
                """,
                encoding="utf-8",
            )
            cfg = agent.load_config(cfg_path)

        self.assertEqual(cfg["server_url"], "http://localhost:8000")
        self.assertEqual(cfg["max_tasks"], 500)
        self.assertTrue(cfg["dry_run"])
        self.assertEqual(cfg["gsp_query_mode"], "api")
        self.assertEqual(cfg["gsp_country_codes"], ["FR"])
        self.assertEqual(cfg["sheet"], "")
        self.assertTrue(cfg["under_approval_report_path"].endswith("under_approval_reports.local.json"))

    def test_load_config_normalizes_sheet_filter(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.json"
            cfg_path.write_text(
                """
                {
                  "server_url": "http://localhost:8000/",
                  "agent_token": "token",
                  "sheet": "2026.7"
                }
                """,
                encoding="utf-8",
            )
            cfg = agent.load_config(cfg_path)

        self.assertEqual(cfg["sheet"], "2026.07")

    def test_load_config_rejects_placeholder_token(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.json"
            cfg_path.write_text(
                '{"server_url":"http://localhost:8000","agent_token":"PASTE_TOKEN"}',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                agent.load_config(cfg_path)

    def test_extracts_application_detail_payload(self) -> None:
        checker = agent.GspApiStatusChecker({"gsp_base_url": "https://gsp.dahuasecurity.com"})
        app = checker._application_from_detail(
            {
                "code": 0,
                "data": {
                    "priceListApplication": {
                        "priceListApplicationId": "PLA20260602141029254",
                        "status": "Under Approval",
                        "currentStep": "Country Product Manager",
                        "taskers": "LEON HOU(30195)",
                    }
                },
            }
        )

        self.assertIsNotNone(app)
        self.assertEqual(app["currentStep"], "Country Product Manager")
        self.assertEqual(app["taskers"], "LEON HOU(30195)")


class FakeResponse:
    def __init__(self, body: dict, status_code: int = 200):
        self._body = body
        self.status_code = status_code
        self.text = ""

    def json(self) -> dict:
        return self._body


class FakeScanSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, *, json: dict, timeout: int) -> FakeResponse:
        self.calls.append((url, json))
        if url.endswith(agent.GSP_LIST_PATH):
            page = int(json["pageNum"])
            records = (
                [
                    {
                        "priceListApplicationId": "PLA-2",
                        "status": "Under Approval",
                        "countryCode": "FR",
                    },
                    {
                        "priceListApplicationId": "PLA-IGNORED",
                        "status": "Approved",
                        "countryCode": "FR",
                    },
                ]
                if page == 1
                else []
            )
            return FakeResponse({"code": 0, "data": {"records": records}})
        if url.endswith(agent.GSP_DETAIL_PATH):
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "priceListApplication": {
                            "priceListApplicationId": json["priceListApplicationId"],
                            "status": "Under Approval",
                            "currentStep": "Country Product Manager",
                            "taskers": "LEON HOU(30195)",
                            "applicantName": "Alice",
                        }
                    },
                }
            )
        raise AssertionError(f"unexpected URL: {url}")


class FakeUnderApprovalChecker:
    calls = 0

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def __enter__(self):
        type(self).calls += 1
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def scan_under_approval(self, *, limit: int, country_code: str) -> dict:
        return {
            "ok": True,
            "items": [
                {
                    "pla_no": "PLA-2",
                    "status": "Under Approval",
                    "approval_current_step": "CPM",
                    "approval_taskers": "Alice",
                    "country": country_code or "FR",
                }
            ],
            "checked_at": "2026-07-16T00:00:00+00:00",
            "raw": {"pages_scanned": 1},
        }


class UnderApprovalScanTests(unittest.TestCase):
    def base_cfg(self, path: Path, *, dry_run: bool = False) -> dict:
        return {
            "server_url": "http://linux",
            "agent_token": "transport-secret",
            "gsp_query_mode": "api",
            "gsp_base_url": "https://gsp.example",
            "gsp_country_codes": ["FR"],
            "under_approval_page_size": 2,
            "under_approval_max_pages": 5,
            "under_approval_report_path": str(path),
            "max_tasks": 200,
            "dry_run": dry_run,
            "gsp_password": "gsp-secret",
        }

    def test_scanner_uses_only_read_endpoints_and_filters_status_locally(self) -> None:
        cfg = self.base_cfg(Path("unused"))
        checker = agent.GspApiStatusChecker(cfg)
        fake = FakeScanSession()
        checker.session = fake

        result = checker.scan_under_approval(limit=10, country_code="FR")

        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["pla_no"], "PLA-2")
        self.assertEqual(result["items"][0]["approval_taskers"], "LEON HOU(30195)")
        paths = [url.removeprefix(cfg["gsp_base_url"]) for url, _payload in fake.calls]
        self.assertTrue(set(paths).issubset({agent.GSP_LIST_PATH, agent.GSP_DETAIL_PATH}))
        list_payload = next(payload for url, payload in fake.calls if url.endswith(agent.GSP_LIST_PATH))
        self.assertEqual(list_payload["status"], "Under Approval")

    def test_readonly_gate_rejects_submission_endpoint(self) -> None:
        checker = agent.GspApiStatusChecker(self.base_cfg(Path("unused")))
        with self.assertRaises(PermissionError):
            checker._post_readonly("/dahua-b-pricing/priceWorkFlow/startPriceWork", {})

    def test_report_id_is_stable_and_contains_no_scan_text(self) -> None:
        first = agent.under_approval_report_id("scan-sensitive-correlation")
        second = agent.under_approval_report_id("scan-sensitive-correlation")
        self.assertEqual(first, second)
        self.assertNotIn("sensitive", first)

    def test_dry_run_scans_but_never_calls_linux_or_creates_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ledger_path = Path(td) / "reports.json"
            cfg = self.base_cfg(ledger_path, dry_run=True)
            with patch.object(agent, "push_under_approval_report") as push:
                result = agent.run_under_approval_scan(
                    cfg,
                    {"scan_id": "scan-dry", "limit": 10, "country": "FR"},
                    checker_factory=FakeUnderApprovalChecker,
                )

            push.assert_not_called()
            self.assertTrue(result["callback"]["skipped"])
            self.assertFalse(ledger_path.exists())

    def test_timeout_reuses_durable_report_without_rescanning(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ledger_path = Path(td) / "reports.json"
            cfg = self.base_cfg(ledger_path)
            request = {"scan_id": "scan-1", "run_id": "run-1", "session_id": "session-1", "limit": 10}
            FakeUnderApprovalChecker.calls = 0

            with patch.object(agent, "push_under_approval_report", side_effect=TimeoutError("lost response")):
                with self.assertRaises(TimeoutError):
                    agent.run_under_approval_scan(cfg, request, checker_factory=FakeUnderApprovalChecker)

            persisted = json.loads(ledger_path.read_text(encoding="utf-8"))
            report_id = agent.under_approval_report_id("scan-1")
            pending_payload = persisted["reports"][report_id]["payload"]
            self.assertEqual(persisted["reports"][report_id]["status"], "pending")
            self.assertNotIn("transport-secret", ledger_path.read_text(encoding="utf-8"))
            self.assertNotIn("gsp-secret", ledger_path.read_text(encoding="utf-8"))

            with patch.object(agent, "push_under_approval_report", return_value={"ok": True, "replayed": True}) as push:
                result = agent.run_under_approval_scan(cfg, request, checker_factory=FakeUnderApprovalChecker)

            self.assertEqual(FakeUnderApprovalChecker.calls, 1)
            self.assertTrue(result["replayed"])
            self.assertEqual(push.call_args.args[1], pending_payload)
            acked = json.loads(ledger_path.read_text(encoding="utf-8"))["reports"][report_id]
            self.assertEqual(acked["status"], "acked")

    def test_callback_adds_token_only_to_transport_copy(self) -> None:
        cfg = self.base_cfg(Path("unused"))
        payload = {"scan_id": "scan-1", "report_id": "report-1", "items": []}
        with patch.object(agent, "api_post", return_value={"ok": True}) as post:
            agent.push_under_approval_report(cfg, payload)
        sent = post.call_args.args[2]
        self.assertEqual(sent["token"], "transport-secret")
        self.assertNotIn("token", payload)

    def test_api_heartbeat_advertises_under_approval_capability(self) -> None:
        cfg = {
            **self.base_cfg(Path("unused")),
            "backend_id": "windows",
            "backend_display_name": "Windows",
            "headless": True,
            "edge_channel": "msedge",
            "control_url": "http://127.0.0.1:8765",
            "listen_host": "127.0.0.1",
            "listen_port": 8765,
        }
        with patch.object(agent, "api_post", return_value={"ok": True}) as post:
            agent.send_heartbeat(cfg)
        payload = post.call_args.args[2]
        self.assertIn("gsp.under_approval_scan", payload["capabilities"])


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
class StatusExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._playwright = sync_playwright().start()
        try:
            cls._browser = cls._playwright.chromium.launch()
        except Exception as exc:  # browser binary is optional on Linux CI/server nodes
            cls._playwright.stop()
            raise unittest.SkipTest(f"playwright browser is unavailable: {type(exc).__name__}") from exc

    @classmethod
    def tearDownClass(cls) -> None:
        if getattr(cls, "_browser", None) is not None:
            cls._browser.close()
        if getattr(cls, "_playwright", None) is not None:
            cls._playwright.stop()

    def _extract(self, html: str, pla_no: str = "PLA20260605143508433") -> dict:
        page = self._browser.new_page()
        try:
            page.set_content(html)
            checker = agent.GspStatusChecker({"gsp_url": "about:blank"})
            return checker._extract_status(page, pla_no)
        finally:
            page.close()

    def test_extracts_status_from_regular_table(self) -> None:
        result = self._extract(
            """
            <table>
              <thead><tr><th>PLA NO.</th><th>Status</th></tr></thead>
              <tbody>
                <tr><td>PLA20260605143508433</td><td>Approved</td></tr>
              </tbody>
            </table>
            """
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "Approved")

    def test_extracts_status_from_div_grid(self) -> None:
        result = self._extract(
            """
            <div role="grid">
              <div role="row">
                <span role="columnheader">PLA NO.</span>
                <span role="columnheader">状态</span>
              </div>
              <div role="row">
                <span role="cell">PLA20260605143508433</span>
                <span role="cell">审批通过</span>
              </div>
            </div>
            """
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "审批通过")


if __name__ == "__main__":
    unittest.main()
