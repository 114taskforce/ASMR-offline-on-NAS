"""搜索 API — 调 asmr.one 搜索接口，同时把结果 upsert 到本地 SQLite"""
from __future__ import annotations

import json
import re
import sys
import os
import urllib.parse

from flask import Blueprint, current_app, jsonify, request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import ipv6_net as net
import asmr_client as api

search_bp = Blueprint("search", __name__)


def _make_client(cfg: dict):
    """创建客户端（每次请求独立初始化，线程安全）。

    不在这里调用 enable_strict_ipv6：它会全局替换 socket 类，影响整个 Flask 进程；
    出站请求本身已由 ipv6_net 强制走 AF_INET6，无需额外护栏。
    """
    pins = cfg.get("pins") or {}
    proxy = net.parse_proxy(cfg.get("proxy") or "") if cfg.get("proxy") else None
    cfg_net = net.NetConfig(
        pins=pins,
        proxy=proxy,
        sni_policy=cfg.get("sni_policy", "auto"),
        verify_hostname=bool(cfg.get("verify_hostname", True)),
        timeout=float(cfg.get("timeout", 30)),
        retries=int(cfg.get("retries", 3)),
    )
    return api.AsmrClient(cfg_net, account=cfg.get("account", "guest"),
                          password=cfg.get("password", "guest"))


def _names(items) -> list[str]:
    """把 [{'name': x}] / [x] / 单个 dict 统一成名字列表。"""
    if isinstance(items, dict):
        items = [items]
    out: list[str] = []
    for it in items or []:
        n = it.get("name") if isinstance(it, dict) else str(it)
        if n:
            out.append(n)
    return out


_ZH_ORDER = {"CHI_HANS": 0, "CHI": 1, "CHI_HANT": 2}


def _norm_lang(code, label="") -> tuple[str, str]:
    """把上游语言代码/标签归一成 (代码, 中文名)。"""
    c = str(code or "").strip()
    cu = c.upper()
    probe = cu + " " + str(label or "")
    if cu in ("CHI_HANS", "ZH-CN", "ZH_HANS", "CHS", "SC") or "简" in probe or "簡" in probe:
        return "CHI_HANS", "简体中文"
    if cu in ("CHI_HANT", "ZH-TW", "ZH_HANT", "CHT", "TC") or "繁" in probe:
        return "CHI_HANT", "繁体中文"
    if cu in ("CHI", "ZHO", "ZH", "CN") or "中文" in probe:
        return "CHI", "中文"
    if cu in ("JPN", "JA", "JP") or "日本語" in probe or "日语" in probe:
        return "JPN", "日语"
    if cu in ("ENG", "EN") or "英语" in probe or "英語" in probe:
        return "ENG", "英语"
    return cu, str(label or "")


def _is_zh(code: str) -> bool:
    return code in _ZH_ORDER


def _work_lang(raw: dict) -> tuple[str, str]:
    """作品自身语言：优先 translation_info，其次同 workno 的 language_editions。"""
    info = raw.get("translation_info") or {}
    if isinstance(info, dict) and info.get("lang"):
        return _norm_lang(info.get("lang"))
    source_id = str(raw.get("source_id") or raw.get("original_workno") or "")
    for ed in raw.get("language_editions") or []:
        if isinstance(ed, dict) and str(ed.get("workno") or "") == source_id:
            return _norm_lang(ed.get("lang"), ed.get("label") or "")
    title = str(raw.get("title") or raw.get("name") or "")
    if "简体中文版" in title:
        return "CHI_HANS", "简体中文"
    if "繁体中文版" in title:
        return "CHI_HANT", "繁体中文"
    if "中文版" in title:
        return "CHI", "中文"
    if isinstance(info, dict) and info.get("is_original"):
        return "JPN", "日语"
    return "", ""


def _work_editions(raw: dict, self_id: str) -> list[dict]:
    """可直接下载的其他语言版本（只认 other_language_editions_in_db）。"""
    out: list[dict] = []
    seen: set[str] = set()
    for it in raw.get("other_language_editions_in_db") or []:
        if not isinstance(it, dict):
            continue
        sid = str(it.get("source_id") or it.get("id") or "")
        if not sid or sid == self_id or sid in seen:
            continue
        seen.add(sid)
        code, label = _norm_lang(it.get("lang"))
        out.append({
            "source_id": sid,
            "title": it.get("title") or "",
            "lang": code,
            "lang_label": label,
            "is_original": bool(it.get("is_original")),
            "cover_url": f"/api/cover/{sid}?v=2",
        })
    out.sort(key=lambda e: (_ZH_ORDER.get(e["lang"], 9), e["source_id"]))
    return out


def _edition_group(raw: dict) -> str:
    """版本组：同组即为同一作品的不同语言版本。"""
    for ed in raw.get("language_editions") or []:
        if isinstance(ed, dict) and ed.get("edition_id") is not None:
            return str(ed.get("edition_id"))
    info = raw.get("translation_info") or {}
    if isinstance(info, dict):
        for key in ("parent_workno", "original_workno"):
            if info.get(key):
                return str(info.get(key))
    return str(raw.get("source_id") or raw.get("original_workno") or "")


def _chinese_edition(editions: list[dict]) -> dict | None:
    for code in ("CHI_HANS", "CHI", "CHI_HANT"):
        for ed in editions:
            if ed.get("lang") == code:
                return ed
    return None


def _chinese_announced(raw: dict, editions: list[dict]) -> bool:
    """上游已公布中文版本，但没有可直接下载的条目。"""
    if _chinese_edition(editions):
        return False
    for ed in raw.get("language_editions") or []:
        if isinstance(ed, dict):
            code, _ = _norm_lang(ed.get("lang"), ed.get("label") or "")
            if _is_zh(code):
                return True
    return False


def _original_workno(raw: dict) -> str:
    info = raw.get("translation_info") or {}
    return str(info.get("original_workno") or "") if isinstance(info, dict) else ""


def _parse_work(raw: dict) -> dict:
    """把 asmr.one API 返回的 work 转成统一格式。"""
    source_id = str(raw.get("source_id") or raw.get("original_workno") or "")
    title = raw.get("title") or raw.get("name") or ""
    release = raw.get("release") or raw.get("create_date") or ""
    tags = _names(raw.get("tags"))
    # asmr.one 原始作者字段是 circle（社团）/ vas（声优）
    tags_circle = _names(raw.get("tags_circle")) or _names(raw.get("circle"))
    tags_va = _names(raw.get("tags_va")) or _names(raw.get("vas"))
    cover = raw.get("mainCoverUrl") or raw.get("samCoverUrl") or raw.get("thumbnailCoverUrl") or raw.get("cover_url") or ""
    # 统一走本地代理：前端不受 Referer / IPv6 直连限制，且能命中本地已刮削的封面
    if source_id:
        cover = f"/api/cover/{source_id}?v=2"
    age_raw = str(raw.get("age_category_string") or "").lower()
    if age_raw in ("r15", "r-15"):
        age_level = "r15"
    elif age_raw in ("general", "all", "all_ages"):
        age_level = "all"
    else:
        age_level = "adult"
    lang, lang_label = _work_lang(raw)
    editions = _work_editions(raw, source_id)
    chinese_edition = _chinese_edition(editions)
    chinese_announced = _chinese_announced(raw, editions)
    edition_group = _edition_group(raw)
    original_workno = _original_workno(raw)
    # 搜索列表可能不返回版本字段，标记是否可信，前端可据此决定是否实时补齐
    lang_known = (isinstance(raw.get("other_language_editions_in_db"), list)
                  or isinstance(raw.get("language_editions"), list)
                  or isinstance(raw.get("translation_info"), dict))
    return {
        "source_id": source_id,
        "title": title,
        "release_date": release,
        "tags": tags,
        "tags_circle": tags_circle,
        "tags_va": tags_va,
        "age_level": age_level,
        "has_subtitle": bool(raw.get("has_subtitle", False)),
        "work_count": raw.get("work_count", 0),
        "dl_count": raw.get("dl_count", 0),
        "rating": float(raw.get("rating") or 0),
        "price": raw.get("price", 0),
        "cover_url": cover,
        "lang": lang,
        "lang_label": lang_label,
        "editions": editions,
        "chinese_edition": chinese_edition,
        "chinese_announced": chinese_announced,
        "edition_group": edition_group,
        "original_workno": original_workno,
        "lang_known": lang_known,
    }


def _terms(raw: str) -> tuple[list[str], list[str]]:
    """把空格/逗号分隔的输入拆成 (包含词, 排除词)；- 前缀表示排除。"""
    pos: list[str] = []
    neg: list[str] = []
    for tok in re.split(r"[\s,]+", raw or ""):
        tok = tok.strip()
        if not tok:
            continue
        if tok.startswith("-") and len(tok) > 1:
            neg.append(tok[1:])
        else:
            pos.append(tok)
    return pos, neg


@search_bp.route("/search", methods=["GET"])
def search():
    """
    GET /api/search
      ?q=关键词
      &tag=标签1,标签2
      &va=声优名
      &circle=社团
      &order=release|dl_count|rating|price
      &sort=desc|asc
      &page=1
      &pageSize=20
      &has_subtitle=0|1
      &from_date=YYYY-MM-DD
      &to_date=YYYY-MM-DD
      &exclude_age=all,r15,adult
    """
    cfg = current_app.config["APP_CONFIG"]
    db = current_app.config["DB"]

    q = (request.args.get("q") or "").strip()
    tag = (request.args.get("tag") or "").strip()
    va = (request.args.get("va") or "").strip()
    circle = (request.args.get("circle") or "").strip()
    exclude_tag = (request.args.get("exclude_tag") or "").strip()
    exclude_va = (request.args.get("exclude_va") or "").strip()
    exclude_circle = (request.args.get("exclude_circle") or "").strip()
    order = request.args.get("order", "release")
    sort = request.args.get("sort", "desc")
    page = int(request.args.get("page", 1))
    page_size = min(int(request.args.get("pageSize", 20)), 100)
    has_subtitle = request.args.get("has_subtitle")
    from_date = request.args.get("from_date")
    to_date = request.args.get("to_date")
    exclude_age = (request.args.get("exclude_age") or "").strip()
    exclude_downloaded = request.args.get("exclude_downloaded") in ("1", "true")

    try:
        c = _make_client(cfg)
        c.login()

        # 构造搜索表达式
        # 语法: 关键词1,关键词2,@key:value,-key:value?param=value&param=value
        # 关键词支持 "AA BB" 表示同时包含、"AA -BB" 表示包含 AA 且排除 BB
        # FILTER_KEYS = tag, circle, va, duration, rate, price, sell, age, lang
        # PARAM_KEYS  = order, sort, subtitle, page, pageSize
        keywords, neg_keywords = _terms(q)
        # 上游对排除条件支持不完整，收集全部排除词用于本地兜底过滤
        neg_all: list[str] = list(neg_keywords)

        filters: list[str] = []
        for key, raw, neg_raw in (("tag", tag, exclude_tag),
                                  ("va", va, exclude_va),
                                  ("circle", circle, exclude_circle)):
            pos_terms, neg_terms = _terms(raw)
            neg_terms += _terms(neg_raw)[0] + _terms(neg_raw)[1]
            neg_all += neg_terms
            # 同一个 key 只能出现一次（API 限制），多余条件退化为关键词
            if pos_terms:
                filters.append(f"{key}:{pos_terms[0]}")
                keywords += pos_terms[1:]
            for i, val in enumerate(neg_terms):
                if i == 0:
                    filters.append(f"-{key}:{val}")
                else:
                    neg_keywords.append(val)

        keywords += ["-" + k for k in neg_keywords]

        kw_str = ",".join(keywords)
        filter_str = ",".join(filters) if filters else ""
        expr = f"{kw_str}@{filter_str}" if (kw_str or filter_str) else "@"

        # 有排除词或排除已下载时多取一些，过滤后再截断，避免整页被过滤成空白
        fetch_size = min(page_size * 2, 100) if (neg_all or exclude_downloaded) else page_size

        # params 拼到最后（asmr_client.build_search_path 会解析 ?key=value）
        param_parts = [f"order={order}", f"sort={sort}", f"page={page}", f"pageSize={fetch_size}"]
        if has_subtitle and has_subtitle != "all":
            param_parts.append(f"subtitle={has_subtitle}")
        param_str = "?" + "&".join(param_parts)

        # 分级排除：勾选=排除。上游只支持单个 age 包含筛选，
        # 因此把「排除」转成「保留其余分级」：保留 1 个→单查，保留 2 个→两次查询归并。
        excluded = [a for a in exclude_age.split(",") if a in ("all", "r15", "adult")]
        keep_ages = [a for a in ("all", "r15", "adult") if a not in excluded]
        age_upstream = {"all": "general", "r15": "r15", "adult": "adult"}

        def _expr_with_age(age_val: str) -> str:
            fs = filters + ([f"age:{age_upstream[age_val]}"] if age_val else [])
            fs_str = ",".join(fs)
            base = f"{kw_str}@{fs_str}" if (kw_str or fs_str) else "@"
            return base + param_str

        if not keep_ages:
            # 三个分级全被排除
            full_expr = _expr_with_age("")
            data = {"works": [], "pagination": {"totalCount": 0}}
        elif len(keep_ages) == 3:
            full_expr = _expr_with_age("")
            data = c.search(full_expr)
        elif len(keep_ages) == 1:
            full_expr = _expr_with_age(keep_ages[0])
            data = c.search(full_expr)
        else:
            # 保留两个分级 = 合并两次上游查询，再按排序字段归并
            parts = [c.search(_expr_with_age(a)) for a in keep_ages]
            merged: list[dict] = []
            total = 0
            for part in parts:
                if isinstance(part, dict):
                    merged += part.get("works", [])
                    total += int((part.get("pagination") or {}).get("totalCount") or 0)
            if order == "release":
                key_fn = lambda w: str(w.get("release") or "")
            elif order == "price":
                key_fn = lambda w: float(w.get("price") or 0)
            elif order == "rating":
                key_fn = lambda w: float(w.get("rate_average_2dp") or 0)
            else:
                key_fn = lambda w: float(w.get("dl_count") or 0)
            merged.sort(key=key_fn, reverse=(sort != "asc"))
            data = {"works": merged[:fetch_size],
                    "pagination": {"totalCount": total}}
            full_expr = _expr_with_age("")
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": "search_failed", "detail": str(e)}), 502

    works_raw = data.get("works", []) if isinstance(data, dict) else []

    # 排除词兜底过滤：上游对 "-词" / "-tag:值" 支持不完整，这里再筛一遍
    if neg_all:
        neg_lower = [t.lower() for t in neg_all if t]

        def _hay(raw: dict) -> str:
            parts = [str(raw.get("title") or ""), str(raw.get("name") or "")]
            for k in ("tags", "tags_circle", "tags_va", "circle", "vas"):
                parts += _names(raw.get(k))
            return " ".join(parts).lower()

        works_raw = [
            w for w in works_raw
            if not any(n in _hay(w) for n in neg_lower)
        ][:page_size]

    # 本地二次筛选（API 不支持的筛选条件）
    if from_date or to_date:
        from_d = from_date or ""
        to_d = to_date or "9999-12-31"
        works_raw = [
            w for w in works_raw
            if (w.get("release") or "") >= from_d and (w.get("release") or "") <= to_d
        ]

    works_out: list[dict] = []
    for raw in works_raw:
        w = _parse_work(raw)
        if w["source_id"]:
            # upsert 到本地
            try:
                db.upsert_work(w)
            except Exception:
                pass
            # 合并本地信息
            local = db.get_work(w["source_id"])
            if local and local.get("local_path"):
                w["local_path"] = local["local_path"]
                w["file_count"] = local.get("file_count", 0)
                w["total_size"] = local.get("total_size", 0)
        works_out.append(w)

    # 排除已下载：本地已入库的作品不返回
    if exclude_downloaded:
        works_out = [w for w in works_out if not w.get("local_path")]

    pagination = data.get("pagination", {}) if isinstance(data, dict) else {}
    return jsonify({
        "works": works_out,
        "page": page,
        "pageSize": page_size,
        "total": pagination.get("totalCount", 0),
        "expr": full_expr,
    })


@search_bp.route("/works", methods=["GET"])
def list_works():
    """热门/全部作品列表（不支持关键词，只支持 order/sort）"""
    cfg = current_app.config["APP_CONFIG"]

    page = int(request.args.get("page", 1))
    page_size = min(int(request.args.get("pageSize", 20)), 100)
    order = request.args.get("order", "release")
    sort = request.args.get("sort", "desc")

    try:
        c = _make_client(cfg)
        c.login()
        data = c.works(page=page, page_size=page_size, order=order, sort=sort)
    except Exception as e:
        return jsonify({"error": "works_failed", "detail": str(e)}), 502

    works_raw = data.get("works", [])
    works_out = [_parse_work(w) for w in works_raw]
    return jsonify({
        "works": works_out,
        "page": page,
        "pageSize": page_size,
    })
