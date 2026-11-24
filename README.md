# 洞察工坊 · 多维交互分析与实验度量平台

面向数据分析师的多维下钻分析、指标口径管理、A/B实验评估、群体圈选分群、归因分析与数据看板构建的交互分析平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              23个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 指标定义 | `metric_def` | 指标口径 | 指标编号、指标名称、计算口径 |
| 维度定义 | `dimension_def` | 分析维度 | 维度编号、维度名称、字段来源 |
| 多维查询 | `olap_query` | 查询方案 | 方案编号、查询名称、选择指标 |
| 群体圈选 | `segment_builder` | 人群包 | 人群编号、人群名称、圈选条件 |
| A/B实验 | `ab_experiment` | 实验方案 | 方案编号、实验名称、实验分组 |
| 实验分析 | `experiment_result` | 实验报告 | 报告编号、关联实验、指标名称 |
| 归因分析 | `attribution_model` | 归因模型 | 模型编号、模型名称、归因算法 |
| 看板定义 | `dashboard_def` | 数据看板 | 看板编号、看板名称、布局配置 |
| 下钻路径 | `drill_path` | 下钻方案 | 方案编号、父级维度、子级维度 |
| 同期群分析 | `cohort_analysis` | 同期群 | 群组编号、群组名称、划分周期 |
| 数据导出 | `data_export` | 导出任务 | 任务编号、数据来源、导出格式 |
| 查询缓存 | `query_cache` | 缓存条目 | 条目编号、查询哈希、结果行数 |
| 定时分析 | `scheduled_analysis` | 调度任务 | 任务编号、分析方案、Cron表达式 |
| 指标对比 | `metric_compare` | 对比方案 | 方案编号、主指标、对比指标 |
| 异常检测 | `anomaly_detect` | 检测规则 | 规则编号、目标指标、检测算法 |
| 分析快照 | `data_snapshot` | 数据快照 | 快照编号、分析方案、数据行数 |
| 分群比较 | `user_segment` | 比较方案 | 方案编号、群体A、群体B |
| 事件追踪 | `event_track` | 事件定义 | 事件编号、事件名称、事件属性 |
| 排行榜 | `ranking_board` | 排行方案 | 方案编号、排行指标、排行维度 |
| 漏斗分析 | `funnel_def` | 漏斗方案 | 方案编号、步骤序列、转化窗口 |
| 数据集关联 | `dataset_join` | 关联方案 | 方案编号、主数据集、关联数据集 |
| 报告生成 | `report_builder` | 报告模板 | 模板编号、报告名称、引用指标 |
| 有效转化批次 | `valid_conversion_batch` | 转化批次 | 批次编号、关联实验、入组人数、有效购买数 |

## 实验结案口径

实验终止即停收结案。停收前已入组的用户，其订单即使在负责人点结案之后才回传，
也要按「有效转化批次」重算，而不是丢弃或回写已冻结的结案快照：

- **分母** = 该轮次内、停收时点前完成入组的用户（入组时实际归入的轮次为准）；
- **分子** = 这些用户在轮次内、入组之后、停收之前下单的原订单，每笔只记一次；
- 停收后才入组的用户（例如 15:00 停收、15:03 才进组）进入下一轮，不进旧实验分母；
- 结案后回传的迟到转化进入**补审**，差异未处置前日报跳过该轮结果；
- 补审差异交**财务复算**后单列在日报 `corrections`，与结案口径互斥领用，来回切换不会重复计数；
- 日报 `complete` 只反映「是否有依据不完整的已结案轮次」；后续轮次尚未结案只列入 `pending_rounds`，不阻断已结案轮次的日报。

核心实现是 `backend/app/experiment_closure.py`（确定性账本）和
`backend/app/services/valid_conversion_batch.py`（平台编排）。
指标对比模块从批次取数，日报见前端「结案日报」页面。

### 结案接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/experiment-closure/rounds` | 登记一轮实验的停收时间窗 |
| POST | `/api/experiment-closure/enrollments` | 登记入组（按实际时点归轮次） |
| POST | `/api/experiment-closure/conversions` | 登记订单 / 迟到回传（原订单幂等） |
| POST | `/api/experiment-closure/close` | 结案并冻结快照 |
| GET | `/api/experiment-closure/daily-report/{实验}` | 只返回依据完整的日报 |
| POST | `/api/experiment-closure/handoff` | 把未处置差异移交财务 |
| POST | `/api/experiment-closure/finance-recalc` | 财务复算某笔原订单 |
| POST | `/api/experiment-closure/transfer` | 在结案侧 / 财务侧间移订单归属 |
| GET | `/api/experiment-closure/{实验}/claims/{原订单}` | 核对订单当前归属 |

`POST /api/ab_experiment/{id}/actions` 的「终止实验」动作会自动结案账本里已停收的那一轮。

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据分析与数据应用
