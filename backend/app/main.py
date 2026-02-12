# backend/app/main.py
from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from backend.engine.engine import PricingEngine, EngineConfig


APP_ROOT = Path(__file__).resolve().parents[2]  # .../backend
REPO_ROOT = APP_ROOT.parent  # repo root
RUNTIME_DIR = REPO_ROOT / "runtime"
UPLOADS_DIR = RUNTIME_DIR / "uploads"
OUTPUTS_DIR = RUNTIME_DIR / "outputs"
LOGS_DIR = RUNTIME_DIR / "logs"
DATA_DIR = RUNTIME_DIR / "data"  # 真实数据放这里（FrancePrice/SysPrice/mapping）

# 产物文件名保持与你原先一致
OUT_COUNTRY = "Country_import_upload_Model.xlsx"
OUT_COUNTRY_CUSTOMER = "Country&Customer_import_upload_Model.xlsx"

STATE_NAME = "state.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_dirs() -> None:
    for d in (UPLOADS_DIR, OUTPUTS_DIR, LOGS_DIR, DATA_DIR):
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


class QueryReq(BaseModel):
    pn: str = Field(..., description="Part No.")


app = FastAPI(title="Dahua Pricing Auto (Deploy Server)", version="0.1.0")

_engine: Optional[PricingEngine] = None


@app.on_event("startup")
def _startup() -> None:
    _ensure_dirs()
    global _engine
    # 配置：真实数据与 mapping 都规划到 runtime/data 下
    # 约定：
    #   runtime/data/FrancePrice.xlsx
    #   runtime/data/SysPrice.xls
    #   runtime/data/productline_map_france_full.csv
    #   runtime/data/productline_map_sys_full.csv
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


@app.post("/api/batch")
def batch(
    level: str = Form(..., description="country | country_customer"),
    file: UploadFile = File(...),
) -> Dict[str, Any]:
    """
    批量：上传 PN 列表（txt / xlsx / csv），生成导出 xlsx。
    level:
      - country            -> Country_import_upload_Model.xlsx
      - country_customer   -> Country&Customer_import_upload_Model.xlsx
    """
    assert _engine is not None
    level_norm = (level or "").strip().lower()
    if level_norm not in ("country", "country_customer"):
        raise HTTPException(status_code=400, detail="level must be country or country_customer")

    if not file.filename:
        raise HTTPException(status_code=400, detail="file name missing")

    job_id = uuid.uuid4().hex[:16]
    up_dir, out_dir, lg_dir = _job_dirs(job_id)

    # 保存上传文件
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
        "level": level_norm,
        "input_name": file.filename,
        "input_path": str(input_path),
        "output_files": [],
        "report": None,
        "error": None,
    }
    _write_state(job_id, state)

    # 先做同步闭环：直接跑批处理
    try:
        state["status"] = "running"
        state["started_at"] = _utc_now_iso()
        _write_state(job_id, state)

        report = _engine.run_batch(
            input_path=input_path,
            level=level_norm,
            out_dir=out_dir,
        )

        out_name = OUT_COUNTRY if level_norm == "country" else OUT_COUNTRY_CUSTOMER
        out_file = out_dir / out_name
        if not out_file.exists():
            raise RuntimeError(f"output file missing: {out_file}")

        state["status"] = "done"
        state["finished_at"] = _utc_now_iso()
        state["output_files"] = [str(out_file)]
        state["report"] = report
        _write_state(job_id, state)

        return {"job_id": job_id, "status": "done"}
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
    level = st.get("level")
    out_name = OUT_COUNTRY if level == "country" else OUT_COUNTRY_CUSTOMER

    out_dir = OUTPUTS_DIR / job_id
    out_file = out_dir / out_name
    if not out_file.exists():
        raise HTTPException(status_code=404, detail="output file not found")

    return FileResponse(
        path=str(out_file),
        filename=out_name,  # 浏览器下载名保持原样
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
