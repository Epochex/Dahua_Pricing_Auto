import math
from typing import Dict, Iterable, Set

import pandas as pd
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


def _format_value(v):
    if v is None:
        return "Price Not Found"
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
    把结果 dict 渲染成 tabulate 表格文本。
    """
    calc_set: Set[str] = set(calculated_fields)
    rows = []

    seen = set()
    for col in COLUMNS_TO_SHOW:
        if col in seen:
            continue
        seen.add(col)

        val = final_values.get(col)
        val = _format_value(val)

        if col in calc_set and val != "Price Not Found":
            val = f"{val} | calculated"

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
        return "计算状态：自动化计算失败，已切换为 Excel 原始价格"

    if calc_fields:
        return f"计算状态：自动化计算成功（产品线：{cat} | 系列：{series_display}）"

    # auto_success=True 但没有任何字段需要计算 → 全部来自 France 原始数据
    return f"计算状态：无需自动补全，全部使用 France 原始价格（产品线：{cat} | 系列：{series_display}）"
