"""读取 Excel + 基本字段标准化
- 暴露 load_all() 返回字典化数据集
"""
import os
from typing import Dict

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')


def load_all() -> Dict:
    """加载 data/ 下的所有源表，返回一个简单结构供其它模块使用。
    这里为示例 stub：真实项目建议使用 pandas.read_excel 并做字段标准化。
    """
    # TODO: replace with real Excel parsing
    return {
        'france_price': [],
        'sys_price': [],
        'productline_map': [],
    }
