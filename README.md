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
- `whereabouts`：季度行踪申报；`dispatch`：赛外检查派单。

## 行踪申报与派单规则

- 运动员每期提交一个本季度的60分钟可检查时段（`slot_date`+`slot_start`）和地点；每运动员每季度仅一份申报。
- 改址通过`amend`动作完成，旧版本保留在`versions`中，版本号递增；只能提交/修改当前季度，季度关档（季度结束）后不能补写或改址。
- 创建派单（`dispatch`）时按检查日期读取该季度申报的有效版本（检查日期当天及之前最近提交的版本），并把版本号、时段和地点快照写入派单；地点缺失或时段已过（检查日期/时刻晚于时段结束）则拒绝派单，原运动员名单不受影响。
- 派单状态机：`dispatched`→`executed`→`missed`，或`dispatched`→`cancelled`。只有实际执行（`executed`）的检查才能登记未命中；取消不计入未命中。
- 同一运动员在任意未命中日期往前12个月（365天）内累计第3次未命中时，系统自动以`system`身份开立`source=whereabouts`的案件进入待审；已有待审案件时不重复立案。
- `GET /api/whereabouts/summary/<athlete_id>`返回申报版本、派单和近12个月/累计未命中次数，演示页面据此展示。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/whereabouts/summary/<athlete_id>`：行踪申报版本、派单与未命中累计。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

身份、实验室结果和听证材料均为原型模型，不替代正式反兴奋剂信息系统或证据鉴定流程。
