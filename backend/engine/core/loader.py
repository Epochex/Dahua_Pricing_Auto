# backend/engine/core/loader.py
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

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


def pn_key_raw(pn: str) -> str:
    return safe_upper(pn)


def pn_key_base(pn: str) -> str:
    """
    base key：去掉空白 + 去掉末尾标准后缀（-0001 / -0026 这类）。

    目标：对齐你本地 CLI 的 normalize_pn_base 行为，使：
      - 1.0.01.04.42701-0026  的 base key = 1.0.01.04.42701
      - 1.0.01.04.42701       的 base key = 1.0.01.04.42701

    说明：这里只“去掉最后一段 -dddd（4位数字）”，不做更激进的改写，避免误伤。
    """
    s = safe_upper(pn).replace(" ", "")
    m = re.match(r"^(.*?)-([0-9]{4})$", s)
    if m:
        return m.group(1)
    return s


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

    # 索引：raw/base -> row index（first match）
    fr_idx_raw: dict = None
    fr_idx_base: dict = None
    sys_idx_raw: dict = None
    sys_idx_base: dict = None


def _pick_pn_column(df: pd.DataFrame) -> str:
    """
    在不同表结构里找到 PN 列名。
    你原表里可能是 'Part No.' 或 'Part No' 或 'PartNo' 等。
    """
    candidates = [
        "Part No.",
        "Part No",
        "PART NO.",
        "PART NO",
        "PartNo",
        "PN",
        "pn",
        "Part Number",
        "Part Num",
        "PART NUM",
    ]
    cols = list(df.columns)
    for c in candidates:
        if c in cols:
            return c
    # fallback：尝试包含关键词
    for c in cols:
        uc = str(c).upper()
        if "PART" in uc and "NO" in uc:
            return c
        if "PART" in uc and "NUM" in uc:
            return c
    raise ValueError("cannot find PN column in dataframe")


def _build_index(df: pd.DataFrame) -> Tuple[dict, dict]:
    pn_col = _pick_pn_column(df)
    raw_map = {}
    base_map = {}
    for i, v in enumerate(df[pn_col].tolist()):
        r = pn_key_raw(v)
        b = pn_key_base(v)
        if r and r not in raw_map:
            raw_map[r] = i
        if b and b not in base_map:
            base_map[b] = i
    return raw_map, base_map


def _read_excel_any(path: Path) -> pd.DataFrame:
    # .xls/.xlsx：统一用 pandas 读
    suffix = path.suffix.lower()
    if suffix == ".xls":
        return pd.read_excel(path, engine="xlrd")
    return pd.read_excel(path)


def load_all_data(data_dir: Path) -> DataBundle:
    """
    约定 runtime/data 内文件名：
      - FrancePrice.xlsx
      - SysPrice.xls 或 SysPrice.xlsx
      - productline_map_france_full.csv
      - productline_map_sys_full.csv
    """
    data_dir = Path(data_dir)
    fr_path = data_dir / "FrancePrice.xlsx"
    sys_xls = data_dir / "SysPrice.xls"
    sys_xlsx = data_dir / "SysPrice.xlsx"
    map_fr_path = data_dir / "productline_map_france_full.csv"
    map_sys_path = data_dir / "productline_map_sys_full.csv"

    if not fr_path.exists():
        raise FileNotFoundError(f"missing {fr_path}")
    if sys_xls.exists():
        sys_path = sys_xls
    elif sys_xlsx.exists():
        sys_path = sys_xlsx
    else:
        raise FileNotFoundError(f"missing {sys_xls} or {sys_xlsx}")

    if not map_fr_path.exists():
        raise FileNotFoundError(f"missing {map_fr_path}")
    if not map_sys_path.exists():
        raise FileNotFoundError(f"missing {map_sys_path}")

    france_df = _read_excel_any(fr_path)
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
        france_price_path=fr_path,
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
      - .txt：每行一个 PN
      - .csv：第一列/或含 PN 字段
      - .xlsx/.xls：第一列/或含 PN 字段
    """
    path = Path(path)
    suf = path.suffix.lower()

    if suf == ".txt":
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        pns = []
        for ln in lines:
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            pns.append(s)
        return pns

    if suf == ".csv":
        df = pd.read_csv(path)
        col = df.iloc[:, 0]
        return [str(x).strip() for x in col.tolist() if str(x).strip()]

    if suf in (".xlsx", ".xls"):
        df = pd.read_excel(path)
        col = df.iloc[:, 0]
        return [str(x).strip() for x in col.tolist() if str(x).strip()]

    raise ValueError("unsupported pn list file")
