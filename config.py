import os
import sys

# =========================
# 基本信息
# =========================

APP_TITLE = "大华法国驻地专用自动化 报价计算|查询|信息汇总 mini软件"
DATA_DATE = "2025.11.24"
AUTHOR_INFO = (
    "当前仍处于测试阶段,若对数据产生疑问请立刻联系对应PM和技术人员进行价格核对\n"
    "对于硬盘存储类设备，近期价格波动频繁，请注意实时报价\n"
    "使用中发现任何问题，请联系开发人员 林建克 LIN Jianke\n"
    "Huachat: Jianke LIN | 微信: Epochex404"
)


# =========================
# 路径工具
# =========================

def get_base_dir() -> str:
    """
    返回程序“根目录”：
    - 源码运行：config.py 所在目录
    - PyInstaller 打包：sys._MEIPASS
    """
    if hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS  # type: ignore[attr-defined]
    return os.path.dirname(os.path.abspath(__file__))


def get_data_path(filename: str) -> str:
    """
    data 目录下文件：
        data/FrancePrice.xlsx
        data/SysPrice.xls
    """
    base_dir = get_base_dir()
    data_dir = os.path.join(base_dir, "data")
    return os.path.join(data_dir, filename)


def get_mapping_path(filename: str) -> str:
    """
    mapping 目录下文件：
        mapping/productline_map_france_full.csv
        mapping/productline_map_sys_full.csv
    """
    base_dir = get_base_dir()
    mapping_dir = os.path.join(base_dir, "mapping")
    return os.path.join(mapping_dir, filename)


def get_file_in_base(filename: str) -> str:
    """
    根目录下文件：
        List_PN.txt
        Country_import_upload_Model.xlsx
        Country&Customer_import_upload_Model.xlsx
    """
    base_dir = get_base_dir()
    return os.path.join(base_dir, filename)
