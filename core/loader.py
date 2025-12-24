import os
import pandas as pd

from config import get_data_path, get_mapping_path


def load_france_price() -> pd.DataFrame:
    path = get_data_path("FrancePrice.xlsx")
    return pd.read_excel(path)


def load_sys_price() -> pd.DataFrame:

    candidates = [
        "SysPrice.xls",
        "SysPrice.xlsx",
    ]

    tried = []
    for name in candidates:
        path = get_data_path(name)
        tried.append(path)
        if os.path.exists(path):
            return pd.read_excel(path)

    raise FileNotFoundError(
        "未找到 SysPrice 数据文件，已尝试以下路径：\n" + "\n".join(tried)
    )


def load_france_mapping() -> pd.DataFrame:
    """
    映射文件：mapping/productline_map_france_full.csv
    """
    path = get_mapping_path("productline_map_france_full.csv")
    return pd.read_csv(path)


def load_sys_mapping() -> pd.DataFrame:
    """
    映射文件：mapping/productline_map_sys_full.csv
    """
    path = get_mapping_path("productline_map_sys_full.csv")
    return pd.read_csv(path)
