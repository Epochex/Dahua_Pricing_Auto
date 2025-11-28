import os
import sys

# =========================
# 基本信息
# =========================

APP_TITLE = "大华法国驻地 产品定价解析计算自动化CLI软件"
DATA_DATE = "2025.11.26"
AUTHOR_INFO = (
    "当前仍处于测试阶段,若对数据产生疑问请立刻联系对应PM和技术人员进行价格核对\n"
    "对于硬盘存储类设备，近期价格波动频繁，请注意实时报价\n"
    "使用中发现任何问题，请联系开发人员 林建克 LIN Jianke\n"
    "Huachat: Jianke LIN | 微信: Epochex404"
)


# =========================
# 路径工具（内部资源）
# =========================

def get_base_dir() -> str:
    """
    返回“内部资源”的根目录：
    - 源码运行：config.py 所在目录
    - PyInstaller 打包：sys._MEIPASS （解压后的临时目录）
    用于 data/、mapping/ 这类随程序一起打包进去的资源。
    """
    if hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS  # type: ignore[attr-defined]
    return os.path.dirname(os.path.abspath(__file__))


def get_data_path(filename: str) -> str:
    """
    data 目录下文件（随 exe 打包）：
        data/FrancePrice.xlsx
        data/SysPrice.xls
    """
    base_dir = get_base_dir()
    data_dir = os.path.join(base_dir, "data")
    return os.path.join(data_dir, filename)


def get_mapping_path(filename: str) -> str:
    """
    mapping 目录下文件（随 exe 打包）：
        mapping/productline_map_france_full.csv
        mapping/productline_map_sys_full.csv
    """
    base_dir = get_base_dir()
    mapping_dir = os.path.join(base_dir, "mapping")
    return os.path.join(mapping_dir, filename)


# =========================
# 路径工具（外部可见文件）
# =========================

def _get_exe_dir() -> str:
    """
    返回“可写目录”（外部可见）：
    - 打包后：exe 所在目录（os.path.dirname(sys.executable)）
    - 源码运行：config.py 所在目录（方便开发调试）
    用于：
      - List_PN.txt（对外暴露可编辑）
      - 导出的 Country_import_upload_Model.xlsx / Country&Customer_import_upload_Model.xlsx
    """
    if getattr(sys, 'frozen', False) and hasattr(sys, 'executable'):
        # PyInstaller 打包后的 exe
        return os.path.dirname(sys.executable)
    # 源码运行时：就用当前文件所在目录，方便调试
    return os.path.dirname(os.path.abspath(__file__))


def get_file_in_base(filename: str) -> str:
    """
    exe 同目录下的外部文件：
        List_PN.txt
        Country_import_upload_Model.xlsx
        Country&Customer_import_upload_Model.xlsx
    注意：打包后，这些文件不会放在 sys._MEIPASS 中，而是放在 exe 同目录。
    """
    base_dir = _get_exe_dir()
    return os.path.join(base_dir, filename)
