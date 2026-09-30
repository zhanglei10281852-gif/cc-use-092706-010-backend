# 城市生态运营服务

这是一个面向城市湿地保护团队的 Python 后端服务。项目提供本地 HTTP 接口、SQLite 持久化、身份与角色管理、审计记录、任务编排和可扩展的生态数据处理边界，便于在单机环境中保存运营状态并复核业务决定。

## 运行环境

- Python 3.11 或更高版本
- SQLite 3（使用 Python 标准库）

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据文件位于 `data/compute-operations.db`，可以复制 `.env.example` 后调整本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康接口为 `GET /api/system/health`。所有状态变化都写入 SQLite，并由应用内事务保证关联记录的一致性。

## 跨部门行动包与交接链

`/api/handover/packages` 提供园林、水务、志愿者等部门间的材料交接能力：

- 发起人定义行动包、材料清单（照片说明、检测数值、处置意见等）与逐项敏感字段；
- 接收方在期限内逐项 `accepted`/`rejected` 确认，支持部分接收；存在退回项时可整包退回修订，重新转交开启新交接环节；
- 每次转交生成不可覆盖的链式版本（SHA256 摘要引用上一版本），交接、确认、退回、接管记录由数据库触发器禁止改写或删除；
- 超过接收期限的交接可由持有 `handover.duty` 权限的值班人员改派（原责任环节保留）；
- 全部材料确认后行动包封存为“已完成”，禁止无痕修改，仅允许发起部门留痕更正；
- 敏感字段明文仅对发起部门与当前接收部门可见，历史参与方与版本快照均返回掩码值；
- 全部动作写入 `audit_events`，可通过 `/api/audit?resource_type=handover_package` 查询。

权限点：`handover.read`、`handover.manage`、`handover.duty`（管理员默认拥有全部权限）。


## 测试

```bash
python -m pytest
```

测试覆盖参数校验、身份权限、事务边界、任务状态、失败恢复、审计写入和现有生态计算接口。

## 编译检查

```bash
python -m compileall -q app tests
```

## 本地验收

```bash
python -m app.cli check-db
python -m app.cli smoke
```

`check-db` 检查 SQLite 完整性和外键设置，`smoke` 在进程内调用健康接口并验证基础路由。项目不依赖外部数据库、消息队列或网络服务。
