# backend/engine/core/pricing_engine.py
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd

from backend.engine.core.classifier import classify_category_and_price_group, detect_series
from backend.engine.core.loader import DataBundle, normalize_pn_base, normalize_pn_raw
from backend.engine.core.pricing_rules import DDP_RULES, PRICE_RULES


PRICE_COLS = [
    "FOB C(EUR)",
    "DDP A(EUR)",
    "Suggested Reseller(EUR)",
    "Gold(EUR)",
    "Silver(EUR)",
    "Ivory(EUR)",
    "MSRP(EUR)",
]

# =========================
# Sys FOB Adjust（涨价系数）
# =========================
# 仅在“使用 Sys 计算 FOB 且 France FOB 缺失”时生效。
# 默认值可为空；通过 /api/admin/uplift（前端 Adjust 列）可热更新。
UPLIFT_PCT_BY_LINE: Dict[str, float] = {}
# 关键词涨价规则（叠加在 Sys FOB Adjust 之后）：
# [{"keyword":"ASC","pct":0.15,"enabled":True}, ...]
KEYWORD_UPLIFT_RULES: List[Dict[str, Any]] = []


def _detect_uplift_line_key(
    category: str,
    series_display: str,
    france_row: Optional[pd.Series],
    sys_row: Optional[pd.Series],
) -> str:
    """
    识别“涨价产品线 key”，用于 Sys FOB 阶段的 uplift。

    注意：
    - 该识别逻辑与 PRICE_RULES 的 series_key 不同；这里只为涨价策略服务。
    - 优先从可见的 Series / Second Product Line / 型号串中提取关键信息。
    """
    big_parts = []
    if series_display:
        big_parts.append(series_display)

    # France 信息
    if france_row is not None:
        for col in ("Series", "系列", "External Model", "Internal Model", "Description"):
            if col in france_row and pd.notna(france_row[col]):
                big_parts.append(str(france_row[col]))

    # Sys 信息
    if sys_row is not None:
        for col in (
            "Second Product Line",
            "Catelog Name",
            "External Model",
            "Internal Model",
            "First Product Line",
        ):
            if col in sys_row and pd.notna(sys_row[col]):
                big_parts.append(str(sys_row[col]))

    big = " ".join(big_parts).upper()
    cat_up = (category or "").strip().upper()

    # ---- IPC：优先识别 IPC8 / IPC7 / IPC5 / IPC3 / IPC2 / IPC1 ----
    if cat_up == "IPC":
        for k in ("IPC8", "IPC7", "IPC5", "IPC3", "IPC2", "IPC1"):
            if k in big:
                return k

        # 兜底：根据 HFW/HDW 后第一位数字猜代数（覆盖 8/7/5/3/2/1）
        m = re.search(r"H[DF]W([0-9])", big)
        if m:
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

    # ---- NVR / IVSS / EVS / IVD ----
    if cat_up in {"NVR", "IVSS", "EVS"}:
        if any(x in big for x in ("IVD", "IVSS", "EVS")):
            return "IVD/IVSS/EVS"

        if "NVR6" in big and "XI" in big:
            return "NVR6 XI"
        if "NVR5" in big and "EI2" in big:
            return "NVR5 EI2"
        if "NVR4" in big:
            return "NVR4"

        return ""

    # ---- 其他：按 category 自身处理 ----
    if cat_up in {"PTZ", "ITC", "SCP"}:
        return cat_up

    # Thermal：按 TPC
    if cat_up == "THERMAL":
        if "TPC" in big:
            return "TPC"
        return ""

    return ""


def _pick_sys_uplift(
    *,
    category: str,
    price_group: str,
    effective_price_group: str,
    price_rule_key: Optional[str],
    series_display: str,
    france_row: Optional[pd.Series],
    sys_row: Optional[pd.Series],
) -> Tuple[str, float]:
    """
    选择 Sys FOB uplift（仅 Sys 反算 FOB 场景）。

    优先级：
    1) 命中的子规则 key（ruleName，排除 _default_）
    2) 命中的价格组 key（product group）
    3) 原始 price_group / category
    4) legacy 检测 key（兼容历史配置）
    """
    candidates: List[str] = []

    def _push(v: Optional[str]) -> None:
        s = str(v or "").strip()
        if not s:
            return
        if s in candidates:
            return
        candidates.append(s)

    if price_rule_key and str(price_rule_key).strip() != "_default_":
        _push(price_rule_key)
    _push(effective_price_group)
    _push(price_group)
    _push(category)

    legacy_key = _detect_uplift_line_key(category, series_display, france_row, sys_row)
    _push(legacy_key)

    for k in candidates:
        pct = _to_float(UPLIFT_PCT_BY_LINE.get(k))
        if pct is None or pct <= 0:
            continue
        return k, pct

    return "", 0.0


def _keyword_matches_model_text(keyword: str, model_text: str) -> bool:
    kw = str(keyword or "").strip().upper()
    if not kw:
        return False
    text = str(model_text or "").strip().upper()
    if not text:
        return False

    # 型号关键词只在型号段前缀上匹配，避免 BASIC -> ASI 这类误命中。
    for segment in re.split(r"[^A-Z0-9]+", text):
        if not segment:
            continue
        if segment.startswith(kw):
            return True
        if segment.startswith(f"DHI{kw}") or segment.startswith(f"DH{kw}"):
            return True
    return False


def _collect_keyword_model_values(
    france_row: Optional[pd.Series],
    sys_row: Optional[pd.Series],
) -> List[str]:
    parts: List[str] = []
    if france_row is not None:
        for col in ("External Model", "Internal Model"):
            if col in france_row and pd.notna(france_row[col]):
                parts.append(str(france_row[col]))

    if sys_row is not None:
        for col in ("External Model", "Internal Model"):
            if col in sys_row and pd.notna(sys_row[col]):
                parts.append(str(sys_row[col]))

    return parts


def _pick_sys_keyword_uplift(
    *,
    category: str,
    price_group: str,
    effective_price_group: str,
    price_rule_key: Optional[str],
    series_display: str,
    france_row: Optional[pd.Series],
    sys_row: Optional[pd.Series],
) -> Tuple[List[str], float]:
    """
    关键词涨价（仅 Sys 反算 FOB 场景）：
    - 仅在 Internal Model / External Model 中做型号关键词匹配
    - 命中的 pct 做叠加（相加）
    """
    if not KEYWORD_UPLIFT_RULES:
        return [], 0.0

    model_values = _collect_keyword_model_values(france_row, sys_row)
    hits: List[str] = []
    total_pct = 0.0
    seen: Set[str] = set()

    for rule in KEYWORD_UPLIFT_RULES:
        if not isinstance(rule, dict):
            continue
        if rule.get("enabled", True) is False:
            continue

        kw = str(rule.get("keyword") or "").strip()
        pct = _to_float(rule.get("pct"))
        if not kw or pct is None or pct <= 0:
            continue

        kw_up = kw.upper()
        if not any(_keyword_matches_model_text(kw_up, model_text) for model_text in model_values):
            continue

        if kw_up in seen:
            continue
        seen.add(kw_up)
        total_pct += pct
        hits.append(kw)

    return hits, total_pct


def _to_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return None
        f = float(v)
        if math.isnan(f):
            return None
        return f
    except Exception:  # noqa: BLE001
        return None


def compute_ddp_a_from_fob(fob: Optional[float], category: str) -> Optional[float]:
    if fob is None or fob <= 0:
        return None
    rule = DDP_RULES.get(category)
    if not rule:
        return None
    ddp = fob
    for pct in rule:
        ddp *= (1 + pct)
    return ddp


def pick_price_rule_with_key(price_group: str, series_key: str) -> Tuple[Optional[Dict], Optional[str]]:
    """
    根据 price_group (大类) + series_key 在 PRICE_RULES 中选择一条规则，并返回“命中的 key”。

    返回:
      (rule_dict, matched_key)

    matched_key 含义：
      - 精确匹配到某个子规则：返回该子规则 key
      - 未命中任何子规则：若存在 _default_，返回 "_default_"
      - price_group 不存在：返回 (None, None)
    """
    cat_rule = PRICE_RULES.get(price_group)
    if not cat_rule:
        return None, None

    s_key_up = (series_key or "").strip().upper()
    if s_key_up:
        # 先尝试精确匹配（不区分大小写）
        for k, v in cat_rule.items():
            if k == "_default_":
                continue
            if str(k).strip().upper() == s_key_up:
                return v, k

        # 再尝试包含匹配
        for k, v in cat_rule.items():
            if k == "_default_":
                continue
            k_up = str(k).strip().upper()
            if k_up in s_key_up or s_key_up in k_up:
                return v, k

    # 默认规则
    if "_default_" in cat_rule:
        return cat_rule.get("_default_"), "_default_"

    return None, None


def compute_channel_prices(ddp_a: float, rule: Dict) -> Dict[str, Optional[float]]:
    """
    ddp_a → 各渠道价（reseller / gold / silver / ivory(installer) / msrp）
    返回不 round 的原始浮点数，显示格式在 formatter 里统一控制。
    """
    if ddp_a is None or rule is None:
        return {}

    def from_ddp(p):
        if p is None:
            return None
        return ddp_a / (1 - p)

    reseller = from_ddp(rule.get("reseller"))
    gold = from_ddp(rule.get("gold"))
    silver = from_ddp(rule.get("silver"))
    ivory = from_ddp(rule.get("ivory"))
    msrp_pct = rule.get("msrp_on_installer")

    if ivory is not None and msrp_pct is not None:
        msrp = ivory / (1 - msrp_pct)
    else:
        msrp = None

    # 有的品类 reseller 没有单独折扣，直接用 DDP A
    if reseller is None:
        reseller = ddp_a

    return {
        "DDP A(EUR)": ddp_a,
        "Suggested Reseller(EUR)": reseller,
        "Gold(EUR)": gold,
        "Silver(EUR)": silver,
        "Ivory(EUR)": ivory,
        "MSRP(EUR)": msrp,
    }


def _all_prices_present(fr_row: Optional[pd.Series]) -> bool:
    """
    只有 France 行存在且 7 个价格都非空时才认为“全部原始价格齐全”。
    """
    if fr_row is None:
        return False
    for col in PRICE_COLS:
        if col not in fr_row or _to_float(fr_row[col]) is None:
            return False
    return True


def _normalize_sales_type(v) -> str:
    """
    统一 Sales Type 输出为：
      - SMB / DISTRIBUTION / PROJECT / UNKNOWN
    """
    if v is None:
        return "UNKNOWN"
    try:
        if pd.isna(v):
            return "UNKNOWN"
    except Exception:  # noqa: BLE001
        pass
    s = str(v).strip().upper()
    if s in {"SMB", "DISTRIBUTION", "PROJECT"}:
        return s
    return "UNKNOWN"


def _norm_key(s: Optional[str]) -> str:
    if not s:
        return ""
    return str(s).strip().upper()


# =========================
# Pricing rule resolution
# =========================
# 目标：
# 1) 以 “Series/系列” 识别结果为主（series_key / series_display），优先决定使用哪个 PRICE_RULES 大类。
# 2) 若 Series 无法映射到 PRICE_RULES，再 fallback 到 price_group（产品线大类）。
# 3) 业务约束：EAS 与 “电子防盗门 / Electronic Anti-theft System” 使用同一套定价公式 -> 统一映射到 'EAS'。

_EAS_ALIASES: Tuple[str, ...] = (
    "EAS",
    "ELECTRONIC ANTI-THEFT SYSTEM",
    "ELECTRONIC ANTI-THEFT SYSTEM (EAS)",
    "ELECTRONIC ANTI-THEFT SYSTEM（EAS）",
    "电子防盗",
    "电子防盗门",
    "电子防盗系统",
)

_STRICT_PRICE_GROUP_POLICY: Dict[str, Dict[str, Any]] = {
    "ACCESS CONTROL": {"default": "ACCESS CONTROL", "allowed": {"ACCESS CONTROL"}},
    "ALARM": {"default": "Alarm", "allowed": {"Alarm"}},
    "WIFI相机": {"default": "WIFI相机", "allowed": {"WIFI相机"}},
    "DOORBELL": {"default": "Doorbell", "allowed": {"Doorbell"}},
}


def _norm_upper_text(v: Optional[str]) -> str:
    return str(v or "").strip().upper()


def strict_price_group_default(category: Optional[str]) -> Optional[str]:
    pol = _STRICT_PRICE_GROUP_POLICY.get(_norm_upper_text(category))
    if not pol:
        return None
    return str(pol.get("default") or "").strip() or None


def is_strict_price_group_compatible(category: Optional[str], price_group: Optional[str]) -> bool:
    pol = _STRICT_PRICE_GROUP_POLICY.get(_norm_upper_text(category))
    if not pol:
        return True
    allowed = {str(x).strip().upper() for x in (pol.get("allowed") or set())}
    return _norm_upper_text(price_group) in allowed


def _sanitize_price_group_for_category(
    category: Optional[str],
    price_group: Optional[str],
) -> Tuple[Optional[str], bool]:
    """
    对部分强约束产品线执行一致性修正，避免出现 category 与 price_group 跨线错配。
    返回 (sanitized_price_group, changed)
    """
    pg = str(price_group).strip() if price_group is not None else None
    if is_strict_price_group_compatible(category, pg):
        return pg, False
    default_pg = strict_price_group_default(category)
    if default_pg:
        return default_pg, True
    return pg, False


def _series_implies_eas(series_key: str, series_display: str) -> bool:
    sk = _norm_key(series_key)
    sd = _norm_key(series_display)

    # 直接包含 EAS
    if "EAS" in sk or "EAS" in sd:
        return True

    # 电子防盗关键词（中文）
    if "电子防盗" in sk or "电子防盗" in sd:
        return True

    # 英文 Anti-theft
    if "ANTI-THEFT" in sk or "ANTI-THEFT" in sd:
        return True

    # 兜底：别名表
    for a in _EAS_ALIASES:
        au = _norm_key(a)
        if au and (au == sk or au == sd or au in sk or au in sd):
            return True

    return False


def resolve_price_group_for_rules(price_group: str, series_key: str, series_display: str) -> str:
    """
    返回用于 PRICE_RULES 的大类 key（effective_price_group）。

    规则：
    - 如果 series 指向 EAS（含“电子防盗门”等），则强制用 'EAS'
      （并保证 EAS 与电子防盗门共用同一套公式）
    - 否则若 series_key 本身就是 PRICE_RULES 的 top-level key，优先用 series_key
    - 否则若 price_group 在 PRICE_RULES，使用 price_group
    - 否则：返回 price_group（后续会 NOTFOUND）
    """
    pg = (price_group or "").strip()
    sk = (series_key or "").strip()

    if _series_implies_eas(sk, series_display):
        return "EAS"

    if sk and sk in PRICE_RULES:
        return sk

    if pg and pg in PRICE_RULES:
        return pg

    return pg


def _choose_sys_base_price_from_sys(sys_row: pd.Series) -> Tuple[Optional[float], str, Optional[str]]:
    """
    新规则（无交互）：
      - Sales Type in {DISTRIBUTION, SMB} -> Min Price
      - Sales Type == PROJECT            -> Area Price
      - 其他/缺失                         -> None
    返回：(base_price, sales_type_norm, basis_field_name)
    """
    sales = _normalize_sales_type(sys_row.get("Sales Type"))

    min_price = _to_float(sys_row.get("Min Price"))
    area_price = _to_float(sys_row.get("Area Price"))

    if sales in {"SMB", "DISTRIBUTION"}:
        return min_price, sales, "Min Price"
    if sales == "PROJECT":
        return area_price, sales, "Area Price"

    return None, sales, None


def _fallback_recorder_category(fr_row: Optional[pd.Series], sys_row: Optional[pd.Series]) -> Tuple[str, Optional[str]]:
    """
    pricing_engine 级别的兜底：
    当 mapping / classifier 仍返回 UNKNOWN 时，基于型号/字段文本强制识别录像机大类，
    避免 UNKNOWN 直接中断自动定价（你当前这批 NVR4 就是这种症状）。

    返回 (category, price_group_hint)
    """
    parts = []

    if fr_row is not None:
        for col in ("Internal Model", "External Model", "Series", "系列", "Description", "Second Product Line"):
            if col in fr_row and pd.notna(fr_row[col]):
                parts.append(str(fr_row[col]))

    if sys_row is not None:
        for col in ("Internal Model", "External Model", "Second Product Line", "Catelog Name", "First Product Line"):
            if col in sys_row and pd.notna(sys_row[col]):
                parts.append(str(sys_row[col]))

    big = " ".join(parts).upper()

    # 优先：更“专名”的类
    if "IVSS" in big:
        return "IVSS", "IVSS"
    if re.search(r"\bEVS\b", big) or "EVS" in big:
        return "EVS", "EVS"
    if re.search(r"\bXVR\b", big) or "XVR" in big:
        return "XVR", "XVR"

    # NVR（覆盖 NVR4104HS / NVR4216 / NVR5xxx / NVR6xxx 等）
    if re.search(r"\bNVR\b", big) or re.search(r"\bNVR[0-9]", big):
        return "NVR", "NVR"

    return "UNKNOWN", None


def build_original_values(
    fr_row: Optional[pd.Series],
    sys_row: Optional[pd.Series],
) -> Dict[str, object]:
    """
    构造基础输出字段：
      - 优先用 France
      - 当 France 行不存在时，从 Sys 填充基础信息字段
    价格列一律从 France 拿（France 没有则先置 None，后面统一通过计算补）
    """
    data: Dict[str, object] = {}

    # Part No.
    if fr_row is not None and "Part No." in fr_row:
        data["Part No."] = fr_row.get("Part No.")
    elif sys_row is not None and "Part Num" in sys_row:
        data["Part No."] = sys_row.get("Part Num")
    else:
        data["Part No."] = None

    # Series（展示用兜底；更精细在 detect_series 里）
    if fr_row is not None:
        data["Series"] = fr_row.get("Series") or fr_row.get("系列")
    elif sys_row is not None:
        data["Series"] = sys_row.get("Second Product Line") or sys_row.get("Catelog Name")
    else:
        data["Series"] = None

    # 模型 / 状态 / 描述
    if fr_row is not None:
        data["External Model"] = fr_row.get("External Model")
        data["Internal Model"] = fr_row.get("Internal Model")
        data["Sales Status"] = fr_row.get("Sales Status")
        data["Description"] = fr_row.get("Description")
    elif sys_row is not None:
        data["External Model"] = sys_row.get("External Model")
        data["Internal Model"] = sys_row.get("Internal Model")
        data["Sales Status"] = sys_row.get("Release Status")
        data["Description"] = None
    else:
        data["External Model"] = None
        data["Internal Model"] = None
        data["Sales Status"] = None
        data["Description"] = None

    # 价格列：只看 France（不改你原逻辑）
    if fr_row is not None:
        for col in PRICE_COLS:
            data[col] = fr_row.get(col)
    else:
        for col in PRICE_COLS:
            data[col] = None

    return data


def compute_prices_for_part(
    part_no: str,
    france_row: Optional[pd.Series],
    sys_row: Optional[pd.Series],
    france_map: pd.DataFrame,
    sys_map: pd.DataFrame,
    force_category: Optional[str] = None,
    force_price_group: Optional[str] = None,
    force_series_key: Optional[str] = None,
    force_full_recalc: bool = False,
) -> Dict:
    """
    输出 result dict：
      - final_values / calculated_fields / category / price_group / series_display / series_key / pricing_rule_name
      - sys_sales_type: 本 PN 在 Sys 中的 Sales Type（规范化后的）
      - sys_basis_field: 本次 Sys FOB 计算若发生，用的是哪列（Min Price / Area Price）
      - sys_basis_price: 该层级在 Sys 表对应底价
      - sys_basis_price_used: 本次是否实际用于反算 FOB（仅 France FOB 缺失时）
      - sys_uplift_key: 本次 Sys FOB uplift 命中的 key（若未命中则 None）
      - sys_keyword_uplift_pct / sys_keyword_uplift_hits: 关键词叠加涨价命中信息
    """
    # 1) 产品线 & 价格组
    category, price_group = classify_category_and_price_group(
        france_row, sys_row, france_map, sys_map
    )

    # 1.5) 兜底：如果 mapping/classifier 仍然返回 UNKNOWN，则强制按型号识别录像机大类（NVR/IVSS/EVS/XVR）
    # 目的：避免 UNKNOWN 直接触发 PRICE_RULES:NOTFOUND，导致整条链路不算价
    if (category is None) or (category == "UNKNOWN"):
        fb_cat, fb_pg = _fallback_recorder_category(france_row, sys_row)
        if fb_cat != "UNKNOWN":
            category, price_group = fb_cat, (fb_pg or fb_cat)

    if force_category:
        category = str(force_category).strip()
    if force_price_group:
        price_group = str(force_price_group).strip()

    if not price_group:
        price_group = category

    # 自动分类时，强约束类目禁止跨线 price_group（例如 ACCESS CONTROL 被误配到 WIFI相机）
    if not force_price_group:
        price_group, _ = _sanitize_price_group_for_category(category, price_group)

    # 2) Series（展示 + 给 PRICE_RULES 选子规则用）
    series_display, series_key = detect_series(france_row, sys_row, price_group)
    if force_series_key:
        series_key = str(force_series_key).strip()

    # 3) 原始值（France 优先，France 不存在则从 Sys 补基础字段）
    final_values = build_original_values(france_row, sys_row)
    calculated_fields: Set[str] = set()

    # ===== Sys 侧：提前解析 Sales Type（仅用于打印/记录，不改变 France 优先级）=====
    sys_sales_type = "UNKNOWN"
    sys_basis_field: Optional[str] = None
    sys_basis_price: Optional[float] = None
    if sys_row is not None:
        sys_basis_price, sys_sales_type, sys_basis_field = _choose_sys_base_price_from_sys(sys_row)

    # ===== 预计算：本次会使用的 PRICE_RULES 规则名字（即使最终不需要补全渠道价，也可输出供核对）=====
    effective_price_group = resolve_price_group_for_rules(price_group, series_key, series_display)
    price_rule_dict, price_rule_key = pick_price_rule_with_key(effective_price_group, series_key)
    if price_rule_key is None:
        pricing_rule_name = "PRICE_RULES:NOTFOUND"
    else:
        pricing_rule_name = f"PRICE_RULES['{effective_price_group}']['{price_rule_key}']"

    # 3.5) 无法识别产品线 → 不做任何自动计算
    if category is None or category == "UNKNOWN":
        # server 端不强依赖 print，这里保留语义但不影响 API
        return {
            "final_values": final_values,
            "calculated_fields": calculated_fields,
            "category": category or "UNKNOWN",
            "price_group": price_group,
            "series_display": series_display,
            "series_key": series_key or "",
            "pricing_rule_name": pricing_rule_name,
            "auto_success": False,
            "used_sys": False,
            "sys_sales_type": sys_sales_type,
            "sys_basis_field": None,
            "sys_basis_price": sys_basis_price,
            "sys_basis_price_used": None,
            "sys_uplift_key": None,
            "sys_keyword_uplift_pct": 0.0,
            "sys_keyword_uplift_hits": [],
        }

    # 4) 如果 France 所有价格都齐全 → 全部 Original（不计算）
    if _all_prices_present(france_row) and not force_full_recalc:
        return {
            "final_values": final_values,
            "calculated_fields": calculated_fields,
            "category": category,
            "price_group": price_group,
            "series_display": series_display,
            "series_key": series_key or "",
            "pricing_rule_name": pricing_rule_name,
            "auto_success": True,
            "used_sys": False,
            "sys_sales_type": sys_sales_type,
            "sys_basis_field": None,
            "sys_basis_price": sys_basis_price,
            "sys_basis_price_used": None,
            "sys_uplift_key": None,
            "sys_keyword_uplift_pct": 0.0,
            "sys_keyword_uplift_hits": [],
        }

    # 5) 需要补全：先找 FOB
    fob = _to_float(final_values.get("FOB C(EUR)"))

    used_sys = False
    used_sys_basis_field: Optional[str] = None
    used_sys_basis_price: Optional[float] = None
    used_sys_uplift_key: Optional[str] = None
    used_sys_keyword_uplift_pct: float = 0.0
    used_sys_keyword_uplift_hits: List[str] = []

    # 只在 France 缺失 FOB 时，才允许从 Sys 计算 FOB（不改你原逻辑）
    if (fob is None or fob <= 0) and sys_row is not None:
        base_price, sales_norm, basis_field = _choose_sys_base_price_from_sys(sys_row)
        sys_sales_type = sales_norm
        sys_basis_price = base_price
        if base_price is not None and base_price > 0:
            fob = base_price * 0.9

            # Sys FOB uplift：仅当 France FOB 缺失且使用 Sys 时生效
            uplift_key, uplift_pct = _pick_sys_uplift(
                category=category,
                price_group=price_group,
                effective_price_group=effective_price_group,
                price_rule_key=price_rule_key,
                series_display=series_display,
                france_row=france_row,
                sys_row=sys_row,
            )
            if uplift_pct and uplift_pct > 0:
                fob = fob * (1 + uplift_pct)
                used_sys_uplift_key = uplift_key

            kw_hits, kw_pct = _pick_sys_keyword_uplift(
                category=category,
                price_group=price_group,
                effective_price_group=effective_price_group,
                price_rule_key=price_rule_key,
                series_display=series_display,
                france_row=france_row,
                sys_row=sys_row,
            )
            if kw_pct > 0:
                fob = fob * (1 + kw_pct)
                used_sys_keyword_uplift_pct = kw_pct
                used_sys_keyword_uplift_hits = kw_hits

            final_values["FOB C(EUR)"] = fob
            calculated_fields.add("FOB C(EUR)")
            used_sys = True
            used_sys_basis_field = basis_field  # Min Price / Area Price
            used_sys_basis_price = base_price

    # 6) DDP A：如果 France 没写，就用 FOB + DDP_RULES 算
    ddp_existing = _to_float(final_values.get("DDP A(EUR)"))
    if force_full_recalc:
        ddp_a = compute_ddp_a_from_fob(fob, category)
        if ddp_a is not None:
            final_values["DDP A(EUR)"] = ddp_a
            calculated_fields.add("DDP A(EUR)")
    elif ddp_existing is not None and ddp_existing > 0:
        ddp_a = ddp_existing
    else:
        ddp_a = compute_ddp_a_from_fob(fob, category)
        if ddp_a is not None:
            final_values["DDP A(EUR)"] = ddp_a
            calculated_fields.add("DDP A(EUR)")

    # 7) 渠道价：如果某列缺失且有 DDP + 价格组规则，就计算补全
    if ddp_a is not None:
        if price_rule_dict is not None:
            channel_prices = compute_channel_prices(ddp_a, price_rule_dict)
            for col in PRICE_COLS:
                if col == "FOB C(EUR)":
                    continue
                if col not in channel_prices:
                    continue
                if force_full_recalc:
                    if channel_prices[col] is not None:
                        final_values[col] = channel_prices[col]
                        calculated_fields.add(col)
                    continue
                if _to_float(final_values.get(col)) is None:
                    final_values[col] = channel_prices[col]
                    calculated_fields.add(col)

    return {
        "final_values": final_values,
        "calculated_fields": calculated_fields,
        "category": category,
        "price_group": price_group,
        "series_display": series_display,
        "series_key": series_key or "",
        "pricing_rule_name": pricing_rule_name,
        "auto_success": True,
        "used_sys": used_sys,
        "sys_sales_type": sys_sales_type,
        "sys_basis_field": used_sys_basis_field,
        "sys_basis_price": sys_basis_price,
        "sys_basis_price_used": used_sys_basis_price,
        "sys_uplift_key": used_sys_uplift_key,
        "sys_keyword_uplift_pct": used_sys_keyword_uplift_pct,
        "sys_keyword_uplift_hits": used_sys_keyword_uplift_hits,
    }


# ======================================================================
# Server-side matching helpers (exact/base + base fallback fill)
# ======================================================================

def _pick_pn_col(df: pd.DataFrame) -> str:
    """
    在不同表里 PN 列名可能不同：
      - France: Part No.
      - Sys:   Part Num / Part No.
    这里做一个尽量稳的选择。
    """
    candidates = [
        "Part No.",
        "Part No",
        "PartNum",
        "Part Num",
        "PN",
        "P/N",
        "PartNumber",
        "Part Number",
    ]
    cols = list(df.columns)
    # 先精确命中
    for c in candidates:
        if c in cols:
            return c
    # 再做不区分大小写的近似
    low = {str(c).strip().lower(): c for c in cols}
    for c in candidates:
        k = c.strip().lower()
        if k in low:
            return low[k]
    # 兜底：第一列
    return cols[0]


def _find_row_with_fallback(
    df: pd.DataFrame,
    idx_raw: Dict[str, int],
    idx_base: Dict[str, int],
    key_raw: str,
    key_base: str,
) -> Tuple[Optional[pd.Series], str, Optional[str]]:
    """
    返回 (row, mode, matched_pn)
      mode: exact | base | none
    """
    if key_raw and key_raw in idx_raw:
        i = idx_raw[key_raw]
        try:
            row = df.iloc[int(i)]
            # matched_pn：尽量用表中 PN 列
            pn_col = _pick_pn_col(df)
            matched = str(row.get(pn_col)) if pn_col in row else key_raw
            return row, "exact", matched
        except Exception:
            return None, "none", None

    if key_base and key_base in idx_base:
        i = idx_base[key_base]
        try:
            row = df.iloc[int(i)]
            pn_col = _pick_pn_col(df)
            matched = str(row.get(pn_col)) if pn_col in row else key_base
            return row, "base", matched
        except Exception:
            return None, "none", None

    return None, "none", None


def _fill_missing_prices_from_base(
    df: pd.DataFrame,
    row: Optional[pd.Series],
    base_key_raw: str,
) -> Tuple[Optional[pd.Series], bool, Optional[str]]:
    """
    当输入 PN 带 -xxxx 后缀导致 exact 行缺价时，
    用 base PN 的行把缺失的价格列补齐（只补 PRICE_COLS 中缺失者）。
    返回 (patched_row, changed, fallback_pn)
    """
    if row is None:
        return row, False, None
    if not base_key_raw:
        return row, False, None

    pn_col = _pick_pn_col(df)
    try:
        series_pn = df[pn_col].astype(str).str.strip().str.lower()
    except Exception:
        return row, False, None

    hits = df[series_pn == str(base_key_raw).strip().lower()]
    if hits.empty:
        return row, False, None

    base_row = hits.iloc[0]

    patched = row.copy()
    changed = False
    for c in PRICE_COLS:
        if _to_float(patched.get(c)) is None:
            v = base_row.get(c)
            if _to_float(v) is not None:
                patched[c] = v
                changed = True

    fb_pn = str(base_row.get(pn_col)) if pn_col in base_row else base_key_raw
    return patched, changed, fb_pn


# ======================================================================
# Public server API functions
# ======================================================================
def compute_one(
    data: DataBundle,
    pn: str,
    force_category: Optional[str] = None,
    force_price_group: Optional[str] = None,
    force_series_key: Optional[str] = None,
    force_full_recalc: bool = False,
) -> Dict[str, Any]:
    """
    server API：单个 PN 查询
    """
    key_raw = normalize_pn_raw(pn)
    key_base = normalize_pn_base(pn)

    fr_row, fr_mode, fr_matched = _find_row_with_fallback(
        data.france_df, data.fr_idx_raw, data.fr_idx_base, key_raw, key_base
    )
    sys_row, sys_mode, sys_matched = _find_row_with_fallback(
        data.sys_df, data.sys_idx_raw, data.sys_idx_base, key_raw, key_base
    )

    warnings: List[str] = []

    # 去后缀补价：仅当 base_key != raw_key（说明发生了可截断）
    used_fb = False
    fb_from: Optional[str] = None
    used_fr_fb = False
    used_sys_fb = False
    fr_fb_pn: Optional[str] = None
    sys_fb_pn: Optional[str] = None

    if key_base and key_base != key_raw:
        fr_row, used_fr_fb, fr_fb_pn = _fill_missing_prices_from_base(
            data.france_df, fr_row, key_base
        )
        sys_row, used_sys_fb, sys_fb_pn = _fill_missing_prices_from_base(
            data.sys_df, sys_row, key_base
        )

        used_fb = bool(used_fr_fb or used_sys_fb)
        if used_fb:
            fb_from = str(fr_fb_pn or sys_fb_pn or key_base)
            warnings.append(f"price_fallback_from_base_pn={fb_from}")

    if fr_row is None and sys_row is None:
        return {
            "pn": pn,
            "status": "not_found",
            "final_values": {},
            "calculated_fields": [],
            "warnings": warnings,
        }

    force_category_norm = str(force_category).strip() if force_category else None
    force_price_group_norm = str(force_price_group).strip() if force_price_group else None
    force_series_key_norm = str(force_series_key).strip() if force_series_key else None

    result = compute_prices_for_part(
        pn,
        fr_row,
        sys_row,
        data.map_fr,
        data.map_sys,
        force_category=force_category_norm,
        force_price_group=force_price_group_norm,
        force_series_key=force_series_key_norm,
        force_full_recalc=bool(force_full_recalc),
    )
    result["final_values"]["Part No."] = pn  # 强制覆盖为用户输入

    # Black 型号提醒（不影响计算，只做可追溯 warning）
    internal = result.get("final_values", {}).get("Internal Model")
    if internal is not None:
        s = str(internal)
        if "black" in s.lower():
            warnings.append("internal_model_contains_black_check_white_variant")

    return {
        "pn": pn,
        "status": "ok",
        "final_values": result["final_values"],
        "calculated_fields": sorted(list(result.get("calculated_fields") or [])),
        "meta": {
            "category": result.get("category"),
            "price_group": result.get("price_group"),
            "series_display": result.get("series_display"),
            "series_key": result.get("series_key"),
            "pricing_rule_name": result.get("pricing_rule_name"),
            "used_sys": result.get("used_sys"),
            "sys_sales_type": result.get("sys_sales_type"),
            "sys_basis_field": result.get("sys_basis_field"),
            "sys_basis_price": result.get("sys_basis_price"),
            "sys_basis_price_used": result.get("sys_basis_price_used"),
            "sys_uplift_key": result.get("sys_uplift_key"),
            "sys_keyword_uplift_pct": result.get("sys_keyword_uplift_pct"),
            "sys_keyword_uplift_hits": result.get("sys_keyword_uplift_hits"),
            "fr_match_mode": fr_mode,
            "sys_match_mode": sys_mode,
            "fr_matched_pn": fr_matched,
            "sys_matched_pn": sys_matched,

            # 新增：fallback 可追溯信息（对并发/审计很关键）
            "used_price_fallback": used_fb,
            "fallback_base_pn": fb_from,
            "used_fr_fallback": bool(used_fr_fb),
            "used_sys_fallback": bool(used_sys_fb),
            "manual_override": bool(force_category_norm or force_price_group_norm),
            "forced_category": force_category_norm,
            "forced_price_group": force_price_group_norm,
            "forced_series_key": force_series_key_norm,
            "force_full_recalc": bool(force_full_recalc),
        },
        "warnings": warnings,
    }


def compute_many(data: DataBundle, pns: List[str], level: str) -> List[Dict[str, Any]]:
    """
    batch：按输入 PN 顺序返回
    level: country | country_customer（此处仅透传给导出层；计算逻辑不依赖 level）
    """
    _ = level
    out: List[Dict[str, Any]] = []
    for pn in pns:
        s = str(pn).strip()
        if not s:
            continue
        out.append(compute_one(data, s))
    return out
