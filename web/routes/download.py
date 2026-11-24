"""下载任务 API — 批量下载 + 后台 worker + 进度轮询 + 断点续传"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

from flask import Blueprint, current_app, jsonify, request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import ipv6_net as net
import asmr_client as api
import downloader as dl
from web.config import pick_storage_root, storage_entries
from web.routes.search import _parse_work

download_bp = Blueprint("download", __name__)

# 全局任务运行状态（task_id -> {stop_event, thread}）
_running_tasks: dict[int, dict[str, Any]] = {}
_running_tasks_lock = threading.Lock()


# --------------------------------------------------------------------------
# 核心下载 worker（严格 IPv6 在线程内启用）
# --------------------------------------------------------------------------

ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _clip_bytes(s: str, limit: int) -> str:
    """按 UTF-8 字节数截断，且不切坏多字节字符。"""
    b = s.encode("utf-8")
    if len(b) <= limit:
        return s
    return b[:limit].decode("utf-8", "ignore")


def _sanitize(name: str, max_bytes: int = 180) -> str:
    """清理非法字符并按字节截断。

    文件系统单个名字上限 255 字节（日文 1 字 = 3 字节），
    截断时追加短哈希，避免同目录不同长名截断后重名。
    """
    name = ILLEGAL.sub("_", str(name)).strip().rstrip(" .")
    if not name:
        return "untitled"
    if len(name.encode("utf-8")) <= max_bytes:
        return name
    stem, ext = os.path.splitext(name)
    if len(ext.encode("utf-8")) > 20:
        stem, ext = name, ""
    digest = hashlib.md5(name.encode("utf-8")).hexdigest()[:6]
    budget = max_bytes - len(ext.encode("utf-8")) - len(digest) - 1
    stem = _clip_bytes(stem, max(16, budget)).rstrip(" .")
    return f"{stem or 'file'}-{digest}{ext}"


def _work_folder_name(wid: str, info: dict) -> str:
    rjid = _sanitize(wid.upper(), 16)
    date = _sanitize(str(info.get("release") or "unknown"), 20)
    subtitle = "subtitle" if info.get("has_subtitle") else "nosub"
    title = _sanitize(str(info.get("title") or info.get("name") or "untitled"), 120)
    return f"{rjid}-{date}-{subtitle}-{title}"


def _filter_entries(entries: list, cfg: dict) -> list:
    """按下载配置过滤远程清单，下载与完整性校验共用同一规则。"""
    exclude_ext = {x.strip().lower().lstrip(".")
                   for x in (cfg.get("exclude_ext") or "").split(",") if x.strip()}
    include_ext = {x.strip().lower().lstrip(".")
                   for x in (cfg.get("include_ext") or "").split(",") if x.strip()}
    exclude_kw = {k.strip().lower()
                  for k in (cfg.get("exclude_keyword") or "").split(",") if k.strip()}

    def should_keep(entry):
        ext = os.path.splitext(entry.name)[1].lower().lstrip(".")
        if include_ext and ext not in include_ext:
            return False
        if exclude_ext and ext in exclude_ext:
            return False
        for kw in exclude_kw:
            if kw and kw in (entry.rel_dir + "/" + entry.name).lower():
                return False
        return True

    return [e for e in entries if should_keep(e)]


def _entry_parts(entry) -> tuple[str, str]:
    rel = "/".join(_sanitize(p) for p in entry.rel_dir.split("/") if p)
    return rel, _sanitize(entry.name)


def _entry_local_path(base_dir: str, entry) -> str:
    rel, fname = _entry_parts(entry)
    return os.path.join(base_dir, rel, fname)


def _entry_key(entry) -> str:
    """清单条目的相对路径键（下载与完整性校验共用同一口径）。"""
    rel, fname = _entry_parts(entry)
    return f"{rel}/{fname}" if rel else fname


# --------------------------------------------------------------------------
# 最小下载：旧语言版本文件复用（硬链接）
# --------------------------------------------------------------------------

REUSE_EXTS = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg",
              ".jpg", ".jpeg", ".png", ".webp"}
REUSE_AUDIO_EXTS = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg"}
_TRACK_RE = re.compile(r"(?i)track[\s_\-]*(\d+)")
_DUR_CACHE: dict[str, float] = {}


def _track_no(name: str) -> str:
    """提取 Track 编号；取不到返回空串。"""
    base = os.path.basename(name)
    m = _TRACK_RE.search(base)
    if not m:
        m = re.match(r"\s*(\d+)", base)
    return (m.group(1).lstrip("0") or "0") if m else ""


def _probe_seconds(path: str) -> float:
    """ffprobe 读取时长（秒），失败返回 0；同路径缓存。"""
    if path in _DUR_CACHE:
        return _DUR_CACHE[path]
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=20,
        )
        val = float((p.stdout or "").strip() or 0)
    except Exception:
        val = 0.0
    _DUR_CACHE[path] = val
    return val


def _index_local_files(base_dir: str) -> dict[tuple[str, int], list[str]]:
    """按 (扩展名, 大小) 索引旧版本可复用的媒体/图片文件。"""
    index: dict[tuple[str, int], list[str]] = {}
    if not base_dir or not os.path.isdir(base_dir):
        return index
    for dirpath, dirnames, filenames in os.walk(base_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in REUSE_EXTS:
                continue
            full = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if size <= 0:
                continue
            index.setdefault((ext, size), []).append(full)
    return index


def _infer_storage_root(cfg: dict, path: str) -> str:
    """把作品目录归到已配置存储根（用于保证硬链接同设备）。"""
    real = os.path.realpath(path)
    for r in storage_entries(cfg):
        root = os.path.realpath(r["path"])
        try:
            if os.path.commonpath([real, root]) == root:
                return r["path"]
        except ValueError:
            continue
    return ""


def _match_reuse(entry, index: dict, used: set[str]) -> str | None:
    """按“扩展名 + 精确大小 + Track 编号 + 音频时长”匹配旧文件。"""
    ext = os.path.splitext(entry.name)[1].lower()
    if ext not in REUSE_EXTS or not entry.size:
        return None
    cands = [p for p in index.get((ext, int(entry.size)), []) if p not in used]
    if not cands:
        return None
    want_no = _track_no(entry.name)
    if len(cands) > 1 and want_no:
        by_no = [p for p in cands if _track_no(p) == want_no]
        if by_no:
            cands = by_no
    if len(cands) > 1 and entry.duration > 0:
        by_dur = [p for p in cands
                  if abs(_probe_seconds(p) - entry.duration) <= 0.5]
        if by_dur:
            cands = by_dur
        else:
            return None
    if len(cands) != 1:
        return None
    src = cands[0]
    if ext in REUSE_AUDIO_EXTS and entry.duration > 0:
        if abs(_probe_seconds(src) - entry.duration) > 0.5:
            return None
    return src


def _build_link_map(entries: list, index: dict) -> dict[str, str]:
    """为本次要落盘的中文清单挑选可硬链接的旧文件。"""
    link_map: dict[str, str] = {}
    used: set[str] = set()
    if not index:
        return link_map
    for e in entries:
        src = _match_reuse(e, index, used)
        if not src:
            continue
        key = _entry_key(e)
        link_map[key] = src
        used.add(src)
    return link_map


def _actual_size(base_dir: str, entries: list) -> int:
    """按清单统计本地已存在文件的字节数（定向补齐后重算作品占用）。"""
    total = 0
    for e in entries:
        try:
            total += os.path.getsize(_entry_local_path(base_dir, e))
        except OSError:
            continue
    return total


COVER_NAME_HINTS = ("cover", "表紙", "表纸", "ジャケット", "jacket", "package", "パッケージ", "main")
COVER_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def _is_image_file(path: str) -> bool:
    """按文件头判断是否为可用的 JPEG/PNG/WebP 图片。"""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:
        return False
    return (head.startswith(b"\xff\xd8\xff")
            or head.startswith(b"\x89PNG\r\n\x1a\n")
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"))


def _find_cover_files(base_dir: str, expected: set[str]) -> list[dict]:
    """识别作品根目录下确实可用的封面图（远程清单之外的 cover/表紙 等）。"""
    covers: list[dict] = []
    try:
        names = sorted(os.listdir(base_dir))
    except OSError:
        return covers
    for name in names:
        if name.startswith(".") or name in expected:
            continue
        stem, ext = os.path.splitext(name)
        if ext.lower() not in COVER_EXTS:
            continue
        if not any(h in stem.lower() for h in COVER_NAME_HINTS):
            continue
        full = os.path.join(base_dir, name)
        if not os.path.isfile(full) or not _is_image_file(full):
            continue
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        if size > 0:
            covers.append({"path": name, "actual_size": size})
    return covers



def _scrape_cover(netcfg, wid: str, base_dir: str) -> None:
    """根目录没有 cover 类图片时，把官方封面存成 cover.jpg。"""
    try:
        for f in os.listdir(base_dir):
            full = os.path.join(base_dir, f)
            if not os.path.isfile(full):
                continue
            if os.path.splitext(f)[1].lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                continue
            low = f.lower()
            if any(h in low for h in COVER_NAME_HINTS):
                return
    except OSError:
        return
    # 封面 id：完整编号优先（VJ 等必须带前缀），再退回纯数字
    cands = [wid.upper()]
    try:
        _, num = api.AsmrClient.normalize_id(wid)
        if num not in cands:
            cands.append(num)
    except ValueError:
        pass
    for cid in cands:
        try:
            r = net.request(f"https://api.asmr.one/api/cover/{cid}.jpg?type=main", "GET",
                            cfg=netcfg, headers={"Referer": "https://asmr.one/"}, timeout=20)
            if r.status == 200 and len(r.body or b"") > 500:
                with open(os.path.join(base_dir, "cover.jpg"), "wb") as fh:
                    fh.write(r.body)
                return
        except Exception:
            continue


def _purge_local_work(db, cfg: dict, source_id: str) -> None:
    """覆盖下载前清空本地旧文件（仅限已配置的存储根内）。"""
    w = db.get_work(source_id)
    if not w:
        return
    lp = w.get("local_path") or ""
    if not lp or not os.path.isdir(lp):
        return
    real = os.path.realpath(lp)
    allowed = False
    for r in storage_entries(cfg):
        root = os.path.realpath(r["path"])
        try:
            if os.path.commonpath([real, root]) == root:
                allowed = True
                break
        except ValueError:
            continue
    if not allowed:
        return
    shutil.rmtree(real, ignore_errors=True)
    db.update_local_work(source_id, "", "", 0, 0)


def _delete_unfinished_work(db, cfg: dict, source_id: str) -> None:
    """取消暂停任务时删除当前未完成作品，目录必须位于已配置存储根内。"""
    w = db.get_work(source_id)
    if not w:
        return
    lp = w.get("local_path") or ""
    if lp and os.path.isdir(lp):
        real = os.path.realpath(lp)
        allowed = False
        for r in storage_entries(cfg):
            root = os.path.realpath(r["path"])
            try:
                if os.path.commonpath([real, root]) == root:
                    allowed = True
                    break
            except ValueError:
                continue
        if not allowed:
            raise RuntimeError("作品目录不在已配置存储根内，拒绝删除")
        shutil.rmtree(real)
    # 目录已经不存在时也清理数据库残留
    db.delete_work(source_id)


def verify_work_integrity(db, cfg: dict, source_id: str,
                          entries: list | None = None) -> dict:
    """按远程清单、本地存在性和字节大小校验作品，并持久化报告。"""
    checked_at = time.strftime("%Y-%m-%dT%H:%M:%S")

    def persist(report: dict) -> dict:
        for key in ("missing", "size_mismatch", "extra_files", "unknown_size", "covers"):
            values = report.get(key) or []
            report[f"{key}_count"] = len(values)
        stored = dict(report)
        for key in ("missing", "size_mismatch", "extra_files", "unknown_size", "covers"):
            stored[key] = (report.get(key) or [])[:200]
        db.set_work_integrity(source_id, report["status"], checked_at, stored)
        report["checked_at"] = checked_at
        return report

    w = db.get_work(source_id)
    if not w:
        return {"source_id": source_id, "status": "error",
                "error": "work_not_found", "checked_at": checked_at}

    try:
        if entries is None:
            from web.routes.search import _make_client
            client = _make_client(cfg)
            client.login()
            num = api.AsmrClient.normalize_id(source_id)[1]
            entries = _filter_entries(
                api.AsmrClient.flatten(client.tracks(num)), cfg)
    except Exception as e:
        return persist({"source_id": source_id, "status": "error",
                        "error": str(e), "expected_files": 0,
                        "missing": [], "size_mismatch": [],
                        "extra_files": [], "unknown_size": [], "covers": []})

    # 相对路径统一使用 /，与下载时的清理、截断规则完全一致
    expected: dict[str, int] = {}
    for e in entries:
        key = _entry_key(e)
        try:
            size = int(e.size or 0)
        except (TypeError, ValueError):
            size = 0
        expected[key] = size

    base_dir = w.get("local_path") or ""
    report = {"source_id": source_id, "status": "complete",
              "expected_files": len(expected), "missing": [],
              "size_mismatch": [], "extra_files": [], "unknown_size": [],
              "covers": []}

    if not base_dir or not os.path.isdir(base_dir):
        report["status"] = "incomplete"
        report["missing"] = [
            {"path": rel, "expected_size": size}
            for rel, size in sorted(expected.items())
        ]
        return persist(report)

    for rel, expected_size in sorted(expected.items()):
        full = os.path.join(base_dir, *rel.split("/"))
        if not os.path.isfile(full):
            report["missing"].append({"path": rel, "expected_size": expected_size})
            continue
        actual_size = os.path.getsize(full)
        if expected_size > 0:
            if actual_size != expected_size:
                report["size_mismatch"].append({
                    "path": rel, "expected_size": expected_size,
                    "actual_size": actual_size,
                })
        else:
            report["unknown_size"].append({"path": rel, "actual_size": actual_size})

    # 根目录确实正确的封面图计入清单，不再当作多余文件
    report["covers"] = _find_cover_files(base_dir, set(expected))
    cover_paths = {c["path"] for c in report["covers"]}

    for dirpath, dirnames, filenames in os.walk(base_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, base_dir).replace(os.sep, "/")
            if rel not in expected and rel not in cover_paths:
                report["extra_files"].append({"path": rel,
                                              "actual_size": os.path.getsize(full)})

    # 多余文件和远程未知大小只警告；缺失或大小不符才是不完整
    if report["missing"] or report["size_mismatch"]:
        report["status"] = "incomplete"
    return persist(report)


def _download_worker(task_id: int, work_ids: list[str], cfg: dict, db,
                     overwrite: set[str] | None = None):
    """后台下载线程入口。"""
    rt = _running_tasks.get(task_id, {})
    stop_event = rt.get("stop_event")
    pause_event = rt.get("pause_event")

    # 定向补齐清单：作品 ID -> 需要重下的相对路径；为空时按整部作品下载
    task_row = db.get_task(task_id) or {}
    filter_options = task_row.get("filter_options")
    if not isinstance(filter_options, dict):
        try:
            filter_options = json.loads(filter_options or "{}")
        except (TypeError, ValueError):
            filter_options = {}
    repair_files: dict[str, list[str]] = filter_options.get("repair_files") or {}
    reuse_from = filter_options.get("reuse_from") or {}
    if not isinstance(reuse_from, dict):
        reuse_from = {}

    try:
        net.enable_strict_ipv6()

        # 客户端
        pins = cfg.get("pins") or {}
        proxy = net.parse_proxy(cfg.get("proxy") or "") if cfg.get("proxy") else None
        netcfg = net.NetConfig(
            pins=pins,
            proxy=proxy,
            sni_policy=cfg.get("sni_policy", "auto"),
            verify_hostname=bool(cfg.get("verify_hostname", True)),
            timeout=float(cfg.get("timeout", 30)),
            retries=int(cfg.get("retries", 3)),
        )
        client = api.AsmrClient(netcfg, account=cfg.get("account", "guest"),
                                password=cfg.get("password", "guest"))
        client.login()

        started = db.update_task_if(
            task_id, "running", ("pending",),
            started_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
        if not started:
            # 启动前已被暂停/取消：不再覆盖状态
            if pause_event and pause_event.is_set():
                db.update_task_if(task_id, "paused", ("pausing",))
            return

        totals = {"ok": 0, "skip": 0, "fail": 0, "reuse": 0,
                  "files_ok": 0, "total_size": 0}
        all_files: list[dict] = []

        for wid in work_ids:
            if stop_event and stop_event.is_set():
                db.update_task(task_id, status="cancelled",
                               error="user_cancelled",
                               finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
                return
            if pause_event and pause_event.is_set():
                db.update_task(task_id, status="paused")
                return

            # 覆盖模式：先清空该作品已有的本地文件
            if overwrite and wid in overwrite:
                try:
                    _purge_local_work(db, cfg, wid)
                except Exception:
                    pass

            try:
                num = api.AsmrClient.normalize_id(wid)[1]
                info = client.work_info(num)
            except Exception as e:
                totals["fail"] += 1
                db.update_task(task_id, progress_json=json.dumps({"error": f"{wid}: {e}"}))
                continue

            source_id = str(info.get("source_id") or wid)
            folder = _work_folder_name(source_id, info)

            # 写入 works 表（复用搜索的解析逻辑，确保社团/声优等作者信息入库）
            parsed = _parse_work(info)
            w_data = {
                "source_id": source_id,
                "title": parsed["title"],
                "release_date": parsed["release_date"],
                "tags": parsed["tags"],
                "tags_circle": parsed["tags_circle"],
                "tags_va": parsed["tags_va"],
                "has_subtitle": parsed["has_subtitle"],
                "work_count": parsed["work_count"],
                "dl_count": parsed["dl_count"],
                "rating": parsed["rating"],
                "price": parsed["price"],
                "age_level": parsed["age_level"],
                "cover_url": f"/api/cover/{source_id}",
            }
            try:
                db.upsert_work(w_data)
            except Exception:
                pass
            # 本次信息来自远程，顺带刷新销量时间，避免后台又抓一遍
            if parsed.get("dl_count"):
                try:
                    db.set_work_dl_refresh(source_id, parsed["dl_count"],
                                           parsed.get("rating"), parsed.get("price"))
                except Exception:
                    pass

            db.update_task(task_id, current_work=source_id)

            # 定向补齐：只重下校验结果中缺失/大小不符的文件
            existing = db.get_work(source_id) or {}
            existing_base = existing.get("local_path") or ""
            repair_paths = {str(p) for p in (repair_files.get(wid)
                                             or repair_files.get(source_id) or [])
                            if p}
            reuse_base = (bool(repair_paths) and bool(existing_base)
                          and os.path.isdir(existing_base))
            if not reuse_base:
                repair_paths = set()  # 目录不存在时退回整部作品下载

            # 获取音轨 + 过滤
            try:
                all_entries = _filter_entries(
                    api.AsmrClient.flatten(client.tracks(num)), cfg)
            except Exception as e:
                totals["fail"] += 1
                continue
            if not all_entries:
                continue

            entries = ([e for e in all_entries if _entry_key(e) in repair_paths]
                       if repair_paths else all_entries)
            if not entries:
                continue

            # 语言版本更新：从旧版目录挑选可硬链接复用的文件
            reuse_old_id = str(reuse_from.get(wid) or reuse_from.get(source_id) or "")
            link_map: dict[str, str] = {}
            reuse_root = ""
            if reuse_old_id and reuse_old_id != source_id and not reuse_base:
                old_work = db.get_work(reuse_old_id) or {}
                old_base = old_work.get("local_path") or ""
                if old_base and os.path.isdir(old_base):
                    reuse_root = (old_work.get("storage_root")
                                  or _infer_storage_root(cfg, old_base))
                    link_map = _build_link_map(entries, _index_local_files(old_base))
                    if not reuse_root:
                        link_map = {}  # 无法确定旧版存储根，放弃复用

            if reuse_base:
                # 补齐直接写回原目录，进度只统计目标文件
                base_dir = existing_base
                root = existing.get("storage_root") or ""
            else:
                # 按优先级选存储空间：第一个剩余容量足够的根；全部不足则中止并警告
                estimated = sum(max(0, int(e.size or 0)) for e in entries
                                if _entry_key(e) not in link_map)
                root = ""
                if link_map:
                    try:
                        free = shutil.disk_usage(reuse_root).free
                    except OSError:
                        free = 0
                    if free >= estimated:
                        root = reuse_root
                    else:
                        # 同盘空间不足：放弃复用，按完整大小选盘
                        link_map = {}
                        estimated = sum(max(0, int(e.size or 0)) for e in entries)
                if not root:
                    root = pick_storage_root(cfg, estimated)
                if not root:
                    need = f"约 {estimated / 1024**3:.1f} GB" if estimated else "所需空间"
                    db.update_task(
                        task_id, status="failed",
                        error=f"[STORAGE_FULL] 所有存储空间剩余容量均不足（{source_id} 需要{need}），"
                              "请释放空间或在系统设置中添加新的存储空间",
                        finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                        progress_json=json.dumps({"summary": totals}),
                    )
                    return
                base_dir = os.path.join(root, folder)
                try:
                    os.makedirs(base_dir, exist_ok=True)
                except OSError as e:
                    db.update_task(task_id, status="failed", error=f"无法创建目录: {e}",
                                   finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
                    return

            # 写入 files 表
            file_records: list[dict] = []
            for e in entries:
                rel, fname = _entry_parts(e)
                local_path = _entry_local_path(base_dir, e)
                fr = {
                    "work_id": source_id,
                    "track_url": e.url,
                    "rel_path": rel,
                    "filename": fname,
                    "size": e.size,
                    "status": "pending",
                    "local_path": local_path,
                }
                fr["id"] = db.upsert_file(source_id, fname,
                    track_url=e.url, rel_path=rel, size=e.size,
                    status="pending", local_path=local_path, downloaded_size=0)
                file_records.append(fr)

            # 下载每个文件
            workers = max(1, int(cfg.get("workers", 4)))
            tasks = []
            for i, (e, fr) in enumerate(zip(entries, file_records)):
                tasks.append((i, e, fr))

            results: list[tuple[int, str, int]] = []
            lock = threading.Lock()

            def download_one(idx, e, fr):
                if stop_event and stop_event.is_set():
                    return (idx, "cancelled", 0)
                # 暂停：未开始的文件直接退出，正在下载的文件继续下完
                if pause_event and pause_event.is_set():
                    return (idx, "paused", 0)

                local_path = fr["local_path"]
                os.makedirs(os.path.dirname(local_path), exist_ok=True)

                # 已存在且大小对 → skip
                if os.path.exists(local_path):
                    local_size = os.path.getsize(local_path)
                    if local_size > 0 and (e.size <= 0 or local_size == e.size):
                        return (idx, "skip", local_size)

                # 旧语言版本可复用：硬链接到中文版目标路径，零网络下载
                src = link_map.get(_entry_key(e))
                if src and not os.path.exists(local_path):
                    try:
                        os.link(src, local_path)
                        linked = os.path.getsize(local_path)
                        db.update_file_status(fr["id"], "downloaded", local_path)
                        db.update_file_progress(fr["id"], linked, 0)
                        return (idx, "reuse", linked)
                    except OSError:
                        pass  # 跨设备/权限问题：退回正常下载

                # Range 断点续传
                offset = os.path.getsize(local_path) if os.path.exists(local_path) else 0
                headers = {
                    "Referer": "https://asmr.one/",
                    "Origin": "https://asmr.one",
                }
                if client.token:
                    headers["Authorization"] = "Bearer " + client.token
                if offset > 0:
                    headers["Range"] = f"bytes={offset}-"

                retries = max(1, int(cfg.get("retries", 3)))
                for attempt in range(retries):
                    if stop_event and stop_event.is_set():
                        return (idx, "cancelled", offset)
                    try:
                        conn, resp, status, rh = net.download_stream(
                            e.url, cfg=netcfg, headers=headers,
                            timeout=float(cfg.get("timeout", 30)))
                        if status not in (200, 206):
                            conn.close()
                            time.sleep(0.5)
                            continue
                        if status == 200:
                            offset = 0
                        mode = "ab" if (status == 206 and offset > 0) else "wb"
                        start_t = time.monotonic()
                        last_t = start_t
                        last_downloaded = offset
                        with open(local_path, mode) as f:
                            while True:
                                if stop_event and stop_event.is_set():
                                    conn.close()
                                    return (idx, "cancelled", os.path.getsize(local_path))
                                chunk = resp.read(65536)
                                if not chunk:
                                    break
                                f.write(chunk)
                                offset += len(chunk)
                                # 实时更新进度（每 0.5 秒）
                                now = time.monotonic()
                                if now - last_t >= 0.5:
                                    sp = (offset - last_downloaded) / max(now - last_t, 0.001)
                                    db.update_file_progress(fr["id"], offset, sp)
                                    last_t = now
                                    last_downloaded = offset
                        conn.close()
                        # 完成
                        total = os.path.getsize(local_path)
                        db.update_file_status(fr["id"], "downloaded", local_path)
                        db.update_file_progress(fr["id"], total, 0)
                        return (idx, "ok", total)
                    except Exception as ex:
                        last_ex = ex
                        try:
                            conn.close()
                        except Exception:
                            pass
                        if attempt < retries - 1:
                            time.sleep(0.5 * (2 ** attempt))
                        else:
                            # 失败
                            db.update_file_status(fr["id"], "failed")
                            return (idx, "fail", 0)
                return (idx, "fail", 0)

            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futs = [pool.submit(download_one, *t) for t in tasks]
                for fut in concurrent.futures.as_completed(futs):
                    results.append(fut.result())

                    # 更新 task 进度
                    with lock:
                        done_count = sum(1 for r in results
                                         if r[1] in ("ok", "skip", "reuse", "fail", "cancelled"))
                        progress = {
                            "current_work": source_id,
                            "total_entries": len(entries),
                            "done_entries": done_count,
                            "results": {
                                "ok": sum(1 for r in results if r[1] == "ok"),
                                "skip": sum(1 for r in results if r[1] == "skip"),
                                "reuse": sum(1 for r in results if r[1] == "reuse"),
                                "fail": sum(1 for r in results if r[1] == "fail"),
                            }
                        }
                        db.update_task(task_id, progress_json=json.dumps(progress))

            # 汇总这个作品
            ok = sum(1 for r in results if r[1] == "ok")
            sk = sum(1 for r in results if r[1] == "skip")
            ru = sum(1 for r in results if r[1] == "reuse")
            fl = sum(1 for r in results if r[1] == "fail")
            totals["ok"] += ok
            totals["skip"] += sk
            totals["reuse"] += ru
            totals["fail"] += fl
            totals["files_ok"] += ok + sk + ru
            totals["total_size"] += sum(r[2] for r in results
                                        if r[1] in ("ok", "skip", "reuse"))

            # 更新 works 表（暂停时未下载的文件不计入 file_count）
            paused_now = bool(pause_event and pause_event.is_set())
            if repair_paths:
                # 定向补齐：按完整清单重算文件数与占用，避免把已有文件漏算
                if paused_now:
                    final_count = sum(
                        1 for e in all_entries
                        if os.path.isfile(_entry_local_path(base_dir, e)))
                else:
                    final_count = len(all_entries) - fl
                final_size = _actual_size(base_dir, all_entries)
            else:
                final_count = ((ok + sk + ru) if paused_now
                               else (len(all_entries) - fl))
                final_size = sum((r[2] for r in results
                                  if r[1] in ("ok", "skip", "reuse")))
            db.update_local_work(source_id, os.path.join(base_dir), root, final_count, final_size)

            if paused_now:
                # 当前文件已下完，剩余文件留待继续时跳过已下载部分续传
                db.update_task(task_id, status="paused",
                               progress_json=json.dumps({
                                   "current_work": source_id,
                                   "total_entries": len(entries),
                                   "done_entries": ok + sk + ru + fl,
                                   "results": {"ok": ok, "skip": sk,
                                               "reuse": ru, "fail": fl},
                               }))
                return

            # 封面刮削（目录里已有图片则跳过）
            _scrape_cover(netcfg, source_id, base_dir)
            # 复用当前远程清单做本地校验（定向补齐后也回归整部作品的完整性）
            report: dict = {}
            try:
                report = verify_work_integrity(db, cfg, source_id, all_entries)
            except Exception as e:
                try:
                    checked_at = time.strftime("%Y-%m-%dT%H:%M:%S")
                    db.set_work_integrity(source_id, "error", checked_at,
                                          {"status": "error", "error": str(e),
                                           "checked_at": checked_at})
                except Exception:
                    pass
            # 语言版本更新：中文版清单完全一致后才删除旧版
            if (reuse_old_id and reuse_old_id != source_id and not paused_now
                    and fl == 0 and report.get("status") == "complete"):
                try:
                    _purge_local_work(db, cfg, reuse_old_id)
                except Exception:
                    pass
            # 作品已完整结束，避免暂停后取消误删这个已完成作品
            db.update_task(task_id, current_work=None)

        # 只有所有文件/作品都成功（或完整跳过）才算完成
        final_status = "done" if totals["fail"] == 0 else "failed"
        final_error = None if final_status == "done" else f"{totals['fail']} 个文件或作品下载失败"
        db.update_task(task_id,
                       status=final_status,
                       error=final_error,
                       finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                       progress_json=json.dumps({"summary": totals}))

    except Exception as e:
        import traceback
        traceback.print_exc()
        db.update_task(task_id, status="failed",
                       error=str(e),
                       finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    finally:
        with _running_tasks_lock:
            _running_tasks.pop(task_id, None)


def _reconcile_task(task: dict | None) -> dict | None:
    """pausing 但 worker 已不在内存 → 修正为 paused（自愈历史卡死任务）。"""
    if not task or task.get("status") != "pausing":
        return task
    with _running_tasks_lock:
        alive = task["id"] in _running_tasks
    if not alive:
        db = current_app.config["DB"]
        db.update_task(task["id"], status="paused")
        task["status"] = "paused"
    return task


def _launch_task(task_id: int, work_ids: list[str], cfg: dict, db,
                 overwrite: set[str] | None = None,
                 task_updates: dict | None = None) -> bool:
    """注册并启动任务线程；任务仍在内存中时返回 False。"""
    stop_event = threading.Event()
    pause_event = threading.Event()
    thread = threading.Thread(
        target=_download_worker,
        args=(task_id, work_ids, cfg, db, overwrite),
        daemon=True,
    )
    with _running_tasks_lock:
        if task_id in _running_tasks:
            return False
        if task_updates:
            db.update_task(task_id, **task_updates)
        _running_tasks[task_id] = {
            "stop_event": stop_event,
            "pause_event": pause_event,
            "thread": thread,
            "stop": stop_event,
        }
        try:
            thread.start()
        except Exception:
            _running_tasks.pop(task_id, None)
            raise
    return True


# --------------------------------------------------------------------------
# HTTP 路由
# --------------------------------------------------------------------------

@download_bp.route("/download", methods=["POST"])
def create_download_task():
    """
    POST /api/download
      body: {"ids": ["RJ01426915", "RJ01037721"], "filter": {...}, "options": {...}}
    """
    data = request.get_json(silent=True) or {}
    ids = data.get("ids") or []
    if not ids:
        return jsonify({"error": "no_ids"}), 400

    cfg = current_app.config["APP_CONFIG"]
    db = current_app.config["DB"]

    # 规范化 ID
    normalized = []
    for raw in ids:
        raw = str(raw).strip()
        try:
            _, num = api.AsmrClient.normalize_id(raw)
            normalized.append(api.AsmrClient.normalize_id(raw)[0] + num)
        except ValueError:
            normalized.append(raw)

    # 需先删除的本地旧语言版本（同一作品只保留 1 个版本）
    remove_ids: list[str] = []
    seen_remove: set[str] = set()
    for raw in (data.get("remove_ids") or [])[:100]:
        raw = str(raw).strip()
        if not raw:
            continue
        try:
            _, num = api.AsmrClient.normalize_id(raw)
            rid = api.AsmrClient.normalize_id(raw)[0] + num
        except ValueError:
            rid = raw.upper()
        if rid in seen_remove or rid in normalized:
            continue
        seen_remove.add(rid)
        remove_ids.append(rid)
    for rid in remove_ids:
        _purge_local_work(db, cfg, rid)

    # 语言版本更新：中文版复用旧版文件；旧版等中文版校验通过后再删除
    def _norm_id(raw) -> str:
        val = str(raw).strip()
        if not val:
            return ""
        try:
            prefix, num = api.AsmrClient.normalize_id(val)
            return prefix + num
        except ValueError:
            return val.upper()

    reuse_from: dict[str, str] = {}
    raw_reuse = data.get("reuse_from") or {}
    if isinstance(raw_reuse, dict):
        for new_raw, old_raw in list(raw_reuse.items())[:100]:
            new_id, old_id = _norm_id(new_raw), _norm_id(old_raw)
            if not new_id or not old_id or new_id == old_id:
                continue
            if new_id not in normalized:
                continue
            old_work = db.get_work(old_id) or {}
            if not old_work.get("local_path"):
                continue
            reuse_from[new_id] = old_id

    purge_after: list[str] = []
    for raw in (data.get("purge_after") or [])[:100]:
        rid = _norm_id(raw)
        if rid and rid in reuse_from.values() and rid not in purge_after:
            purge_after.append(rid)

    filter_options = data.get("filter") or {}
    if not isinstance(filter_options, dict):
        filter_options = {}
    else:
        filter_options = dict(filter_options)
    if reuse_from:
        filter_options["reuse_from"] = reuse_from
        filter_options["purge_after"] = purge_after
        filter_options.setdefault("source", "lang_update")

    task_id = db.create_task(normalized, filter_options)

    # 覆盖列表：这些作品会先删除本地旧文件再重新下载
    overwrite: set[str] = set()
    for raw in (data.get("overwrite") or []):
        raw = str(raw).strip()
        try:
            _, num = api.AsmrClient.normalize_id(raw)
            overwrite.add(api.AsmrClient.normalize_id(raw)[0] + num)
        except ValueError:
            overwrite.add(raw.upper())

    _launch_task(task_id, normalized, cfg, db, overwrite)

    return jsonify({"task_id": task_id, "status": "pending", "work_ids": normalized})


# 语言版本详情缓存：下载前复核候选关系，避免重复请求上游
_lang_info_cache: dict[str, tuple[float, dict]] = {}
_lang_info_lock = threading.Lock()
_LANG_INFO_TTL = 600


@download_bp.route("/download/lang-info", methods=["POST"])
def download_lang_info():
    """POST /api/download/lang-info  body: {"ids": [...]}

    只读查询作品的语言版本关系，用于本地库旧记录兜底与切换前复核。
    """
    data = request.get_json(silent=True) or {}
    raw_ids = data.get("ids") or []
    if not isinstance(raw_ids, list) or not raw_ids:
        return jsonify({"items": []})

    cfg = current_app.config["APP_CONFIG"]
    now = time.time()
    items: list[dict] = []
    todo: list[str] = []
    for raw in raw_ids[:50]:
        sid = str(raw).strip()
        if not sid:
            continue
        with _lang_info_lock:
            hit = _lang_info_cache.get(sid)
        if hit and now - hit[0] < _LANG_INFO_TTL:
            items.append(hit[1])
        else:
            todo.append(sid)

    if todo:
        from web.routes.search import _make_client
        client = None
        try:
            client = _make_client(cfg)
            client.login()
        except Exception:
            client = None
        for sid in todo:
            if client is None:
                items.append({"id": sid, "error": "client_unavailable"})
                continue
            try:
                num = api.AsmrClient.normalize_id(sid)[1]
                parsed = _parse_work(client.work_info(num))
                with _lang_info_lock:
                    _lang_info_cache[sid] = (time.time(), parsed)
                items.append(parsed)
            except Exception as e:
                items.append({"id": sid, "error": str(e)})
    return jsonify({"items": items})


@download_bp.route("/download/tasks", methods=["GET"])
def list_download_tasks():
    status_filter = request.args.get("status")
    db = current_app.config["DB"]
    tasks = [_reconcile_task(t) for t in db.list_tasks(status_filter)]
    return jsonify({"tasks": tasks})


@download_bp.route("/download/tasks/<int:task_id>", methods=["GET"])
def get_download_task(task_id: int):
    db = current_app.config["DB"]
    task = _reconcile_task(db.get_task(task_id))
    if not task:
        return jsonify({"error": "task_not_found"}), 404
    # 附加当前文件进度
    work_id = task.get("current_work")
    if work_id:
        task["files"] = db.get_work_files(work_id)
    return jsonify(task)


@download_bp.route("/download/tasks/<int:task_id>/pause", methods=["POST"])
def pause_download_task(task_id: int):
    """暂停：正在下载的文件下完后停止，未开始的文件不再下载。"""
    db = current_app.config["DB"]
    task = _reconcile_task(db.get_task(task_id))
    if not task:
        return jsonify({"error": "task_not_found"}), 404
    if task["status"] not in ("running", "pending"):
        return jsonify({"task_id": task_id, "status": task["status"]})
    with _running_tasks_lock:
        rt = _running_tasks.get(task_id)
        if rt and rt.get("pause_event") is not None:
            rt["pause_event"].set()
    if rt and rt.get("pause_event") is not None:
        # 条件更新：worker 可能已抢先写入 paused/done/failed，不能覆盖终态
        db.update_task_if(task_id, "pausing", ("running", "pending"))
    else:
        # worker 不在内存（如服务重启过），直接标记暂停
        db.update_task_if(task_id, "paused", ("running", "pending", "pausing"))
    latest = _reconcile_task(db.get_task(task_id)) or task
    return jsonify({"task_id": task_id, "status": latest["status"]})


@download_bp.route("/download/tasks/<int:task_id>/resume", methods=["POST"])
def resume_download_task(task_id: int):
    """继续已暂停的任务：已下完的文件自动跳过，半成品走 Range 断点续传。"""
    db = current_app.config["DB"]
    task = _reconcile_task(db.get_task(task_id))
    if not task:
        return jsonify({"error": "task_not_found"}), 404
    if task["status"] != "paused":
        return jsonify({"task_id": task_id, "status": task["status"]})
    cfg = current_app.config["APP_CONFIG"]
    launched = _launch_task(
        task_id, task.get("work_ids") or [], cfg, db,
        task_updates={"status": "pending", "error": None},
    )
    if not launched:
        # 旧 worker 尚未退出（pausing 中），等它置 paused 后再继续
        return jsonify({"task_id": task_id, "status": "pausing",
                        "error": "worker_still_stopping"}), 409
    return jsonify({"task_id": task_id, "status": "running"})


@download_bp.route("/download/tasks/<int:task_id>/retry", methods=["POST"])
def retry_download_task(task_id: int):
    """把失败任务按队列中保留的作品原地重试，复用原任务 ID。"""
    db = current_app.config["DB"]
    task = db.get_task(task_id)
    if not task:
        return jsonify({"error": "task_not_found"}), 404
    if task["status"] != "failed":
        return jsonify({"task_id": task_id, "status": task["status"],
                        "error": "invalid_state"}), 409

    original = task.get("work_ids") or []
    data = request.get_json(silent=True) or {}
    raw_ids = data.get("ids") if "ids" in data else original
    ids = list(dict.fromkeys(
        str(raw).strip().upper() for raw in (raw_ids or []) if str(raw).strip()
    ))
    if not ids:
        return jsonify({"error": "no_ids"}), 400
    if any(sid not in original for sid in ids):
        return jsonify({"error": "ids_not_in_original_task"}), 400

    # 已在其他任务中排队/下载中的作品不再重复下载，避免两个任务同时写同一批文件
    active_ids: set[str] = set()
    for t in db.list_tasks(None):
        if t.get("id") == task_id:
            continue
        if t.get("status") not in ("pending", "running", "pausing"):
            continue
        tids = t.get("work_ids") or []
        cur = t.get("current_work")
        start = tids.index(cur) if cur in tids else 0
        active_ids.update(tids[start:])

    # 已完整下载（校验通过）的跳过；不完整的保留，交给 worker 断点续传
    downloading: list[str] = []
    downloaded: list[str] = []
    todo: list[str] = []
    for sid in ids:
        if sid in active_ids:
            downloading.append(sid)
            continue
        w = db.get_work(sid) or {}
        report = w.get("integrity_report") or {}
        if (w.get("local_path") and os.path.isdir(w["local_path"])
                and report.get("status") == "complete"):
            downloaded.append(sid)
            continue
        todo.append(sid)

    if not todo:
        return jsonify({"task_id": task_id, "status": task["status"], "work_ids": [],
                        "skipped": {"downloading": downloading,
                                    "downloaded": downloaded}})
    ids = todo

    cfg = current_app.config["APP_CONFIG"]
    launched = _launch_task(
        task_id, ids, cfg, db,
        task_updates={
            "work_ids": json.dumps(ids),
            "status": "pending",
            "current_work": None,
            "progress_json": "{}",
            "error": None,
            "started_at": None,
            "finished_at": None,
        },
    )
    if not launched:
        return jsonify({"task_id": task_id, "status": task["status"],
                        "error": "task_active"}), 409
    return jsonify({"task_id": task_id, "status": "pending", "work_ids": ids,
                    "skipped": {"downloading": downloading,
                                "downloaded": downloaded}})


@download_bp.route("/download/tasks/<int:task_id>", methods=["DELETE"])
def cancel_download_task(task_id: int):
    """取消已暂停任务，并删除当前未完成作品；其他作品不受影响。"""
    db = current_app.config["DB"]
    task = _reconcile_task(db.get_task(task_id))
    if not task:
        return jsonify({"error": "task_not_found"}), 404
    if task["status"] == "cancelled":
        return jsonify({"task_id": task_id, "cancelled": True, "deleted_work": None})
    if task["status"] != "paused":
        return jsonify({"error": "pause_required", "status": task["status"],
                        "message": "请先暂停任务，再取消并删除当前未完成作品"}), 409
    with _running_tasks_lock:
        if task_id in _running_tasks:
            return jsonify({"error": "worker_still_stopping",
                            "message": "任务仍在停止，请稍后再取消"}), 409

    current_work = task.get("current_work")
    if current_work:
        try:
            _delete_unfinished_work(db, current_app.config["APP_CONFIG"], current_work)
        except Exception as e:
            return jsonify({"error": "delete_failed", "detail": str(e)}), 500
    db.cancel_task(task_id)
    return jsonify({"task_id": task_id, "cancelled": True,
                    "deleted_work": current_work})


# --------------------------------------------------------------------------
# 销量数据定时刷新：超过 7 天未更新的作品，每小时尝试刷新一次
# --------------------------------------------------------------------------

_dl_refresh_started = False
_dl_refresh_log = logging.getLogger("asmr.dl_refresh")


def _refresh_stale_dl_counts(db, cfg: dict, *, days: int = 7,
                             limit: int = 80) -> int:
    """逐个刷新过期作品的销量；单个失败不写时间戳，下轮自动重试。"""
    rows = db.works_for_dl_refresh(days=days, limit=limit)
    if not rows:
        return 0
    from web.routes.search import _make_client
    client = _make_client(cfg)
    client.login()
    updated = 0
    for row in rows:
        sid = str(row.get("source_id") or "")
        if not sid:
            continue
        try:
            num = api.AsmrClient.normalize_id(sid)[1]
            info = client.work_info(num)
            if info.get("dl_count") is None:
                continue
            parsed = _parse_work(info)
            db.set_work_dl_refresh(sid, info.get("dl_count"),
                                   parsed.get("rating"), parsed.get("price"),
                                   parsed.get("age_level"))
            updated += 1
        except Exception:
            continue
        time.sleep(0.4)
    return updated


def start_dl_refresh_scheduler(db, cfg: dict) -> None:
    """后台线程：启动 1 分钟后首轮刷新，之后每小时检查一次。"""
    global _dl_refresh_started
    if _dl_refresh_started:
        return
    _dl_refresh_started = True

    def loop():
        time.sleep(60)
        while True:
            try:
                _refresh_stale_dl_counts(db, cfg)
            except Exception as e:
                _dl_refresh_log.warning("销量刷新失败：%s", e)
            time.sleep(3600)

    threading.Thread(target=loop, name="dl-count-refresh", daemon=True).start()

