"""
ipv6_net.py - 严格 IPv6-only 的 HTTP(S) 客户端

设计目标（针对 asmr.one 下载场景）：
  1. 进程内硬性禁止任何 IPv4 出站：socket.getaddrinfo 只返回 AAAA 结果，
     显式 AF_INET 查询直接抛错；所有连接只创建 AF_INET6 socket。
  2. 支持 SNI 变体（末尾加 "."、大写）以绕过链路上基于明文 SNI 的 RST 阻断，
     同时保留证书链 + 主机名校验。
  3. 支持手动固定 IPv6 地址（绕开被污染的 DNS）与可选 IPv6 代理。
  4. 支持重定向、重试/退避、流式读取与 Range 断点续传。

仅使用 Python 标准库。
"""
from __future__ import annotations

import base64
import http.client
import re
import socket
import ssl
import struct
import subprocess
import time
import urllib.parse
import warnings
from dataclasses import dataclass, field
from typing import Optional

_ORIG_GETADDRINFO = socket.getaddrinfo
_ORIG_SOCKET = socket.socket


class IPv4Forbidden(OSError):
    """尝试建立 IPv4 连接时被抛出。"""


class _GuardedSocket(_ORIG_SOCKET):
    """拒绝任何 AF_INET socket；未指定 family 时默认 AF_INET6。"""

    def __init__(self, family=-1, type=-1, proto=-1, fileno=None):
        if family == socket.AF_INET:
            raise IPv4Forbidden("AF_INET socket creation blocked (strict IPv6-only mode)")
        if family == -1:
            family = socket.AF_INET6
        super().__init__(family, type, proto, fileno)


def enable_strict_ipv6() -> None:
    """全局安装 IPv6-only 护栏。程序入口调用一次即可。"""
    if getattr(socket, "_asmr_ipv6_patched", False):
        return

    def guarded(host, port, family=0, type=0, proto=0, flags=0):
        if family == socket.AF_INET:
            raise IPv4Forbidden(
                f"IPv4 is disabled (strict IPv6-only mode): {host}:{port}"
            )
        res = _ORIG_GETADDRINFO(host, port, socket.AF_INET6, type, proto, flags)
        v6 = [r for r in res if r[0] == socket.AF_INET6]
        if not v6:
            raise socket.gaierror(
                f"no AAAA record for {host!r}; IPv4 is forbidden in strict mode. "
                f"Use --pin HOST=IPv6 to specify an address manually."
            )
        return v6

    socket.getaddrinfo = guarded
    socket.socket = _GuardedSocket  # type: ignore[misc,assignment]
    socket._asmr_ipv6_patched = True  # type: ignore[attr-defined]


def resolve_aaaa(host: str, port: int, pin: Optional[str] = None) -> list[tuple[str, int]]:
    """只解析 AAAA；pin 为手动指定的 IPv6 字面量。"""
    if pin:
        return [(pin.strip("[]"), port)]
    res = _ORIG_GETADDRINFO(host, port, socket.AF_INET6, socket.SOCK_STREAM)
    addrs = [r[4][0] for r in res if r[0] == socket.AF_INET6]
    if not addrs:
        raise socket.gaierror(f"no AAAA record for {host!r}")
    return [(a, port) for a in dict.fromkeys(addrs)]


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

# 这些域名的明文 SNI 在部分 IPv6 链路上会被 RST，需要 SNI 变体
DEFAULT_BYPASS_HOSTS = (
    "asmr.one",
    "api.asmr.one",
    "www.asmr.one",
    "api.asmr-300.com",
    "asmr-300.com",
    "asmr-200.com",
    "asmr-100.com",
)


@dataclass
class ProxyConfig:
    scheme: str                 # "http" | "socks5"
    host: str
    port: int
    user: Optional[str] = None
    password: Optional[str] = None


@dataclass
class NetConfig:
    pins: dict[str, str] = field(default_factory=dict)   # host -> IPv6 字面量
    proxy: Optional[ProxyConfig] = None
    bind_ipv6: Optional[str] = None   # 出栈源 IPv6 地址（锁定某块网卡，绝不可能是 IPv4）
    bypass_hosts: tuple[str, ...] = DEFAULT_BYPASS_HOSTS
    sni_policy: str = "auto"    # auto | always | never
    verify_hostname: bool = True
    timeout: float = 20.0
    retries: int = 3
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
    )


def parse_proxy(raw: str) -> Optional[ProxyConfig]:
    """支持 http://[::1]:8080、socks5://user:pass@[2001:db8::1]:1080"""
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    u = urllib.parse.urlsplit(raw)
    scheme = u.scheme.lower()
    if scheme not in ("http", "https", "socks5", "socks5h"):
        raise ValueError(f"unsupported proxy scheme: {scheme}")
    if not u.hostname:
        raise ValueError(f"invalid proxy address: {raw}")
    return ProxyConfig(
        scheme="socks5" if scheme.startswith("socks5") else "http",
        host=u.hostname,
        port=u.port or (1080 if scheme.startswith("socks5") else 8080),
        user=urllib.parse.unquote(u.username) if u.username else None,
        password=urllib.parse.unquote(u.password) if u.password else None,
    )


def parse_pins(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for it in items or []:
        if "=" not in it:
            raise ValueError(f"--pin expects HOST=IPv6, got {it!r}")
        k, v = it.split("=", 1)
        v = v.strip("[]")
        # 提前校验是合法 IPv6
        try:
            socket.inet_pton(socket.AF_INET6, v)
        except OSError as e:
            raise ValueError(f"invalid IPv6 address for {k}: {v}") from e
        out[k.strip().lower()] = v
    return out


# --------------------------------------------------------------------------
# 本机网卡 IPv6 源地址发现：把出栈流量锁定到指定网卡（仍然只可能是 IPv6）
# --------------------------------------------------------------------------

@dataclass
class InterfaceIPv6:
    title: str                        # ipconfig 段落全名
    short: str                        # 去掉 "以太网适配器 " 之类的短名
    ifindex: Optional[int]
    addresses: list[str]              # 可路由 IPv6（ULA / 全局）
    temporary: list[str]              # RFC4941 临时地址，Windows 默认出栈优先

    @property
    def label(self) -> str:
        return f"{self.short} (ifIndex={self.ifindex})" if self.ifindex else self.short

    def pick(self) -> Optional[str]:
        return (self.temporary or self.addresses or [None])[0]


def validate_bind_ipv6(value: str) -> str:
    """绑定地址必须是 IPv6 字面量；任何 IPv4 输入一律抛 IPv4Forbidden。"""
    v = (value or "").strip().strip("[]")
    if "%" in v:                       # 允许 fd00::1%27 这类带 scope 的写法
        v = v.split("%", 1)[0]
    if not v:
        raise ValueError("empty IPv6 bind address")
    try:
        socket.inet_pton(socket.AF_INET, v)
    except OSError:
        pass
    else:
        raise IPv4Forbidden(f"IPv4 is forbidden: {v!r} is an IPv4 address")
    try:
        socket.inet_pton(socket.AF_INET6, v)
    except OSError as e:
        raise ValueError(f"invalid IPv6 bind address: {value!r}") from e
    if v.lower().startswith("fe80"):
        raise ValueError(f"link-local address cannot be an egress source: {v}")
    if v == "::1":
        raise ValueError("loopback ::1 cannot be an egress source")
    return v


def _ipconfig_text() -> str:
    """读取 ipconfig /all 的文本（Windows 中文控制台用 mbcs 解码）。"""
    try:
        raw = subprocess.run(["ipconfig", "/all"], capture_output=True,
                             timeout=30).stdout or b""
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"cannot run ipconfig: {e}") from e
    for enc in ("mbcs", "gbk", "utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def dns_servers() -> list[str]:
    """列出本机配置的 DNS 服务器，用于提示可能的 IPv4 DNS 侧信道。"""
    servers: list[str] = []
    for line in _ipconfig_text().splitlines():
        if re.search(r"DNS\s*(服务器|Servers|Server)", line, re.I):
            # 在整行里找地址，避免 rsplit(":") 截掉 IPv6 DNS 字面量
            found = re.findall(
                r"(?:\d{1,3}\.){3}\d{1,3}|[0-9a-fA-F]{0,4}(?::[0-9a-fA-F]{0,4}){2,}",
                line)
            for addr in found:
                if addr not in servers:
                    servers.append(addr)
    return servers


def interfaces_ipv6() -> list[InterfaceIPv6]:
    """解析 ipconfig /all，列出每块网卡的非 link-local IPv6 地址与 ifIndex。"""
    text = _ipconfig_text()
    out: list[InterfaceIPv6] = []
    cur: Optional[InterfaceIPv6] = None
    for line in text.splitlines():
        if (line and not line[0].isspace() and line.rstrip().endswith(":")
                and re.search(r"适配器|adapter", line, re.I)):
            title = line.strip().rstrip(":").strip()
            short = re.sub(r"^.*?(?:适配器|adapter)\s+", "", title,
                           flags=re.I).strip() or title
            cur = InterfaceIPv6(title, short, None, [], [])
            out.append(cur)
            continue
        if cur is None or "IPv6" not in line:
            continue
        found = re.findall(r"[0-9a-fA-F]{0,4}(?::[0-9a-fA-F]{0,4}){2,}(?:%\d+)?", line)
        if not found:
            continue
        val = found[-1]
        scope = None
        if "%" in val:
            val, _, sc = val.partition("%")
            m = re.match(r"\d+", sc)
            if m:
                scope = int(m.group())
        try:
            socket.inet_pton(socket.AF_INET6, val)
        except OSError:
            continue
        low = line.lower()
        is_link_local = (val.lower().startswith("fe80")
                         or "本地链接" in line or "link-local" in low)
        is_temporary = "临时" in line or "temporary" in low
        if is_link_local:
            if scope is not None and cur.ifindex is None:
                cur.ifindex = scope
        elif is_temporary:
            cur.temporary.append(val)
        else:
            cur.addresses.append(val)
    return out


def discover_bind_ipv6(interface: str) -> tuple[str, str]:
    """按接口名或 ifIndex 找到该网卡的 IPv6 源地址，返回 (地址, 接口描述)。"""
    key = (interface or "").strip()
    if not key:
        raise ValueError("empty interface name")
    ifaces = interfaces_ipv6()
    if key.isdigit():
        idx = int(key)
        matches = [i for i in ifaces if i.ifindex == idx]
    else:
        low = key.lower()
        matches = [i for i in ifaces
                   if i.short.lower() == low or i.title.lower() == low]
        if not matches:
            matches = [i for i in ifaces
                       if low in i.title.lower() or low in i.short.lower()]
    usable = [i for i in matches if i.pick()]
    if not usable:
        avail = ", ".join(
            f"{i.short}(ifIndex={i.ifindex}, IPv6={len(i.addresses) + len(i.temporary)})"
            for i in ifaces) or "无"
        raise ValueError(
            f"接口 {key!r} 没有可用的非 link-local IPv6 地址；本机接口: {avail}")
    iface = usable[0]
    return validate_bind_ipv6(iface.pick()), iface.label


def needs_bypass(host: str, cfg: NetConfig) -> bool:
    h = host.lower().rstrip(".")
    if cfg.sni_policy == "always":
        return True
    if cfg.sni_policy == "never":
        return False
    return any(h == d or h.endswith("." + d) for d in cfg.bypass_hosts)


def sni_variants(host: str, cfg: NetConfig) -> list[str]:
    """返回按优先级排列的候选 SNI。"""
    if needs_bypass(host, cfg):
        return [host.upper() + ".", host + ".", host.upper()]
    return [host, host.upper() + ".", host + "."]


# --------------------------------------------------------------------------
# 底层连接
# --------------------------------------------------------------------------

def _recv_exact(s: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("unexpected EOF during proxy handshake")
        buf += chunk
    return buf


def _direct(ip: str, port: int, timeout: float,
            bind_ipv6: Optional[str] = None) -> socket.socket:
    # 使用原始 socket 类，避免 ssl 对 socket 子类的兼容性问题；
    # 显式 AF_INET6 保证连接一定是 IPv6（绝无 IPv4 回退）。
    s = _ORIG_SOCKET(socket.AF_INET6, socket.SOCK_STREAM)
    s.settimeout(timeout)
    if bind_ipv6:
        # 2 元组 bind：Windows 上给 ULA 源地址附加非零 scope_id 会报 10049。
        # 绑定到该网卡独有的源地址即等价于锁定出接口。
        s.bind((validate_bind_ipv6(bind_ipv6), 0))
    s.connect((ip, port, 0, 0))
    return s


def _connect_through_proxy(target_host: str, target_port: int,
                           proxy: ProxyConfig, cfg: NetConfig,
                           timeout: float) -> socket.socket:
    addrs = resolve_aaaa(proxy.host, proxy.port, cfg.pins.get(proxy.host.lower()))
    last: Optional[Exception] = None
    for ip, port in addrs:
        try:
            s = _direct(ip, port, timeout, cfg.bind_ipv6)
            if proxy.scheme == "socks5":
                methods = b"\x05\x02\x00\x02" if proxy.user else b"\x05\x01\x00"
                s.sendall(methods)
                r = _recv_exact(s, 2)
                if r[0] != 5:
                    raise ConnectionError("not a SOCKS5 proxy")
                if r[1] == 0x02:
                    u = (proxy.user or "").encode()
                    p = (proxy.password or "").encode()
                    s.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
                    if _recv_exact(s, 2)[1] != 0:
                        raise ConnectionError("SOCKS5 authentication failed")
                elif r[1] != 0:
                    raise ConnectionError(f"SOCKS5 method rejected: {r[1]}")
                hb = target_host.encode("idna")
                s.sendall(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb
                          + struct.pack("!H", target_port))
                hdr = _recv_exact(s, 4)
                if hdr[1] != 0:
                    raise ConnectionError(f"SOCKS5 CONNECT failed: reply={hdr[1]}")
                atyp = hdr[3]
                if atyp == 1:
                    _recv_exact(s, 6)
                elif atyp == 3:
                    n = _recv_exact(s, 1)[0]
                    _recv_exact(s, n + 2)
                elif atyp == 4:
                    _recv_exact(s, 18)
                else:
                    raise ConnectionError(f"unknown SOCKS5 ATYP {atyp}")
            else:  # HTTP CONNECT
                auth = ""
                if proxy.user:
                    tok = base64.b64encode(
                        f"{proxy.user}:{proxy.password or ''}".encode()
                    ).decode()
                    auth = f"Proxy-Authorization: Basic {tok}\r\n"
                req = (f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
                       f"Host: {target_host}:{target_port}\r\n{auth}"
                       f"Proxy-Connection: close\r\n\r\n")
                s.sendall(req.encode())
                buf = b""
                while b"\r\n\r\n" not in buf:
                    d = s.recv(4096)
                    if not d:
                        raise ConnectionError("proxy closed during CONNECT")
                    buf += d
                first = buf.split(b"\r\n", 1)[0]
                if b" 200" not in first:
                    raise ConnectionError(f"proxy CONNECT failed: {first!r}")
            return s
        except Exception as e:  # 换下一个代理 AAAA
            last = e
            try:
                s.close()
            except Exception:
                pass
    raise last or ConnectionError("proxy unreachable")


def _default_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False   # SNI 是变体，主机名在 wrap 之后手动校验
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _verify_peer(tls: ssl.SSLSocket, host: str) -> None:
    cert = tls.getpeercert()
    if not cert:
        raise ssl.SSLError("server did not present a certificate")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            ssl.match_hostname(cert, host)   # type: ignore[attr-defined]
            return
        except (ssl.CertificateError, AttributeError):
            pass
    # 手工比对 SAN（Python 未来版本移除 match_hostname 时的兜底）
    names = [v for k, v in cert.get("subjectAltName", ()) if k.lower() in ("dns", "ip address")]
    h = host.lower().rstrip(".")
    for n in names:
        n = n.lower().rstrip(".")
        if n == h or (n.startswith("*.") and h.endswith(n[1:])):
            return
    raise ssl.SSLError(f"certificate does not match host {host!r}")


class V6HTTPSConnection(http.client.HTTPSConnection):
    """只走 AF_INET6 的 HTTPS 连接；Host 头仍使用真实域名。"""

    def __init__(self, host: str, port: int = 443, *, cfg: NetConfig,
                 sni: str, timeout: float):
        super().__init__(host, port, timeout=timeout, context=_default_context())
        self._cfg = cfg
        self._sni = sni
        self._v6_timeout = timeout

    def connect(self) -> None:  # noqa: D102
        addrs = resolve_aaaa(self.host, self.port,
                             self._cfg.pins.get(self.host.lower()))
        last: Optional[Exception] = None
        for ip, port in addrs:
            raw: Optional[socket.socket] = None
            try:
                if self._cfg.proxy:
                    raw = _connect_through_proxy(
                        self.host, self.port, self._cfg.proxy,
                        self._cfg, self._v6_timeout)
                else:
                    raw = _direct(ip, port, self._v6_timeout, self._cfg.bind_ipv6)
                self.sock = self._context.wrap_socket(raw, server_hostname=self._sni)
                if self._cfg.verify_hostname:
                    _verify_peer(self.sock, self.host)
                return
            except Exception as e:
                last = e
                if raw is not None:
                    try:
                        raw.close()
                    except Exception:
                        pass
        raise last or ConnectionError(f"no reachable IPv6 address for {self.host}")


# --------------------------------------------------------------------------
# 高层请求
# --------------------------------------------------------------------------

@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str

    def json(self):
        import json
        return json.loads(self.body.decode("utf-8", "replace"))

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


_RETRYABLE = (ConnectionResetError, TimeoutError, ConnectionError,
              ssl.SSLError, socket.timeout, OSError)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, IPv4Forbidden):
        return False
    return isinstance(exc, _RETRYABLE)


def request(url: str, method: str = "GET", *, cfg: NetConfig,
            headers: Optional[dict] = None, body: Optional[bytes] = None,
            timeout: Optional[float] = None,
            retries: Optional[int] = None,
            follow_redirects: bool = True,
            max_redirects: int = 5,
            stream: bool = False,
            _server_retry: int = 1):
    """
    发起一个纯 IPv6 的 HTTPS 请求。
    stream=True 时返回 (conn, http.client.HTTPResponse)，调用方负责 conn.close()。
    """
    tmo = timeout if timeout is not None else cfg.timeout
    tries = retries if retries is not None else cfg.retries
    redirects = 0

    while True:
        u = urllib.parse.urlsplit(url)
        if u.scheme != "https":
            raise ValueError(f"only https is supported (got {u.scheme!r})")
        host = u.hostname or ""
        port = u.port or 443
        path = u.path or "/"
        if u.query:
            path += "?" + u.query

        hdrs = {
            "User-Agent": cfg.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "identity",   # 避免 gzip，简化处理
            "Connection": "close",
        }
        if headers:
            hdrs.update(headers)
        hdrs.setdefault("Host", u.netloc.split("@")[-1])

        snis = sni_variants(host, cfg)
        last: Optional[BaseException] = None
        conn: Optional[V6HTTPSConnection] = None
        resp = None
        for attempt in range(max(1, tries)):
            # 失败后轮换 SNI 变体，自动适配不同网络环境
            sni = snis[min(attempt, len(snis) - 1)]
            try:
                conn = V6HTTPSConnection(host, port, cfg=cfg, sni=sni,
                                         timeout=tmo)
                conn.request(method, path, body=body, headers=hdrs)
                resp = conn.getresponse()
                break
            except BaseException as e:
                last = e
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                conn = None
                if not _is_retryable(e) or attempt == max(1, tries) - 1:
                    raise
                time.sleep(0.4 * (attempt + 1))
        if conn is None or resp is None:
            raise last or ConnectionError(f"request failed: {url}")

        status = resp.status
        rh = {k.lower(): v for k, v in resp.getheaders()}

        if follow_redirects and status in (301, 302, 303, 307, 308) and redirects < max_redirects:
            loc = rh.get("location")
            conn.close()
            if not loc:
                raise ConnectionError(f"{status} without Location: {url}")
            url = urllib.parse.urljoin(url, loc)
            redirects += 1
            if status in (301, 302, 303) and method not in ("GET", "HEAD"):
                method, body = "GET", None
            continue

        if status >= 500 or status == 429:
            conn.close()
            if _server_retry > 0:
                time.sleep(0.6)
                return request(url, method, cfg=cfg, headers=headers, body=body,
                               timeout=tmo, retries=1,
                               follow_redirects=follow_redirects,
                               max_redirects=max_redirects, stream=stream,
                               _server_retry=0)

        if stream:
            return conn, resp
        data = resp.read()
        conn.close()
        return Response(status, rh, data, url)


def download_stream(url: str, *, cfg: NetConfig, headers: Optional[dict] = None,
                    timeout: Optional[float] = None):
    """返回 (conn, resp, status, headers)，供分块读取大文件。"""
    conn, resp = request(url, "GET", cfg=cfg, headers=headers,
                         timeout=timeout, stream=True)
    return conn, resp, resp.status, {k.lower(): v for k, v in resp.getheaders()}


def probe_egress(cfg: NetConfig, target_ip: str, port: int = 443,
                 timeout: Optional[float] = None) -> tuple[str, int]:
    """建立一次真实 TCP 连接并返回 (源 IPv6, 源端口)，用于验证绑定确实生效。"""
    tmo = timeout if timeout is not None else min(cfg.timeout, 10.0)
    s = _direct(target_ip, port, tmo, cfg.bind_ipv6)
    try:
        local = s.getsockname()
        return str(local[0]), int(local[1])
    finally:
        s.close()


def egress_trace(cfg: NetConfig, timeout: Optional[float] = None) -> dict[str, str]:
    """
    通过 Cloudflare 的 trace 端点读取「公网出口 IPv6」。
    用途：自查下载流量最终从哪个 IPv6 地址出去（判断是否为免流通道）。
    """
    r = request("https://www.cloudflare.com/cdn-cgi/trace", cfg=cfg,
                timeout=timeout or min(cfg.timeout, 12.0), retries=2)
    if not r.ok:
        raise ConnectionError(f"trace endpoint returned HTTP {r.status}")
    out: dict[str, str] = {}
    for line in r.body.decode("utf-8", "replace").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out
