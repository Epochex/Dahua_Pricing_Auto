import math
from typing import Dict, Optional, Set, Tuple

import pandas as pd

from .classifier import detect_series, classify_category_and_price_group
from .pricing_rules import DDP_RULES, PRICE_RULES


PRICE_COLS = [
    "FOB C(EUR)",
    "DDP A(EUR)",
    "Suggested Reseller(EUR)",
    "Gold(EUR)",
    "Silver(EUR)",
    "Ivory(EUR)",
    "MSRP(EUR)",
]


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


def pick_price_rule(price_group: str, series_key: str) -> Optional[Dict]:
    """
    根据 price_group (大类) + series_key 在 PRICE_RULES 中选择一条规则。
    """
    cat_rule = PRICE_RULES.get(price_group)
    if not cat_rule:
        return None

    s_key_up = series_key.strip().upper()
    if s_key_up:
        # 先尝试精确匹配（不区分大小写）
        for k, v in cat_rule.items():
            if k == "_default_":
                continue
            if k.upper() == s_key_up:
                return v
        # 再尝试包含匹配
        for k, v in cat_rule.items():
            if k == "_default_":
                continue
            if k.upper() in s_key_up or s_key_up in k.upper():
                return v

    return cat_rule.get("_default_")


def compute_channel_prices(ddp_a: float, rule: Dict) -> Dict[str, Optional[float]]:
    """
    ddp_a → 各渠道价（reseller / gold / silver / ivory(installer) / msrp）

    注意：
    - 这里只返回“原始浮点值”，不做任何 round；
    - 最终展示时统一在 formatter 里做格式化。
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

    # 不 round，保持全精度，后面 formatter 再统一控制显示位数
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


def _choose_sys_base_price(part_no: str, sales_type: str, sys_row: pd.Series) -> Optional[float]:
    """
    根据 Sales Type 或交互式选择，从 Sys 的价格列中选一个作为 FOB 计算基准。

    - 若 sales_type 为 SMB / DISTRIBUTION → 使用 Min Price
    - 若 sales_type 为 PROJECT → 使用 Area Price
    - 其他情况（包括 France 行缺失 / 没有 Sales Type）：
        进入交互模式，让用户在 Min / Area / Standard 里选一个。
    """
    sales_up = (sales_type or "").strip().upper()

    min_price = _to_float(sys_row.get("Min Price"))
    area_price = _to_float(sys_row.get("Area Price"))
    std_price = _to_float(sys_row.get("Standard Price"))

    # 1) 有明确 Sales Type 的情况，保持原有逻辑
    if sales_up in {"SMB", "DISTRIBUTION"}:
        return min_price
    if sales_up == "PROJECT":
        return area_price

    # 2) 无 Sales Type 或无法识别 → 交互选择
    #    注意：这里可能在批量模式下被调用，会逐条询问。
    while True:
        print("\n[Sys 定价基准选择] 需要从 Sys 表价格中选择 FOB 计算基准：")
        print(f"  PN = {part_no}")
        # print(f"  Min Price      = {min_price if min_price is not None else 'N/A'}")
        # print(f"  Area Price     = {area_price if area_price is not None else 'N/A'}")
        # print(f"  Standard Price = {std_price if std_price is not None else 'N/A'}")
        print(f"  1 = Min Price | FOB L      = {'可用' if min_price is not None else 'N/A'}")
        print(f"  2 = Area Price | FOB N     = {'可用' if area_price is not None else 'N/A'}")
        print(f"  3 = Standard Price | FOB S = {'可用' if std_price is not None else 'N/A'}")
        choice = input("请选择 Sys 价格基准 (1 = Min|FOB L, 2 = Area|FOB N, 3 = Standard|FOB S, q=跳过本 PN)：").strip().lower()

        mapping = {
            "1": min_price,
            "2": area_price,
            "3": std_price,
        }

        if choice in {"q", "quit", "exit"}:
            return None

        if choice not in mapping:
            print("输入无效，请重试。")
            continue

        selected_price = mapping[choice]

        # 若价格不存在则提示
        if selected_price is None:
            print("[Warning] 该 FOB 价格为 N/A，不可选择，请重新选择。")
            continue

        return selected_price


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

    # Series（展示用，这里只做简单兜底；更精细的在 classifier.detect_series 里）
    if fr_row is not None:
        data["Series"] = fr_row.get("Series") or fr_row.get("系列")
    elif sys_row is not None:
        # 没有 France 时，拿 Sys 的 Second Product Line 做兜底
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

    # 价格列：只看 France
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
) -> Dict:
    """
    核心：给定 PN 对应的 France 行 / Sys 行，输出：
      - final_values: 各字段最终值
      - calculated_fields: 被计算补全的字段名集合
      - category: 产品线（DDP_RULES 的 key）
      - price_group: 价格组（PRICE_RULES 顶层 key）
      - series_display: 展示用 series
      - auto_success: 是否完成了自动识别
      - used_sys: 是否用到了 Sys 做价格计算
    """
    # 1) 产品线 & 价格组
    category, price_group = classify_category_and_price_group(
        france_row, sys_row, france_map, sys_map
    )
    if not price_group:
        price_group = category

    # 2) Series（展示 + 给 PRICE_RULES 选子规则用）
    series_display, series_key = detect_series(france_row, sys_row, price_group)

    # 3) 原始值（France 优先，France 不存在则从 Sys 补基础字段）
    final_values = build_original_values(france_row, sys_row)
    calculated_fields: Set[str] = set()

    # 3.5) 无法识别产品线 → 不做任何自动计算，直接人工介入
    if category is None or category == "UNKNOWN":
        print(
            f"[Warn] PN={part_no} 无法从 France/Sys 映射中识别产品线，"
            "已保留原始价格字段，不进行自动计算，请人工介入确认。"
        )
        return {
            "final_values": final_values,
            "calculated_fields": calculated_fields,
            "category": category or "UNKNOWN",
            "price_group": price_group,
            "series_display": series_display,
            "auto_success": False,
            "used_sys": False,
        }

    # 4) 如果 France 所有价格都齐全 → 直接视为“全部 Original”，不做计算
    if _all_prices_present(france_row):
        auto_success = True
        return {
            "final_values": final_values,
            "calculated_fields": calculated_fields,
            "category": category,
            "price_group": price_group,
            "series_display": series_display,
            "auto_success": auto_success,
            "used_sys": False,
        }

    # 5) 需要做补全：先找 FOB
    fob = _to_float(final_values.get("FOB C(EUR)"))

    used_sys = False
    if (fob is None or fob <= 0) and sys_row is not None:
        # France 没给 FOB，尝试用 Sys + Sales Type / 交互选择 算 FOB
        sales_type = ""
        if france_row is not None and "Sales Type" in france_row:
            sales_type = str(france_row.get("Sales Type") or "")
        base_price = _choose_sys_base_price(part_no, sales_type, sys_row)
        if base_price is not None and base_price > 0:
            fob = base_price * 0.9
            # 不 round，保留原始浮点值
            final_values["FOB C(EUR)"] = fob
            calculated_fields.add("FOB C(EUR)")
            used_sys = True

    # 6) DDP A：如果 France 没写，就用 FOB + DDP_RULES 算
    ddp_existing = _to_float(final_values.get("DDP A(EUR)"))
    if ddp_existing is not None and ddp_existing > 0:
        ddp_a = ddp_existing
    else:
        ddp_a = compute_ddp_a_from_fob(fob, category)
        if ddp_a is not None:
            # 不 round，保留原始浮点值
            final_values["DDP A(EUR)"] = ddp_a
            calculated_fields.add("DDP A(EUR)")

    # 7) 渠道价：如果某列缺失且有 DDP + 价格组规则，就计算补全
    if ddp_a is not None:
        price_rule = pick_price_rule(price_group, series_key)
        if price_rule is not None:
            channel_prices = compute_channel_prices(ddp_a, price_rule)
            for col in PRICE_COLS:
                if col == "FOB C(EUR)":
                    continue  # FOB 不在 channel_prices 里
                if _to_float(final_values.get(col)) is None and col in channel_prices:
                    final_values[col] = channel_prices[col]
                    calculated_fields.add(col)

    auto_success = True  # 能走到这里说明产品线识别成功，且已尝试自动补全

    return {
        "final_values": final_values,
        "calculated_fields": calculated_fields,
        "category": category,
        "price_group": price_group,
        "series_display": series_display,
        "auto_success": auto_success,
        "used_sys": used_sys,
    }
