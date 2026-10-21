# AI Study Assistant — 生产镜像（云端 LLM/Embedding 默认，无需 Ollama）
#
# 构建与运行见 README「部署交付」章节。
# 约定：.env / feedback.db / .rag_cache 绝不进入镜像（见 .dockerignore）；
# 运行时通过 -e / --env-file 注入密钥，数据与向量缓存挂载卷持久化。

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖层单独缓存：requirements 不变时不重新装包
COPY requirements.txt .
RUN pip install -r requirements.txt

# 应用代码与前端（.dockerignore 已排除密钥/数据/缓存）
COPY *.py ./
COPY static/ ./static/
COPY knowledge/ ./knowledge/

# 数据目录（SQLite 数据库 + 向量缓存）挂载卷，容器重建不丢数据
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# 默认把数据库与向量缓存指到挂载卷（可被 -e 覆盖）
ENV FEEDBACK_DB_PATH=/app/data/feedback.db \
    RAG_CACHE_DIR=/app/data/.rag_cache

EXPOSE 8000

# 健康检查走 /health（轻量，不调用付费模型）
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
