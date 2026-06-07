from pathlib import Path
import sys
import tempfile
import unittest

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


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
class StatusExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._playwright = sync_playwright().start()
        cls._browser = cls._playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._browser.close()
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
