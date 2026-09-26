# 反兴奋剂检测与结果管理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8301`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8301
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `athlete`：运动员；`sample`：检测样本；`case`：结果管理案件。
- `whereabouts`：运动员按季度（如 `2026-Q3`）提交的行踪申报，每期一个 60 分钟可检查时段和地点；同一运动员同一季度只有一份申报，改址通过 `amend` 追加版本，全部旧版本保留在 `whereabouts_versions` 表；`close` 关档后不能补写。
- `dispatch`：赛外检查派单。派单时按检查日期读取当时有效的申报版本（地点、时段、版本号快照进派单），地点缺失或 60 分钟时段已过一律拒绝。派单只能 `execute`（登记 `hit`/`miss`）或 `cancel`；取消不计数。
- `review`：同一运动员滚动 12 个月内第 3 次未命中自动产生的待审案件（`pending_review`），由 panel 用 `close_review` 结案。

身份角色新增 `athlete`（可申报和改址）；`inspector` 负责派单、执行和取消。

## 行踪与派单接口

- `POST /api/whereabouts`：申报，字段 `athlete_id`、`period`、`location`、`window_starts_at`（时段固定 60 分钟，须落在季度内）。
- `POST /api/entities/<id>/actions`：`amend`（改址/改时段，旧版本保留）、`close`（季度关档，之后拒绝补写）。
- `GET /api/whereabouts/effective?athlete_id=...&period=...&check_at=...`：检查员按检查日期读取有效版本。
- `GET /api/whereabouts/<id>/versions`：某份申报的全部历史版本。
- `POST /api/dispatches`：派单，字段 `athlete_id`、`period`、`check_at`；无申报、地点缺失或时段已过返回 `400`。
- `POST /api/entities/<id>/actions`：`execute`（`data.outcome` 为 `hit`/`miss`）、`cancel`（需 `reason`）。
- `GET /api/miss-count?athlete_id=...[&at=...]`：滚动 12 个月未命中累计（取消不计数）。
- `GET /api/dashboard`：版本、派单和累计次数总览（即首页表格数据）。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

身份、实验室结果和听证材料均为原型模型，不替代正式反兴奋剂信息系统或证据鉴定流程。
