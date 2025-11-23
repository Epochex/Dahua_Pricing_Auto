import os
import sys

# =========================
# 基本信息（直接沿用你原来的）
# =========================

APP_TITLE = "大华法国驻地专用自动化 报价计算|查询|信息汇总 mini软件"
DATA_DATE = "2025.11.13"
AUTHOR_INFO = (
    "当前仍处于测试阶段,若对数据产生疑问请立刻联系对应PM和技术人员进行价格核对\n"
    "对于硬盘存储类设备，近期价格波动频繁，请注意实时报价\n"
    "使用中发现任何问题，请联系开发人员 林建克 LIN Jianke\n"
    "Huachat: Jianke LIN | 微信: Epochex404"
)


# =========================
# 数据路径获取
# =========================

def get_data_path(filename: str) -> str:
    """
    返回 data 目录下某个文件的绝对路径。

    - 本地开发：项目结构假定为
        根目录/
            core/
            data/
            config.py
    - PyInstaller 打包：
        dist/xxx/
            data/FrancePrice.xlsx
            data/SysPrice.xls
            ...
      启动时 sys._MEIPASS 指向运行目录，把 data 一起打进去即可：
        pyinstaller main.py --add-data "data;data"
    """
    if hasattr(sys, "_MEIPASS"):
        # 打包后的运行目录
        base_dir = sys._MEIPASS  # type: ignore[attr-defined]
    else:
        # 源码运行：config.py 所在目录就是项目根目录
        base_dir = os.path.dirname(os.path.abspath(__file__))

    data_dir = os.path.join(base_dir, "data")
    return os.path.join(data_dir, filename)
