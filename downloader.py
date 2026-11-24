"""
downloader.py - asmr.one 严格 IPv6-only 下载器

参考 fireinrain/asmr-downloader v2 的接口流程与功能设计，但网络层硬性只走 IPv6。

用法:
  python downloader.py check
  python downloader.py search "护士,-中出@duration:1h?order=dl_count&sort=desc" -c 10
  python downloader.py works -c 10
  python downloader.py list RJ01426915 -d
  python downloader.py download RJ01426915 -o ./downloads
  python downloader.py download hot -n 5 -o ./downloads
  python downloader.py export RJ01426915 -o ./export

所有连接均为 AF_INET6；进程内 socket.getaddrinfo 被拦截，IPv4 请求会直接报错。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

import asmr_client as api
import ipv6_net as net

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = {
    "api_base": "https://api.asmr.one",
    "account": "guest",
    "password": "guest",
    "download_dir": os.path.join(HERE, "downloads"),
    "workers": 4,
    "qps": 1.0,
    "retries": 3,
    "timeout": 30.0,
    "prefer_media": "all",
    "include_ext": "",
    "exclude_ext": "",
    "include_keyword": "",
    "exclude_keyword": "",
    "force": False,
    "proxy": "",
    "bind_ipv6": "",          # 出栈源 IPv6 地址（只可能是 IPv6）
    "bind_interface": "",     # 网卡名或 ifIndex，如 "以太网 4" / 27
    "pins": {},
    "sni_policy": "auto",
    "verify_hostname": True,
}

AUDIO_EXTS = {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg",
              ".wma", ".opus", ".aiff", ".ape", ".mp4"}
ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3",
                "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
                "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6",
                "LPT7", "LPT8", "LPT9"}


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}PB"


def sanitize(name: str, max_bytes: int = 180) -> str:
    """清理非法字符并按 UTF-8 字节截断（单名上限 255 字节）。"""
    name = ILLEGAL.sub("_", str(name)).strip().rstrip(" .")
    if not name:
        return "untitled"
    if name.split(".")[0].upper() in WIN_RESERVED:
        name = "_" + name
    if len(name.encode("utf-8")) <= max_bytes:
        return name
    stem, ext = os.path.splitext(name)
    if len(ext.encode("utf-8")) > 20:
        stem, ext = name, ""
    digest = hashlib.md5(name.encode("utf-8")).hexdigest()[:6]
    budget = max_bytes - len(ext.encode("utf-8")) - len(digest) - 1
    raw = stem.encode("utf-8")[:max(16, budget)]
    stem = raw.decode("utf-8", "ignore").rstrip(" .")
    return f"{stem or 'file'}-{digest}{ext}"


def safe_relpath(rel_dir: str, name: str) -> str:
    parts = [sanitize(p) for p in rel_dir.split("/") if p.strip()]
    parts.append(sanitize(name))
    return os.path.join(*parts) if parts else "untitled"


def win_long(path: str) -> str:
    """Windows 长路径支持（>260 字符）。"""
    p = os.path.abspath(path)
    if os.name == "nt" and not p.startswith("\\\\?\\"):
        return "\\\\?\\" + p.replace("/", "\\")
    return p


_print_lock = threading.Lock()


def say(*a) -> None:
    with _print_lock:
        print(*a, flush=True)


class RateLimiter:
    def __init__(self, qps: float):
        self.interval = 1.0 / qps if qps and qps > 0 else 0.0
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            target = max(now, self._next)
            self._next = target + self.interval
        delay = target - time.monotonic()
        if delay > 0:
            time.sleep(delay)


@dataclass
class Settings:
    cfg: dict[str, Any]
    netcfg: net.NetConfig

    def __getitem__(self, k):
        return self.cfg[k]


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

def load_settings(args) -> Settings:
    cfg = dict(DEFAULT_CONFIG)
    if getattr(args, "config", None):
        with open(args.config, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))

    overrides = {
        "api_base": "api_base", "account": "account", "password": "password",
        "download_dir": "dir", "workers": "workers", "qps": "qps",
        "retries": "retries", "timeout": "timeout",
        "prefer_media": "prefer_media", "include_ext": "include_ext",
        "exclude_ext": "exclude_ext", "include_keyword": "include_keyword",
        "exclude_keyword": "exclude_keyword", "force": "force",
        "proxy": "proxy", "sni_policy": "sni_policy",
        "bind_ipv6": "bind_ipv6", "bind_interface": "bind_interface",
    }
    for key, attr in overrides.items():
        v = getattr(args, attr, None)
        if v is not None:
            cfg[key] = v

    # 解析出栈网卡绑定：可指定网卡名/ifIndex（自动发现其当前 IPv6 源地址），
    # 或直接给一个 IPv6 字面量。IPv4 字面量在 validate_bind_ipv6 里会被直接拒绝。
    bind_iface = str(cfg.get("bind_interface") or "").strip()
    bind_addr = str(cfg.get("bind_ipv6") or "").strip()
    cfg["bind_interface_label"] = ""
    if bind_iface:
        discovered, label = net.discover_bind_ipv6(bind_iface)
        cfg["bind_interface_label"] = label
        if bind_addr:
            say(f"提示: 同时指定 bind_interface 与 bind_ipv6，以 bind_ipv6 为准"
                f"（忽略 {label} 当前地址 {discovered}）")
        else:
            bind_addr = discovered
    if bind_addr:
        bind_addr = net.validate_bind_ipv6(bind_addr)
    cfg["bind_ipv6"] = bind_addr
    cfg["bind_interface"] = bind_iface

    pins = net.parse_pins(
        [f"{k}={v}" for k, v in (cfg.get("pins") or {}).items()]
    )
    if getattr(args, "pin", None):
        pins.update(net.parse_pins(args.pin))
    cfg["pins"] = pins

    proxy = net.parse_proxy(cfg.get("proxy") or "")
    netcfg = net.NetConfig(
        pins=pins,
        proxy=proxy,
        bind_ipv6=bind_addr or None,
        sni_policy=cfg.get("sni_policy", "auto"),
        verify_hostname=bool(cfg.get("verify_hostname", True)),
        timeout=float(cfg["timeout"]),
        retries=int(cfg["retries"]),
    )
    return Settings(cfg, netcfg)


def make_client(st: Settings) -> api.AsmrClient:
    c = api.AsmrClient(st.netcfg, api_base=st["api_base"],
                       account=st["account"], password=st["password"])
    c.login()
    return c


def parse_ids(client: api.AsmrClient, raw: str, count: int = 1) -> list[str]:
    """支持 'RJxxx,RJyyy'、'hot'/'hot100'、以及裸数字。"""
    raw = raw.strip()
    if raw.lower() in ("hot", "hot100", "popular"):
        data = client.works(page=1, page_size=min(count, 100),
                            order="dl_count", sort="desc")
        ids = []
        for w in data.get("works", []):
            sid = w.get("source_id") or w.get("original_workno")
            if sid:
                ids.append(str(sid))
        return ids[:count]
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            prefix, num = api.AsmrClient.normalize_id(part)
            out.append(prefix + num)
        except ValueError:
            out.append(part)   # 允许直接传数字 ID
    return out


def id_to_num(wid: str) -> str:
    if re.fullmatch(r"(?i)(RJ|VJ|BJ|AJ|CJ|DL|NP|AL|KN)\d+", wid):
        return api.AsmrClient.normalize_id(wid)[1]
    return wid


def work_folder_name(wid: str, info: dict) -> str:
    rjid = wid.upper()
    date = sanitize(str(info.get("release") or "unknown"), 20)
    subtitle = "subtitle" if info.get("has_subtitle") else "nosub"
    title = sanitize(str(info.get("title") or info.get("name") or "untitled"), 80)
    return f"{rjid}-{date}-{subtitle}-{title}"


# --------------------------------------------------------------------------
# 过滤
# --------------------------------------------------------------------------

def apply_filters(entries: list[api.FileEntry], st: Settings) -> list[api.FileEntry]:
    cfg = st.cfg
    prefer = str(cfg.get("prefer_media") or "all").strip().lower()
    if prefer and prefer != "all":
        audio = [e for e in entries if os.path.splitext(e.name)[1].lower() in AUDIO_EXTS]
        other = [e for e in entries if e not in audio]
        chosen = ""
        for rule in (r.strip().lower().lstrip(".") for r in prefer.split(">")):
            if not rule:
                continue
            if any(os.path.splitext(e.name)[1].lower() == "." + rule for e in audio):
                chosen = "." + rule
                break
        entries = ([e for e in audio if os.path.splitext(e.name)[1].lower() == chosen]
                   if chosen else other)

    def ext_set(raw: str) -> set[str]:
        return {"." + x.strip().lower().lstrip(".")
                for x in (raw or "").split(",") if x.strip()}

    inc, exc = ext_set(cfg.get("include_ext", "")), ext_set(cfg.get("exclude_ext", ""))
    if inc:
        entries = [e for e in entries
                   if any(e.name.lower().endswith(x) for x in inc)]
    if exc:
        entries = [e for e in entries
                   if not any(e.name.lower().endswith(x) for x in exc)]

    def kw_list(raw: str) -> list[str]:
        return [x.strip().lower() for x in (raw or "").split(",") if x.strip()]

    inc_kw, exc_kw = kw_list(cfg.get("include_keyword", "")), kw_list(cfg.get("exclude_keyword", ""))
    if inc_kw:
        entries = [e for e in entries
                   if any(k in (e.rel_dir + "/" + e.name).lower() for k in inc_kw)]
    if exc_kw:
        entries = [e for e in entries
                   if not any(k in (e.rel_dir + "/" + e.name).lower() for k in exc_kw)]
    return entries


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------

def download_file(e: api.FileEntry, dest: str, st: Settings, token: str,
                  limiter: RateLimiter) -> tuple[str, str, int]:
    cfg = st.cfg
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    force = bool(cfg.get("force"))
    local = os.path.getsize(win_long(dest)) if os.path.exists(dest) else 0

    if not force and local > 0 and (e.size <= 0 or local == e.size):
        return ("skip", dest, local)

    headers = {
        "Referer": "https://asmr.one/",
        "Origin": "https://asmr.one",
    }
    if token:
        headers["Authorization"] = "Bearer " + token

    retries = max(1, int(cfg["retries"]))
    last: Optional[BaseException] = None
    for attempt in range(retries):
        offset = os.path.getsize(win_long(dest)) if os.path.exists(dest) else 0
        if force and attempt == 0:
            offset = 0
        if offset > 0:
            headers["Range"] = f"bytes={offset}-"
        else:
            headers.pop("Range", None)
        limiter.wait()
        conn = None
        try:
            conn, resp, status, rh = net.download_stream(
                e.url, cfg=st.netcfg, headers=headers,
                timeout=float(cfg["timeout"]))
            if status not in (200, 206):
                conn.close()
                raise RuntimeError(f"HTTP {status} for {e.name}")
            if status == 200:
                offset = 0
            elif offset > 0:
                say(f"    -> 206 断点续传，从 {human(offset)} 继续")
            mode = "ab" if (status == 206 and offset > 0) else "wb"
            written = 0
            with open(win_long(dest), mode) as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
            conn.close()
            total = os.path.getsize(win_long(dest))
            if e.size > 0 and total != e.size:
                raise RuntimeError(f"incomplete: {total}/{e.size} bytes")
            return ("ok", dest, total)
        except BaseException as ex:
            last = ex
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            if isinstance(ex, net.IPv4Forbidden):
                raise
            if attempt < retries - 1:
                time.sleep(0.6 * (2 ** attempt))
    raise RuntimeError(f"failed after {retries} attempts: {last}")


def download_work(client: api.AsmrClient, wid: str, st: Settings,
                  limiter: RateLimiter, *, dry_run: bool = False,
                  limit: int = 0) -> dict[str, Any]:
    cfg = st.cfg
    num = id_to_num(wid)
    info = client.work_info(num)
    folder = work_folder_name(wid, info)
    base = os.path.join(cfg["download_dir"], folder)
    entries = apply_filters(api.AsmrClient.flatten(client.tracks(num)), st)
    if limit > 0:
        entries = entries[:limit]

    say(f"\n[{wid}] {info.get('title', '')}")
    say(f"  目录: {folder}")
    say(f"  待处理文件: {len(entries)}  预计总大小: "
        f"{human(sum(e.size for e in entries if e.size > 0))}")
    if dry_run:
        for e in entries[:20]:
            say(f"    - {safe_relpath(e.rel_dir, e.name)}  ({human(e.size)})")
        if len(entries) > 20:
            say(f"    ... 其余 {len(entries)-20} 个省略")
        return {"ok": 0, "skip": 0, "fail": 0, "dry": len(entries)}

    tasks = [(e, os.path.join(base, safe_relpath(e.rel_dir, e.name)))
             for e in entries]
    results: dict[str, int] = {"ok": 0, "skip": 0, "fail": 0}
    errors: list[str] = []
    counter = {"n": 0}
    lock = threading.Lock()

    def run(item):
        e, dest = item
        try:
            status, _, size = download_file(e, dest, st, client.token or "", limiter)
            results[status] += 1
            with lock:
                counter["n"] += 1
                say(f"  [{counter['n']}/{len(tasks)}] {status:4} "
                    f"{human(size):>9}  {os.path.relpath(dest, base)}")
        except Exception as ex:
            results["fail"] += 1
            errors.append(f"{dest}\t{ex}")
            with lock:
                counter["n"] += 1
                say(f"  [{counter['n']}/{len(tasks)}] FAIL  {e.name}: {ex}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=int(cfg["workers"])) as pool:
        list(pool.map(run, tasks))

    if errors:
        log = os.path.join(cfg["download_dir"], "download_errors.log")
        os.makedirs(cfg["download_dir"], exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            for line in errors:
                f.write(line + "\n")
    return results


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------

def classify_egress(ip: str) -> str:
    """根据公网出口 IPv6 前缀给出计费口径提示（仅供参考，非权威判定）。"""
    if not ip:
        return "未取到出口地址"
    low = ip.lower()
    if low.startswith("2001:da8"):
        return ("CERNET2 教育网 IPv6 (2001:da8::/32) —— 多数高校对 IPv6 不计费，"
                "但国际方向是否免费需向校方确认")
    if low.startswith(("2001:250", "2001:251")):
        return "CERNET 教育网 IPv6 —— 通常属免流通道，仍需确认计费口径"
    if low.startswith(("240e", "2408", "2409", "240a")):
        return "基础运营商 IPv6（电信/联通/移动）—— 按宽带套餐计费，一般不属于免流"
    if low.startswith(("fc", "fd")):
        return "ULA 私有地址（NAT66 未按预期转换？）—— 请检查路由器配置"
    return "其他公网 IPv6 段，需向网络管理员确认该段是否计费"


def cmd_egress(st: Settings) -> int:
    say("=== 出口路径 / 免流自查 ===")
    say("说明: 是否免流由学校/运营商的计费系统决定，本工具只能保证「下载流量不承载在 IPv4 上」。")
    try:
        tr = net.egress_trace(st.netcfg)
        ip = tr.get("ip", "")
        say(f"公网出口 IPv6 : {ip or '(未返回)'}")
        say(f"Cloudflare 节点: {tr.get('colo', '?')}   协议: {tr.get('visit_scheme', '?')}")
        say(f"地址段判断    : {classify_egress(ip)}")
    except Exception as e:
        say(f"!! 出口探测失败: {type(e).__name__}: {e}")
        return 1
    try:
        servers = net.dns_servers()
        v4 = [s for s in servers if ":" not in s]
        say(f"本机 DNS       : {', '.join(servers) or '未读到'}")
        if v4:
            say(f"注意: 其中 {len(v4)} 个为 IPv4，系统 DNS 解析会产生少量 IPv4 报文"
                "（非下载流量，量极小）。")
            say("      若要彻底消除，用 --pin 固定所有域名，或配置 IPv6 DNS 服务器。")
    except Exception as e:
        say(f"DNS 信息读取失败: {e}")
    say("自查建议: 下载前后对比计费系统 / 路由器里的 IPv4 与 IPv6 流量计数。")
    return 0


def cmd_check(st: Settings, args) -> int:
    if getattr(args, "egress", False):
        return cmd_egress(st)
    say("=== 严格 IPv6 环境检查 ===")

    # 1) 本机网卡 IPv6 概览
    try:
        ifaces = [i for i in net.interfaces_ipv6() if i.addresses or i.temporary]
        say("本机网卡 IPv6 概览:")
        for i in ifaces:
            say(f"  {i.label:34} {', '.join(i.temporary + i.addresses)}")
    except Exception as e:
        say(f"!! 网卡枚举失败: {e}")
    if getattr(args, "interfaces", False):
        return 0

    # 2) 实测 IPv4 护栏（用户最关心的硬保证）
    try:
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        say("!! IPv4 护栏失效：AF_INET socket 竟然可以创建")
        return 1
    except net.IPv4Forbidden:
        say("IPv4 护栏: 已启用（实测 AF_INET socket 被拒绝，进程内不可能有 IPv4 出站）")

    # 3) 系统默认出栈地址（未绑定时的参考值）
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        s.connect(("2001:4860:4860::8888", 80))
        say(f"系统默认 IPv6 出栈地址: {s.getsockname()[0]}")
        s.close()
    except Exception as e:
        say(f"!! 无法获取 IPv6 出栈地址: {e}")
        return 1

    # 4) 出栈网卡绑定 + 真实 TCP 源地址验证
    label = (st.cfg.get("bind_interface_label") or st.cfg.get("bind_interface") or "")
    bind = st.netcfg.bind_ipv6
    if bind:
        say(f"锁定网卡: {label or '(按源地址指定)'}")
        say(f"绑定源 IPv6: {bind}")
    else:
        say("锁定网卡: 未指定，由系统 IPv6 路由自动选择")
    api_host = st["api_base"].split("//")[-1].split("/")[0]
    try:
        target = net.resolve_aaaa(api_host, 443,
                                  st["pins"].get(api_host.lower()))[0][0]
        src, sport = net.probe_egress(st.netcfg, target, timeout=10)
        say(f"真实 TCP 出栈源地址: {src}:{sport}  (目标 {target})")
        if bind:
            if src.lower() == bind.lower():
                say("绑定生效: 实际源地址 == 绑定地址，流量确实走所选网卡")
            else:
                say(f"!! 绑定未生效: 期望 {bind}，实际 {src}")
                return 1
    except Exception as e:
        say(f"!! 出栈 TCP 探测失败: {type(e).__name__}: {e}")
        return 1

    hosts = [api_host, "asmr.one", "raw.kiko-play-niptan.one"]
    for h in dict.fromkeys(hosts):
        try:
            addrs = net.resolve_aaaa(h, 443, st["pins"].get(h.lower()))
            say(f"AAAA {h:28} -> {', '.join(a for a, _ in addrs[:3])}")
        except Exception as e:
            say(f"AAAA {h:28} -> 失败: {e}")

    say(f"SNI 策略: {st.netcfg.sni_policy} "
        f"(需变体的域名: {', '.join(st.netcfg.bypass_hosts[:3])} ...)")
    say(f"代理: {st.cfg.get('proxy') or '无（直连）'}")

    try:
        c = make_client(st)
        say("API 登录: OK (JWT 已获取)")
        data = c.works(page=1, page_size=1)
        say(f"API /api/works: OK, 站点作品总数分页信息 = {data.get('pagination')}")
        tr = c.tracks("01426915")
        entries = api.AsmrClient.flatten(tr)
        if entries:
            from urllib.parse import urlsplit
            mh = urlsplit(entries[0].url).hostname or ""
            say(f"媒体域名: {mh}")
            try:
                say(f"  AAAA -> {', '.join(a for a, _ in net.resolve_aaaa(mh, 443)[:2])}")
            except Exception as e:
                say(f"  AAAA 失败: {e}")
    except Exception as e:
        say(f"!! API 检查失败: {type(e).__name__}: {e}")
        return 1
    say("=== 检查完成：全链路可走纯 IPv6 ===")
    return 0


def cmd_search(st: Settings, args) -> int:
    import urllib.parse
    c = make_client(st)
    total = max(args.count, 1)
    page_size = min(total, 100)
    got, page = 0, 1
    say(f"搜索: {args.expr}")
    while got < total:
        main, _, qs = args.expr.partition("?")
        params: dict[str, str] = {}
        for item in (x for x in qs.split("&") if "=" in x):
            k, v = item.split("=", 1)
            params[k] = v
        params["page"] = str(page)
        params.setdefault("pageSize", str(page_size))
        data = c.search(main + "?" + urllib.parse.urlencode(params))
        works = data.get("works", [])
        if not works:
            break
        batch = works[: total - got]
        for w in batch:
            say(f"  {w.get('source_id', ''):12} {str(w.get('title', ''))[:60]}")
        got += len(batch)
        page += 1
        if len(works) < page_size:
            break
    say(f"共 {got} 条")
    return 0


def cmd_works(st: Settings, args) -> int:
    c = make_client(st)
    data = c.works(page=args.page, page_size=args.count,
                   order=args.order, sort="desc")
    for w in data.get("works", []):
        say(f"  {w.get('source_id', ''):12} "
            f"{str(w.get('title', ''))[:60]}")
    say(f"共 {len(data.get('works', []))} 条")
    return 0


def cmd_list(st: Settings, args) -> int:
    c = make_client(st)
    for wid in parse_ids(c, args.ids):
        num = id_to_num(wid)
        entries = api.AsmrClient.flatten(c.tracks(num))
        say(f"\n[{wid}] 共 {len(entries)} 个文件")
        ext_count: dict[str, int] = {}
        for e in entries:
            ext = os.path.splitext(e.name)[1].lower() or "(无扩展名)"
            ext_count[ext] = ext_count.get(ext, 0) + 1
        for ext, n in sorted(ext_count.items(), key=lambda x: -x[1]):
            say(f"  {ext:12} {n}")
        if args.detail:
            for e in entries:
                say(f"    {safe_relpath(e.rel_dir, e.name)}  {human(e.size)}")
    return 0


def cmd_download(st: Settings, args) -> int:
    c = make_client(st)
    ids = parse_ids(c, args.ids, args.number)
    if not ids:
        say("没有可下载的作品 ID")
        return 1
    limiter = RateLimiter(float(st["qps"]))
    totals = {"ok": 0, "skip": 0, "fail": 0, "dry": 0}
    for wid in ids:
        try:
            r = download_work(c, wid, st, limiter,
                              dry_run=args.dry_run, limit=args.limit)
            for k in totals:
                totals[k] += r.get(k, 0)
        except Exception as e:
            say(f"!! [{wid}] 失败: {type(e).__name__}: {e}")
            totals["fail"] += 1
    say(f"\n=== 汇总: 新下载 {totals['ok']}, 跳过 {totals['skip']}, "
        f"失败 {totals['fail']}"
        + (f", dry-run 列出 {totals['dry']}" if args.dry_run else "") + " ===")
    say(f"保存目录: {st['download_dir']}")
    return 1 if totals["fail"] else 0


def cmd_export(st: Settings, args) -> int:
    c = make_client(st)
    out = args.dir or os.path.join(st["download_dir"], "export")
    for wid in parse_ids(c, args.ids, args.number):
        num = id_to_num(wid)
        info = c.work_info(num)
        folder = work_folder_name(wid, info)
        d = os.path.join(out, folder)
        os.makedirs(d, exist_ok=True)
        entries = api.AsmrClient.flatten(c.tracks(num))
        links = os.path.join(d, "links.txt")
        with open(links, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(e.url + "\n")
        script = os.path.join(d, "aria2_download.sh")
        with open(script, "w", encoding="utf-8", newline="\n") as f:
            f.write("#!/usr/bin/env bash\n"
                    "# 注意: aria2 无法硬性禁用 IPv4。\n"
                    "# 严格要求纯 IPv6 时请改用: python downloader.py download " + wid + "\n"
                    "cd \"$(dirname \"$0\")\" || exit 1\n"
                    "aria2c --disable-ipv6=false --async-dns=true \\\n"
                    "       --max-connection-per-server=4 -j 4 \\\n"
                    "       --input-file=links.txt --dir=.\n")
        say(f"[{wid}] 导出 {len(entries)} 个链接 -> {d}")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="asmr.one 严格 IPv6-only 下载器（参考 fireinrain/asmr-downloader）")
    p.add_argument("--config", help="JSON 配置文件路径")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--api-base", help="API 基址，默认 https://api.asmr.one")
    common.add_argument("--account", help="登录账号，默认 guest")
    common.add_argument("--password", help="登录密码，默认 guest")
    common.add_argument("-o", "--dir", dest="dir", help="下载/输出目录")
    common.add_argument("--workers", type=int, help="并发数，默认 4")
    common.add_argument("--qps", type=float, help="每秒请求上限，默认 1.0，0 表示不限")
    common.add_argument("--retries", type=int, help="单文件失败重试次数，默认 3")
    common.add_argument("--timeout", type=float, help="连接/读取超时秒数，默认 30")
    common.add_argument("--proxy", help="IPv6 代理: http://[::1]:8080 或 socks5://[2001:db8::1]:1080")
    common.add_argument("--bind-interface", metavar="IFACE",
                        help="锁定出栈网卡：接口名或 ifIndex，如 '以太网 4' 或 27")
    common.add_argument("--bind-ipv6", metavar="IPv6",
                        help="锁定出栈源 IPv6 地址（IPv4 一律拒绝）；优先于 --bind-interface")
    common.add_argument("--pin", action="append", metavar="HOST=IPv6",
                        help="手动固定域名的 IPv6 地址（可重复，绕开被污染的 DNS）")
    common.add_argument("--sni-policy", choices=["auto", "always", "never"],
                        help="SNI 变体策略，默认 auto（仅对 asmr 域名启用）")
    common.add_argument("--prefer-media", help="音频格式优先级，如 mp3>wav>flac，或 all")
    common.add_argument("--include-ext", help="扩展名白名单，如 .mp3,.png")
    common.add_argument("--exclude-ext", help="扩展名黑名单，如 .mp4,.webm")
    common.add_argument("--include-keyword", help="路径关键词白名单，逗号分隔")
    common.add_argument("--exclude-keyword", help="路径关键词黑名单，逗号分隔")
    common.add_argument("-f", "--force", action="store_true", default=None,
                        help="强制覆盖已存在文件")
    # 全局选项只挂在子解析器上（避免 argparse 子命名空间默认值覆盖根解析结果）。
    # 因此选项请写在子命令之后，例如: downloader.py download RJxxx --workers 8
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("check", parents=[common], help="诊断 IPv6 环境与 API 连通性")
    s.add_argument("--interfaces", action="store_true",
                   help="只列出本机网卡的非 link-local IPv6 地址与 ifIndex 后退出")
    s.add_argument("--egress", action="store_true",
                   help="探测公网出口 IPv6、判断地址段并提示 DNS/IPv4 侧信道（免流自查）")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("search", parents=[common], help="搜索作品")
    s.add_argument("expr", help='查询串，如 "护士,-中出@duration:1h?order=dl_count"')
    s.add_argument("-c", "--count", type=int, default=10)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("works", parents=[common], help="列出作品")
    s.add_argument("-c", "--count", type=int, default=10)
    s.add_argument("--page", type=int, default=1)
    s.add_argument("--order", default="release",
                   choices=["release", "dl_count", "create_date", "rating", "price", "id", "nsfw"])
    s.set_defaults(func=cmd_works)

    s = sub.add_parser("list", parents=[common], help="查看作品音轨文件")
    s.add_argument("ids")
    s.add_argument("-d", "--detail", action="store_true")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("download", parents=[common], help="下载作品")
    s.add_argument("ids", help="RJ 号（逗号分隔）或 hot")
    s.add_argument("-n", "--number", type=int, default=10, help="hot 模式数量")
    s.add_argument("--limit", type=int, default=0, help="每个作品最多下载 N 个文件（0=不限）")
    s.add_argument("--dry-run", action="store_true", help="只列出待下载文件，不实际下载")
    s.set_defaults(func=cmd_download)

    s = sub.add_parser("export", parents=[common], help="导出下载链接")
    s.add_argument("ids")
    s.add_argument("-n", "--number", type=int, default=10)
    s.set_defaults(func=cmd_export)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    net.enable_strict_ipv6()      # 第一件事：封死 IPv4
    p = build_parser()
    args = p.parse_args(argv)
    if not hasattr(args, "number"):
        args.number = 10
    if not hasattr(args, "limit"):
        args.limit = 0
    if not hasattr(args, "dry_run"):
        args.dry_run = False
    if not hasattr(args, "pin"):
        args.pin = None
    try:
        st = load_settings(args)
    except Exception as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return 2
    try:
        return args.func(st, args)
    except KeyboardInterrupt:
        say("\n已中断")
        return 130
    except net.IPv4Forbidden as e:
        say(f"!! 检测到 IPv4 请求，已被阻止: {e}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
