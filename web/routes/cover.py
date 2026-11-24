"""封面 API — 本地刮削优先，其次磁盘缓存，最后回源 asmr.one。"""
from __future__ import annotations

import os
import sys

from flask import Blueprint, Response, current_app, jsonify, send_file

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import ipv6_net as net
import asmr_client as api

cover_bp = Blueprint("cover", __name__)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
# 封面类文件名优先级（越大越可能是封面）
NAME_HINTS = ("表紙", "表纸", "cover", "jacket", "package", "パッケージ", "パッケージ画像", "main")


def _find_root_cover(local_dir: str) -> str | None:
    """只在作品根目录找 cover/表紙 这类命名的图片，避免误取杂项插图。"""
    try:
        names = os.listdir(local_dir)
    except OSError:
        return None
    best, best_score = None, -1
    for name in names:
        full = os.path.join(local_dir, name)
        if not os.path.isfile(full):
            continue
        if os.path.splitext(name)[1].lower() not in IMAGE_EXTS:
            continue
        low = name.lower()
        if not any(h in low for h in NAME_HINTS):
            continue
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        score = 20 if low.startswith(("cover", "main")) else 10
        score += min(size // 200_000, 5)
        if score > best_score:
            best, best_score = full, score
    return best


def _find_local_cover(local_dir: str) -> str | None:
    """在作品目录里找封面图（最多两层），优先命中 cover/表紙 这类命名。"""
    best, best_score = None, -1
    for root, dirs, files in os.walk(local_dir):
        depth = root[len(local_dir):].count(os.sep)
        if depth >= 2:
            dirs[:] = []
        for f in files:
            if os.path.splitext(f)[1].lower() not in IMAGE_EXTS:
                continue
            full = os.path.join(root, f)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            low = f.lower()
            score = 10 if any(h in low for h in NAME_HINTS) else 0
            score += min(size // 200_000, 5)  # 大图更可能是正式封面
            if score > best_score:
                best, best_score = full, score
    return best


def _covers_dir(cfg: dict) -> str:
    db_path = cfg.get("db_path") or ""
    base = os.path.dirname(db_path) if db_path else os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data")
    return os.path.join(base, "covers")


def _cover_id_candidates(source_id: str) -> list[str]:
    """封面接口的 id 候选：完整编号优先（VJ 等必须带前缀），再退回纯数字。"""
    out = [source_id.upper()]
    try:
        _, num = api.AsmrClient.normalize_id(source_id)
        if num not in out:
            out.append(num)
    except ValueError:
        pass
    return out


def _fetch_remote_cover(cfg: dict, source_id: str) -> bytes | None:
    nc = net.NetConfig(
        pins=cfg.get("pins") or {},
        timeout=float(cfg.get("timeout", 20)),
        retries=2,
        sni_policy=cfg.get("sni_policy", "auto"),
        verify_hostname=bool(cfg.get("verify_hostname", True)),
    )
    headers = {"Referer": "https://asmr.one/", "Accept": "image/*, */*"}
    for cid in _cover_id_candidates(source_id):
        for kind in ("main", "sam"):
            url = f"https://api.asmr.one/api/cover/{cid}.jpg?type={kind}"
            try:
                r = net.request(url, "GET", cfg=nc, headers=headers, timeout=20)
                if r.status == 200 and len(r.body or b"") > 500:
                    return r.body
            except Exception:
                continue
    return None


@cover_bp.route("/cover/<source_id>", methods=["GET"])
def get_cover(source_id: str):
    """返回作品封面（根目录 cover 类图片 → 磁盘缓存 → 回源 → 本地图片兜底）。"""
    db = current_app.config["DB"]
    cfg = current_app.config["APP_CONFIG"]

    w = db.get_work(source_id)
    local_dir = (w or {}).get("local_path") or ""
    if not local_dir or not os.path.isdir(local_dir):
        local_dir = ""

    # 1. 作品根目录里的 cover 类图片（下载时由官方 main 封面写入）
    if local_dir:
        root_cover = _find_root_cover(local_dir)
        if root_cover:
            return send_file(root_cover, max_age=86400)

    # 2. 磁盘缓存（来自官方接口）
    cache_dir = _covers_dir(cfg)
    cache = os.path.join(cache_dir, source_id + ".jpg")
    if os.path.exists(cache) and os.path.getsize(cache) > 500:
        return send_file(cache, mimetype="image/jpeg", max_age=86400)

    # 3. 回源
    body = _fetch_remote_cover(cfg, source_id)
    if body:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(cache, "wb") as f:
                f.write(body)
        except OSError:
            pass
        return Response(body, mimetype="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})

    # 4. 兜底：作品目录内其它图片
    if local_dir:
        local = _find_local_cover(local_dir)
        if local:
            return send_file(local, max_age=86400)

    return jsonify({"error": "cover_not_found"}), 404
