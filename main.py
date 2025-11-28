from __future__ import annotations

import sys
import time
import os
from typing import TYPE_CHECKING, List, Dict
import math

from config import APP_TITLE, DATA_DATE, AUTHOR_INFO, get_file_in_base

if TYPE_CHECKING:
    import pandas as pd


# =========================
# PN 标准化工具
# =========================

def normalize_pn_raw(raw) -> str:
    """
    原样标准化：
    - 转字符串
    - strip 去空格
    - lower 小写
    不做任何截断，用于“完全匹配”。
    """
    if raw is None:
        return ""
    return str(raw).strip().lower()


def normalize_pn_base(raw) -> str:
    """
    “前缀 key” 标准化，用于兜底匹配。

    需求：1.0.01.01.16317-9002 == 1.0.01.01.16317

    规则：
    1) 先做 normalize_pn_raw 得到 s
    2) 若 s 中包含 '-'，拆成 base-suffix：
       - 如果 base 只由 [0-9 .] 组成，且包含至少一个 '.'，
         认为是 Dahua 内部编码：返回 base 作为前缀 key
       - 否则不截断，直接返回 s
    3) 没有 '-' 就直接返回 s
    """
    s = normalize_pn_raw(raw)
    if not s:
        return ""

    base, sep, suffix = s.partition("-")
    if sep and base and suffix:
        base_compact = base.replace(" ", "")
        # 限定：只在“点分数字”风格编码上去掉后缀，避免误伤其他真带“-”的料号
        if base_compact and all((c.isdigit() or c == ".") for c in base_compact) and "." in base_compact:
            return base

    return s


def _prepare_index(df: pd.DataFrame, col_name: str) -> pd.DataFrame:
    """
    给 DataFrame 增加两个 key：
    - _pn_key_raw  : 用于完全匹配
    - _pn_key_base : 用于前缀兜底匹配
    """
    df = df.copy()
    df["_pn_key_raw"] = df[col_name].apply(normalize_pn_raw)   # 精准匹配
    df["_pn_key_base"] = df[col_name].apply(normalize_pn_base)  # 前缀兜底
    return df


# =========================
# 匹配工具
# =========================

def _mode_label(mode: str) -> str:
    """
    把内部匹配模式翻译成可读中文。
    """
    if mode == "exact_raw":
        return "精确匹配"
    if mode == "fallback_suffix_to_base":
        return "输入带后缀，回退到无后缀基础料号"
    if mode == "fallback_base_to_suffix":
        return "输入无后缀，回退到带后缀国际料号"
    return "未匹配"


def _find_row_with_fallback(
    df: pd.DataFrame,
    key_raw: str,
    key_base: str,
    pn_col_name: str,
):
    """
    在 df 中按“精确 → 兜底”的顺序找一行，且区分输入是否带后缀。

    返回 (row_or_None, match_mode, matched_pn_str)

    情况 1：输入不带后缀，例如 1.0.01.19.10564
      1) 先按 _pn_key_raw == key_raw 精确匹配
      2) 若找不到，再按 _pn_key_base == key_base 匹配
         （可以匹配到 1.0.01.19.10564-9001 这类带国际后缀的行）

    情况 2：输入带后缀，例如 1.0.01.19.10564-9001
      1) 先按 _pn_key_raw == key_raw 精确匹配
      2) 若找不到，再按 _pn_key_raw == key_base 匹配
         （即只兜底到“不带后缀”的基础料号 1.0.01.19.10564）
    """
    if not key_raw and not key_base:
        return None, "none", None

    # 先尝试精确匹配
    if key_raw:
        m = df[df["_pn_key_raw"] == key_raw]
        if not m.empty:
            row = m.iloc[0]
            matched_pn = row.get(pn_col_name)
            return row, "exact_raw", matched_pn

    if not key_base:
        return None, "none", None

    # 判断“输入是否带有可截断的后缀”
    has_suffix = ("-" in key_raw) and (key_base != key_raw)

    if has_suffix:
        # 输入带后缀：兜底只找“没有后缀”的那一行（raw == base）
        m = df[df["_pn_key_raw"] == key_base]
        if not m.empty:
            row = m.iloc[0]
            matched_pn = row.get(pn_col_name)
            return row, "fallback_suffix_to_base", matched_pn
    else:
        # 输入不带后缀：兜底找任意 base 相同的行（可以是带后缀的国际料号）
        m = df[df["_pn_key_base"] == key_base]
        if not m.empty:
            row = m.iloc[0]
            matched_pn = row.get(pn_col_name)
            return row, "fallback_base_to_suffix", matched_pn

    return None, "none", None


# =========================
# 导出 DataFrame 构造
# =========================

def build_export_df(rows: List[Dict], level: str) -> pd.DataFrame:
    """
    rows: 每个元素形如
        {
            "final_values": {...},
            "calculated_fields": set([...]),
        }
    level: "1" -> Country; "2" -> Country&Customer

    导出时对“Calculated 的价格列”应用与控制台一致的取整规则：
      - < 30 → 四舍五入到 1 位小数
      - ≥ 30 → 四舍五入到整数
    Original 列保持原始数值。
    """
    data = []

    for item in rows:
        fv = item.get("final_values", {}) or {}
        calc_set = set(item.get("calculated_fields") or [])

        def fmt(col_name: str):
            """
            导出单元格用的数值：
              - 如果 col_name 在 calculated_fields 里 → 做数值取整
              - 否则直接返回原始值（保持原始小数位）
            """
            raw = fv.get(col_name)
            if col_name not in calc_set:
                return raw

            # 只对 Calculated 列做统一取整
            try:
                num = float(raw)
                if math.isnan(num):
                    return None
                return round_price_number(num)
            except (TypeError, ValueError):
                return raw

        pn = fv.get("Part No.")
        fob = fmt("FOB C(EUR)")
        ddp = fmt("DDP A(EUR)")
        reseller = fmt("Suggested Reseller(EUR)")
        gold = fmt("Gold(EUR)")
        silver = fmt("Silver(EUR)")
        ivory = fmt("Ivory(EUR)")
        msrp = fmt("MSRP(EUR)")

        if level == "1":
            # Country 模板
            row = {
                "Part No.": pn,
                "FOB C": fob,
                "DDP A": ddp,
                "Reseller S": reseller,
                "SI-S": gold,     # Gold
                "SI-A": silver,   # Silver
                "MSTP": ivory,    # MSTP = Ivory
                "MSRP": msrp,
            }
        else:
            # Country & Customer 模板
            si_a = gold  # Gold
            si_s = si_a  # Diamond = Gold
            row = {
                "Part No.": pn,
                "FOB C": fob,
                "DDP A": ddp,
                "Reseller S": reseller,
                "SI-S": si_s,     # Diamond (同 Gold)
                "SI-A": si_a,     # Gold
                "SI-B": silver,   # Silver
                "MSTP": ivory,    # Ivory
                "MSRP": msrp,
            }

        data.append(row)

    return pd.DataFrame(data)


# =========================
# 批量模式
# =========================

def run_batch(
    france_df: pd.DataFrame,
    sys_df: pd.DataFrame,
    france_map: pd.DataFrame,
    sys_map: pd.DataFrame,
) -> None:
    """
    批量模式：
      - 读取根目录 List_PN.txt，每行一个 PN
      - 对每个 PN 计算价格
      - 在控制台打印每个 PN 的表格结果（含 Original/Calculated 标记）
      - 导出 Country / Country&Customer 模板
      - 所有 PN 处理完后，再一次性汇总输出“找不到的 PN 列表”
    """
    list_path = get_file_in_base("List_PN.txt")
    if not os.path.exists(list_path):
        # 第一次使用：自动创建模板文件
        with open(list_path, "w", encoding="utf-8") as f:
            f.write(
                "# List_PN.txt 模板\n"
                "# 每行一个 Part No.\n"
                "# 示例：\n"
                "# 1.1.02.08.14034-002\n"
                "# 1.0.01.19.10564\n"
                "\n"
            )
        print(f"❌ 未找到批量 PN 列表文件\n")
        print(f"✅ 已在当前目录创建模板文件：{list_path}")
        print("  请编辑该文件，填入每行一个 PN 后，再次进入批量模式。")
        return


    with open(list_path, "r", encoding="utf-8") as f:
        pns = [line.strip() for line in f if line.strip()]

    if not pns:
        print("❌ List_PN.txt 为空，批量处理取消。")
        return

    print(f"\n检测到批量模式，共 {len(pns)} 个 PN，将依次计算价格...\n")

    # 每个元素：{"final_values": {...}, "calculated_fields": set([...])}
    results_for_export: List[Dict] = []
    not_found: List[str] = []

    for idx, pn in enumerate(pns, start=1):
        key_raw = normalize_pn_raw(pn)
        key_base = normalize_pn_base(pn)

        fr_row, fr_mode, fr_matched = _find_row_with_fallback(
            france_df, key_raw, key_base, "Part No."
        )
        sys_row, sys_mode, sys_matched = _find_row_with_fallback(
            sys_df, key_raw, key_base, "Part Num"
        )

        if fr_row is None and sys_row is None:
            # 暂时只记录，等所有 PN 处理完再统一输出
            not_found.append(pn)
            continue

        # ===== 匹配信息提示 =====
        print("=" * 80)
        print(f"[Batch {idx}/{len(pns)}] PN = {pn}")
        print(
            f"[Match] France: {fr_matched if fr_matched is not None else '未命中'} "
            f"({ _mode_label(fr_mode) }) | "
            f"Sys: {sys_matched if sys_matched is not None else '未命中'} "
            f"({ _mode_label(sys_mode) })"
        )

        result = compute_prices_for_part(pn, fr_row, sys_row, france_map, sys_map)

        # 报价维度一律使用“输入的 PN”，而不是底层匹配到的 France/Sys PN
        fv = result["final_values"]
        fv["Part No."] = pn

        print(build_status_line(result))
        print()
        print(render_table(fv, result["calculated_fields"]))
        print()

        # 简单检查：至少有 DDP A，否则基本没法上传
        if fv.get("DDP A(EUR)") is None:
            print(f"[Warn] PN={pn} 未得到有效 DDP A 价格，仍写入导出表但需人工复核。")

        results_for_export.append(
            {
                "final_values": fv,
                "calculated_fields": set(result.get("calculated_fields") or []),
            }
        )

    # 先输出“完全找不到”的 PN 汇总信息
    if not_found:
        print("\n以下 PN 在 France / Sys 中均未找到（已跳过）：")
        for pn in not_found:
            print(f"[Skip] PN={pn} 在 France / Sys 中均未找到，跳过。")

    if not results_for_export:
        print("\n❌ 没有任何 PN 计算成功，批量处理结束。")
        return

    # 选择导出模板层级
    print("\n价格处理完成，即将导出为上传模板，定价层级为？")
    print("  Country 层级输入 1")
    print("  Country & Customer 层级输入 2")

    level = input("请输入 1 / 2（输入 q 放弃导出）：").strip()
    while level not in {"1", "2"}:
        if level.lower() in {"q", "quit", "exit"}:
            print("已放弃导出。")
            return
        level = input("请输入 1 或 2（输入 q 放弃导出）：").strip()

    df_export = build_export_df(results_for_export, level)

    if level == "1":
        out_name = "Country_import_upload_Model.xlsx"
    else:
        out_name = "Country&Customer_import_upload_Model.xlsx"

    out_path = get_file_in_base(out_name)
    df_export.to_excel(out_path, index=False)
    print(f"\n✅ 导出完成：{out_path}\n")


# =========================
# 交互主循环
# =========================

def main() -> None:
    print("=" * 80)
    print(APP_TITLE)
    print(f"当前价格数据源更新日期：{DATA_DATE}")
    print(AUTHOR_INFO)
    print("=" * 80)

    print("正在加载依赖库和数据，请稍候...\n", flush=True)

    # 这里 import 重型库
    import pandas as pd
    from core.loader import (
        load_france_price,
        load_sys_price,
        load_france_mapping,
        load_sys_mapping,
    )
    from core.pricing_engine import compute_prices_for_part
    from core.formatter import render_table, build_status_line, round_price_number

    # ===== 载入数据 =====
    try:
        print("[1/5] 正在加载 FrancePrice.xlsx...", flush=True)
        france_df = load_france_price()

        print("[2/5] 正在加载 SysPrice.xls...", flush=True)
        sys_df = load_sys_price()

        print("[3/5] 正在加载 Mapping 映射表...", flush=True)
        france_map = load_france_mapping()
        sys_map = load_sys_mapping()

    except FileNotFoundError as e:
        print(f"❌ 载入数据失败：{e}")
        input("按回车退出...")
        sys.exit(1)

    print("[4/5] 国家侧和系统侧数据载入完成", flush=True)

    # 标准化 PN 索引：同时生成 raw/base 两套 key
    france_df = _prepare_index(france_df, "Part No.")
    sys_df = _prepare_index(sys_df, "Part Num")
    print("[5/5] 精准索引和模糊识别索引模块加载完成\n", flush=True)

    while True:
        part_no = input("\n请输入 Part No.（输入 quit 退出，直接回车进入批量模式）：").strip()

        # 批量模式
        if part_no == "":
            run_batch(france_df, sys_df, france_map, sys_map)
            continue

        if part_no.lower() in {"quit", "exit", "q"}:
            print("程序已退出，Merci Auvoir！")
            break

        key_raw = normalize_pn_raw(part_no)
        key_base = normalize_pn_base(part_no)

        fr_row, fr_mode, fr_matched = _find_row_with_fallback(
            france_df, key_raw, key_base, "Part No."
        )
        sys_row, sys_mode, sys_matched = _find_row_with_fallback(
            sys_df, key_raw, key_base, "Part Num"
        )

        if fr_row is None and sys_row is None:
            print("❌ France / Sys 中均未找到该 PN，请确认 PN 是否正确或联系 PM 新增。")
            continue

        print(
            f"[Match] France: {fr_matched if fr_matched is not None else '未命中'} "
            f"({ _mode_label(fr_mode) }) | "
            f"Sys: {sys_matched if sys_matched is not None else '未命中'} "
            f"({ _mode_label(sys_mode) })"
        )

        result = compute_prices_for_part(part_no, fr_row, sys_row, france_map, sys_map)

        # 同样在单条查询里覆盖 Part No. 为“输入的 PN”
        result["final_values"]["Part No."] = part_no

        print("\n查询结果如下：")
        print(build_status_line(result))
        print()
        print(render_table(result["final_values"], result["calculated_fields"]))


if __name__ == "__main__":    
    main()
