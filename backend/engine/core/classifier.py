# core/classifier.py
import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

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


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:  # noqa: BLE001
        pass
    text = str(value).strip()
    return text or None


@dataclass(frozen=True)
class MappingRuleMatch:
    """One auditable decision-table match.

    The pricing path still consumes the first match.  The verifier consumes
    every match so a broad, early rule cannot hide a conflicting stronger
    signal later in the table.
    """

    rule_id: str
    rule_index: str
    priority: Optional[float]
    category: str
    price_group_hint: Optional[str]
    specificity: int
    conditions: Tuple[Dict[str, str], ...]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _normalized_priority(value: Any) -> Optional[float]:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def collect_mapping_matches(
    row: pd.Series,
    mapping: pd.DataFrame,
    *,
    limit: Optional[int] = None,
) -> List[MappingRuleMatch]:
    """Return all matching decision-table rows with their provenance."""
    if mapping is None or mapping.empty:
        return []

    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")

    if "priority" in mapping.columns:
        iter_rules = mapping.sort_values("priority", ascending=True).iterrows()
    else:
        iter_rules = mapping.iterrows()

    matches: List[MappingRuleMatch] = []
    for index, rule in iter_rules:
        conditions: List[Dict[str, str]] = []
        specificity = 0
        matched = True
        for suffix in ("1", "2"):
            field = _normalize_field_name(rule.get(f"field{suffix}"))
            if not field:
                if suffix == "1":
                    matched = False
                break
            match_type = str(rule.get(f"match_type{suffix}") or "").strip().lower()
            pattern = safe_upper(rule.get(f"pattern{suffix}"))
            actual = safe_upper(row.get(field))
            if not pattern or match_type not in {"equals", "contains"}:
                matched = False
                break
            condition_matched = actual == pattern if match_type == "equals" else pattern in actual
            if not condition_matched:
                matched = False
                break
            specificity += 2 if match_type == "equals" else 1
            conditions.append(
                {
                    "field": field,
                    "match_type": match_type,
                    "pattern": pattern,
                    "actual": actual,
                }
            )

        if not matched:
            continue

        category = _optional_text(rule.get("category")) or "UNKNOWN"
        price_group_hint = _optional_text(rule.get("price_group_hint"))
        rule_id = _optional_text(rule.get("rule_id")) or f"mapping-row:{index}"
        matches.append(
            MappingRuleMatch(
                rule_id=rule_id,
                rule_index=str(index),
                priority=_normalized_priority(rule.get("priority")),
                category=category,
                price_group_hint=price_group_hint,
                specificity=specificity,
                conditions=tuple(conditions),
            )
        )
        if limit is not None and len(matches) >= limit:
            break
    return matches


def apply_mapping(row: pd.Series, mapping: pd.DataFrame) -> Tuple[str, Optional[str]]:
    """
    通用映射逻辑：
      - 按 priority 从小到大匹配
      - 支持 equals / contains 两种模式
      - 返回 (category, price_group_hint)
    """
    matches = collect_mapping_matches(row, mapping, limit=1)
    if not matches:
        return "UNKNOWN", None
    first = matches[0]
    return first.category, first.price_group_hint


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


_TURNSTILE_TOKENS = (
    "PEDESTRIAN TURNSTILE",
    "TURNSTILE",
    "人行道闸",
    "翼闸",
    "摆闸",
    "三辊闸",
    "速通门",
    "闸机",
)


def _is_turnstile_text(v) -> bool:
    s = safe_upper(v)
    if not s:
        return False
    return any(tok in s for tok in _TURNSTILE_TOKENS)


def _is_ptz_project_camera(
    france_row: Optional[pd.Series],
    sys_row: Optional[pd.Series],
) -> bool:
    fields = []
    if france_row is not None:
        for col in (
            "Series",
            "Second Level Product Category",
            "Description",
            "External Model",
            "Internal Model",
        ):
            if col in france_row and pd.notna(france_row[col]):
                fields.append(france_row[col])

    if sys_row is not None:
        for col in (
            "First Product Line",
            "Second Product Line",
            "Catelog Name",
            "External Model",
            "Internal Model",
        ):
            if col in sys_row and pd.notna(sys_row[col]):
                fields.append(sys_row[col])

    big = safe_upper(" ".join(str(x) for x in fields))
    if "PTZ CAMERAS FOR OVERSEAS PROJECT" not in big and "POSITIONING SYSTEMS" not in big:
        return False

    return bool(re.search(r"\b(?:DHI|DH)\s*-\s*PTZ", big) or re.search(r"\bPTZ[0-9]", big))


def _is_security_inspection_text(v) -> bool:
    s = safe_upper(v)
    if not s:
        return False
    return any(
        tok in s
        for tok in (
            "DAHUA ISCAN",
            "SECURITY INSPECTION EQUIPMENT",
            "BAGGAGE INSPECTION",
            "LUGGAGE AND PARCEL",
            "PEOPLE SCREENING",
            "ANTI-TERRORIST AND EXPLOSION-PROOF",
        )
    )


def _collect_model_texts(
    france_row: Optional[pd.Series],
    sys_row: Optional[pd.Series],
) -> list[str]:
    vals: list[str] = []
    for row in (france_row, sys_row):
        if row is None:
            continue
        for col in ("Internal Model", "External Model"):
            if col in row and pd.notna(row[col]):
                s = str(row[col]).strip()
                if s:
                    vals.append(s)
    return vals


def _model_blob(france_row: Optional[pd.Series], sys_row: Optional[pd.Series]) -> str:
    vals = _collect_model_texts(france_row, sys_row)
    if not vals:
        return ""
    return safe_upper(" ".join(vals))


def _model_evidence_override(
    france_row: Optional[pd.Series],
    sys_row: Optional[pd.Series],
) -> Optional[Tuple[str, str]]:
    """
    Strong model-prefix evidence learned from the current runtime data.

    The mapping CSVs are product-line based and can miss newer lines such as
    "Cameras for Overseas Distribution Channels". When model text is explicit,
    prefer it over broad learned fallbacks.
    """
    big = _model_blob(france_row, sys_row)
    if not big:
        return None

    # Recorders first: avoid treating XVR/NVR strings as generic camera text.
    if re.search(r"\b(?:DHI|DH)?-?IVSS", big) or re.search(r"\bIVSS[0-9]", big):
        return ("IVSS", "IVSS")
    if re.search(r"\b(?:DHI|DH)?-?EVS", big) or re.search(r"\bEVS[0-9]", big):
        return ("EVS", "EVS")
    if re.search(r"\b(?:DHI|DH)?-?XVR", big) or re.search(r"\bXVR[0-9]", big):
        return ("XVR", "XVR")
    if re.search(r"\b(?:DHI|DH)?-?NVR", big) or re.search(r"\bNVR[0-9]", big):
        return ("NVR", "NVR")

    if re.search(r"\b(?:DHI|DH)?-?TPC", big) or re.search(r"\bTPC[-0-9A-Z]", big):
        return ("THERMAL", "THERMAL")

    if (
        re.search(r"\b(?:DHI|DH)?-?PTZ", big)
        or re.search(r"\bPTZ[0-9]", big)
        or re.search(r"\b(?:DHI|DH)?-?SD[0-9]", big)
        or re.search(r"\bSD[0-9]", big)
    ):
        return ("PTZ", "PTZ")

    if re.search(r"\b(?:DHI|DH)-?IPC\b", big) or re.search(r"\bIPC[-A-Z0-9]", big):
        return ("IPC", "IPC")
    if re.search(r"\b(?:DHI|DH)-?(?:HFW|HDBW|HDW|HDB)[0-9]", big):
        return ("IPC", "IPC")
    if re.search(r"\b(?:HFW|HDBW|HDW|HDB)[0-9]", big):
        return ("IPC", "IPC")

    if re.search(r"\b(?:DHI|DH)?-?HAC[-A-Z0-9]", big) or re.search(r"\bHAC[-A-Z0-9]", big):
        return ("HAC", "HAC")

    return None


def detect_strong_model_evidence(
    france_row: Optional[pd.Series],
    sys_row: Optional[pd.Series],
) -> Optional[Tuple[str, str]]:
    """Expose strong, deterministic model evidence to independent verifiers."""
    return _model_evidence_override(france_row, sys_row)


def _forced_category_override(
    france_row: Optional[pd.Series],
    sys_row: Optional[pd.Series],
) -> Optional[Tuple[str, str]]:
    """
    高优先级业务修正：
    - 人行道闸（Pedestrian Turnstile）统一按 ACCESS CONTROL 处理
    """
    if sys_row is not None:
        first_line = safe_upper(sys_row.get("First Product Line"))
        second_line = safe_upper(sys_row.get("Second Product Line"))
        if first_line == "INTELLIGENT BUILDING" and second_line == "PEDESTRIAN TURNSTILE":
            return ("ACCESS CONTROL", "ACCESS CONTROL")

    fields = []
    if france_row is not None:
        for col in (
            "Product Line",
            "Product Line(CN)",
            "First Level Product Category",
            "Second Level Product Category",
            "Series",
            "Description",
            "Product Name",
            "Product Name(CN)",
            "External Model",
            "Internal Model",
        ):
            if col in france_row and pd.notna(france_row[col]):
                fields.append(france_row[col])

    if sys_row is not None:
        for col in (
            "First Product Line",
            "Second Product Line",
            "Catelog Name",
            "Series",
            "Product Name",
            "Product Name(CN)",
            "External Model",
            "Internal Model",
        ):
            if col in sys_row and pd.notna(sys_row[col]):
                fields.append(sys_row[col])

    if any(_is_turnstile_text(v) for v in fields):
        return ("ACCESS CONTROL", "ACCESS CONTROL")

    if _is_ptz_project_camera(france_row, sys_row):
        return ("PTZ", "PTZ")

    if any(_is_security_inspection_text(v) for v in fields):
        return ("安检机", "安检机")

    model_override = _model_evidence_override(france_row, sys_row)
    if model_override is not None:
        return model_override

    return None


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

    m = re.search(r"H(?:DBW|DB|DW|FW)([0-9])", big)
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
    forced = _forced_category_override(france_row, sys_row)
    if forced is not None:
        return forced

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
