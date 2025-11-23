"""纯配置：DDP_RULES / PRICE_RULES 等
注意：此文件只保存规则/映射，不包含计算逻辑。
"""
# 示例规则结构 — 按项目需要扩展
DDP_RULES = {
    'default_margin': 0.15,
    'country_adjustments': {
        'FR': 0.05,
    }
}

PRICE_RULES = {
    'channels': ['fob', 'ddp_a', 'distributor'],
    'rounding': 2,
}
