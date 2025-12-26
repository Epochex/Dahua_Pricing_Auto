# core/formatter.py
import math
import os
import re
import textwrap
from typing import Dict, Iterable, Set
from decimal import Decimal, ROUND_HALF_UP

import tabulate as _tab
from tabulate import tabulate

_tab.WIDE_CHARS_MODE = True
_tab.PRESERVE_WHITESPACE = True

_NO_ANSI = os.getenv("DAHUA_NO_ANSI", "").strip().lower() in {"1", "true", "yes", "y", "on"}

# 两列模式下：Description 每行最大宽度（防止撑爆整表）
DESC_WRAP_WIDTH = 92

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

PRICE_COLS = {
    "FOB C(EUR)",
    "DDP A(EUR)",
    "Suggested Reseller(EUR)",
    "Gold(EUR)",
    "Silver(EUR)",
    "Ivory(EUR)",
    "MSRP(EUR)",
}

_ZERO_WIDTH = re.compile(r"[\u200B-\u200F\uFEFF]")


def _bold(s: str) -> str:
    if _NO_ANSI:
        return s
    return f"\033[1m{s}\033[0m"


def round_price_number(num: float) -> float:
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
    except Exception:
        pass
    return v


def _sanitize_cell_text(x) -> str:
    """
    清洗不可见字符 + 避免 '|' 伪装成表格边框
    """
    s = "" if x is None else str(x)
    s = s.replace("\r", "")
    s = s.replace("\t", "    ")
    s = _ZERO_WIDTH.sub("", s)
    s = s.replace("|", "¦")  # 内容里的竖线换掉，避免和表框混淆
    return s


def _wrap_multiline_text_keep_lines(s: str, width: int) -> str:
    """
    Description：逐行 wrap，去掉行首的“¦ ”（原来可能是“| ”）
    """
    s = _sanitize_cell_text(s)
    lines = s.splitlines() or [s]
    out = []

    for line in lines:
        line = line.rstrip()
        line = re.sub(r"^\s*¦\s*", "", line)

        if not line:
            out.append("")
            continue

        out.extend(
            textwrap.wrap(
                line,
                width=width,
                break_long_words=False,
                break_on_hyphens=False,
            )
            or [""]
        )

    return "\n".join(out)


def _format_price_if_calculated(col: str, raw, calc_set: Set[str]):
    """
    如果该价格字段是 Calculated，则按规则格式化数值；
    Original 则保留原始显示（你的当前逻辑就是这样）。
    """
    if col not in PRICE_COLS or raw is None or col not in calc_set:
        return raw

    try:
        num = float(raw)
        if math.isnan(num):
            return None
        num2 = round_price_number(num)
        return f"{num2:.1f}" if num2 < 30 else str(int(num2))
    except (TypeError, ValueError):
        return raw


def render_table(final_values: Dict[str, object], calculated_fields: Iterable[str]) -> str:
    """
    输出两列表：Name | Value
    对价格行：把 Original/Calculated 标签拼到 Value 末尾：
      230.0 | Original
      297   | Calculated
    """
    calc_set: Set[str] = set(calculated_fields)
    rows = []

    for col in COLUMNS_TO_SHOW:
        raw = final_values.get(col)
        raw = _format_price_if_calculated(col, raw, calc_set)

        val = _format_value(raw)

        col_txt = _sanitize_cell_text(col)

        # Description：自动换行
        if col == "Description" and val not in {"Value Not Found", "Price Not Found"}:
            val_txt = _wrap_multiline_text_keep_lines(str(val), width=DESC_WRAP_WIDTH)
        else:
            val_txt = _sanitize_cell_text(val)

        # 价格行：追加标签到 Value（并用一个明显但不破坏对齐的分隔）
        if col in PRICE_COLS and val not in {"Value Not Found", "Price Not Found"}:
            tag = "Calculated" if col in calc_set else "Original"
            # 用 " | " 会变成内容竖线（已被替换规则改成 ¦），所以这里用 "  •  " 更干净
            val_txt = f"{val_txt}  |  {tag}"

        if col in BOLD_COLS:
            col_disp = _bold(col_txt)
            val_disp = _bold(val_txt)
        else:
            col_disp = col_txt
            val_disp = val_txt

        rows.append([col_disp, val_disp])

    return tabulate(rows, headers=["Name", "Value"], tablefmt="grid")


def build_status_line(result: Dict) -> str:
    cat = result.get("category") or "UNKNOWN"
    series_display = result.get("series_display") or ""
    calc_fields = result.get("calculated_fields") or set()
    auto_success = bool(result.get("auto_success"))

    if not auto_success:
        return "计算状态：自动化计算失败，已切换为原始价格（若存在）"

    if calc_fields:
        return f"计算状态：自动化计算成功（产品线：{cat} | 系列：{series_display}）"

    return f"计算状态：无需自动补全，全部使用原始价格（产品线：{cat} | 系列：{series_display}）"


def build_sys_calc_line(result: Dict) -> str:
    if not result.get("used_sys"):
        return ""
    sales = result.get("sys_sales_type") or "UNKNOWN"
    basis = result.get("sys_basis_field") or "UNKNOWN"
    return f"[计算层级]：{sales}（Sys 基准字段：{basis}）"
