import sys
from typing import List, Dict

import os
import pandas as pd

from config import (
    APP_TITLE,
    DATA_DATE,
    AUTHOR_INFO,
    get_file_in_base,
)
from core.loader import (
    load_france_price,
    load_sys_price,
    load_france_mapping,
    load_sys_mapping,
)
from core.pricing_engine import compute_prices_for_part
from core.formatter import render_table, build_status_line


def _prepare_index(df: pd.DataFrame, col_name: str, key_col: str = "_pn_key") -> pd.DataFrame:
    """
    给 DataFrame 增加一个标准化的 PN key 列，便于查找。
    col_name: 原表中的 PN 列名，例如 "Part No." 或 "Part Num"
    """
    df = df.copy()
    df[key_col] = (
        df[col_name]
        .astype(str)
        .str.strip()
        .str.lower()
    )
    return df


def build_export_df(rows: List[Dict], level: str) -> pd.DataFrame:
    """
    rows: 每个元素是 compute_prices_for_part 的 result["final_values"]。
    level: "1" -> Country; "2" -> Country&Customer
    """
    data = []

    for fv in rows:
        pn = fv.get("Part No.")
        fob = fv.get("FOB C(EUR)")
        ddp = fv.get("DDP A(EUR)")
        reseller = fv.get("Suggested Reseller(EUR)")
        gold = fv.get("Gold(EUR)")
        silver = fv.get("Silver(EUR)")
        ivory = fv.get("Ivory(EUR)")
        msrp = fv.get("MSRP(EUR)")

        if level == "1":
            # Country 模板
            row = {
                "Part No.": pn,
                "FOB C": fob,
                "DDP A": ddp,
                "Reseller S": reseller,
                "SI-S": gold,     # Gold
                "SI-A": silver,   # Silver
                "SI-B": ivory,    # Ivory
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
                "STP": ivory,     # Ivory
                "MSRP": msrp,
            }

        data.append(row)

    return pd.DataFrame(data)


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
    """
    list_path = get_file_in_base("List_PN.txt")
    if not os.path.exists(list_path):
        print(f"❌ 未找到批量 PN 列表文件：{list_path}")
        return

    with open(list_path, "r", encoding="utf-8") as f:
        pns = [line.strip() for line in f if line.strip()]

    if not pns:
        print("❌ List_PN.txt 为空，批量处理取消。")
        return

    print(f"\n检测到批量模式，共 {len(pns)} 个 PN，将依次计算价格...\n")

    results_for_export: List[Dict] = []

    for idx, pn in enumerate(pns, start=1):
        key = pn.strip().lower()
        fr_row = None
        sys_row = None

        fr_matches = france_df[france_df["_pn_key"] == key]
        if not fr_matches.empty:
            fr_row = fr_matches.iloc[0]

        sys_matches = sys_df[sys_df["_pn_key"] == key]
        if not sys_matches.empty:
            sys_row = sys_matches.iloc[0]

        if fr_row is None and sys_row is None:
            print(f"[Skip] PN={pn} 在 France / Sys 中均未找到，跳过。")
            continue

        result = compute_prices_for_part(pn, fr_row, sys_row, france_map, sys_map)
        fv = result["final_values"]

        # 控制台输出当前 PN 的表格，复用单条查询的展示逻辑
        print("=" * 80)
        print(f"[Batch {idx}/{len(pns)}] PN = {pn}")
        print(build_status_line(result))
        print()
        print(render_table(fv, result["calculated_fields"]))
        print()

        # 简单检查：至少有 DDP A，否则基本没法上传
        if fv.get("DDP A(EUR)") is None:
            print(f"[Warn] PN={pn} 未得到有效 DDP A 价格，仍写入导出表但需人工复核。")

        results_for_export.append(fv)

    if not results_for_export:
        print("❌ 没有任何 PN 计算成功，批量处理结束。")
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


def main() -> None:
    print("=" * 80)
    print(APP_TITLE)
    print(f"当前价格数据源更新日期：{DATA_DATE}")
    print(AUTHOR_INFO)
    print("=" * 80)

    # ===== 载入数据 =====
    try:
        france_df = load_france_price()
    except FileNotFoundError:
        print("❌ 无法找到 data/FrancePrice.xlsx，请检查 data 目录。")
        input("按回车退出...")
        sys.exit(1)

    try:
        sys_df = load_sys_price()
    except FileNotFoundError:
        print("❌ 无法找到 data/SysPrice.xls，请检查 data 目录。")
        input("按回车退出...")
        sys.exit(1)

    france_map = load_france_mapping()
    sys_map = load_sys_mapping()

    # 标准化 PN 索引
    france_df = _prepare_index(france_df, "Part No.")
    sys_df = _prepare_index(sys_df, "Part Num")

    while True:
        part_no = input("\n请输入 Part No.（输入 quit 退出，直接回车进入批量模式）：").strip()

        # 批量模式
        if part_no == "":
            run_batch(france_df, sys_df, france_map, sys_map)
            continue

        if part_no.lower() in {"quit", "exit", "q"}:
            print("程序已退出，感谢使用！")
            break

        key = part_no.strip().lower()

        # 在 France / Sys 表中查 PN
        fr_row = None
        fr_matches = france_df[france_df["_pn_key"] == key]
        if not fr_matches.empty:
            fr_row = fr_matches.iloc[0]

        sys_row = None
        sys_matches = sys_df[sys_df["_pn_key"] == key]
        if not sys_matches.empty:
            sys_row = sys_matches.iloc[0]

        if fr_row is None and sys_row is None:
            print("❌ France / Sys 中均未找到该 PN，请确认 PN 是否正确或联系 PM 新增。")
            continue

        result = compute_prices_for_part(part_no, fr_row, sys_row, france_map, sys_map)

        print("\n查询结果如下：")
        print(build_status_line(result))
        print()
        print(render_table(result["final_values"], result["calculated_fields"]))


if __name__ == "__main__":
    main()
