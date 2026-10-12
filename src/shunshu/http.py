"""抓取与编码处理。盗版站大量是 gb18030，编码判断必须走在解析前面。"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import requests

_META_CHARSET = re.compile(rb"""charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.I)
_HTML_ESSENCE = re.compile(rb"<\s*(?:html|head|body|meta|div|table|dl|ul|p|a)\b", re.I)

PROXY_SCHEMES = ("http", "https", "socks5")
_PROXY_TELL = re.compile(r"proxy|socks", re.I)


class FetchError(Exception):
    """网络/响应层面的失败，monitor 会保留旧状态并按退避重试。"""


class ConnectionFailureError(FetchError):
    """连接根本没建立：连接被拒/超时/DNS 失败，一次 HTTP 响应都没拿到。

    代理探测只在直连抛这个错时才改走代理；站点只要给了 HTTP 答复
    （4xx/5xx/空页/非 HTML），就说明网络路径本身是通的，不该误记成"需要代理"。
    """


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
    """并发检查时每线程各用一个 Session：requests.Session 不是线程安全的。

    线程池本身常驻复用：工作线程不销毁，绑在 threading.local 上的 Session 才活得过
    单轮检查，keep-alive 连接也能跨轮复用；每轮现建线程池等于每轮把连接池全部作废。
    """

    def __init__(self, user_agent: str, timeout: float = 15.0, retries: int = 2,
                 backoff: float = 3.0, max_bytes: int = 3_000_000, workers: int = 6):
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
        # 常驻并发池：workers 变化（重新加载配置）时关掉旧池再建，不中途改已建池的容量
        self._pool: ThreadPoolExecutor | None = None
        self._pool_workers = 0
        self._pool_lock = threading.Lock()

    @property
    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(self.headers)
            # 禁止从环境变量（HTTP_PROXY / HTTPS_PROXY / ALL_PROXY / NO_PROXY）读取代理：
            # 用户没手动给来源配 proxy 时直连，配了才走指定代理，行为更可控、速度更稳定
            session.trust_env = False
            self._local.session = session
        return session

    def pool(self, workers: int) -> ThreadPoolExecutor:
        """返回与并发数等大的常驻线程池；并发配置变了就关旧池建新池。"""
        workers = max(1, int(workers))
        with self._pool_lock:
            if self._pool is not None and self._pool_workers != workers:
                # 不等手头任务也不取消：已提交（含还在排队）的抓取让它在旧池里跑完，
                # 只是后续轮次改用新池。这里若 cancel_futures，正在跑的那一轮里
                # 排队中的来源会抛 CancelledError 把整轮检查打断
                self._pool.shutdown(wait=False)
                self._pool = None
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=workers,
                                                thread_name_prefix="shunshu-fetch")
                self._pool_workers = workers
            return self._pool

    def close(self) -> None:
        """释放常驻池：重新加载配置换 Fetcher、进程退出时调用。假抓取器没有此方法，调用前先 getattr。

        这里才取消还在排队的任务（cancel_futures）：调用方已经放弃这个抓取器，
        排队的抓取没有继续跑的意义；正在执行的任务不受影响，跑完自然退出。
        """
        with self._pool_lock:
            if self._pool is not None:
                self._pool.shutdown(wait=False, cancel_futures=True)
                self._pool = None
                self._pool_workers = 0

    def fetch_html(self, url: str, proxy: str = "") -> str:
        assert_http_url(url)
        endpoints = proxy_endpoints(proxy)
        last_error = "未知错误"
        # 最后一次失败是不是"连接没建立"：决定最终抛 ConnectionFailureError 还是普通 FetchError
        connection_failed = False
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(self.backoff * attempt)
            try:
                resp = self.session.get(url, timeout=self.timeout, stream=True,
                                        allow_redirects=True, proxies=endpoints)
                try:
                    if resp.status_code >= 500:
                        # 5xx 多为站点临时故障，退避后值得再试一次
                        last_error = f"HTTP {resp.status_code}"
                        connection_failed = False
                        continue
                    if resp.status_code >= 400:
                        # 4xx 是站点的明确答复（永久搬家/反爬/不存在）：重试结果不会变，
                        # 而且对探测来说"能回 4xx"证明这条网络路径是通的，直接失败
                        raise FetchError(f"HTTP {resp.status_code}")
                    body = self._read_capped(resp)
                finally:
                    resp.close()
                ctype = resp.headers.get("Content-Type", "")
                if body[:2048].strip() and not _HTML_ESSENCE.search(body[:4096]) and "html" not in ctype.lower():
                    raise FetchError(f"响应不是 HTML（Content-Type={ctype or '未知'}），可能是下载链或接口")
                charset = resp.encoding if "charset" in ctype.lower() else None
                text = decode_body(body, charset)
                if not text.strip():
                    # 服务器给了响应但内容为空：属于"站点有答复"，不当连接失败，也不重试
                    raise FetchError("响应内容为空")
                return text
            except FetchError:
                # 4xx/非 HTML/空页/超限：立即抛出，保持原类型（ConnectionFailureError 不会在这里产生）
                raise
            except (requests.exceptions.ProxyError, requests.exceptions.InvalidSchema) as exc:
                if not endpoints:
                    # 直连理论上不会收到 ProxyError，保守按可重试的连接失败处理
                    last_error = f"{type(exc).__name__}: {exc}"
                    connection_failed = True
                    continue
                # 代理没启动 / socks 代理缺依赖：重试也不会好，直接报出去让人去改 novels.json
                raise ProxyError(f"{proxy_label(proxy)} 走不通：{_proxy_hint(exc)}") from exc
            except requests.exceptions.SSLError as exc:
                # 注意 SSLError 在 requests 里继承自 ConnectionError，必须放在它前面截：
                # SSL 握手失败时 TCP 已经连上，属于「站点有答复但证书不干净」，
                # 不算连接失败，代理探测也不能据此把站推给代理
                last_error = f"{type(exc).__name__}: {exc}"
                connection_failed = False
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                # socks 走不通时 requests 只报 ConnectionError，但句子里一定带着 proxy/socks 这个词；
                # 站点自己断链（RemoteDisconnected）不含它，仍按网络错误重试
                if endpoints and _PROXY_TELL.search(str(exc)):
                    raise ProxyError(f"{proxy_label(proxy)} 走不通：{_proxy_hint(exc)}") from exc
                last_error = f"{type(exc).__name__}: {exc}"
                connection_failed = True
            except requests.RequestException as exc:
                # SSL 等其它请求异常：尝试重试用尽后按普通 FetchError 报（站点给不出干净响应，
                # 但区别于"连不上"，代理探测不会据此切代理）
                last_error = f"{type(exc).__name__}: {exc}"
                connection_failed = False
            except Exception as exc:  # 站点返回畸形内容时 requests 会抛各种底层异常
                last_error = f"{type(exc).__name__}: {exc}"
                connection_failed = False
        if connection_failed:
            raise ConnectionFailureError(last_error)
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
