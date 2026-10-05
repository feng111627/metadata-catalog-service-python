# Metadata Catalog Service

一个元数据目录服务：登记数据实体及其 schema 版本、维护表级/列级血缘、
基于血缘做变更影响分析，并在版本演进与并发访问下保证行为可预期。

## 架构

```
catalog/
  models.py         元数据模型（Entity / Schema / Field / LineageEdge / Caller ...）
  errors.py         错误类型与可区分的拒绝原因码（RejectReason）
  compatibility.py  schema 演进兼容性规则
  store.py          线程安全实体存储（乐观锁 + 写时复制）
  lineage.py        血缘图（幂等登记、循环检测、稳定查询）
  impact.py         影响分析（下游实体/字段/层级）
  audit.py          只追加审计日志
  service.py        门面：编排以上模块，统一权限与审计
  api.py            FastAPI HTTP 层
main.py             服务入口
tests/              按业务域划分的测试
```

## 元数据模型

- **Entity**：数据实体（表/视图等），由 `entity_id` 唯一标识，属于某个
  `namespace`（权限边界），携带 `lock_version`（乐观锁）与 schema 版本列表。
- **Schema / Field**：schema 是一组有序字段；字段有 `name`、`type`
  （STRING/INT/BIGINT/DOUBLE/DECIMAL/BOOL/TIMESTAMP/DATE）、`nullable`、`comment`。
- **SchemaVersion**：实体每次兼容演进追加一个版本（version 从 1 递增），
  记录创建人与时间，历史版本不可变。
- **LineageEdge**：表级血缘边 `upstream -> downstream`，可附带列级映射
  `ColumnLineage(upstream_entity, upstream_column, downstream_column)`。
- **Caller**：调用方身份 = `caller_id` + 可见命名空间集合（或管理员）。
- **AuditRecord**：`时间 / 谁 / 动作 / 目标 / 结论(SUCCESS|REJECTED:<原因>) / 细节`。

## 兼容性规则（向后兼容）

`check_compatibility(old, new)` 判断新 schema 是否可被接受，任一违例即拒绝，
原因码可区分（按字段名确定性顺序报告首个违例）：

| 变更 | 结论 | 原因码 |
|---|---|---|
| 新增 nullable 字段 | ✅ 兼容 | — |
| 类型安全 widening（INT→BIGINT/DOUBLE，BIGINT→DOUBLE） | ✅ 兼容 | — |
| 删除已有字段 | ❌ 拒绝 | `INCOMPATIBLE_FIELD_REMOVED` |
| 类型变更（非 widening 白名单） | ❌ 拒绝 | `INCOMPATIBLE_TYPE_CHANGED` |
| nullable → not-null | ❌ 拒绝 | `INCOMPATIBLE_NULLABILITY_TIGHTENED` |
| 新增 not-null 字段 | ❌ 拒绝 | `INCOMPATIBLE_REQUIRED_ADDED` |

输入校验：字段缺失（`MISSING_FIELD`）或结构非法（`INVALID_STRUCTURE`，
如空 schema、重复字段、未知类型）一律拒绝，不会被静默写入。

## 并发语义

- 存储层所有操作在同一把锁下原子完成；`get` 返回深拷贝，锁外无法篡改。
- 更新走**乐观锁**：调用方携带读到的 `lock_version`，不匹配则整体拒绝
  （`VERSION_CONFLICT`），冲突结果确定——先获得锁的一方成功。
- 更新采用**写时复制**：变更先在副本上执行，全部成功才提交；
  中途失败则存储内状态不变，不存在部分写入。
- 调用方收到 `VERSION_CONFLICT` 后重读重试即可收敛（测试验证 4 线程 ×
  25 次递增一次不丢）。

## 血缘与影响分析语义

- **幂等登记**：同一 `(upstream, downstream)` 边重复登记不产生重复边；
  列级映射按并集合并，结果与登记次数、顺序无关。
- **循环拒绝**：新边若会成环（含自环）则整体拒绝并报告 `CYCLE_DETECTED`，
  不静默去重或截断；被拒绝的边不写入。
- **查询稳定**：所有查询结果按 `(upstream, downstream)` 排序，同一血缘
  多次查询结论一致。
- **影响分析**：从变更实体沿血缘向下游传播；
  - 影响层级 = 源到该实体的**最长路径**（DAG 上确定）；
  - 指定变更列时沿列级血缘精确传播；边缺少列级映射时保守地将下游
    整表视为受影响（结果中 `columns` 为空表示整表）；
  - 结果按 `(level, entity_id)` 排序，可复现。

## 权限与审计

- 每个操作都携带 `Caller`，按命名空间控制可见范围；管理员可见全部。
- 对无权访问的实体，"不存在"与"无权限"返回**完全相同的错误**
  （HTTP 404 + 固定文案 `access denied`），无法通过错误信息探测实体是否存在。
- 血缘查询与影响分析结果按调用方可见范围过滤。
- 所有操作（无论成败）写入审计日志，可按 target/caller 查询还原
  "谁在什么时间改了什么、结论是什么"；普通调用方只能看到自己的记录。

## HTTP API

身份通过请求头注入（演示用）：`X-Caller-Id`（必填）、
`X-Caller-Namespace`（逗号分隔）、`X-Caller-Admin: true`。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/entities` | 登记实体（含初始 schema） |
| GET | `/entities/{id}` | 查询实体 |
| PUT | `/entities/{id}/schema` | schema 演进（需 `expected_lock_version`） |
| POST | `/lineage` | 登记血缘（表级 + 可选列级） |
| GET | `/lineage/{entity_id}` | 查询直接上下游 |
| POST | `/impact/{entity_id}` | 影响分析（body 可带 `columns`） |
| GET | `/audit?target=` | 查询审计记录 |

错误映射：校验失败 422，兼容性/并发/循环冲突 409（均带可区分的
`reason`），权限 404（固定文案，不暴露存在性）。

## 本地验证

```bash
# 安装依赖
uv sync

# 运行全部测试（日志开头会打印业务覆盖矩阵与各域用例数）
uv run pytest

# 只跑某个业务域
uv run pytest tests/test_compatibility.py   # 兼容性拒绝
uv run pytest tests/test_lineage.py         # 循环血缘
uv run pytest tests/test_concurrency.py     # 并发更新
uv run pytest tests/test_impact.py          # 影响分析
uv run pytest tests/test_service.py -k permission  # 权限边界

# 启动服务
uv run python main.py   # 监听 127.0.0.1:8000

# 手动冒烟
curl -s -X POST localhost:8000/entities \
  -H 'X-Caller-Id: alice' -H 'X-Caller-Namespace: finance' \
  -H 'Content-Type: application/json' \
  -d '{"entity_id":"orders","namespace":"finance","name":"orders","owner":"alice",
       "schema":{"fields":[{"name":"id","type":"INT","nullable":false}]}}'
```
