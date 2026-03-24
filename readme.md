# 大华法国驻地专用产品定价自动化平台

> [!IMPORTANT]
> 这份文档面向后续维护人员。
> 本文不再维护价格公式大表，也不再作为人工定价 SOP 使用。
> 价格公式、涨价百分比、渠道系数以代码默认值和运行时规则文件为准，README 只保留业务流程、系统架构、运行原理、日常使用和维护方式。

## 1. 平台定位

这是一个面向大华法国驻地定价场景的自动化平台，用来完成以下工作：

- 按 PN 查询产品价格来源与自动计算结果
- 在 France 价格表缺失时，用 Sys 价格表反算 FOB 并继续推导渠道价
- 批量导出可直接上传到 GSP 的 `Country_import_upload_Model.xlsx`
- 在线维护 DDP 规则、渠道规则、Sys FOB Adjust、关键词额外涨价规则
- 对同一 `External Model` 的产品集群做对齐、导出和复核
- 对产品线映射表做审计、重建和持久化

这套系统的目标不是替代业务判断，而是把“取数、分类、补算、导出、追溯”自动化，让维护者把精力放在规则校验和异常处理上。

## 2. 业务流程

### 2.1 日常使用流程

1. 销售或市场提出定价需求，提供一个或一批 PN。
2. 在 `QUERY` 页先查单个 PN，确认：
   - France 是否已有定价
   - Sys 是否命中
   - 自动识别到的产品线、子线、系列是否正确
   - 价格是否来自 France、Sys 反算，还是 External Model 锚定
3. 如果需要批量处理，在 `BATCH` 页上传 txt/csv/xlsx/xls，等待后台线程产出导入模板。
4. 如果自动计算逻辑不符合当前业务规则，在 `RULES` 或 `KEYWORD` 页修改规则并保存重载。
5. 导出生成的 `Country_import_upload_Model.xlsx`，再导入 GSP。
6. 对异常 PN 做人工复核，例如：
   - 产品线识别不对
   - France/Sys 都无数据
   - 命中了不该命中的关键词涨价
   - External Model 集群中某些型号价格需要参考法国侧锚点

### 2.2 平台处理原则

- France 国家侧价格优先。
- France 缺失或价格层级不全时，才使用 Sys 侧底价补算。
- Sys 反算 FOB 时，可能再叠加：
  - `Adjust` 列对应的 Sys FOB adjust
  - `KEYWORD` 页配置的关键词额外涨价
- 查询与批量导出都保留追溯信息，方便维护者判断价格来源。

## 3. 仓库结构

```text
/data/Dahua_Pricing_Auto
├── backend/
│   ├── app/main.py                # FastAPI 入口，API、批量任务、规则持久化
│   └── engine/
│       ├── engine.py              # PricingEngine 壳层
│       └── core/
│           ├── loader.py          # 读取 France/Sys Excel、mapping CSV，建立索引
│           ├── classifier.py      # 产品线识别、系列识别、强制业务修正
│           ├── pricing_engine.py  # 单个 PN 的核心计算链路
│           ├── pricing_rules.py   # 默认 DDP_RULES / PRICE_RULES
│           └── formatter.py       # 导出模板格式化与分段取整
├── frontend/
│   ├── src/App.jsx                # 单页前端，QUERY/BATCH/RULES/KEYWORD/META
│   └── dist/                      # 构建后的静态文件，由 nginx 提供
├── deploy/
│   ├── nginx/                     # nginx 站点配置
│   ├── systemd/                   # dahua-pricing-backend.service
│   └── scripts/                   # 持久化部署、mapping 重建和审计脚本
├── mapping/                       # 仓库内默认 mapping CSV
├── script/                        # 常用重启脚本
├── requirements.txt
└── readme.md
```

> [!NOTE]
> 当前线上主链路以 `backend/` + `frontend/` + `deploy/` + `runtime` 为准。
> 仓库根目录里仍保留了一些历史文件，例如 `main.py`、`gui_app.py`、`export.py`、模板 xlsx、旧 PDF，这些不是当前 Web 平台的核心入口，维护时优先看本 README 中列出的主链路文件。

## 4. Runtime 结构

线上运行时数据不放在仓库里，而是放在：

```text
/data/dahua_pricing_runtime
├── data/
│   ├── FrancePrice.xlsx
│   └── SysPrice.xlsx
├── mapping/
│   ├── productline_map_france_full.csv
│   └── productline_map_sys_full.csv
├── admin/
│   ├── ddp_rules.json
│   ├── price_rules.json
│   ├── uplift.json
│   └── keyword_uplift.json
├── uploads/                       # 批量任务原始上传文件
├── outputs/                       # 单查导出、批量导出、external model 导出
└── logs/                          # 任务日志、mapping 审计结果
```

### 4.1 哪些内容是“线上生效值”

请牢记下面的优先级：

1. 代码中的 `backend/engine/core/pricing_rules.py` 是默认规则。
2. 运行时 `admin/*.json` 是当前线上覆盖值。
3. 服务启动时会自动加载 `admin/*.json` 覆盖代码默认值。
4. 因此，线上规则以 `/data/dahua_pricing_runtime/admin/*.json` 为准，不以 README 为准，也不一定等于源码默认值。

这也是为什么：

- 改 `RULES` / `KEYWORD` 页后，即使不改源码，也会立刻影响线上计算。
- 只改源码里的默认规则，如果不重启后端，线上不会立刻更新。

## 5. 系统架构与运行原理

### 5.1 整体架构

```text
Browser
  -> nginx
    -> frontend/dist 静态页
    -> /api/* 反代到 FastAPI
FastAPI
  -> PricingEngine
    -> 加载 France/Sys Excel
    -> 加载 France/Sys mapping CSV
    -> 加载 admin/*.json 规则覆盖
    -> 单查 / 批量 / 规则维护 / external model cluster
Runtime
  -> data/ mapping/ admin/ uploads/ outputs/ logs/
```

### 5.2 启动时做了什么

后端启动时会在 `backend/app/main.py` 的 `startup` 阶段完成：

1. 创建 runtime 目录结构。
2. 读取 `admin/uplift.json`、`admin/keyword_uplift.json`、`admin/ddp_rules.json`、`admin/price_rules.json`。
3. 构建 `PricingEngine`。
4. 从 `runtime/data` 读取 `FrancePrice` 和 `SysPrice`。
5. 从 `runtime/mapping` 读取 France/Sys 两套产品线映射表。
6. 为 France/Sys 两张价格表构建 PN 原始索引和 base PN 索引。

### 5.3 单个 PN 的计算链路

单查的核心逻辑在 `backend/engine/core/pricing_engine.py`，顺序大致如下：

1. 先按原始 PN 精确匹配 France / Sys。
2. 如果精确匹配失败，再尝试同基底 PN fallback。
3. 根据 France/Sys 行和 mapping 识别：
   - `category`
   - `price_group`
   - `series_display`
   - `series_key`
4. 先取 France 现成价格；France 缺失时，才从 Sys 补基础字段。
5. 如果 France 的 `FOB C(EUR)` 缺失，才允许按 Sys 的销售层级底价反算 FOB。
6. 反算 FOB 时，可能继续叠加：
   - `Adjust` 规则
   - `KEYWORD` 关键词涨价
7. 用 DDP 规则计算 `DDP A(EUR)`。
8. 用渠道规则继续推导：
   - Reseller
   - Gold
   - Silver
   - Ivory
   - MSRP
9. 返回价格字段 + 诊断元信息，供前端展示。

### 5.4 关键业务规则

- France 价格优先于 Sys。
- Sys 侧只在 France 的 FOB 缺失时才参与 FOB 反算。
- `Adjust` 和关键词涨价只在“France FOB 缺失 + Sys 反算 FOB”场景生效。
- 外部型号集群模式会把同一 `External Model` 的产品一起列出来。
- 如果法国侧存在同 `External Model` 且价格完整的锚点，集群模式可用法国锚点覆盖其余型号价格。
- 批量导出统一写成 country 模板结构。
- 导出时会做本地分段格式化：
  - `< 30` 保留 2 位小数
  - `>= 30` 四舍五入取整

### 5.5 为什么需要 mapping

平台不是单靠字符串硬编码识别产品线，而是通过两套 mapping 表：

- `productline_map_france_full.csv`
- `productline_map_sys_full.csv`

做 France / Sys 各自的分类映射。`classifier.py` 还会在 mapping 之外做少量强制业务修正，例如：

- 人行道闸统一转到 `ACCESS CONTROL`
- 录像机类目（NVR / IVSS / EVS / XVR）的兜底识别
- IPC、PTZ、Thermal 等产品的系列识别

因此，当识别结果不正确时，优先检查 mapping，而不是先改 README。

## 6. 页面说明

### 6.1 QUERY

用途：

- 单 PN 查询
- 查看价格来源与诊断信息
- 手动指定产品线重算
- 导出单个 PN 的上传模板
- 进入 `External Model` 扩展索引模式

维护人员最需要关注的字段：

- `法国国家侧匹配`
- `系统侧匹配`
- `计算状态`
- `计算层级`
- `Calc Layer (Sys)`
- `Sys Basis Price Used`

### 6.2 BATCH

用途：

- 上传 txt/csv/xlsx/xls 批量计算
- 后端用线程异步执行
- 任务状态落盘到 `outputs/{job_id}/state.json`
- 结果导出到 `outputs/{job_id}/Country_import_upload_Model.xlsx`

### 6.3 RULES

用途：

- 维护 DDP 规则
- 维护价格渠道规则
- 维护 Sys FOB Adjust

保存逻辑：

- `SAVE+RELOAD` 会写入 `runtime/admin/*.json`
- 保存后规则立即进入内存生效
- 不需要改源码里的规则表

### 6.4 KEYWORD

用途：

- 维护关键词额外涨价规则
- 预览受影响 PN

当前逻辑：

- 只在 `Internal Model / External Model` 上做关键词命中
- 只在 Sys 反算 FOB 的场景生效
- Preview 只展示价格真正发生变化的 PN

### 6.5 META

用途：

- 查看当前引擎加载状态
- 查看 France/Sys 数据更新时间
- 查看当前使用的数据文件和 mapping 文件路径

这页适合在更新数据或重启服务后做第一轮确认。

## 7. 日常维护操作

### 7.1 更新 France / Sys 数据表

1. 用新的价格表替换：
   - `/data/dahua_pricing_runtime/data/FrancePrice.xlsx`
   - `/data/dahua_pricing_runtime/data/SysPrice.xlsx`
2. 重启后端：

```bash
cd /data/Dahua_Pricing_Auto
bash script/restart_backend.sh
```

3. 打开 `META` 页确认新的更新时间已经变化。

### 7.2 更新 mapping

如果只是微调少量规则，可以直接修改：

- `/data/dahua_pricing_runtime/mapping/productline_map_france_full.csv`
- `/data/dahua_pricing_runtime/mapping/productline_map_sys_full.csv`

然后重启后端。

如果要基于当前价格表重新生成 mapping，可使用：

```bash
cd /data/Dahua_Pricing_Auto
python3 deploy/scripts/rebuild_mapping_from_prices.py
python3 deploy/scripts/mapping_audit.py
```

说明：

- `rebuild_mapping_from_prices.py` 用现有数据和历史标签重建建议版 mapping
- `mapping_audit.py` 用来审计未知类目、严格不匹配和摘要
- 审计产物会写到 `runtime/logs/mapping_audit/`

### 7.3 更新规则

优先级建议如下：

- 日常规则维护：直接在前端 `RULES` / `KEYWORD` 页改
- 版本基线调整：再同步回源码默认值

线上当前规则文件：

- `/data/dahua_pricing_runtime/admin/ddp_rules.json`
- `/data/dahua_pricing_runtime/admin/price_rules.json`
- `/data/dahua_pricing_runtime/admin/uplift.json`
- `/data/dahua_pricing_runtime/admin/keyword_uplift.json`

### 7.4 重启服务

后端重启：

```bash
cd /data/Dahua_Pricing_Auto
bash script/restart_backend.sh
```

前端重建并重启 nginx：

```bash
cd /data/Dahua_Pricing_Auto
bash script/restart_frontend.sh
```

前后端一起重启：

```bash
cd /data/Dahua_Pricing_Auto
bash script/restart_all.sh
```

### 7.5 持久化部署

如果要在服务器上重新部署：

```bash
cd /data/Dahua_Pricing_Auto
sudo bash deploy/scripts/deploy_persistent.sh
```

部署脚本会完成：

- 安装 nginx / python venv
- 准备 runtime 目录
- 同步 mapping 到 runtime
- 构建 frontend
- 安装 systemd 服务
- 安装 nginx 站点配置

## 8. 使用者与维护者需要知道的“来源判断”

当某个价格看起来不对时，请先判断它来自哪条链路：

- France 原价
- Sys 反算 FOB
- Sys 反算 FOB + Adjust
- Sys 反算 FOB + Keyword
- External Model 法国锚点覆盖
- 手动重算覆盖

这套平台的很多“看起来像 bug”的现象，实际上都是来源判断没看清楚导致的。先看 Query 页的诊断块，再决定要改数据、改 mapping，还是改规则。

## 9. 常见问题

### 9.1 改了规则但价格没变

先区分是哪种规则：

- `RULES` / `KEYWORD` 页修改后，正常应立即写入 runtime 并生效
- 如果你是直接改源码默认值，则必须重启后端
- 如果这条 PN 本来就走 France 原价，而不是 Sys 反算 FOB，那么 `Adjust` / 关键词涨价不会生效

### 9.2 为什么 query 里是 `Calculated(Sys)`，但某些涨价没叠加

`Calculated(Sys)` 只说明结果是通过 Sys 链路补算出来的，不代表一定命中了 `Adjust` 或关键词规则。还需要看：

- `sys_uplift_key`
- `sys_keyword_uplift_hits`
- `sys_keyword_uplift_pct`

### 9.3 为什么会出现 502

最常见原因是：

- 重启后端或 nginx 的窗口期
- 后端还没完成启动时，nginx 已经开始转发请求

通常等几秒后再刷新即可。如果持续 502，再检查：

```bash
systemctl status dahua-pricing-backend.service --no-pager
systemctl status nginx --no-pager
```

### 9.4 为什么 README 里不再放公式大表

因为公式和百分比会持续迭代，而 README 极易过期。当前系统的真实生效值来自：

1. 源码默认规则
2. runtime/admin 覆盖规则

把整套大表复制进 README，只会制造第二份容易过期的“伪真相”。

## 10. 维护建议

- 把 README 当成“系统说明书”，不要把它当规则数据库。
- 把规则文件当成“线上当前状态”，不要只盯着源码默认值。
- 先看 Query 页诊断，再决定改 mapping、改规则还是换数据表。
- 大批量规则变更前，先用 `KEYWORD preview` 或小批量 `BATCH` 做抽样验证。
- 每次更新数据表后，先看 `META` 页确认更新时间，再开始查询。

## 11. 关键文件索引

- 后端入口：`backend/app/main.py`
- 引擎壳层：`backend/engine/engine.py`
- 数据加载：`backend/engine/core/loader.py`
- 分类与系列识别：`backend/engine/core/classifier.py`
- 核心计算链路：`backend/engine/core/pricing_engine.py`
- 默认规则：`backend/engine/core/pricing_rules.py`
- 导出格式：`backend/engine/core/formatter.py`
- 前端页面：`frontend/src/App.jsx`
- 持久化部署：`deploy/scripts/deploy_persistent.sh`
- 重启脚本：`script/restart_backend.sh`、`script/restart_frontend.sh`、`script/restart_all.sh`
