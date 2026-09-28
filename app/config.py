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

import logging
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


# ★ `DATA_DIR` 用**模块级常量**算出来，而不是直接写成类属性。
#
#   因为它不止自己是一个配置项，还是下面所有路径的**根**：
#   images / stopwords / userdict / 默认数据库位置都从它派生。
#   写进类里的话，下面几行就得引用"同一个类里上面那行"——
#   能跑，但依赖类体执行顺序，而且改起来很容易漏掉一处。
#
#   ★ 这里修的是一个实测出来的毛病：`DATA_DIR` 一度**只**决定
#     `settings.data_dir` 自己，而真正有数据的四样东西
#     （库、图片、停用词表、自定义词典）全都还指着
#     `PROJECT_ROOT/data`。于是设了 `DATA_DIR=/tmp/x` 的效果是——
#     /tmp/x 被创建出来了（`ensure_dirs` 建的），**但它是空的**，
#     数据一个都没搬。看到目录出现，人就会以为搬成功了。
#     这比"这个配置项不存在"更坏：不存在的配置项会报错，
#     而这个只会让人相信一件假事。
_DATA_DIR = Path(os.getenv("DATA_DIR", str(PROJECT_ROOT / "data")))


def _resolve_database_url(raw: str) -> str:
    """把 sqlite 的**相对**路径解析成基于项目根目录的绝对路径。

    ★ 为什么必须解析：`sqlite:///data/ecom.db` 这种写法里，相对路径是
      **相对于进程的工作目录**的，不是相对于项目根目录。于是同一个配置
      在不同目录下启动会指向不同的库，实测过两种结局：

        在项目根启动        → 用 data/ecom.db，正常
        在没有 data/ 的目录 → 崩，报 "unable to open database file"
        在**有** data/ 的目录 → **静默建出一个全新的空库**，
                              全套表都在，商品 0 条，不报错不告警

      第三种最坏：服务照常起来，界面上什么都没有，而人只会以为"还没采集"。
      这个项目已经写过好几次同类问题（见 docs 16.10），都是同一个形状：
      **库改了（或换了一个），读的人不知道**。

    ★ 解析到 `PROJECT_ROOT` 而不是 cwd：`DATABASE_URL=sqlite:///data/ecom.db`
      几乎总是"我要项目里的那个库"，而不是"我要 cwd 下的库"。

    ★ 只管 sqlite 的相对路径。其它情况原样返回：
      · 非 sqlite 的 URL（postgresql://... 等）有它自己的解析规则，不碰
      · `sqlite://`（内存库）和 `:memory:` 没有路径可解析
      · 已经是绝对路径的（`sqlite:////tmp/x.db`，或 `sqlite:///E:/...`）不动
    """
    prefix = "sqlite:///"

    # `sqlite://`（内存库）不带第三个斜杠，:memory: 也不该被当成文件路径
    if not raw.startswith(prefix) or ":memory:" in raw:
        return raw

    path = raw[len(prefix):]
    if not path:
        return raw

    # Windows 盘符（E:/x）不以 / 开头，但它是绝对路径——必须单独判，
    # 否则会被当成相对路径拼到项目根下面去（E:\VScodde\ecom-intel\E:\x）
    looks_absolute = path.startswith("/") or (
        len(path) > 1 and path[1] == ":" and path[0].isalpha()
    )
    if looks_absolute:
        return raw

    resolved = (PROJECT_ROOT / path).resolve().as_posix()
    # ★ 改写了用户给的配置值，就得说出来。静默改写本身也是"配置说的和
    #   实际做的不一样"，只是方向相反而已。
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

    # 停用词表和 jieba 自定义词典放文件而不是硬编码在 py 里。
    # 理由：面试时能直接展示"这是可扩展的"——加词不用改代码。
    stopwords_file = Path(os.getenv("STOPWORDS_FILE", str(_DATA_DIR / "stopwords.txt")))
    userdict_file = Path(os.getenv("USERDICT_FILE", str(_DATA_DIR / "userdict.txt")))

    # ---- 数据库 ---------------------------------------------------------------
    # ★ Windows 路径必须转成正斜杠（as_posix），否则 SQLAlchemy 拼 URL 时会
    #   出现 sqlite:///E:\VScodde\... 这种反斜杠路径，SQLite 打不开。
    database_url = _resolve_database_url(
        os.getenv("DATABASE_URL", f"sqlite:///{(_DATA_DIR / 'ecom.db').as_posix()}")
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
