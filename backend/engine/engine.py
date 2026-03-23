# backend/engine/engine.py
from __future__ import annotations

import time
from datetime import datetime, timezone
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

        def _mtime_epoch(path: Optional[Path]) -> Optional[float]:
            if path is None:
                return None
            try:
                return float(path.stat().st_mtime)
            except Exception:
                return None

        def _epoch_to_iso(epoch: Optional[float]) -> Optional[str]:
            if epoch is None:
                return None
            try:
                return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()
            except Exception:
                return None

        country_epoch = _mtime_epoch(self.data.france_price_path)
        sys_epoch = _mtime_epoch(self.data.sys_price_path)
        return {
            "loaded": True,
            "loaded_at_epoch": self._loaded_at,
            "data_dir": str(self.cfg.data_dir),
            "france_price_file": str(self.data.france_price_path) if self.data.france_price_path else None,
            "sys_price_file": str(self.data.sys_price_path) if self.data.sys_price_path else None,
            "country_data_updated_at_epoch": country_epoch,
            "country_data_updated_at_iso": _epoch_to_iso(country_epoch),
            "sys_data_updated_at_epoch": sys_epoch,
            "sys_data_updated_at_iso": _epoch_to_iso(sys_epoch),
            "map_fr_file": str(self.data.map_fr_path) if self.data.map_fr_path else None,
            "map_sys_file": str(self.data.map_sys_path) if self.data.map_sys_path else None,
            "rows_france": int(self.data.france_df.shape[0]),
            "rows_sys": int(self.data.sys_df.shape[0]),
        }

    def query_one(
        self,
        pn: str,
        force_category: Optional[str] = None,
        force_price_group: Optional[str] = None,
        force_series_key: Optional[str] = None,
        force_full_recalc: bool = False,
    ) -> Dict[str, Any]:
        if self.data is None:
            raise RuntimeError("engine not loaded")
        return compute_one(
            self.data,
            pn,
            force_category=force_category,
            force_price_group=force_price_group,
            force_series_key=force_series_key,
            force_full_recalc=force_full_recalc,
        )

    def run_batch(self, input_path: Path, level: str, out_dir: Path) -> Dict[str, Any]:
        """
        input_path: 上传文件路径（txt/csv/xlsx/xls）
        level: 保留参数仅兼容旧调用；导出结构统一按 country
        out_dir: /runtime/outputs/{job_id}
        产出文件名统一：Country_import_upload_Model.xlsx
        """
        if self.data is None:
            raise RuntimeError("engine not loaded")

        # 计算逻辑本身不依赖导出层级；这里统一导出 country 模板
        level_norm = "country"

        pns = parse_pn_list_file(input_path)
        results = compute_many(self.data, pns, level=level_norm)

        # 生成导出 DF
        frames = build_export_frames(results)

        out_dir.mkdir(parents=True, exist_ok=True)
        write_export_xlsx(frames, out_dir=out_dir, level=level_norm)

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
