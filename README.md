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

### 登记：幂等 + 增量合并

- **一条边只算一条**：血缘边的身份是上下游实体对 `(upstream, downstream)`，
  与登记次数、登记批次无关。列映射在同一条边上做**集合并**。
- **分批补登**：同一对表可以分多次登记列映射，查询得到的是各批**合并、去重**
  之后的完整映射；不会因为分几次登记而产生多条边或重复列映射。
- **返回值表达“有无新增”**：`register_lineage` / `POST /lineage` 返回
  `added`（布尔）。新建边或至少新增了一条映射时为 `true`；重复提交完全相同的
  内容、或本批没有任何新增时**不改任何状态**并返回 `false`（审计中记为
  `success (idempotent replay)`）。
- **环检测**：新增边前检查是否成环（含自环与间接环），成环则以 `CYCLE_DETECTED`
  （HTTP 409）拒绝并在消息中给出环路径；校验与写入在同一临界区，拒绝后图不变。
  仅给已存在的边补列映射不会引入新边，因此不会成环。

### 下线：整条撤销 / 部分撤销 / 回滚

`revoke_lineage` / `POST /lineage/revoke` 支持把已登记的血缘下线：

| 用法 | 行为 |
| --- | --- |
| 不带 `columns` | **整条撤销**：边及其全部列映射一并删除（表级边也一样） |
| 带 `columns` | **部分撤销**：只摘除指定的列映射；剩下的映射保留；最后一条映射摘除后边自动消失 |

- **立即收敛**：撤销完成后，血缘查询与影响分析立刻按新状态给结果，不再带出已下线内容。
- **撤销幂等**：撤销一条不存在的边、或撤销一组并未登记（已撤销）的列映射，
  都按**成功的无操作**处理（HTTP 200，返回 `removed: false`），而不是错误。
- **撤后重登从干净状态开始**：整条撤销后重新登记只会包含本次提交的映射，
  之前撤掉的旧映射不会被翻出来；若只撤销部分映射后又重新登记同一条映射，
  则按一次普通的新增处理（`added: true`）。
- **撤销审计**：每次撤销都记录 actor、时间、目标 `up->down`、范围
  （`scope=edge|columns`）与剩余映射数；幂等无操作记为
  `success (idempotent no-op)`，可在 `GET /audit` 查到“谁在什么时候撤了什么”。
- **图合法性**：每次撤销后都对整图做无环后验校验，撤销不可能把图弄成不合法状态。

### 原子性、失败与并发

- **全有或全无**：一批列映射只要有任何一项非法（引用了不存在的字段、结构错误等），
  整批被拒绝（`INVALID_INPUT`，HTTP 400），**不会出现只进了一半、或把已有映射
  删掉一部分**的情况；失败前后已有数据完全一致（审计记 `rejected` 及原因）。
  登记时同一请求内重复的列映射对会被拒绝；撤销时同请求内重复项按同一幂等意图去重。
- **并发可预期**：合并与撤销都在单把存储锁内原子完成。多人同时给同一对表补登/
  撤销时，每条映射恰好生效一次，不会丢失有效内容也不会产生重复记录，最终状态确定。

### 查询与影响分析

- **查询确定性**：血缘查询与影响分析结果按固定规则排序（实体 id、深度、列名），多次查询结论一致。
- **影响分析**（`POST /entities/{id}/impact`）：从源实体沿下游 BFS，返回每个受影响实体的
  `depth`（1 = 直接下游）与受影响列。指定 `columns` 时仅沿匹配的列映射传播，
  映射链中断处不再向下传播；不指定时按整表影响处理（沿实体传播，下游边的全部映射列视为受影响）。
- **审计**：每次变更尝试（含被拒绝的）、撤销与影响分析都会记录 actor、时间、目标与结论，
  `GET /audit` 仅 admin 可读。

## 权限模型

- 请求头 `X-API-Key` 鉴权；内置 key：`admin-key`（全部 namespace）、
  `alice-key`（analytics）、`bob-key`（billing）。
- 普通调用方只能看到/操作自己 namespace 的实体；越权读取、演进、连血缘、做影响分析
  一律返回 **404 NOT_FOUND**，与"实体不存在"不可区分，**不会泄露实体是否存在**。
- 向无权限的 namespace 写入返回 403；影响分析结果按调用方可见范围过滤。

## 与原有行为的兼容范围

- **接口兼容**：`POST /lineage` 的路径、请求体与 `{"added": bool}` 返回结构不变；
  原有“相同内容重复登记返回 `added: false`”的调用方无需改动。
- **语义增强而非破坏**：变化仅在于同一对表的列映射由“整边相等才算幂等”升级为
  “按列映射集合并去重”——以前给同对表第二次提交不同列会被当成另一条边，现在会
  合并进同一条边，这正是增量补登所需的行为；边的排序、环拒绝、审计等规则保持不变。
- **新增能力不影响旧流程**：下线/回滚是新端点 `POST /lineage/revoke`，不使用则
  血缘行为与之前一致；schema 演进、权限、影响分析等模块未改变。

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
| `tests/test_lineage.py` | 表级/列级血缘幂等、直接/间接/自环拒绝、列映射校验、查询确定性（原有行为，未退化） |
| `tests/test_lineage_lifecycle.py` | 分批补登合并去重、有无新增返回值、整条/部分撤销、撤销幂等、影响即时收敛、撤后干净重登、撤销审计、失败原子性、撤销后图合法 |
| `tests/test_lineage_concurrency.py` | 并发分批补登不丢不重、并发补登/撤销最终确定且全程无环无重边、并发整条撤销仅一次生效 |
| `tests/test_concurrency.py` | 过期 revision 确定性冲突、并发演进唯一胜出、重试不丢更新、并发血缘幂等（原有行为，未退化） |
| `tests/test_impact.py` | 多层级影响、列级传播与终止、结果确定性、影响分析审计（原有行为，未退化） |
| `tests/test_auth.py` | namespace 隔离、存在性不泄露、越权写/撤销拒绝、影响结果按可见性过滤、审计仅 admin |
| `tests/test_api.py` | HTTP 状态码映射（400/401/404/409/422）、增量合并与撤销端点语义、撤销失败不产生部分删除 |

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

# 增量补登列映射（同一对表分两批，返回 added 表示这批有无新增）
curl -s -X POST localhost:8000/lineage -H 'X-API-Key: alice-key' \
  -H 'content-type: application/json' \
  -d '{"upstream_id":"users","downstream_id":"user_mart","columns":[{"upstream":"id","downstream":"uid"}]}'
curl -s -X POST localhost:8000/lineage -H 'X-API-Key: alice-key' \
  -H 'content-type: application/json' \
  -d '{"upstream_id":"users","downstream_id":"user_mart","columns":[{"upstream":"name","downstream":"display_name"}]}'
# -> 两批合并成同一条边上的两条列映射；再重放任一批返回 {"added": false}

# 只撤销其中一条列映射（其余保留）；撤销不存在的内容返回 removed=false 且不报错
curl -s -X POST localhost:8000/lineage/revoke -H 'X-API-Key: alice-key' \
  -H 'content-type: application/json' \
  -d '{"upstream_id":"users","downstream_id":"user_mart","columns":[{"upstream":"id","downstream":"uid"}]}'

# 整条血缘下线
curl -s -X POST localhost:8000/lineage/revoke -H 'X-API-Key: alice-key' \
  -H 'content-type: application/json' \
  -d '{"upstream_id":"users","downstream_id":"user_mart"}'

# 审计（仅 admin），撤销动作 action=revoke_lineage，幂等无操作 outcome 可区分
curl -s localhost:8000/audit -H 'X-API-Key: admin-key'
```

## 代码结构

```
app/
  models.py         # 领域模型（实体、schema 版本、血缘边、审计记录）
  errors.py         # 带稳定 reason code 的错误类型
  compatibility.py  # schema 演进兼容规则
  store.py          # 线程安全内存存储，实体乐观并发；血缘按端点对存列映射集合，锁内原子合并/撤销
  lineage.py        # 环检测（加边前断言 / 整图无环后验）与下游影响 BFS
  audit.py          # 追加式审计日志
  auth.py           # API key 鉴权与 namespace 授权
  service.py        # 业务编排（校验、兼容、血缘、影响、审计、权限）
  api.py            # FastAPI 路由与错误码映射
main.py             # 服务入口
tests/              # 按业务域划分的测试
```
