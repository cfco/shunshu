"""抓取与编码处理。盗版站大量是 gb18030，编码判断必须走在解析前面。"""

from __future__ import annotations

import re
import threading
import time
from urllib.parse import urlsplit

import requests
from requests.utils import get_environ_proxies

_META_CHARSET = re.compile(rb"""charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.I)
_HTML_ESSENCE = re.compile(rb"<\s*(?:html|head|body|meta|div|table|dl|ul|p|a)\b", re.I)

PROXY_SCHEMES = ("http", "https", "socks5")
_PROXY_TELL = re.compile(r"proxy|socks", re.I)


class FetchError(Exception):
    """网络/响应层面的失败，monitor 会保留旧状态并按退避重试。"""


class ProxyError(FetchError):
    """代理本身不通：和「站点抓不到」分开记，一眼能看出是代理没启动还是站点改版。"""


def proxy_label(proxy: str) -> str:
    """给日志和菜单看的代理名：去掉账号密码，避免凭据落进 data/shunshu.log。"""
    parts = urlsplit((proxy or "").strip())
    if not parts.scheme or not parts.hostname:
        return (proxy or "").strip()
    port = f":{parts.port}" if _has_port(parts) else ""
    return f"{parts.scheme.lower()}://{parts.hostname}{port}"


def _has_port(parts) -> bool:
    try:
        return parts.port is not None
    except ValueError:
        return False


def proxy_endpoints(proxy: str) -> dict[str, str]:
    """把来源上手写的代理字符串换成 requests 用的字典。

    只允许 http/https/socks5：socks4 之类 requests 支持得不完整，与其到了站点再报一个
    看不懂的错误，不如在这里就说清格式。
    """
    value = (proxy or "").strip()
    if not value:
        return {}
    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:
        raise ProxyError(f"代理端口不是数字：{proxy_label(value)}") from None
    if scheme not in PROXY_SCHEMES or not parts.hostname or port == 0:
        raise ProxyError(f"代理地址格式不对：{proxy_label(value)}（只支持 http:// 、https:// 、socks5://）")
    return {"http": value, "https": value}


def system_proxy_for(url: str) -> str:
    """这台机器当前对该网址生效的系统代理——就是浏览器用的那套（环境变量 + 注册表）。

    来源没写 `proxy` 时 requests 走的正是这里，所以菜单要把它显示出来，否则看不出
    一个国内站也被代理软件绕了一圈；绕过表（no_proxy）由 requests 一并算好。
    """
    try:
        target = assert_http_url(url)
        scheme = urlsplit(target).scheme.lower()
        return str(get_environ_proxies(target, no_proxy=None).get(scheme) or "")
    except (ValueError, OSError):
        return ""


_POOL_PREFIX = re.compile(r"^\s*(?:\w*(?:Connection|Pool))\(\s*host='[^']*'(?:,\s*port=\d+)?\s*\)\s*:\s*", re.I)


def _deepest_error(exc: BaseException, depth: int = 0) -> BaseException:
    """requests 的代理异常是套娃的，最里层那句才是人话（WinError / Errno）。"""
    if depth >= 6:
        return exc
    for candidate in (getattr(exc, "reason", None), *getattr(exc, "args", ())):
        if isinstance(candidate, BaseException) and candidate is not exc:
            return _deepest_error(candidate, depth + 1)
    return exc


def _proxy_hint(exc: Exception) -> str:
    text = str(_deepest_error(exc)) or str(exc)
    if "missing dependencies" in text.lower():
        return "SOCKS 代理需要 PySocks，装一下：uv add PySocks"
    for _ in range(3):
        text = _POOL_PREFIX.sub("", text)
    text = text.replace("Failed to establish a new connection:", "").strip(" '\"")
    return text[:160] or type(exc).__name__


def _sniff_charset(body: bytes) -> str | None:
    head = body[:4096]
    m = _META_CHARSET.search(head)
    if m:
        name = m.group(1).decode("ascii", "ignore").strip().lower()
        if name in ("gb2312", "gbk"):
            # gb2312 声明的页面常混入 gb18030 才有的字
            return "gb18030"
        return name
    return None


def decode_body(body: bytes, header_charset: str | None) -> str:
    """按 header → meta → utf-8 严格 → gb18030 严格 → 兜底替换 的顺序定编码。"""
    candidates: list[str] = []
    for name in (header_charset, _sniff_charset(body)):
        if name:
            norm = name.strip().lower().replace("=", "")
            if norm in ("gb2312", "gbk"):
                norm = "gb18030"
            if norm not in candidates:
                candidates.append(norm)
    for fallback in ("utf-8", "gb18030"):
        if fallback not in candidates:
            candidates.append(fallback)
    for enc in candidates:
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("gb18030", "replace")


def assert_http_url(url: str) -> str:
    """只放行 http(s)：托盘菜单和 toast 的 launch 会把 URL 交给 shell，必须挡掉 javascript:/data:/本地路径。"""
    parts = urlsplit((url or "").strip())
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError(f"不是可用的 http(s) 地址: {url!r}")
    return parts.geturl()


_TRUST_STORE_DONE = False


def use_system_trust_store() -> None:
    """TLS 改用 Windows 系统证书库验，行为跟浏览器一致。

    盗版站常常只发叶子证书、少发中间证书：浏览器会自己补链，certifi 不会，
    于是浏览器能开的站在工具里报 CERTIFICATE_VERIFY_FAILED。代理软件自签的 CA
    装进了系统库，走系统库也就跟着信了。补不上就退回 certifi，不会比现在更差。
    """
    global _TRUST_STORE_DONE
    if _TRUST_STORE_DONE:
        return
    _TRUST_STORE_DONE = True
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:
        pass


class Fetcher:
    """并发检查时每线程各用一个 Session：requests.Session 不是线程安全的。"""

    def __init__(self, user_agent: str, timeout: float = 15.0, retries: int = 2,
                 backoff: float = 3.0, max_bytes: int = 3_000_000):
        use_system_trust_store()
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.backoff = backoff
        self.max_bytes = max_bytes
        self.headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Upgrade-Insecure-Requests": "1",
        }
        self._local = threading.local()

    @property
    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(self.headers)
            self._local.session = session
        return session

    def fetch_html(self, url: str, proxy: str = "") -> str:
        assert_http_url(url)
        endpoints = proxy_endpoints(proxy)
        last_error = "未知错误"
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(self.backoff * attempt)
            try:
                resp = self.session.get(url, timeout=self.timeout, stream=True,
                                        allow_redirects=True, proxies=endpoints)
                try:
                    if resp.status_code >= 400:
                        last_error = f"HTTP {resp.status_code}"
                        continue
                    body = self._read_capped(resp)
                finally:
                    resp.close()
                ctype = resp.headers.get("Content-Type", "")
                if body[:2048].strip() and not _HTML_ESSENCE.search(body[:4096]) and "html" not in ctype.lower():
                    raise FetchError(f"响应不是 HTML（Content-Type={ctype or '未知'}），可能是下载链或接口")
                charset = resp.encoding if "charset" in ctype.lower() else None
                text = decode_body(body, charset)
                if not text.strip():
                    last_error = "响应内容为空"
                    continue
                return text
            except FetchError:
                raise
            except (requests.exceptions.ProxyError, requests.exceptions.InvalidSchema) as exc:
                if not endpoints:
                    last_error = f"{type(exc).__name__}: {exc}"
                    continue
                # 代理没启动 / socks 代理缺依赖：重试也不会好，直接报出去让人去改 novels.json
                raise ProxyError(f"{proxy_label(proxy)} 走不通：{_proxy_hint(exc)}") from exc
            except requests.exceptions.ConnectionError as exc:
                # socks 走不通时 requests 只报 ConnectionError，但句子里一定带着 proxy/socks 这个词；
                # 站点自己断链（RemoteDisconnected）不含它，仍按网络错误重试
                if endpoints and _PROXY_TELL.search(str(exc)):
                    raise ProxyError(f"{proxy_label(proxy)} 走不通：{_proxy_hint(exc)}") from exc
                last_error = f"{type(exc).__name__}: {exc}"
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            except Exception as exc:  # 站点返回畸形内容时 requests 会抛各种底层异常
                last_error = f"{type(exc).__name__}: {exc}"
        raise FetchError(last_error)

    def _read_capped(self, resp: requests.Response) -> bytes:
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total > self.max_bytes:
                raise FetchError(f"响应超过 {self.max_bytes} 字节上限，疑似非目录页")
        return b"".join(chunks)
