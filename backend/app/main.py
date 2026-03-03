# backend/app/main.py
from __future__ import annotations

import copy
import json
import math
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from collections import Counter, defaultdict

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.engine.engine import EngineConfig, PricingEngine
from backend.engine.core import pricing_engine as pricing_engine_mod
from backend.engine.core import pricing_rules as pricing_rules_mod
from backend.engine.core.formatter import build_export_frames, write_export_xlsx


APP_ROOT = Path(__file__).resolve().parents[2]  # .../backend
REPO_ROOT = APP_ROOT.parent  # repo root
RUNTIME_DIR = Path("/data/runtime")
UPLOADS_DIR = RUNTIME_DIR / "uploads"
OUTPUTS_DIR = RUNTIME_DIR / "outputs"
LOGS_DIR = RUNTIME_DIR / "logs"
DATA_DIR = RUNTIME_DIR / "data"
ADMIN_DIR = RUNTIME_DIR / "admin"

OUT_COUNTRY = "Country_import_upload_Model.xlsx"
OUT_COUNTRY_CUSTOMER = "Country&Customer_import_upload_Model.xlsx"  # backward-compat symbol only

STATE_NAME = "state.json"
UPLIFT_CFG = ADMIN_DIR / "uplift.json"
DDP_RULES_CFG = ADMIN_DIR / "ddp_rules.json"
PRICE_RULES_CFG = ADMIN_DIR / "price_rules.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dirs() -> None:
    for d in (UPLOADS_DIR, OUTPUTS_DIR, LOGS_DIR, DATA_DIR, ADMIN_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _job_dirs(job_id: str) -> Tuple[Path, Path, Path]:
    up = UPLOADS_DIR / job_id
    out = OUTPUTS_DIR / job_id
    lg = LOGS_DIR / job_id
    up.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    lg.mkdir(parents=True, exist_ok=True)
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


def _normalize_ddp_rules_payload(payload: Any) -> Dict[str, tuple[float, float, float, float]]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be object: {category: [p1,p2,p3,p4]}")
    out: Dict[str, tuple[float, float, float, float]] = {}
    for k, v in payload.items():
        kk = str(k).strip()
        if not kk:
            continue
        if not isinstance(v, (list, tuple)) or len(v) != 4:
            raise HTTPException(status_code=400, detail=f"ddp_rules[{kk}] must be array length=4")
        vals = tuple(_normalize_number(x, f"ddp_rules[{kk}]") for x in v)
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


def _sorted_ddp_rules_dict() -> Dict[str, list[float]]:
    return {
        k: [float(x) for x in v]
        for k, v in sorted(pricing_rules_mod.DDP_RULES.items(), key=lambda kv: kv[0])
    }


def _sorted_price_rules_dict() -> Dict[str, Any]:
    return dict(sorted(_deep_jsonable(pricing_rules_mod.PRICE_RULES).items(), key=lambda kv: kv[0]))


def _apply_rule_overrides_if_exist() -> None:
    if UPLIFT_CFG.exists():
        data = _normalize_uplift_payload(_read_json_file(UPLIFT_CFG))
        pricing_engine_mod.UPLIFT_PCT_BY_LINE.clear()
        pricing_engine_mod.UPLIFT_PCT_BY_LINE.update(data)

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


class QueryRecomputeReq(BaseModel):
    pn: str = Field(..., description="Part No.")
    force_category: Optional[str] = Field(default=None, description="override DDP category")
    force_price_group: Optional[str] = Field(default=None, description="override PRICE_RULES group")
    force_series_key: Optional[str] = Field(default=None, description="override PRICE_RULES subgroup key")


class QueryExportReq(BaseModel):
    pn: str = Field(..., description="Part No.")
    force_category: Optional[str] = Field(default=None, description="override DDP category")
    force_price_group: Optional[str] = Field(default=None, description="override PRICE_RULES group")
    force_series_key: Optional[str] = Field(default=None, description="override PRICE_RULES subgroup key")
    force_full_recalc: bool = Field(default=False, description="force full price recalculation")


def _norm_optional_text(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
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


app = FastAPI(title="Dahua Pricing Auto (Deploy Server)", version="0.2.0")

_engine: Optional[PricingEngine] = None


@app.on_event("startup")
def _startup() -> None:
    _ensure_dirs()
    _apply_rule_overrides_if_exist()
    global _engine
    cfg = EngineConfig(runtime_dir=RUNTIME_DIR)
    _engine = PricingEngine(cfg)
    _engine.load()


@app.get("/api/meta")
def meta() -> Dict[str, Any]:
    assert _engine is not None
    return _engine.meta()


@app.post("/api/query")
def query_one(req: QueryReq) -> Dict[str, Any]:
    assert _engine is not None
    pn = (req.pn or "").strip()
    if not pn:
        raise HTTPException(status_code=400, detail="pn is empty")
    return _engine.query_one(pn)


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
    _validate_query_overrides(
        force_category,
        force_price_group,
        force_series_key,
        require_any=True,
    )

    return _engine.query_one(
        pn,
        force_category=force_category,
        force_price_group=force_price_group,
        force_series_key=force_series_key,
        force_full_recalc=True,
    )


@app.post("/api/query/export")
def query_export(req: QueryExportReq) -> FileResponse:
    assert _engine is not None
    pn = (req.pn or "").strip()
    if not pn:
        raise HTTPException(status_code=400, detail="pn is empty")

    force_category = _norm_optional_text(req.force_category)
    force_price_group = _norm_optional_text(req.force_price_group)
    force_series_key = _norm_optional_text(req.force_series_key)
    _validate_query_overrides(
        force_category,
        force_price_group,
        force_series_key,
        require_any=False,
    )

    result = _engine.query_one(
        pn,
        force_category=force_category,
        force_price_group=force_price_group,
        force_series_key=force_series_key,
        force_full_recalc=bool(req.force_full_recalc),
    )
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


@app.post("/api/batch")
def batch(
    level: str = Form(..., description="country | country_customer"),
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

    # 统一导出结构（兼容前端旧值）
    level_norm = "country"

    if not file.filename:
        raise HTTPException(status_code=400, detail="file name missing")

    job_id = uuid.uuid4().hex[:16]
    up_dir, out_dir, _lg_dir = _job_dirs(job_id)

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
        "input_name": file.filename,
        "input_path": str(input_path),
        "output_files": [],
        "report": None,
        "error": None,
    }
    _write_state(job_id, state)

    try:
        state["status"] = "running"
        state["started_at"] = _utc_now_iso()
        _write_state(job_id, state)

        report = _engine.run_batch(input_path=input_path, level=level_norm, out_dir=out_dir)

        out_name = OUT_COUNTRY
        out_file = out_dir / out_name
        if not out_file.exists():
            raise RuntimeError(f"output file missing: {out_file}")

        state["status"] = "done"
        state["finished_at"] = _utc_now_iso()
        state["output_files"] = [str(out_file)]
        state["report"] = report
        _write_state(job_id, state)

        return {"job_id": job_id, "status": "done", "export_layout": "country"}
    except Exception as e:
        state["status"] = "failed"
        state["finished_at"] = _utc_now_iso()
        state["error"] = f"{type(e).__name__}: {e}"
        _write_state(job_id, state)
        raise HTTPException(status_code=500, detail=state["error"])


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
# Admin APIs
# =========================

@app.get("/api/admin/uplift")
def admin_get_uplift() -> Dict[str, float]:
    return _sorted_uplift_dict()


@app.put("/api/admin/uplift")
def admin_put_uplift(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = _normalize_uplift_payload(payload)
    pricing_engine_mod.UPLIFT_PCT_BY_LINE.clear()
    pricing_engine_mod.UPLIFT_PCT_BY_LINE.update(data)
    _write_json_file(UPLIFT_CFG, _sorted_uplift_dict())
    return {"ok": True, "count": len(pricing_engine_mod.UPLIFT_PCT_BY_LINE)}


@app.get("/api/admin/ddp-rules")
def admin_get_ddp_rules() -> Dict[str, list[float]]:
    return _sorted_ddp_rules_dict()


@app.put("/api/admin/ddp-rules")
def admin_put_ddp_rules(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = _normalize_ddp_rules_payload(payload)
    pricing_rules_mod.DDP_RULES.clear()
    pricing_rules_mod.DDP_RULES.update(data)
    _write_json_file(DDP_RULES_CFG, _sorted_ddp_rules_dict())
    return {"ok": True, "count": len(pricing_rules_mod.DDP_RULES)}


@app.get("/api/admin/pricing-rules")
def admin_get_price_rules() -> Dict[str, Any]:
    return _sorted_price_rules_dict()


@app.put("/api/admin/pricing-rules")
def admin_put_price_rules(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = _normalize_price_rules_payload(payload)
    pricing_rules_mod.PRICE_RULES.clear()
    pricing_rules_mod.PRICE_RULES.update(copy.deepcopy(data))
    _write_json_file(PRICE_RULES_CFG, _sorted_price_rules_dict())
    return {"ok": True, "count_groups": len(pricing_rules_mod.PRICE_RULES)}
