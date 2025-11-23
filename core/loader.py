import os
import pandas as pd


def _default_data_path(filename: str) -> str:
    """
    在没有 config.get_data_path 的情况下，
    默认认为项目结构是:
        根目录/
            core/
            data/
    """
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, "data", filename)


# 尝试从 config 导入 get_data_path，如果没有就用上面的默认逻辑
try:
    from config import get_data_path as _get_data_path  # type: ignore
except Exception:  # noqa: BLE001
    _get_data_path = _default_data_path


def get_data_path(filename: str) -> str:
    return _get_data_path(filename)


def load_france_price() -> pd.DataFrame:
    """读取 FrancePrice.xlsx"""
    path = get_data_path("FrancePrice.xlsx")
    return pd.read_excel(path)


def load_sys_price() -> pd.DataFrame:
    """读取 SysPrice.xls"""
    path = get_data_path("SysPrice.xls")
    return pd.read_excel(path)


def load_france_mapping() -> pd.DataFrame:
    """读取 France 映射表"""
    path = get_data_path("productline_map_france_full.csv")
    df = pd.read_csv(path)
    if "priority" in df.columns:
        df = df.sort_values("priority")
    return df


def load_sys_mapping() -> pd.DataFrame:
    """读取 Sys 映射表"""
    path = get_data_path("productline_map_sys_full.csv")
    df = pd.read_csv(path)
    if "priority" in df.columns:
        df = df.sort_values("priority")
    return df
