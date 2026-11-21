# 表盘回卷还原（Dial Rewind）

## 业务背景

试封表盘是模数计数器，区间起止读数只保留末位。表盘回卷跨圈后，直接用
`止读数 − 起读数` 会少算件数（甚至得到负数）。真实区间产量必须把回卷的圈数补回：

```
区间产量 x_i = (总件止 − 总件起) + 总件模数 × 圈数 k_i
区间漏封 y_i = (漏封止 − 漏封起) + 漏封模数 × 圈数 l_i
```

## 录入口径

每个班次（shift）按区间录入：

- 工艺（A 或 B）；
- 总件表、漏封表各自的回卷模数；
- 两只表的起读数、止读数（读数必须落在对应模数范围内）；
- 区间产量上限 `upper`。

再录全班实点的总件数与漏封数。约束：

- 区间可跨多圈（圈数 ≥ 0，由求解器枚举）；
- 任一区间 `0 ≤ 漏封 ≤ 总件 ≤ 上限`；
- 全班 `漏封 ≤ 总件`，否则录入即拒绝（422）。

## 求解与判定（`src/services/rewind_solver.py`，纯标准库）

1. 为每个区间枚举全部合法 `(x, y, k, l)` 候选；
2. 有界动态规划在 `(累计总件, 累计漏封)` 状态上推进，记录每个可达状态下
   工艺 A 累计总件/漏封的最小值与最大值；
3. 终态必须恰好等于全班实点，否则判 **无解**（`no_solution`）；
4. 终态处 A 的最小/最大累计值若不相等，说明存在多组可行还原且工艺级
   A/B 计数不一致，判 **计数歧义**（`ambiguous`）；
5. 计算超过 5 秒（或中间状态超预算）判 **超时**（`timeout`）。

关键口径：**允许区间解不同，只要所有可行解的工艺 A/B 计数完全相同就判定唯一、
可采用**。求解结果同时返回逐区间公式依据与若干可行区间解样本，便于追溯。

## 版本与并发（`src/services/rewind.py`）

- 每次新建、补录、更正都让班次 `version + 1`；还原记录保存计算时的版本。
- 采用（adopt）在单事务内：
  `SELECT ... FOR UPDATE` 锁班次 → 校验还原为 `ok` → 校验
  `expected_version` 与还原版本都等于当前版本 → 写入原检验 `observations`
  （A/B 两行，`success = 总件 − 漏封`、`failure = 漏封`）→ 写采用记录 → 置 `adopted`。
- 采用时源数据已改（版本不符）返回 409，要求重新查看依据、重新还原。
- `rewind_adoptions.restoration_id` 唯一约束兜底，并发/重试不会重复写入。
- 已采用班次允许更正：旧采用保留，状态转 `superseded`，需基于新版重新还原。

## API（前缀 `/api/rewind`，需登录；写操作需 admin/analyst）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/shifts` | 班次列表 |
| POST | `/shifts` | 新建班次（录入即校验） |
| GET | `/shifts/{id}` | 班次详情（区间、还原历史、采用记录） |
| PATCH | `/shifts/{id}` | 补录/更正（升版） |
| POST | `/shifts/{id}/restorations` | 按当前版计算还原 |
| GET | `/shifts/{id}/restorations/{rid}` | 查看单次还原依据 |
| POST | `/shifts/{id}/restorations/{rid}/adopt` | 采用并写入原检验（带 `expected_version`） |

## 前端

「回卷还原」导航页（`frontend/src/pages/rewind.js`）支持录数、增删区间、
补录更正、触发还原、查看逐区间公式与可行解样本，以及带版本确认的采用。

## 测试与验收

本机无网络/pip，测试用 `tests/_ormshim/` 下的最小 SQLAlchemy/pydantic/FastAPI
垫片在 sqlite 上运行（生产仍用 requirements.txt 的真实依赖与 MySQL 8）：

```bash
cd backend
python3 tests/test_rewind_solver.py        # 19 项：求解器 + 120 组随机暴力对拍
python3 tests/test_rewind_integration.py   # 6 项：录入→还原→依据→采用 全链路
python3 tests/test_rewind_concurrency.py   # 2 项：8 线程并发采用、采用撞上更正
python3 tests/verify_acceptance.py         # 题目两个验收点实证
```

验收 A（区间解不同、工艺计数相同）实测：3 组可行区间分配
`(0,20)/(10,10)/(20,0)`，但 A 计数恒为 `总件20/漏封0`，判定唯一并可采用。

验收 B（采用撞上更正）实测：更正后旧版 v1 采用返回 409；8 线程并发采用当前版，
恰好 1 次成功、原检验只写入 A/B 两行，无重复写入。
