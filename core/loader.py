import pandas as pd

from config import get_data_path, get_mapping_path


def load_france_price() -> pd.DataFrame:
    path = get_data_path("FrancePrice.xlsx")
    return pd.read_excel(path)


def load_sys_price() -> pd.DataFrame:
    path = get_data_path("SysPrice.xls")
    # xlrd 只支持 xls
    return pd.read_excel(path)


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
