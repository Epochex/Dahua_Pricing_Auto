# backend/engine/core/formatter.py
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

PRICE_DECIMAL_THRESHOLD = 10


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return None
            v = s
        f = float(v)
        if pd.isna(f):
            return None
        return f
    except Exception:
        return None


def _format_price_piecewise(v) -> Optional[float | int]:
    """
    本地规则：
      - < 10  : 保留 2 位小数
      - >= 10 : 四舍五入取整
    """
    f = _to_float(v)
    if f is None:
        return None
    if f < PRICE_DECIMAL_THRESHOLD:
        return round(f, 2)
    return int(round(f))


def _pick_final_prices(result_row: Dict[str, Any]) -> Dict[str, Optional[float]]:
    fv = (result_row or {}).get("final_values") or {}
    return {
        "FOB": _to_float(fv.get("FOB C(EUR)")),
        "DDP": _to_float(fv.get("DDP A(EUR)")),
        "RESELLER": _to_float(fv.get("Suggested Reseller(EUR)")),
        "GOLD": _to_float(fv.get("Gold(EUR)")),
        "SILVER": _to_float(fv.get("Silver(EUR)")),
        "IVORY": _to_float(fv.get("Ivory(EUR)")),
        "MSRP": _to_float(fv.get("MSRP(EUR)")),
    }


def build_export_frames(results: List[Dict[str, Any]]) -> Dict[str, pd.DataFrame]:
    rows: List[Dict[str, Any]] = []

    for r in results:
        pn = (r or {}).get("pn")
        status = (r or {}).get("status")

        if status != "ok":
            rows.append(
                {
                    "Part No.": pn,
                    "FOB": None,
                    "DDP": None,
                    "RESELLER": None,
                    "GOLD": None,
                    "SILVER": None,
                    "IVORY": None,
                    "MSRP": None,
                }
            )
            continue

        prices = _pick_final_prices(r)
        rows.append(
            {
                "Part No.": pn,
                "FOB": prices["FOB"],
                "DDP": prices["DDP"],
                "RESELLER": prices["RESELLER"],
                "GOLD": prices["GOLD"],
                "SILVER": prices["SILVER"],
                "IVORY": prices["IVORY"],
                "MSRP": prices["MSRP"],
            }
        )

    df = pd.DataFrame(rows)
    return {"export": df}


def write_export_xlsx(frames: Dict[str, pd.DataFrame], out_dir: Path, level: str) -> Path:
    """
    统一导出 country 结构（无论前端传 country / country_customer）。
    文件名统一保持：
      - Country_import_upload_Model.xlsx

    导出时应用本地“分段取整”规则：
      - <10 保留2位小数
      - >=10 取整
    """
    level_norm = (level or "").strip().lower()
    if level_norm not in ("country", "country_customer"):
        raise ValueError("level must be country or country_customer")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    df = frames.get("export")
    if df is None or df.empty:
        df = pd.DataFrame(
            columns=["Part No.", "FOB", "DDP", "RESELLER", "GOLD", "SILVER", "IVORY", "MSRP"]
        )

    f_fob = df["FOB"].apply(_format_price_piecewise)
    f_ddp = df["DDP"].apply(_format_price_piecewise)
    f_res = df["RESELLER"].apply(_format_price_piecewise)
    f_gold = df["GOLD"].apply(_format_price_piecewise)
    f_silver = df["SILVER"].apply(_format_price_piecewise)
    f_ivory = df["IVORY"].apply(_format_price_piecewise)
    f_msrp = df["MSRP"].apply(_format_price_piecewise)

    out_name = "Country_import_upload_Model.xlsx"
    df_out = pd.DataFrame(
        {
            "Part No.": df["Part No."],
            "FOB C": f_fob,
            "DDP A": f_ddp,
            "Reseller S": f_res,
            "SI-S": f_gold,    # Gold
            "SI-A": f_silver,  # Silver
            "MSTP": f_ivory,   # Ivory
            "MSRP": f_msrp,
        }
    )

    out_path = out_dir / out_name
    df_out.to_excel(out_path, index=False)
    return out_path
