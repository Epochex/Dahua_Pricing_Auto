"""产品线 / 系列识别逻辑（只负责分类）
- 提供 classify(pn, data) -> dict（例如 {'productline': 'PL1', 'series': 'S1'}）
"""

def classify(pn: str, data: dict) -> dict:
    """简单分类示例：尝试在 productline_map 中匹配 PN。
    返回包含分类标签的 dict。
    """
    # 这是一个占位实现
    if not pn:
        return {'productline': None, 'series': None}
    # 伪规则：以字母开头归为 "A" 产品线，否则为 "Others"
    if pn[0].isalpha():
        return {'productline': 'Alpha', 'series': 'Default'}
    return {'productline': 'Numeric', 'series': 'Default'}
