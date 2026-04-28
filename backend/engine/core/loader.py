# backend/engine/core/loader.py
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
    """
    raw key：用于 exact 匹配
    - strip
    - upper
    """
    return safe_upper(pn)


_BASE_SUFFIX_RE = re.compile(r"^(.*?)-\d{4}$")  # 末尾形如 -0006 / -0048


def normalize_pn_base(pn: str) -> str:
    """
    base key：用于“同基底”匹配（你当前 server 逻辑强依赖这个）
    - strip + upper
    - 去空格
    - 若末尾带 -xxxx（四位数字）则截断
      例：1.0.01.04.42701-0026 -> 1.0.01.04.42701
    """
    s = safe_upper(pn)
    s = s.replace(" ", "")
    if not s:
        return ""
    m = _BASE_SUFFIX_RE.match(s)
    if m:
        return m.group(1)
    return s


def _read_excel_any(path: Path) -> pd.DataFrame:
    """
    按真实文件格式优先选择引擎，后缀仅作为兜底：
    - xlsx/xlsm (zip/OOXML) -> openpyxl
    - xls (OLE/BIFF8)       -> xlrd
    说明：
    - openpyxl 不支持 .xls（BIFF8 老格式）
    - xlrd 2.x 不支持 .xlsx
    - 数据更新链路里可能出现 xlsx 内容但命名为 .xls 的文件，需要按文件头兼容
    """
    path = Path(path)
    suffix = path.suffix.lower()
    try:
        with path.open("rb") as f:
            head = f.read(8)
    except OSError:
        head = b""

    # OOXML files are zip archives and start with PK, even when the suffix is wrong.
    if head.startswith(b"PK"):
        return pd.read_excel(path, engine="openpyxl")

    # Legacy .xls files use the OLE Compound File Binary Format.
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return pd.read_excel(path, engine="xlrd")

    if suffix == ".xls":
        # 需要 xlrd（仅 .xls）
        return pd.read_excel(path, engine="xlrd")

    if suffix in (".xlsx", ".xlsm"):
        # 强制使用 openpyxl（你的目标）
        return pd.read_excel(path, engine="openpyxl")

    # 兜底（理论上当前业务不会走到这里）
    return pd.read_excel(path)


def _pick_existing(*paths: Path) -> Path:
    """
    从候选路径中选择第一个存在的文件。
    """
    for p in paths:
        if p.exists():
            return p
    raise FileNotFoundError(
        "No candidate file exists:\n" + "\n".join(str(p) for p in paths)
    )


def _pick_pn_column(df: pd.DataFrame) -> str:
    """
    在不同表结构里找到 PN 列名。
    """
    candidates = [
        "Part No.",
        "Part No",
        "PART NO.",
        "PART NO",
        "PartNo",
        "Part Num",
        "PART NUM",
        "PN",
        "pn",
        "P/N",
        "Part Number",
        "PartNumber",
    ]
    cols = list(df.columns)

    # 1) 先精确命中（含大小写 variants）
    for c in candidates:
        if c in cols:
            return c

    # 2) 再做不区分大小写精确
    low_map = {str(c).strip().lower(): c for c in cols}
    for c in candidates:
        k = c.strip().lower()
        if k in low_map:
            return low_map[k]

    # 3) 再做包含匹配（更宽松）
    for c in cols:
        uc = str(c).upper()
        if "PART" in uc and "NO" in uc:
            return c
        if "PART" in uc and "NUM" in uc:
            return c
        if uc in ("PN", "P/N"):
            return c

    raise ValueError("cannot find PN column in dataframe")


def _build_index(df: pd.DataFrame) -> Tuple[Dict[str, int], Dict[str, int]]:
    pn_col = _pick_pn_column(df)
    raw_map: Dict[str, int] = {}
    base_map: Dict[str, int] = {}

    for i, v in enumerate(df[pn_col].tolist()):
        r = normalize_pn_raw(v)
        b = normalize_pn_base(v)
        # 保留第一次出现的位置（避免重复 PN 乱跳）
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
    约定（你当前 runtime 结构）：
      runtime_dir/data/FrancePrice.xlsx 或 FrancePrice.xls
      runtime_dir/data/SysPrice.xls 或 SysPrice.xlsx
      runtime_dir/mapping/productline_map_france_full.csv
      runtime_dir/mapping/productline_map_sys_full.csv
    """
    data_dir = Path(data_dir)
    runtime_dir = data_dir.parent
    mapping_dir = runtime_dir / "mapping"

    france_path = _pick_existing(
        data_dir / "FrancePrice.xlsx",
        data_dir / "FrancePrice.xls",
    )

    sys_path = _pick_existing(
        data_dir / "SysPrice.xls",
        data_dir / "SysPrice.xlsx",
    )

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
      - .txt：逐行；支持空格/逗号/分号/制表符分隔的多个 PN
      - .csv：优先 PN 列，否则第一列
      - .xlsx/.xls/.xlsm：优先 PN 列，否则第一列
    """
    path = Path(path)
    suf = path.suffix.lower()

    if suf == ".txt":
        out: List[str] = []
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            # 一行可能粘贴了多个 PN
            tokens = [t.strip() for t in re.split(r"[\s,\t;]+", s) if t.strip()]
            out.extend(tokens)
        return out

    if suf == ".csv":
        df = pd.read_csv(path)
        if df.empty:
            return []
        try:
            col = _pick_pn_column(df)
            series = df[col]
        except Exception:
            series = df.iloc[:, 0]
        return [str(x).strip() for x in series.tolist() if str(x).strip()]

    if suf in (".xlsx", ".xls", ".xlsm"):
        df = _read_excel_any(path)
        if df.empty:
            return []
        try:
            col = _pick_pn_column(df)
            series = df[col]
        except Exception:
            series = df.iloc[:, 0]
        return [str(x).strip() for x in series.tolist() if str(x).strip()]

    raise ValueError("only .txt/.csv/.xlsx/.xls/.xlsm supported")
