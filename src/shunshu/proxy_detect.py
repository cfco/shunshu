"""添加书源时自动探测：这个站该直连还是走本机代理。

只在「添加书源」那一下执行一次：先直连，直连"连不上"再依次试本机代理候选；
结论（直连 / 某个代理 URL）由 app 层记进 config.json 的 site_proxies 并写到来源上，
之后每一轮抓取都只按配置走，不再临时探测，也不读系统/环境代理。

判定"连不上"只认连接级失败（拒绝/超时/DNS）：站点只要给了 HTTP 答复
（哪怕是 4xx/5xx/空页/非 HTML），就说明这条网络路径是通的，不能因为一次
临时 503 就把站误记成"需要代理"。

候选代理来源只有两处，且只指向回环地址：
1. Windows 注册表里的 LAN 代理设置（代理软件开了「系统代理」就在这里）；
2. 常见代理客户端的默认端口兜底（Clash/v2rayN 等）。
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from .http import ConnectionFailureError, FetchError, Fetcher, ProxyError, proxy_endpoints

# 探测用的短超时：添加是手动低频操作，直连等一个连接超时确认「不通」即可接受。
# 探测 Fetcher 不重试（retries=0），避免一个连不上的站等满两三倍超时。
PROBE_TIMEOUT = 6.0
# TCP 预探活超时：先过滤掉根本没监听的端口，HTTP 探测才不会干等
TCP_TIMEOUT = 1.0

# 常见本机代理客户端的默认监听端口，注册表读不到时兜底；只探 127.0.0.1，绝不扫外网
_LOCAL_PROXY_PORTS: tuple[tuple[str, int], ...] = (
    ("http", 7890),    # Clash / Clash Verge 混合端口
    ("http", 7891),    # Clash 旧版
    ("http", 10809),   # v2rayN 的 HTTP 端口
    ("socks5", 10808), # v2rayN 的 SOCKS 端口
    ("http", 2080),    # 部分代理客户端
    ("socks5", 1080),  # 传统 SOCKS 客户端
    ("http", 8888),
    ("http", 8080),
    ("socks5", 1086),
)

# 注册表 ProxyServer 里 "http=host:port;https=host:port" 形态的协议前缀
_PREFIXED = ("http", "https", "socks", "socks5")


@dataclass(slots=True)
class ProbeResult:
    """探测结论：proxy 为空串表示直连，非空表示走该代理；全不通时 probe_url 返回 None。

    html 是探测那一下抓回的目录页 HTML：首轮建档直接复用，省掉重复的一次敲门；
    站点虽可达但内容不可用（4xx/非 HTML 等）时留空串，首轮照常自己再抓。
    """

    proxy: str
    html: str = ""


def _endpoint_key(candidate: str) -> tuple[str, int] | None:
    """按主机名+端口归一：http=/https= 指向同一端口时只探一次（协议差异对 HTTP 代理无意义）。"""
    parts = urlsplit(candidate)
    if not parts.hostname:
        return None
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return None
    return parts.hostname.lower(), port


def parse_proxy_server(raw: str | None) -> list[str]:
    """把注册表 ProxyServer 的几种写法拆成代理 URL 列表，https 项排在前面（目录页多是 https）。

    支持的形态：
      "127.0.0.1:7890"
      "http=127.0.0.1:7890;https=127.0.0.1:7890"
      "http://127.0.0.1:7890"
    已带协议名的原样保留；裸 host:port 按 HTTP 代理补协议。空白与非法项丢弃。
    同一 host:port 的多个写法只保留一个（优先 https 形式）。
    """
    text = (raw or "").strip()
    if not text:
        return []
    pending: list[str] = []
    for piece in text.replace(",", ";").split(";"):
        item = piece.strip()
        if not item:
            continue
        scheme, sep, rest = item.partition("=")
        if sep and scheme.lower() in _PREFIXED:
            item = rest.strip()
            # 注册表写 socks= 时 requests 只认 socks5（本机 socks5 代理占绝大多数）
            scheme = "socks5" if scheme.lower() == "socks" else scheme.lower()
            if "://" not in item:
                item = f"{scheme}://{item}"
        elif "://" in item:
            pass  # 已带协议，交给末尾的 hostname 校验
        elif "=" not in item:
            item = f"http://{item}"  # 裸 host:port 按 HTTP 代理补协议
        else:
            continue  # "=8080" 这种既无协议前缀又不是合法 host:port 的畸形项
        if urlsplit(item).hostname:
            pending.append(item)
    # https= 与 http= 指向同一端口时，让 https 形式先试
    pending.sort(key=lambda p: 0 if p.startswith("https://") else 1)
    return _dedupe_by_endpoint(pending)


def _dedupe_by_endpoint(candidates: list[str]) -> list[str]:
    """按 host:port 归一去重并保序（输入顺序里靠前的写法胜出）。"""
    seen: set[tuple[str, int]] = set()
    out: list[str] = []
    for candidate in candidates:
        key = _endpoint_key(candidate)
        if key is None or key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def registry_proxy_server() -> str | None:
    """读 Windows LAN 设置里的 ProxyServer；代理开关没开或非 Windows 返回 None。"""
    try:
        import winreg
    except ImportError:  # 非 Windows
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not enabled:
                return None
            value, _ = winreg.QueryValueEx(key, "ProxyServer")
            return str(value) if value else None
    except OSError:
        return None


def candidate_proxies() -> list[str]:
    """本机代理候选，注册表代理排第一，常见默认端口兜底，按 host:port 去重保序。"""
    candidates = [*parse_proxy_server(registry_proxy_server()),
                  *(f"{scheme}://127.0.0.1:{port}" for scheme, port in _LOCAL_PROXY_PORTS)]
    return _dedupe_by_endpoint(candidates)


def tcp_reachable(proxy_url: str, timeout: float = TCP_TIMEOUT) -> bool:
    """代理候选的 TCP 预探活：端口没监听就直接跳过，不浪费一次 HTTP 超时。

    只对回环地址做预探（候选本来也全是回环）；万一传入别的主机，默认放行不在这里耗时。
    """
    parts = urlsplit(proxy_url)
    host = parts.hostname or ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        return True
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def make_probe_fetcher(user_agent: str, max_bytes: int) -> Fetcher:
    """探测专用抓取器：短超时、不重试。TLS 仍走系统证书库（Fetcher 构造时注入）。"""
    return Fetcher(user_agent=user_agent, timeout=PROBE_TIMEOUT, retries=0,
                   backoff=1.0, max_bytes=max_bytes)


def probe_url(fetcher: Fetcher, url: str) -> ProbeResult | None:
    """先直连后走代理地试一个目录页。

    返回 ProbeResult(proxy="", html=...)  直连可达，此站不需要代理；
    返回 ProbeResult(proxy=代理URL, ...) 直连连不上、该本机代理可达；
    返回 None                            直连和所有候选代理都连不上。
    只有连接级失败（ConnectionFailureError/ProxyError）才换路径：站点给了 HTTP 答复
    就算可达，临时 4xx/5xx/空页/非 HTML 不会再把站推给代理。
    fetcher 由调用方构造（短超时、不重试）；测试可注入只实现 fetch_html(url, proxy="") 的假对象。
    """
    try:
        html = fetcher.fetch_html(url, proxy="")
        return ProbeResult(proxy="", html=html)
    except ConnectionFailureError:
        # 直连根本没建立（拒绝/超时/DNS）：这才是"需要代理"的唯一情形
        pass
    except FetchError:
        # 站点通过直连给了 HTTP 答复（4xx/5xx/空页/非 HTML/证书等）：网络是通的，
        # 判直连可达，内容层问题交给后续抓取/解析去报，不在这里误切代理
        return ProbeResult(proxy="", html="")
    except (ValueError, OSError):
        return None

    for candidate in candidate_proxies():
        try:
            proxy_endpoints(candidate)  # 格式校验：只放行 http/https/socks5
        except ProxyError:
            continue
        if not tcp_reachable(candidate):
            continue
        try:
            html = fetcher.fetch_html(url, proxy=candidate)
            return ProbeResult(proxy=candidate, html=html)
        except (ConnectionFailureError, ProxyError):
            # 代理端口没真通（在监听但不是代理、socks 缺依赖、经代理仍连不上站）：试下一个
            continue
        except FetchError:
            # 代理连通且站点给了 HTTP 答复：这条路径可用（内容层问题与选路无关）
            return ProbeResult(proxy=candidate, html="")
        except (ValueError, OSError):
            continue
    return None
