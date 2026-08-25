# backend/app/main.py
from __future__ import annotations

import copy
import hmac
import json
import math
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from collections import Counter, defaultdict

from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from backend.app.agent_ops import (
    AgentAutomation,
    AgentConfigReq,
    AgentGspQueueReq,
    AgentGspStatusResultReq,
    AgentGspUnderApprovalQueueReq,
    AgentGspUnderApprovalResultReq,
    AgentReplayEvalReq,
    AgentSheetStatusUpdateAckReq,
    AgentSheetStatusUpdatesReq,
    AgentSheetProbeReq,
    AgentSheetPushReq,
    AgentToolBackendHeartbeatReq,
)
from backend.app.pricing_workflow import (
    PricingWorkflowClaimReq,
    PricingWorkflowCreateReq,
    PricingWorkflowReportReq,
    PricingWorkflowRetryReq,
    PricingWorkflowStore,
    WorkflowConflict,
    WorkflowError,
    WorkflowNotFound,
    WorkflowValidationError,
)
from backend.app.demo_pipeline import (
    DemoPipeline,
    DemoPipelineError,
    DemoRunBusy,
    DemoRunNotFound,
    DemoRunReq,
)
from backend.app.sheet_workflow_ingest import SheetWorkflowIngestCoordinator
from backend.app.price_data_store import (
    PriceDataConflict,
    PriceDataError,
    PriceDataNotFound,
    PriceDataStore,
)
from backend.app.evolution.control_plane import EvolutionControlPlane
from backend.app.evolution.evaluation import EvaluationError
from backend.app.evolution.event_store import EventStoreError
from backend.app.evolution.registry import RegistryError
from backend.app.evolution.release import ReleaseError
from backend.engine.engine import EngineConfig, PricingEngine
from backend.engine.core import pricing_engine as pricing_engine_mod
from backend.engine.core import pricing_rules as pricing_rules_mod
from backend.engine.core.formatter import build_export_frames, write_export_xlsx
from backend.engine.core.loader import normalize_pn_raw, parse_pn_list_file


APP_ROOT = Path(__file__).resolve().parents[2]  # .../backend
REPO_ROOT = APP_ROOT.parent  # repo root
RUNTIME_DIR = Path(os.getenv("DAHUA_PRICING_RUNTIME_DIR", "/data/dahua_pricing_runtime"))
UPLOADS_DIR = RUNTIME_DIR / "uploads"
OUTPUTS_DIR = RUNTIME_DIR / "outputs"
LOGS_DIR = RUNTIME_DIR / "logs"
DATA_DIR = RUNTIME_DIR / "data"
ADMIN_DIR = RUNTIME_DIR / "admin"
MAPPING_DIR = RUNTIME_DIR / "mapping"

OUT_COUNTRY = "Country_import_upload_Model.xlsx"
OUT_COUNTRY_CUSTOMER = "Country&Customer_import_upload_Model.xlsx"  # backward-compat symbol only

STATE_NAME = "state.json"
UPLIFT_CFG = ADMIN_DIR / "uplift.json"
KEYWORD_UPLIFT_CFG = ADMIN_DIR / "keyword_uplift.json"
DDP_RULES_CFG = ADMIN_DIR / "ddp_rules.json"
PRICE_RULES_CFG = ADMIN_DIR / "price_rules.json"
PRICE_DATA_UPLOAD_MAX_BYTES = int(os.getenv("DAHUA_PRICE_DATA_UPLOAD_MAX_BYTES", str(100 * 1024 * 1024)))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dirs() -> None:
    for d in (UPLOADS_DIR, OUTPUTS_DIR, DATA_DIR, ADMIN_DIR, MAPPING_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _copy_upload_limited(upload: UploadFile, target: Path, *, limit: int = PRICE_DATA_UPLOAD_MAX_BYTES) -> int:
    total = 0
    with target.open("wb") as stream:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise HTTPException(status_code=413, detail="price data file exceeds upload size limit")
            stream.write(chunk)
    return total


def _job_dirs(job_id: str) -> Tuple[Path, Path, Path]:
    up = UPLOADS_DIR / job_id
    out = OUTPUTS_DIR / job_id
    lg = LOGS_DIR / job_id
    up.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    return up, out, lg


def _state_path(job_id: str) -> Path:
    return (OUTPUTS_DIR / job_id) / STATE_NAME


def _write_state(job_id: str, state: Dict[str, Any]) -> None:
    p = _state_path(job_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _read_state(job_id: str) -> Dict[str, Any]:
    p = _state_path(job_id)
    if not p.exists():
        raise HTTPException(status_code=404, detail="job_id not found")
    return json.loads(p.read_text(encoding="utf-8"))


def _read_json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json_file(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _deep_jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _deep_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_deep_jsonable(v) for v in obj]
    return obj


def _normalize_number(v: Any, field: str) -> float:
    try:
        n = float(v)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"{field}: invalid number -> {v!r}") from e
    if not math.isfinite(n):
        raise HTTPException(status_code=400, detail=f"{field}: non-finite number -> {v!r}")
    return n


def _normalize_uplift_payload(payload: Any) -> Dict[str, float]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be object: {uplift_key: pct}")
    out: Dict[str, float] = {}
    for k, v in payload.items():
        kk = str(k).strip()
        if not kk:
            continue
        out[kk] = _normalize_number(v, f"uplift[{kk}]")
    return out


def _normalize_keyword_uplift_payload(payload: Any) -> list[Dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("rules")
    if payload is None:
        payload = []
    if not isinstance(payload, list):
        raise HTTPException(
            status_code=400,
            detail="payload must be array: [{keyword, pct, enabled}]",
        )

    rows: list[Dict[str, Any]] = []
    for i, it in enumerate(payload):
        if not isinstance(it, dict):
            raise HTTPException(status_code=400, detail=f"keyword_uplift[{i}] must be object")
        kw = str(it.get("keyword") or "").strip()
        if not kw:
            continue
        pct = _normalize_number(it.get("pct"), f"keyword_uplift[{i}].pct")
        enabled = bool(it.get("enabled", True))
        rows.append(
            {
                "keyword": kw,
                "pct": pct,
                "enabled": enabled,
            }
        )

    dedup: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        k = str(r.get("keyword") or "").upper()
        dedup[k] = r
    out = list(dedup.values())
    out.sort(key=lambda r: str(r.get("keyword") or ""))
    return out


def _normalize_ddp_rules_payload(payload: Any) -> Dict[str, tuple[float, float, float, float, float, float]]:
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail="payload must be object: {category: [p1,p2,p3,p4,ddp_adjust,fob_adjust]}",
        )
    out: Dict[str, tuple[float, float, float, float, float, float]] = {}
    for k, v in payload.items():
        kk = str(k).strip()
        if not kk:
            continue
        if not isinstance(v, (list, tuple)) or len(v) not in {4, 5, 6}:
            raise HTTPException(status_code=400, detail=f"ddp_rules[{kk}] must be array length=4, 5 or 6")
        # 兼容旧配置：第五项是 DDP Adjust，第六项是该产品线 FOB Adjust。
        raw_vals = list(v) + [0.0] * (6 - len(v))
        vals = tuple(_normalize_number(x, f"ddp_rules[{kk}]") for x in raw_vals)
        out[kk] = vals
    return out


def _normalize_price_rules_payload(payload: Any) -> Dict[str, Dict[str, Dict[str, Optional[float]]]]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be object")
    allowed_leaf_keys = {"reseller", "gold", "silver", "ivory", "msrp_on_installer"}
    out: Dict[str, Dict[str, Dict[str, Optional[float]]]] = {}
    for group_k, group_v in payload.items():
        g = str(group_k).strip()
        if not g:
            continue
        if not isinstance(group_v, dict):
            raise HTTPException(status_code=400, detail=f"price_rules[{g}] must be object")
        sub_out: Dict[str, Dict[str, Optional[float]]] = {}
        for rule_k, rule_v in group_v.items():
            rk = str(rule_k).strip()
            if not rk:
                continue
            if not isinstance(rule_v, dict):
                raise HTTPException(status_code=400, detail=f"price_rules[{g}][{rk}] must be object")
            leaf: Dict[str, Optional[float]] = {}
            for leaf_k, leaf_v in rule_v.items():
                lk = str(leaf_k).strip()
                if lk not in allowed_leaf_keys:
                    raise HTTPException(
                        status_code=400,
                        detail=f"price_rules[{g}][{rk}] invalid key {lk!r}; allowed={sorted(allowed_leaf_keys)}",
                    )
                if leaf_v is None or (isinstance(leaf_v, str) and leaf_v.strip() == ""):
                    leaf[lk] = None
                else:
                    leaf[lk] = _normalize_number(leaf_v, f"price_rules[{g}][{rk}][{lk}]")
            sub_out[rk] = leaf
        out[g] = sub_out
    return out


def _sorted_uplift_dict() -> Dict[str, float]:
    return dict(sorted(pricing_engine_mod.UPLIFT_PCT_BY_LINE.items(), key=lambda kv: kv[0]))


def _sorted_keyword_uplift_rows() -> list[Dict[str, Any]]:
    rows = [
        {
            "keyword": str(r.get("keyword") or "").strip(),
            "pct": float(r.get("pct") or 0.0),
            "enabled": bool(r.get("enabled", True)),
        }
        for r in (pricing_engine_mod.KEYWORD_UPLIFT_RULES or [])
        if isinstance(r, dict)
    ]
    rows = [r for r in rows if r["keyword"]]
    rows.sort(key=lambda r: r["keyword"])
    return rows


def _sorted_ddp_rules_dict() -> Dict[str, list[float]]:
    return {
        k: [float(x) for x in (list(v) + [0.0] * (6 - len(v)))]
        for k, v in sorted(pricing_rules_mod.DDP_RULES.items(), key=lambda kv: kv[0])
    }


def _sorted_price_rules_dict() -> Dict[str, Any]:
    return dict(sorted(_deep_jsonable(pricing_rules_mod.PRICE_RULES).items(), key=lambda kv: kv[0]))


def _apply_rule_overrides_if_exist() -> None:
    if UPLIFT_CFG.exists():
        data = _normalize_uplift_payload(_read_json_file(UPLIFT_CFG))
        pricing_engine_mod.UPLIFT_PCT_BY_LINE.clear()
        pricing_engine_mod.UPLIFT_PCT_BY_LINE.update(data)

    if KEYWORD_UPLIFT_CFG.exists():
        data = _normalize_keyword_uplift_payload(_read_json_file(KEYWORD_UPLIFT_CFG))
        pricing_engine_mod.KEYWORD_UPLIFT_RULES.clear()
        pricing_engine_mod.KEYWORD_UPLIFT_RULES.extend(data)

    if DDP_RULES_CFG.exists():
        data = _normalize_ddp_rules_payload(_read_json_file(DDP_RULES_CFG))
        pricing_rules_mod.DDP_RULES.clear()
        pricing_rules_mod.DDP_RULES.update(data)

    if PRICE_RULES_CFG.exists():
        data = _normalize_price_rules_payload(_read_json_file(PRICE_RULES_CFG))
        pricing_rules_mod.PRICE_RULES.clear()
        pricing_rules_mod.PRICE_RULES.update(data)


class QueryReq(BaseModel):
    pn: str = Field(..., description="Part No.")
    apply_black_markup: bool = Field(default=False, description="apply detected variant markups (Black policy, ATC Sys delta)")


class QueryRecomputeReq(BaseModel):
    pn: str = Field(..., description="Part No.")
    force_category: Optional[str] = Field(default=None, description="override DDP category")
    force_price_group: Optional[str] = Field(default=None, description="override PRICE_RULES group")
    force_series_key: Optional[str] = Field(default=None, description="override PRICE_RULES subgroup key")
    manual_sys_basis_price_used: Optional[float] = Field(
        default=None,
        description="manual override for Sys Basis Price Used",
    )
    manual_fob: Optional[float] = Field(default=None, description="manual override for FOB C(EUR)")
    manual_price_field: Optional[str] = Field(
        default=None,
        description="manual price tier to use as the reverse-calculation anchor",
    )
    manual_price_value: Optional[float] = Field(
        default=None,
        description="manual price value for manual_price_field",
    )
    manual_final_values: Optional[Dict[str, Any]] = Field(
        default=None,
        description="direct final price-tier overrides applied after calculation",
    )
    apply_black_markup: bool = Field(default=False, description="apply detected variant markups (Black policy, ATC Sys delta)")


class QueryExportReq(BaseModel):
    pn: str = Field(..., description="Part No.")
    force_category: Optional[str] = Field(default=None, description="override DDP category")
    force_price_group: Optional[str] = Field(default=None, description="override PRICE_RULES group")
    force_series_key: Optional[str] = Field(default=None, description="override PRICE_RULES subgroup key")
    force_full_recalc: bool = Field(default=False, description="force full price recalculation")
    manual_sys_basis_price_used: Optional[float] = Field(
        default=None,
        description="manual override for Sys Basis Price Used",
    )
    manual_fob: Optional[float] = Field(default=None, description="manual override for FOB C(EUR)")
    manual_price_field: Optional[str] = Field(
        default=None,
        description="manual price tier to use as the reverse-calculation anchor",
    )
    manual_price_value: Optional[float] = Field(
        default=None,
        description="manual price value for manual_price_field",
    )
    manual_final_values: Optional[Dict[str, Any]] = Field(
        default=None,
        description="direct final price-tier overrides applied after calculation",
    )
    apply_black_markup: bool = Field(default=False, description="apply detected variant markups (Black policy, ATC Sys delta)")


class ExternalModelReq(BaseModel):
    pn: str = Field(..., description="Part No.")
    apply_france_anchor: bool = Field(
        default=True,
        description="if france has complete priced row under same external model, override cluster prices",
    )


class ModelSearchReq(BaseModel):
    query: str = Field(..., description="internal/external model query")
    limit: int = Field(default=100, ge=1, le=300, description="max matched devices to return")


class KeywordUpliftPreviewReq(BaseModel):
    keyword: str = Field(..., description="match keyword in Internal Model / External Model")
    pct: float = Field(..., description="preview uplift pct for the keyword")
    enabled: bool = Field(default=True, description="if false, preview returns empty impact")


class EvolutionArtifactReq(BaseModel):
    token: Optional[str] = Field(default=None)
    kind: str
    name: str
    version: str
    content: Any
    parent_version: Optional[str] = None
    source: Any = "manual"
    created_by: str = "developer"
    json_schema: Dict[str, Any] = Field(default_factory=dict)
    business_constraints: Any = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class EvolutionArtifactTransitionReq(BaseModel):
    token: Optional[str] = Field(default=None)
    to_status: str
    actor: str
    reason: str


class EvolutionEvaluationReq(BaseModel):
    token: Optional[str] = Field(default=None)
    payload: Dict[str, Any] = Field(default_factory=dict)


class EvolutionReleaseProposalReq(BaseModel):
    token: Optional[str] = Field(default=None)
    evaluation_id: str
    candidate_pins: Dict[str, str]
    baseline_pins: Dict[str, str]
    environment: str = "production"
    actor: str = "release-api"
    release_key: Optional[str] = None


class EvolutionActorReq(BaseModel):
    token: Optional[str] = Field(default=None)
    actor: str = "release-api"


class EvolutionCanaryReq(EvolutionActorReq):
    fraction: float = Field(gt=0, le=0.10)
    approved_by: str


class EvolutionPromoteReq(EvolutionActorReq):
    approved_by: str


class EvolutionWindowReq(EvolutionActorReq):
    window_id: str
    metrics: Dict[str, Any] = Field(default_factory=dict)


class EvolutionRollbackReq(EvolutionActorReq):
    reason: str


class SheetWorkflowBackfillReq(BaseModel):
    token: Optional[str] = Field(default=None)
    push_id: str = "latest"
    source_id: str = Field(min_length=1, max_length=500)
    apply: bool = False
    expected_manifest_hash: Optional[str] = None
    limit: int = Field(default=100, ge=1, le=500)


class EvolutionCaseReviewReq(BaseModel):
    token: Optional[str] = Field(default=None)
    disposition: str
    reviewer: str
    rationale: str
    expected_terminal_state: Optional[str] = None
    expected_action: Optional[str] = None
    strata: Dict[str, str] = Field(default_factory=dict)


def _norm_optional_text(v: Any) -> Optional[str]:
    if v is None:
        return None
    try:
        if math.isnan(v):
            return None
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    if s.lower() in {"nan", "none", "nat", "<na>"}:
        return None
    return s or None


def _validate_query_overrides(
    force_category: Optional[str],
    force_price_group: Optional[str],
    force_series_key: Optional[str],
    *,
    require_any: bool = False,
) -> None:
    if require_any and not force_category and not force_price_group:
        raise HTTPException(status_code=400, detail="force_category or force_price_group is required")

    if force_category and force_category not in pricing_rules_mod.DDP_RULES:
        raise HTTPException(
            status_code=400,
            detail=f"force_category not found in DDP_RULES: {force_category!r}",
        )

    if force_price_group and force_price_group not in pricing_rules_mod.PRICE_RULES:
        raise HTTPException(
            status_code=400,
            detail=f"force_price_group not found in PRICE_RULES: {force_price_group!r}",
        )

    if force_series_key and force_price_group:
        group_rules = pricing_rules_mod.PRICE_RULES.get(force_price_group) or {}
        if force_series_key not in group_rules:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"force_series_key not found in PRICE_RULES[{force_price_group!r}]: "
                    f"{force_series_key!r}"
                ),
            )

    if force_category and force_price_group:
        if not pricing_engine_mod.is_strict_price_group_compatible(force_category, force_price_group):
            default_pg = pricing_engine_mod.strict_price_group_default(force_category)
            raise HTTPException(
                status_code=400,
                detail=(
                    f"incompatible force_category/force_price_group: "
                    f"{force_category!r} vs {force_price_group!r}; "
                    f"expected price_group={default_pg!r}"
                ),
            )


def _normalize_manual_price_override(v: Any, field: str) -> Optional[float]:
    if v is None:
        return None
    n = _normalize_number(v, field)
    if n <= 0:
        raise HTTPException(status_code=400, detail=f"{field}: must be > 0")
    return n


def _normalize_manual_price_field(v: Any) -> Optional[str]:
    s = _norm_optional_text(v)
    if not s:
        return None
    aliases = {
        "sys_basis_price_used": pricing_engine_mod.MANUAL_SYS_BASIS_PRICE_FIELD,
        "sys basis price used": pricing_engine_mod.MANUAL_SYS_BASIS_PRICE_FIELD,
        "fob": "FOB C(EUR)",
        "fob c": "FOB C(EUR)",
        "ddp": "DDP A(EUR)",
        "ddp a": "DDP A(EUR)",
        "reseller": "Suggested Reseller(EUR)",
        "suggested reseller": "Suggested Reseller(EUR)",
        "gold": "Gold(EUR)",
        "silver": "Silver(EUR)",
        "ivory": "Ivory(EUR)",
        "msrp": "MSRP(EUR)",
    }
    field = aliases.get(s.strip().lower(), s)
    if field not in pricing_engine_mod.MANUAL_PRICE_FIELDS:
        raise HTTPException(
            status_code=400,
            detail=f"manual_price_field not supported: {s!r}",
        )
    return field


def _validate_manual_recompute_inputs(
    manual_sys_basis_price_used: Optional[float],
    manual_fob: Optional[float],
    manual_price_field: Optional[str],
    manual_price_value: Optional[float],
) -> None:
    has_manual_price_field = bool(manual_price_field) or manual_price_value is not None
    if bool(manual_price_field) != (manual_price_value is not None):
        raise HTTPException(
            status_code=400,
            detail="manual_price_field and manual_price_value must be provided together",
        )

    manual_modes = [
        manual_sys_basis_price_used is not None,
        manual_fob is not None,
        has_manual_price_field,
    ]
    if sum(1 for x in manual_modes if x) > 1:
        raise HTTPException(
            status_code=400,
            detail="manual price override fields cannot be provided together",
        )


def _build_category_price_groups() -> Dict[str, list[str]]:
    out: Dict[str, list[str]] = {}
    if _engine is None or _engine.data is None:
        return out

    cnt: Dict[str, Counter[str]] = defaultdict(Counter)
    for df in (_engine.data.map_fr, _engine.data.map_sys):
        if df is None or df.empty:
            continue
        for _, row in df.iterrows():
            cat = _norm_optional_text(row.get("category"))
            pg = _norm_optional_text(row.get("price_group_hint"))
            if not cat or not pg:
                continue
            if pg not in pricing_rules_mod.PRICE_RULES:
                continue
            cnt[cat][pg] += 1

    for cat in sorted(cnt.keys()):
        ranked = [k for k, _ in cnt[cat].most_common() if k in pricing_rules_mod.PRICE_RULES]
        if cat in pricing_rules_mod.PRICE_RULES and cat in ranked:
            ranked = [cat] + [x for x in ranked if x != cat]
        elif cat in pricing_rules_mod.PRICE_RULES:
            ranked = [cat] + ranked
        out[cat] = ranked

    for cat in sorted(pricing_rules_mod.DDP_RULES.keys()):
        if cat in out:
            continue
        if cat in pricing_rules_mod.PRICE_RULES:
            out[cat] = [cat]

    return out


def _pick_col(df: Any, candidates: tuple[str, ...]) -> Optional[str]:
    if df is None:
        return None
    cols = [str(c) for c in getattr(df, "columns", [])]
    if not cols:
        return None
    low_map = {str(c).strip().lower(): c for c in cols}
    for c in candidates:
        if c in cols:
            return c
        k = c.strip().lower()
        if k in low_map:
            return low_map[k]
    return None


def _to_float_soft(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        if not math.isfinite(f):
            return None
        return f
    except Exception:
        return None


def _collect_external_model_cluster_pns(pn: str) -> tuple[str, list[str]]:
    assert _engine is not None and _engine.data is not None
    base = _engine.query_one(pn)
    if str(base.get("status", "")).lower() != "ok":
        raise HTTPException(status_code=404, detail=f"pn not found: {pn}")

    ext_model = _norm_optional_text((base.get("final_values") or {}).get("External Model"))
    if not ext_model:
        raise HTTPException(status_code=400, detail=f"external model is empty for pn: {pn}")

    ext_up = ext_model.upper()
    pns: list[str] = []
    seen: set[str] = set()

    for df in (_engine.data.france_df, _engine.data.sys_df):
        pn_col = _pick_col(
            df,
            ("Part No.", "Part No", "Part Num", "PN", "P/N", "Part Number", "PartNumber"),
        )
        ext_col = _pick_col(df, ("External Model", "ExternalModel"))
        if not pn_col or not ext_col:
            continue
        for _, row in df.iterrows():
            em = _norm_optional_text(row.get(ext_col))
            part = _norm_optional_text(row.get(pn_col))
            if not em or not part:
                continue
            if em.upper() != ext_up:
                continue
            key = normalize_pn_raw(part)
            if not key or key in seen:
                continue
            seen.add(key)
            pns.append(part)

    input_key = normalize_pn_raw(pn)
    if input_key and input_key not in seen:
        pns.insert(0, pn)
    return ext_model, pns


def _normalize_model_match_text(v: Any) -> str:
    s = str(v or "").strip().upper()
    if not s:
        return ""
    return re.sub(r"[^A-Z0-9]+", "", s)


def _score_model_match(query_norm: str, value: Any) -> tuple[int, str]:
    target = _normalize_model_match_text(value)
    if not query_norm or not target:
        return 0, ""
    if target == query_norm:
        return 300, "exact"
    if target.startswith(query_norm):
        return 200, "prefix"
    if query_norm in target:
        return 100, "contains"
    return 0, ""


def _collect_model_search_candidates(
    df: Any,
    *,
    source: str,
    query_norm: str,
    seen: Dict[str, Dict[str, Any]],
) -> None:
    if df is None or getattr(df, "empty", True):
        return

    pn_col = _pick_col(df, ("Part No.", "Part No", "Part Num", "PN", "P/N", "Part Number", "PartNumber"))
    internal_col = _pick_col(df, ("Internal Model",))
    external_col = _pick_col(df, ("External Model", "ExternalModel"))
    if not pn_col or (not internal_col and not external_col):
        return

    for _, row in df.iterrows():
        pn = _norm_optional_text(row.get(pn_col))
        if not pn:
            continue

        matches: List[tuple[str, int, str, Optional[str]]] = []
        for field_name, col_name in (("internal_model", internal_col), ("external_model", external_col)):
            if not col_name:
                continue
            raw_val = _norm_optional_text(row.get(col_name))
            score, match_type = _score_model_match(query_norm, raw_val)
            if score > 0:
                matches.append((field_name, score, match_type, raw_val))
        if not matches:
            continue

        best_field, best_score, best_type, _raw_val = max(matches, key=lambda x: (x[1], x[0]))
        _ = best_field
        item = seen.get(pn)
        if item is None:
            item = {
                "pn": pn,
                "score": best_score,
                "match_type": best_type,
                "matched_fields": set(),
                "sources": set(),
                "internal_model_raw": None,
                "external_model_raw": None,
            }
            seen[pn] = item
        if best_score > int(item.get("score") or 0):
            item["score"] = best_score
            item["match_type"] = best_type

        cast_fields: Set[str] = item["matched_fields"]
        cast_sources: Set[str] = item["sources"]
        cast_sources.add(source)
        for field_name, _score, _match_type, raw_val in matches:
            cast_fields.add(field_name)
            key = "internal_model_raw" if field_name == "internal_model" else "external_model_raw"
            if raw_val and not item.get(key):
                item[key] = raw_val


def _search_models(req: ModelSearchReq) -> Dict[str, Any]:
    assert _engine is not None and _engine.data is not None
    query = (req.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is empty")

    query_norm = _normalize_model_match_text(query)
    if not query_norm:
        raise HTTPException(status_code=400, detail="query has no searchable characters")

    seen: Dict[str, Dict[str, Any]] = {}
    _collect_model_search_candidates(
        _engine.data.france_df,
        source="france",
        query_norm=query_norm,
        seen=seen,
    )
    _collect_model_search_candidates(
        _engine.data.sys_df,
        source="sys",
        query_norm=query_norm,
        seen=seen,
    )

    ranked = sorted(
        seen.values(),
        key=lambda x: (
            -int(x.get("score") or 0),
            str(x.get("pn") or ""),
            str(x.get("internal_model_raw") or ""),
            str(x.get("external_model_raw") or ""),
        ),
    )

    items: List[Dict[str, Any]] = []
    for idx, matched in enumerate(ranked[: int(req.limit)], start=1):
        row = _engine.query_one(str(matched.get("pn") or ""))
        review = _build_batch_review_item(idx, row)
        review["match_type"] = matched.get("match_type")
        review["match_score"] = matched.get("score")
        review["matched_fields"] = sorted(list(matched.get("matched_fields") or []))
        review["sources"] = sorted(list(matched.get("sources") or []))
        review["internal_model_raw"] = matched.get("internal_model_raw")
        review["external_model_raw"] = matched.get("external_model_raw")
        items.append(review)

    return {
        "query": query,
        "normalized_query": query_norm,
        "count": len(items),
        "total_hits": len(ranked),
        "limit": int(req.limit),
        "items": items,
    }


def _pick_france_anchor_prices(
    external_model: str,
    *,
    lens_signature: Optional[List[str]] = None,
    data_bundle: Optional[Any] = None,
) -> Dict[str, Any]:
    assert _engine is not None and _engine.data is not None
    df = (data_bundle or _engine.data).france_df
    pn_col = _pick_col(
        df,
        ("Part No.", "Part No", "Part Num", "PN", "P/N", "Part Number", "PartNumber"),
    )
    ext_col = _pick_col(df, ("External Model", "ExternalModel"))
    if not pn_col or not ext_col:
        return {"pn": None, "prices": None, "lens_signature": [], "rejection_reason": "columns_missing"}

    ext_up = str(external_model).strip().upper()
    price_cols = list(pricing_engine_mod.PRICE_COLS)
    candidates: List[tuple[Any, Dict[str, float], List[str]]] = []
    for _, row in df.iterrows():
        em = _norm_optional_text(row.get(ext_col))
        if not em or em.upper() != ext_up:
            continue
        prices: Dict[str, float] = {}
        ok = True
        for c in price_cols:
            f = _to_float_soft(row.get(c))
            if f is None or f <= 0:
                ok = False
                break
            prices[c] = f
        if ok:
            candidate_lens = pricing_engine_mod._extract_optical_signature(
                row.get("Internal Model"), row.get(ext_col)
            )
            candidates.append((row, prices, candidate_lens))

    target_lens = sorted(list(lens_signature or []))
    compatible = candidates
    if target_lens:
        compatible = [item for item in candidates if item[2] == target_lens]
    if not compatible:
        reason = "lens_mismatch" if target_lens and candidates else "anchor_not_found_or_incomplete"
        return {"pn": None, "prices": None, "lens_signature": [], "rejection_reason": reason}

    distinct_internal = {
        _normalize_model_match_text(item[0].get("Internal Model")) for item in compatible
    }
    distinct_internal.discard("")
    if len(distinct_internal) > 1:
        return {
            "pn": None,
            "prices": None,
            "lens_signature": [],
            "rejection_reason": "ambiguous_external_model_after_lens_filter",
        }

    row, prices, anchor_lens = compatible[0]
    return {
        "pn": _norm_optional_text(row.get(pn_col)),
        "prices": prices,
        "lens_signature": anchor_lens,
        "rejection_reason": None,
    }


def _apply_external_model_anchor_to_row(
    row: Dict[str, Any],
    *,
    apply_france_anchor: bool,
    anchor_cache: Optional[Dict[str, Dict[str, Any]]] = None,
    data_bundle: Optional[Any] = None,
) -> bool:
    status = str(row.get("status", "")).lower()
    meta = dict(row.get("meta") or {})
    row["meta"] = meta

    if status != "ok":
        meta["external_model_anchor_applied"] = False
        meta["external_model_anchor_pn"] = None
        meta["external_model_anchor_changed"] = False
        return False

    fv = dict(row.get("final_values") or {})
    row["final_values"] = fv
    ext_model = _norm_optional_text(fv.get("External Model"))
    row_lens = pricing_engine_mod._extract_optical_signature(
        fv.get("Internal Model"), fv.get("External Model")
    )
    if not apply_france_anchor or not ext_model:
        meta["external_model_anchor_applied"] = False
        meta["external_model_anchor_pn"] = None
        meta["external_model_anchor"] = ext_model
        meta["external_model_anchor_changed"] = False
        meta["external_model_row_lens_signature"] = row_lens
        return False

    key = f"{ext_model.upper()}|{'/'.join(row_lens)}"
    anchor: Dict[str, Any]
    if anchor_cache is not None and key in anchor_cache:
        anchor = anchor_cache[key]
    else:
        anchor = _pick_france_anchor_prices(
            ext_model,
            lens_signature=row_lens,
            data_bundle=data_bundle,
        )
        if anchor_cache is not None:
            anchor_cache[key] = anchor
    anchor_pn = _norm_optional_text(anchor.get("pn"))
    anchor_prices = anchor.get("prices")
    anchor_lens = list(anchor.get("lens_signature") or [])
    rejection_reason = _norm_optional_text(anchor.get("rejection_reason"))

    if not anchor_prices:
        meta["external_model_anchor_applied"] = False
        meta["external_model_anchor_pn"] = None
        meta["external_model_anchor"] = ext_model
        meta["external_model_anchor_changed"] = False
        meta["external_model_row_lens_signature"] = row_lens
        meta["external_model_anchor_lens_signature"] = []
        meta["external_model_anchor_rejection_reason"] = rejection_reason
        if rejection_reason:
            ws = list(row.get("warnings") or [])
            warning = f"external_model_anchor_rejected_{rejection_reason}"
            if warning not in ws:
                ws.append(warning)
            row["warnings"] = ws
        return False

    cf = set(row.get("calculated_fields") or [])
    before_vals = {c: _to_float_soft(fv.get(c)) for c in pricing_engine_mod.PRICE_COLS}
    for c, v in anchor_prices.items():
        fv[c] = v
        cf.discard(c)
    row_changed = any(before_vals.get(c) != _to_float_soft(fv.get(c)) for c in pricing_engine_mod.PRICE_COLS)

    row["calculated_fields"] = sorted(list(cf))
    meta["external_model_anchor_applied"] = True
    meta["external_model_anchor_pn"] = anchor_pn
    meta["external_model_anchor"] = ext_model
    meta["external_model_anchor_changed"] = row_changed
    meta["external_model_row_lens_signature"] = row_lens
    meta["external_model_anchor_lens_signature"] = anchor_lens
    meta["external_model_anchor_rejection_reason"] = None
    ws = list(row.get("warnings") or [])
    w = f"external_model_anchor_price_from_fr={anchor_pn or 'UNKNOWN'}"
    if w not in ws:
        ws.append(w)
    row["warnings"] = ws
    return row_changed


def _query_cluster_by_external_model(
    pn: str,
    *,
    apply_france_anchor: bool,
) -> Dict[str, Any]:
    assert _engine is not None
    external_model, pns = _collect_external_model_cluster_pns(pn)
    if not pns:
        raise HTTPException(status_code=404, detail=f"no rows found for external model: {external_model}")

    anchor_cache: Dict[str, Dict[str, Any]] = {}
    rows: list[Dict[str, Any]] = []
    changed_pns: list[str] = []
    anchor_pn: Optional[str] = None
    anchor_pns: set[str] = set()
    anchor_applied = False
    for item_pn in pns:
        r = _engine.query_one(item_pn)
        row_changed = _apply_external_model_anchor_to_row(
            r,
            apply_france_anchor=apply_france_anchor,
            anchor_cache=anchor_cache,
        )
        r_meta = r.get("meta") or {}
        if r_meta.get("external_model_anchor_applied"):
            anchor_applied = True
            anchor_pn = _norm_optional_text(r_meta.get("external_model_anchor_pn")) or anchor_pn
            if anchor_pn:
                anchor_pns.add(anchor_pn)
        if row_changed:
            changed_pns.append(item_pn)
        rows.append(r)

    return {
        "ok": True,
        "input_pn": pn,
        "external_model": external_model,
        "count": len(rows),
        "anchor_applied": bool(anchor_applied),
        "anchor_pn": anchor_pn,
        "anchor_pns": sorted(anchor_pns),
        "anchor_changed_count": len(changed_pns),
        "anchor_changed_pns": changed_pns,
        "rows": rows,
    }


def _pick_col(df, candidates: list[str]) -> str:
    cols = list(df.columns)
    low_map = {str(c).strip().lower(): c for c in cols}
    for c in candidates:
        if c in cols:
            return c
    for c in candidates:
        cc = str(c).strip().lower()
        if cc in low_map:
            return low_map[cc]
    return cols[0]


def _keyword_matches_model_text(keyword: str, model_text: Any) -> bool:
    kw = str(keyword or "").strip().upper()
    if not kw:
        return False
    text = str(model_text or "").strip().upper()
    if not text:
        return False

    for segment in re.split(r"[^A-Z0-9]+", text):
        if not segment:
            continue
        if segment.startswith(kw):
            return True
        if segment.startswith(f"DHI{kw}") or segment.startswith(f"DH{kw}"):
            return True
    return False


def _row_matches_keyword_models(row: Any, keyword: str, fields: list[str]) -> bool:
    for f in fields:
        if f in row and _keyword_matches_model_text(keyword, row.get(f)):
            return True
    return False


def _collect_keyword_candidate_pns(keyword: str) -> list[str]:
    assert _engine is not None and _engine.data is not None
    data = _engine.data
    kw = str(keyword or "").strip().upper()
    if not kw:
        return []

    fr = data.france_df
    sy = data.sys_df
    fr_pn_col = _pick_col(fr, ["Part No.", "Part No", "Part Num", "PN", "P/N", "Part Number"])
    sy_pn_col = _pick_col(sy, ["Part Num", "Part No.", "Part No", "PN", "P/N", "Part Number"])

    fr_fields = ["Internal Model", "External Model"]
    sy_fields = ["Internal Model", "External Model"]

    pns: set[str] = set()
    for _, r in fr.iterrows():
        if _row_matches_keyword_models(r, kw, fr_fields):
            pn = str(r.get(fr_pn_col) or "").strip()
            if pn:
                pns.add(pn)
    for _, r in sy.iterrows():
        if _row_matches_keyword_models(r, kw, sy_fields):
            pn = str(r.get(sy_pn_col) or "").strip()
            if pn:
                pns.add(pn)
    return sorted(pns)

def _keyword_preview_all_sources(keyword: str, pct: float, enabled: bool) -> Dict[str, Any]:
    assert _engine is not None and _engine.data is not None
    if not enabled:
        return {
            "ok": True,
            "keyword": keyword,
            "pct": pct,
            "enabled": False,
            "candidate_count": 0,
            "affected_count": 0,
            "displayed_count": 0,
            "total_candidates": 0,
            "total": 0,
            "shown": 0,
            "rows": [],
        }

    kw = str(keyword or "").strip()
    if not kw:
        raise HTTPException(status_code=400, detail="keyword is empty")
    if not math.isfinite(float(pct)):
        raise HTTPException(status_code=400, detail="pct must be finite")

    candidate_pns = _collect_keyword_candidate_pns(kw)
    if not candidate_pns:
        return {
            "ok": True,
            "keyword": kw,
            "pct": pct,
            "enabled": True,
            "candidate_count": 0,
            "affected_count": 0,
            "displayed_count": 0,
            "total_candidates": 0,
            "total": 0,
            "shown": 0,
            "rows": [],
        }

    origin_rules = copy.deepcopy(pricing_engine_mod.KEYWORD_UPLIFT_RULES)
    base_rules = [
        r for r in copy.deepcopy(pricing_engine_mod.KEYWORD_UPLIFT_RULES)
        if str(r.get("keyword") or "").strip().upper() != kw.upper()
    ]
    preview_rules = copy.deepcopy(base_rules)
    preview_rules.append({"keyword": kw, "pct": float(pct), "enabled": True})

    impacted_rows: list[Dict[str, Any]] = []
    try:
        pricing_engine_mod.KEYWORD_UPLIFT_RULES.clear()
        pricing_engine_mod.KEYWORD_UPLIFT_RULES.extend(base_rules)
        base_results = {pn: pricing_engine_mod.compute_one(_engine.data, pn) for pn in candidate_pns}

        pricing_engine_mod.KEYWORD_UPLIFT_RULES.clear()
        pricing_engine_mod.KEYWORD_UPLIFT_RULES.extend(preview_rules)
        new_results = {pn: pricing_engine_mod.compute_one(_engine.data, pn) for pn in candidate_pns}
    finally:
        pricing_engine_mod.KEYWORD_UPLIFT_RULES.clear()
        pricing_engine_mod.KEYWORD_UPLIFT_RULES.extend(origin_rules)

    for pn in candidate_pns:
        b = base_results.get(pn) or {}
        n = new_results.get(pn) or {}
        bfv = b.get("final_values") or {}
        nfv = n.get("final_values") or {}
        bfob = _to_float_soft(bfv.get("FOB C(EUR)"))
        nfob = _to_float_soft(nfv.get("FOB C(EUR)"))
        if bfob is None or nfob is None:
            continue
        if abs(nfob - bfob) <= 1e-9:
            continue
        delta_pct = (nfob / bfob - 1.0) if bfob else None
        impacted_rows.append(
            {
                "pn": pn,
                "internal_model": str(nfv.get("Internal Model") or "").strip(),
                "external_model": str(nfv.get("External Model") or "").strip(),
                "category": str((n.get("meta") or {}).get("category") or "").strip(),
                "price_group": str((n.get("meta") or {}).get("price_group") or "").strip(),
                "fob_before": bfob,
                "fob_after": nfob,
                "ddp_after": _to_float_soft(nfv.get("DDP A(EUR)")),
                "reseller_after": _to_float_soft(nfv.get("Suggested Reseller(EUR)")),
                "gold_after": _to_float_soft(nfv.get("Gold(EUR)")),
                "silver_after": _to_float_soft(nfv.get("Silver(EUR)")),
                "ivory_after": _to_float_soft(nfv.get("Ivory(EUR)")),
                "msrp_after": _to_float_soft(nfv.get("MSRP(EUR)")),
                "delta_pct": delta_pct,
                "applied_hits": (n.get("meta") or {}).get("sys_keyword_uplift_hits") or [],
            }
        )

    impacted_rows.sort(key=lambda x: (-(x.get("delta_pct") or 0.0), str(x.get("pn") or "")))
    return {
        "ok": True,
        "keyword": kw,
        "pct": float(pct),
        "enabled": True,
        "candidate_count": len(candidate_pns),
        "affected_count": len(impacted_rows),
        "displayed_count": len(impacted_rows),
        "total_candidates": len(candidate_pns),
        "total": len(impacted_rows),
        "shown": len(impacted_rows),
        "rows": impacted_rows,
    }


app = FastAPI(title="Dahua Pricing Auto (Deploy Server)", version="0.2.0")

_engine: Optional[PricingEngine] = None
_agent: Optional[AgentAutomation] = None
_pricing_workflows: Optional[PricingWorkflowStore] = None
_sheet_workflow_ingest: Optional[SheetWorkflowIngestCoordinator] = None
_evolution: Optional[EvolutionControlPlane] = None
_demo_pipeline: Optional[DemoPipeline] = None
_price_data_store: Optional[PriceDataStore] = None


@app.on_event("startup")
def _startup() -> None:
    _ensure_dirs()
    _apply_rule_overrides_if_exist()
    global _engine
    cfg = EngineConfig(runtime_dir=RUNTIME_DIR)
    _engine = PricingEngine(cfg)
    global _price_data_store
    _price_data_store = PriceDataStore(RUNTIME_DIR)
    active_bundle, active_version = _price_data_store.bootstrap()
    _engine.install_data(active_bundle, version=active_version)
    global _agent
    _agent = AgentAutomation(RUNTIME_DIR)
    _agent.ensure_dirs()
    _agent.start_poller(_agent_compute_rows)
    global _pricing_workflows
    _pricing_workflows = PricingWorkflowStore(RUNTIME_DIR)
    _pricing_workflows.ensure_dirs()
    global _sheet_workflow_ingest
    _sheet_workflow_ingest = SheetWorkflowIngestCoordinator(
        RUNTIME_DIR / "agent" / "sheet_workflow_ingest" / "state.json"
    )
    global _evolution
    _evolution = EvolutionControlPlane(
        RUNTIME_DIR,
        execution_enabled=os.getenv("DAHUA_EVOLUTION_EXECUTION_ENABLED", "false").lower() == "true",
    )
    _evolution.ensure_baseline()
    # 演示管线按需初始化：它只服务 /api/demo/*，不应该让定价服务的启动依赖它。


def _require_agent() -> AgentAutomation:
    assert _agent is not None
    return _agent


def _require_pricing_workflows() -> PricingWorkflowStore:
    global _pricing_workflows
    if _pricing_workflows is None:
        _pricing_workflows = PricingWorkflowStore(RUNTIME_DIR)
        _pricing_workflows.ensure_dirs()
    return _pricing_workflows


def _require_sheet_workflow_ingest() -> SheetWorkflowIngestCoordinator:
    global _sheet_workflow_ingest
    if _sheet_workflow_ingest is None:
        _sheet_workflow_ingest = SheetWorkflowIngestCoordinator(
            RUNTIME_DIR / "agent" / "sheet_workflow_ingest" / "state.json"
        )
    return _sheet_workflow_ingest


def _require_evolution() -> EvolutionControlPlane:
    global _evolution
    if _evolution is None:
        _evolution = EvolutionControlPlane(RUNTIME_DIR, execution_enabled=False)
        _evolution.ensure_baseline()
    return _evolution


def _require_demo_pipeline() -> DemoPipeline:
    global _demo_pipeline
    if _demo_pipeline is None:
        _demo_pipeline = DemoPipeline(
            RUNTIME_DIR,
            # 用 lambda 而不是直接传函数对象，保证测试里替换模块级函数后依然生效。
            compute_rows=lambda pns, apply_black_markup: _agent_compute_rows(pns, apply_black_markup),
            workflow_store=_require_pricing_workflows,
            engine_meta=lambda: _engine.meta() if _engine is not None else {"loaded": False},
            execution_context=lambda session_id=None: _workflow_execution_context(session_id),
        )
        _demo_pipeline.ensure_dirs()
    return _demo_pipeline


def _demo_http_error(exc: DemoPipelineError) -> HTTPException:
    if isinstance(exc, DemoRunNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, DemoRunBusy):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def _require_price_data_store() -> PriceDataStore:
    global _price_data_store
    if _price_data_store is None:
        _price_data_store = PriceDataStore(RUNTIME_DIR)
    return _price_data_store


def _require_price_data_admin_token(
    token: Optional[str] = Header(default=None, alias="X-Price-Data-Admin-Token"),
) -> None:
    configured = os.getenv("DAHUA_PRICE_DATA_ADMIN_TOKEN", "")
    # Missing configuration is deliberately fail-closed. Keep the response generic
    # so neither configuration state nor the expected token is disclosed.
    if not configured or not token or not hmac.compare_digest(token, configured):
        raise HTTPException(status_code=403, detail="administrator authorization required")


def _price_data_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, PriceDataNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PriceDataConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, PriceDataError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="price data operation failed")


def _workflow_execution_context(session_id: Optional[str] = None) -> Dict[str, Any]:
    if _evolution is None:  # Direct unit tests do not run FastAPI startup.
        return {
            "run_id": f"run-{uuid.uuid4().hex}",
            "session_id": str(session_id or f"session-{uuid.uuid4().hex}"),
            "environment": "test-unpinned",
            "bundle_hash": "uninitialized",
            "pins": {},
        }
    return _evolution.resolve_execution_context(session_id=session_id)


def _capture_workflow(task: Dict[str, Any]) -> Dict[str, Any]:
    if _evolution is None:
        return {"ok": True, "skipped": "control_plane_not_started"}
    try:
        return _evolution.capture_workflow(task)
    except Exception as exc:
        # The durable task trace remains available for later reconciliation;
        # an observability outage must not repeat or undo a business action.
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:500]}"}


def _with_observability(task: Dict[str, Any]) -> Dict[str, Any]:
    return {**task, "observability": _capture_workflow(task)}


def _evolution_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (ValueError, EvaluationError)):
        return HTTPException(status_code=400, detail=str(exc))
    if "not found" in str(exc).lower() or isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (RegistryError, ReleaseError, EventStoreError)):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")


def _workflow_http_error(exc: WorkflowError) -> HTTPException:
    if isinstance(exc, WorkflowNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, WorkflowConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, WorkflowValidationError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


def _agent_compute_rows(pns: List[str], apply_black_markup: bool) -> Dict[str, Any]:
    assert _engine is not None and _engine.data is not None
    data_snapshot, data_version = _engine.snapshot()
    rows: list[Dict[str, Any]] = []
    items: list[Dict[str, Any]] = []
    not_found: list[str] = []
    warnings: list[Dict[str, Any]] = []
    anchor_applied_count = 0
    anchor_changed_count = 0
    black_markup_applied_count = 0
    atc_markup_applied_count = 0
    anchor_cache: Dict[str, Dict[str, Any]] = {}

    for i, pn in enumerate(pns, start=1):
        row = pricing_engine_mod.compute_one(data_snapshot, pn, apply_black_markup=False)
        _apply_external_model_anchor_to_row(
            row,
            apply_france_anchor=True,
            anchor_cache=anchor_cache,
            data_bundle=data_snapshot,
        )
        pricing_engine_mod.apply_variant_markups(
            data_snapshot,
            row,
            apply=apply_black_markup,
        )
        rows.append(row)
        items.append(_build_batch_review_item(i, row))

        if str(row.get("status", "")).lower() == "not_found":
            not_found.append(str(row.get("pn") or pn))
        row_meta = row.get("meta") or {}
        if row_meta.get("external_model_anchor_applied"):
            anchor_applied_count += 1
        if row_meta.get("external_model_anchor_changed"):
            anchor_changed_count += 1
        black_variant = row_meta.get("black_variant") or {}
        if black_variant.get("applied"):
            black_markup_applied_count += 1
        atc_variant = row_meta.get("atc_variant") or {}
        if atc_variant.get("applied"):
            atc_markup_applied_count += 1
        for w in (row.get("warnings") or []):
            warnings.append({"pn": row.get("pn"), "w": w})

    return {
        "rows": rows,
        "price_data_version": data_version,
        "report": {
            "count_total": len(rows),
            "count_not_found": len(not_found),
            "not_found": not_found,
            "warnings": warnings,
            "count_anchor_applied": anchor_applied_count,
            "count_anchor_changed": anchor_changed_count,
            "count_black_markup_applied": black_markup_applied_count,
            "count_atc_markup_applied": atc_markup_applied_count,
            "items": items,
        },
    }


@app.get("/api/meta")
def meta() -> Dict[str, Any]:
    assert _engine is not None
    return _engine.meta()


@app.get("/api/agent/config")
def agent_get_config() -> Dict[str, Any]:
    return _require_agent().read_config(redacted=True)


@app.put("/api/agent/config")
def agent_put_config(req: AgentConfigReq) -> Dict[str, Any]:
    return _require_agent().save_config(req)


@app.get("/api/agent/state")
def agent_state() -> Dict[str, Any]:
    return _require_agent().read_state()


@app.get("/api/agent/traces")
def agent_traces(limit: int = 50) -> Dict[str, Any]:
    return _require_agent().list_agent_traces(limit=limit)


@app.get("/api/agent/traces/{run_id}")
def agent_trace(run_id: str) -> Dict[str, Any]:
    return _require_agent().read_agent_trace(run_id)


@app.get("/api/agent/events")
def agent_events(limit: int = 50) -> Dict[str, Any]:
    return _require_agent().list_business_events(limit=limit)


@app.get("/api/agent/events/{event_id}")
def agent_event(event_id: str) -> Dict[str, Any]:
    return _require_agent().read_business_event(event_id)


@app.get("/api/agent/sessions")
def agent_sessions(limit: int = 50) -> Dict[str, Any]:
    return _require_agent().list_agent_sessions(limit=limit)


@app.get("/api/agent/sessions/{session_id}")
def agent_session(session_id: str) -> Dict[str, Any]:
    return _require_agent().read_agent_session(session_id)


@app.get("/api/agent/contexts")
def agent_contexts(limit: int = 50) -> Dict[str, Any]:
    return _require_agent().list_context_packages(limit=limit)


@app.get("/api/agent/contexts/{context_id}")
def agent_context(context_id: str) -> Dict[str, Any]:
    return _require_agent().read_context_package(context_id)


@app.get("/api/agent/skill-calls")
def agent_skill_calls(limit: int = 50) -> Dict[str, Any]:
    return _require_agent().list_skill_calls(limit=limit)


@app.get("/api/agent/dispatches")
def agent_dispatches(limit: int = 50) -> Dict[str, Any]:
    return _require_agent().list_dispatch_decisions(limit=limit)


@app.get("/api/agent/observations")
def agent_observations(limit: int = 50) -> Dict[str, Any]:
    return _require_agent().list_observations(limit=limit)


@app.get("/api/agent/memory-updates")
def agent_memory_updates(limit: int = 50) -> Dict[str, Any]:
    return _require_agent().list_memory_updates(limit=limit)


@app.get("/api/agent/memory/pla/{pla_no}")
def agent_pla_timeline(pla_no: str) -> Dict[str, Any]:
    return _require_agent().read_pla_timeline(pla_no)


@app.get("/api/agent/skills")
def agent_skills() -> Dict[str, Any]:
    return _require_agent().list_agent_skills()


@app.get("/api/agent/skills/{skill_name}")
def agent_skill(skill_name: str) -> Dict[str, Any]:
    return _require_agent().read_agent_skill(skill_name)


@app.get("/api/agent/tool-backends")
def agent_tool_backends() -> Dict[str, Any]:
    return _require_agent().list_tool_backends()


@app.post("/api/agent/tool-backends/heartbeat")
def agent_tool_backend_heartbeat(req: AgentToolBackendHeartbeatReq) -> Dict[str, Any]:
    return _require_agent().record_tool_backend_heartbeat(req)


@app.get("/api/agent/reflections")
def agent_reflections(limit: int = 50) -> Dict[str, Any]:
    return _require_agent().list_agent_reflections(limit=limit)


@app.post("/api/agent/evals/replay")
def agent_replay_eval(req: AgentReplayEvalReq) -> Dict[str, Any]:
    return _require_agent().run_replay_eval(req)


@app.post("/api/agent/dingtalk/event")
def agent_dingtalk_event(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be object")
    return _require_agent().handle_dingtalk_event(payload, headers=dict(request.headers), source="dingtalk")


@app.post("/api/agent/pricing-task")
def agent_create_pricing_task(req: PricingWorkflowCreateReq) -> Dict[str, Any]:
    cfg = _require_agent().read_config(redacted=False)
    if req.submission_authorized:
        _require_agent()._check_desktop_agent_token(req.token)
    try:
        task = _require_pricing_workflows().create(
            req,
            _agent_compute_rows,
            default_apply_black_markup=bool(cfg.get("apply_black_markup", True)),
            execution_context=_workflow_execution_context(req.session_id),
        )
        visible = task if req.submission_authorized else PricingWorkflowStore.redact(task)
        return {**visible, "observability": _capture_workflow(task)}
    except WorkflowError as exc:
        raise _workflow_http_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/agent/pricing-workflows")
def agent_pricing_workflows(limit: int = 50, state: Optional[str] = None) -> Dict[str, Any]:
    try:
        return _require_pricing_workflows().list(limit=limit, state=state)
    except WorkflowError as exc:
        raise _workflow_http_error(exc) from exc


@app.post("/api/agent/pricing-workflows/claim")
def agent_claim_pricing_workflow(req: PricingWorkflowClaimReq) -> Dict[str, Any]:
    _require_agent()._check_desktop_agent_token(req.token)
    try:
        task = _require_pricing_workflows().claim(
            worker_id=req.worker_id,
            capabilities=req.capabilities,
            lease_seconds=req.lease_seconds,
        )
        return _with_observability(task) if task.get("claimed") else task
    except WorkflowError as exc:
        raise _workflow_http_error(exc) from exc


@app.post("/api/agent/pricing-workflows/{task_id}/report")
def agent_report_pricing_workflow(task_id: str, req: PricingWorkflowReportReq) -> Dict[str, Any]:
    _require_agent()._check_desktop_agent_token(req.token)
    try:
        task = _require_pricing_workflows().report(
            task_id,
            worker_id=req.worker_id,
            lease_id=req.lease_id,
            report_id=req.report_id,
            outcome=req.outcome,
            pla_no=req.pla_no,
            detail=req.detail,
            evidence=req.evidence,
        )
        return _with_observability(task)
    except WorkflowError as exc:
        raise _workflow_http_error(exc) from exc


@app.post("/api/agent/pricing-workflows/{task_id}/retry")
def agent_retry_pricing_workflow(task_id: str, req: PricingWorkflowRetryReq) -> Dict[str, Any]:
    _require_agent()._check_desktop_agent_token(req.token)
    try:
        task = _require_pricing_workflows().retry(
            task_id,
            reason=req.reason,
            target_state=req.target_state,
            expected_version=req.expected_version,
        )
        return _with_observability(task)
    except WorkflowError as exc:
        raise _workflow_http_error(exc) from exc


@app.get("/api/agent/tasks/{task_id}")
def agent_task_status(task_id: str) -> Dict[str, Any]:
    task = _require_agent().read_task(task_id)
    if task.get("schema_version") == 1:
        return PricingWorkflowStore.redact(task)
    return task


@app.get("/api/agent/sheet/parsed/latest")
def agent_sheet_parsed_latest() -> Dict[str, Any]:
    return _require_agent().read_parsed_sheet_push("latest")


@app.get("/api/agent/sheet/parsed/{push_id}")
def agent_sheet_parsed(push_id: str) -> Dict[str, Any]:
    return _require_agent().read_parsed_sheet_push(push_id)


@app.post("/api/agent/gsp/status-queue")
def agent_gsp_status_queue(req: AgentGspQueueReq) -> Dict[str, Any]:
    return _require_agent().gsp_status_queue(req)


@app.post("/api/agent/gsp/status-result")
def agent_gsp_status_result(req: AgentGspStatusResultReq) -> Dict[str, Any]:
    return _require_agent().save_gsp_status_result(req)


@app.post("/api/agent/gsp/under-approval-queue")
def agent_gsp_under_approval_queue(req: AgentGspUnderApprovalQueueReq) -> Dict[str, Any]:
    return _require_agent().gsp_under_approval_queue(req)


@app.post("/api/agent/gsp/under-approval-result")
def agent_gsp_under_approval_result(req: AgentGspUnderApprovalResultReq) -> Dict[str, Any]:
    return _require_agent().save_gsp_under_approval_result(req)


@app.options("/api/agent/sheet/status-updates/pending")
def agent_sheet_status_updates_pending_options(request: Request, response: Response) -> Dict[str, Any]:
    _agent_sheet_push_cors(request, response)
    return {"ok": True}


@app.post("/api/agent/sheet/status-updates/pending")
def agent_sheet_status_updates_pending(
    req: AgentSheetStatusUpdatesReq,
    request: Request,
    response: Response,
) -> Dict[str, Any]:
    _agent_sheet_push_cors(request, response)
    return _require_agent().sheet_status_updates(req)


@app.options("/api/agent/sheet/status-updates/ack")
def agent_sheet_status_updates_ack_options(request: Request, response: Response) -> Dict[str, Any]:
    _agent_sheet_push_cors(request, response)
    return {"ok": True}


@app.post("/api/agent/sheet/status-updates/ack")
def agent_sheet_status_updates_ack(
    req: AgentSheetStatusUpdateAckReq,
    request: Request,
    response: Response,
) -> Dict[str, Any]:
    _agent_sheet_push_cors(request, response)
    return _require_agent().ack_sheet_status_updates(req)


@app.get("/api/agent/tasks/{task_id}/download")
def agent_task_download(task_id: str) -> FileResponse:
    _ = task_id
    raise HTTPException(status_code=403, detail="agent pricing task download is disabled in sheet-probe phase")


@app.post("/api/agent/sheet/probe")
def agent_sheet_probe(req: AgentSheetProbeReq) -> Dict[str, Any]:
    return _require_agent().probe_sheet_source(req)


def _agent_sheet_push_cors(request: Request, response: Response) -> None:
    origin = request.headers.get("origin") or ""
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    requested_headers = request.headers.get("access-control-request-headers")
    response.headers["Access-Control-Allow-Headers"] = requested_headers or "content-type, authorization"
    response.headers["Access-Control-Max-Age"] = "600"


@app.options("/api/agent/sheet/push")
def agent_sheet_push_options(request: Request, response: Response) -> Dict[str, Any]:
    _agent_sheet_push_cors(request, response)
    return {"ok": True}


@app.post("/api/agent/sheet/push")
def agent_sheet_push(req: AgentSheetPushReq, request: Request, response: Response) -> Dict[str, Any]:
    _agent_sheet_push_cors(request, response)
    agent = _require_agent()
    received = agent.receive_sheet_push(req)
    raw_payload = req.payload if isinstance(req.payload, dict) else {}
    spreadsheet_id = str(raw_payload.get("spreadsheetId") or "").strip()
    if not spreadsheet_id:
        return {**received, "workflow_ingest": {"ok": True, "skipped": "spreadsheetId missing"}}
    parsed = agent.read_parsed_sheet_push(str(received.get("push_id") or "latest"))
    cfg = agent.read_config(redacted=False)

    def create_safe_workflow(workflow_req: PricingWorkflowCreateReq) -> Dict[str, Any]:
        task = _require_pricing_workflows().create(
            workflow_req,
            _agent_compute_rows,
            default_apply_black_markup=bool(cfg.get("apply_black_markup", True)),
            execution_context=_workflow_execution_context(workflow_req.session_id),
        )
        _capture_workflow(task)
        return task

    workflow_ingest = _require_sheet_workflow_ingest().process(
        parsed,
        spreadsheet_id=spreadsheet_id,
        allow_new_rows=bool(cfg.get("sheet_workflow_auto_create_enabled", False)),
        create_workflow=create_safe_workflow,
    )
    return {**received, "workflow_ingest": workflow_ingest}


@app.post("/api/agent/sheet/workflow-backfill")
def agent_sheet_workflow_backfill(req: SheetWorkflowBackfillReq) -> Dict[str, Any]:
    """Two-step preview/apply for already-existing pending Sheet rows.

    Applying is allowed only while the whole agent is in dry-run mode with
    group replies disabled.  It creates pricing/manual-review tasks but cannot
    authorize GSP, notify a group, or lease work to the Windows agent.
    """

    agent = _require_agent()
    agent._check_desktop_agent_token(req.token)
    cfg = agent.read_config(redacted=False)
    if not bool(cfg.get("dry_run")) or bool(cfg.get("group_reply_enabled")):
        raise HTTPException(status_code=409, detail="backfill requires dry_run=true and group_reply_enabled=false")
    parsed = agent.read_parsed_sheet_push(req.push_id)

    def create_safe_workflow(workflow_req: PricingWorkflowCreateReq) -> Dict[str, Any]:
        if workflow_req.submission_authorized or workflow_req.notify or workflow_req.gsp_payload:
            raise WorkflowValidationError("backfill request attempted an external side effect")
        task = _require_pricing_workflows().create(
            workflow_req,
            _agent_compute_rows,
            default_apply_black_markup=bool(cfg.get("apply_black_markup", True)),
            execution_context=_workflow_execution_context(workflow_req.session_id),
        )
        _capture_workflow(task)
        return task

    return _require_sheet_workflow_ingest().backfill(
        parsed,
        spreadsheet_id=req.source_id,
        expected_manifest_hash=req.expected_manifest_hash,
        apply=req.apply,
        limit=req.limit,
        create_workflow=create_safe_workflow,
    )


@app.post("/api/agent/poller/run-once")
def agent_poller_run_once() -> Dict[str, Any]:
    return _require_agent().run_poll_once()


# =========================
# Evolution control-plane APIs (no external side effects)
# =========================

@app.get("/api/evolution/status")
def evolution_status() -> Dict[str, Any]:
    control = _require_evolution()
    bundle = control.registry.resolve_bundle("production")
    releases = control.releases.list(environment="production")
    return {
        "ok": True,
        "mode": "shadow_only" if not control.releases.execution_enabled else "execution_eligible",
        "active_bundle": {"bundle_hash": bundle["bundle_hash"], "pins": bundle["pins"]},
        "event_count": len(control.events.query()),
        "release_count": releases["count"],
        "windows_agent_allowed": False,
        "notifications_allowed": False,
    }


@app.get("/api/evolution/events")
def evolution_events(
    task_id: Optional[str] = None,
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    try:
        events = _require_evolution().events.query(
            task_id=task_id,
            run_id=run_id,
            session_id=session_id,
            limit=max(1, min(limit, 1000)),
        )
        return {"count": len(events), "events": events}
    except Exception as exc:
        raise _evolution_http_error(exc) from exc


@app.get("/api/evolution/replay/{task_id}")
def evolution_replay(task_id: str) -> Dict[str, Any]:
    try:
        return _require_evolution().events.export_replay_bundle(task_id)
    except Exception as exc:
        raise _evolution_http_error(exc) from exc


@app.get("/api/evolution/artifacts")
def evolution_artifacts(
    kind: Optional[str] = None,
    name: Optional[str] = None,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        rows = _require_evolution().registry.list(kind=kind, name=name, status=status)
        return {"count": len(rows), "artifacts": rows}
    except Exception as exc:
        raise _evolution_http_error(exc) from exc


@app.get("/api/evolution/cases")
def evolution_cases(status: Optional[str] = None) -> Dict[str, Any]:
    try:
        return _require_evolution().cases.list(status=status)
    except Exception as exc:
        raise _evolution_http_error(exc) from exc


@app.get("/api/evolution/cases/evaluation-dataset")
def evolution_case_dataset() -> Dict[str, Any]:
    try:
        return _require_evolution().cases.evaluation_cases()
    except Exception as exc:
        raise _evolution_http_error(exc) from exc


@app.get("/api/evolution/cases/suggestions")
def evolution_case_suggestions(minimum_occurrences: int = 3) -> Dict[str, Any]:
    try:
        return _require_evolution().cases.suggestions(minimum_occurrences=minimum_occurrences)
    except Exception as exc:
        raise _evolution_http_error(exc) from exc


@app.post("/api/evolution/cases/{case_id}/review")
def evolution_review_case(case_id: str, req: EvolutionCaseReviewReq) -> Dict[str, Any]:
    _require_agent()._check_desktop_agent_token(req.token)
    try:
        return _require_evolution().cases.review(
            case_id,
            disposition=req.disposition,
            reviewer=req.reviewer,
            rationale=req.rationale,
            expected_terminal_state=req.expected_terminal_state,
            expected_action=req.expected_action,
            strata=req.strata,
        )
    except Exception as exc:
        raise _evolution_http_error(exc) from exc


@app.post("/api/evolution/artifacts")
def evolution_register_artifact(req: EvolutionArtifactReq) -> Dict[str, Any]:
    _require_agent()._check_desktop_agent_token(req.token)
    try:
        return _require_evolution().registry.register(
            req.kind,
            req.name,
            req.version,
            req.content,
            parent_version=req.parent_version,
            source=req.source,
            created_by=req.created_by,
            json_schema=req.json_schema,
            business_constraints=req.business_constraints,
            metadata=req.metadata,
            status="draft",
        )
    except Exception as exc:
        raise _evolution_http_error(exc) from exc


@app.post("/api/evolution/artifacts/{kind}/{name}/{version}/transition")
def evolution_transition_artifact(
    kind: str,
    name: str,
    version: str,
    req: EvolutionArtifactTransitionReq,
) -> Dict[str, Any]:
    _require_agent()._check_desktop_agent_token(req.token)
    try:
        return _require_evolution().registry.transition(
            kind,
            name,
            version,
            req.to_status,
            actor=req.actor,
            reason=req.reason,
        )
    except Exception as exc:
        raise _evolution_http_error(exc) from exc


@app.get("/api/evolution/bundles/{environment}")
def evolution_bundle(environment: str = "production") -> Dict[str, Any]:
    try:
        return _require_evolution().registry.resolve_bundle(environment)
    except Exception as exc:
        raise _evolution_http_error(exc) from exc


@app.post("/api/evolution/evaluations")
def evolution_run_evaluation(req: EvolutionEvaluationReq) -> Dict[str, Any]:
    _require_agent()._check_desktop_agent_token(req.token)
    try:
        return _require_evolution().run_precomputed_evaluation(req.payload)
    except Exception as exc:
        raise _evolution_http_error(exc) from exc


@app.get("/api/evolution/evaluations/{evaluation_id}")
def evolution_evaluation(evaluation_id: str) -> Dict[str, Any]:
    try:
        return _require_evolution().read_evaluation(evaluation_id)
    except Exception as exc:
        raise _evolution_http_error(exc) from exc


@app.get("/api/evolution/releases")
def evolution_releases(environment: Optional[str] = None) -> Dict[str, Any]:
    try:
        return _require_evolution().releases.list(environment=environment)
    except Exception as exc:
        raise _evolution_http_error(exc) from exc


@app.post("/api/evolution/releases")
def evolution_propose_release(req: EvolutionReleaseProposalReq) -> Dict[str, Any]:
    _require_agent()._check_desktop_agent_token(req.token)
    try:
        return _require_evolution().propose_release(
            evaluation_id=req.evaluation_id,
            candidate_pins=req.candidate_pins,
            baseline_pins=req.baseline_pins,
            environment=req.environment,
            actor=req.actor,
            release_key=req.release_key,
        )
    except Exception as exc:
        raise _evolution_http_error(exc) from exc


@app.post("/api/evolution/releases/{release_id}/shadow")
def evolution_start_shadow(release_id: str, req: EvolutionActorReq) -> Dict[str, Any]:
    _require_agent()._check_desktop_agent_token(req.token)
    try:
        return _require_evolution().releases.start_shadow(release_id, actor=req.actor)
    except Exception as exc:
        raise _evolution_http_error(exc) from exc


@app.post("/api/evolution/releases/{release_id}/metric-windows")
def evolution_record_window(release_id: str, req: EvolutionWindowReq) -> Dict[str, Any]:
    _require_agent()._check_desktop_agent_token(req.token)
    try:
        return _require_evolution().record_release_window(
            release_id,
            window_id=req.window_id,
            metrics=req.metrics,
            actor=req.actor,
        )
    except Exception as exc:
        raise _evolution_http_error(exc) from exc


@app.post("/api/evolution/releases/{release_id}/canary")
def evolution_start_canary(release_id: str, req: EvolutionCanaryReq) -> Dict[str, Any]:
    _require_agent()._check_desktop_agent_token(req.token)
    try:
        return _require_evolution().releases.start_canary(
            release_id,
            fraction=req.fraction,
            actor=req.actor,
            approved_by=req.approved_by,
        )
    except Exception as exc:
        raise _evolution_http_error(exc) from exc


@app.post("/api/evolution/releases/{release_id}/promote")
def evolution_promote(release_id: str, req: EvolutionPromoteReq) -> Dict[str, Any]:
    _require_agent()._check_desktop_agent_token(req.token)
    try:
        return _require_evolution().promote_release(
            release_id,
            actor=req.actor,
            approved_by=req.approved_by,
        )
    except Exception as exc:
        raise _evolution_http_error(exc) from exc


@app.post("/api/evolution/releases/{release_id}/rollback")
def evolution_rollback(release_id: str, req: EvolutionRollbackReq) -> Dict[str, Any]:
    _require_agent()._check_desktop_agent_token(req.token)
    try:
        return _require_evolution().rollback_release(
            release_id,
            reason=req.reason,
            actor=req.actor,
        )
    except Exception as exc:
        raise _evolution_http_error(exc) from exc


@app.get("/api/demo/config")
def demo_config() -> Dict[str, Any]:
    return _require_demo_pipeline().config()


@app.post("/api/demo/run")
def demo_run(req: DemoRunReq) -> Dict[str, Any]:
    try:
        return _require_demo_pipeline().start_run(req)
    except DemoPipelineError as exc:
        raise _demo_http_error(exc) from exc


@app.get("/api/demo/runs")
def demo_runs(limit: int = 50) -> Dict[str, Any]:
    return _require_demo_pipeline().list_runs(limit=limit)


@app.get("/api/demo/runs/{run_id}")
def demo_run_snapshot(run_id: str) -> Dict[str, Any]:
    try:
        return _require_demo_pipeline().snapshot(run_id)
    except DemoPipelineError as exc:
        raise _demo_http_error(exc) from exc


@app.get("/api/demo/runs/{run_id}/stream")
def demo_run_stream(run_id: str) -> StreamingResponse:
    try:
        events = _require_demo_pipeline().stream(run_id)
    except DemoPipelineError as exc:
        raise _demo_http_error(exc) from exc
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/demo/runs/{run_id}/artifact")
def demo_run_artifact(run_id: str) -> FileResponse:
    try:
        path, filename = _require_demo_pipeline().artifact(run_id)
    except DemoPipelineError as exc:
        raise _demo_http_error(exc) from exc
    return FileResponse(
        path=str(path),
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.post("/api/query")
def query_one(req: QueryReq) -> Dict[str, Any]:
    assert _engine is not None
    pn = (req.pn or "").strip()
    if not pn:
        raise HTTPException(status_code=400, detail="pn is empty")
    return _engine.query_one(pn, apply_black_markup=bool(req.apply_black_markup))


@app.get("/api/query/options")
def query_options() -> Dict[str, Any]:
    category_price_groups = _build_category_price_groups()
    group_rule_keys = {
        str(g): sorted([str(k) for k in (v.keys() if isinstance(v, dict) else [])])
        for g, v in pricing_rules_mod.PRICE_RULES.items()
    }
    return {
        "categories": sorted([str(k) for k in pricing_rules_mod.DDP_RULES.keys()]),
        "price_groups": sorted([str(k) for k in pricing_rules_mod.PRICE_RULES.keys()]),
        "category_price_groups": category_price_groups,
        "group_rule_keys": group_rule_keys,
    }


@app.post("/api/query/recompute")
def query_recompute(req: QueryRecomputeReq) -> Dict[str, Any]:
    assert _engine is not None
    pn = (req.pn or "").strip()
    if not pn:
        raise HTTPException(status_code=400, detail="pn is empty")

    force_category = _norm_optional_text(req.force_category)
    force_price_group = _norm_optional_text(req.force_price_group)
    force_series_key = _norm_optional_text(req.force_series_key)
    try:
        manual_final_values = pricing_engine_mod.normalize_manual_final_price_overrides(
            req.manual_final_values
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _validate_query_overrides(
        force_category,
        force_price_group,
        force_series_key,
        require_any=not bool(manual_final_values),
    )
    manual_sys_basis_price_used = _normalize_manual_price_override(
        req.manual_sys_basis_price_used,
        "manual_sys_basis_price_used",
    )
    manual_fob = _normalize_manual_price_override(req.manual_fob, "manual_fob")
    manual_price_field = _normalize_manual_price_field(req.manual_price_field)
    manual_price_value = _normalize_manual_price_override(req.manual_price_value, "manual_price_value")
    _validate_manual_recompute_inputs(
        manual_sys_basis_price_used,
        manual_fob,
        manual_price_field,
        manual_price_value,
    )

    try:
        return _engine.query_one(
            pn,
            force_category=force_category,
            force_price_group=force_price_group,
            force_series_key=force_series_key,
            force_full_recalc=bool(
                force_category
                or force_price_group
                or manual_sys_basis_price_used is not None
                or manual_fob is not None
                or manual_price_value is not None
            ),
            manual_sys_basis_price_used=manual_sys_basis_price_used,
            manual_fob=manual_fob,
            manual_price_field=manual_price_field,
            manual_price_value=manual_price_value,
            manual_final_values=manual_final_values,
            apply_black_markup=bool(req.apply_black_markup),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/query/export")
def query_export(req: QueryExportReq) -> FileResponse:
    assert _engine is not None
    pn = (req.pn or "").strip()
    if not pn:
        raise HTTPException(status_code=400, detail="pn is empty")

    force_category = _norm_optional_text(req.force_category)
    force_price_group = _norm_optional_text(req.force_price_group)
    force_series_key = _norm_optional_text(req.force_series_key)
    try:
        manual_final_values = pricing_engine_mod.normalize_manual_final_price_overrides(
            req.manual_final_values
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _validate_query_overrides(
        force_category,
        force_price_group,
        force_series_key,
        require_any=False,
    )
    manual_sys_basis_price_used = _normalize_manual_price_override(
        req.manual_sys_basis_price_used,
        "manual_sys_basis_price_used",
    )
    manual_fob = _normalize_manual_price_override(req.manual_fob, "manual_fob")
    manual_price_field = _normalize_manual_price_field(req.manual_price_field)
    manual_price_value = _normalize_manual_price_override(req.manual_price_value, "manual_price_value")
    _validate_manual_recompute_inputs(
        manual_sys_basis_price_used,
        manual_fob,
        manual_price_field,
        manual_price_value,
    )

    try:
        result = _engine.query_one(
            pn,
            force_category=force_category,
            force_price_group=force_price_group,
            force_series_key=force_series_key,
            force_full_recalc=bool(req.force_full_recalc),
            manual_sys_basis_price_used=manual_sys_basis_price_used,
            manual_fob=manual_fob,
            manual_price_field=manual_price_field,
            manual_price_value=manual_price_value,
            manual_final_values=manual_final_values,
            apply_black_markup=bool(req.apply_black_markup),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if str(result.get("status", "")).lower() != "ok":
        raise HTTPException(status_code=404, detail=f"pn not found: {pn}")

    export_dir = OUTPUTS_DIR / f"single_export_{uuid.uuid4().hex[:16]}"
    export_dir.mkdir(parents=True, exist_ok=True)
    frames = build_export_frames([result])
    out_path = write_export_xlsx(frames, out_dir=export_dir, level="country")

    safe_pn = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in pn).strip("_")
    if not safe_pn:
        safe_pn = "part"
    download_name = f"{safe_pn}_Country_import_upload_Model.xlsx"

    return FileResponse(
        path=str(out_path),
        filename=download_name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.post("/api/query/external-model-index")
def query_external_model_index(req: ExternalModelReq) -> Dict[str, Any]:
    assert _engine is not None
    pn = (req.pn or "").strip()
    if not pn:
        raise HTTPException(status_code=400, detail="pn is empty")
    return _query_cluster_by_external_model(pn, apply_france_anchor=bool(req.apply_france_anchor))


@app.post("/api/models/search")
def model_search(req: ModelSearchReq) -> Dict[str, Any]:
    return _search_models(req)


@app.post("/api/query/external-model-export")
def query_external_model_export(req: ExternalModelReq) -> FileResponse:
    assert _engine is not None
    pn = (req.pn or "").strip()
    if not pn:
        raise HTTPException(status_code=400, detail="pn is empty")

    cluster = _query_cluster_by_external_model(pn, apply_france_anchor=bool(req.apply_france_anchor))
    rows = list(cluster.get("rows") or [])
    if not rows:
        raise HTTPException(status_code=404, detail=f"no cluster rows for pn: {pn}")

    external_model = _norm_optional_text(cluster.get("external_model")) or "external_model"
    safe_ext = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in external_model).strip("_")
    if not safe_ext:
        safe_ext = "external_model"

    export_dir = OUTPUTS_DIR / f"external_model_export_{uuid.uuid4().hex[:16]}"
    export_dir.mkdir(parents=True, exist_ok=True)
    frames = build_export_frames(rows)
    out_path = write_export_xlsx(frames, out_dir=export_dir, level="country")
    download_name = f"{safe_ext}_all_Country_import_upload_Model.xlsx"
    return FileResponse(
        path=str(out_path),
        filename=download_name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _build_batch_review_item(idx: int, row: Dict[str, Any]) -> Dict[str, Any]:
    fv = row.get("final_values") or {}
    meta = row.get("meta") or {}
    black_variant = meta.get("black_variant") or {}
    atc_variant = meta.get("atc_variant") or {}
    anchor_applied = bool(meta.get("external_model_anchor_applied"))
    anchor_pn = _norm_optional_text(meta.get("external_model_anchor_pn"))
    used_sys = bool(meta.get("used_sys"))
    sys_basis_field = _norm_optional_text(meta.get("sys_basis_field"))
    price_source = "SYS_FLOOR" if used_sys else ("FR_ANCHOR" if anchor_applied else "COUNTRY")
    if used_sys and sys_basis_field:
        price_source = f"SYS_FLOOR({sys_basis_field})"
    if anchor_applied and anchor_pn:
        price_source = f"FR_ANCHOR({anchor_pn})"
    if black_variant.get("applied"):
        white_pn = _norm_optional_text(black_variant.get("white_pn"))
        markup = black_variant.get("markup_eur")
        markup_text = f"{markup:g}" if isinstance(markup, (int, float)) else "?"
        price_source = f"BLACK+{markup_text}({white_pn})" if white_pn else f"BLACK+{markup_text}"
    elif black_variant.get("eligible"):
        white_pn = _norm_optional_text(black_variant.get("white_pn"))
        price_source = f"BLACK WHITE FOUND({white_pn})" if white_pn else "BLACK WHITE FOUND"
    if atc_variant.get("applied"):
        base_pn = _norm_optional_text(atc_variant.get("base_pn"))
        delta = atc_variant.get("markup_eur")
        price_source = f"ATC+SYS({base_pn}, +{delta:g})" if base_pn and isinstance(delta, (int, float)) else "ATC+SYS"
    elif atc_variant.get("eligible"):
        base_pn = _norm_optional_text(atc_variant.get("base_pn"))
        price_source = f"ATC BASE FOUND({base_pn})" if base_pn else "ATC BASE FOUND"
    return {
        "idx": idx,
        "pn": row.get("pn"),
        "status": row.get("status"),
        "internal_model": fv.get("Internal Model"),
        "external_model": fv.get("External Model"),
        "category": meta.get("category"),
        "price_group": meta.get("price_group"),
        "series_display": meta.get("series_display"),
        "series_key": meta.get("series_key"),
        "pricing_rule_name": meta.get("pricing_rule_name"),
        "price_priority": meta.get("price_priority"),
        "fr_match_mode": meta.get("fr_match_mode"),
        "fr_matched_pn": meta.get("fr_matched_pn"),
        "sys_match_mode": meta.get("sys_match_mode"),
        "sys_matched_pn": meta.get("sys_matched_pn"),
        "price_source": price_source,
        "black_variant": black_variant,
        "atc_variant": atc_variant,
        "black_markup_applied": bool(black_variant.get("applied")),
        "black_white_pn": black_variant.get("white_pn"),
        "black_white_internal_model": black_variant.get("white_internal_model"),
        "atc_markup_applied": bool(atc_variant.get("applied")),
        "atc_base_pn": atc_variant.get("base_pn"),
        "atc_base_internal_model": atc_variant.get("base_internal_model"),
        "anchor_changed": bool(meta.get("external_model_anchor_changed")),
        "used_sys": used_sys,
        "fob": fv.get("FOB C(EUR)"),
        "ddp": fv.get("DDP A(EUR)"),
        "reseller": fv.get("Suggested Reseller(EUR)"),
        "gold": fv.get("Gold(EUR)"),
        "silver": fv.get("Silver(EUR)"),
        "ivory": fv.get("Ivory(EUR)"),
        "msrp": fv.get("MSRP(EUR)"),
        "warnings": list(row.get("warnings") or []),
    }


def _run_batch_job(job_id: str) -> None:
    assert _engine is not None
    data_snapshot, data_version = _engine.snapshot()
    state = _read_state(job_id)
    input_path = Path(state.get("input_path") or "")
    level_norm = str(state.get("level") or "country").strip().lower() or "country"
    apply_black_markup = bool(state.get("apply_black_markup"))
    price_priority = pricing_engine_mod.normalize_price_priority(state.get("price_priority"))
    out_dir = OUTPUTS_DIR / job_id

    try:
        state["status"] = "running"
        state["started_at"] = _utc_now_iso()
        state["price_data_version"] = data_version
        _write_state(job_id, state)

        pns_raw = parse_pn_list_file(input_path)
        pns = [str(x).strip() for x in pns_raw if str(x).strip()]
        total = len(pns)
        state["progress_total"] = total
        state["progress_done"] = 0
        state["progress_percent"] = 0.0
        state["progress_current_pn"] = None
        state["progress_anchor_applied"] = 0
        state["progress_anchor_changed"] = 0
        state["progress_black_markup_applied"] = 0
        state["progress_atc_markup_applied"] = 0
        state["progress_not_found"] = 0
        _write_state(job_id, state)

        results: list[Dict[str, Any]] = []
        items: list[Dict[str, Any]] = []
        not_found: list[str] = []
        warnings: list[Dict[str, Any]] = []
        anchor_applied_count = 0
        anchor_changed_count = 0
        black_markup_applied_count = 0
        atc_markup_applied_count = 0
        anchor_cache: Dict[str, Dict[str, Any]] = {}

        for i, pn in enumerate(pns, start=1):
            row = pricing_engine_mod.compute_one(
                data_snapshot,
                pn,
                apply_black_markup=False,
                price_priority=price_priority,
            )
            use_country_anchor = bool(
                price_priority == pricing_engine_mod.PRICE_PRIORITY_COUNTRY_FIRST
                or not (row.get("meta") or {}).get("used_sys")
            )
            _apply_external_model_anchor_to_row(
                row,
                apply_france_anchor=use_country_anchor,
                anchor_cache=anchor_cache,
                data_bundle=data_snapshot,
            )
            pricing_engine_mod.apply_variant_markups(
                data_snapshot,
                row,
                apply=apply_black_markup,
            )

            results.append(row)
            items.append(_build_batch_review_item(i, row))

            if str(row.get("status", "")).lower() == "not_found":
                not_found.append(str(row.get("pn") or pn))
            row_meta = row.get("meta") or {}
            if row_meta.get("external_model_anchor_applied"):
                anchor_applied_count += 1
            if row_meta.get("external_model_anchor_changed"):
                anchor_changed_count += 1
            black_variant = row_meta.get("black_variant") or {}
            if black_variant.get("applied"):
                black_markup_applied_count += 1
            atc_variant = row_meta.get("atc_variant") or {}
            if atc_variant.get("applied"):
                atc_markup_applied_count += 1
            for w in (row.get("warnings") or []):
                warnings.append({"pn": row.get("pn"), "w": w})

            state["progress_done"] = i
            state["progress_percent"] = round((i * 100.0 / total), 2) if total > 0 else 100.0
            state["progress_current_pn"] = pn
            state["progress_anchor_applied"] = anchor_applied_count
            state["progress_anchor_changed"] = anchor_changed_count
            state["progress_black_markup_applied"] = black_markup_applied_count
            state["progress_atc_markup_applied"] = atc_markup_applied_count
            state["progress_not_found"] = len(not_found)
            _write_state(job_id, state)

        frames = build_export_frames(results)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = write_export_xlsx(frames, out_dir=out_dir, level=level_norm)
        if not out_file.exists():
            raise RuntimeError(f"output file missing: {out_file}")

        report = {
            "count_total": len(results),
            "count_not_found": len(not_found),
            "not_found": not_found,
            "warnings": warnings,
            "count_anchor_applied": anchor_applied_count,
            "count_anchor_changed": anchor_changed_count,
            "count_black_markup_applied": black_markup_applied_count,
            "count_atc_markup_applied": atc_markup_applied_count,
            "items": items,
        }

        state["status"] = "done"
        state["finished_at"] = _utc_now_iso()
        state["output_files"] = [str(out_file)]
        state["report"] = report
        state["progress_done"] = len(results)
        state["progress_total"] = len(results)
        state["progress_percent"] = 100.0
        state["progress_current_pn"] = None
        state["error"] = None
        _write_state(job_id, state)
    except Exception as e:
        state["status"] = "failed"
        state["finished_at"] = _utc_now_iso()
        state["error"] = f"{type(e).__name__}: {e}"
        _write_state(job_id, state)


@app.post("/api/batch")
def batch(
    level: str = Form(..., description="country | country_customer"),
    apply_black_markup: bool = Form(False, description="apply detected variant markups (Black policy, ATC Sys delta)"),
    price_priority: str = Form(
        pricing_engine_mod.PRICE_PRIORITY_COUNTRY_FIRST,
        description="country_first | system_first",
    ),
    file: UploadFile = File(...),
) -> Dict[str, Any]:
    """
    批量导出（已统一为 country 导出结构）：
    - 兼容接收 country / country_customer
    - 实际导出文件统一为 Country_import_upload_Model.xlsx
    """
    assert _engine is not None
    level_input = (level or "").strip().lower()
    if level_input not in ("country", "country_customer"):
        raise HTTPException(status_code=400, detail="level must be country or country_customer")
    try:
        price_priority_norm = pricing_engine_mod.normalize_price_priority(price_priority)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # 统一导出结构（兼容前端旧值）
    level_norm = "country"

    if not file.filename:
        raise HTTPException(status_code=400, detail="file name missing")

    job_id = uuid.uuid4().hex[:16]
    up_dir, _out_dir, _lg_dir = _job_dirs(job_id)

    suffix = Path(file.filename).suffix.lower()
    if suffix not in (".txt", ".csv", ".xlsx", ".xls"):
        raise HTTPException(status_code=400, detail="only .txt/.csv/.xlsx/.xls supported")

    input_path = up_dir / f"input{suffix}"
    with input_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    state = {
        "job_id": job_id,
        "status": "queued",
        "created_at": _utc_now_iso(),
        "started_at": None,
        "finished_at": None,
        "level": level_norm,              # 实际执行 level
        "level_input": level_input,       # 保留原始请求
        "export_layout": "country",
        "apply_black_markup": bool(apply_black_markup),
        "price_priority": price_priority_norm,
        "input_name": file.filename,
        "input_path": str(input_path),
        "output_files": [],
        "report": None,
        "error": None,
        "progress_total": 0,
        "progress_done": 0,
        "progress_percent": 0.0,
        "progress_current_pn": None,
        "progress_anchor_applied": 0,
        "progress_anchor_changed": 0,
        "progress_black_markup_applied": 0,
        "progress_atc_markup_applied": 0,
        "progress_not_found": 0,
    }
    _write_state(job_id, state)

    worker = threading.Thread(
        target=_run_batch_job,
        args=(job_id,),
        daemon=True,
        name=f"batch-job-{job_id}",
    )
    worker.start()
    return {
        "job_id": job_id,
        "status": "queued",
        "export_layout": "country",
        "apply_black_markup": bool(apply_black_markup),
        "price_priority": price_priority_norm,
    }


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> Dict[str, Any]:
    return _read_state(job_id)


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str) -> FileResponse:
    st = _read_state(job_id)
    if st.get("status") != "done":
        raise HTTPException(status_code=409, detail=f"job not done, status={st.get('status')}")

    out_dir = OUTPUTS_DIR / job_id
    out_file = out_dir / OUT_COUNTRY
    if not out_file.exists():
        # 兼容旧任务
        legacy = out_dir / OUT_COUNTRY_CUSTOMER
        if legacy.exists():
            out_file = legacy
        else:
            raise HTTPException(status_code=404, detail="output file not found")

    return FileResponse(
        path=str(out_file),
        filename=out_file.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# =========================
# Price data release APIs
# =========================

@app.get("/api/admin/price-data")
def price_data_metadata() -> Dict[str, Any]:
    return _require_price_data_store().metadata()


@app.get("/api/admin/price-data/versions")
def price_data_versions() -> Dict[str, Any]:
    return _require_price_data_store().list_versions()


@app.post(
    "/api/admin/price-data/stage",
    dependencies=[Depends(_require_price_data_admin_token)],
)
def price_data_upload_candidate(
    france_file: UploadFile = File(...),
    sys_file: UploadFile = File(...),
    actor: Optional[str] = Header(default=None, alias="X-Price-Data-Actor"),
) -> Dict[str, Any]:
    for label, upload in (("FrancePrice.xlsx", france_file), ("SysPrice.xlsx", sys_file)):
        if not upload.filename or Path(upload.filename).suffix.lower() != ".xlsx":
            raise HTTPException(status_code=400, detail=f"{label} must be an .xlsx file")

    upload_dir = _require_price_data_store().root / "upload-tmp" / uuid.uuid4().hex
    france_path = upload_dir / "FrancePrice.xlsx"
    sys_path = upload_dir / "SysPrice.xlsx"
    try:
        upload_dir.mkdir(parents=True, exist_ok=False)
        france_size = _copy_upload_limited(france_file, france_path)
        sys_size = _copy_upload_limited(sys_file, sys_path)
        if france_size == 0 or sys_size == 0:
            raise HTTPException(status_code=400, detail="price data files must not be empty")
        assert _engine is not None
        current, _version = _engine.snapshot()
        candidate = _require_price_data_store().create_candidate(
            france_path,
            sys_path,
            actor=actor or "api-admin",
            current=current,
        )
        return {"ok": True, "candidate": candidate}
    except HTTPException:
        raise
    except Exception as exc:
        raise _price_data_http_error(exc) from exc
    finally:
        shutil.rmtree(upload_dir, ignore_errors=True)


@app.post(
    "/api/admin/price-data/{version_id}/publish",
    dependencies=[Depends(_require_price_data_admin_token)],
)
def price_data_publish_candidate(
    version_id: str,
    actor: Optional[str] = Header(default=None, alias="X-Price-Data-Actor"),
) -> Dict[str, Any]:
    try:
        bundle, manifest = _require_price_data_store().publish(version_id, actor=actor or "api-admin")
        assert _engine is not None
        _engine.install_data(bundle, version=version_id)
        return {"ok": True, "active_version_id": version_id, "version": manifest}
    except Exception as exc:
        raise _price_data_http_error(exc) from exc


@app.post(
    "/api/admin/price-data/{version_id}/rollback",
    dependencies=[Depends(_require_price_data_admin_token)],
)
def price_data_rollback(
    version_id: str,
    actor: Optional[str] = Header(default=None, alias="X-Price-Data-Actor"),
) -> Dict[str, Any]:
    try:
        bundle, manifest = _require_price_data_store().rollback(version_id, actor=actor or "api-admin")
        assert _engine is not None
        _engine.install_data(bundle, version=version_id)
        return {"ok": True, "active_version_id": version_id, "version": manifest}
    except Exception as exc:
        raise _price_data_http_error(exc) from exc


# =========================
# Admin APIs
# =========================

@app.get("/api/admin/uplift")
def admin_get_uplift() -> Dict[str, float]:
    return _sorted_uplift_dict()


@app.get("/api/admin/keyword-uplift")
def admin_get_keyword_uplift() -> list[Dict[str, Any]]:
    return _sorted_keyword_uplift_rows()


@app.put("/api/admin/keyword-uplift", dependencies=[Depends(_require_price_data_admin_token)])
def admin_put_keyword_uplift(payload: Any = Body(...)) -> Dict[str, Any]:
    data = _normalize_keyword_uplift_payload(payload)
    pricing_engine_mod.KEYWORD_UPLIFT_RULES.clear()
    pricing_engine_mod.KEYWORD_UPLIFT_RULES.extend(data)
    _write_json_file(KEYWORD_UPLIFT_CFG, _sorted_keyword_uplift_rows())
    return {"ok": True, "count": len(pricing_engine_mod.KEYWORD_UPLIFT_RULES)}


@app.post("/api/admin/keyword-uplift/preview", dependencies=[Depends(_require_price_data_admin_token)])
def admin_preview_keyword_uplift(req: KeywordUpliftPreviewReq) -> Dict[str, Any]:
    keyword = (req.keyword or "").strip()
    pct = _normalize_number(req.pct, "pct")
    return _keyword_preview_all_sources(keyword, pct, bool(req.enabled))


@app.put("/api/admin/uplift", dependencies=[Depends(_require_price_data_admin_token)])
def admin_put_uplift(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = _normalize_uplift_payload(payload)
    pricing_engine_mod.UPLIFT_PCT_BY_LINE.clear()
    pricing_engine_mod.UPLIFT_PCT_BY_LINE.update(data)
    _write_json_file(UPLIFT_CFG, _sorted_uplift_dict())
    return {"ok": True, "count": len(pricing_engine_mod.UPLIFT_PCT_BY_LINE)}


@app.get("/api/admin/ddp-rules")
def admin_get_ddp_rules() -> Dict[str, list[float]]:
    return _sorted_ddp_rules_dict()


@app.put("/api/admin/ddp-rules", dependencies=[Depends(_require_price_data_admin_token)])
def admin_put_ddp_rules(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = _normalize_ddp_rules_payload(payload)
    pricing_rules_mod.DDP_RULES.clear()
    pricing_rules_mod.DDP_RULES.update(data)
    _write_json_file(DDP_RULES_CFG, _sorted_ddp_rules_dict())
    return {"ok": True, "count": len(pricing_rules_mod.DDP_RULES)}


@app.get("/api/admin/pricing-rules")
def admin_get_price_rules() -> Dict[str, Any]:
    return _sorted_price_rules_dict()


@app.put("/api/admin/pricing-rules", dependencies=[Depends(_require_price_data_admin_token)])
def admin_put_price_rules(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = _normalize_price_rules_payload(payload)
    pricing_rules_mod.PRICE_RULES.clear()
    pricing_rules_mod.PRICE_RULES.update(copy.deepcopy(data))
    _write_json_file(PRICE_RULES_CFG, _sorted_price_rules_dict())
    return {"ok": True, "count_groups": len(pricing_rules_mod.PRICE_RULES)}


@app.post("/api/admin/reload-rules", dependencies=[Depends(_require_price_data_admin_token)])
def admin_reload_rules() -> Dict[str, Any]:
    _apply_rule_overrides_if_exist()
    return {
        "ok": True,
        "uplift_count": len(pricing_engine_mod.UPLIFT_PCT_BY_LINE),
        "keyword_uplift_count": len(pricing_engine_mod.KEYWORD_UPLIFT_RULES),
        "ddp_count": len(pricing_rules_mod.DDP_RULES),
        "price_group_count": len(pricing_rules_mod.PRICE_RULES),
        "uplift_keys": sorted([str(k) for k in pricing_engine_mod.UPLIFT_PCT_BY_LINE.keys()]),
    }
