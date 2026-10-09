"""全局配置。

设计取向：**每一项都有可用的默认值**（API key 除外）——git clone 下来不写 .env
也能跑起来、把测试跑绿。优先级：真实环境变量 > .env 文件 > 这里的默认值。
不用 pydantic-settings 之类的重型方案：配置项少且都是标量，一个类就够。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录：app/config.py 往上两级。
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 把 .env 读进 os.environ。★ 必须在任何 os.getenv 之前调用，否则读到的全是默认值；
# .env 不存在时静默跳过——没有 .env 也能跑。
load_dotenv(PROJECT_ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    """把 "1"/"true"/"yes"/"on" 这类字符串解析成布尔值。

    手写的原因：os.getenv("X", True) 在 X="false" 时返回字符串 "false"（真值！）。
    """
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# ★ `DATA_DIR` 用模块级常量算出来：它不止是一个配置项，还是下面所有路径的根
#   （images / stopwords / userdict / 默认库位置都从它派生），写进类里就得依赖
#   类体执行顺序，改起来容易漏。
_DATA_DIR = Path(os.getenv("DATA_DIR", str(PROJECT_ROOT / "data")))


def _resolve_database_url(raw: str) -> str:
    """把 sqlite 的**相对**路径解析成基于项目根目录的绝对路径，其余原样返回。

    ★ 相对路径是相对于**进程工作目录**的：换个目录启动就指向另一个库，最坏时
      SQLite 在那边**静默建出一个全空的新库**（表齐全、0 条数据），服务照常
      起来，人只会以为"还没采集"。
    """
    prefix = "sqlite:///"

    # `sqlite://`（内存库）和 :memory: 都不是文件路径
    if not raw.startswith(prefix) or ":memory:" in raw:
        return raw

    path = raw[len(prefix):]
    if not path:
        return raw

    # Windows 盘符（E:/x）不以 / 开头但它是绝对路径，必须单独判，
    # 否则会被拼到项目根下面去
    looks_absolute = path.startswith("/") or (
        len(path) > 1 and path[1] == ":" and path[0].isalpha()
    )
    if looks_absolute:
        return raw

    resolved = (PROJECT_ROOT / path).resolve().as_posix()
    # ★ 改写了用户给的配置值，就得说出来——静默改写就是"说的和做的不一样"。
    logging.getLogger(__name__).warning(
        "DATABASE_URL 里的 sqlite 路径是相对的（%s），已按项目根目录解析为 %s——"
        "相对路径本来是相对于**启动目录**的，换个目录启动就会连到另一个库。",
        raw,
        resolved,
    )
    return f"sqlite:///{resolved}"


class Settings:
    # ---- 路径 -----------------------------------------------------------------
    project_root = PROJECT_ROOT

    data_dir = _DATA_DIR
    """所有运行时数据的根。**改了它，下面几项会一起跟着改**（默认值层面）。

    ★ 每一项的 env 覆盖仍然独立：设了 `IMAGES_DIR` 就以它为准，
      不会因为 `DATA_DIR` 而失效。这样"只想搬图片"和"整个搬到别处"
      两种需求都成立。
    """

    images_dir = Path(os.getenv("IMAGES_DIR", str(_DATA_DIR / "images")))

    # 停用词表和 jieba 自定义词典放文件而不是硬编码在 py 里：加词不用改代码。
    stopwords_file = Path(os.getenv("STOPWORDS_FILE", str(_DATA_DIR / "stopwords.txt")))
    userdict_file = Path(os.getenv("USERDICT_FILE", str(_DATA_DIR / "userdict.txt")))

    # ---- 数据库 ---------------------------------------------------------------
    # ★ Windows 路径必须转成正斜杠（as_posix），否则 SQLAlchemy 拼 URL 时会
    #   出现 sqlite:///E:\VScodde\... 这种反斜杠路径，SQLite 打不开。
    database_url = _resolve_database_url(
        os.getenv("DATABASE_URL", f"sqlite:///{(_DATA_DIR / 'ecom.db').as_posix()}")
    )

    # ---- 服务 -----------------------------------------------------------------
    # ★ 8001 而不是 8000：8000 被本机另一个服务占着——撞端口只会报"Address
    #   already in use"、不告诉你谁占了，所以端口从一开始就错开。
    app_port = int(os.getenv("APP_PORT", "8001"))
    app_host = os.getenv("APP_HOST", "127.0.0.1")

    # ---- GLM（走 OpenAI 兼容接口）---------------------------------------------
    # 用 openai SDK 指向智谱的兼容端点：同一套代码改三个环境变量就能切
    # Ollama / SiliconFlow / 任何兼容服务。
    glm_base_url = os.getenv("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    glm_api_key = os.getenv("GLM_API_KEY", "")
    glm_model = os.getenv("GLM_MODEL", "glm-4-flash")
    # ★ 智谱的 temperature 是开区间 (0, 1)，不是 OpenAI 的 [0, 2]——传 1.5 直接报错。
    glm_temperature = float(os.getenv("GLM_TEMPERATURE", "0.3"))
    # 报告要输出完整 JSON（含多个分析维度），4096 才够。
    glm_max_tokens = int(os.getenv("GLM_MAX_TOKENS", "4096"))
    glm_timeout = float(os.getenv("GLM_TIMEOUT", "120"))

    # ---- 采集 -----------------------------------------------------------------
    # 请求间隔（秒）。写进配置而不是散在代码里：限速是显式、可审计的约束。
    crawl_delay_min = float(os.getenv("CRAWL_DELAY_MIN", "2.0"))
    crawl_delay_max = float(os.getenv("CRAWL_DELAY_MAX", "3.0"))
    crawl_timeout = float(os.getenv("CRAWL_TIMEOUT", "30"))

    # ---- 任务生命周期 ---------------------------------------------------------
    # ★ 陈旧任务阈值：running 超过这么多分钟没更新就算"可能已中断"。**一个值定义
    #   一次，两个进程都读它**（worker 决定要不要打回 pending 重跑，接口决定界面
    #   显不显示"可能已中断"）——两份拷贝一旦不一致，现象很难查。
    stale_task_minutes = int(os.getenv("STALE_TASK_MINUTES", "10"))

    # ---- 测试 -----------------------------------------------------------------
    # 测试环境下 engine 指向内存库，避免污染开发用的 data/ecom.db。
    testing = _bool("TESTING", False)

    @classmethod
    def ensure_dirs(cls) -> None:
        """确保运行时目录存在（应用启动时显式调用）。

        不写在模块顶层：导入时产生副作用（建目录）会让测试难以隔离。
        """
        cls.data_dir.mkdir(parents=True, exist_ok=True)
        cls.images_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
