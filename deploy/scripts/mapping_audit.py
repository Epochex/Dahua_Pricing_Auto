#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.engine.core.classifier import apply_mapping
from backend.engine.core.pricing_engine import is_strict_price_group_compatible
from backend.engine.core.pricing_rules import DDP_RULES, PRICE_RULES


RUNTIME_DIR = Path("/data/dahua_pricing_runtime")
OUT_DIR = RUNTIME_DIR / "logs" / "mapping_audit"


def _pick_col(df: pd.DataFrame, candidates: Iterable[str]) -> str:
    cols = list(df.columns)
    low = {str(c).strip().lower(): c for c in cols}
    for c in candidates:
        if c in cols:
            return c
    for c in candidates:
        k = c.strip().lower()
        if k in low:
            return low[k]
    return cols[0]


def _read_runtime_data() -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    fr_path = RUNTIME_DIR / "data" / "FrancePrice.xlsx"
    if not fr_path.exists():
        fr_path = RUNTIME_DIR / "data" / "FrancePrice.xls"

    sys_path = RUNTIME_DIR / "data" / "SysPrice.xlsx"
    if not sys_path.exists():
        sys_path = RUNTIME_DIR / "data" / "SysPrice.xls"

    map_fr_path = RUNTIME_DIR / "mapping" / "productline_map_france_full.csv"
    map_sys_path = RUNTIME_DIR / "mapping" / "productline_map_sys_full.csv"

    fr_df = pd.read_excel(fr_path, engine="openpyxl" if fr_path.suffix.lower() != ".xls" else "xlrd")
    sys_df = pd.read_excel(sys_path, engine="openpyxl" if sys_path.suffix.lower() != ".xls" else "xlrd")
    map_fr = pd.read_csv(map_fr_path)
    map_sys = pd.read_csv(map_sys_path)
    return fr_df, sys_df, map_fr, map_sys


def _audit_side(df: pd.DataFrame, mapping: pd.DataFrame, side: str) -> Dict[str, Any]:
    rows_unknown = []
    rows_strict_mismatch = []
    by_category = Counter()
    by_price_group = Counter()
    unknown_firstline = Counter()

    pn_col = _pick_col(df, ["Part No.", "Part Num", "Part No", "PN", "P/N"])
    first_col = "First Level Product Category" if side == "france" else "First Product Line"
    second_col = "Second Level Product Category" if side == "france" else "Second Product Line"

    for _, row in df.iterrows():
        cat, pg = apply_mapping(row, mapping)
        cat = str(cat or "").strip() or "UNKNOWN"
        pg = str(pg or "").strip() or ""

        by_category[cat] += 1
        by_price_group[pg or "<EMPTY>"] += 1

        first_val = str(row.get(first_col, "") or "").strip()
        if cat == "UNKNOWN":
            unknown_firstline[first_val or "<EMPTY>"] += 1
            rows_unknown.append(
                {
                    "pn": str(row.get(pn_col, "") or "").strip(),
                    "first_line": first_val,
                    "second_line": str(row.get(second_col, "") or "").strip(),
                    "series": str(row.get("Series", "") or row.get("系列", "") or "").strip(),
                    "internal_model": str(row.get("Internal Model", "") or "").strip(),
                    "external_model": str(row.get("External Model", "") or "").strip(),
                }
            )
            continue

        if pg and not is_strict_price_group_compatible(cat, pg):
            rows_strict_mismatch.append(
                {
                    "pn": str(row.get(pn_col, "") or "").strip(),
                    "category": cat,
                    "price_group": pg,
                    "first_line": first_val,
                    "second_line": str(row.get(second_col, "") or "").strip(),
                }
            )

    return {
        "total": int(len(df)),
        "unknown_count": int(len(rows_unknown)),
        "strict_mismatch_count": int(len(rows_strict_mismatch)),
        "unknown_rows": rows_unknown,
        "strict_mismatch_rows": rows_strict_mismatch,
        "unknown_firstline_top": unknown_firstline.most_common(30),
        "category_top": by_category.most_common(30),
        "price_group_top": by_price_group.most_common(30),
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fr_df, sys_df, map_fr, map_sys = _read_runtime_data()

    fr = _audit_side(fr_df, map_fr, "france")
    sy = _audit_side(sys_df, map_sys, "sys")

    pd.DataFrame(fr["unknown_rows"]).to_csv(OUT_DIR / "unknown_france.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(fr["strict_mismatch_rows"]).to_csv(
        OUT_DIR / "strict_mismatch_france.csv", index=False, encoding="utf-8-sig"
    )
    pd.DataFrame(sy["unknown_rows"]).to_csv(OUT_DIR / "unknown_sys.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(sy["strict_mismatch_rows"]).to_csv(
        OUT_DIR / "strict_mismatch_sys.csv", index=False, encoding="utf-8-sig"
    )

    summary = {
        "france": {
            "total": fr["total"],
            "unknown_count": fr["unknown_count"],
            "strict_mismatch_count": fr["strict_mismatch_count"],
            "unknown_firstline_top": fr["unknown_firstline_top"],
        },
        "sys": {
            "total": sy["total"],
            "unknown_count": sy["unknown_count"],
            "strict_mismatch_count": sy["strict_mismatch_count"],
            "unknown_firstline_top": sy["unknown_firstline_top"],
        },
        "rule_key_check": {
            "mapping_categories": sorted(
                {
                    str(c).strip()
                    for c in pd.concat([map_fr["category"], map_sys["category"]], ignore_index=True)
                    if str(c).strip()
                }
            ),
            "mapping_price_groups": sorted(
                {
                    str(g).strip()
                    for g in pd.concat([map_fr["price_group_hint"], map_sys["price_group_hint"]], ignore_index=True)
                    if str(g).strip()
                }
            ),
            "missing_ddp_categories": sorted(
                {
                    c
                    for c in {
                        str(x).strip()
                        for x in pd.concat([map_fr["category"], map_sys["category"]], ignore_index=True)
                        if str(x).strip()
                    }
                    if c and c not in DDP_RULES
                }
            ),
            "missing_price_groups": sorted(
                {
                    g
                    for g in {
                        str(x).strip()
                        for x in pd.concat([map_fr["price_group_hint"], map_sys["price_group_hint"]], ignore_index=True)
                        if str(x).strip()
                    }
                    if g and g not in PRICE_RULES
                }
            ),
        },
        "output_dir": str(OUT_DIR),
    }

    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
