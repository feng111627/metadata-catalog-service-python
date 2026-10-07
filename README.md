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
- **分批补登（增量合并）**：同一对上下游表在血缘中**始终只有一条边**。列级映射可以
  分多批登记，服务端按"并集 + 去重"合并；两批补登后查到的是合并后的完整列映射。
  - 返回值 `added` 精确表达本次是否带来新增：新表对、或批次中含此前没有的映射为
    `true`；原样重放、或整批都是已有映射时为 `false`，**且不改动任何状态**。
  - 只对"全新表对"做环检测；向已存在的边追加列映射不可能成环，不会被拒绝。
- **下线 / 回滚**（`POST /lineage/deregister`）：
  - 请求体不带 `columns`（或为 `null`）→ **整条撤销**（表级、列级边都适用）；
  - 带 `columns` 列表 → **只撤销指定的列映射**；列级边的最后一条映射被撤掉后，
    边本身也消失，不会留下一条没有映射的空边。
  - **幂等**：撤销本来就不存在的边或映射按成功处理，返回 `removed: false`，不报错误；
    引用了当前 schema 中已不存在字段的撤销同样视为"本来就没有"的 no-op。
  - 撤销后血缘查询与影响分析**立即按新状态收敛**，不再带出任何已下线内容。
  - 撤销后再登记从干净状态开始，**不会翻出此前撤掉的旧映射**。
  - 每次撤销（含幂等 no-op 与被拒绝请求）都写审计：actor、时间、目标、范围与结果。
  - 删边/删映射只会让图变小，**不可能制造环或不合法状态**。
- **原子性（all-or-nothing）**：一次补登或撤销在单临界区内完成；若批次中任何一部分
  不合法（如引用不存在的字段、结构错误），整体拒绝，**不会只写入/删掉一半**，失败前后
  已有数据完全一致。
- **并发可预期**：多人同时给同一对表补登/撤销，在同一把锁内串行化——有效内容不丢失、
  不出现重复映射、每对表始终一条边；对互不相交映射的并发增删满足交换律，最终状态确定。
- **环检测**：加边前检查是否成环（含自环与间接环），成环则以 `CYCLE_DETECTED`
  （HTTP 409）拒绝并在消息中给出环路径；环检测与写边在同一临界区，拒绝后图不变。
- **查询确定性**：血缘查询与影响分析结果按固定规则排序（实体 id、深度、列名），多次查询结论一致。
- **影响分析**（`POST /entities/{id}/impact`）：从源实体沿下游 BFS，返回每个受影响实体的
  `depth`（1 = 直接下游）与受影响列。指定 `columns` 时仅沿匹配的列映射传播，
  映射链中断处不再向下传播；不指定时按整表影响处理。
- **审计**：每次变更尝试（含被拒绝的）与影响分析都会记录 actor、时间、目标与结论，
  `GET /audit` 仅 admin 可读。

### 与原有行为的兼容范围

- `POST /lineage` 的请求体与 `added: bool` 返回结构**保持不变**；变化仅是存储语义：
  同一对表的多次登记从"多条边"收敛为"一条边 + 合并后的列映射"。旧调用方（只登记一次、
  或原样重放）行为完全一致：首次 `true`、重放 `false`。
- 先登记表级血缘（不带 `columns`）、后补列映射的用法现在也会合并到同一条边（此前会留下
  两条边）；这属于对既有歧义行为的修正。
- 下线能力通过**新端点** `POST /lineage/deregister` 提供，不改动任何既有端点；成功返回
  200 与 `{"removed": bool}`，不存在 404（撤销缺失内容是幂等成功）。

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

最近一次完整运行日志保存在 `logs/pytest-latest.log`，其中逐用例列出业务、用例名与
PASSED/FAILED 结果。

测试按业务域组织，日志可直接看出覆盖面：

| 测试文件 | 覆盖业务 |
| --- | --- |
| `tests/test_schema_compatibility.py` | 兼容演进接受、四类不兼容拒绝原因、非法输入拒绝、拒绝后无部分写入 |
| `tests/test_lineage.py` | 表级/列级血缘幂等、直接/间接/自环拒绝、列映射校验、查询确定性 |
| `tests/test_lineage_incremental.py` | 分批补登合并去重、始终一条边、`added` 区分有无新增、坏批次整体不生效、表级→列级合并、排序确定性 |
| `tests/test_lineage_deregister.py` | 整条/按列撤销、幂等 no-op、最后映射撤掉边消失、影响分析立即收敛、干净重登、撤销审计留痕、坏请求不改状态 |
| `tests/test_lineage_concurrency.py` | 并发分批补登不丢不重、并发增删不相交映射结果确定、并发撤销不丢映射、全撤+重登终态合法、混合压力下图始终合法 |
| `tests/test_concurrency.py` | 过期 revision 确定性冲突、并发演进唯一胜出、重试不丢更新、并发血缘幂等 |
| `tests/test_impact.py` | 多层级影响、列级传播与终止、结果确定性、影响分析审计 |
| `tests/test_auth.py` | namespace 隔离、存在性不泄露、越权写拒绝、影响结果按可见性过滤、审计仅 admin |
| `tests/test_api.py` | HTTP 状态码映射（400/401/404/409/422）、错误体结构、分批合并与下线端点（含幂等 200、400） |

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

# 血缘分批补登（同一对表两次提交，合并为一条边；第二次有新映射仍返回 added=true）
curl -s -X POST localhost:8000/lineage -H 'X-API-Key: alice-key' \
  -H 'content-type: application/json' \
  -d '{"upstream_id":"users","downstream_id":"users_vip","columns":[{"upstream":"id","downstream":"uid"}]}'
curl -s -X POST localhost:8000/lineage -H 'X-API-Key: alice-key' \
  -H 'content-type: application/json' \
  -d '{"upstream_id":"users","downstream_id":"users_vip","columns":[{"upstream":"name","downstream":"uname"}]}'

# 只撤销其中一条列映射（重复撤销返回 removed=false，仍为 200）
curl -s -X POST localhost:8000/lineage/deregister -H 'X-API-Key: alice-key' \
  -H 'content-type: application/json' \
  -d '{"upstream_id":"users","downstream_id":"users_vip","columns":[{"upstream":"id","downstream":"uid"}]}'

# 整条撤销（不带 columns）
curl -s -X POST localhost:8000/lineage/deregister -H 'X-API-Key: alice-key' \
  -H 'content-type: application/json' \
  -d '{"upstream_id":"users","downstream_id":"users_vip"}'

# 审计（仅 admin）
curl -s localhost:8000/audit -H 'X-API-Key: admin-key'
```

## 代码结构

```
app/
  models.py         # 领域模型（实体、schema 版本、血缘边、审计记录）
  errors.py         # 带稳定 reason code 的错误类型
  compatibility.py  # schema 演进兼容规则
  store.py          # 线程安全内存存储，乐观并发；血缘按表对键控，原子 merge/remove
  lineage.py        # 环检测与下游影响 BFS
  audit.py          # 追加式审计日志
  auth.py           # API key 鉴权与 namespace 授权
  service.py        # 业务编排（校验、兼容、血缘、影响、审计、权限）
  api.py            # FastAPI 路由与错误码映射
main.py             # 服务入口
tests/              # 按业务域划分的测试
```
