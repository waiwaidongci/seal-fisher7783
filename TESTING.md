# 测试记录（TESTING）

本机环境无 Docker、无 pip/网络，无法安装 requirements.txt 中的 FastAPI/SQLAlchemy/
PyMySQL 等依赖，也无法启动 MySQL/Redis。因此回卷功能的自动化测试采用：

- **求解器** `test_rewind_solver.py`：纯标准库，无任何第三方依赖，可直接运行。
- **服务/集成/并发/HTTP 测试**：通过 `tests/_ormshim/` 下的最小
  SQLAlchemy 2 / pydantic / FastAPI / PyJWT 垫片在 sqlite3 上运行。
  垫片仅实现项目实际用到的接口（含 `with_for_update` 当前读、脏更新跟踪、
  唯一约束、JSON 列），生产环境仍由 requirements.txt 的真实依赖与 MySQL 8 承载，
  业务服务代码 `src/services/rewind.py` 不区分环境。

## 实际运行结果（2026-11-21，Python 3.11.2）

| 模块 | 用例数 | 结果 | 覆盖 |
|---|---|---|---|
| `tests/test_rewind_solver.py` | 19 | OK | 候选枚举、跨多圈、上限裁剪、无解、歧义、区间解不同但工艺计数相同、超时（可注入时钟）、输入校验、120 组随机暴力对拍 |
| `tests/test_rewind_integration.py` | 6 | OK | 录入→还原→查看依据→采用写原检验全链路、同计数多解采用、无解/歧义/超时不可采用、并发补录版本过期拒绝、重试不重复写入、采用后更正保留历史 |
| `tests/test_rewind_concurrency.py` | 2 | OK | 8 线程并发采用恰好 1 次成功且仅 2 行写入；采用与更正并发碰撞的两种合法时序 |
| `tests/test_rewind_http.py` | 5 | OK | 真实路由函数的状态码（201/403/404/409/422）、权限、路由表挂载 |
| `tests/verify_acceptance.py` | 2 场景 | OK | 题目两个验收点实证（输出见下） |

合计 32 个 unittest 用例 + 验收脚本，全部通过；`python3 -m py_compile` 全量编译通过；
前端全部 ES module 通过 `node --check` 语法校验。

## 验收点实证输出

```
=== 验收 A：区间解不同，但工艺计数相同 ===
还原状态: ok
可行还原组数: 3
采样到的不同区间分配: [(0, 20), (10, 10), (20, 0)]
工艺计数 A: {'total': 20, 'missed': 0}  B: {'total': 0, 'missed': 0}
采用成功，写入行: ['REWIND-1-v1-A', 'REWIND-1-v1-B']
结论 A：区间解有多种，但所有可行解给出同一组 A/B 计数，判定唯一并允许采用。✓

=== 验收 B：采用撞上更正 ===
基于旧版 v1 采用被拒: {'code': 409, 'detail': '采用依据版本 v1 已过期，当前为 v2，请重新查看还原依据'}
并发采用结果: ['ok', '409', '409', '409', '409', '409', '409', '409']
原检验实际写入行数: 2
结论 B：源数据改动后旧版采用被拒；并发重试仅一次写入，无重复。✓
```

## 在具备 Docker 的环境复跑

```bash
docker compose up -d --build
docker compose exec -T backend python -m pytest tests/ -q   # 装好 pytest 后
# 或不依赖 pytest：
docker compose exec -T backend python tests/test_rewind_solver.py
docker compose exec -T backend python tests/verify_acceptance.py
```

垫片目录 `tests/_ormshim/` 仅在测试 `sys.path` 中优先加载，不影响生产镜像
（Dockerfile 安装真实依赖，导入时优先命中 site-packages）。
