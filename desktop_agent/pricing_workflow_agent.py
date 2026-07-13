from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

import requests

from gsp_status_agent import GspApiStatusChecker, api_post, load_config, utc_now_iso


INSERT_APPLICATION_PATH = "/dahua-b-pricing/priceListApplication/insertPriceListApplication"
SAVE_APPLICATION_PATH = "/dahua-b-pricing/priceListApplication/saveApplicationAndProduct"
CREATE_WORKFLOW_PATH = "/dahua-b-pricing/priceWorkFlow/createPriceWork"
START_WORKFLOW_PATH = "/dahua-b-pricing/priceWorkFlow/startPriceWork"
LIST_APPLICATION_PATH = "/dahua-b-pricing/priceListApplication/pageByEntity"
DETAIL_APPLICATION_PATH = "/dahua-b-pricing/priceListApplication/getApplicationDetailAndCategory"

REQUIRED_SUBMISSION_TEMPLATES = (
    "application",
    "save_application_and_product",
    "create_price_work",
    "start_price_work",
)


def load_workflow_config(path: Path) -> dict[str, Any]:
    cfg = load_config(path)
    base_dir = path.resolve().parent
    cfg["workflow_enabled"] = bool(cfg.get("workflow_enabled", False))
    cfg["gsp_submission_enabled"] = bool(cfg.get("gsp_submission_enabled", False))
    cfg["workflow_poll_interval_seconds"] = max(5, int(cfg.get("workflow_poll_interval_seconds") or 30))
    cfg["workflow_lease_seconds"] = max(30, min(900, int(cfg.get("workflow_lease_seconds") or 120)))
    cfg["workflow_verification_pages"] = max(1, min(20, int(cfg.get("workflow_verification_pages") or 5)))
    ledger_path = Path(str(cfg.get("workflow_ledger_path") or "pricing_workflow_ledger.local.json"))
    if not ledger_path.is_absolute():
        ledger_path = base_dir / ledger_path
    cfg["workflow_ledger_path"] = str(ledger_path.resolve())
    cfg["worker_id"] = str(cfg.get("worker_id") or cfg.get("backend_id") or "windows-gsp-agent").strip()
    return cfg


class WorkflowLedger:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if isinstance(value, dict):
                value.setdefault("schema_version", 1)
                value.setdefault("executions", {})
                value.setdefault("pending_reports", {})
                return value
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass
        return {"schema_version": 1, "executions": {}, "pending_reports": {}}

    def _write(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
        tmp.replace(self.path)

    def execution(self, effect_key: str) -> dict[str, Any]:
        data = self._read()
        execution = (data.get("executions") or {}).get(effect_key) or {}
        return copy.deepcopy(execution)

    def record_stage(self, effect_key: str, stage: str, values: Optional[dict[str, Any]] = None) -> None:
        data = self._read()
        executions = data.setdefault("executions", {})
        execution = executions.setdefault(effect_key, {"effect_key": effect_key, "stages": {}})
        execution.setdefault("stages", {})[stage] = {"completed_at": utc_now_iso(), **dict(values or {})}
        for key, value in (values or {}).items():
            if key in {"pla_no", "workflow_id"} and value:
                execution[key] = value
        execution["updated_at"] = utc_now_iso()
        self._write(data)

    def put_pending_report(self, report_id: str, payload: dict[str, Any]) -> None:
        data = self._read()
        data.setdefault("pending_reports", {})[report_id] = {
            "created_at": utc_now_iso(),
            "payload": copy.deepcopy(payload),
        }
        self._write(data)

    def pending_reports(self) -> list[tuple[str, dict[str, Any]]]:
        data = self._read()
        return [
            (report_id, copy.deepcopy(item.get("payload") or {}))
            for report_id, item in (data.get("pending_reports") or {}).items()
        ]

    def remove_pending_report(self, report_id: str) -> None:
        data = self._read()
        (data.get("pending_reports") or {}).pop(report_id, None)
        self._write(data)


def _recursive_find(value: Any, keys: set[str]) -> Optional[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and item not in (None, ""):
                return str(item).strip()
        for item in value.values():
            found = _recursive_find(item, keys)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _recursive_find(item, keys)
            if found:
                return found
    return None


def _render_template(value: Any, replacements: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: _render_template(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_template(item, replacements) for item in value]
    if isinstance(value, str):
        rendered = value
        for key, replacement in replacements.items():
            rendered = rendered.replace("{{" + key + "}}", str(replacement or ""))
        return rendered
    return value


def _contains_effect_key(record: dict[str, Any], effect_key: str) -> bool:
    fields = (
        "description",
        "comment",
        "remark",
        "remarks",
        "applicationDescription",
        "applicationComment",
    )
    return any(effect_key in str(record.get(field) or "") for field in fields)


def _append_effect_marker(application: dict[str, Any], effect_key: str) -> dict[str, Any]:
    result = copy.deepcopy(application)
    description = str(result.get("description") or "").strip()
    if effect_key not in description:
        result["description"] = (description + " " + effect_key).strip()
    comment = str(result.get("comment") or "").strip()
    if effect_key not in comment:
        result["comment"] = (comment + " " + effect_key).strip()
    return result


def _is_submitted_application(record: dict[str, Any]) -> bool:
    submitted = record.get("isSubmit")
    if submitted in (True, 1, "1", "true", "True", "Y", "yes"):
        return True
    status = str(record.get("status") or "").strip().lower().replace("_", " ")
    return status in {
        "under approval",
        "approved",
        "rejected",
        "审批中",
        "审批通过",
        "已驳回",
    }


def _normalize_approval_outcome(status: Any) -> str:
    text = str(status or "").strip().lower().replace("_", " ")
    if text in {"approved", "审批通过", "已通过"}:
        return "approved"
    if text in {"rejected", "declined", "已驳回", "审批拒绝"}:
        return "rejected"
    if text in {"under approval", "pending", "审批中", "待审批"}:
        return "under_approval"
    return "unknown"


class GspPricingWorkflowClient:
    """GSP workflow client with a durable stage journal.

    The four write endpoint names are taken from the deployed GSP frontend.  The
    payload shapes are not guessed: callers must provide all four templates in
    ``gsp_payload``.  Placeholders ``{{effect_key}}``, ``{{pla_no}}`` and
    ``{{workflow_id}}`` are rendered only after the preceding response is known.
    """

    def __init__(self, cfg: dict[str, Any], ledger: WorkflowLedger):
        self.cfg = cfg
        self.ledger = ledger
        self.checker = GspApiStatusChecker(cfg)

    def __enter__(self) -> "GspPricingWorkflowClient":
        self.checker.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.checker.__exit__(exc_type, exc, tb)

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.checker.session.post(
            self.cfg["gsp_base_url"] + path,
            json=payload,
            timeout=30,
        )
        body = self.checker._json_response(response)
        if response.status_code == 401:
            self.checker.login()
            response = self.checker.session.post(
                self.cfg["gsp_base_url"] + path,
                json=payload,
                timeout=30,
            )
            body = self.checker._json_response(response)
        if response.status_code >= 400:
            raise RuntimeError(f"GSP {path} failed HTTP {response.status_code}")
        code = body.get("code")
        if code not in (None, 0, 200, "0", "200") or body.get("success") is False:
            message = body.get("message") or body.get("msg") or "business response rejected"
            raise RuntimeError(f"GSP {path} rejected: {message}")
        return body

    def _records(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        body = self._post(LIST_APPLICATION_PATH, payload)
        return self.checker._records_from_body(body)

    def _detail(self, pla_no: str) -> dict[str, Any]:
        body = self._post(DETAIL_APPLICATION_PATH, {"priceListApplicationId": pla_no})
        detail = self.checker._application_from_detail(body)
        return detail or {}

    def find_by_effect_key(self, effect_key: str) -> dict[str, Any]:
        successful_queries = 0
        for country_code in self.cfg["gsp_country_codes"]:
            for page_num in range(1, int(self.cfg["workflow_verification_pages"]) + 1):
                records = self._records(
                    {
                        "pageNum": page_num,
                        "pageSize": 50,
                        "countryCode": country_code,
                    }
                )
                successful_queries += 1
                if not records:
                    break
                for record in records:
                    pla_no = str(record.get("priceListApplicationId") or "").strip()
                    detail = self._detail(pla_no) if pla_no and not _contains_effect_key(record, effect_key) else {}
                    merged = dict(record)
                    merged.update({key: value for key, value in detail.items() if value not in (None, "")})
                    if _contains_effect_key(merged, effect_key):
                        execution = self.ledger.execution(effect_key)
                        return {
                            "found": True,
                            "pla_no": pla_no or str(merged.get("priceListApplicationId") or "").strip(),
                            "status": str(merged.get("status") or ""),
                            "submitted": _is_submitted_application(merged),
                            "resumable": bool(execution),
                            "country_code": country_code,
                        }
                if len(records) < 50:
                    break
        return {"found": False, "confirmed_absent": successful_queries > 0, "queries": successful_queries}

    @staticmethod
    def _validate_templates(gsp_payload: dict[str, Any]) -> Optional[str]:
        missing = [key for key in REQUIRED_SUBMISSION_TEMPLATES if not isinstance(gsp_payload.get(key), dict)]
        if missing:
            return "missing object template(s): " + ", ".join(missing)
        placeholders = json.dumps(gsp_payload, ensure_ascii=False)
        if "{{pla_no}}" not in placeholders:
            return "submission templates must contain {{pla_no}}"
        return None

    def submit(self, claim: dict[str, Any]) -> dict[str, Any]:
        if not self.cfg.get("gsp_submission_enabled"):
            return {"outcome": "invalid_payload", "detail": "gsp_submission_enabled is false"}
        gsp_payload = dict((claim.get("input") or {}).get("gsp_payload") or {})
        validation_error = self._validate_templates(gsp_payload)
        if validation_error:
            return {"outcome": "invalid_payload", "detail": validation_error}

        effect_key = str(claim.get("effect_key") or "").strip()
        execution = self.ledger.execution(effect_key)
        known_pla = str(execution.get("pla_no") or (claim.get("submission") or {}).get("pla_no") or "").strip()
        if not known_pla:
            existing = self.find_by_effect_key(effect_key)
            if existing.get("found"):
                known_pla = str(existing.get("pla_no") or "").strip()
                if existing.get("submitted"):
                    return {"outcome": "submitted", "pla_no": known_pla, "evidence": existing}
                if not existing.get("resumable"):
                    return {
                        "outcome": "manual_review",
                        "pla_no": known_pla,
                        "detail": "application marker exists but no local stage ledger can prove a safe resume",
                        "evidence": existing,
                    }

        replacements = {"effect_key": effect_key, "pla_no": known_pla, "workflow_id": execution.get("workflow_id") or ""}
        stages = execution.get("stages") or {}
        if not known_pla and "application_created" not in stages:
            application = _append_effect_marker(dict(gsp_payload["application"]), effect_key)
            application = _render_template(application, replacements)
            body = self._post(INSERT_APPLICATION_PATH, application)
            known_pla = _recursive_find(body, {"priceListApplicationId", "applicationId", "plaNo"}) or ""
            if not known_pla:
                return {
                    "outcome": "unknown",
                    "detail": "application create returned success without a recognizable PLA number",
                    "evidence": {"stage": "application_created"},
                }
            self.ledger.record_stage(effect_key, "application_created", {"pla_no": known_pla})
            replacements["pla_no"] = known_pla
            execution = self.ledger.execution(effect_key)
            stages = execution.get("stages") or {}

        if "application_saved" not in stages:
            body = self._post(
                SAVE_APPLICATION_PATH,
                _render_template(gsp_payload["save_application_and_product"], replacements),
            )
            self.ledger.record_stage(effect_key, "application_saved", {"pla_no": known_pla})
            stages = self.ledger.execution(effect_key).get("stages") or {}

        if "workflow_created" not in stages:
            body = self._post(CREATE_WORKFLOW_PATH, _render_template(gsp_payload["create_price_work"], replacements))
            workflow_id = _recursive_find(body, {"workflowId", "workFlowId", "processInstanceId", "id"}) or ""
            self.ledger.record_stage(
                effect_key,
                "workflow_created",
                {"pla_no": known_pla, "workflow_id": workflow_id},
            )
            replacements["workflow_id"] = workflow_id
            stages = self.ledger.execution(effect_key).get("stages") or {}

        if "workflow_started" not in stages:
            execution = self.ledger.execution(effect_key)
            replacements["workflow_id"] = execution.get("workflow_id") or replacements.get("workflow_id") or ""
            self._post(START_WORKFLOW_PATH, _render_template(gsp_payload["start_price_work"], replacements))
            self.ledger.record_stage(
                effect_key,
                "workflow_started",
                {"pla_no": known_pla, "workflow_id": replacements["workflow_id"]},
            )
        return {"outcome": "submitted", "pla_no": known_pla, "evidence": {"stage": "workflow_started"}}

    def verify(self, claim: dict[str, Any]) -> dict[str, Any]:
        result = self.find_by_effect_key(str(claim.get("effect_key") or ""))
        if not result.get("found"):
            if result.get("confirmed_absent"):
                return {"outcome": "absent", "evidence": result}
            return {"outcome": "unknown", "detail": "GSP search did not produce conclusive evidence", "evidence": result}
        if result.get("submitted"):
            return {"outcome": "found", "pla_no": result.get("pla_no"), "evidence": result}
        return {
            "outcome": "incomplete",
            "pla_no": result.get("pla_no"),
            "evidence": result,
            "detail": "matching GSP application exists but submission is not confirmed",
        }

    def check_approval(self, claim: dict[str, Any]) -> dict[str, Any]:
        pla_no = str((claim.get("submission") or {}).get("pla_no") or "").strip()
        if not pla_no:
            return {"outcome": "manual_review", "detail": "task has no PLA number"}
        result = self.checker.query_status(pla_no)
        if not result.get("ok"):
            return {"outcome": "unknown", "pla_no": pla_no, "detail": result.get("detail") or result.get("error")}
        return {
            "outcome": _normalize_approval_outcome(result.get("status")),
            "pla_no": pla_no,
            "detail": result.get("detail"),
            "evidence": {
                "status": result.get("status"),
                "approval_current_step": result.get("approval_current_step"),
                "approval_taskers": result.get("approval_taskers"),
            },
        }


def claim_workflow(cfg: dict[str, Any]) -> dict[str, Any]:
    capabilities = ["verify_gsp_submission", "check_gsp_approval"]
    if cfg.get("gsp_submission_enabled"):
        capabilities.insert(0, "submit_gsp")
    return api_post(
        cfg,
        "/api/agent/pricing-workflows/claim",
        {
            "token": cfg["agent_token"],
            "worker_id": cfg["worker_id"],
            "capabilities": capabilities,
            "lease_seconds": cfg["workflow_lease_seconds"],
        },
    )


def report_workflow(cfg: dict[str, Any], task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return api_post(cfg, f"/api/agent/pricing-workflows/{task_id}/report", payload)


def flush_pending_reports(cfg: dict[str, Any], ledger: WorkflowLedger) -> None:
    for report_id, payload in ledger.pending_reports():
        try:
            task_id = str(payload.pop("task_id"))
            payload["token"] = cfg["agent_token"]
            report_workflow(cfg, task_id, payload)
        except Exception:
            continue
        ledger.remove_pending_report(report_id)


def execute_claim(
    cfg: dict[str, Any],
    claim: dict[str, Any],
    client: GspPricingWorkflowClient,
) -> dict[str, Any]:
    action = str(claim.get("action") or "")
    try:
        if action == "submit_gsp":
            return client.submit(claim)
        if action == "verify_gsp_submission":
            return client.verify(claim)
        if action == "check_gsp_approval":
            return client.check_approval(claim)
        return {"outcome": "manual_review", "detail": f"unsupported action: {action}"}
    except requests.Timeout as exc:
        return {"outcome": "timeout", "detail": f"{type(exc).__name__}: result unknown"}
    except requests.RequestException as exc:
        return {"outcome": "transport_error", "detail": f"{type(exc).__name__}: result unknown"}
    except Exception as exc:
        return {"outcome": "manual_review", "detail": f"{type(exc).__name__}: {str(exc)[:1000]}"}


def run_once(
    cfg: dict[str, Any],
    *,
    client_factory: Callable[[dict[str, Any], WorkflowLedger], GspPricingWorkflowClient] = GspPricingWorkflowClient,
) -> dict[str, Any]:
    ledger = WorkflowLedger(Path(cfg["workflow_ledger_path"]))
    flush_pending_reports(cfg, ledger)
    claim = claim_workflow(cfg)
    if not claim.get("claimed"):
        return {"ok": True, "claimed": False}

    with client_factory(cfg, ledger) as client:
        result = execute_claim(cfg, claim, client)
    lease = claim.get("lease") or {}
    report_id = f"report:{lease.get('lease_id')}"
    report = {
        "task_id": claim["task_id"],
        "worker_id": cfg["worker_id"],
        "lease_id": lease.get("lease_id"),
        "report_id": report_id,
        "outcome": result.get("outcome") or "manual_review",
        "pla_no": result.get("pla_no"),
        "detail": result.get("detail"),
        "evidence": result.get("evidence") or {},
    }
    ledger.put_pending_report(report_id, report)
    transport_report = copy.deepcopy(report)
    task_id = str(transport_report.pop("task_id"))
    transport_report["token"] = cfg["agent_token"]
    response = report_workflow(cfg, task_id, transport_report)
    ledger.remove_pending_report(report_id)
    return {"ok": True, "claimed": True, "action": claim.get("action"), "result": result, "report": response}


def main() -> int:
    parser = argparse.ArgumentParser(description="Dahua pricing workflow worker")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    cfg = load_workflow_config(Path(args.config))
    if not cfg.get("workflow_enabled"):
        print(json.dumps({"ok": False, "disabled": True, "reason": "workflow_enabled is false"}, ensure_ascii=False))
        return 2
    while True:
        try:
            result = run_once(cfg)
            print(json.dumps(result, ensure_ascii=False))
        except Exception as exc:
            print(
                json.dumps(
                    {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:1000]}", "retrying": not args.once},
                    ensure_ascii=False,
                )
            )
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(cfg["workflow_poll_interval_seconds"])


if __name__ == "__main__":
    sys.exit(main())
