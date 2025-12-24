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

# 控制三列最大宽度：Name / Value / Source
# 你觉得列间距大就把 VALUE_COL_WIDTH 调小，比如 70
NAME_COL_WIDTH = 22
VALUE_COL_WIDTH = 88
SOURCE_COL_WIDTH = 12

DESC_WRAP_WIDTH = VALUE_COL_WIDTH  # 保持一致

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


_ZERO_WIDTH = re.compile(r"[\u200B-\u200F\uFEFF]")


def _sanitize_cell_text(x) -> str:
    """
    - 去掉 \r
    - 展开 \t
    - 去掉零宽字符（有些数据源会带，导致宽度计算与显示不一致）
    - 把内容里的 '|' 替换为 '¦'，避免与表格边框混淆
    """
    s = "" if x is None else str(x)
    s = s.replace("\r", "")
    s = s.replace("\t", "    ")
    s = _ZERO_WIDTH.sub("", s)
    s = s.replace("|", "¦")
    return s


def _wrap_multiline_text_keep_lines(s: str, width: int) -> str:
    """
    对含换行的文本逐行 wrap。
    另外：去掉每行开头的“¦/|”样式前缀（你现在 Description 里经常有这种伪竖线）
    """
    s = _sanitize_cell_text(s)
    lines = s.splitlines() or [s]
    out = []

    for line in lines:
        line = line.rstrip()

        # 关键：去掉每行开头的 “¦ ”（原始可能是 “| ”）
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


def render_table(final_values: Dict[str, object], calculated_fields: Iterable[str]) -> str:
    calc_set: Set[str] = set(calculated_fields)
    rows = []

    for col in COLUMNS_TO_SHOW:
        raw = final_values.get(col)

        # 价格列：Calculated 的做格式化
        if col in PRICE_COLS and raw is not None and col in calc_set:
            try:
                num = float(raw)
                if math.isnan(num):
                    raw = None
                else:
                    num2 = round_price_number(num)
                    raw = f"{num2:.1f}" if num2 < 30 else str(int(num2))
            except (TypeError, ValueError):
                pass

        val = _format_value(raw)

        # Description：强制 wrap，避免撑爆 Value 列
        if col == "Description" and val not in {"Value Not Found", "Price Not Found"}:
            val_txt = _wrap_multiline_text_keep_lines(str(val), width=DESC_WRAP_WIDTH)
        else:
            val_txt = _sanitize_cell_text(val)

        # Source：仅价格列显示
        source = ""
        if col in PRICE_COLS and val not in {"Value Not Found", "Price Not Found"}:
            source = "Calculated" if col in calc_set else "Original"

        col_txt = _sanitize_cell_text(col)
        src_txt = _sanitize_cell_text(source)

        if col in BOLD_COLS:
            col_disp = _bold(col_txt)
            val_disp = _bold(val_txt)
            src_disp = _bold(src_txt) if src_txt else ""
        else:
            col_disp = col_txt
            val_disp = val_txt
            src_disp = src_txt

        rows.append([col_disp, val_disp, src_disp])

    # 关键：限制三列最大宽度，Source 列不会被推到很远
    return tabulate(
        rows,
        headers=["Name", "Value", "Source"],
        tablefmt="grid",
        maxcolwidths=[NAME_COL_WIDTH, VALUE_COL_WIDTH, SOURCE_COL_WIDTH],
    )


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
