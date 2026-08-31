# backend/engine/core/loader.py
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd


REPORT_PRICE_GLOB = "reportPrice_*.xlsx"
FRANCE_PRICE_XLSX = "FrancePrice.xlsx"
PRICE_LIST_GLOBS = ("*PriceList.xls", "*PriceList.xlsx")
SYS_PRICE_XLSX = "SysPrice.xlsx"
EXCEL_HEADER_SCAN_ROWS = 50


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


_DAHUA_DOTTED_PN_SUFFIX_RE = re.compile(r"^([0-9.]+)-(.+)$")


def normalize_pn_base(pn: str) -> str:
    """
    base key：用于“同基底”匹配（你当前 server 逻辑强依赖这个）
    - strip + upper
    - 去空格
    - 若点分数字 PN 带国际化/区域后缀，则截断
      例：1.0.01.04.42701-0026 -> 1.0.01.04.42701
      例：1.0.99.12.10604-003  -> 1.0.99.12.10604
    """
    s = safe_upper(pn)
    s = s.replace(" ", "")
    if not s:
        return ""
    m = _DAHUA_DOTTED_PN_SUFFIX_RE.match(s)
    if m and "." in m.group(1):
        return m.group(1)
    return s


def _base_index_priority(raw_key: str, base_key: str) -> int:
    """
    Prefer the canonical PN for a base key, then the common -9001
    internationalized row, then keep the first remaining row.
    """
    raw = str(raw_key or "").strip().upper()
    base = str(base_key or "").strip().upper()
    if raw == base:
        return 0
    if raw == f"{base}-9001":
        return 1
    return 2


def _try_pick_pn_column(df: pd.DataFrame) -> Optional[str]:
    try:
        return _pick_pn_column(df)
    except ValueError:
        return None


def _read_excel_table(path: Path, *, engine: str) -> pd.DataFrame:
    """Read the first PN-bearing table, discovering its sheet and header row."""
    with pd.ExcelFile(path, engine=engine) as workbook:
        first = pd.read_excel(workbook, sheet_name=0)
        if _try_pick_pn_column(first) is not None:
            return first

        for sheet_name in workbook.sheet_names:
            probe = pd.read_excel(
                workbook,
                sheet_name=sheet_name,
                header=None,
                nrows=EXCEL_HEADER_SCAN_ROWS,
            )
            for header_row, values in probe.iterrows():
                header = pd.DataFrame(columns=values.tolist())
                if _try_pick_pn_column(header) is None:
                    continue
                table = pd.read_excel(
                    workbook,
                    sheet_name=sheet_name,
                    header=int(header_row),
                )
                if _try_pick_pn_column(table) is not None:
                    return table

        return first


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
        return _read_excel_table(path, engine="openpyxl")

    # Legacy .xls files use the OLE Compound File Binary Format.
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return _read_excel_table(path, engine="xlrd")

    if suffix == ".xls":
        # 需要 xlrd（仅 .xls）
        return _read_excel_table(path, engine="xlrd")

    if suffix in (".xlsx", ".xlsm"):
        # 强制使用 openpyxl（你的目标）
        return _read_excel_table(path, engine="openpyxl")

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


def _latest_report_price_path(data_dir: Path) -> Optional[Path]:
    candidates = [p for p in Path(data_dir).glob(REPORT_PRICE_GLOB) if p.is_file()]
    if not candidates:
        return None

    def _sort_key(path: Path) -> Tuple[float, str]:
        try:
            mtime = float(path.stat().st_mtime)
        except OSError:
            mtime = 0.0
        return mtime, path.name

    return max(candidates, key=_sort_key)


def _latest_file_by_globs(data_dir: Path, patterns: Tuple[str, ...]) -> Optional[Path]:
    candidates: List[Path] = []
    for pattern in patterns:
        candidates.extend(p for p in Path(data_dir).glob(pattern) if p.is_file())
    if not candidates:
        return None

    def _sort_key(path: Path) -> Tuple[float, str]:
        try:
            mtime = float(path.stat().st_mtime)
        except OSError:
            mtime = 0.0
        return mtime, path.name

    return max(candidates, key=_sort_key)


def _is_ooxml_excel(path: Path) -> bool:
    try:
        with Path(path).open("rb") as f:
            return f.read(4).startswith(b"PK")
    except OSError:
        return False


def _normalize_report_price_file(report_path: Path, target_path: Path) -> Path:
    """
    Convert a GSP reportPrice_*.xlsx export into the runtime FrancePrice.xlsx.

    GSP country exports currently contain:
      - sheet 1: navigation
      - sheet 2: products
      - products row 1: Back to Navigation helper row
      - products row 2: real header

    The pricing engine expects a normal one-sheet table, so we keep only the
    products sheet and read it with row 2 as the header.
    """
    report_path = Path(report_path)
    target_path = Path(target_path)

    df = _read_excel_any(report_path)
    df = df.dropna(how="all")
    df = df.loc[:, ~df.columns.astype(str).str.match(r"^Unnamed:")]

    # Fail early if the normalized file would not be usable as FrancePrice.
    _pick_pn_column(df)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(f".{target_path.stem}.tmp{target_path.suffix}")
    try:
        with pd.ExcelWriter(tmp_path, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="products", index=False)
        tmp_path.replace(target_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    try:
        report_path.unlink()
    except OSError:
        # Normalization succeeded; a leftover source file should not block
        # startup/data reload.
        pass

    return target_path


def _normalize_price_list_file(price_list_path: Path, target_path: Path) -> Path:
    """
    Consume a Sys PriceList export and replace runtime SysPrice.xlsx.

    Most GSP exports named "(timestamp) PriceList.xls" are actually OOXML/xlsx
    files with an .xls suffix. For those, a validated atomic rename preserves the
    original workbook. If a true legacy .xls appears, convert it into xlsx.
    """
    price_list_path = Path(price_list_path)
    target_path = Path(target_path)

    df = _read_excel_any(price_list_path)
    _pick_pn_column(df)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    if _is_ooxml_excel(price_list_path):
        price_list_path.replace(target_path)
        return target_path

    tmp_path = target_path.with_name(f".{target_path.stem}.tmp{target_path.suffix}")
    try:
        with pd.ExcelWriter(tmp_path, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="Sheet1", index=False)
        tmp_path.replace(target_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    try:
        price_list_path.unlink()
    except OSError:
        pass

    return target_path


def _prepare_france_price_file(data_dir: Path) -> Path:
    """
    If a fresh GSP reportPrice_*.xlsx was dropped into runtime/data, consume it
    and turn it into the canonical FrancePrice.xlsx before loading data.
    """
    data_dir = Path(data_dir)
    report_path = _latest_report_price_path(data_dir)
    if report_path is not None:
        return _normalize_report_price_file(report_path, data_dir / FRANCE_PRICE_XLSX)

    return _pick_existing(
        data_dir / FRANCE_PRICE_XLSX,
        data_dir / "FrancePrice.xls",
    )


def _prepare_sys_price_file(data_dir: Path) -> Path:
    """
    If a fresh "(timestamp) PriceList.xls[x]" was dropped into runtime/data,
    consume it and turn it into the canonical SysPrice.xlsx before loading data.
    """
    data_dir = Path(data_dir)
    price_list_path = _latest_file_by_globs(data_dir, PRICE_LIST_GLOBS)
    if price_list_path is not None:
        return _normalize_price_list_file(price_list_path, data_dir / SYS_PRICE_XLSX)

    return _pick_existing(
        data_dir / "SysPrice.xls",
        data_dir / SYS_PRICE_XLSX,
    )


def prepare_price_data_files(data_dir: Path) -> Tuple[Path, Path]:
    """
    Normalize fresh France/Sys exports in runtime/data and return canonical paths.
    This is used by both loader startup and restart scripts.
    """
    data_dir = Path(data_dir)
    france_path = _prepare_france_price_file(data_dir)
    sys_path = _prepare_sys_price_file(data_dir)
    return france_path, sys_path


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

    # 3) 容忍导出器插入换行、不间断空格或标点差异。
    canonical_pn_headers = {"PARTNO", "PARTNUM", "PARTNUMBER", "PN"}
    for c in cols:
        canonical = re.sub(r"[^A-Z0-9]+", "", str(c).upper())
        if canonical in canonical_pn_headers:
            return c

    # 4) 再做包含匹配（更宽松）
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
    base_priority: Dict[str, int] = {}

    for i, v in enumerate(df[pn_col].tolist()):
        r = normalize_pn_raw(v)
        b = normalize_pn_base(v)
        # 保留第一次出现的位置（避免重复 PN 乱跳）
        if r and r not in raw_map:
            raw_map[r] = i
        if not b:
            continue
        pri = _base_index_priority(r, b)
        if b not in base_map:
            base_map[b] = i
            base_priority[b] = pri
        elif pri < base_priority.get(b, 999):
            base_map[b] = i
            base_priority[b] = pri

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

    france_path, sys_path = prepare_price_data_files(data_dir)

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
