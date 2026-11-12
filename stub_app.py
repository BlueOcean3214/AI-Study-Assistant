"""测试用 ASGI 入口：装上确定性 Embedding 打桩后，暴露与 `main:app` 相同的应用。

用途：需要「真实 HTTP 服务」但不想依赖本地 Ollama / 云端 Key 的集成测试。
例如 test_upload.py 会子进程启动 uvicorn，如果让它直接跑 `main:app`，
RAG 检索就会去连 localhost:11434（或要求云端 Key）——迁移到 API 之后这不该是必需依赖。

注意：这里只打桩 Embedding（向量质量见 stub_embedding 的能力边界说明），
对话模型仍然需要真实后端；需要对话的用例请先用 real_backend.chat_ready() 判定再跑。
"""

import stub_embedding

stub_embedding.install()

from main import app  # noqa: E402  （必须在打桩之后导入）
