"""图片代理：把第三方图床的图取回来，由我们自己的域名发给浏览器。

为什么代理：防盗链（图床普遍校验 Referer）；图片地址是采集来的**外部输入**，
前端直接塞进 <img src> 等于"数据里能塞什么地址，浏览器就请求什么地址"；以及
**不把访客的 IP 送给第三方**。

不做白名单的后果不是"开放代理"，是标准 SSRF。四道闸门，顺序有意义：
① 协议（只允许 http/https，少了它 `file://` 能读本机文件）② 主机名白名单
③ IP 字面量一律拒绝（含 `2130706433`/`0x7f000001`/`0177.0.0.1`/`127.1` 变体）
④ 解析出的地址必须是公网地址——④ 必须存在：白名单挡的是"名字"，实际连的是
"地址"，攻击者可以让**真的在白名单里**的域名解析到 127.0.0.1。**残余风险如实
记**：解析一次、httpx 再解析一次，两次之间的 TOCTOU 窗口（DNS rebinding）没
关掉（见 docs/可行性评估.md 18.3）。

★ Content-Type 必须是 image/*：代理**在同源下**返回第三方内容，不检查类型的话，
  上游的 HTML 会被浏览器当成我们自己站点的页面渲染——**同源 XSS**；SVG 单独
  拒绝（能携带 <script>）。
★ 本接口**不接受 ?source=**（取字节，不是数据查询），测试全程不联网。
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import socket
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, Query, Response

from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/proxy-image", tags=["images"])


class ImageProxyError(Exception):
    """一次被拒绝的代理请求。带 HTTP 状态码：400=URL 本身不对、403=我拒绝去取
    （不在白名单/解析到内网）、502=上游没给出可用的图——分开才能一眼看出是谁的问题。
    """

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


# ---------------------------------------------------------------------------
# 闸门参数
# ---------------------------------------------------------------------------
# 白名单按**域名后缀**匹配。★ 故意不放行任何"通用图床"：白名单一旦宽到"能放
# 用户提供的图"，它就不再是闸门了。（库里实际商品的图都在 zdmimg.com / smzdm.com。）
ALLOWED_IMAGE_HOSTS: tuple[str, ...] = ("zdmimg.com", "smzdm.com")

# 单张图上限 5MB（真实商品图在 10~200KB 量级）。它的作用是**防止被当成内存
# 放大器**，不是卡正常图片。
MAX_IMAGE_BYTES = 5 * 1024 * 1024

# 上游超时：取不到就报错，不能让一个挂住的图床把 API 的工作线程占满。
FETCH_TIMEOUT = 10.0

# 最多跟几跳重定向。★ 每一跳都会重新过一遍全部闸门——不让 httpx 自己跟，
# 否则 `白名单域名 → 302 → http://127.0.0.1:8001/` 直接绕过白名单。
MAX_REDIRECTS = 3

REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})

# ★ 单独拒掉 SVG：它能携带 <script>，而返回"图片"时没法区分"当图片用"和
#   "当文档打开"——直接不给。
BLOCKED_IMAGE_TYPES = frozenset({"image/svg+xml", "image/svg"})


# ---------------------------------------------------------------------------
# 闸门 ③：IP 字面量
# ---------------------------------------------------------------------------
def _is_ip_literal(host: str) -> bool:
    """这个主机名是不是一个 IP 地址字面量（含各种进制/简写变体）。

    ★ 两道判断覆盖的是**不同写法**：`ipaddress.ip_address` 认标准写法（含
      IPv6）；`socket.inet_aton` 认 C 风格变体——"2130706433"（十进制）、
      "0x7f000001"（十六进制）、"0177.0.0.1"（八进制）、"127.1"（少写几段）
      都指向 127.0.0.1，而 `ipaddress` 这四个**一个都不认**。
    ★ 拒绝的是**所有** IP 字面量而不只是内网 IP：白名单按域名匹配，IP 本来就
      不可能通过，提前拒绝才能保证"内网地址根本没进入解析"。
    """
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    try:
        socket.inet_aton(host)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 闸门 ④：解析出来的地址
# ---------------------------------------------------------------------------
def _resolve(host: str) -> list[str]:
    """解析主机名的**全部**地址。★ 测试会 monkeypatch 掉这个函数。

    ★ 返回列表而不是取第一个：双栈域名同时有 A 和 AAAA 记录，攻击者可以让
      **其中一条**指向内网——只看第一条的实现会被 50% 的概率绕过。
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        # 解析不了就是取不到图，不是客户端写错了地址
        raise ImageProxyError(502, f"域名解析失败：{host}") from exc
    return sorted({info[4][0] for info in infos})


def _assert_public(ip_text: str) -> None:
    """这个地址必须是公网地址。

    ★ 用 `is_global` 一个判断而不是手写私有网段清单：手写清单的失效方式是
      **静默的**——漏掉 `100.64.0.0/10`（运营商级 NAT）不会报错，只会放行。
      `is_global` 覆盖回环、内网、云元数据端点（169.254.169.254），以及 IPv6
      侧的 `::1` / `::ffff:127.0.0.1`（IPv6 写法的回环——只查 IPv4 私有段的
      实现会放它过去）。
    """
    ip = ipaddress.ip_address(ip_text)
    if not ip.is_global:
        raise ImageProxyError(403, f"该域名解析到非公网地址 {ip_text}，拒绝代理")


# ---------------------------------------------------------------------------
# 闸门 ① ②：协议与主机名
# ---------------------------------------------------------------------------
def _host_allowed(host: str) -> bool:
    """主机名是否在白名单内。

    ★ 后缀匹配必须卡在**点边界**上：`endswith("zdmimg.com")` 会让
      `evil-zdmimg.com` 通过（那种域名任何人都能注册）。
    ★ 结尾的 `.` 先去掉：`y.zdmimg.com.` 是合法 FQDN，解析结果一样但字符串
      比较不一样——"写法不同、指向相同"的绕过点。
    """
    host = host.lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in ALLOWED_IMAGE_HOSTS)


def _validate(url: str) -> str:
    """过一遍全部闸门，返回可以安全请求的 URL。

    返回的是 `urlunsplit` 重新拼出来的规范化串——刻意**不是**原样返回：我们
    真正会去请求的就是这个串，不留"我检查的和请求的不是同一个"的缝。
    """
    if not url or not url.strip():
        raise ImageProxyError(400, "缺少 url 参数")
    raw = url.strip()

    # ★ 先扫字符，再做结构化解析：凡是"同一串字符被两方理解成不同东西"的地方
    #   都是绕过点——空白（`inet_aton("8.8.8.8 ")` 实测**成功**）、控制字符，以及
    #   反斜杠（**浏览器把 `\` 当 `/`，urlparse 不当**）。
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        raise ImageProxyError(400, "url 里含空白或控制字符")
    if "\\" in raw:
        raise ImageProxyError(400, "url 里含反斜杠（浏览器会把它当成斜杠，含义有歧义）")

    try:
        parts = urlsplit(raw)
    except ValueError as exc:  # 极少数畸形串（比如端口非数字）会在这抛
        raise ImageProxyError(400, f"url 解析失败：{exc}") from exc

    # ① 协议。只认 http/https，`file://` / `gopher://` / `data:` 一律出局。
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ImageProxyError(400, f"只允许 http/https，收到 {parts.scheme!r}")

    # 用户名密码段（`https://user:pass@host/`）。★ urlsplit 的处理是正确的，但
    #   "正确"只在我们这一侧成立——有些下游按第一个 @ 切，直接拒绝最省心。
    if "@" in parts.netloc:
        raise ImageProxyError(400, "url 里不允许带用户名/密码")

    try:
        # `.port` 会在端口不是数字时抛 ValueError（比如 ":abc"）
        port = parts.port
    except ValueError as exc:
        raise ImageProxyError(400, f"端口不合法：{exc}") from exc

    host = (parts.hostname or "").lower()
    if not host:
        raise ImageProxyError(400, "url 里没有主机名")

    # ③ IP 字面量放在白名单检查之前：提前拒绝才能保证"内网地址根本没进入解析"。
    if _is_ip_literal(host):
        raise ImageProxyError(400, "不允许直接使用 IP 地址，只接受白名单域名")

    # ② 主机名白名单
    if not _host_allowed(host):
        raise ImageProxyError(
            403, f"域名不在图片白名单内：{host}（允许：{'、'.join(ALLOWED_IMAGE_HOSTS)}）"
        )

    # ④ 解析出来的每一个地址都必须是公网地址
    for ip_text in _resolve(host):
        _assert_public(ip_text)

    # 重新拼一遍。★ urlunsplit 会丢掉 fragment，那是对的——`#` 之后根本不会
    #   发给服务器，留着只会让缓存键多出一堆变体。
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, ""))


# ---------------------------------------------------------------------------
# 取字节
# ---------------------------------------------------------------------------
class _Upstream(NamedTuple):
    """从上游拿到的东西（还没做任何判断）。

    ★ 特意把"取"和"判"分开：测试可以 monkeypatch `_download` 造出任意响应，
      而**所有判断逻辑仍然是真跑一遍的**。
    """

    status: int
    content_type: str
    location: str | None
    body: bytes


def _download(url: str) -> _Upstream:
    """发一次请求，读回响应。★ 测试会 monkeypatch 掉这个函数。

    ★ 不跟随重定向（`follow_redirects=False`）：让 httpx 自己跟，302 的目标就
      **没有过闸门**——所以我们自己跟，每一跳都重过闸门。
    """
    try:
        with httpx.Client(
            headers=_HEADERS, timeout=FETCH_TIMEOUT, follow_redirects=False
        ) as client:
            with client.stream("GET", url) as resp:
                if resp.status_code in REDIRECT_CODES:
                    # 重定向响应体没有意义，不读
                    return _Upstream(
                        resp.status_code,
                        resp.headers.get("content-type", ""),
                        resp.headers.get("location"),
                        b"",
                    )

                # ★ 边读边数、超了立刻 break："限制大小"和"检查大小"的区别——
                #   写成 `body = resp.read()` 再判断长度的话，上游发 10GB 我们会
                #   先把它收进内存，然后才报"太大了"。
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > MAX_IMAGE_BYTES:
                        break
                return _Upstream(
                    resp.status_code,
                    resp.headers.get("content-type", ""),
                    None,
                    b"".join(chunks),
                )
    except httpx.HTTPError as exc:
        # 连接失败、超时、TLS 报错都归到这里。★ 只回类型名不回原始串：原始异常里
        # 可能带上完整 URL，而这段消息是要发给浏览器的。
        raise ImageProxyError(502, f"请求上游失败（{type(exc).__name__}）") from exc


# ★ User-Agent 在这里重新定义，而不是 from scraper.sources.smzdm import HEADERS：
#   那会让 **Web 层依赖采集层**，方向与本项目相反（见 app/main.py 顶部第 2 条）。
#   代价是一个字符串重复两处，收益是 web 进程不必为了发一张图去 import 整个采集模块。
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}


# ---- 磁盘缓存 -----------------------------------------------------------------
#
# ★ 它解决的问题不是"省流量"，是**离线演示**：面试现场断网/图床被墙/防盗链策略
#   变了时，抓过一次的图从本地出，演示本身就不依赖网络。
# ★ 缓存是**无上限**的，是有意的取舍：单张有 5MB 上限、数据源有限，最坏情况
#   有界；逐出逻辑真上生产该交给 CDN/nginx，而不是写在应用里。
def _cache_base(url: str) -> Path:
    """URL → 缓存文件路径。

    ★ 用 sha256 而不是把 URL 转义成文件名：转义规则要自己维护、长度不可控
      （Windows 有 260 字符路径限制）；分两级目录避免一个目录堆几千个文件。
    """
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return settings.images_dir / digest[:2] / digest


def _load_cached(url: str) -> tuple[bytes, str] | None:
    """命中就返回 (字节, content-type)，否则 None。

    ★ 缓存这条路径**不再做类型检查**——靠的是一条不变量："进缓存的图一定过了
      `image/*` 那道闸门"（`_store` 只在检查之后被调用）；不变量一破，缓存就
      成了绕过类型检查的旁路。
    """
    base = _cache_base(url)
    meta = Path(str(base) + ".ct")
    try:
        # ★ 缺任何一个都当**未命中**：这样"写了一半就崩"只会导致重新取一次，
        #   而不是返回一张没有类型（或者更糟，只有半张）的图。
        if base.is_file() and meta.is_file():
            content_type = meta.read_text(encoding="ascii").strip()
            if content_type:
                return base.read_bytes(), content_type
    except OSError:
        # 读缓存失败不是错误，只是没命中
        logger.warning("读图片缓存失败，改走网络：%s", base, exc_info=True)
    return None


def _store(url: str, body: bytes, content_type: str) -> None:
    """把取回来的图写进缓存。★ 写失败**不能**让请求失败：图已经在手里了，
    缓存只是下次更快（把"加速手段"做成"失败点"是很常见的自伤）。
    """
    base = _cache_base(url)
    meta = Path(str(base) + ".ct")
    try:
        base.parent.mkdir(parents=True, exist_ok=True)

        # ★ 先写临时文件再 os.replace，是为了**原子**：直接 open(base, "wb") 时
        #   中途被杀会留下一个"看起来存在、其实是半张图"的文件，而它会被当成命中
        #   一直用下去。临时文件名带 uuid，防止两个请求同时取同一张图时互相踩。
        tmp = base.with_name(f"{base.name}.{uuid4().hex[:8]}.tmp")
        tmp.write_bytes(body)
        os.replace(tmp, base)

        # 类型单独存一个文件。★ body 先、meta 后：万一在这里崩了，磁盘上是
        #   "有 body 没 meta"= 未命中，下次重取即可；反过来更别扭。
        meta.write_text(content_type, encoding="ascii")
    except OSError:
        logger.warning("图片缓存写入失败，本次直接返回", exc_info=True)


# ---------------------------------------------------------------------------
# 对外只暴露两个东西：路由，和 fetch_image
# ---------------------------------------------------------------------------
def fetch_image(url: str, *, refresh: bool = False) -> tuple[bytes, str]:
    """取一张图，返回 (字节, content-type)。

    `refresh=True` 跳过缓存（对应接口上的 `?refresh=1`）：缓存的失效方式**不只是
    "太旧"**——图床在同一个 URL 下换了图、或上次缓存写进了一张错的图时，"清一下
    重取"是唯一能自救的操作。
    """
    if not refresh:
        cached = _load_cached(url)
        if cached is not None:
            return cached

    current = url
    # ★ 循环次数是 MAX_REDIRECTS + 1：3 跳重定向意味着最多发 4 次请求，
    #   多出来的那一次是跟完最后一跳之后那次真正取图。
    for _ in range(MAX_REDIRECTS + 1):
        safe_url = _validate(current)  # ← 每一跳都重过全部闸门
        up = _download(safe_url)

        if up.status in REDIRECT_CODES:
            if not up.location:
                raise ImageProxyError(502, f"上游返回 {up.status} 但没有 Location")
            # urljoin 相对跳转（`Location: /a.jpg`）能正确解析
            current = urljoin(safe_url, up.location)
            continue

        if up.status != 200:
            raise ImageProxyError(502, f"上游返回 {up.status}")

        # 类型判断见模块顶部那段。`; charset=...` 之类的参数要切掉再比。
        content_type = up.content_type.split(";")[0].strip().lower()
        if not content_type.startswith("image/"):
            raise ImageProxyError(502, f"上游返回的不是图片（{content_type or '没有 Content-Type'}）")
        if content_type in BLOCKED_IMAGE_TYPES:
            raise ImageProxyError(502, f"拒绝代理 {content_type}（SVG 能携带脚本）")

        if len(up.body) > MAX_IMAGE_BYTES:
            raise ImageProxyError(502, f"图片超过 {MAX_IMAGE_BYTES} 字节上限")

        # ★ 缓存键用**最初请求的 url**，不是跳转后的地址：调用方下次问的还是最初
        #   那个地址，用它的键才能命中。
        _store(url, up.body, content_type)
        return up.body, content_type

    raise ImageProxyError(502, f"重定向超过 {MAX_REDIRECTS} 次")


@router.get("")
def proxy_image(
    url: str = Query(..., description="要代理的图片地址，域名必须在白名单内"),
    refresh: bool = Query(False, description="跳过磁盘缓存，强制重新抓取"),
) -> Response:
    """把第三方图床的图取回来，用我们自己的域名发出去。

    ★ 为什么是 `def` 而不是 `async def`：这个函数里有两处**阻塞**调用
      （`socket.getaddrinfo` 的 DNS 查询、httpx 的同步请求）。写成 `async def`
      会让它们跑在事件循环线程上，一张响应慢的图就能卡住**整个 API**——包括别人的
      请求；写成普通 `def` 时 FastAPI 会把它丢进线程池，阻塞的只是那一个线程。
    """
    try:
        body, content_type = fetch_image(url, refresh=refresh)
    except ImageProxyError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.reason) from exc

    return Response(
        content=body,
        media_type=content_type,
        headers={
            # ★ nosniff 是这里最要紧的一个头：它禁止浏览器"猜"类型——没有它的话，
            #   一个 content-type 是 image/png 但内容其实是 HTML 的响应，被直接
            #   导航打开时会被当成网页渲染。
            "X-Content-Type-Options": "nosniff",
            # 就算真被当成文档打开了，这段 CSP 也让里面的脚本执行不了。
            "Content-Security-Policy": "default-src 'none'; sandbox",
            # 浏览器侧缓存一天：图片内容基本不变（URL 里带日期和唯一 id），
            # 而这样翻页、切数据源时不会每次都来问我们。
            "Cache-Control": "public, max-age=86400",
        },
    )
