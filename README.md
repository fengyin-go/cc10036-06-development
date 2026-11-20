# 多维分析查询与指标平台

面向数据分析师和业务用户，提供多维聚合查询、指标口径、分群圈选和数据集市的数据分析平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
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
| 指标定义 | `metric_def` | 指标 | 指标编码、指标名称、业务域 |
| 多维立方 | `cube_builder` | OLAP立方 | 立方名称、事实表、维度列表 |
| 切片查询 | `slice_query` | 查询模板 | 模板名称、数据立方、查询维值 |
| 分群圈选 | `segment_rule` | 分群规则 | 分群名称、人群类型、筛选条件 |
| 看板布局 | `dashboard_layout` | 看板配置 | 看板名称、所属业务域、图表布局 |
| 即席导出 | `adhoc_export` | 导出任务 | 任务名称、查询SQL、导出格式 |
| 报表订阅 | `report_subscription` | 订阅配置 | 订阅名称、报表对象、推送周期 |
| 异常检测 | `anomaly_detect` | 检测规则 | 规则名称、监控指标、检测算法 |
| 下钻路径 | `drill_path` | 下钻配置 | 路径名称、起始维度、下钻层级 |
| 语义层 | `semantic_layer` | 语义定义 | 语义名称、物理表、字段映射 |
| 查询加速 | `query_accelerator` | 加速配置 | 配置名称、目标查询、加速方式 |
| 归因模型 | `attribution_model` | 归因配置 | 模型名称、归因对象、归因窗口 |
| 同期群分析 | `cohort_analysis` | 群组配置 | 群组名称、初始事件、回访事件 |
| 数据血缘 | `data_lineage` | 血缘节点 | 节点名称、节点类型、上游列表 |
| 查询调度 | `query_scheduler` | 调度计划 | 计划名称、查询语句、执行周期 |
| 结果缓存 | `result_cache` | 缓存条目 | 缓存键、查询描述、结果行数 |
| 数据范围 | `olap_scope` | 范围规则 | 规则名称、数据范围、授权对象 |
| 实验分析 | `ab_analysis` | 实验配置 | 实验名称、对照组、实验组 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 转化归因结算与同期群对账

归因模型负责给出「触点权重怎么分」，同期群负责给出「按哪个周期看渠道贡献」，
两者通过转化归因数据串联，口径都写在输出里，不靠约定俗成。

- 归因模型收敛口径：`GET /api/analytics/attribution/caliber/{model_id}`
  （模型类型、归因窗口天数、衰减半衰期、U 型首末权重、留存周期、结算基数）。
- 结算 + 渠道贡献：`POST /api/analytics/attribution-settlement`，入参
  `{model_id, config_id, model_overrides, events?, touchpoints?, max_periods?}`。
  `model_overrides` 可临时改 `window_days`、`settle_basis`、`period_days` 等；
  不传 `events`/`touchpoints` 时用内置跨期样例：同一笔转化落在第二个留存周期，
  两个合格触点分别在首期和第二期。
- 结算账单：`GET /api/analytics/attribution/bills`，每次重算都会留存一条账单并
  记录当次使用的模型条件；`settlement.bill_id` 指向本次账单。
- 归因模型页与同期群分析页底部会显示同一个结算工作台，可切换模型、窗口阈值和
  周期口径后重算。

结算规则：

1. 只有 `touch_time <= conversion_time` 且差值不超过归因窗口的触点合格；
   晚于转化或超出窗口的触点计入 `excluded_touchpoints`，不参与权重分配。
2. 同一 `conversion_id`（同一回访）重复提交只结算一次，计入
   `totals.duplicate_submissions`。
3. `settle_basis=return_week`（运营按回访周复盘）把整笔转化的权重计入转化所在周期；
   `settle_basis=touch_week`（渠道按触达周结算）把权重拆到各触点所在周期。
   两种口径分母相同，都是「同期群初始事件用户数（去重）」，因此只改变列归属。
4. 渠道贡献矩阵的周期轴由 `period_days` 决定（7 天显示为「周N」），
   `reconcile` 段给出权重合计、分母与当次模型条件，可直接按账单对账。

测试：`cd backend && .venv/bin/python -m unittest discover -s tests -v`

领域：数据工程
子方向：数据分析与数据应用
