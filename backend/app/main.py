# backend/app/main.py
from __future__ import annotations

import copy
import json
import math
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from collections import Counter, defaultdict

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.engine.engine import EngineConfig, PricingEngine
from backend.engine.core.classifier import apply_mapping
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
_GROUP_KEY_ALIASES: Dict[str, str] = {
    "AC": "ACCESS CONTROL",
    "ACCESSCONTROL": "ACCESS CONTROL",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dirs() -> None:
    for d in (UPLOADS_DIR, OUTPUTS_DIR, LOGS_DIR, DATA_DIR, ADMIN_DIR, MAPPING_DIR):
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


def _normalize_keyword_uplift_payload(payload: Any) -> list[Dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("rules")
    if payload is None:
        payload = []
    if not isinstance(payload, list):
        raise HTTPException(
            status_code=400,
            detail="payload must be array: [{price_group, keyword, pct, enabled}]",
        )

    rows: list[Dict[str, Any]] = []
    for i, it in enumerate(payload):
        if not isinstance(it, dict):
            raise HTTPException(status_code=400, detail=f"keyword_uplift[{i}] must be object")
        pg = _normalize_group_key(it.get("price_group"))
        kw = str(it.get("keyword") or "").strip()
        if not pg or not kw:
            continue
        pct = _normalize_number(it.get("pct"), f"keyword_uplift[{i}].pct")
        enabled = bool(it.get("enabled", True))
        rows.append(
            {
                "price_group": pg,
                "keyword": kw,
                "pct": pct,
                "enabled": enabled,
            }
        )

    dedup: Dict[tuple[str, str], Dict[str, Any]] = {}
    for r in rows:
        k = (_normalize_group_key(r.get("price_group")), str(r.get("keyword") or "").upper())
        dedup[k] = r
    out = list(dedup.values())
    out.sort(key=lambda r: (str(r.get("price_group") or ""), str(r.get("keyword") or "")))
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


def _sorted_keyword_uplift_rows() -> list[Dict[str, Any]]:
    rows = [
        {
            "price_group": str(r.get("price_group") or "").strip(),
            "keyword": str(r.get("keyword") or "").strip(),
            "pct": float(r.get("pct") or 0.0),
            "enabled": bool(r.get("enabled", True)),
        }
        for r in (pricing_engine_mod.KEYWORD_UPLIFT_RULES or [])
        if isinstance(r, dict)
    ]
    rows = [r for r in rows if r["price_group"] and r["keyword"]]
    rows.sort(key=lambda r: (r["price_group"], r["keyword"]))
    return rows


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


class ExternalModelReq(BaseModel):
    pn: str = Field(..., description="Part No.")
    apply_france_anchor: bool = Field(
        default=True,
        description="if france has complete priced row under same external model, override cluster prices",
    )


class KeywordUpliftPreviewReq(BaseModel):
    price_group: Optional[str] = Field(default=None, description="optional filter by price_group/category hint")
    keyword: str = Field(..., description="contains keyword in model/series/product name")
    limit: int = Field(default=30, ge=1, le=300)


def _norm_optional_text(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _normalize_group_key(v: Optional[str]) -> str:
    s = str(v or "").strip().upper()
    if not s:
        return ""
    compact = s.replace(" ", "")
    return _GROUP_KEY_ALIASES.get(compact, s)


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


def _pick_france_anchor_prices(external_model: str) -> tuple[Optional[str], Optional[Dict[str, float]]]:
    assert _engine is not None and _engine.data is not None
    df = _engine.data.france_df
    pn_col = _pick_col(
        df,
        ("Part No.", "Part No", "Part Num", "PN", "P/N", "Part Number", "PartNumber"),
    )
    ext_col = _pick_col(df, ("External Model", "ExternalModel"))
    if not pn_col or not ext_col:
        return None, None

    ext_up = str(external_model).strip().upper()
    price_cols = list(pricing_engine_mod.PRICE_COLS)
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
            return _norm_optional_text(row.get(pn_col)), prices
    return None, None


def _apply_external_model_anchor_to_row(
    row: Dict[str, Any],
    *,
    apply_france_anchor: bool,
    anchor_cache: Optional[Dict[str, tuple[Optional[str], Optional[Dict[str, float]]]]] = None,
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
    if not apply_france_anchor or not ext_model:
        meta["external_model_anchor_applied"] = False
        meta["external_model_anchor_pn"] = None
        meta["external_model_anchor"] = ext_model
        meta["external_model_anchor_changed"] = False
        return False

    key = ext_model.upper()
    anchor: tuple[Optional[str], Optional[Dict[str, float]]]
    if anchor_cache is not None and key in anchor_cache:
        anchor = anchor_cache[key]
    else:
        anchor = _pick_france_anchor_prices(ext_model)
        if anchor_cache is not None:
            anchor_cache[key] = anchor
    anchor_pn, anchor_prices = anchor

    if not anchor_prices:
        meta["external_model_anchor_applied"] = False
        meta["external_model_anchor_pn"] = None
        meta["external_model_anchor"] = ext_model
        meta["external_model_anchor_changed"] = False
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

    anchor_cache: Dict[str, tuple[Optional[str], Optional[Dict[str, float]]]] = {}
    rows: list[Dict[str, Any]] = []
    changed_pns: list[str] = []
    anchor_pn: Optional[str] = None
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


def _sys_keyword_preview(price_group: Optional[str], keyword: str, limit: int) -> Dict[str, Any]:
    assert _engine is not None and _engine.data is not None
    data = _engine.data
    df = data.sys_df
    map_df = data.map_sys

    pg_filter = _normalize_group_key(price_group)
    kw = str(keyword or "").strip().upper()
    if not kw:
        raise HTTPException(status_code=400, detail="keyword is empty")

    pn_col = _pick_col(df, ["Part Num", "Part No.", "Part No", "PN", "P/N", "Part Number"])
    fields = [
        "Internal Model",
        "External Model",
        "Second Product Line",
        "Catelog Name",
        "Product Name",
        "Product Name(CN)",
    ]

    total = 0
    rows = []
    for _, r in df.iterrows():
        cat, pg = apply_mapping(r, map_df)
        if pg_filter:
            cands = {_normalize_group_key(cat), _normalize_group_key(pg)}
            if pg_filter not in cands:
                continue

        txt_parts = []
        for f in fields:
            if f in r and str(r.get(f) or "").strip():
                txt_parts.append(str(r.get(f)))
        txt = " ".join(txt_parts).upper()
        if kw not in txt:
            continue

        total += 1
        if len(rows) < limit:
            rows.append(
                {
                    "pn": str(r.get(pn_col) or "").strip(),
                    "internal_model": str(r.get("Internal Model") or "").strip(),
                    "external_model": str(r.get("External Model") or "").strip(),
                    "first_line": str(r.get("First Product Line") or "").strip(),
                    "second_line": str(r.get("Second Product Line") or "").strip(),
                    "catelog_name": str(r.get("Catelog Name") or "").strip(),
                    "category": str(cat or "").strip(),
                    "price_group_hint": str(pg or "").strip(),
                }
            )

    return {
        "ok": True,
        "price_group": price_group,
        "keyword": keyword,
        "total": total,
        "shown": len(rows),
        "rows": rows,
    }


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


@app.post("/api/query/external-model-index")
def query_external_model_index(req: ExternalModelReq) -> Dict[str, Any]:
    assert _engine is not None
    pn = (req.pn or "").strip()
    if not pn:
        raise HTTPException(status_code=400, detail="pn is empty")
    return _query_cluster_by_external_model(pn, apply_france_anchor=bool(req.apply_france_anchor))


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
    anchor_applied = bool(meta.get("external_model_anchor_applied"))
    anchor_pn = _norm_optional_text(meta.get("external_model_anchor_pn"))
    price_source = "FR_ANCHOR" if anchor_applied else "NORMAL"
    if anchor_applied and anchor_pn:
        price_source = f"FR_ANCHOR({anchor_pn})"
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
        "fr_match_mode": meta.get("fr_match_mode"),
        "fr_matched_pn": meta.get("fr_matched_pn"),
        "sys_match_mode": meta.get("sys_match_mode"),
        "sys_matched_pn": meta.get("sys_matched_pn"),
        "price_source": price_source,
        "anchor_changed": bool(meta.get("external_model_anchor_changed")),
        "used_sys": bool(meta.get("used_sys")),
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
    state = _read_state(job_id)
    input_path = Path(state.get("input_path") or "")
    level_norm = str(state.get("level") or "country").strip().lower() or "country"
    out_dir = OUTPUTS_DIR / job_id

    try:
        state["status"] = "running"
        state["started_at"] = _utc_now_iso()
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
        state["progress_not_found"] = 0
        _write_state(job_id, state)

        results: list[Dict[str, Any]] = []
        items: list[Dict[str, Any]] = []
        not_found: list[str] = []
        warnings: list[Dict[str, Any]] = []
        anchor_applied_count = 0
        anchor_changed_count = 0
        anchor_cache: Dict[str, tuple[Optional[str], Optional[Dict[str, float]]]] = {}

        for i, pn in enumerate(pns, start=1):
            row = _engine.query_one(pn)
            _apply_external_model_anchor_to_row(
                row,
                apply_france_anchor=True,
                anchor_cache=anchor_cache,
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
            for w in (row.get("warnings") or []):
                warnings.append({"pn": row.get("pn"), "w": w})

            state["progress_done"] = i
            state["progress_percent"] = round((i * 100.0 / total), 2) if total > 0 else 100.0
            state["progress_current_pn"] = pn
            state["progress_anchor_applied"] = anchor_applied_count
            state["progress_anchor_changed"] = anchor_changed_count
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
    return {"job_id": job_id, "status": "queued", "export_layout": "country"}


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


@app.get("/api/admin/keyword-uplift")
def admin_get_keyword_uplift() -> list[Dict[str, Any]]:
    return _sorted_keyword_uplift_rows()


@app.put("/api/admin/keyword-uplift")
def admin_put_keyword_uplift(payload: Any = Body(...)) -> Dict[str, Any]:
    data = _normalize_keyword_uplift_payload(payload)
    pricing_engine_mod.KEYWORD_UPLIFT_RULES.clear()
    pricing_engine_mod.KEYWORD_UPLIFT_RULES.extend(data)
    _write_json_file(KEYWORD_UPLIFT_CFG, _sorted_keyword_uplift_rows())
    return {"ok": True, "count": len(pricing_engine_mod.KEYWORD_UPLIFT_RULES)}


@app.post("/api/admin/keyword-uplift/preview")
def admin_preview_keyword_uplift(req: KeywordUpliftPreviewReq) -> Dict[str, Any]:
    price_group = _norm_optional_text(req.price_group)
    keyword = (req.keyword or "").strip()
    return _sys_keyword_preview(price_group, keyword, int(req.limit))


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


@app.post("/api/admin/reload-rules")
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
