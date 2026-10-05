# Metadata Catalog Service

一个基于 FastAPI 的元数据目录服务，支持数据实体与 schema 版本登记、表级/列级血缘维护、
基于血缘的变更影响分析，并在版本演进与并发访问下提供可预期的行为。

## 元数据模型

| 概念 | 说明 |
| --- | --- |
| `DataEntity` | 数据实体（table / view / stream），属于某个 `namespace`，持有完整 schema 历史 |
| `SchemaVersion` | 不可变的 schema 版本，由若干 `Field(name, type, required)` 组成，版本号从 1 单调递增 |
| `revision` | 实体的乐观并发令牌，每次成功变更 +1，更新时必须携带期望值 |
| `LineageEdge` | 有向血缘边 `upstream_id -> downstream_id`，可携带列级映射 `ColumnMapping(upstream, downstream)` |
| `AuditRecord` | 追加式审计记录：seq、时间戳、操作者、动作、目标、结果（success / rejected + 原因） |
| `Principal` | 调用方身份，由 API key 解析；admin 可见全部，普通用户仅限其 namespace |

## Schema 兼容规则

演进（`POST /entities/{id}/schema-versions`）按以下规则判定，**兼容才接受，不兼容明确拒绝**，
拒绝原因以机器可区分的 `code` 返回（HTTP 422）：

| 变更 | 结论 | 拒绝原因码 |
| --- | --- | --- |
| 新增 optional 字段 | 兼容 | — |
| 类型加宽（int→long→double、int→double、float→double） | 兼容 | — |
| 删除字段 | 拒绝 | `FIELD_REMOVED` |
| 字段类型变更（非加宽） | 拒绝 | `FIELD_TYPE_CHANGED` |
| 新增 required 字段 | 拒绝 | `REQUIRED_FIELD_ADDED` |
| optional 字段变 required | 拒绝 | `FIELD_BECAME_REQUIRED` |

输入校验是严格的：字段缺失、类型非法、字段名重复、空字段列表等一律以
`INVALID_INPUT`（HTTP 400）拒绝，**不会静默写入**。

并发语义：所有变更在单锁内原子完成——校验/兼容性检查失败时不产生任何部分写入；
更新携带 `expected_revision`，不匹配时返回 `REVISION_CONFLICT`（HTTP 409），
同一 revision 的并发更新**恰好一个成功**，调用方重读最新 revision 后重试即可，不会互相覆盖。

## 血缘与影响分析语义

- **登记幂等**：同一条边（含列映射）重复登记返回 `added: false`，图中不会重复。
- **环检测**：加边前检查是否成环（含自环与间接环），成环则以 `CYCLE_DETECTED`
  （HTTP 409）拒绝并在消息中给出环路径；环检测与写边在同一临界区，拒绝后图不变。
- **查询确定性**：血缘查询与影响分析结果按固定规则排序（实体 id、深度），多次查询结论一致。
- **影响分析**（`POST /entities/{id}/impact`）：从源实体沿下游 BFS，返回每个受影响实体的
  `depth`（1 = 直接下游）与受影响列。指定 `columns` 时仅沿匹配的列映射传播，
  映射链中断处不再向下传播；不指定时按整表影响处理。
- **审计**：每次变更尝试（含被拒绝的）与影响分析都会记录 actor、时间、目标与结论，
  `GET /audit` 仅 admin 可读。

## 权限模型

- 请求头 `X-API-Key` 鉴权；内置 key：`admin-key`（全部 namespace）、
  `alice-key`（analytics）、`bob-key`（billing）。
- 普通调用方只能看到/操作自己 namespace 的实体；越权读取、演进、连血缘、做影响分析
  一律返回 **404 NOT_FOUND**，与"实体不存在"不可区分，**不会泄露实体是否存在**。
- 向无权限的 namespace 写入返回 403；影响分析结果按调用方可见范围过滤。

## 本地验证

```bash
uv sync                 # 安装依赖
uv run pytest           # 运行全部测试（-v 已内置，日志按业务域分文件展示用例）
uv run python main.py   # 启动服务，默认 127.0.0.1:8000
```

测试按业务域组织，日志可直接看出覆盖面：

| 测试文件 | 覆盖业务 |
| --- | --- |
| `tests/test_schema_compatibility.py` | 兼容演进接受、四类不兼容拒绝原因、非法输入拒绝、拒绝后无部分写入 |
| `tests/test_lineage.py` | 表级/列级血缘幂等、直接/间接/自环拒绝、列映射校验、查询确定性 |
| `tests/test_concurrency.py` | 过期 revision 确定性冲突、并发演进唯一胜出、重试不丢更新、并发血缘幂等 |
| `tests/test_impact.py` | 多层级影响、列级传播与终止、结果确定性、影响分析审计 |
| `tests/test_auth.py` | namespace 隔离、存在性不泄露、越权写拒绝、影响结果按可见性过滤、审计仅 admin |
| `tests/test_api.py` | HTTP 状态码映射（400/401/404/409/422）与错误体结构 |

### 手动验证示例

```bash
# 登记实体
curl -s -X POST localhost:8000/entities -H 'X-API-Key: alice-key' -H 'content-type: application/json' -d '{
  "entity_id": "users", "name": "users", "namespace": "analytics",
  "kind": "table", "owner": "alice",
  "fields": [{"name": "id", "type": "long", "required": true}]}'

# 不兼容演进被拒绝（422 FIELD_TYPE_CHANGED）
curl -s -X POST localhost:8000/entities/users/schema-versions -H 'X-API-Key: alice-key' \
  -H 'content-type: application/json' \
  -d '{"expected_revision": 1, "fields": [{"name": "id", "type": "string", "required": true}]}'

# 影响分析
curl -s -X POST localhost:8000/entities/users/impact -H 'X-API-Key: alice-key' \
  -H 'content-type: application/json' -d '{}'

# 审计（仅 admin）
curl -s localhost:8000/audit -H 'X-API-Key: admin-key'
```

## 代码结构

```
app/
  models.py         # 领域模型（实体、schema 版本、血缘边、审计记录）
  errors.py         # 带稳定 reason code 的错误类型
  compatibility.py  # schema 演进兼容规则
  store.py          # 线程安全内存存储，乐观并发，原子加边
  lineage.py        # 环检测与下游影响 BFS
  audit.py          # 追加式审计日志
  auth.py           # API key 鉴权与 namespace 授权
  service.py        # 业务编排（校验、兼容、血缘、影响、审计、权限）
  api.py            # FastAPI 路由与错误码映射
main.py             # 服务入口
tests/              # 按业务域划分的测试
```
