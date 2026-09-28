
"""FastAPI 应用入口。

启动：
    uvicorn app.main:app --reload --port 8001
    然后打开 http://127.0.0.1:8001

================================================================================
本文件的三条硬约束（都不是风格问题，破了就出事）
================================================================================

1. **静态目录必须【最后】挂载。**
   `app.mount("/", StaticFiles(...))` 会吃掉它之后注册的所有路径。
   顺序反了的话，`/api/stats/overview` 会走进静态文件处理器去找一个
   名叫 "api/stats/overview" 的文件，然后返回 404——
   而报错信息完全不会提示"是你 mount 的顺序错了"，
   看起来就像是路由没写对。

2. **绝不在这个进程里跑 Playwright。**
   Windows 上 uvicorn 的 `--reload` 用子进程模式，worker 落在
   `SelectorEventLoop`，而它不支持 `asyncio.create_subprocess_exec`——
   Playwright 启动 Chromium 正是靠这个，会直接抛 `NotImplementedError`。
   本项目的采集层是**独立进程**（`python -m scraper.worker`），
   天然避开这个坑。这反过来也是"采集层独立于 Web 层"最硬的理由之一。
   （所以这里可以放心用 `--reload`；只有本机调试 Playwright 脚本时不能加。）

3. **启动时创建表。**
   `data/` 在 .gitignore 里，所以 `git clone` 下来是没有数据库文件的。
   不建表的话，前端第一个请求就会 500（no such table: products），
   而报错发生在查询里、不在启动时——排查起来要绕一圈。
   建表放在启动时，是"clone 下来就能跑"这个承诺的一部分。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.analysis.glm_client import key_configured
from app.api import alerts, analysis, images, products, stats, tasks
from app.config import settings
from app.db.base import init_database

# ★ 这一行不能删，也不能挪到函数里。
#   表的定义写在 app/db/models.py，它是**通过被导入**把表注册到
#   Base.metadata 上的。没人导入它，create_all() 就是个空操作。
#   （同一个陷阱在 tests/conftest.py 里也记了一笔——
#     那次的表现是"单独跑某个测试文件才失败"。）
#
# ★ 建表逻辑本身已经搬到 app/db/base.py 的 init_database() 了，
#   因为采集 worker 也要建表，而它不该为了这个去 import FastAPI 应用入口。
#   这一行留着是因为 Web 层自己也查询这些表，导入即注册的约束对它同样成立。
import app.db.models  # noqa: F401

logger = logging.getLogger(__name__)


def warm_up() -> None:
    """预热分词器。

    ★ 为什么要预热：jieba 第一次分词要加载词典（约 0.5 秒）。
      不预热的话，这个开销会落在**第一个访问词云的用户**身上，
      表现为"服务刚起来时首页特别慢"，之后又正常了——
      很容易被误判成"数据库连接池没热"之类的别的问题。

    ★ 预热失败不能影响服务启动：词典文件缺失只是词云难看一点，
      不该让整个服务起不来。所以这里吞掉异常只记日志。
    """
    try:
        from app.keywords import ensure_jieba

        ensure_jieba()
    except Exception:  # noqa: BLE001 —— 预热是尽力而为，任何失败都不该阻塞启动
        logger.warning("jieba 预热失败，词云接口会在首次请求时再尝试初始化", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动/关闭钩子。

    ★ 测试环境下跳过建表：
      测试用的是内存库（见 tests/conftest.py），完全没有必要碰
      data/ecom.db。而且"跑一次测试顺手改了开发库"是那种
      当时看不出、事后很难解释的副作用。
    """
    if not settings.testing:
        init_database()
        warm_up()
    yield


app = FastAPI(
    title="电商竞品情报分析平台",
    description=(
        "采集 → 清洗 → 存储 → 可视化 → AI 分析 → 导出/告警。\n\n"
        # ★ 这段话是 /docs 页面顶部的说明，也就是**对外承诺**。
        #   它一度写的是"所有统计接口都支持 ?source="，而
        #   `/api/stats/sources` 并不接受这个参数——它是"数据源清单"，
        #   本身就在回答"有哪些 source"，再按 source 过滤它没有意义。
        #   于是 /docs 上的一条承诺，被自己列出的端点之一否掉。
        #   现在改成把例外点名写出来，并且由
        #   tests/test_api_smoke.py 从**路由表**核对这句话
        #   （理由见那条测试：手写清单会漂移，路由表不会）。
        "统计接口（`/api/stats/*`）除 `/api/stats/sources` 外都支持 "
        "`?source=all|mock|smzdm` 数据源过滤：`mock` 是生成的演示数据，"
        "`smzdm` 是真实采集数据，两者在同一个库里但**永不混算**。\n\n"
        "（`/api/stats/sources` 是那个例外：它返回的就是数据源清单本身，"
        "再按 source 过滤它没有意义。这句话由测试从接口定义里核对。）"
    ),
    version="0.2.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# CORS
#
# ★ 为什么不用 `allow_origins=["*"]`：
#   本项目的设计是"前端由同一个服务托管"（下面的静态挂载），
#   也就是同源请求，**根本不需要 CORS**。
#   这里配 CORS 只是为了允许另一种开发方式：用 VSCode 的 Live Server
#   （或任何静态服务器，端口是随机的）单独伺服 frontend/。
#
#   用正则限定 localhost 而不是通配符，是因为通配符一旦被复制到
#   真实部署里，就等于允许任意网站读取本服务的响应。
#   这类配置的默认值应当"在开发机上够用，在公网上不够用"。
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
    # ★ 必须是 False。浏览器明确禁止 `allow_credentials=True` 与通配来源
    #   组合使用，而更重要的是：本项目用不到 Cookie/认证，
    #   开着它只会平白扩大攻击面。
    allow_credentials=False,
)


# ---------------------------------------------------------------------------
# 路由。★ 必须在静态挂载之前注册。
# ---------------------------------------------------------------------------
app.include_router(stats.router)
app.include_router(products.router)
app.include_router(analysis.router)
app.include_router(tasks.router)
# ★ 告警有"接口"和"检查器"两半，分居两个进程：
#   接口（本文件）负责配置和查询，检查器（scraper/check_alerts.py）
#   由采集 worker 每 5 分钟叫一次。接口里那个 POST /api/alerts/check
#   只是**手动**叫一次同一个函数，不是第二份实现。
app.include_router(alerts.router)
# ★ 图片代理**不接受 ?source=**——和 /api/stats/sources 一样是例外之一。
#   取图的是浏览器的 <img> 加载器，不是我们的 JS，而且它是一次取字节的动作
#   而不是一次数据查询。理由写在 app/api/images.py 顶部。
#   ★ 这里原本写的是"本项目**唯一一个**不接受 ?source= 的接口"。
#     核实后发现不成立：光是不接受这个参数的端点就有 11 个
#     （单条资源、任务、健康检查、告警配置……它们都不按数据源过滤）。
#     改动时我没有再手写一个"一共两个"——那只是把同一个错误改小一点。
#     现在只陈述"本接口不接受"，需要完整清单的话由
#     tests/test_api_smoke.py 从接口定义里数出来。
#   ★ "唯一""全都""永不"这类词是最该被核对的词：它们读起来最强，
#     也最容易被写下来之后没人再验一次。
app.include_router(images.router)


# ★ 这里**曾经**有一份 `_llm_key_configured()` 的实现，和
#   `app/analysis/glm_client.key_configured()` 是同一套规则的两份代码。
#   两份已经**实测分叉**了：对 `replace_me_please` / `example-key-123` /
#   `xxxxxx` / `TODO_KEY` 这四个值，这里判"已配置"、glm_client 判"未配置"。
#
#   后果不是"少拦一个占位符"，而是**两个接口互相矛盾**：
#     · `/api/health` 说"AI 分析可用" → 前端于是把「生成分析报告」按钮点亮
#     · 用户点下去 → 分析接口抛"GLM_API_KEY 未配置"
#   前端按钮的可用状态是照着健康检查来的，所以用户看到的是
#   "按钮亮着，一点就报错"——最难排查的那类不一致：
#   两个地方各自都是自洽的，只有放在一起才矛盾。
#
#   现在只留一处规则，health 直接问分析模块。同一类问题在本项目里
#   已经收拢过几次（`apply_source`、`app/aggregate.py`、`app/keywords.py`），
#   判据都一样：**同一条规则被写第二遍时，它就有了分叉的可能。**


@app.get("/api/health", tags=["meta"])
def health() -> dict[str, object]:
    """健康检查。

    ★ 不查数据库。这个接口的用途是回答"进程还活着吗"，
      而不是"数据库通不通"。掺进数据库查询之后，
      数据库慢的时候健康检查会超时，于是编排系统把**还活着**的
      服务杀掉重启——这是"健康检查把自己检查死"的经典事故。
      数据库连通性如果要检查，应该是另一个独立接口。
    """
    return {
        "status": "ok",
        "app_port": settings.app_port,
        "database": "sqlite",
        # 前端据此显示"AI 分析可用/未配置"，而不是点下去才报错。
        # ★ 这里必须调用分析模块自己的判定，理由见上面那段注释——
        #   两份实现分叉之后，"按钮亮着但一点就报错"就是这么来的。
        "llm_configured": key_configured(),
    }


# ---------------------------------------------------------------------------
# 静态托管。★ 只能放在文件最后。
# ---------------------------------------------------------------------------

_FRONTEND_DIR = settings.project_root / "frontend"

if not _FRONTEND_DIR.is_dir():
    # 明确报出期望的路径，而不是让 StaticFiles 抛一句
    # "Directory 'frontend' does not exist"——那个信息没告诉你
    # 它是相对哪个目录找的。
    raise RuntimeError(
        f"前端目录不存在：{_FRONTEND_DIR}\n"
        "（这个路径是从 app/main.py 往上两级推出来的，"
        "如果你的项目结构不同，请检查 app/config.py 里的 PROJECT_ROOT）"
    )

# html=True → 访问 / 时自动返回 index.html，不需要额外写一个路由。
# 这就是"零构建、纯静态前端"的全部配置。
app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
