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


def apply_mapping(row: pd.Series, mapping: pd.DataFrame) -> Tuple[str, Optional[str]]:
    """
    通用映射逻辑：
      - 逐行按 priority 顺序匹配
      - 支持 equals / contains 两种模式
      - 返回 (category, price_group_hint)
    """
    if mapping is None or mapping.empty:
        return "UNKNOWN", None

    for _, rule in mapping.iterrows():
        field1 = _normalize_field_name(rule.get("field1"))
        if not field1:
            continue
        match_type1 = str(rule.get("match_type1") or "").strip().lower()
        pattern1 = safe_upper(rule.get("pattern1"))
        value1 = safe_upper(row.get(field1))

        if match_type1 == "equals":
            if value1 != pattern1:
                continue
        elif match_type1 == "contains":
            if pattern1 not in value1:
                continue
        else:
            continue

        field2 = _normalize_field_name(rule.get("field2"))
        if field2:
            match_type2 = str(rule.get("match_type2") or "").strip().lower()
            pattern2 = safe_upper(rule.get("pattern2"))
            value2 = safe_upper(row.get(field2))

            if match_type2 == "equals":
                if value2 != pattern2:
                    continue
            elif match_type2 == "contains":
                if pattern2 not in value2:
                    continue
            else:
                continue

        category = str(rule.get("category") or "").strip()
        price_group = str(rule.get("price_group_hint") or "").strip() or None
        if not category:
            continue
        return category, price_group

    return "UNKNOWN", None


# ========= Series 识别（给 PRICE_RULES 用） =========


def _detect_ipc_series_key(big: str) -> str:
    """
    big: 已经 upper() 的串，包含 Series / Internal / External 的拼接
    返回给 PRICE_RULES 用的 series key，比如 "PSDW" / "IPC2" / "IPC3-S2" 等
    """

    # 先看明显的字串
    if "PSDW" in big:
        return "PSDW"
    if "PINHOLE" in big or "针孔" in big:
        return "针孔"

    # Multi-sensor / Special 系
    if "MULTI-SENSOR" in big or "MULTISENSOR" in big or "SPECIAL" in big or "IPC7" in big:
        return "IPC5/7/MULTI-SENSOR / SPECIAL"

    # 直接带 IPC5 / IPC3 / IPC2 / IPC1 文本的
    for key in ["IPC5", "IPC3-S2", "IPC2-PRO", "IPC2", "IPC1"]:
        if key in big:
            return key

    # 根据 HFW/HDW 后第一位数字猜代数
    m = re.search(r"H[DF]W(\d)", big)
    if m:
        d = m.group(1)
        if d == "5":
            return "IPC5"
        if d == "3":
            return "IPC3-S2"
        if d == "2":
            # 再看有没有 PRO
            if "PRO" in big:
                return "IPC2-PRO"
            return "IPC2"
        if d == "1":
            return "IPC1"

    return ""


def detect_series(
    france_row: Optional[pd.Series],
    sys_row: Optional[pd.Series],
    price_group: Optional[str],
) -> Tuple[str, str]:
    """
    返回 (series_display, series_key_for_price_rules)

    series_display: 给用户看的 Series（直接取 France 的 Series / 系列）
    series_key_for_price_rules: 用于在 PRICE_RULES[price_group] 里选择子规则
    """
    # 展示用 Series：优先 France 表
    series_display = ""
    if france_row is not None:
        for col in ("Series", "系列"):
            if col in france_row and pd.notna(france_row[col]):
                series_display = str(france_row[col]).strip()
                break
    if not series_display and sys_row is not None:
        # 兜底：用 Sys 的 Second Product Line
        for col in ("Second Product Line", "Catelog Name"):
            if col in sys_row and pd.notna(sys_row[col]):
                series_display = str(sys_row[col]).strip()
                break

    # 给 PRICE_RULES 用的 series key
    series_key = ""
    pg = (price_group or "").strip().upper()
    if pg == "IPC":
        pieces = []
        if france_row is not None:
            for col in ("Series", "系列", "External Model", "Internal Model"):
                if col in france_row and pd.notna(france_row[col]):
                    pieces.append(str(france_row[col]))
        if sys_row is not None:
            for col in ("Internal Model", "External Model", "Second Product Line"):
                if col in sys_row and pd.notna(sys_row[col]):
                    pieces.append(str(sys_row[col]))
        big = safe_upper(" ".join(pieces))
        series_key = _detect_ipc_series_key(big)

    # Thermal 简单处理
    if pg == "THERMAL":
        if france_row is not None:
            s_up = safe_upper(france_row.get("Series") or france_row.get("系列"))
            if "TPC4" in s_up or "TPC5" in s_up:
                series_key = "TPC4 TPC5"
            elif "TPC" in s_up:
                series_key = "TPC"

    # 其他品类目前用不到细分规则，返回空即可（PRICE_RULES 会走 _default_）
    return series_display, series_key or ""
    

def classify_category_and_price_group(
    france_row: Optional[pd.Series],
    sys_row: Optional[pd.Series],
    france_map: pd.DataFrame,
    sys_map: pd.DataFrame,
) -> Tuple[str, Optional[str]]:
    """
    综合 France + Sys 两侧信息确定 category & price_group_hint。
    优先使用 France 映射，失败再用 Sys，最后兜底 IPC。
    """
    if france_row is not None:
        cat, grp = apply_mapping(france_row, france_map)
        if cat != "UNKNOWN":
            return cat, grp or cat

    if sys_row is not None:
        cat, grp = apply_mapping(sys_row, sys_map)
        if cat != "UNKNOWN":
            return cat, grp or cat

    # 最终兜底：按 IPC 处理
    return "IPC", "IPC"
