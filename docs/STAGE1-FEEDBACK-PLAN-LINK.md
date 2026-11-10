# 第一阶段：学习计划与反馈的关联（feedbacks.plan_id）

- 日期：2026-11-10
- 基线 commit：`fb3829c`
- 范围：只做数据关联，不涉及长期记忆、状态评估、LangGraph 新工作流

## 一、改了什么

| 文件 | 改动 |
| --- | --- |
| `database.py` | `SCHEMA_VERSION` 5 → 6；新增 `_migration_v6_feedback_plan_link`；新增 `get_plan_by_id()`；`insert_feedback()` / `get_feedback_list()` / `get_feedback_since()` 支持 `plan_id` |
| `main.py` | `Feedback.plan_id`（`int | None`，`ge=1`，`strict=True`）；`/feedback` 写库前校验计划存在 + 任务名一致 |
| `static/feedback.html` | 从 `sessionStorage.current_plan` 读 `plan_id` 并随请求提交；失败时显示服务端 `message` |
| `test_database_migrations.py` | 追加 V6 段：`plan_id` 列、索引、旧数据 NULL |
| `test_feedback_plan_link.py` | 新增：迁移 / 读写 / HTTP 接口 / 前端静态检查，共 16 个用例 |

未改动：`plan_service.py`、`confirmation_service.py`、`agent_*`、`rag_*`、`plan-confirmation.js`。

## 二、V6 迁移

```sql
ALTER TABLE feedbacks ADD COLUMN plan_id INTEGER;          -- 可空，历史行保持 NULL
CREATE INDEX IF NOT EXISTS idx_feedbacks_plan_id ON feedbacks(plan_id);
```

- 幂等：先查 `PRAGMA table_info`，索引用 `IF NOT EXISTS`；每次启动全量重跑也安全
- 旧反馈一律不回填：没有可靠的计划来源，回填等于伪造关联
- 不加外键（第一阶段不做全局 FK 改造）
- 降级保护不变：V6 库用旧代码打开会报"版本高于程序支持"

## 三、接口约定

`POST /feedback` 新增可选字段 `plan_id`（正整数）。

| 场景 | 状态码 | 说明 |
| --- | --- | --- |
| 省略 `plan_id` | 200 | 旧客户端行为，`plan_id` 存 NULL |
| 计划存在且任务名一致 | 200 | 正常关联 |
| 计划不存在 | 400 `invalid_plan_id` | 不写库 |
| 任务名不一致 | 400 `plan_task_mismatch` | 不写库 |
| `"12"` / `true` / `0` | 422 | `strict=True` + `ge=1`，不做隐式转换 |

`GET /feedback` 每条记录新增 `plan_id`（历史数据为 `null`）。

边界决定：不校验计划日期；不限计划状态；同一计划允许多条反馈（重学/补做）。

## 四、测试

运行方式（项目根目录）：

```powershell
.\.venv\Scripts\python.exe test_database_migrations.py
.\.venv\Scripts\python.exe test_feedback_plan_link.py
.\.venv\Scripts\python.exe test_plan_service.py
.\.venv\Scripts\python.exe test_plan_api.py
.\.venv\Scripts\python.exe test_plan_frontend_flow.py
.\.venv\Scripts\python.exe test_agent_tools.py
.\.venv\Scripts\python.exe test_save_plan_tool.py
```

实测结果（2026-11-10）：

| 文件 | 改动前 | 改动后 |
| --- | --- | --- |
| test_database_migrations.py | 27 PASS | 31 PASS（+4 条 V6 断言） |
| test_plan_service.py | 70 PASS | 70 PASS |
| test_plan_api.py | 50 PASS | 50 PASS |
| test_plan_frontend_flow.py | 34 PASS（exit 1，Ollama 未启动） | 34 PASS（同上） |
| test_agent_tools.py | 25 PASS（exit 1，Ollama 未启动） | 25 PASS（同上） |
| test_save_plan_tool.py | 41 PASS（exit 1，Ollama 未启动） | 41 PASS（同上） |
| test_feedback_plan_link.py | — | 16 用例全部通过 |

说明：三个文件 exit=1 都是本机 `localhost:11434` 没有 Ollama 进程（`bge-m3` 嵌入调用失败），
改动前用干净的 `fb3829c` worktree 跑同样失败，属环境问题，与本次改动无关。
断言级 FAIL 数为 0。

## 五、测试技术基线（更新）

- 本项目**不使用 pytest**，`test_*.py` 是自写脚本：`check(name, condition)` + `PASS:` 行 + 直接运行
- 不新增测试依赖；新测试保持 `def test_xxx(): assert ...` 形式并带 `__main__` 运行器，
  将来若引入 pytest 可直接收集，不需要改写
- 所有测试必须使用独立夹具库（`_test_*.db`），并断言真实 `feedback.db` 字节数不变
- 需要断言"服务端拒绝"的用例，必须同时断言数据库行数不变（防止静默写库）

## 六、已知限制

1. `plans` 没有删除 API，但仍不存在外键：人工改库可能产生悬空 `plan_id`；读取侧只展示、不 JOIN
2. 任务名比较是"折叠空白后完全相等"，不做模糊匹配；计划内容被人工改写后，旧反馈不会被追溯
3. 未校验计划日期，跨天提交同一条计划也被接受（已固化为测试，避免以后误改）
