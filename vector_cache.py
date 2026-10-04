"""向量缓存持久化：把 chunk 的 embedding 存到本地文件，重启后直接复用。

只负责"存 / 取 / 压实"，不涉及检索逻辑，也不依赖 embedding_service。
全部使用标准库（json / base64 / hashlib / array / os / pathlib）。

磁盘结构：
    .rag_cache/meta.json          缓存元信息（模型、维度、编码格式、模型指纹）
    .rag_cache/embeddings.jsonl   一行一条 {"h": <sha256>, "v": <base64 float32>}

内存结构（由调用方维护）：
    {content_hash: array("f")}

约定：
- 键是 chunk 正文的 SHA-256（完整 64 位），不是 source，也不是序号。
- 正文相同 → 同一个键 → 同一个向量（跨文件复用）。
- 正文变化 → 键变化 → 自动未命中，旧键变成无人引用的死条目（由 compact_cache 清理）。
- 追加写：每批 embedding 成功后立即 append，不需要重写整个文件。
- meta.json 用"临时文件 + os.replace"原子写入；embeddings.jsonl 追加时即使最后一行
  被截断，读取时也只会跳过那一行。
"""

import array
import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


# 缓存结构版本：解析方式或字段含义变化时 +1，旧缓存会被整体忽略
FORMAT = 1

# 向量编码方式：float32 + base64（比 JSON 数字文本小 2.5 倍）
VECTOR_ENCODING = "base64-float32"

META_FILENAME = "meta.json"
VECTORS_FILENAME = "embeddings.jsonl"

# 缓存目录：默认放在项目根目录下的 .rag_cache/，
# 可以用环境变量 RAG_CACHE_DIR 覆盖（测试隔离或换目录时用）。
CACHE_DIR = Path(
    os.environ.get("RAG_CACHE_DIR")
    or (Path(__file__).resolve().parent / ".rag_cache")
)


def cache_dir():
    """当前缓存目录（每次都从模块变量读取，方便测试 patch）。"""

    return Path(CACHE_DIR)


def meta_path():
    return cache_dir() / META_FILENAME


def vectors_path():
    return cache_dir() / VECTORS_FILENAME


def chunk_hash(content):
    """chunk 正文的 SHA-256（完整 64 位十六进制，不截断）。

    必须传入与切片/embedding 实际使用的同一份归一化正文，
    否则换行符差异会产生两个不同的键。
    """

    if not isinstance(content, str):
        raise TypeError("chunk 正文必须是字符串")

    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def to_float32(vector):
    """统一成 float32 数组：与磁盘往返后的精度一致，内存占用约为 list 的 1/8。"""

    return array.array("f", vector)


def encode_vector(vector):
    """向量 -> base64(float32) 字符串。"""

    return base64.b64encode(to_float32(vector).tobytes()).decode("ascii")


def decode_vector(text):
    """base64(float32) 字符串 -> float32 数组。"""

    return array.array("f", base64.b64decode(text))


def build_meta(model, dim, model_digest=None, chunk_max_chars=None):
    """构造 meta.json 的内容。"""

    return {
        "format": FORMAT,
        "model": model,
        "dim": dim,
        "vector_encoding": VECTOR_ENCODING,
        "chunk_max_chars": chunk_max_chars,
        "model_digest": model_digest,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def check_meta(meta, model, expected_dim=None, model_digest=None):
    """校验 meta 是否还能用，返回 (是否可用, 原因)。

    - format / model / vector_encoding 必须一致；
    - 传了 expected_dim 就校验维度；
    - 双方都有 model_digest 且不一致，说明模型权重被就地更新过，判定不可用。
    """

    if not isinstance(meta, dict):
        return False, "meta.json 缺失或不是 JSON 对象"

    if meta.get("format") != FORMAT:
        return False, f"缓存格式不一致（文件 {meta.get('format')} != 当前 {FORMAT}）"

    if meta.get("model") != model:
        return False, f"模型不一致（缓存 {meta.get('model')} != 当前 {model}）"

    if meta.get("vector_encoding") != VECTOR_ENCODING:
        return False, f"向量编码不一致（{meta.get('vector_encoding')}）"

    if expected_dim is not None and meta.get("dim") != expected_dim:
        return False, f"维度不一致（缓存 {meta.get('dim')} != 当前 {expected_dim}）"

    cached_digest = meta.get("model_digest")
    if cached_digest and model_digest and cached_digest != model_digest:
        return False, "模型权重已更新（model_digest 变化）"

    return True, ""


def read_meta():
    """读取 meta.json；缺失或损坏时返回 None（调用方按"无缓存"处理）。"""

    path = meta_path()

    if not path.exists():
        return None

    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as error:
        print("缓存 meta 读取失败，将忽略旧缓存：", error)
        return None


def write_meta(meta):
    """原子写入 meta.json（临时文件 + os.replace）。"""

    path = meta_path()
    temp_path = path.parent / (path.name + ".tmp")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)

        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(meta, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temp_path, path)
        return True
    except OSError as error:
        print("缓存 meta 写入失败（不影响检索）：", error)
        return False


def read_vectors():
    """逐行读取 embeddings.jsonl，返回 {hash: array("f")}。

    坏行/末行截断只跳过那一行并打印告警，不会让调用方失败。
    """

    path = vectors_path()
    vectors = {}

    if not path.exists():
        return vectors

    skipped = 0

    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()

                if not line:
                    continue

                try:
                    record = json.loads(line)
                    vectors[record["h"]] = decode_vector(record["v"])
                except (ValueError, KeyError, TypeError) as error:
                    skipped += 1

                    if skipped <= 3:
                        print(f"跳过损坏的缓存行（第 {line_number} 行）：{type(error).__name__}")
    except OSError as error:
        print("缓存向量读取失败，将忽略旧缓存：", error)
        return {}

    if skipped:
        print(f"向量缓存共跳过 {skipped} 行损坏记录")

    return vectors


def append_vectors(vectors):
    """把 {hash: vector} 追加到 embeddings.jsonl，返回写入条数。

    每个 embedding 批次成功后调用一次，因此中途失败不会丢掉已成功的批次。
    """

    if not vectors:
        return 0

    path = vectors_path()

    try:
        path.parent.mkdir(parents=True, exist_ok=True)

        with path.open("a", encoding="utf-8", newline="\n") as handle:
            for content_hash, vector in vectors.items():
                record = {"h": content_hash, "v": encode_vector(vector)}
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")

            handle.flush()
            os.fsync(handle.fileno())

        return len(vectors)
    except OSError as error:
        print("向量缓存追加失败（不影响检索）：", error)
        return 0


def compact_cache(referenced_hashes):
    """只保留被引用的 hash，返回 (保留数, 删除数)。

    显式调用才压实；平时只追加，不重写整个文件。
    """

    referenced = set(referenced_hashes)
    path = vectors_path()

    if not path.exists():
        return 0, 0

    kept = []
    removed = 0

    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()

                if not line:
                    continue

                try:
                    record = json.loads(line)
                except ValueError:
                    removed += 1
                    continue

                if record.get("h") in referenced:
                    kept.append(line)
                else:
                    removed += 1

        temp_path = path.parent / (path.name + ".tmp")

        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            for line in kept:
                handle.write(line + "\n")

            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temp_path, path)
    except OSError as error:
        print("缓存压实失败：", error)
        return len(kept), removed

    return len(kept), removed


def archive_cache(reason=""):
    """把当前缓存文件改名归档（模型/格式变化时用），返回归档后的路径或 None。"""

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    archived = None

    for path in (meta_path(), vectors_path()):
        if not path.exists():
            continue

        target = path.parent / f"{path.name}.{stamp}.bak"

        try:
            os.replace(path, target)
            archived = target
        except OSError as error:
            print("缓存归档失败：", error)

    if archived:
        print(f"旧缓存已归档（{reason}）：{archived.name}")

    return archived
