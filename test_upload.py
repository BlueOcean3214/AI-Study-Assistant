"""POST /upload 接口测试：直接运行 python test_upload.py。

不需要 httpx：本文件自己启动一个 uvicorn 进程（不带 --reload，便于干净退出），
再用 requests 访问真实 HTTP 接口。
也可以用 UPLOAD_TEST_BASE_URL 指定一个已经在跑的服务，此时不再自己启动。

覆盖需求：正常上传 / source / chunk_count / 非 txt / 超 2MB / 空文件名 /
路径穿越 / 上传后立即可检索 / /ask 与其它接口未受影响。
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests

PROJECT_DIR = Path(__file__).resolve().parent
KNOWLEDGE_DIR = PROJECT_DIR / "knowledge"
SERVER_LOG = PROJECT_DIR / "_test_upload_server.log"

MAX_UPLOAD_BYTES = 2 * 1024 * 1024

UPLOAD_NAME = "_test_upload_条件概率.txt"
UPLOAD_CONTENT = (
    "条件概率是概率论中的重要概念。\n\n"
    "条件概率表示在已知事件发生的条件下，另一个事件发生的概率。\n\n"
    "计算公式：P(A|B) = P(AB) / P(B)。\n"
).encode("utf-8")

CREATED_FILES = [UPLOAD_NAME, "evil_test_upload.txt", "evil_win_test_upload.txt"]


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"FAILED: {name} {detail}")
    print(f"PASS: {name} {detail}")


def cleanup_files():
    for filename in CREATED_FILES:
        target = KNOWLEDGE_DIR / filename
        if target.exists():
            target.unlink()

    for escape in (
        PROJECT_DIR / "evil_test_upload.txt",
        PROJECT_DIR / "evil_win_test_upload.txt",
        PROJECT_DIR.parent / "evil_test_upload.txt",
        PROJECT_DIR.parent / "evil_win_test_upload.txt",
    ):
        if escape.exists():
            escape.unlink()


def pick_free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until_ready(base_url, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(f"{base_url}/", timeout=2).status_code == 200:
                return True
        except requests.exceptions.RequestException:
            time.sleep(0.4)
    return False


cleanup_files()
if SERVER_LOG.exists():
    SERVER_LOG.unlink()

external_base_url = os.environ.get("UPLOAD_TEST_BASE_URL")
process = None
log_handle = None

if external_base_url:
    base_url = external_base_url.rstrip("/")
    print(f"使用已有服务：{base_url}")
else:
    port = pick_free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_handle = SERVER_LOG.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(PROJECT_DIR),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    print(f"已启动测试服务：{base_url}（日志：{SERVER_LOG.name}）")

try:
    if not wait_until_ready(base_url):
        print(SERVER_LOG.read_text(encoding="utf-8")[-2000:] if SERVER_LOG.exists() else "无日志")
        raise SystemExit("测试服务启动失败")

    # 0. /docs 与 OpenAPI 里必须能看到 POST /upload
    openapi = requests.get(f"{base_url}/openapi.json", timeout=10).json()
    check("OpenAPI 暴露 POST /upload", "/upload" in openapi["paths"] and "post" in openapi["paths"]["/upload"])
    check(
        "requestBody 为 multipart/form-data",
        "multipart/form-data" in openapi["paths"]["/upload"]["post"]["requestBody"]["content"],
    )
    check("GET /docs 可访问", requests.get(f"{base_url}/docs", timeout=10).status_code == 200)

    # 1~3. 正常上传
    response = requests.post(
        f"{base_url}/upload",
        files={"file": (UPLOAD_NAME, UPLOAD_CONTENT, "text/plain")},
        timeout=30,
    )
    check("正常上传返回 200", response.status_code == 200, f"status={response.status_code}")
    payload = response.json()
    check("返回 message", payload.get("message") == "文档上传成功", f"payload={payload}")
    check("返回 source", payload.get("source") == UPLOAD_NAME, f"source={payload.get('source')}")
    check("返回 chunk_count > 0", payload.get("chunk_count", 0) > 0, f"chunk_count={payload.get('chunk_count')}")
    check("返回 size_bytes 正确", payload.get("size_bytes") == len(UPLOAD_CONTENT), f"size={payload.get('size_bytes')}")
    check("文件确实落在 knowledge/ 下", (KNOWLEDGE_DIR / UPLOAD_NAME).exists())

    # 重复上传相同内容应幂等（document_service 的行为）
    repeat = requests.post(
        f"{base_url}/upload",
        files={"file": (UPLOAD_NAME, UPLOAD_CONTENT, "text/plain")},
        timeout=30,
    )
    check("重复上传相同内容仍成功", repeat.status_code == 200, f"status={repeat.status_code}")

    # chunk_count 要与 document_service 的切片结果一致
    import document_service

    expected_chunks = len(
        document_service.chunk_text(
            document_service.read_text(KNOWLEDGE_DIR / UPLOAD_NAME),
            UPLOAD_NAME,
        )
    )
    check(
        "chunk_count 与 document_service 一致",
        payload["chunk_count"] == expected_chunks,
        f"{payload['chunk_count']} == {expected_chunks}",
    )

    # 4. 非 txt 被拒绝
    for bad_name, bad_type in (
        ("notes.pdf", "application/pdf"),
        ("word.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("pic.png", "image/png"),
        ("photo.jpg", "image/jpeg"),
    ):
        bad = requests.post(
            f"{base_url}/upload",
            files={"file": (bad_name, b"dummy-bytes", bad_type)},
            timeout=30,
        )
        check(
            f"拒绝非 txt 文件 {bad_name}",
            bad.status_code == 400 and bad.json().get("message") == "目前只支持 .txt 文件",
            f"status={bad.status_code} body={bad.text[:80]}",
        )

    # 5. 超过 2 MB 被拒绝
    oversized = requests.post(
        f"{base_url}/upload",
        files={"file": ("big.txt", b"a" * (MAX_UPLOAD_BYTES + 1), "text/plain")},
        timeout=60,
    )
    check(
        "拒绝超过 2MB 的文件",
        oversized.status_code == 400 and oversized.json().get("message") == "文件过大，当前最大支持 2 MB",
        f"status={oversized.status_code} body={oversized.text[:80]}",
    )
    check("超大文件未落盘", not (KNOWLEDGE_DIR / "big.txt").exists())

    # 刚好 2 MB 应当通过（边界值）
    boundary = requests.post(
        f"{base_url}/upload",
        files={"file": ("_test_upload_boundary.txt", b"a" * MAX_UPLOAD_BYTES, "text/plain")},
        timeout=60,
    )
    CREATED_FILES.append("_test_upload_boundary.txt")
    check("2MB 边界值可上传", boundary.status_code == 200, f"status={boundary.status_code}")
    check("边界文件切成多个 chunk", boundary.json().get("chunk_count", 0) > 1, f"chunk_count={boundary.json().get('chunk_count')}")

    # 立刻删掉这个 2MB 文件：它会有几千个 chunk，会把后面检索用的 embedding 批量请求拖到超时
    (KNOWLEDGE_DIR / "_test_upload_boundary.txt").unlink(missing_ok=True)
    check("边界文件已移除", not (KNOWLEDGE_DIR / "_test_upload_boundary.txt").exists())

    # 6. 空文件名被拒绝
    empty_name = requests.post(
        f"{base_url}/upload",
        files={"file": ("", b"some content", "text/plain")},
        timeout=30,
    )
    check(
        "拒绝空文件名",
        empty_name.status_code == 400,
        f"status={empty_name.status_code} body={empty_name.text[:80]}",
    )

    # 7. 路径穿越：只保留文件名，不能写到 knowledge 之外
    traversal = requests.post(
        f"{base_url}/upload",
        files={"file": ("../../evil_test_upload.txt", b"traversal attempt", "text/plain")},
        timeout=30,
    )
    check("路径穿越请求被安全处理", traversal.status_code == 200, f"status={traversal.status_code}")
    check(
        "只保留文件名",
        traversal.json().get("source") == "evil_test_upload.txt",
        f"source={traversal.json().get('source')}",
    )
    check("文件写在 knowledge/ 内", (KNOWLEDGE_DIR / "evil_test_upload.txt").exists())
    check(
        "没有写到知识库外部",
        not (PROJECT_DIR / "evil_test_upload.txt").exists()
        and not (PROJECT_DIR.parent / "evil_test_upload.txt").exists(),
    )

    windows_traversal = requests.post(
        f"{base_url}/upload",
        files={"file": ("..\\..\\evil_win_test_upload.txt", b"traversal attempt", "text/plain")},
        timeout=30,
    )
    check(
        "Windows 反斜杠路径同样安全",
        windows_traversal.status_code == 200
        and windows_traversal.json().get("source") == "evil_win_test_upload.txt",
        f"status={windows_traversal.status_code} source={windows_traversal.json().get('source')}",
    )
    check(
        "反斜杠未逃出知识库",
        (KNOWLEDGE_DIR / "evil_win_test_upload.txt").exists()
        and not (PROJECT_DIR / "evil_win_test_upload.txt").exists()
        and not (PROJECT_DIR.parent / "evil_win_test_upload.txt").exists(),
    )

    # 8. 上传后 RAG 立即可检索（复用现有 rag_service，不经过上传接口）
    import rag_service

    retrieved = rag_service.retrieve_chunks("条件概率怎么算")
    check("上传后立即可被检索", bool(retrieved), f"n={len(retrieved)}")
    check(
        "检索命中刚上传的文件",
        bool(retrieved) and retrieved[0]["source"] == UPLOAD_NAME,
        f"top={retrieved[0]['source'] if retrieved else None} score={retrieved[0]['score'] if retrieved else None}",
    )

    # 10. 现有 /ask 未受影响
    ask = requests.get(f"{base_url}/ask", params={"question": "条件概率怎么算"}, timeout=180)
    check("/ask 返回 200", ask.status_code == 200, f"status={ask.status_code}")
    ask_payload = ask.json()
    check("返回结构仍为 answer/sources/error", set(ask_payload) == {"answer", "sources", "error"}, f"keys={sorted(ask_payload)}")
    check("/ask error 为 null", ask_payload["error"] is None, f"error={ask_payload['error']}")
    check("/ask sources 不为空", bool(ask_payload["sources"]), f"n={len(ask_payload['sources'])}")
    check(
        "/ask sources 指向新上传的文件",
        bool(ask_payload["sources"]) and ask_payload["sources"][0]["source"] == UPLOAD_NAME,
        f"top={ask_payload['sources'][0]['source'] if ask_payload['sources'] else None}",
    )
    check("/ask answer 非空", bool(ask_payload["answer"]), f"len={len(ask_payload['answer'] or '')}")

    # 其它既有接口
    check("GET / 正常", requests.get(f"{base_url}/", timeout=10).json() == {"message": "AI Study Assistant"})
    feedback = requests.get(f"{base_url}/feedback", timeout=10)
    check("GET /feedback 正常", feedback.status_code == 200 and "feedback" in feedback.json())
    check(
        "GET /next-plan 未受影响",
        requests.get(f"{base_url}/next-plan", timeout=180).status_code == 200,
    )
    check(
        "GET /analyze 未受影响",
        requests.get(f"{base_url}/analyze", timeout=180).status_code == 200,
    )
finally:
    cleanup_files()

    if process is not None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        print("测试服务已停止，退出码:", process.returncode)

    if log_handle is not None:
        log_handle.close()

    for _ in range(20):
        if not SERVER_LOG.exists():
            break
        try:
            SERVER_LOG.unlink()
        except PermissionError:
            # 子进程可能还没完全释放日志句柄
            time.sleep(0.3)

check("测试文件已清理", all(not (KNOWLEDGE_DIR / name).exists() for name in CREATED_FILES))

if SERVER_LOG.exists():
    print(f"WARN: 服务日志未能删除，可手动删除 {SERVER_LOG.name}")
else:
    check("服务日志已清理", True)

print("\n全部用例通过")
