import sys

import pandas as pd

from config import APP_TITLE, DATA_DATE, AUTHOR_INFO
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
        part_no = input("\n请输入 Part No.（输入 quit 退出）：").strip()
        if part_no.lower() in {"quit", "exit", "q"}:
            print("程序已退出，感谢使用！")
            break
        if not part_no:
            continue

        key = part_no.strip().lower()

        # 在 France 表中查 PN
        fr_row = None
        fr_matches = france_df[france_df["_pn_key"] == key]
        if not fr_matches.empty:
            fr_row = fr_matches.iloc[0]

        # 在 Sys 表中查 PN
        sys_row = None
        sys_matches = sys_df[sys_df["_pn_key"] == key]
        if not sys_matches.empty:
            sys_row = sys_matches.iloc[0]

        if fr_row is None and sys_row is None:
            print("❌ France / Sys 中均未找到该 PN，请确认 PN 是否正确或联系 PM 新增。")
            continue

        # 价格引擎：一起把 France + Sys 行丢进去
        result = compute_prices_for_part(
            part_no,
            fr_row,
            sys_row,
            france_map,
            sys_map,
        )

        print("\n查询结果如下：")
        print(build_status_line(result))
        print()
        print(render_table(result["final_values"], result["calculated_fields"]))


if __name__ == "__main__":
    main()
