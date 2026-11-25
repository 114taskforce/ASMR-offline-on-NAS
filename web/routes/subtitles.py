"""字幕生成 API — VAD + Haas-ja 识别 → 大模型翻译 → 双语 VTT。

生成文件写入作品根目录下的 `生成字幕/{日文版,中文版,双语版}/`，文件名以
类别前缀开头（如 `双语_RJ01426915_01_本編.vtt`）。实际任务由
web.subtitles.SubtitleEngine 的后台队列执行，接口只负责入队、查询进度和设置。
"""
from __future__ import annotations

import os
import time

from flask import Blueprint, current_app, jsonify, request

from web.subtitles import apischedule, layout
from web.subtitles import asmr_translator as asr_engine

subtitles_bp = Blueprint("subtitles", __name__)

GENERATED_DIR = layout.GENERATED_DIR
ZH_VTT = layout.LEGACY_SUFFIX
_ACTIVE = ("queued", "running", "paused", "pending_translate")

_SETTING_KEYS = (
    "api_base", "api_key", "api_model", "api_timeout", "api_thinking",
    "drop_generated_chinese", "asr_threads", "model_idle_minutes",
    "vad_threshold", "vad_min_speech", "vad_min_silence",
    "asr_min_conf", "batch_max_minutes", "api_offpeak",
)

MIN_MEM_GB = 4.0


def _mem_total_gb() -> float:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3
    except (AttributeError, ValueError, OSError):
        return 0.0


def _mem_ok() -> bool:
    """生成字幕的硬性要求：本机总内存 ≥ 4GB。"""
    return _mem_total_gb() >= MIN_MEM_GB


def _cfg() -> dict:
    return current_app.config.get("APP_CONFIG") or {}


def _engine():
    from web.subtitles import get_engine
    return get_engine(current_app)


def _settings_payload() -> dict:
    sub = _cfg().get("subtitle") or {}
    out = {k: sub.get(k) for k in _SETTING_KEYS}
    out["api_key"] = ""
    out["api_key_set"] = bool((sub.get("api_key") or "").strip())
    level = str(sub.get("api_thinking") or "").strip().lower()
    if level not in ("off", "low", "medium", "high"):
        # 兼容旧配置里的「关闭思考」开关
        level = "off" if sub.get("api_disable_thinking", True) else "medium"
    out["api_thinking"] = level
    out["drop_generated_chinese"] = sub.get("drop_generated_chinese") or "keep"
    out["asr_threads"] = sub.get("asr_threads") or 4
    out["asr_workers"] = asr_engine.asr_workers(_cfg())
    out["asr_workers_max"] = asr_engine.worker_limit()
    out["cpu_cores"] = os.cpu_count() or 1
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    out["mem_total_gb"] = round(int(line.split()[1]) / 1048576, 1)
                    break
    except OSError:
        pass
    out["vad_threshold"] = sub.get("vad_threshold") or 0.4
    out["vad_min_speech"] = sub.get("vad_min_speech") or 0.25
    out["vad_min_silence"] = sub.get("vad_min_silence") or 0.2
    out["asr_min_conf"] = sub.get("asr_min_conf") or 0.12
    out["batch_max_minutes"] = sub.get("batch_max_minutes") or 60
    out["model_idle_minutes"] = sub.get("model_idle_minutes", 10)
    out["api_offpeak"] = apischedule.get_schedule(_cfg())
    out["mem_ok"] = _mem_ok()
    out["mem_total_gb"] = round(_mem_total_gb(), 1)
    out["min_mem_gb"] = MIN_MEM_GB
    return out


@subtitles_bp.route("/subtitles/settings", methods=["GET"])
def get_settings():
    return jsonify(_settings_payload())


@subtitles_bp.route("/subtitles/settings", methods=["POST"])
def save_settings():
    from web.config import save_config

    cfg = _cfg()
    sub = cfg.setdefault("subtitle", {})
    body = request.get_json(silent=True) or {}

    for key in ("api_base", "api_model"):
        if key in body:
            sub[key] = str(body.get(key) or "").strip()
    if body.get("api_key"):
        sub["api_key"] = str(body["api_key"]).strip()
    if str(body.get("api_thinking") or "") in ("off", "low", "medium", "high"):
        sub["api_thinking"] = body["api_thinking"]
    if body.get("asr_workers") is not None:
        try:
            n = int(body["asr_workers"])
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            sub["asr_workers"] = max(1, min(n, asr_engine.worker_limit()))
    for key, cast in (("api_timeout", float), ("asr_threads", int),
                      ("vad_threshold", float), ("vad_min_speech", float),
                      ("vad_min_silence", float), ("asr_min_conf", float),
                      ("batch_max_minutes", int), ("model_idle_minutes", int)):
        if key in body:
            try:
                sub[key] = cast(body[key])
            except (TypeError, ValueError):
                pass
    if body.get("drop_generated_chinese") in ("ask", "keep", "auto_drop"):
        sub["drop_generated_chinese"] = body["drop_generated_chinese"]
    if isinstance(body.get("api_offpeak"), dict):
        raw = body["api_offpeak"]
        sub["api_offpeak"] = {
            "enabled": bool(raw.get("enabled")),
            "windows": apischedule.normalize_windows(raw.get("windows")),
        }

    try:
        save_config(cfg)
    except Exception as ex:
        return jsonify({"error": "save_failed", "detail": str(ex)[:200]}), 500
    return jsonify(_settings_payload())


@subtitles_bp.route("/subtitles/test", methods=["POST"])
def test_settings():
    """用表单里的地址/模型/Key 发一次最小请求，验证翻译 API 是否可用。"""
    cfg = _cfg()
    sub = dict(cfg.get("subtitle") or {})
    body = request.get_json(silent=True) or {}
    for key in ("api_base", "api_model"):
        if str(body.get(key) or "").strip():
            sub[key] = str(body[key]).strip()
    if str(body.get("api_key") or "").strip():
        sub["api_key"] = str(body["api_key"]).strip()
    if not (sub.get("api_base") and sub.get("api_model") and sub.get("api_key")):
        return jsonify({"error": "not_configured",
                        "detail": "请先填写 API 地址、模型和 Key"}), 400

    probe = dict(cfg)
    probe["subtitle"] = sub
    started = time.time()
    try:
        reply = asr_engine._llm_chat(
            probe, [{"role": "user", "content": "ping"}], timeout=20.0)
    except Exception as ex:
        return jsonify({"error": "request_failed", "detail": str(ex)[:200]}), 502
    return jsonify({
        "ok": True,
        "ms": int((time.time() - started) * 1000),
        "reply": (reply or "").strip()[:80],
    })


@subtitles_bp.route("/subtitles/engine/overview", methods=["GET"])
def engine_overview():
    ov = _engine().overview()
    jobs = current_app.config["DB"].list_subtitle_jobs(30)
    ov["recent_jobs"] = jobs
    ov["running_jobs"] = [j for j in jobs if j["status"] in _ACTIVE]
    ov["mem_ok"] = _mem_ok()
    ov["mem_total_gb"] = round(_mem_total_gb(), 1)
    return jsonify(ov)


@subtitles_bp.route("/subtitles/engine/clear", methods=["POST"])
def engine_clear():
    return jsonify(_engine().clear_generated())


@subtitles_bp.route("/subtitles/engine/pause", methods=["POST"])
def engine_pause():
    """暂停整个生成器：当前任务立即停下，队列里的任务不再启动。"""
    return jsonify({"paused": _engine().pause_engine()})


@subtitles_bp.route("/subtitles/engine/resume", methods=["POST"])
def engine_resume():
    """恢复生成：把被暂停的任务放回队列继续。"""
    return jsonify({"resumed": _engine().resume_engine()})


@subtitles_bp.route("/subtitles/works", methods=["GET"])
def work_status_list():
    """「生成字幕」抽屉数据：生成中 / 待生成（队列） / 已生成 / 未生成 / 已有官方中字。"""
    db = current_app.config["DB"]
    works, _ = db.list_works(page=1, page_size=100000, local_only=True)

    latest_asr: dict[str, dict] = {}
    latest_tr: dict[str, dict] = {}
    for job in db.list_subtitle_jobs(300):
        target = latest_tr if job.get("mode") == "translate" else latest_asr
        target.setdefault(job["work_id"], job)

    running, translating, pending, queue, generated, missing, official = \
        [], [], [], [], [], [], []
    for w in works:
        lp = w.get("local_path") or ""
        if not lp or not os.path.isdir(lp):
            continue
        # 以实际目录为准：DB 里的 generated_zh 可能是旧文件留下的过期标记
        gen = layout.has_translated(lp)
        has_official = bool(w.get("has_subtitle"))
        w["has_generated_zh"] = gen
        w["subtitle_status"] = (
            "both" if gen and has_official else
            "generated_only" if gen else
            "official_only" if has_official else "none"
        )
        job = latest_asr.get(w["source_id"])
        job_tr = latest_tr.get(w["source_id"])
        status = (job or {}).get("status")
        tr_status = (job_tr or {}).get("status")
        queuedish = status in ("queued", "paused", "pending_translate")
        # 逐音轨比对：还有音轨只有日文 → 待翻译；所有参与翻译的音轨都有中字才算已完成
        state = layout.analyze_tracks(
            lp, w["source_id"], set())  # 待翻译判定不服从音轨勾选
        buffered = state["buffered"]
        complete = state["complete"]
        # 翻译线程正在跑/被暂停的任务单独放「翻译中」，识别任务留在「生成中」；
        # 同一作品可以同时出现在两栏（双线程并行）
        busy = False
        if tr_status in ("running", "paused") and (
                tr_status == "running" or (job_tr or {}).get("started_at")):
            item = dict(w)
            item["job"] = job_tr
            translating.append(item)
            busy = True
        if status in ("running", "paused") and (
                status == "running" or (job or {}).get("started_at")):
            item = dict(w)
            item["job"] = job
            running.append(item)
            busy = True
        if busy:
            continue
        if job:
            w["job"] = job
        if queuedish:
            queue.append(w)
        # 仍有文件只生成了日文 → 同时挂到「待翻译」
        if buffered:
            pending.append(w)
        elif not queuedish:
            if complete:
                generated.append(w)
            elif has_official:
                official.append(w)
            else:
                missing.append(w)

    queue.sort(key=lambda x: float((x.get("job") or {}).get("sort_order") or 0))
    pending.sort(key=lambda x: float((x.get("job") or {}).get("sort_order") or 0))
    missing.sort(key=lambda x: x.get("release_date") or "", reverse=True)
    generated.sort(key=lambda x: x.get("release_date") or "", reverse=True)
    official.sort(key=lambda x: x.get("release_date") or "", reverse=True)
    cfg = _cfg()
    open_at = apischedule.next_open_at(cfg)
    return jsonify({
        "groups": {"running": running, "translating": translating,
                   "pending": pending, "queue": queue,
                   "generated": generated, "missing": missing,
                   "official": official},
        "counts": {"running": len(running), "translating": len(translating),
                   "pending": len(pending), "queue": len(queue),
                   "generated": len(generated), "missing": len(missing),
                   "official": len(official)},
        "translate": {
            "api_ready": bool(asr_engine.api_ready(cfg)),
            "blocked": apischedule.is_api_blocked(cfg),
            "open_at": open_at.strftime("%H:%M") if open_at else "",
            "waiting": len(pending),
            "active": len(translating),
        },
    })


def _api_configured() -> bool:
    sub = _cfg().get("subtitle") or {}
    return bool((sub.get("api_key") or "").strip())


def _fully_generated(work: dict) -> bool:
    """作品是否已完整生成（所有日文都有对应译文）。

    部分生成的作品（还有音轨缺字幕或缺译文）允许重新加入生成队列，
    引擎会自动跳过已完成的音轨组，只补缺失的部分。
    """
    root = work.get("local_path") or ""
    if not root:
        return False
    state = layout.analyze_tracks(
        root, work.get("source_id"), set())
    return bool(state["complete"])


_API_HINT = "请先在 设置 → 密钥令牌 填写翻译 API"


@subtitles_bp.route("/subtitles/work/<source_id>", methods=["POST"])
def submit_work(source_id: str):
    if not _mem_ok():
        return jsonify({"error": "insufficient_memory",
                        "detail": f"本机总内存不足 {MIN_MEM_GB:g}GB，生成字幕功能不可用"}), 400
    if not _api_configured():
        return jsonify({"error": "api_not_configured", "detail": _API_HINT}), 400
    db = current_app.config["DB"]
    work = db.get_work(source_id)
    if not work or not work.get("local_path"):
        return jsonify({"error": "work_not_local"}), 404
    if work.get("has_subtitle"):
        return jsonify({"error": "has_official_subtitle",
                        "detail": "该作品已有中文版，无需生成字幕"}), 400
    if _fully_generated(work):
        return jsonify({"error": "has_generated_subtitle",
                        "detail": "该作品已完整生成中文字幕"}), 400
    job = _engine().submit_work(source_id, "work")
    return jsonify({"ok": True, "job": job})


@subtitles_bp.route("/subtitles/queue", methods=["POST"])
def enqueue_works():
    """把勾选的作品加入生成队列（按勾选顺序排到队尾）。"""
    if not _mem_ok():
        return jsonify({"error": "insufficient_memory",
                        "detail": f"本机总内存不足 {MIN_MEM_GB:g}GB，生成字幕功能不可用"}), 400
    if not _api_configured():
        return jsonify({"error": "api_not_configured", "detail": _API_HINT}), 400
    body = request.get_json(silent=True) or {}
    ids = [str(x) for x in (body.get("ids") or []) if x]
    db = current_app.config["DB"]
    ok_ids, skipped = [], []
    for wid in ids:
        work = db.get_work(wid)
        if not work:
            skipped.append({"id": wid, "reason": "not_found"})
            continue
        if work.get("has_subtitle"):
            skipped.append({"id": wid, "reason": "has_official"})
            continue
        root = work.get("local_path") or ""
        if root and _fully_generated(work):
            skipped.append({"id": wid, "reason": "has_generated"})
            continue
        ok_ids.append(wid)
    jobs = _engine().enqueue_works(ok_ids) if ok_ids else []
    return jsonify({"queued": len(jobs), "jobs": jobs, "skipped": skipped})


@subtitles_bp.route("/subtitles/queue/cancel", methods=["POST"])
def cancel_queue():
    """把勾选的「待生成」作品从队列移除。"""
    body = request.get_json(silent=True) or {}
    ids = [str(x) for x in (body.get("ids") or []) if x]
    n = current_app.config["DB"].cancel_queued_jobs(ids)
    return jsonify({"removed": n})


@subtitles_bp.route("/subtitles/queue/order", methods=["POST"])
def reorder_queue():
    """按前端拖动后的顺序重写队列优先级。"""
    body = request.get_json(silent=True) or {}
    order = [int(x) for x in (body.get("order") or [])]
    db = current_app.config["DB"]
    for idx, job_id in enumerate(order):
        db.set_job_sort_order(job_id, idx + 1)
    return jsonify({"ok": True, "count": len(order)})


@subtitles_bp.route("/subtitles/tracks/<source_id>", methods=["GET"])
def get_tracks(source_id: str):
    """播放页逐音轨翻译开关：缺省全部参与翻译，官方中字作品不提供开关。"""
    db = current_app.config["DB"]
    work = db.get_work(source_id)
    if not work:
        return jsonify({"error": "not_found"}), 404
    root = work.get("local_path") or ""
    if not os.path.isdir(root):
        return jsonify({"official": bool(work.get("has_subtitle")), "tracks": []})

    # 以文件系统为准，和生成引擎扫描到的相对路径严格一致
    from web.subtitles import AUDIO_EXTS
    from web.subtitles.sources import collect_work_tracks

    prefs = db.get_track_prefs(source_id)
    tracks: list[dict] = []
    durations = {r["rel_path"]: float(r["duration"] or 0)
                 for r in db.list_audio_index(source_id)}
    # 同 stem 的格式副本合并为一行：勾选控制整组是否参与翻译
    for group in collect_work_tracks(root, AUDIO_EXTS, durations=durations, probe=True):
        primary = group[0]
        has_pref = any(rel in prefs for rel in group)
        # 已生成字幕 / 已有日文版（待翻译）的组默认不勾选，避免重复调用 API；
        # 用户手动勾选过则沿用其偏好
        generated = any(layout.exists_translated(root, source_id, rel)
                        for rel in group)
        buffered = any(layout.exists_ja(root, source_id, rel) for rel in group)
        enabled = (all(prefs.get(rel, True) for rel in group) if has_pref
                   else not (generated or buffered))
        tracks.append({
            "rel_path": primary,
            "filename": os.path.basename(primary),
            "translate": enabled,
            "has_ja": buffered,
            "has_zh": generated,
            "count": len(group),
        })
    return jsonify({
        "official": bool(work.get("has_subtitle")),
        "tracks": tracks,
    })


@subtitles_bp.route("/subtitles/tracks/<source_id>", methods=["POST"])
def set_track(source_id: str):
    body = request.get_json(silent=True) or {}
    rel = str(body.get("rel_path") or "").replace("\\", "/")
    if not rel:
        return jsonify({"error": "missing_rel_path"}), 400
    db = current_app.config["DB"]
    translate = bool(body.get("translate"))
    # 勾选按组生效：同 stem 的 WAV/MP3/SE 变体一并写入
    rels = [rel]
    work = db.get_work(source_id)
    root = (work or {}).get("local_path") or ""
    if root and os.path.isdir(root):
        from web.subtitles import AUDIO_EXTS
        from web.subtitles.sources import collect_work_tracks
        durations = {r["rel_path"]: float(r["duration"] or 0)
                     for r in db.list_audio_index(source_id)}
        for group in collect_work_tracks(root, AUDIO_EXTS,
                                         durations=durations, probe=True):
            if rel in group:
                rels = group
                break
    for item in rels:
        db.set_track_translate(source_id, item, translate)
    return jsonify({"ok": True, "paths": rels})


@subtitles_bp.route("/subtitles/batch", methods=["POST"])
def submit_batch():
    if not _api_configured():
        return jsonify({"error": "api_not_configured", "detail": _API_HINT}), 400
    body = request.get_json(silent=True) or {}
    queued = _engine().submit_batch(
        scope=str(body.get("scope") or "missing"),
        max_minutes=int(body.get("max_minutes") or 60),
        limit=int(body.get("limit") or 0),
    )
    return jsonify({"queued": queued})


@subtitles_bp.route("/subtitles/batch/<action>", methods=["POST"])
def batch_control(action: str):
    eng = _engine()
    fn = {"pause": eng.pause_batch, "resume": eng.resume_batch,
          "cancel": eng.cancel_batch}.get(action)
    if not fn:
        return jsonify({"error": "bad_action"}), 400
    return jsonify({"affected": fn()})


@subtitles_bp.route("/subtitles/jobs", methods=["GET"])
def list_jobs():
    limit = min(int(request.args.get("limit", 20)), 200)
    jobs = current_app.config["DB"].list_subtitle_jobs(limit)
    return jsonify({"jobs": jobs})


@subtitles_bp.route("/subtitles/jobs/<int:job_id>/<action>", methods=["POST"])
def job_control(job_id: int, action: str):
    eng = _engine()
    fn = {"pause": eng.pause, "resume": eng.resume,
          "cancel": eng.cancel}.get(action)
    if not fn:
        return jsonify({"error": "bad_action"}), 400
    fn(job_id)
    return jsonify({"ok": True})


# ---------------- 兼容旧接口 ----------------

@subtitles_bp.route("/subtitles/ja/<source_id>", methods=["POST"])
def gen_ja(source_id: str):
    """兼容旧调用：不再同步阻塞，改为入队。"""
    return submit_work(source_id)


@subtitles_bp.route("/subtitles/ja/overview", methods=["GET"])
def ja_overview():
    return engine_overview()


@subtitles_bp.route("/subtitles/ja/<source_id>/files", methods=["GET"])
def list_generated_files(source_id: str):
    db = current_app.config["DB"]
    work = db.get_work(source_id)
    if not work or not work.get("local_path"):
        return jsonify({"error": "work_not_local"}), 404
    root = work["local_path"]
    out: list[dict] = []
    for item in layout.scan(root):
        try:
            sz = os.path.getsize(item["local_path"])
            mt = os.path.getmtime(item["local_path"])
        except OSError:
            continue
        out.append({
            "name": item["name"],
            "category": item["cat"],
            "path": item["rel_path"],
            "size": sz,
            "mtime": mt,
        })
    return jsonify({"files": out})
