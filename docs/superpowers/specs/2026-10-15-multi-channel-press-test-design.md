# 多通道压测平台 设计文档

日期: 2026-10-15
状态: 已批准 (用户确认)
作者: opencode + 用户 (cjrouter)

## 1. 背景与目标

用户运营模型服务中转站，需要本地压测工具，验证上游各模型通道的 TTFT / TPS / 成功率。
现有多通道压测脚本 `climber.py` 为命令行工具，需升级为**前后端分离的 Web 应用**，
按**业务系统标准**严格分层编码。

### 核心需求
- 前端 Vue3 填配置，后端执行压测
- 最多同时跑 5 个通道；只填 1 个也要能跑（动态添加通道，最多 5 个）
- 通道配置独立：url / api_key / model / requests / concurrency / max_tokens
- 压测为长任务，后台执行，前端轮询进度；前端切走/刷新不影响后台任务
- 保留历史记录，可回看之前任何一次测试报告
- 题库全局共用、可编辑（增删改）
- 只采集原始数据，不做攀登系数换算 / 基线判定

## 2. 技术栈

| 层 | 技术 | 说明 |
|---|---|---|
| 后端 | FastAPI (Python 3.12) | 复用现有 climber.py 压测逻辑 |
| 前端 | Vue3 + TypeScript + Vite + Element Plus + Pinia + Vue Router | 严格类型 |
| 存储 | SQLite | 题库 + 历史记录，单文件免部署 |
| 开发模式 | uvicorn --reload + npm run dev (Vite 代理 /api) | |
| 生产模式 | npm run build 产物由 FastAPI 托管 | 单进程启动 |

## 3. 后端架构 (server/)

严格分层：**router → service → repository**，各层职责单一，禁止跨层调用。

```
server/
├── main.py                 # 应用入口/装配/中间件(CORS/异常/请求日志)
├── core/
│   ├── config.py           # 配置管理(环境变量+默认值)
│   ├── logging.py          # 日志配置(滚动文件+控制台)
│   ├── exceptions.py       # 业务异常基类 + 统一异常处理器
│   └── response.py         # 统一响应体
├── routers/                # 表现层: 参数接收/路由, 不写业务逻辑
│   ├── questions.py
│   └── runs.py
├── services/               # 业务层: 用例编排/任务调度/结果落库
│   ├── run_service.py
│   └── question_service.py
├── repositories/           # 数据访问层: SQLite 操作
│   └── db.py
├── schemas/                # Pydantic DTO/校验
│   ├── question.py
│   └── run.py
├── engine.py               # 压测执行引擎(纯逻辑, 不碰 HTTP 层)
└── requirements.txt
```

### 统一响应体
所有接口返回:
```json
{ "code": 0, "message": "ok", "data": {} }
```
- code 0 = 成功; 非 0 = 业务错误码
- 错误统一走异常处理器 (400/404/500/业务码)

### 压测任务状态机
PENDING → RUNNING → SUCCESS / FAILED
- 提交即返回 run_id
- 后台线程池执行各通道压测
- 通道各自独立, 失败不影响其他通道

### 通道生效规则
只跑"填了信息的通道"。校验规则:
- 0 个通道被填 → 拒绝, 提示"至少填 1 个通道"
- 单个通道配置不完整 (缺 url/api_key/model) → 拒绝该通道或整体报错

## 4. 前端架构 (web/)

```
web/src/
├── api/                    # API 层: axios 实例 + 模块化接口
│   ├── request.ts          # 拦截器(统一处理 code/错误提示)
│   ├── questions.ts
│   └── runs.ts
├── views/                  # 页面层: 路由组件
│   ├── Home.vue            # 通道配置 + 开始测试
│   ├── Run.vue             # 实时进度 + 报告
│   └── History.vue         # 历史记录
├── components/             # 可复用组件(通道卡片/进度条/分位表...)
├── stores/                 # Pinia 状态(当前任务/通道表单)
├── router/                 # Vue Router
├── utils/                  # 工具函数
└── types/                  # TS 类型定义(与后端 schema 对齐)
```

- 统一请求封装: axios 实例 + 响应拦截器 (业务码非 0 统一弹错)
- 通道卡片动态添加, 最多 5 个; 空卡片不参与测试
- 实时进度: 每 1 秒轮询 /api/runs/<id>, 展示各通道进度
- 报告: 各通道 AVG + P1/P50/P90/P99 分位表

## 5. API 一览

```
GET  /api/questions          # 获取题库
PUT  /api/questions          # 保存题库
POST /api/runs               # 提交测试 {channels:[...]} → {run_id}
GET  /api/runs/<id>          # 轮询进度/结果
GET  /api/runs               # 历史列表
GET  /api/runs/<id>/detail   # 逐条数据
```

## 6. 数据模型 (SQLite)

### questions
- id, content(唯一), created_at

### runs
- id, name/备注, status, created_at, finished_at
- 总体统计 JSON (每通道汇总)

### run_channels (或汇总存 runs 的 JSON 字段)
- run_id, channel_name, model, url, requests, concurrency, max_tokens
- 汇总: ok/failed/ttft_avg/ttft_pcts/tps_avg/tps_pcts/success_rate

### run_results (逐条)
- run_id, channel_id, 单条 ttft/tps/tokens/error/model_reported...

## 7. 边界与错误处理

- 0 个通道 → 拒绝
- 单条超时 (timeout 默认 300s) 自动计失败
- 单通道失败不影响其他通道
- 中文题库/结果 UTF-8 落库
- 前端 token 过期/网络错误统一提示

## 8. 工程规范

- 后端: Pydantic 校验 + FastAPI OpenAPI 文档 (/docs)
- 前端: ESLint + TypeScript strict
- 命名: 接口/类/变量统一约定, 注释说明业务意图 (业务系统风格)
- 目录职责边界写进 README, 禁止越层
- 禁止硬编码: API key / url 由用户在前端填写, 存库

## 9. 交付物

- server/ 完整后端 (FastAPI, 严格分层)
- web/ 完整前端 (Vue3 + TS)
- README.md 启动/部署说明
- 迁移: climber.py 压测逻辑迁入 engine.py