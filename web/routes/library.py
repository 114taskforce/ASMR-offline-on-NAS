"""本地资源库 API — 列出已下载作品、删除、扫描修复、流式播放"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint, current_app, jsonify, request, send_file, abort
from werkzeug.security import check_password_hash

import asmr_client as api
from web.config import storage_entries

library_bp = Blueprint("library", __name__)

# 作品总时长缓存：local_path -> {sig, total, tracks}
_dur_cache: dict[str, dict] = {}


def _ffprobe_seconds(path: str) -> float:
    """读取单个音频文件的时长（秒），失败返回 0。"""
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=20,
        )
        return float((p.stdout or "").strip() or 0)
    except Exception:
        return 0.0


def _work_audio_files(root: str, limit: int = 800) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() in AUDIO_EXTS:
                out.append(os.path.join(dirpath, name))
        if len(out) >= limit:
            break
    return out[:limit]


def _measure_work_duration(root: str) -> tuple[float, int]:
    """同名声轨（wav/mp3、SE 有/无）去重后求和，按目录缓存。返回 (总秒数, 音轨数)。"""
    files = _work_audio_files(root)
    if not files:
        return 0.0, 0

    try:
        sig = f"{len(files)}:{sum(os.path.getsize(f) for f in files)}"
    except OSError:
        sig = f"{len(files)}:-"
    cached = _dur_cache.get(root)
    if cached and cached.get("sig") == sig:
        return cached["total"], cached["tracks"]

    unique: dict[str, str] = {}
    for f in sorted(files):
        unique.setdefault(os.path.splitext(os.path.basename(f))[0].lower(), f)
    paths = list(unique.values())

    with ThreadPoolExecutor(max_workers=4) as ex:
        secs = list(ex.map(_ffprobe_seconds, paths))
    total = float(sum(secs))
    _dur_cache[root] = {"sig": sig, "total": total, "tracks": len(paths)}
    return total, len(paths)


@library_bp.route("/library/duration/<source_id>", methods=["GET"])
def work_duration(source_id: str):
    """作品总时长：同名声轨（wav/mp3、SE 有/无）去重后求和，按目录缓存。"""
    db = current_app.config["DB"]
    w = db.get_work(source_id)
    root = (w or {}).get("local_path") or ""
    if not w or not root or not os.path.isdir(root):
        return jsonify({"error": "not_local"}), 404

    cached = float(w.get("duration") or 0) > 0
    total, tracks = _measure_work_duration(root)
    if total > 0 and not cached:
        db.set_work_duration(source_id, total)
    return jsonify({"total": total, "tracks": tracks, "cached": cached})


@library_bp.route("/library/durations", methods=["GET"])
def work_durations():
    """批量返回作品总时长（秒），供作品卡片展示；结果写入 works.duration 缓存。"""
    db = current_app.config["DB"]
    raw = (request.args.get("ids") or "").strip()
    ids = [x.strip() for x in raw.split(",") if x.strip()][:40]
    if not ids:
        return jsonify({"durations": {}})

    def one(sid: str) -> tuple[str, float]:
        w = db.get_work(sid)
        if not w:
            return sid, 0.0
        dur = float(w.get("duration") or 0)
        if dur > 0:
            return sid, dur
        root = w.get("local_path") or ""
        if not root or not os.path.isdir(root):
            return sid, 0.0
        total, _tracks = _measure_work_duration(root)
        if total > 0:
            db.set_work_duration(sid, total)
        return sid, total

    out: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=2) as ex:
        for sid, dur in ex.map(one, ids):
            out[sid] = round(dur, 2)
    return jsonify({"durations": out})


_backfill_running = False
_backfill_guard = threading.Lock()


def backfill_work_durations(db) -> int:
    """后台补齐所有本地作品时长。

    works.duration 原本只在卡片被渲染时惰性回写，从未打开过卡片的作品
    （例如只出现在「生成字幕」页的作品）会一直是 0，导致统计总时长漏算。
    """
    global _backfill_running
    with _backfill_guard:
        if _backfill_running:
            return 0
        _backfill_running = True
    done = 0
    try:
        works, _ = db.list_works(page=1, page_size=100000, local_only=True)
        todo: list[tuple[str, str]] = []
        for w in works:
            root = w.get("local_path") or ""
            if not root or not os.path.isdir(root):
                continue
            if float(w.get("duration") or 0) > 0:
                continue
            todo.append((w["source_id"], root))

        def one(item: tuple[str, str]) -> int:
            sid, root = item
            try:
                total, _tracks = _measure_work_duration(root)
                if total > 0:
                    db.set_work_duration(sid, total)
                return 1
            except Exception:
                return 0

        if todo:
            with ThreadPoolExecutor(max_workers=2) as ex:
                for ok in ex.map(one, todo):
                    done += ok
    finally:
        with _backfill_guard:
            _backfill_running = False
    return done


@library_bp.route("/library/works", methods=["GET"])
def list_local_works():
    """列出本地已下载的作品（从 SQLite works 表）。"""
    db = current_app.config["DB"]
    q = (request.args.get("q") or "").strip()
    order = request.args.get("order", "release")
    sort = request.args.get("sort", "desc")
    try:
        seed = int(request.args.get("seed", "0"))
    except (TypeError, ValueError):
        seed = 0
    page = max(1, int(request.args.get("page", 1)))
    raw_ps = (request.args.get("pageSize") or "50").strip().lower()
    page_size = 10000 if raw_ps == "all" else min(int(raw_ps), 100)
    has_subtitle = request.args.get("has_subtitle")
    has_generated = (request.args.get("has_generated") or "").strip()
    from_date = (request.args.get("from_date") or "").strip()
    to_date = (request.args.get("to_date") or "").strip()
    author = (request.args.get("author") or "").strip()
    tag = (request.args.get("tag") or "").strip()
    age = (request.args.get("age") or "").strip()
    exclude_age = (request.args.get("exclude_age") or "").strip()

    hs = None
    if has_subtitle and has_subtitle != "all":
        hs = int(has_subtitle)

    # 先对账：用户可能在 NAS 文件管理器里删过目录，避免 total 与实际不符
    stale, _ = db.list_works(page=1, page_size=2000, local_only=True)
    for w in stale:
        lp = w.get("local_path") or ""
        if lp and not os.path.exists(lp):
            db.update_local_work(w["source_id"], "", "", 0, 0)

    # 「无字幕」要排除已生成字幕的作品（官方无中字但自生成也算有中字）
    if hs == 0:
        all_works, _ = db.list_works(
            page=1, page_size=100000,
            order=order, sort=sort, seed=seed,
            q=q, has_subtitle=0,
            from_date=from_date, to_date=to_date,
            author=author, tag=tag, age=age, exclude_age=exclude_age,
            local_only=True,
        )
        rows = [w for w in all_works if not _decorate_sub_status(w)]
        total = len(rows)
        start = (page - 1) * page_size
        return jsonify({"works": rows[start:start + page_size],
                        "total": total, "page": page})

    # 「有生成字幕」是独立筛选：按 生成字幕/ 目录实际存在与否过滤
    if has_generated in ("1", "gen"):
        all_works, _ = db.list_works(
            page=1, page_size=100000,
            order=order, sort=sort, seed=seed,
            q=q, has_subtitle=hs,
            from_date=from_date, to_date=to_date,
            author=author, tag=tag, age=age, exclude_age=exclude_age,
            local_only=True,
        )
        rows = [w for w in all_works if _decorate_sub_status(w)]
        total = len(rows)
        start = (page - 1) * page_size
        return jsonify({"works": rows[start:start + page_size],
                        "total": total, "page": page})

    works, total = db.list_works(
        page=page, page_size=page_size,
        order=order, sort=sort, seed=seed,
        q=q, has_subtitle=hs,
        from_date=from_date, to_date=to_date,
        author=author, tag=tag, age=age, exclude_age=exclude_age,
        local_only=True,
    )

    for w in works:
        _decorate_sub_status(w)

    return jsonify({"works": works, "total": total, "page": page})


def _decorate_sub_status(w: dict) -> bool:
    """补中字状态：official=官方黄色，generated=自生成蓝色；返回是否有自生成字幕。"""
    lp = w.get("local_path") or ""
    gen = bool(w.get("generated_zh"))
    if lp and not gen:
        from web.subtitles import layout
        gen = layout.has_translated(lp)
    official = bool(w.get("has_subtitle"))
    w["has_generated_zh"] = gen
    w["subtitle_status"] = (
        "both" if official and gen else
        "official_only" if official else
        "generated_only" if gen else "none"
    )
    return gen


@library_bp.route("/library/works/<source_id>", methods=["GET"])
def get_local_work(source_id: str):
    db = current_app.config["DB"]
    w = db.get_work(source_id)
    if not w:
        return jsonify({"error": "not_found"}), 404
    files = db.get_work_files(source_id)
    return jsonify({"work": w, "files": files})


@library_bp.route("/library/works/<source_id>/verify", methods=["POST"])
def verify_local_work(source_id: str):
    """按远程清单和文件大小校验单个本地作品。"""
    from web.routes.download import verify_work_integrity
    report = verify_work_integrity(
        current_app.config["DB"], current_app.config["APP_CONFIG"], source_id)
    if report.get("error") == "work_not_found":
        return jsonify(report), 404
    return jsonify(report)


# ---------------- 全部完整性检查并补齐 ----------------

_verify_all_lock = threading.Lock()
_verify_all_state: dict = {
    "running": False,
    "total": 0,
    "checked": 0,
    "complete": 0,
    "incomplete": 0,
    "error_count": 0,
    "current": "",
    "started_at": None,
    "finished_at": None,
    "repair_task_id": None,
    "message": "",
    "cancelled": False,
}


def _remove_mismatched_files(db, source_id: str) -> int:
    """删除大小不符的旧文件，避免续传拼出错误文件；路径限制在作品目录内。"""
    w = db.get_work(source_id) or {}
    base = w.get("local_path") or ""
    if not base or not os.path.isdir(base):
        return 0
    root = os.path.realpath(base)
    removed = 0
    for item in (w.get("integrity_report") or {}).get("size_mismatch") or []:
        rel = str(item.get("path") or "")
        if not rel:
            continue
        target = os.path.realpath(os.path.join(root, *rel.split("/")))
        if not target.startswith(root + os.sep):
            continue
        try:
            if os.path.isfile(target):
                os.remove(target)
                removed += 1
        except OSError:
            continue
    return removed


def _verify_all_worker(db, cfg: dict) -> None:
    """顺序校验全部本地作品，并把不完整的作品交给下载任务自动补齐。"""
    from web.routes.download import (_filter_entries, _launch_task,
                                     verify_work_integrity)
    from web.routes.search import _make_client

    repair_ids: list[str] = []
    repair_files: dict[str, list[str]] = {}
    try:
        works, _ = db.list_works(page=1, page_size=100000, local_only=True)
        # 目录已在外部删除的记录先清空，避免整部作品被当作缺失而全量重下
        for w in works:
            lp = w.get("local_path") or ""
            if lp and not os.path.exists(lp):
                db.update_local_work(w["source_id"], "", "", 0, 0)
        works = [w for w in works if w.get("local_path")]

        with _verify_all_lock:
            _verify_all_state.update(total=len(works), checked=0, complete=0,
                                     incomplete=0, error_count=0, current="")

        client = _make_client(cfg)
        client.login()

        cancelled = False
        for w in works:
            sid = w["source_id"]
            with _verify_all_lock:
                if _verify_all_state["cancelled"]:
                    cancelled = True
                    break
                _verify_all_state["current"] = sid
            try:
                num = api.AsmrClient.normalize_id(sid)[1]
                entries = _filter_entries(
                    api.AsmrClient.flatten(client.tracks(num)), cfg)
                report = verify_work_integrity(db, cfg, sid, entries)
            except Exception:
                with _verify_all_lock:
                    _verify_all_state["checked"] += 1
                    _verify_all_state["error_count"] += 1
                continue
            status = report.get("status")
            with _verify_all_lock:
                _verify_all_state["checked"] += 1
                if status == "complete":
                    _verify_all_state["complete"] += 1
                elif status == "incomplete":
                    _verify_all_state["incomplete"] += 1
                    # 只记录缺失/大小不符的文件，补齐时按文件定向重下
                    paths = [str(x.get("path")) for x in
                             (report.get("missing") or [])
                             + (report.get("size_mismatch") or [])
                             if x.get("path")]
                    if paths:
                        repair_files[sid] = paths
                        repair_ids.append(sid)
                else:
                    _verify_all_state["error_count"] += 1

        # 已在下载队列中的作品不再重复补齐，避免两个任务同时写同一批文件
        active_works: set[str] = set()
        for t in db.list_tasks(None):
            if t.get("status") in ("pending", "running", "pausing", "paused"):
                active_works.update(t.get("work_ids") or [])
        repair_ids = [sid for sid in repair_ids if sid not in active_works]

        if repair_ids and not cancelled:
            for sid in repair_ids:
                _remove_mismatched_files(db, sid)
            task_id = db.create_task(repair_ids, {
                "source": "integrity_rebuild",
                "repair_files": {sid: repair_files[sid] for sid in repair_ids},
            })
            _launch_task(task_id, repair_ids, cfg, db)
            with _verify_all_lock:
                _verify_all_state["repair_task_id"] = task_id
    except Exception as e:
        with _verify_all_lock:
            _verify_all_state["message"] = f"检查失败：{e}"
    finally:
        with _verify_all_lock:
            if _verify_all_state["cancelled"]:
                _verify_all_state["message"] = (
                    f"已取消比对：已检查 {_verify_all_state['checked']}/"
                    f"{_verify_all_state['total']}")
            _verify_all_state["running"] = False
            _verify_all_state["current"] = ""
            _verify_all_state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")


@library_bp.route("/library/verify-all", methods=["GET"])
def get_verify_all():
    """查询全部检查任务的进度。"""
    with _verify_all_lock:
        return jsonify(dict(_verify_all_state))


@library_bp.route("/library/verify-all", methods=["POST"])
def start_verify_all():
    """启动/取消全部完整性检查；不完整的作品自动创建定向补齐任务。"""
    db = current_app.config["DB"]
    cfg = current_app.config["APP_CONFIG"]
    body = request.get_json(silent=True) or {}
    if body.get("action") == "cancel":
        with _verify_all_lock:
            if _verify_all_state["running"]:
                _verify_all_state["cancelled"] = True
            return jsonify(dict(_verify_all_state))
    with _verify_all_lock:
        if _verify_all_state["running"]:
            return jsonify(dict(_verify_all_state)), 409
        _verify_all_state.update(
            running=True, total=0, checked=0, complete=0, incomplete=0,
            error_count=0, current="", repair_task_id=None, message="",
            cancelled=False,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%S"), finished_at=None,
        )
    threading.Thread(target=_verify_all_worker, args=(db, cfg), daemon=True).start()
    return jsonify({"running": True})


# --------------------------------------------------------------------------
# 中文版审计：扫描无中文字幕的本地作品，列出可更新的中文版候选
# --------------------------------------------------------------------------

_lang_audit_lock = threading.Lock()
_lang_audit_state: dict = {
    "running": False,
    "total": 0,
    "checked": 0,
    "found": 0,
    "dropped": 0,
    "error_count": 0,
    "current": "",
    "candidates": [],
    "started_at": None,
    "finished_at": None,
    "message": "",
    "cancelled": False,
}


def _drop_generated_subtitles(db, source_id: str) -> bool:
    """删除作品「生成字幕」目录下的自生成字幕并清除蓝色中字标记。"""
    w = db.get_work(source_id) or {}
    root = w.get("local_path") or ""
    removed = False
    gdir = os.path.join(root, "生成字幕") if root else ""
    if gdir and os.path.isdir(gdir):
        import shutil
        shutil.rmtree(gdir, ignore_errors=True)
        removed = True
    if removed or w.get("generated_zh"):
        db.set_work_generated_zh(source_id, 0)
    return removed


def _lang_audit_worker(db, cfg: dict) -> None:
    """顺序查询本地无字幕作品的语言版本，并估算最小下载量。"""
    from web.routes.download import (_build_link_map, _entry_key,
                                     _filter_entries, _index_local_files)
    from web.routes.search import _is_zh, _make_client, _parse_work

    try:
        works, _ = db.list_works(page=1, page_size=100000,
                                 has_subtitle=0, local_only=True)
        all_local, _ = db.list_works(page=1, page_size=100000, local_only=True)
        local_ids = {w["source_id"] for w in all_local if w.get("local_path")}
        for w in works:
            lp = w.get("local_path") or ""
            if lp and not os.path.exists(lp):
                db.update_local_work(w["source_id"], "", "", 0, 0)
        works = [w for w in works if w.get("local_path")]

        # 官方中文出现时是否丢弃自生成字幕（设置来自「生成字幕」面板）
        policy = str((cfg.get("subtitle") or {}).get("drop_generated_chinese") or "keep")
        if policy == "auto_drop":
            for w in all_local:
                if w.get("generated_zh") and w.get("has_subtitle"):
                    if _drop_generated_subtitles(db, w["source_id"]):
                        with _lang_audit_lock:
                            _lang_audit_state["dropped"] += 1

        # 已在下载队列中的作品不再重复推荐，避免两个任务同时写同一批文件
        active_works: set[str] = set()
        for t in db.list_tasks(None):
            if t.get("status") in ("pending", "running", "pausing", "paused"):
                active_works.update(t.get("work_ids") or [])

        with _lang_audit_lock:
            _lang_audit_state.update(total=len(works), checked=0, found=0,
                                     error_count=0, current="", candidates=[])

        client = _make_client(cfg)
        client.login()

        for w in works:
            sid = w["source_id"]
            with _lang_audit_lock:
                if _lang_audit_state["cancelled"]:
                    break
                _lang_audit_state["current"] = sid
            try:
                num = api.AsmrClient.normalize_id(sid)[1]
                parsed = _parse_work(client.work_info(num))
                if _is_zh(parsed.get("lang") or ""):
                    with _lang_audit_lock:
                        _lang_audit_state["checked"] += 1
                    continue
                zh = parsed.get("chinese_edition")
                new_id = str((zh or {}).get("source_id") or "")
                if not new_id or new_id in local_ids or new_id in active_works:
                    with _lang_audit_lock:
                        _lang_audit_state["checked"] += 1
                    continue

                new_num = api.AsmrClient.normalize_id(new_id)[1]
                entries = _filter_entries(
                    api.AsmrClient.flatten(client.tracks(new_num)), cfg)
                old_base = w.get("local_path") or ""
                link_map = _build_link_map(entries, _index_local_files(old_base))
                download_files = sum(
                    1 for e in entries if _entry_key(e) not in link_map)
                download_bytes = sum(
                    max(0, int(e.size or 0)) for e in entries
                    if _entry_key(e) not in link_map)
                candidate = {
                    "old_id": sid,
                    "old_title": w.get("title") or parsed.get("title") or "",
                    "old_lang_label": parsed.get("lang_label") or "",
                    "old_cover": f"/api/cover/{sid}?v=2",
                    "new_id": new_id,
                    "new_title": zh.get("title") or "",
                    "new_cover": zh.get("cover_url") or f"/api/cover/{new_id}?v=2",
                    "total_files": len(entries),
                    "reuse_files": len(link_map),
                    "download_files": download_files,
                    "download_bytes": download_bytes,
                }
                if w.get("generated_zh"):
                    candidate["has_generated"] = True
                    if policy == "auto_drop":
                        dropped = _drop_generated_subtitles(db, sid)
                        candidate["generated_dropped"] = dropped
                        if dropped:
                            with _lang_audit_lock:
                                _lang_audit_state["dropped"] += 1
                with _lang_audit_lock:
                    _lang_audit_state["checked"] += 1
                    _lang_audit_state["found"] += 1
                    _lang_audit_state["candidates"].append(candidate)
            except Exception:
                with _lang_audit_lock:
                    _lang_audit_state["checked"] += 1
                    _lang_audit_state["error_count"] += 1
                continue
            time.sleep(0.3)
    except Exception as e:
        with _lang_audit_lock:
            _lang_audit_state["message"] = f"检查失败：{e}"
    finally:
        with _lang_audit_lock:
            if _lang_audit_state["cancelled"]:
                _lang_audit_state["message"] = (
                    f"已取消：已检查 {_lang_audit_state['checked']}/"
                    f"{_lang_audit_state['total']}")
            _lang_audit_state["running"] = False
            _lang_audit_state["current"] = ""
            _lang_audit_state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")


@library_bp.route("/library/lang-audit", methods=["GET"])
def get_lang_audit():
    """查询中文版审计进度与候选清单。"""
    with _lang_audit_lock:
        return jsonify(dict(_lang_audit_state))


@library_bp.route("/library/lang-audit", methods=["POST"])
def start_lang_audit():
    """启动/取消中文版审计。"""
    db = current_app.config["DB"]
    cfg = current_app.config["APP_CONFIG"]
    body = request.get_json(silent=True) or {}
    if body.get("action") == "cancel":
        with _lang_audit_lock:
            if _lang_audit_state["running"]:
                _lang_audit_state["cancelled"] = True
            return jsonify(dict(_lang_audit_state))
    with _lang_audit_lock:
        if _lang_audit_state["running"]:
            return jsonify(dict(_lang_audit_state)), 409
        _lang_audit_state.update(
            running=True, total=0, checked=0, found=0, dropped=0, error_count=0,
            current="", candidates=[], message="", cancelled=False,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%S"), finished_at=None,
        )
    threading.Thread(target=_lang_audit_worker, args=(db, cfg), daemon=True).start()
    return jsonify({"running": True})


@library_bp.route("/library/works/<source_id>", methods=["DELETE"])
def delete_local_work(source_id: str):
    """删除本地文件 + 清理数据库（需系统设置中的删除密码）。"""
    db = current_app.config["DB"]
    cfg = current_app.config["APP_CONFIG"]
    hashed = cfg.get("delete_password_hash")
    if not hashed:
        return jsonify({"error": "no_password",
                        "message": "请先在系统设置中设置删除密码"}), 400
    data = request.get_json(silent=True) or {}
    pw = str(data.get("password") or request.headers.get("X-Delete-Password") or "")
    if not check_password_hash(hashed, pw):
        return jsonify({"error": "bad_password", "message": "密码错误"}), 401

    w = db.get_work(source_id)
    if not w:
        return jsonify({"error": "not_found"}), 404

    # 删除本地目录
    local_path = w.get("local_path", "")
    deleted_files = 0
    if local_path and os.path.exists(local_path):
        import shutil
        try:
            shutil.rmtree(local_path)
            deleted_files = 1
        except Exception as e:
            return jsonify({"error": "delete_failed", "detail": str(e)}), 500

    db.delete_work(source_id)
    return jsonify({"deleted": True, "path": local_path})


@library_bp.route("/library/local-check", methods=["POST"])
def local_check():
    """批量检查作品是否已下载到本地（供下载前冲突提示使用）。"""
    db = current_app.config["DB"]
    data = request.get_json(silent=True) or {}
    out = []
    for raw in (data.get("ids") or []):
        sid = str(raw).strip().upper()
        if not sid:
            continue
        w = db.get_work(sid)
        if w and w.get("local_path") and os.path.isdir(w["local_path"]):
            out.append({
                "source_id": sid,
                "file_count": w.get("file_count") or 0,
                "total_size": w.get("total_size") or 0,
            })
    return jsonify({"local": out})


@library_bp.route("/library/scan", methods=["POST"])
def scan_library():
    """扫描所有存储根，对比 SQLite 记录，修复不一致。"""
    db = current_app.config["DB"]
    cfg = current_app.config["APP_CONFIG"]

    scan_count = 0
    removed_count = 0

    # 1. 检查数据库里的本地作品是否还存在
    works, _ = db.list_works(page=1, page_size=10000, local_only=True)
    for w in works:
        local_path = w.get("local_path", "")
        if local_path and not os.path.exists(local_path):
            db.update_local_work(w["source_id"], "", "", 0, 0)
            removed_count += 1

    # 2. 扫描各存储根，发现新目录
    roots = [r["path"] for r in storage_entries(cfg)]

    new_works = 0
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for entry in os.listdir(root):
            full = os.path.join(root, entry)
            if not os.path.isdir(full):
                continue
            # 目录名格式: RJ01426915-2025-10-22-nosub-title...
            # 尝试匹配 RJ/VJ 等 ID
            import re
            m = re.match(r"^([A-Z]+\d+)", entry.upper())
            if not m:
                continue
            source_id = m.group(1)
            scan_count += 1
            # 如果数据库里没有，加一条
            if not db.get_work(source_id):
                files = [f for f in os.listdir(full) if os.path.isfile(os.path.join(full, f))]
                total_size = sum(os.path.getsize(os.path.join(full, f)) for f in files)
                db.upsert_work({
                    "source_id": source_id,
                    "title": entry,
                    "release_date": "",
                    "tags": [],
                    "tags_circle": [],
                    "tags_va": [],
                    "has_subtitle": 0,
                })
                db.update_local_work(source_id, full, root, len(files), total_size)
                new_works += 1

    return jsonify({
        "scanned_dirs": scan_count,
        "removed_stale": removed_count,
        "newly_found": new_works,
    })


@library_bp.route("/library/downloads", methods=["GET"])
def list_storage_roots():
    """列出所有存储根及其用量。"""
    cfg = current_app.config["APP_CONFIG"]
    roots = [r["path"] for r in storage_entries(cfg)]

    out = []
    for p in roots:
        info = {"path": p, "exists": os.path.isdir(p)}
        if os.path.isdir(p):
            stat = os.statvfs(p)
            total = stat.f_frsize * stat.f_blocks
            free = stat.f_frsize * stat.f_bavail
            info.update({
                "total": total,
                "free": free,
                "used": total - free,
            })
        out.append(info)
    return jsonify({"roots": out})


# ---------------- 文件浏览 ----------------

AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}
SUB_EXTS = {".vtt", ".srt", ".ass", ".ssa", ".lrc", ".sub", ".smi", ".txt"}

MIME_MAP = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac",
    ".m4a": "audio/mp4", ".aac": "audio/aac", ".ogg": "audio/ogg", ".opus": "audio/opus",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska",
    ".mov": "video/quicktime", ".m4v": "video/mp4",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    ".vtt": "text/vtt", ".srt": "text/plain", ".ass": "text/plain",
    ".lrc": "text/plain", ".txt": "text/plain", ".json": "application/json",
}


def _kind(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in SUB_EXTS:
        return "subtitle"
    return "other"


MAX_TREE_DEPTH = 8
MAX_TREE_NODES = 6000


def _build_tree(root: str, rel: str = "", depth: int = 0,
                budget: dict | None = None) -> list[dict]:
    """构建目录树（最多 8 层，节点总数设上限，避免异常目录拖垮页面）。"""
    if budget is None:
        budget = {"left": MAX_TREE_NODES}
    if depth > MAX_TREE_DEPTH or budget["left"] <= 0:
        return []

    base = os.path.join(root, rel) if rel else root
    try:
        entries = os.listdir(base)
    except OSError:
        return []

    dirs, files = [], []
    for name in entries:
        if name.startswith("."):
            continue
        full = os.path.join(base, name)
        my_rel = f"{rel}/{name}" if rel else name
        if os.path.isdir(full) and not os.path.islink(full):
            budget["left"] -= 1
            node = {"name": name, "type": "dir", "rel": my_rel}
            node["children"] = (_build_tree(root, my_rel, depth + 1, budget)
                                if budget["left"] > 0 else [])
            dirs.append(node)
        elif os.path.isfile(full):
            budget["left"] -= 1
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            files.append({
                "name": name, "type": "file", "rel": my_rel,
                "size": size, "kind": _kind(name),
                "ext": os.path.splitext(name)[1].lower(),
            })

    dirs.sort(key=lambda x: x["name"].lower())
    files.sort(key=lambda x: x["name"].lower())
    return dirs + files


@library_bp.route("/library/tree/<source_id>", methods=["GET"])
def get_work_tree(source_id: str):
    """返回本地作品的文件树，供前端文件浏览器使用。"""
    db = current_app.config["DB"]
    w = db.get_work(source_id)
    if not w or not w.get("local_path") or not os.path.isdir(w["local_path"]):
        return jsonify({"error": "not_local"}), 404

    tree = _build_tree(w["local_path"])

    # 统计各类文件数，供前端显示入口
    counts = {"audio": 0, "image": 0, "video": 0, "subtitle": 0, "other": 0}

    def walk(nodes):
        for n in nodes:
            if n["type"] == "dir":
                walk(n.get("children") or [])
            else:
                counts[n["kind"]] += 1

    walk(tree)
    return jsonify({
        "source_id": source_id,
        "title": w.get("title") or source_id,
        "cover_url": f"/api/cover/{source_id}?v=2",
        "tree": tree,
        "counts": counts,
        "integrity_status": w.get("integrity_status") or "",
        "integrity_checked_at": w.get("integrity_checked_at"),
        "integrity_report": w.get("integrity_report") or {},
    })


@library_bp.route("/library/raw/<source_id>", methods=["GET"])
def stream_relative_file(source_id: str):
    """按相对路径流式返回作品目录内的任意文件（图片/视频/音频/字幕）。"""
    db = current_app.config["DB"]
    w = db.get_work(source_id)
    if not w or not w.get("local_path"):
        abort(404)

    rel = (request.args.get("rel") or "").strip().lstrip("/")
    if not rel:
        abort(400)

    root = os.path.realpath(w["local_path"])
    target = os.path.realpath(os.path.join(root, rel))
    if not (target == root or target.startswith(root + os.sep)):
        abort(403)
    if not os.path.isfile(target):
        abort(404)

    ext = os.path.splitext(target)[1].lower()
    return send_file(target, mimetype=MIME_MAP.get(ext, "application/octet-stream"),
                     conditional=True, as_attachment=False)


# ---------------- 流式播放 ----------------

@library_bp.route("/library/file/<source_id>", methods=["GET"])
def stream_local_file(source_id: str):
    """
    GET /api/library/file/<source_id>?file=xxx.mp3
    流式返回本地文件（支持 HTTP Range），供 HTML5 audio/video 播放。
    """
    db = current_app.config["DB"]
    w = db.get_work(source_id)
    if not w or not w.get("local_path"):
        abort(404)

    files = db.get_work_files(source_id)
    file_name = request.args.get("file")

    if file_name:
        # 指定文件
        matched = [f for f in files if f["filename"] == file_name]
        if not matched:
            abort(404)
        local_path = matched[0]["local_path"]
    else:
        # 默认返回第一个音频文件
        audio_exts = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"}
        audio_files = [f for f in files
                       if os.path.splitext(f["filename"])[1].lower() in audio_exts
                       and f["status"] == "downloaded"]
        if not audio_files:
            audio_files = [f for f in files if f["status"] == "downloaded"]
        if not audio_files:
            abort(404)
        local_path = audio_files[0]["local_path"]

    if not os.path.exists(local_path):
        abort(404)

    # MIME
    ext = os.path.splitext(local_path)[1].lower()
    mime_map = {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
        ".opus": "audio/opus",
        ".mp4": "video/mp4",
        ".webm": "video/webm",
    }
    mimetype = mime_map.get(ext, "application/octet-stream")

    # send_file with conditional=True 支持 Range 请求（拖动进度条）
    return send_file(
        local_path,
        mimetype=mimetype,
        conditional=True,
        as_attachment=False,
    )
