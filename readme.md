配置化规则表，列是：

priority：匹配优先级（数字越小越先匹配）

field1：第一匹配字段（来自 France 表）

match_type1：匹配方式：equals / contains

pattern1：在 field1 里要匹配的内容

field2：第二匹配字段（可空）

match_type2：同上

pattern2：同上

category：映射到你 DDP_RULES / PRICE_RULES 里用的产品线名字

price_group_hint：价格组提示（大多数情况和 category 一样，方便以后扩展）

note：中文备注，方便你/PM 审核

你后面在 classifier.py 里只需要按 priority 排序，从上往下依次判断是否匹配即可。