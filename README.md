# 税则税号分组供数 API

新版税则拆并税号后，进口额同比要按**同一组商品**计算。本服务用 FastAPI + SQLite 实现：

1. `PUT /base` 先固定相邻两年各税号进口额（整数分）；存在对照版本后底表即锁定（409）。
2. `POST /mappings` 按递增版号提交**完整**新旧税号对照。同号也按年份区分——未变税号
   也要以 `(code, code)` 显式出现，两侧分别对应底表两个年份的税号集合。
3. 经对照**直接或间接**相连的税号归为一组（二部图连通分量）。
4. `GET /summary` 现行汇总：组号、两侧税号、各年合计。
5. 改版时成员相同的组**沿用组号和金额**（不重算），只有新组才读取底表金额明细；
   组号单调递增、绝不复用。
6. `GET /changes/{version}` 按版取供数变更：首版给全量；后续给被替换的版号、
   应撤下的组号、应写入的汇总。变更与汇总在同一事务生效，落 SQLite，重启后仍可取。
7. 对照漏掉底表税号或引用未知税号 → 422 **整版拒收**，原供数仍可查询。
8. 同版重传原对照 → 200 返回当时的变更（幂等）；同版改内容 → 409 冲突；
   版号不高于现行版且不存在 → 409。
9. 并发改版串行提交，差分以**实际替换的上一版**为准（提交时在读已加写锁的事务内
   重取现行版号）。

## 来源国判定

税号进口额入账后，部分货物的来源国仍在核实。首版对照发布后开放国别明细：

1. `PUT /details/{year}/{code}` 按年份和税号**替换**国别明细：已确认的国别金额
   （国家 → 整数分）+ 每笔待核金额及其候选国家。金额均为非负整数分；国家代码按
   两位大写字母归一化（小写自动转大写、候选去重排序）。
2. **明细合计（已确认 + 待核）须等于底表**，不等 → 422 且**保留上次明细**；
   未发布对照 → 409；税号不在底表 → 404。
3. `GET /groups/{group_id}/origin` 按**现行组号**逐年判定：
   - 组内该年底表总额为零 → `no_imports`（无进口）；
   - 总额非零而缺明细 → `incomplete`，列出缺项（税号 + 底表金额；
     底表为零的税号不要求明细）；
   - 明细齐全 → 给出在**所有可行分摊下都独占首位**的国家（`determined`），
     附首位最坏总额与次优国最好总额；
   - 无法判定（`undetermined`）→ 给出**一份首位并列的分摊**，或**两份首位不同
     的分摊**（含每笔待核金额的分摊去向与各国内合计），供分析员追查。
4. 一次查询的**版号、组成员、国别明细取自同一已提交状态**（单个 SQLite 读事务
   快照）；分摊计算在事务外进行，期间其他实例可继续更正明细和发布对照
   （WAL 下读者不阻塞写者）。

判定原理：设 `low(c)` = c 的已确认金额 + 只能给 c 的待核金额（候选仅 {c}），
`high(d)` = d 的已确认金额 + 候选含 d 的全部待核金额。国家 c 在所有可行分摊下
独占首位 ⟺ 对所有 d≠c 都有 `low(c) > high(d)`——最坏情形（c 只拿必拿、d 拿遍
可拿）可在同一分摊中同时取得，故条件充分必要。无法判定时，对 (low 最大者,
high 最大者) 构造使差距最小的分摊 V1：V1 首位并列即为并列证据；否则其独占
首位 e 在同样构造的 V2 中也不是首位，V2 或并列、或与 V1 凑成两份首位不同的
分摊。

## 启动

### Docker Compose（推荐）

```bash
docker compose up --build
```

服务监听 <http://localhost:8000>，SQLite 落在命名卷 `tariff-data`（`/data/app.db`），
重启后版本与变更保留。

### 本地

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload          # DB_PATH 默认 ./data/app.db
python -m pytest tests/ -q             # 跑测试
```

## 在 Swagger 上演示改版和取数

打开 <http://localhost:8000/docs>（`/` 会自动跳转），依次 “Try it out”：

1. **底表 / `PUT /base`** —— 固定 2023/2024 两年进口额：

   ```json
   {"amounts":[
     {"year":2023,"code":"A","amount_cents":100},
     {"year":2023,"code":"B","amount_cents":200},
     {"year":2023,"code":"C","amount_cents":300},
     {"year":2023,"code":"D","amount_cents":400},
     {"year":2024,"code":"A","amount_cents":110},
     {"year":2024,"code":"B","amount_cents":210},
     {"year":2024,"code":"C","amount_cents":310},
     {"year":2024,"code":"D","amount_cents":410}]}
   ```

2. **对照版本 / `POST /mappings`** —— v1 把 C、D 并成一组：

   ```json
   {"version":1,"entries":[
     {"old_code":"A","new_code":"A"},
     {"old_code":"B","new_code":"B"},
     {"old_code":"C","new_code":"C"},
     {"old_code":"C","new_code":"D"},
     {"old_code":"D","new_code":"D"}]}
   ```

   返回 201：首版全量，组 3 = `{C,D | C,D}`，合计 700 / 720 分。

3. **供数 / `GET /summary`** —— 现行 3 个组：1 `{A}`、2 `{B}`、3 `{C,D}`。
4. **`POST /mappings`** —— v2 拆组（四个单税号恒等对照），返回 201：
   `replaced_version=1`、`removed_group_ids=[3]`、应写入组 4 `{C}`、组 5 `{D}`；
   组 1、2 成员未变，沿用组号和金额，不出现在变更里。
5. **供数 / `GET /changes/{version}`** —— 分别取 1、2 两版变更核对。
6. **旧版重传** —— 再以原内容 `POST /mappings` version=1 → 200 返回当时的全量变更；
   把 entries 改掉再传 version=1 → 409。
7. **拒收** —— version=3 漏掉 `D` → 422 列出 `missing_old_codes`/`missing_new_codes`，
   `GET /summary` 仍是 v2。
8. **国别明细 / `PUT /details/2023/C`** —— 组 4 `{C}` 的 2023 年（合计须=底表 300）：

   ```json
   {"confirmed":{"CN":100,"US":80},
    "pending":[{"amount_cents":120,"candidates":["CN","US"]}]}
   ```

   再 `PUT /details/2024/C`：`{"confirmed":{"CN":250,"US":60}}`（合计=310）。
9. **来源国判定 / `GET /groups/4/origin`** —— 2023 年 `undetermined`，给出两份
   首位不同的分摊（120 分全归 US 则 US 200:100 领先；全归 CN 则 CN 220:80 领先）；
   2024 年 `determined` = CN。
10. **只更正候选国家** —— 重发步骤 8 的明细，仅把候选改为 `["US"]`（金额不变），
    再查 `/groups/4/origin`：2023 年变为 `determined` = US（US 至少 200 > CN 至多 100）。
11. **校验失败保留上次明细** —— `PUT /details/2023/C` 传 `{"confirmed":{"CN":1}}`
    → 422（合计 1 ≠ 300），`GET /details/2023/C` 仍是上一步内容。
12. **缺项** —— `GET /groups/5/origin`：组 5 `{D}` 未报明细，两年都是
    `incomplete`，`missing` 列出 `D` 及其底表金额。

也可以用脚本一键演示同样流程：`./scripts/demo.sh`（需服务已启动）。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| PUT | `/base` | 固定相邻两年底表（整数分）；有版本后 409 |
| GET | `/base` | 查看底表 |
| POST | `/mappings` | 提交完整对照；201 新版 / 200 同版重传 / 409 冲突 / 422 拒收 |
| GET | `/mappings/{version}` | 查看某版对照内容 |
| GET | `/versions` | 版本列表与现行版号 |
| GET | `/summary` | 现行汇总：组号、两侧税号、各年合计 |
| GET | `/changes/{version}` | 按版取供数变更（首版全量） |
| PUT | `/details/{year}/{code}` | 替换国别明细；合计≠底表 422 保留上次 / 未发布对照 409 / 无此底表项 404 |
| GET | `/details/{year}/{code}` | 查看某年某税号的国别明细 |
| GET | `/details` | 列出全部国别明细 |
| GET | `/groups/{group_id}/origin` | 来源国判定：逐年给独占首位国家或追查用分摊证据 |

变更记录结构：

```json
{"version": 2, "replaced_version": 1,
 "removed_group_ids": [3],
 "summaries": [{"group_id": 4, "old_codes": ["C"], "new_codes": ["C"],
                "old_total_cents": 300, "new_total_cents": 310}]}
```

## 设计要点

- **分组**：对照边构成新旧两侧的二部图，连通分量即组；组成员哈希（两侧税号集合）
  决定组身份。
- **沿用**：改版时按成员哈希比对上一版组，命中则沿用组号与库存合计，不触碰底表；
  未命中的新组才 `SUM` 底表金额，组号取自持久化计数器（单调、不复用）。
- **一致性与并发**：写路径全程互斥锁 + `BEGIN IMMEDIATE`；版本行、变更行、现行汇总、
  计数器在同一事务提交，`replaced_version` 在事务内重取，故并发改版以实际替换的
  上一版为准。
- **幂等/冲突**：每版保存对照内容哈希；同版同哈希直接回放当时存的变更记录，
  不同哈希 409。
- **持久化**：`versions` / `changes` / `current_groups` / `country_details` /
  `meta` 全在 SQLite 文件中，重启后按版取变更、现行汇总、国别明细、重传幂等
  全部保留。
- **来源国判定**：明细按 (年份, 税号) 整体替换，写入前校验合计=底表；判定用
  `low(c) > high(d)` 充要条件（见上文"来源国判定"），证据分摊由两次顶点构造
  得出，复杂度随待核笔数线性。
- **快照一致性**：`/groups/{id}/origin` 在单独的只读连接上开一个读事务，版号、
  组成员、底表金额、国别明细同取一个已提交状态，计算在事务外进行；WAL 模式下
  读者不阻塞写者，其他实例可同时更正明细和发布对照。

## 目录

```
app/
  main.py      # FastAPI 路由与 App 工厂（DB_PATH 环境变量）
  store.py     # SQLite 持久化 + 分组/差分/校验/明细/快照领域逻辑
  origin.py    # 来源国判定：low/high 充要条件 + 证据分摊构造
  schemas.py   # Pydantic 模型
tests/test_api.py     # 底表/对照/分组/变更
tests/test_origin.py  # 国别明细与来源国判定
scripts/demo.sh
Dockerfile / docker-compose.yml
requirements.txt / requirements-dev.txt
```
