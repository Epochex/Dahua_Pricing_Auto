"""计算 FOB / DDP A / 各级渠道价
- 暴露 compute_prices(pn, classification, data) -> dict
"""
from typing import Dict
from .pricing_rules import DDP_RULES, PRICE_RULES


def compute_prices(pn: str, classification: Dict, data: Dict) -> Dict:
    """占位计算：返回带有计算字段的 dict。
    真实实现会使用 loader 提供的源表并依据 pricing_rules 执行层级计算。
    """
    base = 100.0  # placeholder base price
    margin = DDP_RULES.get('default_margin', 0.15)
    fob = round(base * (1 - margin), PRICE_RULES.get('rounding', 2))
    ddp_a = round(base * (1 + 0.05), PRICE_RULES.get('rounding', 2))
    return {
        'pn': pn,
        'classification': classification,
        'prices': {
            'base': base,
            'fob': fob,
            'ddp_a': ddp_a,
        }
    }
