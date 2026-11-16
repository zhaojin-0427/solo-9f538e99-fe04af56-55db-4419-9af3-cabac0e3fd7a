# LC-MS 峰表去同位素 API

基于 FastAPI + SQLite 的去同位素（de-isotoping）服务：按方法登记允许电荷与
质量误差，按运行登记峰表，两者都支持带修订号（revision）的乐观并发修改；提交
分析时在同一已提交状态上计算**覆盖全部峰且组数最少**的同位素分组，并给出每个峰
在所有最优划分中的归属可能性，供下游合并化合物特征。

## 业务规则

- 方法（method）：唯一方法号；允许电荷取值 1～3（1～3 个）；非负误差。
- 运行（run）：唯一运行号，归属一个方法；1～12 个峰，峰 ID 在运行内唯一，
  m/z 必须为正数。
- 修订号：方法与运行各自维护从 1 开始、每次修改 +1 的修订号。修改时必须携带
  所见修订号；不一致返回 `409`，响应体给出所见与当前修订号。
- 分析提交时必须携带方法与运行的修订号，任一不符返回 `409`，计算只基于同一
  已提交状态；结果内附两侧修订号与完整快照（`basis`），并持久化可按 ID 取回。
- 分组规则：
  - 单峰可独立成组，电荷为 `null`（无电荷）；
  - 多峰组共用一个允许电荷，组内按 m/z 递增；
  - 相邻峰间距必须满足
    `|(mz[k+1] - mz[k]) - 1.003355 / charge| <= error`；
  - 每个峰恰好使用一次；目标是组数最少；
  - m/z、误差均以十进制字符串接收并用 `Decimal` 精确比较，拒绝 JSON 浮点
    （避免二进制误差），全部比较按十进制原值进行。
- 结果包含：
  - 一组确定性的最优划分 `partition`；
  - 全部最优划分 `optimal_partitions`；
  - 每个峰在**所有**最优划分中可能所属的成员 ID 集合
    （`possible_member_sets`）与电荷（`possible_charges`，单峰为 `null`）；
  - 成员集合与电荷分别给出确定性标记，两者始终相同才置 `certain=true`。

> 十进制说明：`1.003355 / 3` 是无限循环小数，引擎按 60 位有效数字的
> `Decimal` 计算并舍入；误差为 0 时要求与该舍入值严格相等，电荷 1、2 的间距
> （含 `0.5016775`）为有限终止小数，可做到严格精确匹配。

## 启动（Docker Compose）

```bash
docker compose up --build
# API:        http://localhost:8000
# Swagger UI: http://localhost:8000/docs  （可直接在页面上提交分析并查看结果）
# OpenAPI:    http://localhost:8000/openapi.json
```

SQLite 数据保存在命名卷 `deisotope-data`（容器内 `/data/deisotope.db`），
容器带健康检查。可用环境变量 `DEISOTOPE_DB_PATH` 覆盖数据库路径。

## 本地运行（无 Docker）

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
DEISOTOPE_DB_PATH=./data/deisotope.db uvicorn app.main:app --reload
pytest            # 21 个测试（算法 14 + HTTP 7）
```

## API 摘要与示例

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/methods` | 登记方法（修订号初始为 1） |
| GET | `/methods/{method_id}` | 查询方法 |
| PUT | `/methods/{method_id}` | 修订方法（带 `expected_revision`） |
| POST | `/runs` | 登记运行与 1～12 个峰 |
| GET | `/runs/{run_id}` | 查询运行 |
| PUT | `/runs/{run_id}` | 修订运行峰表（带 `expected_revision`） |
| POST | `/analyses` | 提交分析（带两侧修订号，不符 409） |
| GET | `/analyses/{analysis_id}` | 取回已持久化的分析结果 |

```bash
# 1. 登记方法
curl -X POST localhost:8000/methods -H 'Content-Type: application/json' \
  -d '{"method_id":"M1","allowed_charges":[1,2,3],"error":"0.002"}'

# 2. 登记运行（m/z 用十进制字符串）
curl -X POST localhost:8000/runs -H 'Content-Type: application/json' \
  -d '{"run_id":"R1","method_id":"M1","peaks":[
       {"peak_id":"A","mz":"100.000000"},
       {"peak_id":"B","mz":"101.003355"},
       {"peak_id":"C","mz":"101.5050325"}]}'

# 3. 提交分析（修订号不符返回 409）
curl -X POST localhost:8000/analyses -H 'Content-Type: application/json' \
  -d '{"run_id":"R1","method_id":"M1",
       "expected_method_revision":1,"expected_run_revision":1}'
```

歧义示例（A-B 间距符合电荷 1，B-C 间距符合电荷 2）存在两个最优划分：
`[A][B,C@2]` 与 `[A,B@1][C]`，峰 B 的 `certain=false`，
`possible_member_sets=[["A","B"],["B","C"]]`、
`possible_charges=[1,2]`。

## 算法说明

峰按 m/z 排序后做后缀动态规划：对每个后缀起点，枚举“单峰成组”与所有合法
多峰组（电荷 × 组长度），取后续最优值 +1 的最小者；同时保留所有达到最小值的
前驱。DP 保证最优组数，随后沿全部最优前驱 DFS 枚举每个最优划分，再按峰聚合
成员集合与电荷。n ≤ 12，最坏约数千个划分，可即时返回。

## 目录结构

```
app/
  main.py       FastAPI 路由、409/404 处理、分析编排
  schemas.py    Pydantic 模型与精确十进制校验
  db.py         SQLite 持久化、修订号与冲突
  grouping.py   Decimal 精确分组算法
  tests/        pytest 测试（算法 + HTTP 端到端）
Dockerfile / docker-compose.yml / requirements.txt
```
