# AI Study Assistant

面向考研学习的个人 AI 学习助手 MVP。项目根据学习反馈生成下一次学习计划，支持查看动态子任务、记录任务完成情况，并将学习反馈保存在本地 SQLite 数据库中。

## 功能

- 今日计划与任务详情页面
- 根据历史反馈生成下一次计划
- 子任务完成勾选与学习计时
- 任务完成反馈及历史记录
- 基于历史反馈生成学习分析

## 技术栈

- Python、FastAPI、Uvicorn
- SQLite
- Ollama（模型：`qwen3.5:4b`）
- HTML、CSS、JavaScript

## 环境要求

- Python 3.10 或更高版本
- Ollama 已安装并运行，且已准备项目使用的 `qwen3.5:4b` 模型

当前 Ollama 地址和模型名配置在 `main.py`：`http://localhost:11434/api/chat` 和 `qwen3.5:4b`。

## 安装与启动

在项目根目录打开 PowerShell：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

确保 Ollama 已启动并可用后，在项目根目录运行：

```powershell
uvicorn main:app --reload
```

启动后访问：

- 今日计划页面：<http://127.0.0.1:8000/app/plan.html>
- API 文档：<http://127.0.0.1:8000/docs>
- 健康入口：<http://127.0.0.1:8000/>

首次运行时，如果数据库没有学习反馈，`/next-plan` 会提示暂无反馈数据。可以先通过 `/docs` 调用 `POST /feedback` 添加一条初始记录，再打开计划页。

示例请求：

```json
{
  "task": "定积分基础练习",
  "estimated_minutes": 30,
  "actual_minutes": 25,
  "status": "completed",
  "reason": null,
  "completed_subtasks": 2,
  "total_subtasks": 2,
  "completed_questions": 4,
  "total_questions": 4
}
```

## API

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/` | 返回服务名称 |
| `GET` | `/ask?question=...` | 调用本地模型回答问题 |
| `POST` | `/feedback` | 保存一条任务完成反馈 |
| `GET` | `/feedback` | 查询反馈历史 |
| `GET` | `/analyze` | 根据历史反馈生成学习分析 |
| `GET` | `/next-plan` | 根据历史反馈生成下一次学习计划 |
| `DELETE` | `/feedback` | 清空全部反馈记录 |

## 本地数据

应用在项目目录中创建 `feedback.db` 保存 SQLite 数据。`feedback_backup.json` 是可选的旧数据迁移来源：数据库为空时，应用会尝试导入它。数据库和备份文件属于本地运行数据，已加入 `.gitignore`，不应提交包含个人学习记录的文件。

## 项目结构

```text
.
├── main.py
├── static/
│   ├── plan.html
│   ├── detail.html
│   ├── study.html
│   ├── feedback.html
│   └── local-date.js
├── requirements.txt
└── README.md
```
