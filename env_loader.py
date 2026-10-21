"""启动时自动读取根目录 .env（零第三方依赖，语义与 python-dotenv 一致）。

规则：
- 只解析 KEY=VALUE 行：支持 # 注释、空行、首尾空白、单/双引号包裹、export 前缀
- **已存在的环境变量不覆盖**（真实环境变量 / 测试夹具优先于 .env）
- 幂等：重复调用只加载一次
- 解析失败的行跳过（不让应用启动失败），.env 缺失时静默跳过
"""

import os
import threading
from pathlib import Path

_PROJECT_DIR = Path(__file__).resolve().parent
_LOCK = threading.Lock()
_LOADED = False


def _parse_line(line):
    line = line.strip()

    if not line or line.startswith("#"):
        return None

    if line.startswith("export "):
        line = line[len("export "):].strip()

    if "=" not in line:
        return None

    key, _, value = line.partition("=")
    key = key.strip()
    value = value.strip()

    if not key:
        return None

    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]

    return key, value


def load_env(path=None):
    """加载 .env 到 os.environ（已存在的变量不覆盖），返回加载的变量名集合。"""

    global _LOADED

    with _LOCK:
        if _LOADED:
            return set()

        _LOADED = True

    env_path = Path(path) if path else (_PROJECT_DIR / ".env")

    if not env_path.exists():
        return set()

    loaded = set()

    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            parsed = _parse_line(line)

            if parsed is None:
                continue

            key, value = parsed

            if key not in os.environ:
                os.environ[key] = value
                loaded.add(key)
    except OSError as error:
        # .env 读取失败不阻断启动（可能无权限/编码异常），真实配置仍可来自进程环境
        print(f".env 读取失败（继续使用进程环境变量）：{type(error).__name__}")

    return loaded


def reset():
    """测试用：允许再次加载。"""

    global _LOADED

    _LOADED = False
