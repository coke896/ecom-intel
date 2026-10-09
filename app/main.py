
"""FastAPI 应用入口。

启动：uvicorn app.main:app --reload --port 8001，再打开 http://127.0.0.1:8001

三条硬约束（不是风格问题，破了就出事）：
  1. 静态目录必须【最后】挂载，否则它会吃掉之后注册的路由——请求被当成文件名
     查找，全部 404。
  2. 绝不在本进程跑 Playwright：Windows 上 uvicorn `--reload` 落在
     SelectorEventLoop，不支持 Playwright 启动子进程——所以采集是独立进程
     （`python -m scraper.worker`），这也是"采集独立于 Web"的硬理由。
  3. 启动时建表：`data/` 在 .gitignore 里，不建表前端第一个请求就 500，而报错
     出现在查询里，排查要绕一圈。
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

# ★ 不能删也不能挪进函数：表定义靠"被导入"注册到 Base.metadata，没人导入它，
#   create_all() 就是空操作（建表逻辑在 app/db/base.py，采集 worker 也要用）。
import app.db.models  # noqa: F401

logger = logging.getLogger(__name__)


def warm_up() -> None:
    """预热分词器。

    ★ jieba 首次分词要加载词典（约 0.5 秒），不预热就落在第一个访问词云的用户身上；
      词典缺失只是词云难看一点，所以吞掉异常只记日志，不阻塞启动。
    """
    try:
        from app.keywords import ensure_jieba

        ensure_jieba()
    except Exception:  # noqa: BLE001 —— 预热是尽力而为，任何失败都不该阻塞启动
        logger.warning("jieba 预热失败，词云接口会在首次请求时再尝试初始化", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动/关闭钩子。

    ★ 测试环境跳过建表（测试用内存库）：别在跑测试时顺手改了开发库 data/ecom.db。
    """
    if not settings.testing:
        init_database()
        warm_up()
    yield


app = FastAPI(
    title="电商竞品情报分析平台",
    description=(
        "采集 → 清洗 → 存储 → 可视化 → AI 分析 → 导出/告警。\n\n"
        # 这段是 /docs 顶部的对外承诺，由 tests/test_api_smoke.py 从路由表核对——
        # 例外必须点名写出来，否则测试和文档会一起漂移。
        "统计接口（`/api/stats/*`）除 `/api/stats/sources` 外都支持 "
        "`?source=all|mock|smzdm` 数据源过滤：`mock` 是生成的演示数据，"
        "`smzdm` 是真实采集数据，两者在同一个库里但**永不混算**。\n\n"
        "（`/api/stats/sources` 是那个例外：它返回的就是数据源清单本身，"
        "再按 source 过滤它没有意义。这句话由测试从接口定义里核对。）"
    ),
    version="0.2.0",
    lifespan=lifespan,
)


# ---- CORS -------------------------------------------------------------------
# ★ 不用 allow_origins=["*"]：前端本由同服务托管（同源），配它只是为了允许
#   Live Server 单独伺服 frontend/；限定 localhost 是因为通配符一旦被复制进
#   真实部署，就等于放行任意网站读取本服务的响应。

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
    # ★ 必须 False：浏览器禁止它和通配来源组合；本项目也用不到 Cookie/认证。
    allow_credentials=False,
)


# ---- 路由。★ 必须在静态挂载之前注册。----------------------------------------
app.include_router(stats.router)
app.include_router(products.router)
app.include_router(analysis.router)
app.include_router(tasks.router)
# 告警分两半：接口（本文件）负责配置查询，检查器（app/alerts_engine.py）由 worker
# 每 5 分钟叫一次；这里的 POST /check 只是手动叫一次同一函数，不是第二份实现。
app.include_router(alerts.router)
# 图片代理不接受 ?source=：取图的是浏览器 <img>，且是取字节不是数据查询
# （理由见 app/api/images.py 顶部）。
app.include_router(images.router)


# ★ AI key 判定只留一处（app/analysis/glm_client.key_configured）：同一条规则写
#   两遍会分叉——health 说"AI 可用"、分析接口抛"未配置"，前端就是按钮亮着、一点
#   就报错（见 docs/可行性评估.md 15.1）。


@app.get("/api/health", tags=["meta"])
def health() -> dict[str, object]:
    """健康检查。

    ★ 不查数据库：它回答的是"进程还活着吗"；掺进查询后，库一慢它先超时，编排
      系统就会把**还活着**的服务杀掉重启。
    """
    return {
        "status": "ok",
        "app_port": settings.app_port,
        "database": "sqlite",
        # 前端据此显示"AI 可用/未配置"，而不是点下去才报错；判定问分析模块自己。
        "llm_configured": key_configured(),
    }


# ---- 静态托管。★ 只能放在文件最后。------------------------------------------

_FRONTEND_DIR = settings.project_root / "frontend"

if not _FRONTEND_DIR.is_dir():
    # 明确报出期望的路径：StaticFiles 自己的报错不会说它是相对哪个目录找的。
    raise RuntimeError(
        f"前端目录不存在：{_FRONTEND_DIR}\n"
        "（这个路径是从 app/main.py 往上两级推出来的，"
        "如果你的项目结构不同，请检查 app/config.py 里的 PROJECT_ROOT）"
    )

# html=True → 访问 / 时自动返回 index.html，不需要额外写一个路由。
app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
