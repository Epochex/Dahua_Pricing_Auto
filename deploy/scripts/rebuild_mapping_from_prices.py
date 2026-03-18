#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.engine.core.classifier import apply_mapping  # noqa: E402


RUNTIME_DIR = Path("/data/dahua_pricing_runtime")
MAPPING_DIR = REPO_ROOT / "mapping"

COLS = [
    "priority",
    "field1",
    "match_type1",
    "pattern1",
    "field2",
    "match_type2",
    "pattern2",
    "category",
    "price_group_hint",
    "note",
]


def norm(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if s.lower() == "nan":
        return ""
    return s


def load_data() -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    fr_path = RUNTIME_DIR / "data" / "FrancePrice.xlsx"
    if not fr_path.exists():
        fr_path = RUNTIME_DIR / "data" / "FrancePrice.xls"
    sys_path = RUNTIME_DIR / "data" / "SysPrice.xlsx"
    if not sys_path.exists():
        sys_path = RUNTIME_DIR / "data" / "SysPrice.xls"

    fr_map = pd.read_csv(RUNTIME_DIR / "mapping" / "productline_map_france_full.csv")
    sys_map = pd.read_csv(RUNTIME_DIR / "mapping" / "productline_map_sys_full.csv")

    fr_df = pd.read_excel(fr_path, engine="openpyxl" if fr_path.suffix.lower() != ".xls" else "xlrd")
    sys_df = pd.read_excel(sys_path, engine="openpyxl" if sys_path.suffix.lower() != ".xls" else "xlrd")
    return fr_df, sys_df, fr_map, sys_map


def teacher_labels(df: pd.DataFrame, mapping: pd.DataFrame) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for _, row in df.iterrows():
        cat, pg = apply_mapping(row, mapping)
        out.append((norm(cat), norm(pg)))
    return out


def dominant_label(counter: Counter[Tuple[str, str]]) -> Tuple[Tuple[str, str], int, int, float]:
    lab, cnt = counter.most_common(1)[0]
    total = sum(counter.values())
    ratio = cnt / total if total else 0.0
    return lab, cnt, total, ratio


def add_rule(
    rows: List[Dict[str, Any]],
    field1: str,
    match_type1: str,
    pattern1: str,
    field2: str,
    match_type2: str,
    pattern2: str,
    category: str,
    price_group_hint: str,
    note: str,
) -> None:
    rows.append(
        {
            "priority": 0,  # filled later
            "field1": field1,
            "match_type1": match_type1,
            "pattern1": pattern1,
            "field2": field2,
            "match_type2": match_type2,
            "pattern2": pattern2,
            "category": category,
            "price_group_hint": price_group_hint,
            "note": note,
        }
    )


def build_mapping(
    df: pd.DataFrame,
    labels: List[Tuple[str, str]],
    side: str,
) -> pd.DataFrame:
    if side == "france":
        first_col = "First Level Product Category"
        second_col = "Second Level Product Category"
        series_col = "Series"
        internal_col = "Internal Model"
    else:
        first_col = "First Product Line"
        second_col = "Second Product Line"
        series_col = "Second Product Line"
        internal_col = "Internal Model"

    rows: List[Dict[str, Any]] = []

    # 1) Seed high-confidence token rules (capture recorder/mobile branches early).
    seed_tokens = [
        ("IVSS", ("IVSS", "IVSS")),
        ("EVS", ("EVS", "EVS")),
        ("XVR", ("XVR", "XVR")),
        ("MNVR", ("车载后端", "车载")),
    ]
    for token, (cat, pg) in seed_tokens:
        has = df[internal_col].fillna("").astype(str).str.upper().str.contains(token, na=False)
        if int(has.sum()) == 0:
            continue
        add_rule(
            rows,
            internal_col,
            "contains",
            token,
            "",
            "",
            "",
            cat,
            pg,
            f"auto-learn seed: {internal_col} contains {token}",
        )

    if side == "france":
        add_rule(
            rows,
            first_col,
            "equals",
            "General Customized Product",
            series_col,
            "equals",
            "Wireless Alarm",
            "ALARM",
            "ALARM",
            "auto-learn seed: General Customized Product / Wireless Alarm",
        )
        add_rule(
            rows,
            first_col,
            "equals",
            "Mobile",
            series_col,
            "equals",
            "Auto Terminal",
            "车载后端",
            "车载",
            "auto-learn seed: Mobile / Auto Terminal",
        )
        add_rule(
            rows,
            first_col,
            "equals",
            "Account",
            series_col,
            "equals",
            "IP Camera for Distributors",
            "IPC",
            "IPC",
            "auto-learn seed: Account / IP Camera for Distributors",
        )

    # Sys 侧经验规则：Auto Terminal 默认归车载后端，XVR 由上面的 Internal Model contains XVR 抢先命中。
    if side == "sys":
        add_rule(
            rows,
            first_col,
            "equals",
            "Dahua Auto",
            second_col,
            "equals",
            "Auto Terminal",
            "车载后端",
            "车载",
            "auto-learn seed: Dahua Auto / Auto Terminal",
        )
        add_rule(
            rows,
            first_col,
            "equals",
            "Intelligent Building",
            second_col,
            "equals",
            "Pedestrian Turnstile",
            "ACCESS CONTROL",
            "ACCESS CONTROL",
            "manual override: Pedestrian Turnstile -> ACCESS CONTROL",
        )

    # 2) Learn exact first+second rules from dominant labels.
    combo_counter: Dict[Tuple[str, str], Counter[Tuple[str, str]]] = defaultdict(Counter)
    first_counter: Dict[str, Counter[Tuple[str, str]]] = defaultdict(Counter)
    combo_series_counter: Dict[Tuple[str, str], Dict[str, Counter[Tuple[str, str]]]] = defaultdict(
        lambda: defaultdict(Counter)
    )

    for i, (_, r) in enumerate(df.iterrows()):
        y = labels[i]
        if not y[0]:
            y = ("ACCESSORY", "ACCESSORY")
        f1 = norm(r.get(first_col))
        f2 = norm(r.get(second_col))
        ss = norm(r.get(series_col))
        combo_counter[(f1, f2)][y] += 1
        first_counter[f1][y] += 1
        if ss:
            combo_series_counter[(f1, f2)][ss][y] += 1

    # 2.1) For impure combinations, learn "first + series" exception rules first.
    for (f1, f2), cnt in sorted(combo_counter.items(), key=lambda kv: sum(kv[1].values()), reverse=True):
        _, _, total, ratio = dominant_label(cnt)
        if total < 2 or ratio >= 0.98:
            continue

        dom_label, _, _, _ = dominant_label(cnt)
        for series_val, sc in sorted(combo_series_counter[(f1, f2)].items(), key=lambda kv: sum(kv[1].values()), reverse=True):
            lab, lab_cnt, lab_total, lab_ratio = dominant_label(sc)
            if lab == dom_label:
                continue
            if lab_total < 1 or lab_ratio < 1.0:
                continue
            if not f1 or not series_val:
                continue
            add_rule(
                rows,
                first_col,
                "equals",
                f1,
                series_col,
                "equals",
                series_val,
                lab[0],
                lab[1] or lab[0],
                f"auto-learn exception from impure combo: ({f1}, {f2})",
            )

    # 2.2) Add core first+second rules.
    for (f1, f2), cnt in sorted(combo_counter.items(), key=lambda kv: sum(kv[1].values()), reverse=True):
        lab, _, total, ratio = dominant_label(cnt)
        if total >= 3 and ratio < 0.85:
            continue
        if total < 3 and ratio < 1.0:
            continue
        if not f1:
            continue
        add_rule(
            rows,
            first_col,
            "equals",
            f1,
            second_col,
            "equals",
            f2,
            lab[0],
            lab[1] or lab[0],
            f"auto-learn dominant first+second ({total}, purity={ratio:.3f})",
        )

    # 3) First-line fallback rules.
    for f1, cnt in sorted(first_counter.items(), key=lambda kv: sum(kv[1].values()), reverse=True):
        if not f1:
            continue
        lab, _, total, ratio = dominant_label(cnt)
        if ratio < 0.80:
            continue
        add_rule(
            rows,
            first_col,
            "equals",
            f1,
            "",
            "",
            "",
            lab[0],
            lab[1] or lab[0],
            f"auto-learn first-line fallback ({total}, purity={ratio:.3f})",
        )

    # 4) Ultimate catch-all fallback: never return UNKNOWN.
    add_rule(
        rows,
        first_col,
        "contains",
        "",
        "",
        "",
        "",
        "ACCESSORY",
        "ACCESSORY",
        "auto-learn final fallback",
    )

    # Fill monotonic priorities.
    for i, r in enumerate(rows, start=5):
        r["priority"] = i

    out = pd.DataFrame(rows, columns=COLS)
    return out


def backup_mapping_files() -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = MAPPING_DIR / "backup" / ts
    backup_dir.mkdir(parents=True, exist_ok=True)
    for fn in ["productline_map_france_full.csv", "productline_map_sys_full.csv"]:
        src = MAPPING_DIR / fn
        if src.exists():
            shutil.copy2(src, backup_dir / fn)
    return backup_dir


def write_mapping(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)


def copy_to_runtime() -> None:
    runtime_mapping = RUNTIME_DIR / "mapping"
    runtime_mapping.mkdir(parents=True, exist_ok=True)
    for fn in ["productline_map_france_full.csv", "productline_map_sys_full.csv"]:
        shutil.copy2(MAPPING_DIR / fn, runtime_mapping / fn)


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild mapping csv from price data via data-driven learning")
    parser.add_argument("--apply", action="store_true", help="overwrite mapping files in repo and runtime")
    args = parser.parse_args()

    fr_df, sys_df, fr_map, sys_map = load_data()
    fr_labels = teacher_labels(fr_df, fr_map)
    sys_labels = teacher_labels(sys_df, sys_map)

    fr_new = build_mapping(fr_df, fr_labels, "france")
    sys_new = build_mapping(sys_df, sys_labels, "sys")

    out_tmp = RUNTIME_DIR / "logs" / "mapping_rebuild_preview"
    out_tmp.mkdir(parents=True, exist_ok=True)
    write_mapping(fr_new, out_tmp / "productline_map_france_full.csv")
    write_mapping(sys_new, out_tmp / "productline_map_sys_full.csv")
    print(f"preview written: {out_tmp}")
    print(f"france rules: {len(fr_new)}")
    print(f"sys rules: {len(sys_new)}")

    if not args.apply:
        print("dry-run only. use --apply to overwrite mapping files.")
        return

    backup_dir = backup_mapping_files()
    write_mapping(fr_new, MAPPING_DIR / "productline_map_france_full.csv")
    write_mapping(sys_new, MAPPING_DIR / "productline_map_sys_full.csv")
    copy_to_runtime()
    print(f"backup created: {backup_dir}")
    print("mapping files overwritten in repo and /data/dahua_pricing_runtime/mapping")


if __name__ == "__main__":
    main()
