# Dahua France Pricing Mini Tool

大华法国驻地专用 **自动化 报价计算｜查询｜信息汇总 mini 软件**  
通过本地 Excel 价格表与产品线映射规则，实现对 PN（Part No.）的半自动定价和批量导出。

---

## 1. 功能概述

- **单条查询**
  - 输入一个 Part No.，在 France / Sys 价格表中自动匹配；
  - 自动识别产品线与系列（category / price group / series）；
  - 根据规则补全缺失的 FOB / DDP A / 渠道价；
  - 以表格形式展示，并标记 **Original** / **Calculated** 字段。

- **批量处理**
  - 读取根目录 `List_PN.txt` 中的多个 PN；
  - 对每个 PN 执行同样的匹配与计算逻辑；
  - 将结果导出为 Dahua Portal 上传模板：
    - `Country_import_upload_Model.xlsx`
    - `Country&Customer_import_upload_Model.xlsx`

- **智能匹配**
  - 支持带国际后缀的 PN（如 `-9001` / `-002`）与无后缀 PN 自动对应；
  - France / Sys 双侧联合判断产品线；
  - 特别处理 **Accessories / Accessory** 避免被误判为 IPC；
  - 对无法识别的产品线输出警告，要求人工介入。

---

## 2. 项目结构

```text
.
├── main.py                        # 主入口：交互式查询 / 批量模式
├── config.py                      # 全局配置与路径工具
├── export.py                      # 辅助脚本：导出所有 .py 文件到一个文本（调试用）
├── core
│   ├── loader.py                  # 载入 Excel / CSV
│   ├── classifier.py              # 产品线 / 价格组 / 系列 识别
│   ├── pricing_rules.py           # DDP_RULES & PRICE_RULES 定价规则常量
│   ├── pricing_engine.py          # 定价核心引擎：FOB / DDP / 渠道价计算
│   └── formatter.py               # 控制台输出格式与统一“价格取整规则”
├── data
│   ├── FrancePrice.xlsx           # 法国价格表（FOB / DDP / 渠道价 原始数据）
│   └── SysPrice.xls               # Sys Price 表（Min / Area / Standard Price 等）
├── mapping
│   ├── productline_map_france_full.csv   # France 侧产品线映射规则
│   └── productline_map_sys_full.csv      # Sys 侧产品线映射规则
├── List_PN.txt                    # 批量模式输入 PN 列表（每行一个）
└── output (运行后生成)
    ├── Country_import_upload_Model.xlsx
    └── Country&Customer_import_upload_Model.xlsx
```

## 3. 运行环境与依赖

本工具基于 Python 生态构建，推荐使用 Python 3.10+（已在 Python 3.12 环境充分验证）。运行依赖主要为四类：数据处理（pandas）、Excel 读写（openpyxl / xlrd）、控制台表格渲染（tabulate）。其中 FrancePrice.xlsx 为 xlsx 格式，SysPrice.xls 为旧版 Excel 格式，因此 openpyxl 与 xlrd 缺一不可。安装方式如下：

pip install pandas openpyxl xlrd tabulate

程序目录无需特殊结构，只需确保 data/ 和 mapping/ 子目录中的 Excel 与 CSV 文件保持最新版本，即可在任意操作系统上独立运行。

---

## 4. PN 标准化与匹配策略

为了处理不同文件、不同销售区域、不同阶段产生的料号差异，本工具采用“双层标准化”策略：raw key 与 base key。raw key 用于精确匹配，保持输入字符串的本来面貌（去空格、统一大小写）；base key 则负责去除国际后缀（如 -9001、-002、-S1 等），以捕捉 PN 的主体结构。

在查找 PN 时，程序按如下顺序匹配：  
1）尝试 FrancePrice 与 SysPrice 的精确匹配（raw 层）；  
2）若失败且输入带后缀，则自动退回其 base key 并重新查找；  
3）若输入无后缀，则自动扩展匹配所有带后缀的 France/Sys 记录；  
4）仍无法命中任何条目时，返回“未匹配”，并在查询结果中提示。

这种匹配策略允许用户在查询时不必精确记住后缀，也能对比 France 与 Sys 两侧的数据结构一致性。

---

## 5. 产品线识别（Category / Price Group / Series）

产品线识别由 classifier.py 实现，主要流程包括：  
1）优先依据 France 侧映射文件 productline_map_france_full.csv，通过 Catelog Name 和 First/Second Product Line 映射出 category 与 price group；  
2）若 France 侧无法识别，则回退至 Sys 侧映射 productline_map_sys_full.csv；  
3）对 Accessories/Accessory Cable 执行独立纠偏：只要 Sys 显示其所属附件类，即强制分类为 ACCESSORY 或 ACCESSORY 线缆，避免掉入默认 IPC 类；  
4）在仍无法识别（或映射结果为 UNKNOWN）的情况下，程序不会套用 IPC 默认规则，而是停止自动计算并提示人工介入。

Series 检测则基于 Series 字段（France/Sys 均可提供），用于选择 PRICE_RULES 中对应子规则；若无法识别，则回退到 "_default_"。在未匹配 Series 时，全流程仍可继续进行。

---

## 6. 定价引擎（FOB / DDP A / 渠道价）

定价主流程位于 pricing_engine.py，操作逻辑如下：

（1）优先收集 France 侧所有原始价格字段（FOB、DDP A、Reseller/Gold/Silver/Installer/MSRP）。若 France 在该产品上提供了这 7 个关键价格字段，则视为“定价完整”，全程不做任何计算，也不读取 Sys 数据，这类值均标记为 Original。

（2）若 France 缺少 FOB，则尝试从 Sys 侧读取 Min / Area / Standard Price，按 France 的 Sales Type 选择对应的基准价格，再乘以 0.9 得到 FOB（无 round）。若此步骤失败（例如 Sys 本身也无价格），FOB 将保持 None，后续定价链路也会被自动中断。

（3）使用 category 对应的 DDP_RULES 计算 DDP A；不同产品线的参数项（如 10%、1.1%、2%、0.000198 等）可灵活调整，所有中间值保留全精度浮点，不执行四舍五入。若 France 已提供 DDP A，则使用其原始值。

（4）依据 price group 与 series，从 PRICE_RULES 中选择渠道价规则，对 DDP A 计算 Reseller/Gold/Silver/Installer/MSRP。计算公式统一为：渠道价 = DDP A ÷ (1 - 折扣)。所有自动生成的值被标记为 Calculated。

（5）若 category 无法识别（UNKNOWN），程序会立即终止当前 PN 的计算，只返回原始字段，并要求用户人工处理，避免产生错误定价。

---

## 7. 统一价格格式化策略（formatter）

为了实现 France 定价体系在展示端和批量导出端的一致性，工具强制采用以下取整策略，仅作用于 “Calculated 字段”，而不会改变 Original 字段：

- 价格 < 30 欧：保留 1 位小数（ROUND_HALF_UP），用于保证小额件的一致性精度；
- 价格 ≥ 30 欧：四舍五入到整数（ROUND_HALF_UP），符合门户录单习惯；
- MSRP 等特殊字段适用相同策略，无例外情况。

在控制台显示与 Excel 导出时，formatter 会一次性为所有 Calculated 字段应用格式化，但内部计算链路保留全精度数值，使定价计算在不同环境中保持可重复性。

---

## 8. 批量导出流程

批量模式将所有 PN 封装成统一结构（build_export_df），并根据 Dahua Portal 模板字段要求生成两份 Excel：

1）Country_import_upload_Model.xlsx  
2）Country&Customer_import_upload_Model.xlsx

每条记录包含 Product Line、Internal Model、External Model、PN、Sales Status、Description、Series、Single Value（即 DDP A）、渠道价等字段。所有 Calculated 字段在进入 Excel 前都会经过统一格式化。Original 字段则保持原始展示方式。

若某 PN 在 France / Sys 两侧均未识别出产品线，则其相应行会被标记为人工复核项，但仍保留在导出文件中，便于统一审核。

---

## 9. 调试与辅助工具

项目提供 export.py，用于在开发或排查复杂逻辑时将所有 .py 文件合并输出为一个整体文本，便于向同事或外部技术人员展示完整逻辑。该工具不会影响主流程，也不会修改任何运行环境。

程序在运行过程中会自动打印匹配模式、产品线识别路径、是否使用 Sys 补全 FOB、是否命中 DDP_RULES 或 PRICE_RULES 等信息，便于定位异常情况。

---

## 10. 注意事项与最佳实践

- **France Price 的优先级永远高于 Sys Price**。只要 France 提供了某字段的原始值，就会覆盖所有自动计算逻辑。  
- Accessories 类产品应保证 France 与 Sys 产品线映射文件持续同步，否则可能导致分类偏差，但程序已内置纠偏。  
- 批量处理前建议人工检查 List_PN.txt，避免无效字符、空行或重复项降低处理效率。  
- 所有生成的 Excel 均仅包含必要字段，若需附加内部注释或辅助列，建议在导出后手动添加，避免影响下次自动处理。  
- 对于价格较敏感类（如存储介质、商显、VDP 等）的产品线，建议及时同步映射规则与 DDP/价格策略，以保证工具长期准确性。  

---

## 11. 压缩打包代码
```python
pyinstaller --onefile --name DahuaPricingTool --add-data "data;data" --add-data "mapping;mapping" main.py
```
