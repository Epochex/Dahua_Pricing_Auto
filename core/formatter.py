import math
from typing import Dict, Iterable, Set
from decimal import Decimal, ROUND_HALF_UP

from tabulate import tabulate


COLUMNS_TO_SHOW = [
    "Part No.",
    "Series",
    "External Model",
    "Internal Model",
    "Sales Status",
    "Description",
    "FOB C(EUR)",
    "DDP A(EUR)",
    "Suggested Reseller(EUR)",
    "Gold(EUR)",
    "Silver(EUR)",
    "Ivory(EUR)",
    "MSRP(EUR)",
]

BOLD_COLS = {
    "Part No.",
    "FOB C(EUR)",
    "DDP A(EUR)",
    "Suggested Reseller(EUR)",
    "Gold(EUR)",
    "Silver(EUR)",
    "Ivory(EUR)",
    "MSRP(EUR)",
}

# 需要打 Original / Calculated 标记的价格列
PRICE_COLS = {
    "FOB C(EUR)",
    "DDP A(EUR)",
    "Suggested Reseller(EUR)",
    "Gold(EUR)",
    "Silver(EUR)",
    "Ivory(EUR)",
    "MSRP(EUR)",
}


def round_price_number(num: float) -> float:
    """
    统一的价格取整规则（导出 & 控制台都用这一套）：

    - num < 30  → 四舍五入到 1 位小数
    - num >= 30 → 四舍五入到整数

    使用 Decimal + ROUND_HALF_UP，行为与 Excel 四舍五入一致。
    """
    d = Decimal(str(num))
    if num < 30:
        q = d.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    else:
        q = d.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(q)


def _format_value(v):
    if v is None:
        return "Value Not Found"
    try:
        if isinstance(v, float) and math.isnan(v):
            return "Price Not Found"
    except Exception:  # noqa: BLE001
        pass
    return v


def render_table(
    final_values: Dict[str, object],
    calculated_fields: Iterable[str],
) -> str:
    """
    把结果 dict 渲染成表格。

    规则：
    - 只对“Calculated”的价格做数值格式：
        * 数值 < 30  → 四舍五入到 1 位小数
        * 数值 >=30 → 四舍五入到整数
    - Original 的价格保持原值（不改小数位），只追加标记。
    """
    calc_set: Set[str] = set(calculated_fields)
    rows = []

    seen = set()
    for col in COLUMNS_TO_SHOW:
        if col in seen:
            continue
        seen.add(col)

        raw = final_values.get(col)

        # ===== 只对“Calculated 的价格列”做数值格式 =====
        if col in PRICE_COLS and raw is not None and col in calc_set:
            try:
                num = float(raw)
                if math.isnan(num):
                    raw = None
                else:
                    num2 = round_price_number(num)
                    if num2 < 30:
                        # <30：保留 1 位小数
                        raw = f"{num2:.1f}"
                    else:
                        # >=30：显示为整数
                        raw = str(int(num2))
            except (TypeError, ValueError):
                # 不是数值就保持原样
                pass

        val = _format_value(raw)

        # ===== Original / Calculated 标记 =====
        if col in PRICE_COLS and val != "Price Not Found":
            if col in calc_set:
                val = f"{val} | Calculated"
            else:
                val = f"{val} | Original"

        # ===== 加粗关键列 =====
        if col in BOLD_COLS:
            col_disp = f"\033[1m{col}\033[0m"
            val_disp = f"\033[1m{val}\033[0m"
        else:
            col_disp = col
            val_disp = val

        rows.append([col_disp, val_disp])

    return tabulate(rows, headers=["Name", "Value"], tablefmt="grid")


def build_status_line(result: Dict) -> str:
    """
    根据结果构造一行“计算状态：...”的提示。
    """
    cat = result.get("category") or "UNKNOWN"
    series_display = result.get("series_display") or ""
    calc_fields = result.get("calculated_fields") or set()
    auto_success = bool(result.get("auto_success"))

    if not auto_success:
        return "计算状态：自动化计算失败，已切换为原始价格（若存在）"

    if calc_fields:
        return f"计算状态：自动化计算成功（产品线：{cat} | 系列：{series_display}）"

    # auto_success=True 且没有任何字段需要计算 → 全部 Original
    return f"计算状态：无需自动补全，全部使用原始价格（产品线：{cat} | 系列：{series_display}）"
