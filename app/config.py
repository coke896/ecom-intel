"""全局配置。

设计取向：**每一项都有可用的默认值**（API key 除外）。
这样 `git clone` 下来不写任何 .env 也能把服务跑起来、把测试跑绿——
对求职作品集来说，"clone 下来就能跑" 和 "要先配五个环境变量" 之间的差别，
就是面试官愿不愿意真的点开看。

配置优先级：真实环境变量 > .env 文件 > 这里的默认值。

沿用了 aiwenda 的做法：不用 pydantic-settings 之类的重型方案。
本项目配置项少且都是标量，一个带类属性的类足够，
还省掉了 pydantic 版本升级带来的意外。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录。config.py 在 app/ 下，往上两级就是根（app/config.py -> app/ -> 根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 把 .env 读进 os.environ。
# ★ 必须在下面任何 os.getenv 之前调用，否则读到的全是默认值。
#   .env 不存在时 load_dotenv 不报错、静默跳过——这正是我们要的：
#   没有 .env 也能跑（除了需要 API key 的功能）。
load_dotenv(PROJECT_ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    """把 "1"/"true"/"yes"/"on" 这类字符串解析成布尔值。

    为什么要手写：环境变量本质都是字符串，
    os.getenv("X", True) 在 X="false" 时会返回字符串 "false"（真值！），
    这是个非常隐蔽的坑。
    """
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


class Settings:
    # ---- 路径 -----------------------------------------------------------------
    project_root = PROJECT_ROOT
    data_dir = Path(os.getenv("DATA_DIR", str(PROJECT_ROOT / "data")))
    images_dir = Path(os.getenv("IMAGES_DIR", str(PROJECT_ROOT / "data" / "images")))
    # 停用词表和 jieba 自定义词典放文件而不是硬编码在 py 里。
    # 理由：面试时能直接展示"这是可扩展的"——加词不用改代码。
    stopwords_file = Path(os.getenv("STOPWORDS_FILE", str(PROJECT_ROOT / "data" / "stopwords.txt")))
    userdict_file = Path(os.getenv("USERDICT_FILE", str(PROJECT_ROOT / "data" / "userdict.txt")))

    # ---- 数据库 ---------------------------------------------------------------
    # ★ Windows 路径必须转成正斜杠（as_posix），否则 SQLAlchemy 拼 URL 时会
    #   出现 sqlite:///E:\VScodde\... 这种反斜杠路径，SQLite 打不开。
    database_url = os.getenv(
        "DATABASE_URL", f"sqlite:///{(PROJECT_ROOT / 'data' / 'ecom.db').as_posix()}"
    )

    # ---- 服务 -----------------------------------------------------------------
    # ★ 8001 而不是 8000：8000 被 aiwenda 占着。
    #   两个项目同时跑时撞端口的报错是 "Address already in use"，
    #   它不会告诉你"是谁占了"——所以端口从一开始就错开。
    app_port = int(os.getenv("APP_PORT", "8001"))
    app_host = os.getenv("APP_HOST", "127.0.0.1")

    # ---- GLM（走 OpenAI 兼容接口）---------------------------------------------
    # 用 openai SDK 指向智谱的兼容端点，而不是智谱官方 SDK。
    # 好处：同一套代码改三个环境变量就能切 Ollama / SiliconFlow / 任何兼容服务。
    glm_base_url = os.getenv("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    glm_api_key = os.getenv("GLM_API_KEY", "")
    glm_model = os.getenv("GLM_MODEL", "glm-4-flash")
    # ★ 智谱的 temperature 取值范围是开区间 (0, 1)，不是 OpenAI 的 [0, 2]。
    #   传 1.5 会直接报错——这个坑在 aiwenda 上踩过。
    glm_temperature = float(os.getenv("GLM_TEMPERATURE", "0.3"))
    # 分析报告要输出完整 JSON（含多个分析维度），4096 才够。
    # aiwenda 那边是 1024，因为那里只要一段 Markdown 回答。
    glm_max_tokens = int(os.getenv("GLM_MAX_TOKENS", "4096"))
    glm_timeout = float(os.getenv("GLM_TIMEOUT", "120"))

    # ---- 采集 -----------------------------------------------------------------
    # 请求间隔（秒）。写在配置里而不是硬编码，是为了让"限速"成为
    # 一个显式的、可审计的约束，而不是散落在代码里的 sleep。
    crawl_delay_min = float(os.getenv("CRAWL_DELAY_MIN", "2.0"))
    crawl_delay_max = float(os.getenv("CRAWL_DELAY_MAX", "3.0"))
    crawl_timeout = float(os.getenv("CRAWL_TIMEOUT", "30"))

    # ---- 测试 -----------------------------------------------------------------
    # 测试环境下 engine 指向内存库，避免污染开发用的 data/ecom.db。
    testing = _bool("TESTING", False)

    @classmethod
    def ensure_dirs(cls) -> None:
        """确保运行时目录存在。

        单独一个方法而不是在模块顶层 mkdir：
        导入模块时产生副作用（建目录）会让测试难以隔离。
        由应用启动时显式调用。
        """
        cls.data_dir.mkdir(parents=True, exist_ok=True)
        cls.images_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
