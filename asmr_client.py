"""
asmr_client.py - asmr.one API 封装（参考 fireinrain/asmr-downloader v2）

接口路径与字段命名对照原项目 internal/consts/consts.go：
  /api/auth/me          登录取 JWT
  /api/work/<id>        作品信息
  /api/tracks/<id>      音轨文件树
  /api/search/<expr>    搜索
  /api/works            作品列表 / 元数据
  /api/recommender/popular  热门榜
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, Optional

from ipv6_net import NetConfig, Response, request

DEFAULT_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    "origin": "https://asmr.one",
    "referer": "https://asmr.one/",
    "cache-control": "no-cache",
}

FILTER_KEYS = {"tag", "circle", "va", "duration", "rate", "price", "sell", "age", "lang"}
PARAM_KEYS = {"order", "sort", "subtitle", "page", "pageSize", "includeTranslationWorks"}
DEFAULT_PARAMS = {
    "order": "release", "sort": "desc", "page": "1",
    "pageSize": "20", "subtitle": "0", "includeTranslationWorks": "true",
}


def build_search_path(expr: str) -> str:
    """
    把 asmr-downloader 风格的查询串转成 API 路径。

    语法: 关键词1,关键词2,@key:value,key:value?order=xx&page=2
    输出: /api/search/<urlencoded>?params
    关键词以空格连接（'-' 前缀为排除），过滤条件包成 $key:value$。
    """
    main, _, param_str = expr.partition("?")
    kw_part, sep, filter_part = main.partition("@")
    if not main.strip() and not filter_part.strip():
        raise ValueError("empty search expression")

    parts: list[str] = []
    for kw in (x.strip() for x in kw_part.split(",")):
        if kw:
            if any(c in kw for c in ("@", "?", ":")):
                raise ValueError(f"invalid keyword {kw!r}: reserved character")
            parts.append(kw)

    seen: set[str] = set()
    for f in (x.strip() for x in filter_part.split(",")):
        if not f:
            continue
        if ":" not in f:
            raise ValueError(f"filter must be key:value -> {f!r}")
        key_raw, value = f.split(":", 1)
        neg = key_raw.startswith("-")
        key = key_raw[1:] if neg else key_raw
        if key not in FILTER_KEYS:
            raise ValueError(f"unknown filter key {key!r}; allowed: {sorted(FILTER_KEYS)}")
        # 同一个 key 允许"包含 + 排除"各一条，例如 tag:A 与 -tag:B
        seen_key = ("-" if neg else "") + key
        if seen_key in seen:
            raise ValueError(f"duplicate filter key {seen_key!r}")
        seen.add(seen_key)
        parts.append(f"${'-' if neg else ''}{key}:{value}$")

    params = dict(DEFAULT_PARAMS)
    for item in (x.strip() for x in param_str.split("&")):
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"param must be key=value -> {item!r}")
        k, v = item.split("=", 1)
        if k not in PARAM_KEYS:
            raise ValueError(f"unknown param {k!r}; allowed: {sorted(PARAM_KEYS)}")
        params[k] = v

    # 原项目在 path 前后各留一个空格，服务端按空白切分 token
    text = " " + " ".join(parts) + " "
    encoded = urllib.parse.quote(text, safe="")
    query = urllib.parse.urlencode(params)
    return f"/api/search/{encoded}?{query}"


@dataclass
class FileEntry:
    url: str
    rel_dir: str
    name: str
    size: int = 0
    duration: float = 0.0


class AsmrClient:
    def __init__(self, net_cfg: NetConfig,
                 api_base: str = "https://api.asmr.one",
                 account: str = "guest", password: str = "guest"):
        self.net = net_cfg
        self.api_base = api_base.rstrip("/")
        self.account = account
        self.password = password
        self.token: Optional[str] = None

    # ---------------- 基础 ----------------
    def _call(self, path: str, method: str = "GET",
              body: Optional[bytes] = None, auth: bool = True,
              timeout: Optional[float] = None) -> Response:
        headers = dict(DEFAULT_HEADERS)
        if auth and self.token:
            headers["Authorization"] = "Bearer " + self.token
        if body is not None:
            headers["Content-Type"] = "application/json"
        r = request(self.api_base + path, method, cfg=self.net,
                    headers=headers, body=body, timeout=timeout)
        return r

    def login(self) -> str:
        import json
        payload = json.dumps({"name": self.account,
                              "password": self.password},
                             ensure_ascii=False).encode("utf-8")
        r = self._call("/api/auth/me", "POST", body=payload, auth=False)
        if not r.ok:
            raise RuntimeError(f"login failed HTTP {r.status}: {r.body[:300]!r}")
        data = r.json()
        token = data.get("token")
        if not token:
            raise RuntimeError(f"login response has no token: {data}")
        self.token = token
        return token

    def work_info(self, wid: str) -> dict[str, Any]:
        for path in (f"/api/work/{wid}", f"/api/workinfo/{wid}"):
            try:
                r = self._call(path)
                if r.ok:
                    return r.json()
            except Exception:
                continue
        raise RuntimeError(f"cannot fetch work info for {wid}")

    def tracks(self, wid: str) -> list[dict[str, Any]]:
        r = self._call(f"/api/tracks/{wid}")
        if not r.ok:
            raise RuntimeError(f"tracks HTTP {r.status}: {r.body[:200]!r}")
        return r.json()

    def search(self, expr: str) -> dict[str, Any]:
        r = self._call(build_search_path(expr))
        if not r.ok:
            raise RuntimeError(f"search HTTP {r.status}: {r.body[:200]!r}")
        return r.json()

    def works(self, *, page: int = 1, page_size: int = 20,
              order: str = "release", sort: str = "desc",
              subtitle: int = 0) -> dict[str, Any]:
        q = urllib.parse.urlencode({
            "order": order, "sort": sort, "page": page,
            "pageSize": page_size, "subtitle": subtitle,
        })
        r = self._call("/api/works?" + q)
        if not r.ok:
            raise RuntimeError(f"works HTTP {r.status}")
        return r.json()

    def popular(self) -> Any:
        r = self._call("/api/recommender/popular")
        return r.json() if r.ok else []

    # ---------------- 音轨树展开 ----------------
    @staticmethod
    def normalize_id(raw: str) -> tuple[str, str]:
        m = re.fullmatch(r"(?i)(RJ|VJ|BJ|AJ|CJ|DL|NP|AL|KN)(\d+)", raw.strip())
        if not m:
            raise ValueError(f"not a valid work id: {raw!r} (e.g. RJ01426915)")
        return m.group(1).upper(), m.group(2)

    @staticmethod
    def flatten(tracks: list[dict[str, Any]], base: str = "") -> list[FileEntry]:
        """递归展开 folder -> children，产出待下载文件列表。"""
        out: list[FileEntry] = []
        for t in tracks or []:
            title = str(t.get("title") or "")
            if t.get("type") == "folder" or t.get("children"):
                sub = f"{base}/{title}" if base else title
                out.extend(AsmrClient.flatten(t.get("children") or [], sub))
            else:
                url = (t.get("mediaDownloadUrl") or t.get("mediaDownloadURL")
                       or t.get("downloadUrl") or "")
                if not url:
                    continue
                try:
                    size = int(t.get("size") or 0)
                except (TypeError, ValueError):
                    size = 0
                try:
                    duration = float(t.get("duration") or 0)
                except (TypeError, ValueError):
                    duration = 0.0
                out.append(FileEntry(url=url, rel_dir=base, name=title,
                                     size=size, duration=duration))
        return out
