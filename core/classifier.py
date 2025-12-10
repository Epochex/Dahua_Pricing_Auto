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
      - 按 priority 从小到大匹配
      - 支持 equals / contains 两种模式
      - 返回 (category, price_group_hint)
    """
    if mapping is None or mapping.empty:
        return "UNKNOWN", None

    # 显式按 priority 排序，保证优先级语义
    if "priority" in mapping.columns:
        iter_rules = mapping.sort_values("priority", ascending=True).iterrows()
    else:
        iter_rules = mapping.iterrows()

    for _, rule in iter_rules:
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


def _normalize_category_and_price_group(cat: str, grp: Optional[str]) -> Tuple[str, Optional[str]]:
    """
    统一某些品类的 price_group，用于渠道折扣。

    - 监视器 / 商显 / IT监视器 → 统一走 PRICE_RULES['监视器/商显/LCD']
    - 其他保持原逻辑：price_group_hint 优先，否则用 category 自己
    """
    if not cat:
        return cat, grp
    cat = cat.strip()

    # 折扣规则合并：监视器、商显/TV-WALL、IT监视器 共用同一组折扣行
    if cat in {"监视器", "商显/TV-WALL", "IT监视器"}:
        # 这里只改 price_group，用于 PRICE_RULES；
        # category 仍然保持原值，用于 DDP_RULES（DDP 公式不变）
        return cat, "监视器/商显/LCD"

    # 电子防盗门：DDP 仍用 "电子防盗门"，折扣统一走 ACCESS CONTROL 这棵树
    if cat == "电子防盗门":
        # cat 不动（仍然是电子防盗门，确保用 DDP_RULES["电子防盗门"]）
        # price_group 强制改成 ACCESS CONTROL → 用 PRICE_RULES["ACCESS CONTROL"]
        return cat, "ACCESS CONTROL"

    # 默认逻辑：grp 不为空就用 grp，否则用 cat
    return cat, (grp or cat)



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

    额外规则：
      - Sys 表中若 Catelog Name 显示 Accessories / Accessory，
        则优先按 ACCESSORY / ACCESSORY线缆 处理，避免被 First Product Line=IPC 误判。
    """
    # 1) France 优先
    if france_row is not None:
        cat, grp = apply_mapping(france_row, france_map)
        if cat != "UNKNOWN":
            cat, grp = _normalize_category_and_price_group(cat, grp)
            return cat, grp

    # 2) Sys 信息
    if sys_row is not None:
        cat, grp = apply_mapping(sys_row, sys_map)

        # —— Accessories 特例处理 —— #
        catelog = safe_upper(sys_row.get("Catelog Name"))
        second_pl = safe_upper(sys_row.get("Second Product Line"))

        if "ACCESSORIES" in catelog or "ACCESSORY" in catelog:
            # 原本识别为 UNKNOWN 或 IPC 时，强制纠正为配件类
            if cat in {"UNKNOWN", "IPC"}:
                # 粗分：含 Cabling / Cable / 线缆 / Wire → 走 ACCESSORY线缆 规则
                if any(x in second_pl for x in ["CABLING", "CABLE", "线缆", "WIRE"]):
                    cat = "ACCESSORY线缆"
                    # 顶层 price_group 仍然走 ACCESSORY 这棵树
                    grp = "ACCESSORY"
                else:
                    cat = "ACCESSORY"
                    grp = "ACCESSORY"

        if cat != "UNKNOWN":
            cat, grp = _normalize_category_and_price_group(cat, grp)
            return cat, grp

    # 3) 最终兜底：按 IPC 处理（例如完全识别失败时）
    return "UNKNOWN", None
