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

## 跨部门行动包与交接链

当园林、水务、志愿者团队共同处置滩涂异常时，通过 `/api/handoff/packages` 完成带责任留痕的材料交接：

- 发起人定义材料清单（照片说明、检测数值、处置意见等）与敏感字段、接收部门和期限；
- 接收方在期限内逐项确认（支持部分接收），或整体/逐项退回；
- 退回后发起方重新提交会生成**不可覆盖的新版本**（`(package_id, version_no)` 唯一约束 + SHA-256 清单摘要，版本查询返回 `digest_verified` 用于识别篡改）；
- 全部确认通过后可转交到下一部门，交接链按序号记录每次发送方、接收方、期限、确认人、退回原因；
- 超过期限未完成的环节，值班调度员（`handoff.dispatch` 权限、`duty_officer` 角色）可在 `/api/handoff/packages/overdue` 发现并重新分派；
- 行动包完成即封存（`locked=1`），任何确认、退回、修订、转交、改派都会被拒绝，拒绝事件独立写入审计（`handoff.locked.rejected`），无法无痕修改。

权限：`handoff.read`（查看本部门参与的交接）、`handoff.write`（发起与办理）、`handoff.dispatch`（跨部门超时改派）。验收用例见 `tests/test_handoff.py`，覆盖部分接收、退回修订、重复确认、超时接管、权限隔离和审计查询。
