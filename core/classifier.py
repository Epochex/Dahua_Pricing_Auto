# core/classifier.py
import re
from typing import Optional, Tuple

import pandas as pd


def safe_upper(v) -> str:
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except Exception:  # noqa: BLE001
        pass
    return str(v).strip().upper()



def _normalize_field_name(f) -> str:
    if not isinstance(f, str):
        return ""
    f = f.strip()
    if not f or f.lower() == "nan":
        return ""
    return f



def safe_upper(v) -> str:
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except Exception:
        pass
    return str(v).strip().upper()


def _normalize_field_name(f) -> str:
    if not isinstance(f, str):
        return ""
    f = f.strip()
    if not f or f.lower() == "nan":
        return ""
    return f


def _row_get_with_aliases(row: pd.Series, primary: str, aliases: list[str]) -> str:
    """
    兼容列名拼写差异（比如 Catalog Name / Catelog Name）。
    找到就返回 safe_upper(value)，找不到返回空串。
    """
    keys = [primary] + aliases
    for k in keys:
        if k in row:
            return safe_upper(row.get(k))
    return ""


def apply_mapping(row: pd.Series, mapping: pd.DataFrame) -> Tuple[str, Optional[str]]:
    """
    通用映射逻辑：
      - 先执行硬路由（pre-route），用于“Accessory 线缆/HDMI Extender”等关键字规则
      - 再按 mapping.csv 的 priority 从小到大匹配
      - 支持 equals / contains 两种模式
      - 返回 (category, price_group_hint)
    """

    # =========================
    # 0) Pre-route: ACCESSORY线缆关键字优先规则（比CSV的70/71/75更优先）
    # =========================
    first_pl = safe_upper(row.get("First Product Line"))
    if first_pl == "ACCESSORY":
        # 注意：你 SysPrice 表头里是 “Catelog Name”（拼写就这样），这里做了别名兼容
        cat_name = _row_get_with_aliases(row, "Catelog Name", ["Catalog Name", "Catalogue Name"])
        ext_model = safe_upper(row.get("External Model"))
        int_model = safe_upper(row.get("Internal Model"))

        haystack = " | ".join([cat_name, ext_model, int_model])

        # 你指定的关键字：PFM / HDMI / EXTENDER
        if ("PFM" in haystack) or ("HDMI" in haystack) or ("EXTENDER" in haystack):
            return "ACCESSORY线缆", "ACCESSORY线缆"

    # =========================
    # 1) 正常 CSV Mapping 规则匹配
    # =========================
    if mapping is None or mapping.empty:
        return "UNKNOWN", None

    # 统一列名、去空
    mapping = mapping.copy()
    mapping["field1"] = mapping["field1"].apply(_normalize_field_name)
    mapping["match_type1"] = mapping["match_type1"].apply(_normalize_field_name)
    mapping["pattern1"] = mapping["pattern1"].apply(_normalize_field_name)

    mapping["field2"] = mapping["field2"].apply(_normalize_field_name)
    mapping["match_type2"] = mapping["match_type2"].apply(_normalize_field_name)
    mapping["pattern2"] = mapping["pattern2"].apply(_normalize_field_name)

    # priority 从小到大
    if "priority" in mapping.columns:
        mapping = mapping.sort_values(by="priority", ascending=True, kind="mergesort")

    # 逐条匹配
    for _, rule in mapping.iterrows():
        f1 = rule.get("field1", "")
        t1 = safe_upper(rule.get("match_type1", ""))
        p1 = safe_upper(rule.get("pattern1", ""))

        f2 = rule.get("field2", "")
        t2 = safe_upper(rule.get("match_type2", ""))
        p2 = safe_upper(rule.get("pattern2", ""))

        # rule 目标
        category = str(rule.get("category", "")).strip()
        price_group_hint = str(rule.get("price_group_hint", "")).strip()
        if not category or category.lower() == "nan":
            continue

        def _match(field: str, mtype: str, pattern: str) -> bool:
            if not field:
                return True  # 该条件不存在，视为通过
            v = _row_get_with_aliases(row, field, [])  # CSV里写什么列名就按那个；别名只在 pre-route 里用
            if not v:
                return False
            if mtype == "EQUALS":
                return v == pattern
            if mtype == "CONTAINS":
                return pattern in v
            return False

        ok1 = _match(f1, t1, p1)
        if not ok1:
            continue
        ok2 = _match(f2, t2, p2)
        if not ok2:
            continue

        return category, (price_group_hint if price_group_hint and price_group_hint.lower() != "nan" else None)

    return "UNKNOWN", None


def _heuristic_detect_category_for_recorder(big: str) -> Tuple[str, Optional[str]]:
    """
    当 France/Sys mapping 都未命中时，强兜底识别录像机大类：
    - 优先识别 IVSS / EVS / XVR（比 NVR 更“专名”）
    - 其次识别 NVR
    返回 (category, price_group_hint)
    """
    s = safe_upper(big)

    # IVSS / EVS / XVR
    if "IVSS" in s:
        return "IVSS", "IVSS"
    if re.search(r"\bEVS\b", s) or "EVS" in s:
        return "EVS", "EVS"
    if re.search(r"\bXVR\b", s) or "XVR" in s:
        return "XVR", "XVR"

    # NVR：覆盖 NVR4104HS / NVR4216 / NVR5xxx / NVR6xxx 等
    if re.search(r"\bNVR\b", s) or re.search(r"\bNVR[0-9]", s):
        return "NVR", "NVR"

    return "UNKNOWN", None


def _build_big_text(france_row: Optional[pd.Series], sys_row: Optional[pd.Series]) -> str:
    parts = []

    if france_row is not None:
        for col in ("Internal Model", "External Model", "Series", "系列", "Description", "Second Product Line"):
            if col in france_row and pd.notna(france_row[col]):
                parts.append(str(france_row[col]))

    if sys_row is not None:
        for col in ("Internal Model", "External Model", "Second Product Line", "Catelog Name", "First Product Line"):
            if col in sys_row and pd.notna(sys_row[col]):
                parts.append(str(sys_row[col]))

    return " ".join(parts)


def _detect_ipc_series_key(big: str) -> str:
    # IPC 代际：优先看 IPCx 字样，再兜底看 HFW/HDW 的首位数字
    if "IPC8" in big:
        return "IPC8"
    if "IPC7" in big:
        return "IPC7"
    if "IPC5" in big:
        return "IPC5"
    if "IPC3" in big:
        return "IPC3"
    if "IPC2" in big:
        return "IPC2"
    if "IPC1" in big:
        return "IPC1"

    m = re.search(r"H[DF]W([0-9])", big)
    if not m:
        return ""

    d = m.group(1)
    if d == "8":
        return "IPC8"
    if d == "7":
        return "IPC7"
    if d == "5":
        return "IPC5"
    if d == "3":
        return "IPC3"
    if d == "2":
        return "IPC2"
    if d == "1":
        return "IPC1"

    return ""


def _strip_vendor_prefix(model: str) -> str:
    s = safe_upper(model)
    s = re.sub(r"^(DHI|DH)\s*-\s*", "", s)
    return s.strip()


def _detect_ptz_series_key(big: str) -> str:
    m = re.search(r"\b(?:DHI|DH)\s*-\s*([A-Z0-9]+(?:-[A-Z0-9]+)*)\b", big)
    cand = ""
    if m:
        cand = m.group(1)
    else:
        m2 = re.search(r"\b(SD[0-9A-Z]+(?:-[A-Z0-9]+)*)\b", big)
        if m2:
            cand = m2.group(1)
        else:
            m3 = re.search(r"\b(PTZ[0-9A-Z]+(?:-[A-Z0-9]+)*)\b", big)
            if m3:
                cand = m3.group(1)

    cand = _strip_vendor_prefix(cand)
    if not cand:
        return ""
    token = cand.split("-", 1)[0].strip()
    return token


def _detect_nvr_pricing_group(big: str) -> str:
    """
    把 NVR/IVSS 相关型号映射到两大类 key，用于 PRICE_RULES 选子规则：

      A) "IVSS / NVR6 / NVR5-I/L"
      B) "NVR5-EI/ NVR4 / NVR 2"
    """
    s = big

    # 1) IVSS
    if "IVSS" in s:
        return "IVSS / NVR6 / NVR5-I/L"

    # 2) NVR 代际（NVR 后第一个数字）
    m = re.search(r"\bNVR\s*([0-9])", s)
    gen = m.group(1) if m else ""

    if gen == "6":
        return "IVSS / NVR6 / NVR5-I/L"
    if gen in {"4", "2"}:
        return "NVR5-EI/ NVR4 / NVR 2"

    if gen == "5":
        # EI
        if re.search(r"\bEI\b", s) or re.search(r"-EI\b", s):
            return "NVR5-EI/ NVR4 / NVR 2"

        # I/L
        if re.search(r"-I/L\b", s):
            return "IVSS / NVR6 / NVR5-I/L"

        # -I 或 -L（避免 IR 误判：要求连字符边界）
        if re.search(r"-I\b", s) or re.search(r"-L\b", s):
            return "IVSS / NVR6 / NVR5-I/L"

        return "NVR5-EI/ NVR4 / NVR 2"

    return ""


def detect_series(
    france_row: Optional[pd.Series],
    sys_row: Optional[pd.Series],
    price_group: Optional[str],
) -> Tuple[str, str]:
    """
    返回 (series_display, series_key_for_price_rules)
    """
    series_display = ""
    if france_row is not None:
        for col in ("Series", "系列"):
            if col in france_row and pd.notna(france_row[col]):
                series_display = str(france_row[col]).strip()
                break
    if not series_display and sys_row is not None:
        for col in ("Second Product Line", "Catelog Name"):
            if col in sys_row and pd.notna(sys_row[col]):
                series_display = str(sys_row[col]).strip()
                break

    series_key = ""
    pg = (price_group or "").strip().upper()

    if pg == "IPC":
        pieces = []
        if france_row is not None:
            for col in ("Series", "系列", "External Model", "Internal Model", "Description"):
                if col in france_row and pd.notna(france_row[col]):
                    pieces.append(str(france_row[col]))
        if sys_row is not None:
            for col in ("Internal Model", "External Model", "Second Product Line", "Catelog Name"):
                if col in sys_row and pd.notna(sys_row[col]):
                    pieces.append(str(sys_row[col]))
        big = safe_upper(" ".join(pieces))
        series_key = _detect_ipc_series_key(big)

    if pg == "PTZ":
        pieces = []
        if france_row is not None:
            for col in ("Internal Model", "External Model", "Series", "系列", "Description"):
                if col in france_row and pd.notna(france_row[col]):
                    pieces.append(str(france_row[col]))
        if sys_row is not None:
            for col in ("Internal Model", "External Model", "Second Product Line", "Catelog Name"):
                if col in sys_row and pd.notna(sys_row[col]):
                    pieces.append(str(sys_row[col]))
        big = safe_upper(" ".join(pieces))
        series_key = _detect_ptz_series_key(big)

    if pg in {"NVR", "IVSS", "EVS", "XVR"}:
        pieces = []
        if france_row is not None:
            for col in ("Internal Model", "External Model", "Series", "系列", "Description"):
                if col in france_row and pd.notna(france_row[col]):
                    pieces.append(str(france_row[col]))
        if sys_row is not None:
            for col in ("Internal Model", "External Model", "Second Product Line", "Catelog Name"):
                if col in sys_row and pd.notna(sys_row[col]):
                    pieces.append(str(sys_row[col]))
        big = safe_upper(" ".join(pieces))
        series_key = _detect_nvr_pricing_group(big) or series_key

    if pg == "THERMAL":
        if france_row is not None:
            s_up = safe_upper(france_row.get("Series") or france_row.get("系列"))
            if "TPC4" in s_up or "TPC5" in s_up:
                series_key = "TPC4 TPC5"
            elif "TPC" in s_up:
                series_key = "TPC"

    return series_display, series_key or ""


def classify_category_and_price_group(
    france_row: Optional[pd.Series],
    sys_row: Optional[pd.Series],
    france_map: pd.DataFrame,
    sys_map: pd.DataFrame,
) -> Tuple[str, Optional[str]]:
    """
    综合 France + Sys 两侧信息确定 category & price_group_hint。
    优先使用 France 映射，失败再用 Sys。
    两边都失败时：对录像机大类（NVR/IVSS/EVS/XVR）做强兜底识别，避免 UNKNOWN 直接中断自动定价。
    """
    # 1) France 优先
    if france_row is not None:
        cat, pg = apply_mapping(france_row, france_map)
        if cat != "UNKNOWN":
            return cat, pg

    # 2) Sys 其次
    if sys_row is not None:
        cat, pg = apply_mapping(sys_row, sys_map)
        if cat != "UNKNOWN":
            return cat, pg

    # 3) 两边都失败：强兜底（仅限录像机大类，避免误伤其他品类）
    big = _build_big_text(france_row, sys_row)
    cat, pg = _heuristic_detect_category_for_recorder(big)
    return cat, pg
