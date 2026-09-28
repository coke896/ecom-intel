"""图片代理：把第三方图床的图取回来，由我们自己的域名发给浏览器。

================================================================================
为什么需要一个代理，而不是让浏览器直接 <img src="https://y.zdmimg.com/...">
================================================================================
三个理由，第三个才是重点：

1. **防盗链**：图床普遍校验 Referer，从 localhost 打开的页面直接热链接会被拒。
   换成服务端去取，Referer 这一环就不参与了。
2. **一处收口**：图片地址来自采集数据，而采集数据是**外部输入**。
   前端把它直接塞进 <img src> 的话，"数据里能塞什么地址"
   就等于"浏览器会去请求什么地址"。
3. **★ 不把访客的 IP 和浏览行为送给第三方**。
   直接热链接时，每个打开页面的人的 IP、UA、Referer 都会到 zdmimg 那边。
   这是个很小的隐私考量，但它是"这个代理值得存在"的真正理由——
   前两条都可以靠"换个图床"解决，只有这条不能。

================================================================================
不做白名单的后果不是"开放代理"，是一个标准 SSRF
================================================================================
    GET /api/proxy-image?url=http://127.0.0.1:8001/api/analysis/latest

这一条请求的意思是"**让我的服务器**去读它自己才读得到的东西，然后把结果给你"。
攻击面包括：

  · 本机服务：上面那个 8001 就是本项目自己的 API
  · 内网资产：192.168.* / 10.* 上的管理后台、数据库的 HTTP 接口
  · **云元数据端点 169.254.169.254**：AWS / GCP / 阿里云的实例凭据就在那里，
    拿到它等于直接接管整个云账号。这是 SSRF 最经典也最贵的一种后果。

四道闸门，缺一不可，而且**顺序有意义**：

  ① **协议白名单**（只允许 http/https）。
     少了它，`file:///C:/Users/辰/Desktop/api-key.txt` 会被读出来原样返回。
  ② **主机名白名单**（只允许 *.zdmimg.com / *.smzdm.com）。
  ③ **IP 字面量一律拒绝**，包括 `2130706433`、`0x7f000001`、`0177.0.0.1`、
     `127.1` 这些变体。为什么单独一条：这些地址**不需要 DNS**
     就已经指向内网，必须在解析之前挡住。
     （`2130706433` 是 127.0.0.1 的十进制整数写法，浏览器和 inet_aton 都认。）
  ④ **解析出来的地址必须是公网地址**（见 `_assert_public`）。

================================================================================
★ 这道防线的真正边界：白名单挡的是"名字"，实际连的是"地址"
================================================================================
④ 之所以必须存在，是因为 ② + ③ 合起来仍然不够：

    攻击者可以控制一个**真的在白名单里**的域名，
    然后让它的 DNS 记录指向 127.0.0.1。

（更现实一点的版本：白名单里的某个子域被接管，或者主站有开放重定向。）
名字是白名单里的名字，解析出来的地址是内网地址——
**只检查名字的实现会放它过去**，而且它看起来完全安全。

================================================================================
★ 诚实的残余风险：两次解析之间的窗口（TOCTOU / DNS rebinding）
================================================================================
本模块的流程是：**我们解析一次 → 检查 IP → 交给 httpx 去请求**。
httpx 请求时会**再解析一次**。两次解析之间有一个时间窗口，
恶意 DNS 可以在这期间把答案从"公网 IP"换成"127.0.0.1"。

**这个窗口在本模块里没有被完全关掉**，我不把它说成"我做了完整防护"。
彻底关掉它需要"把连接钉死在已经校验过的那个 IP 上"
（自定义 transport 覆盖 DNS 解析，或者直接连 IP 再手工设 Host 头），
代价是 TLS 的 SNI 和证书校验会变得需要手工处理——
对一个**只放行两个图床域名**的代理来说，不值得。

所以这里的取舍是：用四道闸门把攻击面收窄到
"攻击者必须同时控制一个白名单域名的 DNS"，**并把残余风险写在这里**。
真上生产的话，正确的下一步是给 httpx 挂一个钉住 DNS 结果的 transport。

================================================================================
★ Content-Type 必须是 image/*，这不是洁癖
================================================================================
代理是**在同源下**返回第三方内容的。如果不检查类型，
只要让上游返回一段 HTML，用户点开 `/api/proxy-image?url=...` 这个链接，
浏览器就会**把攻击者的 HTML 当成我们自己站点的页面来渲染**——
这是同源 XSS，不是"显示了一张怪图"。

（跨域的 <img src> 出不了这个事，所以说到底不是图片的问题，是代理的问题。）

三重防护：
  · 上游 Content-Type 必须是 image/*，否则整条请求失败
  · **image/svg+xml 单独拒绝**：它是合法的 image/*，
    但作为顶层文档渲染时内联 <script> 会执行
  · 响应带 `X-Content-Type-Options: nosniff` 和一段 CSP，
    让浏览器即使被直接导航过来也不执行里面的东西

================================================================================
★ 这个接口不接受 ?source=，也【不】走 api.js 的 apiRequest
================================================================================
其它接口都从 `apiRequest` 走（那里统一注入了 `?source=`），这个是例外。
理由有两层：

  · 取图的是**浏览器的 <img> 加载器**，JS 根本没有机会插手，
    所以它不是"我们决定不走 apiRequest"，是走不了。
  · 它本身也不该接受 `?source=`：这是一个**取字节的动作**，
    不是一次数据查询——和 `POST /api/alerts/check` 是同一类
    （理由见 alerts.py 里那段同款说明）。

================================================================================
★ 测试和演示都不联网
================================================================================
`_resolve` / `_download` 都是**模块级函数**，测试 monkeypatch 掉它们，
所以 `tests/test_api_images.py` 一条真实请求都不发。
这不是洁癖：SMZDM 那边已经被 WAF 封过一轮（见 smzdm.py 顶部），
"跑测试顺手又打了一下对方站点"的代价，可能是整条采集线再也验证不了。

顺带一提，磁盘缓存（`data/images/`）让**离线演示**也能看到图：
第一次打开页面时抓过的图，之后断网也还在。
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
    """一次被拒绝的代理请求。

    带上 HTTP 状态码，是因为拒绝的原因分两类，对调用方的意义不同：

      · **400**：这串 URL 本身就不对（没写协议、带控制字符）
      · **403**：URL 能读懂，但我拒绝去取（不在白名单、解析到内网）
      · **502**：URL 没问题，是上游没给出可用的图（连不上、类型不对、太大）

    分开之后，前端排查"图为什么不出来"时能直接看出
    "是我配错了地址"还是"是图床那边的问题"——都塞 500 的话只能靠猜。
    """

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


# ---------------------------------------------------------------------------
# 闸门参数
# ---------------------------------------------------------------------------
# 白名单按**域名后缀**匹配。实测库里 97/100 条真实商品的图都在 y.zdmimg.com，
# smzdm.com 是它的主域（图片偶尔会走主域下的路径），所以两个都放。
#
# ★ 故意不放行任何"通用图床"（imgur、微博图床……）：
#   白名单一旦宽到"能放用户提供的图"，它就不再是一道闸门了。
ALLOWED_IMAGE_HOSTS: tuple[str, ...] = ("zdmimg.com", "smzdm.com")

# 单张图上限 5MB。
# 真实商品图实测在 10~200KB 量级，5MB 留了两个数量级的余量——
# 上限的作用是"防止被当成内存放大器"，不是"卡住正常图片"。
MAX_IMAGE_BYTES = 5 * 1024 * 1024

# 上游超时。取不到就报错，不能让一个挂住的图床把 API 的工作线程占满。
FETCH_TIMEOUT = 10.0

# 最多跟几跳重定向。★ 每一跳都会重新过一遍全部闸门——
# 不加这个的话，`白名单域名 → 302 → http://127.0.0.1:8001/` 直接绕过白名单。
MAX_REDIRECTS = 3

REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})

# ★ 单独拒掉 SVG：它能携带 <script>，而当前返回类型是"图片"时
#   我们没法区分"当图片用"和"当文档打开"——直接不给。
BLOCKED_IMAGE_TYPES = frozenset({"image/svg+xml", "image/svg"})


# ---------------------------------------------------------------------------
# 闸门 ③：IP 字面量
# ---------------------------------------------------------------------------
def _is_ip_literal(host: str) -> bool:
    """这个主机名是不是一个 IP 地址字面量（含各种进制/简写变体）。

    ★ 两道判断都要有，因为它们覆盖的是**不同写法**：

    ① `ipaddress.ip_address` —— 标准写法，含 IPv6（`::1`、`fc00::1`）。
       注意 `urlsplit().hostname` 已经把 `[::1]` 的方括号去掉了。

    ② `socket.inet_aton` —— 覆盖 C 风格的 IPv4 变体，实测：
         "2130706433"  -> 127.0.0.1   （十进制整数）
         "0x7f000001"  -> 127.0.0.1   （十六进制）
         "0177.0.0.1"  -> 127.0.0.1   （八进制）
         "127.1"       -> 127.0.0.1   （少写几段）
       `ipaddress` 这四个**一个都不认**。浏览器和 inet_aton 都认。

    ★ 这里拒绝的是**所有** IP 字面量，不只是内网 IP。
      理由：白名单是按域名匹配的，IP 字面量本来就不可能出现在白名单里。
      提前拒绝省掉了一次 DNS，而且让"解析之前就挡住"这句话成立——
      不然 `2130706433` 会走到解析那一步，而那一步是可以被绕的。
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

    ★ 为什么返回列表而不是取第一个：`getaddrinfo` 对双栈域名会同时返回
      A 和 AAAA 记录，攻击者可以让**其中一条**指向内网而另一条指向公网。
      只看第一条的实现会被 50% 的概率绕过——而且失败的那次看起来完全正常。
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        # 解析不了就是取不到图，不是客户端写错了地址
        raise ImageProxyError(502, f"域名解析失败：{host}") from exc
    return sorted({info[4][0] for info in infos})


def _assert_public(ip_text: str) -> None:
    """这个地址必须是公网地址。

    ★ 用 `is_global` 这一个判断，而不是手写一串私有网段。
      手写清单的失效方式是**静默的**——漏掉 `100.64.0.0/10`（运营商级 NAT）
      或 `198.18.0.0/15`（基准测试网段）不会报错，只会放行。
      而 `is_global` 是 stdlib 里最严的那个谓词，实测它同时覆盖：

          127.0.0.1 / 10.0.0.1 / 172.16.0.1 / 192.168.1.1   （内网与回环）
          169.254.169.254                                    （云元数据端点）
          100.64.0.1 / 198.18.0.1 / 192.0.0.1 / 240.0.0.1     （非公网保留段）
          0.0.0.0                                            （未指定）
          ::1 / fc00::1 / fe80::1 / ::ffff:127.0.0.1          （IPv6 侧，含 v4 映射）

      最后一行值得单独看一眼：`::ffff:127.0.0.1` 是 IPv6 写法的回环地址。
      只查 IPv4 私有段的实现会放它过去——这是"两种写法指向同一个东西"
      这类绕过里的标准案例。
    """
    ip = ipaddress.ip_address(ip_text)
    if not ip.is_global:
        raise ImageProxyError(403, f"该域名解析到非公网地址 {ip_text}，拒绝代理")


# ---------------------------------------------------------------------------
# 闸门 ① ②：协议与主机名
# ---------------------------------------------------------------------------
def _host_allowed(host: str) -> bool:
    """主机名是否在白名单内。

    ★ 后缀匹配必须卡在**点边界**上：
      `host.endswith("zdmimg.com")` 会让 `evil-zdmimg.com` 通过——
      而那个域名是任何人都能注册的，白名单等于没有。
      （同理 `notzdmimg.com`、`zdmimg.com.evil.net` 都要挡住，
        后者靠"必须 host == 域名 或 host.endswith('.' + 域名)"一并解决。）

    ★ 结尾的 `.` 要先去掉：`y.zdmimg.com.` 是合法的 FQDN 写法
      （根域显式写法），DNS 解析结果和不带点完全一样，
      但字符串比较不一样——又一个"写法不同、指向相同"的绕过点。
    """
    host = host.lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in ALLOWED_IMAGE_HOSTS)


def _validate(url: str) -> str:
    """过一遍全部闸门，返回可以安全请求的 URL。

    返回的是 `urlunsplit` 重新拼出来的串——刻意**不是**原样返回：
    规范化过的串才是我们真正会去请求的那个东西，
    后面所有日志和判断都基于它，不留"我检查的和它请求的不是同一个串"的缝。
    """
    if not url or not url.strip():
        raise ImageProxyError(400, "缺少 url 参数")
    raw = url.strip()

    # ★ 先扫字符，再做结构化解析。这一步排掉的是**解析器歧义**——
    #   凡是"同一串字符被两方理解成不同东西"的地方，都是绕过点：
    #     · 空白：`inet_aton("8.8.8.8 ")` 是**成功**的（实测），
    #       所以带空格的地址不先挡掉，就会变成一个我们判成非法、
    #       而下游判成合法的输入。
    #     · 控制字符：urlparse 会把它们当普通字符收下，
    #       而 httpx / 浏览器 / 中间代理的处理各不相同。
    #     · 反斜杠：**浏览器把 `\` 当 `/`，urlparse 不当**。
    #       `http://y.zdmimg.com\@evil.com/` 在两边是两个不同的主机名。
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

    # 用户名密码那一段（`https://user:pass@host/`）。
    #   ★ urlsplit 对这种串的处理是正确的（host 取 @ 之后的部分），
    #     但"正确"只在**我们**这一侧成立——有些下游是按第一个 @ 切的。
    #     与其赌下游的行为，不如直接拒绝：图床地址里不会有凭据。
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

    # ③ IP 字面量。放在白名单检查之前：白名单是按名字匹配的，
    #   一个 IP 在这里注定通不过，提前拒绝才能保证"内网地址根本没进入解析"。
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

    # 重新拼一遍。★ urlunsplit 会丢掉 fragment，那是对的——
    #   `#` 之后的内容根本不会发给服务器，留着只会让缓存键多出一堆变体。
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, ""))


# ---------------------------------------------------------------------------
# 取字节
# ---------------------------------------------------------------------------
class _Upstream(NamedTuple):
    """从上游拿到的东西（还没做任何判断）。

    ★ 特意把"取"和"判"分开：`_download` 只管网络，`fetch_image` 只管策略。
      这样测试可以 monkeypatch 掉 `_download` 造出任意响应，
      而**所有判断逻辑仍然是真跑一遍的**——
      如果判断写在了 `_download` 里，测试就只能连网才能覆盖到它了。
    """

    status: int
    content_type: str
    location: str | None
    body: bytes


def _download(url: str) -> _Upstream:
    """发一次请求，读回响应。★ 测试会 monkeypatch 掉这个函数。

    ★ 不跟随重定向（`follow_redirects=False`）：让 httpx 自己跟，
      它就会把 302 的目标直接取了，而那个目标**没有过闸门**。
      所以我们自己跟，每一跳都从 `_validate` 重新走一遍。
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

                # ★ 边读边数，超了立刻 break——这是"限制大小"和"检查大小"的区别。
                #   如果写成 `body = resp.read()` 再判断长度，
                #   上游发 10GB 我们就会先在内存里收下 10GB，然后才报"太大了"。
                #   那时候"限制"已经没发生过了。
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
        # 连接失败、超时、TLS 报错都归到这里。★ 只回类型名不回原始串：
        # 原始异常里可能带上完整的 URL（包括我们刚拒绝过的那些），
        # 而这个响应是要发给浏览器的。
        raise ImageProxyError(502, f"请求上游失败（{type(exc).__name__}）") from exc


# ★ User-Agent 在这里重新定义，而不是 from scraper.sources.smzdm import HEADERS。
#   那个 import 会让 **Web 层依赖采集层**，方向正好和本项目的架构相反
#   （采集层独立于 Web 层，见 app/main.py 顶部第 2 条）。
#   代价是一个字符串重复了两处，收益是 web 进程不需要为了发一张图
#   去 import 整个采集模块。这个交换是划算的。
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}


# ---------------------------------------------------------------------------
# 磁盘缓存
#
# ★ 它解决的问题不是"省流量"，是**离线演示**：
#   面试现场断网 / 图床被墙 / 对方改了防盗链策略时，
#   如果页面上的图全靠实时取，那就是一片破图图标。
#   抓过一次之后就从本地出，演示这件事本身变得不依赖网络。
#
# ★ 缓存是**无上限**的，这是有意的取舍：
#   单张图有 5MB 上限，而数据源是有限的（库里 100 个真实商品），
#   所以最坏情况是有界的。加一个 LRU 逐出就要引入"何时算过期、
#   怎么并发清理"一整套逻辑，为这点收益不值得。
#   真上生产的话，正确做法是交给 CDN 或 nginx 的缓存层，而不是写在应用里。
# ---------------------------------------------------------------------------
def _cache_base(url: str) -> Path:
    """URL → 缓存文件路径。

    ★ 用 sha256 而不是把 URL 转义成文件名：
       URL 里有 `/`、`?`、`&`，转义规则要自己维护，而且长度不可控
       （Windows 路径有 260 字符限制，一个带长 query 的图床 URL 能超）。
       哈希之后长度固定、字符集安全，代价是"看文件名猜不出是哪张图"——
       而这个代价可以接受，因为排查时看的是 URL 不是文件名。

    ★ 分两级目录（前两位做子目录）：避免一个目录里堆几千个文件。
      这在 Windows 上不是性能优化，是"资源管理器打开那个目录会不会卡死"的问题。
    """
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return settings.images_dir / digest[:2] / digest


def _load_cached(url: str) -> tuple[bytes, str] | None:
    """命中就返回 (字节, content-type)，否则 None。

    ★ 缓存这条路径**不再做类型检查**——它靠的是一条不变量：
      "进缓存的图一定过了 `image/*` 那道闸门"（`_store` 只在检查之后被调用）。
      不变量一旦破掉，缓存就成了绕过类型检查的旁路。

      ★ 变异测试实测出来的分工，写在这里免得下次误改：
        · **非空判断是真正载重的那个**——删掉它，一条测试立刻变红
        · 两个 `is_file()` 是**冗余**的：删掉它们，那条测试**照样绿**，
          因为文件不存在时 `read_bytes` / `read_text` 抛的 OSError
          会被下面的 except 接住，结果碰巧一样。
          留着它们是为了"未命中"写成显式分支，而不是拿异常当控制流——
          但这属于可读性，不属于防线，别把它当成有测试兜底的代码。
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
        # 读缓存失败不是错误，只是没命中（也可能是权限问题，记一笔）
        logger.warning("读图片缓存失败，改走网络：%s", base, exc_info=True)
    return None


def _store(url: str, body: bytes, content_type: str) -> None:
    """把取回来的图写进缓存。

    ★ 写失败**不能**让请求失败：图已经在手里了，缓存只是下次更快。
      （把"加速手段"做成"失败点"是很常见的自伤。）
    """
    base = _cache_base(url)
    meta = Path(str(base) + ".ct")
    try:
        base.parent.mkdir(parents=True, exist_ok=True)

        # ★ 先写临时文件再 os.replace，是为了**原子**。
        #   直接 open(base, "wb") 的话，进程在写到一半时被杀
        #   （或者被 Ctrl+C），目录里就留下一个"看起来存在、
        #   其实是半张图"的文件，而它会被当成命中一直用下去——
        #   表现是"这张图永远缺一半"，且重启也不恢复。
        #   os.replace 在同一文件系统内是原子的：
        #   别人看到的 base 要么不存在，要么是完整的一份。
        #
        #   临时文件名带 uuid，是为了两个请求同时取同一张图时不互相踩：
        #   各写各的临时文件，最后谁后 replace 谁赢——而两份内容是一样的。
        tmp = base.with_name(f"{base.name}.{uuid4().hex[:8]}.tmp")
        tmp.write_bytes(body)
        os.replace(tmp, base)

        # 类型单独存一个文件。★ 顺序是 body 先、meta 后：
        #   万一在这里崩了，磁盘上是"有 body 没 meta"= 未命中，
        #   下次重取即可；反过来则是"有类型没图"，那种状态更别扭。
        meta.write_text(content_type, encoding="ascii")
    except OSError:
        logger.warning("图片缓存写入失败，本次直接返回", exc_info=True)


# ---------------------------------------------------------------------------
# 对外只暴露两个东西：路由，和 fetch_image
# ---------------------------------------------------------------------------
def fetch_image(url: str, *, refresh: bool = False) -> tuple[bytes, str]:
    """取一张图，返回 (字节, content-type)。

    `refresh=True` 跳过缓存（对应接口上的 `?refresh=1`）。
    留着它是因为缓存的失效方式**不只是"太旧"**——
    图床在同一个 URL 下换了图、或者上一次缓存写进了一张错误的图，
    这两种情况下"清一下重取"是唯一能自救的操作。
    """
    if not refresh:
        cached = _load_cached(url)
        if cached is not None:
            return cached

    current = url
    # ★ 循环次数是 MAX_REDIRECTS + 1：3 次重定向意味着最多发 4 次请求。
    #   多出来的这一次是"跟完最后一跳之后那次真正的取图"。
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

        # ★ 缓存键用**最初请求的 url**，不是跳转后的地址。
        #   调用方下次问的还是最初那个地址，用它的键才能命中。
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
      （`socket.getaddrinfo` 的 DNS 查询、httpx 的同步请求）。
      写成 `async def` 会让它们跑在事件循环线程上，
      一张响应慢的图就能卡住**整个 API**——包括别人的请求。
      写成普通 `def`，FastAPI 会把它丢进线程池（默认 40 个线程），
      阻塞的是那一个线程，事件循环照常转。
      （这是本项目里唯一一处"故意不用 async"的地方，所以单独记一笔。）
    """
    try:
        body, content_type = fetch_image(url, refresh=refresh)
    except ImageProxyError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.reason) from exc

    return Response(
        content=body,
        media_type=content_type,
        headers={
            # ★ nosniff 是这里最要紧的一个头：它禁止浏览器"猜"类型。
            #   没有它的话，一个 content-type 是 image/png
            #   但内容其实是 HTML 的响应，在被直接导航打开时会被当成网页渲染。
            "X-Content-Type-Options": "nosniff",
            # 就算真被当成文档打开了，这段 CSP 也让里面的脚本执行不了。
            "Content-Security-Policy": "default-src 'none'; sandbox",
            # 浏览器侧缓存一天：图片内容基本不变（URL 里带日期和唯一 id），
            # 而这样翻页、切数据源时不会每次都来问我们。
            "Cache-Control": "public, max-age=86400",
        },
    )
