"""图片代理：SSRF 防线、同源 XSS 防线、重定向、缓存。

================================================================================
这份测试守的是什么
================================================================================
图片代理是本项目里**唯一一个"服务器替调用方去访问外部地址"的接口**。
别的接口最多是把库里的数据读错，它的坏法是**让服务器去读它不该读的东西**：

  · `?url=file:///C:/Windows/win.ini` → 本机文件被读出来原样返回
  · `?url=http://127.0.0.1:8001/api/analysis/latest` → 自家 API 被当代理跳板
  · `?url=http://2130706433/` → 同上，只是换了种写法（十进制整数 = 127.0.0.1）
  · `?url=http://evil.com/x.jpg` 且 evil.com 的 DNS 指向 169.254.169.254
    → **云元数据端点**，那一行里有整个云账号的临时凭据
  · `?url=http://y.zdmimg.com/x.jpg` 而它 302 到 `http://127.0.0.1:8001/`
    → 白名单在第一跳是成立的，**只看第一跳的实现会放它过去**

还有一类完全不同的坏法：**代理是同源地返回第三方内容的**。
上游如果返回 HTML 而我们不检查类型，用户点开这个链接，
浏览器就把攻击者的 HTML 当成我们站点的页面渲染 —— 同源 XSS。

共同点：**这些全部都是 200**。没有白名单的代理功能完全正常，
返回的字节也是调用方要的字节，只是那些字节来自它不该去的地方。

================================================================================
★ 这份测试一条真实请求都不发
================================================================================
`offline` 是一个 **autouse** 夹具：它把 `_resolve` 和 `_download` 都换成桩，
其中 `_download` 的桩默认**直接抛 AssertionError**。
所以任何一条忘了自己造响应的测试会立刻红，而不是偷偷去连 zdmimg。

这不是洁癖（理由见 smzdm.py 顶部：WAF 已经封过一轮），
而且它让整套测试跑起来**不需要网络** —— clone 下来 `pytest` 就能全绿。

================================================================================
★ 两条刻意的写法
================================================================================
1. **闸门测试都断言"解析器一次都没被调用"**，不只断言状态码。
   状态码对了但地址已经进了解析器，说明防线退到了第 ④ 道 ——
   而 ④ 是**在解析之后**才生效的，那时候 DNS 请求已经发出去了。
   对 `2130706433` 这种写法，这一步的区别就是"有没有泄露一次内网探测"。

2. **反证用例和正例成对出现**（`evil-zdmimg.com` ↔ `y.zdmimg.com`）。
   只测"坏的被挡住"是不够的：一个 `return False` 的实现能让整套闸门测试全绿。
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import images
from app.db.base import get_db
from app.main import app

# 一个公网 IP，用来替掉真实 DNS
PUBLIC_IP = "93.184.216.34"

GOOD_URL = "https://y.zdmimg.com/202609/16/6aaa2bbd797765259.jpg_d200.jpg"

# ★ 在**任何夹具跑之前**抓住真的那个 `_download`。
#   第 10 组测试要用它对着本机服务跑真实链路，
#   而 `offline` 夹具（autouse）会把它换成桩 —— 那时候再想拿真的就拿不到了。
_REAL_DOWNLOAD = images._download


@pytest.fixture()
def client(db):
    """把接口的数据库依赖换成测试会话（和 test_api_alerts.py 同一写法）。

    ★ 这个接口其实**不碰数据库**，但依赖覆盖仍然要写：
      不写的话，app 启动时的 lifespan / 依赖会去连开发库 data/ecom.db。
      而这个接口的全程约束是"不碰开发库"。
    """
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    """★ 全局断网。

    三个替换：
      · `images_dir` → 临时目录。否则测试会把图片写进真的 `data/images/`，
        跑一次 pytest 就在仓库里留下一堆垃圾（而它还在 .gitignore 里，
        所以你会一直不知道）。
      · `_resolve` → 固定返回一个公网 IP。DNS 是**唯一**一个
        "不发 HTTP 但仍然是联网"的步骤，也要断掉。
      · `_download` → 默认抛异常。要造响应的测试自己用 `upstream` 夹具覆盖。
        默认值选"抛异常"而不是"返回一张假图"，是因为前者会**吵**：
        忘了造响应的测试红得很明显，而返回假图会让它以"通过"结束。
    """
    monkeypatch.setattr(images.settings, "images_dir", tmp_path / "images")
    monkeypatch.setattr(images, "_resolve", lambda host: [PUBLIC_IP])

    def _no_network(url: str):
        raise AssertionError(f"这条测试没有桩掉 _download，却想请求：{url}")

    monkeypatch.setattr(images, "_download", _no_network)


@pytest.fixture()
def upstream(monkeypatch):
    """造上游响应，并记录**实际被请求的 URL**。

    用队列而不是"最后一次设置生效"：重定向测试需要
    "第一跳 302、第二跳 200"这种按顺序不同的响应。
    """
    calls: list[str] = []
    queue: list[images._Upstream] = []

    def fake(url: str) -> images._Upstream:
        calls.append(url)
        if not queue:
            raise AssertionError(f"没有排队的响应，却收到了请求：{url}")
        return queue.pop(0)

    def put(status=200, content_type="image/jpeg", location=None, body=b"\xff\xd8fake-jpeg"):
        queue.append(images._Upstream(status, content_type, location, body))

    monkeypatch.setattr(images, "_download", fake)
    return SimpleNamespace(put=put, calls=calls)


def get(client, url, **params):
    return client.get("/api/proxy-image", params={"url": url, **params})


def assert_rejected(client, url, status):
    """断言被拒绝，且**响应体里说明了原因**。

    ★ 只断言状态码是不够的：403 也可能来自"路径不存在"之类的无关原因。
      要求 detail 非空，是要求在"拒绝"这件事上**说得出话**——
      否则用户拿到一个 403 不知道是自己写错了地址还是白名单里没有这个域名。
    """
    resp = get(client, url)
    assert resp.status_code == status, f"{url} 期望 {status}，实际 {resp.status_code}"
    assert isinstance(resp.json().get("detail"), str) and resp.json()["detail"]
    return resp


# ===========================================================================
# 0. 断网夹具本身
# ===========================================================================
def test_the_offline_guard_actually_guards():
    """守的是"断网夹具"。★ 没有这条，一个写错的夹具会让整套测试
    **静默地**开始去连真实图床 —— 而它们多半还是会绿。"""
    with pytest.raises(AssertionError, match="没有桩掉"):
        images._download(GOOD_URL)


# ===========================================================================
# 1. 闸门 ①：协议
# ===========================================================================
@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Windows/win.ini",  # 读了本机文件并原样返回
        "file:///etc/passwd",
        "gopher://y.zdmimg.com/_",  # 能构造任意 TCP 载荷
        "data:text/html,<script>alert(1)</script>",
        "ftp://y.zdmimg.com/a.jpg",
        "//y.zdmimg.com/a.jpg",  # 没有协议：urlsplit 的 scheme 是空的
    ],
)
def test_non_http_schemes_are_rejected(client, url):
    assert_rejected(client, url, 400)


def test_scheme_is_matched_case_insensitively(client, upstream):
    """`HTTP://` 是合法的。★ 用 `.lower()` 比而不是直接比字符串——
    不然大小写会变成一个静默的绕过点（且方向是"误放行"还是"误拒绝"
    取决于写法，两种都不该靠运气）。"""
    upstream.put()
    assert get(client, "HTTP://Y.ZDMIMG.COM/a.jpg").status_code == 200


# ===========================================================================
# 2. 闸门 ②：主机名白名单
# ===========================================================================
@pytest.mark.parametrize(
    "host",
    [
        "evil-zdmimg.com",  # ★ endswith("zdmimg.com") 会让它通过
        "notzdmimg.com",  # ★ 同上
        "zdmimg.com.evil.net",  # 反过来：白名单域名做子域
        "evil.com",
        "localhost",
        "y.zdmimg.com.evil.net",
    ],
)
def test_hosts_outside_the_whitelist_are_rejected(client, host):
    """★ 这一组守的是一个很具体的错误写法：

        host.endswith("zdmimg.com")

    它在 `evil-zdmimg.com` 上是 **True** —— 而那个域名任何人都能注册，
    白名单于是等于没有。正确写法必须卡在点边界上（见 `_host_allowed`）。
    """
    assert_rejected(client, f"https://{host}/a.jpg", 403)


@pytest.mark.parametrize(
    "host",
    [
        "y.zdmimg.com",
        "zdmimg.com",  # 主域本身
        "a.b.c.zdmimg.com",  # 多层子域
        "y.smzdm.com",  # 白名单里的第二个域
    ],
)
def test_whitelisted_hosts_are_accepted(client, upstream, host):
    upstream.put()
    assert get(client, f"https://{host}/a.jpg").status_code == 200


def test_trailing_dot_is_accepted(client, upstream):
    """`y.zdmimg.com.` 是显式写根域的合法 FQDN，**解析结果和不带点完全一样**。

    ★ 它必须被接受，否则就是一个"写法不同、指向相同"的静默缺口；
      而它被接受又要求先 rstrip(".")，否则字符串比较会拒掉它。"""
    upstream.put()
    assert get(client, "https://y.zdmimg.com./a.jpg").status_code == 200


# ===========================================================================
# 3. 闸门 ③：IP 字面量（在解析之前就挡住）
# ===========================================================================
@pytest.mark.parametrize(
    "host",
    [
        "2130706433",  # 127.0.0.1 的十进制整数写法
        "0x7f000001",  # 十六进制
        "0177.0.0.1",  # 八进制
        "127.1",  # 少写两段
        "127.0.0.1",
        "169.254.169.254",  # 云元数据端点
        "10.0.0.1",
        "8.8.8.8",  # ★ 公网 IP 也拒绝：白名单是按域名匹配的
        "[::1]",  # IPv6 回环（urlsplit 会去掉方括号）
    ],
)
def test_ip_literals_are_rejected_without_touching_dns(client, monkeypatch, host):
    """★ 断言的重点是 `resolve_calls == []`，不是状态码。

    状态码对了但地址已经进了解析器，说明防线退到了第 ④ 道；
    而 ④ 是在解析**之后**才生效的 —— 那时候 DNS 查询已经发出去了。
    对 `2130706433` 这种写法，这个区别就是"有没有替攻击者做过一次内网探测"。
    """
    resolve_calls: list[str] = []
    monkeypatch.setattr(images, "_resolve", lambda h: resolve_calls.append(h) or [PUBLIC_IP])

    assert_rejected(client, f"http://{host}/a.jpg", 400)
    assert resolve_calls == []


# ===========================================================================
# 4. 闸门 ④：解析出来的地址
# ===========================================================================
@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # 回环
        "10.1.2.3",  # 内网 A 段
        "172.16.5.5",  # 内网 B 段
        "192.168.1.1",  # 内网 C 段
        "169.254.169.254",  # ★ 云元数据端点
        "100.64.0.1",  # 运营商级 NAT（手写私有段清单最常漏的一个）
        "198.18.0.1",  # 基准测试保留段
        "0.0.0.0",
        "::1",
        "fc00::1",  # IPv6 唯一本地地址
        "fe80::1",  # IPv6 链路本地
        "::ffff:127.0.0.1",  # ★ IPv6 写法的回环：只查 IPv4 私有段的实现会放它过去
    ],
)
def test_resolved_private_addresses_are_rejected(client, monkeypatch, ip):
    """白名单挡的是**名字**，实际连的是**地址**。

    攻击者可以控制一个真的在白名单里的域名，让它的 DNS 指向 127.0.0.1。
    名字合法、地址非法 —— 只检查名字的实现会放它过去，而且看起来很安全。
    """
    monkeypatch.setattr(images, "_resolve", lambda h: [ip])
    assert_rejected(client, GOOD_URL, 403)


def test_every_resolved_address_is_checked_not_just_the_first(client, monkeypatch):
    """★ 双栈域名会同时返回 A 和 AAAA 记录，
    攻击者只需要让**其中一条**指向内网。

    这里把内网地址放在**最后**：只看第一条的实现会放行，
    而它的失败看起来和成功一模一样（返回了图，只是图是从内网取的）。
    """
    monkeypatch.setattr(images, "_resolve", lambda h: ["93.184.216.34", "8.8.4.4", "127.0.0.1"])
    assert_rejected(client, GOOD_URL, 403)


def test_all_public_addresses_pass(client, upstream, monkeypatch):
    """反证：一个内网地址都没有时**必须**放行。

    没有这条，"永远拒绝"的实现也能让上面那批测试全绿。"""
    monkeypatch.setattr(images, "_resolve", lambda h: ["93.184.216.34", "1.1.1.1"])
    upstream.put()
    assert get(client, GOOD_URL).status_code == 200


def test_dns_failure_is_502_not_500(client, monkeypatch):
    """解析不了是**上游**的问题，不是调用方写错了地址。

    ★ 这两者要分开：502 说明"你的地址没问题，是图床那边的事"，
      而 500 只会让人以为是自己传错了参数，然后去翻接口文档。"""

    def boom(host: str):
        raise images.ImageProxyError(502, f"域名解析失败：{host}")

    monkeypatch.setattr(images, "_resolve", boom)
    resp = assert_rejected(client, GOOD_URL, 502)
    assert "解析" in resp.json()["detail"]


# ===========================================================================
# 5. 重定向：每一跳都要重新过闸门
# ===========================================================================
def test_redirect_to_loopback_is_rejected(client, upstream):
    """★ 这是整份文件里最重要的一条。

    `http://y.zdmimg.com/x.jpg` 本身**完全合法** —— 白名单成立、
    协议成立、域名不在内网。攻击者只需要让它 302 到本机服务：

        白名单域名 → 302 → http://127.0.0.1:8001/api/analysis/latest

    如果实现是"检查一次 URL，然后让 httpx 自己跟重定向"
    （`follow_redirects=True`），这一跳**不会被检查**，
    整个白名单就成了一张贴在门上的纸。"""
    upstream.put(status=302, location="http://127.0.0.1:8001/api/analysis/latest")
    assert_rejected(client, GOOD_URL, 400)


def test_redirect_to_a_non_whitelisted_host_is_rejected(client, upstream):
    """同一件事，换成"名字"这一路被挡（走的是另一道闸门，所以单独测）。"""
    upstream.put(status=302, location="http://metadata.google.internal/computeMetadata/v1/")
    assert_rejected(client, GOOD_URL, 403)


def test_redirect_within_the_whitelist_is_followed(client, upstream):
    """反证：白名单**内部**的跳转要能跟。

    没有这条的话，一个"见到 3xx 就报错"的实现也能让上面两条全绿，
    而它会顺手废掉图床的正常行为（真实图床经常 302 到另一个子域）。"""
    upstream.put(status=302, location="https://y.zdmimg.com/real.jpg")
    upstream.put(body=b"\xff\xd8real")

    resp = get(client, GOOD_URL)
    assert resp.status_code == 200
    assert resp.content == b"\xff\xd8real"
    # ★ 第二跳确实发出去了，而且发的是**跳转之后**的地址
    assert len(upstream.calls) == 2
    assert upstream.calls[1] == "https://y.zdmimg.com/real.jpg"


def test_relative_redirect_is_resolved(client, upstream):
    """`Location: /a.jpg` 是相对地址，必须按**当前地址**解析。
    urljoin 少写一个参数就会得到 `/a.jpg` 这种没有主机名的串，
    然后被第 ① 道闸门当成"缺协议"拒掉 —— 报错信息会指向完全无关的地方。"""
    upstream.put(status=302, location="/real.jpg")
    upstream.put(body=b"\xff\xd8real")

    assert get(client, GOOD_URL).status_code == 200
    assert upstream.calls[1] == "https://y.zdmimg.com/real.jpg"


def test_redirect_loop_is_bounded(client, upstream):
    """★ 每一跳都合法，但永远在跳。没有次数上限就是**不返回的请求**，
    而它会一直占着一个工作线程（接口是同步 def，跑在线程池里）。
    几个这样的请求就能把线程池占满，然后整个 API 一起卡住。"""
    for _ in range(images.MAX_REDIRECTS + 2):
        upstream.put(status=302, location="https://y.zdmimg.com/loop.jpg")

    resp = assert_rejected(client, GOOD_URL, 502)
    assert str(images.MAX_REDIRECTS) in resp.json()["detail"]
    # 请求次数被上限卡住，不是"跟到把队列跟空"
    assert len(upstream.calls) == images.MAX_REDIRECTS + 1


def test_redirect_without_location_is_rejected(client, upstream):
    """302 但没有 Location：不能当成 200 用（body 是空的）。"""
    upstream.put(status=302, location=None)
    assert_rejected(client, GOOD_URL, 502)


# ===========================================================================
# 6. 同源 XSS：返回类型必须是图片
# ===========================================================================
@pytest.mark.parametrize(
    "content_type",
    [
        "text/html",  # ★ 浏览器会把它当页面渲染 —— 同源 XSS
        "text/html; charset=utf-8",
        "application/javascript",
        "text/plain",
        "",  # 上游没给类型：不能猜，猜错的方向是"当成 HTML"
    ],
)
def test_non_image_content_types_are_rejected(client, upstream, content_type):
    """代理是**在同源下**返回第三方内容的。

    上游返回一段 HTML、我们不检查类型，用户点开
    `/api/proxy-image?url=...` 这个链接，浏览器就把攻击者的 HTML
    当成我们自己站点的页面来渲染 —— 脚本在**我们的源**上执行，
    能读 localStorage（里面有数据源选择）、能带着 cookie 调我们的接口。

    ★ 跨域的 <img src> 出不了这个事，所以这不是图片的问题，是代理的问题。"""
    upstream.put(content_type=content_type, body=b"<script>alert(document.domain)</script>")
    assert_rejected(client, GOOD_URL, 502)


def test_svg_is_rejected_even_though_it_is_an_image(client, upstream):
    """★ SVG 是合法的 `image/*`，但作为**顶层文档**渲染时内联 <script> 会执行。

    所以"类型以 image/ 开头"这条规则单独用是不够的 ——
    必须把 SVG 单独列出来拒掉。"""
    upstream.put(content_type="image/svg+xml", body=b"<svg onload='alert(1)'/>")
    assert_rejected(client, GOOD_URL, 502)


def test_content_type_parameters_are_ignored(client, upstream):
    """`image/jpeg; charset=binary` 要能通过。
    ★ 直接比字符串会拒掉它 —— 而这类参数在图床上很常见，
      表现为"某个图床的图全部加载不出来"，很难往"类型判断"上想。"""
    upstream.put(content_type="image/jpeg; charset=binary")
    resp = get(client, GOOD_URL)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"


def test_response_carries_nosniff_and_csp(client, upstream):
    """★ nosniff 是这一组里最要紧的头：它禁止浏览器"猜"类型。

    没有它的话，一个 content-type 是 image/png、内容其实是 HTML 的响应，
    在被直接导航打开时仍可能被当成网页渲染 ——
    上面那条"类型必须是图片"的检查就被浏览器自己绕过了。"""
    upstream.put()
    resp = get(client, GOOD_URL)
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in resp.headers["content-security-policy"]


# ===========================================================================
# 7. 大小与上游状态
# ===========================================================================
def test_oversized_image_is_rejected(client, upstream):
    """上限的作用是"不被当成内存放大器"，不是"卡住正常图片"。"""
    upstream.put(body=b"x" * (images.MAX_IMAGE_BYTES + 1))
    assert_rejected(client, GOOD_URL, 502)


def test_image_exactly_at_the_limit_passes(client, upstream):
    """★ 边界要钉住。没有这条的话，`>` 写成 `>=` 不会被任何测试发现，
    而它的表现是"某一张刚好 5MB 的图永远加载不出来"。"""
    upstream.put(body=b"x" * images.MAX_IMAGE_BYTES)
    assert get(client, GOOD_URL).status_code == 200


@pytest.mark.parametrize("status", [204, 301, 403, 404, 429, 500, 503])
def test_upstream_errors_become_502(client, upstream, status):
    """上游的任何非 200 都翻成 502。

    ★ 为什么不是原样透传上游状态码：那样调用方会看到
      "我们自己的接口返回 404"，然后去怀疑路径写错了。
      502 说的是"我替你问了，那边没给成" —— 这才是发生的事。
      （301 单独出现是因为它在我们跟随的重定向集合里，
        但这里给的 Location 是 None，走的是"没有 Location"那条分支；
        301 不在上一组里是为了不把 3xx 的两个分支混在一起。）"""
    if status == 301:
        upstream.put(status=status, location=None)
    else:
        upstream.put(status=status, body=b"")
    assert_rejected(client, GOOD_URL, 502)


# ===========================================================================
# 8. URL 的歧义字符
# ===========================================================================
def test_credentials_in_the_url_are_rejected(client):
    """`https://user:pass@y.zdmimg.com/a.jpg`。

    ★ urlsplit 对这种串的处理是**对的**（host 取 @ 之后的部分），
      但"对"只在我们这一侧成立 —— 有些下游是按**第一个** @ 切的。
      凡是"同一串字符两方理解不同"的地方都要拒绝，
      因为绕过点不需要两边都错，只要两边不一致。"""
    assert_rejected(client, "https://user:pass@y.zdmimg.com/a.jpg", 400)


def test_backslash_is_rejected(client):
    """★ **浏览器把 `\\` 当成 `/`，urlparse 不当**。

    `http://y.zdmimg.com\\@evil.com/x.jpg`：
    浏览器眼里主机是 y.zdmimg.com（合规），urlparse 眼里是 evil.com。
    我们的判断基于 urlparse，所以这里其实是安全的 ——
    但"安全"建立在"我们对、下游也和我们一样"之上，
    而下游到底怎么切不是我们能保证的。直接拒掉，不留这个二义性。"""
    assert_rejected(client, "http://y.zdmimg.com\\@evil.com/x.jpg", 400)


@pytest.mark.parametrize(
    "url",
    [
        "http://y.zdmimg.com/a b.jpg",  # 中间的空格
        "http://y.zdmimg.com/\ta.jpg",  # 制表符
        "http://y.zdmimg.com/a\x00.jpg",  # NUL
    ],
)
def test_inner_whitespace_and_control_chars_are_rejected(client, url):
    """★ 一个实测过的理由：`socket.inet_aton("8.8.8.8 ")` 是**成功**的。

    所以带空白的主机名不先挡掉，就会出现"我们判它非法、
    而某个下游判它合法"的分叉。"""
    assert_rejected(client, url, 400)


def test_surrounding_whitespace_is_tolerated(client, upstream):
    """反证：**两端**的空白要能容忍（从表格里复制粘贴地址很常见）。

    和上一条合起来才是一个完整的行为定义：
    两边剥掉、中间拒绝。只写其中一条的话，另一种写法会被无意中改掉。"""
    upstream.put()
    assert get(client, "  " + GOOD_URL + "  ").status_code == 200


def test_fragment_is_stripped_before_requesting(client, upstream):
    """`#` 之后的内容根本不会发给服务器。

    ★ 留着它不算漏洞，但会让缓存键多出一堆**指向同一张图**的变体
      （`a.jpg`、`a.jpg#x`、`a.jpg#y` 是三个键、三份缓存、三次请求）。"""
    upstream.put()
    get(client, GOOD_URL + "#section")
    assert upstream.calls == [GOOD_URL]


def test_bad_port_is_rejected(client):
    """`urlsplit` 的 `.port` 在端口不是数字时会抛 ValueError，
    而它在 `hostname` 之前就被访问 —— 不接住的话这里是一个 500。"""
    assert_rejected(client, "https://y.zdmimg.com:99999/a.jpg", 400)


def test_missing_url_parameter_is_422(client):
    """不带 url：交给 FastAPI 的参数校验（422），不是我们手写的 400。
    ★ 这一条守的是"url 是必填"——写成 `url: str = ""` 的话它会变成 400，
      而前端拿到的报错会从"缺参数"变成"参数为空"，指向完全不同的排查方向。"""
    resp = client.get("/api/proxy-image")
    assert resp.status_code == 422


@pytest.mark.parametrize("url", ["", "   "])
def test_empty_url_is_400(client, url):
    assert_rejected(client, url, 400)


# ===========================================================================
# 9. 磁盘缓存
# ===========================================================================
def test_second_request_does_not_touch_upstream(client, upstream):
    """★ 缓存存在的理由是**离线演示**，不是省流量：
    面试现场断网 / 图床被墙时，抓过的图还在。

    所以这条测试的意义是"命中缓存 = 一个请求都不发"，
    而不是"快了一点"。"""
    upstream.put(body=b"\xff\xd8once")

    assert get(client, GOOD_URL).content == b"\xff\xd8once"
    upstream.calls.clear()

    resp = get(client, GOOD_URL)
    assert resp.status_code == 200
    assert resp.content == b"\xff\xd8once"
    assert upstream.calls == []  # ← 一个请求都没发


def test_refresh_bypasses_the_cache(client, upstream):
    """`?refresh=1` 跳过缓存。

    ★ 缓存的失效方式不只是"太旧"：图床在同一个 URL 下换了图、
      或者上一次写进了一张错的图 —— 这两种情况下"清一下重取"
      是唯一能自救的操作。没有它就只能手工去 data/images 里删文件。"""
    upstream.put(body=b"\xff\xd8old")
    upstream.put(body=b"\xff\xd8new")

    assert get(client, GOOD_URL).content == b"\xff\xd8old"
    assert get(client, GOOD_URL, refresh=1).content == b"\xff\xd8new"
    assert len(upstream.calls) == 2


def test_cache_is_keyed_by_the_original_url(client, upstream):
    """★ 重定向之后，缓存要挂在**最初请求的那个地址**上。

    挂在跳转后的地址上的话，下次调用方问的还是最初那个 URL，
    于是永远命中不了 —— 缓存看起来在工作（文件确实写了），
    但每一个请求都还是要走一遍完整的三跳。"""
    upstream.put(status=302, location="https://y.zdmimg.com/real.jpg")
    upstream.put(body=b"\xff\xd8moved")

    assert get(client, GOOD_URL).content == b"\xff\xd8moved"
    upstream.calls.clear()

    assert get(client, GOOD_URL).status_code == 200
    assert upstream.calls == []


def test_incomplete_cache_entry_is_treated_as_a_miss(client, upstream):
    """只有图片、没有类型元数据 → 当**未命中**，重新取。

    ★ 这对应"写到一半崩了"：`_store` 是先写图、后写类型的。
      如果这里当成命中，就会返回一个 content-type 为空的响应，
      而浏览器的表现是"图裂了"—— 且**重启也不会恢复**，
      因为那个半成品一直都在，看起来像一个永远修不好的 bug。"""
    base = images._cache_base(GOOD_URL)
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_bytes(b"\xff\xd8half")  # 只写图，不写 .ct

    upstream.put(body=b"\xff\xd8fresh")
    assert get(client, GOOD_URL).content == b"\xff\xd8fresh"


def test_cache_entry_with_empty_content_type_is_a_miss(client, upstream):
    """类型文件存在但是**空的** → 也当未命中。

    ★ 这一条是变异测试挖出来的：把"类型文件必须存在"那个判断删掉，
      整套测试**照样全绿** —— 因为 `read_text` 抛的 FileNotFoundError
      正好被同一个 `except OSError` 接住了，行为碰巧没变。

      但它顺手指出了一个真的洞：**空文件**能通过 `is_file()`，
      于是缓存会命中、并返回一个 `content_type=""` 的响应。
      而"类型必须是 image/*"这条规则，在缓存这条路径上是**不再检查的**
      （靠的是"进缓存的都过过闸门"这条不变量）——
      空的类型文件正是把这条不变量捅破的那个口子。"""
    base = images._cache_base(GOOD_URL)
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_bytes(b"\xff\xd8poisoned")
    (base.parent / (base.name + ".ct")).write_text("", encoding="ascii")

    upstream.put(body=b"\xff\xd8fresh")
    resp = get(client, GOOD_URL)
    assert resp.content == b"\xff\xd8fresh"
    assert resp.headers["content-type"] == "image/jpeg"  # ← 不是空串


def test_cache_write_failure_does_not_break_the_response(client, upstream, monkeypatch):
    """★ 把"加速手段"做成"失败点"是很常见的自伤：
    缓存写不进去（磁盘满、权限不对）不该让用户看不到图 ——
    图已经在手里了。"""
    upstream.put(body=b"\xff\xd8fine")

    def boom(*args, **kwargs):
        raise OSError("磁盘满了")

    monkeypatch.setattr(images.os, "replace", boom)
    assert get(client, GOOD_URL).content == b"\xff\xd8fine"


# ===========================================================================
# 10. 对着一个**真的 HTTP 服务器**跑一遍 _download
#
# ★ 上面所有测试都把 `_download` 换成了桩，这是对的（一条外网请求都不发），
#   但代价是**真正的 httpx 调用一次都没被执行过**。
#   而这里恰好有一条假设是"我以为如此"而不是"我验证过"：
#
#       follow_redirects=False 真的能让 httpx 不跟重定向吗？
#
#   如果它其实会跟（或者哪天版本升级改了默认值），
#   那么"每一跳都重新过闸门"这句话就是**假的**：
#   302 的目标会被 httpx 直接取回来，而我们的闸门一次都没经过它。
#   这是整份文件里唯一一条"桩掉的测试永远发现不了"的漏洞。
#
# ★ 所以这里起一个**本机**的 HTTP 服务（不是外网）：
#   闸门那一半仍然被短路掉（本机地址本来就该被闸门拒绝，
#   这是设计而不是缺陷），但 `_download` 那一半是真的。
#   两半各自有覆盖，合起来才是完整路径。
# ===========================================================================
class _Handler(BaseHTTPRequestHandler):
    """一个小到能一眼看完的图片服务器。"""

    hits: list[str] = []

    def log_message(self, *args):  # noqa: D102 —— 默认实现会往 stderr 打日志，测试里太吵
        pass

    def do_GET(self):  # noqa: N802 —— 这个名字由 stdlib 定，不能改
        type(self).hits.append(self.path)

        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/secret")
            self.end_headers()
            return

        if self.path == "/secret":
            # 只该在"httpx 真的跟了重定向"时被命中
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            self.wfile.write(b"\xff\xd8secret")
            return

        if self.path == "/big.jpg":
            # 比上限大三倍，而且**分块慢慢发**：
            # 整块发的话客户端早就 break 了，服务端还在写，读起来反而看不出区别
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            chunk = b"x" * (256 * 1024)
            try:
                for _ in range(images.MAX_IMAGE_BYTES * 3 // len(chunk)):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionAbortedError):
                pass  # ★ 客户端按大小上限断开是**预期行为**，不是错误
            return

        if self.path == "/html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<script>alert(document.domain)</script>")
            return

        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"\xff\xd8real-bytes")


@pytest.fixture()
def local_http(monkeypatch):
    """一个监听在 127.0.0.1 随机端口上的真 HTTP 服务。

    ★ 它把 `_download` **换回真的那个**。`offline` 夹具（autouse）默认
      把它换成"一被调用就抛 AssertionError"的桩，那个默认值是对的 ——
      它保证了"忘了造响应"的测试会吵闹地失败。
      这里是有意地声明例外：这个夹具存在的前提就是发真请求（对**本机**）。

    ★ 用 monkeypatch 再设一次，而不是直接赋值：monkeypatch 的撤销是一个**栈**，
      所以 teardown 会先把桩装回去、再把真的装回去，最终状态和测试开始时一致。
      手动 `images._download = ...` 的话，撤销顺序要自己维护，
      而且其它测试会跟着一起漏。
    """
    monkeypatch.setattr(images, "_download", _REAL_DOWNLOAD)

    _Handler.hits = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            base=f"http://127.0.0.1:{server.server_address[1]}", hits=_Handler.hits
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_download_does_not_follow_redirects_by_itself(local_http):
    """★★ 整份文件里唯一一条"桩掉就测不到"的漏洞。

    `fetch_image` 的重定向循环之所以安全，前提是 **httpx 自己不跟重定向**。
    如果它跟了，302 的目标会被它直接取回来 —— 而那个目标
    **一次闸门都没经过**，上面那些"重定向到内网被拒"的测试
    全部变成自说自话（它们桩掉了 `_download`，所以永远看不到这件事）。

    所以这里直接调 `_download`（不经过任何桩），断言：
      · 拿回来的是 302，不是最终页
      · `/secret` **从来没有被请求过**
    """
    up = images._download(f"{local_http.base}/redirect")

    assert up.status == 302
    assert up.location == "/secret"
    assert up.body == b""
    assert "/secret" not in local_http.hits, (
        "httpx 自己把重定向跟了 —— 也就是说 302 的目标绕过了全部闸门。"
        "`follow_redirects=False` 没有生效，这是 SSRF 防线的一个真缺口。"
    )


def test_download_reads_real_headers_and_body(local_http):
    """真 httpx 拿回来的 header 和 body。

    ★ 顺带钉住一件容易搞错的事：`_Upstream.content_type` 是**原始 header**，
      带着 `; charset=utf-8`。切分只发生在 `fetch_image` 里那一处 ——
      如果这里也切一次，就会出现"两处都在做规范化"，
      而那种代码在改动时几乎必然只改一处。"""
    up = images._download(f"{local_http.base}/img.jpg")

    assert up.status == 200
    assert up.content_type == "image/jpeg; charset=utf-8"
    assert up.body == b"\xff\xd8real-bytes"


def test_download_stops_reading_at_the_size_cap(local_http):
    """★ "限制大小"和"检查大小"的区别，用真 socket 证明一遍。

    服务端往连接里写的是上限的三倍。如果我们先把整个响应读进内存
    再判断长度，这里拿回来的就是三倍上限的字节 ——
    而那时候"限制"已经没发生过了（内存已经吃掉了）。
    """
    up = images._download(f"{local_http.base}/big.jpg")

    assert len(up.body) > images.MAX_IMAGE_BYTES  # 越界的那一块读到了，交给上层判
    assert len(up.body) < images.MAX_IMAGE_BYTES + 1024 * 1024, (
        f"读回来 {len(up.body)} 字节，说明没有在上限处停下来"
    )


def test_full_stack_against_a_real_server(client, local_http, monkeypatch):
    """★ 把整条链路（除闸门以外）真的跑一遍：httpx → 判断 → 缓存 → 响应。

    `_validate` 在这里被换成直通。**这不是在回避闸门**：
    闸门会拒绝 127.0.0.1 是**对的**（那正是它的职责），
    所以"对着本机服务跑通链路"和"闸门正常工作"这两件事
    在同一个进程里不可能同时成立。
    闸门由上面第 1~4 组用桩覆盖，这里补的是它们覆盖不到的那一半。
    """
    monkeypatch.setattr(images, "_validate", lambda url: url)

    first = client.get("/api/proxy-image", params={"url": f"{local_http.base}/img.jpg"})
    assert first.status_code == 200
    assert first.content == b"\xff\xd8real-bytes"
    assert first.headers["content-type"] == "image/jpeg"  # ← charset 被切掉了

    hits_after_first = len(local_http.hits)
    second = client.get("/api/proxy-image", params={"url": f"{local_http.base}/img.jpg"})
    assert second.status_code == 200
    assert len(local_http.hits) == hits_after_first, "第二次应该命中磁盘缓存，不再请求上游"

    # 同一台服务器上的 HTML，走完真实链路之后仍然要被类型检查挡住
    html = client.get("/api/proxy-image", params={"url": f"{local_http.base}/html"})
    assert html.status_code == 502
