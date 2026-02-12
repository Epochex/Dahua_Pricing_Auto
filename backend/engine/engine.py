# backend/engine/engine.py
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from backend.engine.core.loader import (
    DataBundle,
    load_all_data,
    parse_pn_list_file,
)
from backend.engine.core.pricing_engine import (
    compute_one,
    compute_many,
)
from backend.engine.core.formatter import (
    build_export_frames,
    write_export_xlsx,
)


@dataclass(frozen=True)
class EngineConfig:
    runtime_dir: Path

    @property
    def data_dir(self) -> Path:
        return self.runtime_dir / "data"

    @property
    def outputs_dir(self) -> Path:
        return self.runtime_dir / "outputs"

    @property
    def uploads_dir(self) -> Path:
        return self.runtime_dir / "uploads"

    @property
    def logs_dir(self) -> Path:
        return self.runtime_dir / "logs"


class PricingEngine:
    """
    薄 class：持有 DataBundle（大表 + 索引 + 映射），服务启动时 load 一次。
    """

    def __init__(self, cfg: EngineConfig):
        self.cfg = cfg
        self.data: Optional[DataBundle] = None
        self._loaded_at: Optional[float] = None

    def load(self) -> None:
        self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        self.data = load_all_data(self.cfg.data_dir)
        self._loaded_at = time.time()
        _ = t0  # keep
        # 不做 print；API 层需要 meta() 获取信息

    def meta(self) -> Dict[str, Any]:
        if self.data is None:
            return {"loaded": False}
        return {
            "loaded": True,
            "loaded_at_epoch": self._loaded_at,
            "data_dir": str(self.cfg.data_dir),
            "france_price_file": str(self.data.france_price_path) if self.data.france_price_path else None,
            "sys_price_file": str(self.data.sys_price_path) if self.data.sys_price_path else None,
            "map_fr_file": str(self.data.map_fr_path) if self.data.map_fr_path else None,
            "map_sys_file": str(self.data.map_sys_path) if self.data.map_sys_path else None,
            "rows_france": int(self.data.france_df.shape[0]),
            "rows_sys": int(self.data.sys_df.shape[0]),
        }

    def query_one(self, pn: str) -> Dict[str, Any]:
        if self.data is None:
            raise RuntimeError("engine not loaded")
        return compute_one(self.data, pn)

    def run_batch(self, input_path: Path, level: str, out_dir: Path) -> Dict[str, Any]:
        """
        input_path: 上传文件路径（txt/csv/xlsx/xls）
        level: country | country_customer
        out_dir: /runtime/outputs/{job_id}
        产出文件名保持原样（在 out_dir 内）
        """
        if self.data is None:
            raise RuntimeError("engine not loaded")
        level = (level or "").strip().lower()
        if level not in ("country", "country_customer"):
            raise ValueError("level must be country or country_customer")

        pns = parse_pn_list_file(input_path)
        results = compute_many(self.data, pns, level=level)

        # 生成导出 DF（两个层级都可生成；但这里只写一个）
        frames = build_export_frames(results)

        out_dir.mkdir(parents=True, exist_ok=True)
        write_export_xlsx(frames, out_dir=out_dir, level=level)

        # report：not_found、warnings、统计
        not_found = [r["pn"] for r in results if r.get("status") == "not_found"]
        warnings = []
        for r in results:
            ws = r.get("warnings") or []
            warnings.extend([{"pn": r.get("pn"), "w": w} for w in ws])

        return {
            "count_total": len(results),
            "count_not_found": len(not_found),
            "not_found": not_found,
            "warnings": warnings,
        }
