# 演示指南（Demo Guide）

从零启动到展示核心流程，全程可复现。演示默认使用**本地 Ollama**（零费用）；配置云端密钥后同一套步骤可切云端模型。

## 0. 前置准备（一次性）

```powershell
# 1) Python 虚拟环境 + 依赖
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2) 配置：复制模板并填写
Copy-Item .env.example .env
#   - 本地演示：LLM_PROVIDER=ollama / EMBEDDING_PROVIDER=ollama（见 .env.example 回退段）
#   - 云端演示：填写 LLM_API_KEY / EMBEDDING_API_KEY（以百炼控制台为准）
#   - .env 已被 .gitignore 排除，不会入库

# 3) 本地模型（演示用，约几分钟下载）
ollama pull bge-m3      # Embedding（1024 维）
ollama pull qwen3.5:4b  # 对话/草案（CPU 可跑，长输出 1-3 分钟）
```

## 1. 启动服务

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --port 8000
```

验证（不调用付费模型）：

```powershell
Invoke-WebRequest http://localhost:8000/health   # {"status":"ok", ...}
Invoke-WebRequest http://localhost:8000/ready    # {"ready":true, ... schema_version:5}
```

前端入口（浏览器打开）：

| 页面 | URL |
| --- | --- |
| 今日计划（主推） | http://localhost:8000/app/plan.html |
| RAG 问答（流式） | http://localhost:8000/app/ask.html |
| 任务详情 | http://localhost:8000/app/detail.html |
| 学习计时 | http://localhost:8000/app/study.html |
| 学习反馈 | http://localhost:8000/app/feedback.html |

## 2. 核心演示流程（约 10 分钟）

### 演示 A：RAG 问答 + 流式输出 + 来源可追溯（3 分钟）

1. 打开 `ask.html`，提问「定积分怎么学」。
2. 观察：回答**流式渐显**（SSE），下方列出命中的来源文件与片段。
3. 要点讲解：
   - 检索命中低于阈值（MIN_SCORE=0.5）时自动降级为无资料回答，不编造；
   - 进入 Prompt 的上下文带 `[来源: xxx]` 标签、`(source, content)` 去重、`CONTEXT_MAX_CHARS` 字符预算（超限整块跳过并记日志，不截断来源）；
   - `/ask` 与 `/ask/stream` 共用同一上下文组装，行为一致。

### 演示 B：计划草案 → 用户确认 → 保存（5 分钟，核心亮点）

1. 打开 `plan.html`，点击「生成今日计划草案」。
2. 等待草案卡出现（本地模型约 1–3 分钟；卡片明确标注"待确认 · 尚未保存"）。
3. 点击确认保存，观察三步流程：签发凭证 → 用户确认 → 事务写入，页面恢复为"已保存"。
4. 刷新页面：服务端数据优先，计划仍然存在（不是前端本地状态）。
5. **安全边界讲解**（可现场演示拒绝）：
   - 未确认/伪造 `confirmation_id` 保存 → 服务端拒绝（404/410/409）；
   - 凭证 30 分钟过期、一次性消费、与计划内容 hash 绑定；
   - Agent 工具面没有删除反馈/改知识库的能力。

### 演示 C：反馈回流（2 分钟）

1. 在 `feedback.html` 提交一条学习反馈（或用 `detail.html` 勾选子任务）。
2. 再次生成草案：Agent 通过 `get_recent_feedback` 读取真实反馈并体现在计划中。
3. 查看 `model_usage` 记录（可选）：每次模型调用的 token/耗时/状态都有落库。

### 可选：接口直测

```powershell
Invoke-WebRequest "http://localhost:8000/ask?question=定积分怎么学"
Invoke-WebRequest "http://localhost:8000/ask/stream?question=定积分怎么学"   # SSE
```

## 3. Docker 演示（可选）

```powershell
docker build -t ai-study-assistant:delivery .
docker run -d -p 8000:8000 --env-file .env -v app-data:/app/data ai-study-assistant:delivery
Invoke-WebRequest http://localhost:8000/health
```

（交付验收已实测：镜像构建成功、容器 `/health` 200、`/ready` ready=true、容器内 DB 自动迁移到 v5。）

## 4. 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| `/ask` 返回 `error: ... Ollama ...` | Ollama 未启动或模型未 pull；`ollama serve` / `ollama pull bge-m3 qwen3.5:4b` |
| 草案生成很慢（>1 分钟） | 本地 CPU 推理属正常；调小计划复杂度或改用云端模型 |
| 刷新后草案消失 | 草案是临时态（sessionStorage），未保存即丢弃是**预期行为**；已保存计划刷新后仍在 |
| 重启后未确认的凭证失效 | Confirmation 为进程内存态（已知限制），重新生成草案即可 |
| 端口被占用 | `uvicorn main:app --port 8001` 换端口 |
