# backend/engine/core/loader.py
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple, Dict

import pandas as pd


def safe_upper(v) -> str:
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except Exception:
        pass
    return str(v).strip().upper()


def normalize_pn_raw(pn: str) -> str:
    """raw key：仅做 strip + upper。"""
    return safe_upper(pn)


def normalize_pn_base(pn: str) -> str:
    """
    base key：用于“同系列/同基底”匹配
    - 去空格
    - 保留 '-'（与你现有习惯一致）
    - 若你本地有更复杂规则（例如去掉末尾 -xxxx），就在这里统一实现
    """
    s = safe_upper(pn)
    s = s.replace(" ", "")
    return s


def _read_excel_any(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".xls":
        return pd.read_excel(path, engine="xlrd")
    return pd.read_excel(path)


def _pick_pn_column(df: pd.DataFrame) -> str:
    candidates = [
        "Part No.",
        "Part No",
        "PART NO.",
        "PART NO",
        "PartNo",
        "PN",
        "pn",
        "Part Number",
    ]
    cols = list(df.columns)
    for c in candidates:
        if c in cols:
            return c
    for c in cols:
        uc = str(c).upper()
        if "PART" in uc and "NO" in uc:
            return c
    raise ValueError("cannot find PN column in dataframe")


def _build_index(df: pd.DataFrame) -> Tuple[Dict[str, int], Dict[str, int]]:
    pn_col = _pick_pn_column(df)
    raw_map: Dict[str, int] = {}
    base_map: Dict[str, int] = {}
    for i, v in enumerate(df[pn_col].tolist()):
        r = normalize_pn_raw(v)
        b = normalize_pn_base(v)
        if r and r not in raw_map:
            raw_map[r] = i
        if b and b not in base_map:
            base_map[b] = i
    return raw_map, base_map


@dataclass
class DataBundle:
    france_df: pd.DataFrame
    sys_df: pd.DataFrame
    map_fr: pd.DataFrame
    map_sys: pd.DataFrame

    france_price_path: Optional[Path] = None
    sys_price_path: Optional[Path] = None
    map_fr_path: Optional[Path] = None
    map_sys_path: Optional[Path] = None

    fr_idx_raw: Dict[str, int] = None
    fr_idx_base: Dict[str, int] = None
    sys_idx_raw: Dict[str, int] = None
    sys_idx_base: Dict[str, int] = None


def load_all_data(data_dir: Path) -> DataBundle:
    """
    约定：
      runtime_dir/data/FrancePrice.xlsx
      runtime_dir/data/SysPrice.xls 或 SysPrice.xlsx
      runtime_dir/../mapping/productline_map_france_full.csv
      runtime_dir/../mapping/productline_map_sys_full.csv

    你现在 repo 根目录就有 mapping/，runtime 在 repo/runtime/，
    所以默认 mapping_dir = runtime_dir.parent / "mapping"
    """
    data_dir = Path(data_dir)
    runtime_dir = data_dir.parent
    mapping_dir = runtime_dir.parent / "mapping"

    france_path = data_dir / "FrancePrice.xlsx"
    if not france_path.exists():
        raise FileNotFoundError(f"FrancePrice.xlsx not found: {france_path}")

    sys_candidates = [data_dir / "SysPrice.xls", data_dir / "SysPrice.xlsx"]
    sys_path = None
    for p in sys_candidates:
        if p.exists():
            sys_path = p
            break
    if sys_path is None:
        tried = "\n".join([str(x) for x in sys_candidates])
        raise FileNotFoundError("未找到 SysPrice 数据文件，已尝试以下路径：\n" + tried)

    map_fr_path = mapping_dir / "productline_map_france_full.csv"
    map_sys_path = mapping_dir / "productline_map_sys_full.csv"
    if not map_fr_path.exists():
        raise FileNotFoundError(f"mapping file missing: {map_fr_path}")
    if not map_sys_path.exists():
        raise FileNotFoundError(f"mapping file missing: {map_sys_path}")

    france_df = _read_excel_any(france_path)
    sys_df = _read_excel_any(sys_path)
    map_fr = pd.read_csv(map_fr_path)
    map_sys = pd.read_csv(map_sys_path)

    fr_idx_raw, fr_idx_base = _build_index(france_df)
    sys_idx_raw, sys_idx_base = _build_index(sys_df)

    return DataBundle(
        france_df=france_df,
        sys_df=sys_df,
        map_fr=map_fr,
        map_sys=map_sys,
        france_price_path=france_path,
        sys_price_path=sys_path,
        map_fr_path=map_fr_path,
        map_sys_path=map_sys_path,
        fr_idx_raw=fr_idx_raw,
        fr_idx_base=fr_idx_base,
        sys_idx_raw=sys_idx_raw,
        sys_idx_base=sys_idx_base,
    )


def parse_pn_list_file(path: Path) -> List[str]:
    """
    支持：
      - .txt：逐行 PN
      - .csv：读第一列或名为 Part No./PN 的列
      - .xlsx/.xls：读包含 PN 的列（同 _pick_pn_column 逻辑）
    """
    path = Path(path)
    suf = path.suffix.lower()

    if suf == ".txt":
        pns = []
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if not s:
                continue
            # 允许用户粘贴带空格/制表符的内容
            s = re.split(r"[\s,\t;]+", s)[0].strip()
            if s:
                pns.append(s)
        return pns

    if suf == ".csv":
        df = pd.read_csv(path)
        if df.empty:
            return []
        # 优先找 PN 列
        try:
            col = _pick_pn_column(df)
            series = df[col]
        except Exception:
            series = df.iloc[:, 0]
        return [str(x).strip() for x in series.tolist() if str(x).strip()]

    if suf in (".xlsx", ".xls"):
        df = _read_excel_any(path)
        if df.empty:
            return []
        col = _pick_pn_column(df)
        return [str(x).strip() for x in df[col].tolist() if str(x).strip()]

    raise ValueError("only .txt/.csv/.xlsx/.xls supported")
