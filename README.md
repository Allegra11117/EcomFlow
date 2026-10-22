# 电商运营数据自动化平台（ecom-ops-auto）

一个面向电商运营团队的**数据自动化工作台**：把「平台导数据 → 清洗核对 → 出日报 → 异常提醒 → 内部工具查数」这条每天都要重复的链路，收敛成一条命令 / 一个页面。

- 5 张平台业务表（订单、商品、库存、推广、活动）→ 约 4 万行真实感数据，内置 15 类常见脏数据
- 1 条命令产出 **10 个 Sheet 的 Excel 日报** + 可进邮件的 HTML 摘要
- **14 条校验规则** + 异常工单闭环（去重、分级、累计次数、处理状态）
- **Streamlit 内部工具**：经营看板 / 库存预警 / 异常中心 / AI 问数 / 数据接入
- **AI 辅助**：自然语言问数（离线规则引擎 + 可选大模型）、结果自动解读、**AI 产出交叉校验**
- 28 个自动化测试，全流程 2~3 秒跑完

---

## 一、与岗位职责的对应关系

| 岗位职责 | 本项目落地方式 | 可直接验证的产出 |
|---|---|---|
| 1. 电商数据采集与处理 | `ingest.py` 支持 CSV/Excel/HTTP 接口三种接入，自动识别中文表头与 GBK 编码；`clean.py` 统一时间、状态、金额口径，全流程留痕 | `data/raw/*.csv` → `db/ecom.db`；报表「数据质量」页有每条清洗规则命中行数 |
| 2. 重复流程自动化 | `pipeline.py` 把采集/清洗/校验/落库/报表/提醒编排成一次跑批；`schedule` 支持每日定时；异常自动去重提醒 | `python main.py run` 一条命令，2.6 秒产出全部交付物 |
| 3. 内部工具开发与优化 | Streamlit 多页工作台（看板/库存/异常/问数/上传）；SQLite 数据层；模拟平台开放接口演示接口对接 | `python main.py serve`；`python main.py mock-api` |
| 4. 报表制作与基础分析 | 销售趋势与环比、平台/品类/SKU ABC 贡献、库存可售天数与补货建议、活动增量和 ROI | `output/ecom_daily_report_*.xlsx`（含图表）+ HTML 摘要 |
| 5. AI 辅助开发与应用探索 | NL2SQL（规则引擎兜底）、结果自动解读、SQL 只读护栏、AI 结果与 pandas 基准交叉校验 | `python main.py ask "近30天各平台GMV"`；`python main.py verify-ai` |
| 6. 工具维护与业务沟通 | 统一日志与批次号、失败可读报错、`--dry-run` 式的 check 子命令、工单处理状态、完整使用手册 | `docs/使用说明.md`、`logs/pipeline.log`、`pipeline_runs` 表 |

---

## 二、快速开始

环境：Python 3.11+，依赖只有 pandas / openpyxl / PyYAML / streamlit / pytest。

```bash
pip install -r requirements.txt

python scripts/gen_mock_data.py   # 生成 120 天模拟平台数据（已预置一份，可跳过）
python main.py run                # 一键跑批：采集→清洗→校验→落库→报表→提醒
python main.py serve              # 打开内部工具页面 http://localhost:8501
```

跑批完成后会看到：

```
跑批完成
  批次号   : R20261022-144748
  状态     : success  耗时 2.61s
  数据区间 : 2026-06-24 ~ 2026-10-21
  订单行数 : 40,190
  异常     : 高 4 / 中 20 / 低 3
  excel    : output/ecom_daily_report_20261022_1447.xlsx
  html     : output/ecom_daily_report_20261022_1447.html
  已通知   : console, file
```

---

## 三、常用命令

| 命令 | 作用 |
|---|---|
| `python main.py run` | 完整跑批（可加 `--no-report` / `--no-notify` / `--no-archive`） |
| `python main.py check` | 只跑采集+清洗+校验，打印清洗日志和异常清单（排查数据问题） |
| `python main.py issues` | 查看异常工单；`--level high` 只看高危 |
| `python main.py issues --set DQ0A1B2C3D4E --to fixed --note "已补数"` | 更新工单状态 |
| `python main.py ask "上月哪个品类卖得最好"` | 自然语言问数 |
| `python main.py verify-ai` | 用独立口径校验 AI 生成的 SQL 结果 |
| `python main.py report` | 不重新采集，用库内数据重建报表 |
| `python main.py schedule --at 08:30` | 常驻定时跑批（也可用 Windows 任务计划调用 run） |
| `python main.py gen-data --days 120` | 重新生成模拟数据 |
| `python main.py mock-api` | 启动模拟平台开放接口（演示接口对接） |
| `python main.py serve` | 启动内部工具页面 |

---

## 四、报表里有什么

`output/ecom_daily_report_*.xlsx`（10 个 Sheet，带样式、冻结窗格、筛选和图表）：

| Sheet | 内容 |
|---|---|
| 经营概览 | 近 30 天 GMV / 净销售 / 订单 / 客单价 / 毛利率 / 退款率，含环比、数据质量分、待处理异常数 |
| 日销售趋势 | 每日 GMV、净销售、订单、客单价、毛利率、退款率 + GMV 折线图 |
| 平台表现 | 平台 GMV/占比/客单价/毛利/退款率 + 柱状图 |
| 品类表现 | 品类结构与毛利质量 |
| SKU排行 | TOP20 + 帕累托 ABC 分类（A 类累计 70% GMV） |
| 库存预警 | 可售天数、无动销天数、状态着色、补货数量建议 |
| 活动效果 | 活动期 vs 等长基准期：提升幅度、增量 GMV、预算 ROI、结论 |
| 推广效果 | 渠道曝光/点击/CTR/CPC/CVR/ROI |
| 数据质量 | 采集日志 + 清洗日志（每条规则命中行数） |
| 异常清单 | 14 条规则的校验结果，按等级着色 |

HTML 摘要用于邮件正文，移动端可直接阅读（核心指标卡 + 平台表 + 库存预警 + 异常 Top8）。

---

## 五、项目结构

```
auto/
├── main.py                      # 统一入口（免安装）
├── config/config.yaml           # 数据源、阈值、通知渠道、AI 配置
├── src/ecom_ops/
│   ├── ingest.py                # 采集：CSV/Excel/HTTP，表头与编码适配
│   ├── clean.py                 # 清洗：时间/状态/金额/主数据，全程留痕
│   ├── validate.py              # 14 条校验规则 + 异常清单
│   ├── metrics.py               # 指标口径唯一实现（报表/页面/AI 共用）
│   ├── report.py                # Excel + HTML 报表生成
│   ├── notify.py                # 控制台/归档/飞书·钉钉·企微/SMTP
│   ├── pipeline.py              # 跑批编排、原始数据归档、落库
│   ├── db.py                    # SQLite：业务表、工单、跑批记录、AI 审计
│   ├── cli.py                   # 命令行
│   └── ai/
│       ├── nl2sql.py            # 问数：规则引擎 + 可选大模型 + SQL 护栏
│       ├── narrator.py          # 结果解读（结论可回溯到表格数字）
│       └── verify.py            # AI 产出交叉校验
├── app/dashboard.py             # Streamlit 内部工具
├── scripts/
│   ├── gen_mock_data.py         # 模拟平台导出数据（含脏数据）
│   └── mock_platform_api.py     # 模拟开放接口（零依赖）
├── tests/                       # 28 个测试
├── data/raw/                    # 平台导出原始文件
├── data/archive/                # 每次跑批的原始数据快照
├── output/                      # 报表与提醒归档
└── docs/                        # 使用说明、AI 协作说明
```

---

## 六、数据说明（模拟但贴近真实）

5 张表：`orders`（4.2 万行）、`products`（40 个 SKU）、`inventory`（每日 × SKU × 2 仓）、`traffic`（日 × 平台 × 渠道）、`campaigns`（6 个活动）。

生成时**故意注入**了平台导出最常见的 15 类问题，用来检验清洗与校验能力：

- 订单：完全重复行、同一订单号重复推送且金额不同、三种时间格式混用（`2026-10-01 10:00:00` / `2026/10/01 10:00` / `10/01/2026 10:00`）、实付缺失、金额勾稽不平、数量 ≤0、商品编码不存在、未来时间、中文/大小写混杂的状态与平台名
- 库存：负库存、安全库存缺失、重复快照、长期滞销库存、大促前备货不足导致的可售天数告警
- 流量：花费缺失、点击大于曝光、某平台某天整表断档、归因 GMV 与订单 GMV 偏差
- 商品：SKU 重复、成本缺失

---

## 七、指标口径（全项目唯一）

| 指标 | 口径 |
|---|---|
| GMV | 支付成功 + 已退款订单的实付金额（按支付时点计入） |
| 净销售额 | 支付成功订单实付 - 退款金额 |
| 退款率 | 退款金额 / GMV（异常告警按「周 × 平台」维度，避免小样本误报） |
| 毛利率 | (实付 - 成本 × 数量) / 净销售额 |
| 可售天数 | (现有库存 + 在途) / 近 30 天日均销量 |
| 补货建议 | 日均销量 × 30 天 × 1.2 - 现有 - 在途 |
| 活动提升幅度 | (活动期 GMV - 等长基准期 GMV) / 基准期 GMV |
| ROI | 活动/渠道 GMV / 预算或花费 |

口径集中在 `metrics.py`，报表、页面、AI 问数共用同一实现——避免「同名指标多个数」。

---

## 八、设计取舍

- **为什么 SQLite**：单机内部工具，零运维、随项目携带；需要多人并发时把 `db.py` 换成 MySQL/ClickHouse 即可，上层不用改。
- **为什么 AI 默认走规则引擎**：业务同学电脑上未必有 API Key，且经营数据不能因网络问题查不出来。规则引擎离线可用、结果可解释；配置 `LLM_API_KEY` 后自动优先用大模型，失败静默降级。
- **为什么要 SQL 护栏 + 交叉校验**：大模型生成的 SQL 可能「看起来对、数字错了」。`guard_sql` 强制只读单语句 + 表白名单 + LIMIT；`verify-ai` 用 pandas 独立实现比对 5 个基准问题，偏差超过 1% 判失败。
- **为什么异常要工单化**：日报里的告警如果不去重、不记录处理状态，第二天会重复轰炸，第三天线就会被忽略。工单按 `(规则, 定位)` 唯一，重复出现只累加次数，人工处理状态不被覆盖。

---

## 九、测试

```bash
python -m pytest            # 28 passed
```

覆盖：多格式时间解析、去重与金额回填、状态归一化、指标口径、库存状态判定、活动 lift/ROI、校验规则命中与去重、SQL 护栏、NL2SQL 计划、端到端跑批回归。

---

## 十、5 分钟演示脚本（面试 / 汇报）

| 步骤 | 操作 | 想说明的点 |
|---|---|---|
| 1 | `python main.py run` | 一条命令跑完采集→校验→报表→提醒，2~3 秒 |
| 2 | 打开 `output/ecom_daily_report_*.xlsx`，看「经营概览」「库存预警」「异常清单」 | 报表是自动生成的，有图表、条件着色、补货建议 |
| 3 | `python main.py check` | 出问题时怎么定位：清洗日志 + 异常清单 |
| 4 | `python main.py ask "哪些商品需要补货"` | 自然语言问数，展示生成的 SQL 和结论 |
| 5 | `python main.py verify-ai` | AI 产出的结果有独立口径交叉校验 |
| 6 | `python main.py serve` → 异常中心改工单状态 | 内部工具 + 问题闭环，不只是脚本 |
| 7 | `python main.py mock-api` + 改配置 | 接口对接能力，不是只会读 CSV |

## 十一、下一步可以做什么

1. 接入真实平台 OpenAPI（授权、限流、增量水位），把 `ingest.api` 换成真实客户端
2. 用 Airflow / Windows 任务计划替换内置 scheduler，增加失败重试和告警升级
3. 指标层迁移到 dbt + 数据仓库，支持多店铺/多品牌隔离
4. 报表增加同比、目标达成率、库存周转率等经营指标
5. 把异常工单推送到企微/飞书群并 @ 负责人，形成 SLA 计时
