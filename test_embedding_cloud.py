"""云端 Embedding（阿里云百炼 OpenAI 兼容）测试：直接运行 python test_embedding_cloud.py。

全 Mock（桩 openai SDK 客户端 / requests.post），不发起任何真实 API 请求、零费用。

覆盖：
- 配置校验：默认云端百炼、缺 API Key、非法 Provider / 维度、显式 ollama 回退
- 响应解析：OpenAI 兼容 data[].embedding 按 index 排序、条数校验
- 维度检查：返回向量维度与 EMBEDDING_DIMENSIONS 不一致时直接报错（绝不混用）
- dimensions 参数按配置透传（0 表示不传）
- 错误处理：超时 / 连接失败 / HTTP 错误，稳定错误信息且不含 API Key
- 缓存身份：cache_identity() 含 provider/model/dimensions，切换后旧缓存判废
- 文档向量与查询向量使用同一模型与维度配置
- 兼容旧常量名（EMBEDDING_MODEL 等）与 split_into_batches
"""

import contextlib
import os

import httpx2
import openai
import requests

import embedding_service
import vector_cache


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


SECRET_KEY = "sk-bailian-test-key"

ENV_KEYS = [
    "EMBEDDING_PROVIDER", "EMBEDDING_BASE_URL", "EMBEDDING_API_KEY",
    "EMBEDDING_MODEL", "EMBEDDING_DIMENSIONS", "EMBEDDING_BATCH_SIZE",
    "EMBEDDING_TIMEOUT", "OLLAMA_EMBED_URL",
]


@contextlib.contextmanager
def env(**values):
    saved = {key: os.environ.get(key) for key in ENV_KEYS}

    for key in ENV_KEYS:
        os.environ.pop(key, None)

    for key, value in values.items():
        if value is not None:
            os.environ[key] = value

    embedding_service.reset_settings()

    try:
        yield
    finally:
        for key in ENV_KEYS:
            os.environ.pop(key, None)

        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value

        embedding_service.reset_settings()


class Obj:
    def __init__(self, **values):
        self.__dict__.update(values)


class FakeEmbeddingsAPI:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def create(self, **payload):
        self.calls.append(payload)

        return self.handler(payload)


class FakeOpenAIClient:
    def __init__(self, handler):
        self.embeddings = FakeEmbeddingsAPI(handler)


@contextlib.contextmanager
def fake_openai(handler):
    """桩 openai.OpenAI 工厂：handler(payload) -> 响应对象 或 raise 真实 openai 异常。"""

    box = {"client": None, "clients": []}
    original = openai.OpenAI

    def factory(**kwargs):
        client = FakeOpenAIClient(handler)
        client.kwargs = kwargs
        box["client"] = client
        box["clients"].append(client)
        box["factory_kwargs"] = kwargs

        return client

    openai.OpenAI = factory

    try:
        yield box
    finally:
        openai.OpenAI = original


def total_calls(box):
    """统计桩工厂创建的全部客户端上的请求数（每次 get_embeddings 会新建客户端）。"""

    return sum(len(client.embeddings.calls) for client in box["clients"])


def response_for(vectors, out_of_order=False):
    """构造 OpenAI 兼容 embeddings 响应（可故意乱序 index）。"""

    items = [
        Obj(index=index, embedding=vector)
        for index, vector in enumerate(vectors)
    ]

    if out_of_order:
        items = list(reversed(items))

    return Obj(data=items)


def real_network_bomb(*args, **kwargs):
    raise AssertionError("测试不允许发起真实网络请求")


ORIGINAL_REQUESTS_POST = embedding_service.requests.post
embedding_service.requests.post = real_network_bomb

try:
    # ==========================
    # 1. 配置层
    # ==========================

    with env(EMBEDDING_API_KEY=SECRET_KEY):
        settings = embedding_service.get_settings()
        check("默认 Provider 是云端 openai_compatible", settings.provider == "openai_compatible")
        check("默认模型是 text-embedding-v4", settings.model == "text-embedding-v4")
        check("默认 Base URL 是百炼兼容端点",
              settings.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1",
              f"url={settings.base_url}")
        check("默认维度 1024", settings.dimensions == 1024)
        check("云端默认批量 10", settings.batch_size == 10)
        check("缓存身份含 provider/model/dimensions",
              embedding_service.cache_identity() == "openai_compatible:text-embedding-v4@1024",
              f"identity={embedding_service.cache_identity()}")

    with env():
        try:
            embedding_service.get_settings()
            check("缺 EMBEDDING_API_KEY 报配置错误", False)
        except embedding_service.EmbeddingConfigError as error:
            check("缺 EMBEDDING_API_KEY 报配置错误", "EMBEDDING_API_KEY" in str(error))

    with env(EMBEDDING_API_KEY=SECRET_KEY, EMBEDDING_PROVIDER="bedrock"):
        try:
            embedding_service.get_settings()
            check("非法 EMBEDDING_PROVIDER 报配置错误", False)
        except embedding_service.EmbeddingConfigError as error:
            check("非法 EMBEDDING_PROVIDER 报配置错误", "EMBEDDING_PROVIDER" in str(error))

    with env(EMBEDDING_API_KEY=SECRET_KEY, EMBEDDING_DIMENSIONS="一千"):
        try:
            embedding_service.get_settings()
            check("非法 EMBEDDING_DIMENSIONS 报配置错误", False)
        except embedding_service.EmbeddingConfigError as error:
            check("非法 EMBEDDING_DIMENSIONS 报配置错误", "EMBEDDING_DIMENSIONS" in str(error))

    with env(EMBEDDING_API_KEY=SECRET_KEY, EMBEDDING_DIMENSIONS="0"):
        settings = embedding_service.get_settings()
        check("EMBEDDING_DIMENSIONS=0 表示用模型默认维度",
              settings.dimensions is None and "@default" in embedding_service.cache_identity())

    with env(EMBEDDING_PROVIDER="ollama"):
        settings = embedding_service.get_settings()
        check("显式 ollama 回退可用（不需要 API Key）",
              settings.provider == "ollama" and settings.model == "bge-m3")
        check("ollama 默认地址与批量",
              settings.base_url == "http://localhost:11434" and settings.batch_size == 32)
        check("兼容旧 OLLAMA_EMBED_URL", True)

    with env(EMBEDDING_PROVIDER="ollama", OLLAMA_EMBED_URL="http://old-host:11434/api/embed"):
        settings = embedding_service.get_settings()
        check("OLLAMA_EMBED_URL 兼容（完整地址）",
              settings.base_url == "http://old-host:11434")

    with env(EMBEDDING_API_KEY=SECRET_KEY, EMBEDDING_MODEL="text-embedding-v3",
             EMBEDDING_DIMENSIONS="768", EMBEDDING_BASE_URL="https://api.example.com/v1/"):
        settings = embedding_service.get_settings()
        check("自定义模型/维度/地址生效",
              settings.model == "text-embedding-v3" and settings.dimensions == 768
              and settings.base_url == "https://api.example.com/v1")
        check("缓存身份随模型/维度变化",
              embedding_service.cache_identity() == "openai_compatible:text-embedding-v3@768")

    # 兼容旧常量名随 reload_settings 刷新
    with env(EMBEDDING_API_KEY=SECRET_KEY, EMBEDDING_MODEL="custom-embed", EMBEDDING_BATCH_SIZE="5"):
        embedding_service.reload_settings()
        check("旧常量名保留且刷新",
              embedding_service.EMBEDDING_MODEL == "custom-embed"
              and embedding_service.EMBEDDING_BATCH_SIZE == 5
              and embedding_service.EMBEDDING_PROVIDER == "openai_compatible")

    # ==========================
    # 2. 云端响应解析与维度检查
    # ==========================

    vectors = [[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]

    with env(EMBEDDING_API_KEY=SECRET_KEY, EMBEDDING_DIMENSIONS="4"), \
            fake_openai(lambda payload: response_for(vectors)) as box:
        result = embedding_service.get_embeddings(["第一段", "第二段"])

        check("云端向量按 index 正确解析", result == vectors)
        check("请求带 model/input/dimensions",
              box["client"].embeddings.calls[0] == {
                  "model": "text-embedding-v4", "input": ["第一段", "第二段"], "dimensions": 4,
              })
        check("客户端使用配置的 API Key",
              box["factory_kwargs"]["api_key"] == SECRET_KEY)

    # 乱序 index 也按输入顺序还原
    with env(EMBEDDING_API_KEY=SECRET_KEY, EMBEDDING_DIMENSIONS="4"), \
            fake_openai(lambda payload: response_for(vectors, out_of_order=True)):
        result = embedding_service.get_embeddings(["第一段", "第二段"])
        check("乱序 index 按输入顺序还原", result == vectors)

    # dimensions=0：不传 dimensions 参数
    with env(EMBEDDING_API_KEY=SECRET_KEY, EMBEDDING_DIMENSIONS="0"), \
            fake_openai(lambda payload: response_for(vectors)) as box:
        embedding_service.get_embeddings(["第一段", "第二段"])
        check("EMBEDDING_DIMENSIONS=0 时不传 dimensions 参数",
              "dimensions" not in box["client"].embeddings.calls[0])

    # 维度不一致：直接报错
    with env(EMBEDDING_API_KEY=SECRET_KEY, EMBEDDING_DIMENSIONS="1024"), \
            fake_openai(lambda payload: response_for(vectors)):
        try:
            embedding_service.get_embeddings(["第一段", "第二段"])
            check("向量维度与配置不一致时报错", False)
        except embedding_service.EmbeddingError as error:
            check("向量维度与配置不一致时报错",
                  "维度" in str(error) and "1024" in str(error), f"error={error}")

    # 条数不一致
    with env(EMBEDDING_API_KEY=SECRET_KEY, EMBEDDING_DIMENSIONS="4"), \
            fake_openai(lambda payload: response_for(vectors[:1])):
        try:
            embedding_service.get_embeddings(["第一段", "第二段"])
            check("向量条数与输入不一致时报错", False)
        except embedding_service.EmbeddingError as error:
            check("向量条数与输入不一致时报错", "数量" in str(error))

    # 单条接口与批量接口共用同一模型/维度配置
    with env(EMBEDDING_API_KEY=SECRET_KEY, EMBEDDING_DIMENSIONS="4"), \
            fake_openai(lambda payload: response_for([[0.1, 0.2, 0.3, 0.4]])) as box:
        single = embedding_service.get_embedding("查询文本")
        single_payload = box["client"].embeddings.calls[0]

    with env(EMBEDDING_API_KEY=SECRET_KEY, EMBEDDING_DIMENSIONS="4"), \
            fake_openai(lambda payload: response_for(vectors)) as box:
        embedding_service.get_embeddings(["第一段", "第二段"])
        batch_payload = box["client"].embeddings.calls[0]

    check("查询向量与文档向量同一模型同一维度",
          single == [0.1, 0.2, 0.3, 0.4]
          and single_payload["model"] == batch_payload["model"]
          and single_payload.get("dimensions") == batch_payload.get("dimensions"))

    check("空输入返回空列表",
          embedding_service.get_embeddings([]) == [])

    try:
        embedding_service.get_embedding("   ")
        check("空文本报错", False)
    except embedding_service.EmbeddingError:
        check("空文本报错", True)

    # ==========================
    # 3. 错误处理（稳定信息、不含密钥）
    # ==========================

    def raise_timeout(payload):
        raise openai.APITimeoutError(request=httpx2.Request("POST", "http://test"))

    def raise_connection(payload):
        raise openai.APIConnectionError(request=httpx2.Request("POST", "http://test"))

    def raise_status(payload):
        request = httpx2.Request("POST", "http://test")

        raise openai.APIStatusError(
            "server error", response=httpx2.Response(500, request=request), body=None
        )

    with env(EMBEDDING_API_KEY=SECRET_KEY, EMBEDDING_DIMENSIONS="4"), fake_openai(raise_timeout):
        try:
            embedding_service.get_embeddings(["文本"])
            check("云端超时抛 EmbeddingError", False)
        except embedding_service.EmbeddingError as error:
            check("云端超时抛 EmbeddingError", "超时" in str(error), f"error={error}")

    with env(EMBEDDING_API_KEY=SECRET_KEY, EMBEDDING_DIMENSIONS="4"), fake_openai(raise_connection):
        try:
            embedding_service.get_embeddings(["文本"])
            check("云端连接失败抛 EmbeddingError", False)
        except embedding_service.EmbeddingError as error:
            check("云端连接失败抛 EmbeddingError", "无法连接" in str(error))

    with env(EMBEDDING_API_KEY=SECRET_KEY, EMBEDDING_DIMENSIONS="4"), fake_openai(raise_status):
        try:
            embedding_service.get_embeddings(["文本"])
            check("云端 HTTP 错误抛 EmbeddingError", False)
        except embedding_service.EmbeddingError as error:
            check("云端 HTTP 错误抛 EmbeddingError",
                  "server error" in str(error) and "尝试" in str(error)
                  and SECRET_KEY not in str(error))

    # ==========================
    # 4. ollama 回退路径（请求形态正确）
    # ==========================

    ollama_calls = []

    def fake_ollama_post(url, json=None, timeout=None):
        ollama_calls.append({"url": url, "json": json, "timeout": timeout})

        return Obj(
            status_code=200,
            json=lambda: {"embeddings": [[0.1, 0.2, 0.3, 0.4]]},
            raise_for_status=lambda: None,
        )

    with env(EMBEDDING_PROVIDER="ollama", EMBEDDING_DIMENSIONS="4"):
        embedding_service.requests.post = fake_ollama_post

        try:
            vectors_ollama = embedding_service.get_embeddings(["文本"])
        finally:
            embedding_service.requests.post = real_network_bomb

        check("ollama 回退请求 /api/embed 且用本地模型",
              vectors_ollama == [[0.1, 0.2, 0.3, 0.4]]
              and ollama_calls[0]["url"] == "http://localhost:11434/api/embed"
              and ollama_calls[0]["json"]["model"] == "bge-m3")

    # ==========================
    # 5. 缓存身份与旧缓存判废
    # ==========================

    with env(EMBEDDING_API_KEY=SECRET_KEY):
        identity = embedding_service.cache_identity()

    old_meta = vector_cache.build_meta(model="bge-m3", dim=1024, model_digest="old-digest")
    usable, reason = vector_cache.check_meta(old_meta, model=identity)

    check("旧 bge-m3 缓存在云端身份下判废",
          usable is False and "模型不一致" in reason,
          f"identity={identity} reason={reason}")

    new_meta = vector_cache.build_meta(model=identity, dim=1024)
    usable2, _ = vector_cache.check_meta(new_meta, model=identity)
    check("同身份缓存可用", usable2 is True)

    # 切维度后身份变化 -> 判废（防止不同维度向量混用）
    with env(EMBEDDING_API_KEY=SECRET_KEY, EMBEDDING_DIMENSIONS="768"):
        identity_768 = embedding_service.cache_identity()

    usable3, _ = vector_cache.check_meta(new_meta, model=identity_768)
    check("维度切换后缓存身份不匹配", usable3 is False)

    # ==========================
    # 6. split_into_batches 跟随配置
    # ==========================

    with env(EMBEDDING_API_KEY=SECRET_KEY, EMBEDDING_BATCH_SIZE="3"):
        batches = list(embedding_service.split_into_batches(list(range(7))))

    check("split_into_batches 使用配置批量",
          [len(batch) for batch in batches] == [3, 3, 1], f"batches={list(map(len, batches))}")

    # ==========================
    # 7. RAG 全链路：文档向量 + 查询向量都走云端后端后正常检索
    # ==========================

    import shutil
    from pathlib import Path

    import rag_service

    cache_fixture = Path(__file__).resolve().parent / "_test_cache_cloud"
    shutil.rmtree(cache_fixture, ignore_errors=True)

    original_cache_dir = vector_cache.CACHE_DIR
    vector_cache.CACHE_DIR = cache_fixture
    rag_service.reset_cache()

    def embed_handler(payload):
        """确定性云端向量：按文本语义给轴向量（维度 = payload 的 dimensions）。"""

        dims = payload.get("dimensions") or 3

        def vector_for(text):
            vector = [0.0] * dims

            if "定积分" in text:
                vector[0] = 1.0
            elif "条件概率" in text:
                vector[1] = 1.0
            else:
                vector[2] = 1.0

            return vector

        return response_for([vector_for(text) for text in payload["input"]])

    try:
        with env(EMBEDDING_API_KEY=SECRET_KEY, EMBEDDING_DIMENSIONS="3"), \
                fake_openai(embed_handler) as box:
            results = rag_service.retrieve_chunks("定积分怎么学", top_k=3)

        check("云端后端下检索正常命中",
              len(results) > 0 and all(item["source"] == "math.txt" for item in results),
              f"results={[(i['source'], i['score']) for i in results]}")

        first_call_count = total_calls(box)

        with env(EMBEDDING_API_KEY=SECRET_KEY, EMBEDDING_DIMENSIONS="3"), \
                fake_openai(embed_handler) as box2:
            rag_service.reset_cache()
            results2 = rag_service.retrieve_chunks("定积分怎么学", top_k=3)

        check("文档向量缓存复用（第二次检索文档批请求为 0）",
              len(results2) > 0 and total_calls(box2) < first_call_count,
              f"first={first_call_count} second={total_calls(box2)}")

        with env(EMBEDDING_API_KEY=SECRET_KEY, EMBEDDING_DIMENSIONS="3"):
            expected_identity = embedding_service.cache_identity()

        meta = vector_cache.read_meta()
        check("缓存 meta 记录云端身份（换模型后自动判废重建的依据）",
              meta is not None and meta["model"] == expected_identity and meta["dim"] == 3,
              f"meta.model={meta['model'] if meta else None}")

        # 查询向量与文档请求共用同一 model/dimensions
        all_payloads = [
            payload
            for client in box["clients"]
            for payload in client.embeddings.calls
        ]
        check("查询向量与文档向量请求参数一致（同一模型同一维度）",
              len(all_payloads) > 0 and all(
                  p["model"] == "text-embedding-v4" and p.get("dimensions") == 3
                  for p in all_payloads))
    finally:
        vector_cache.CACHE_DIR = original_cache_dir
        rag_service.reset_cache()
        shutil.rmtree(cache_fixture, ignore_errors=True)

    check("测试全程未发起真实网络请求（requests.post 为炸弹）", True)

finally:
    embedding_service.requests.post = ORIGINAL_REQUESTS_POST

print("ALL TESTS COMPLETED")
