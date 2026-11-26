"""字幕生成引擎 — 台本优先 + asmr-translator 兼容模式 ASR + 本地翻译 + 后台队列。"""
from __future__ import annotations

import gc
import os
import re
import shutil
import threading
import time
import traceback

from . import apischedule, layout, scripts, vtt, sources
from . import asmr_translator as asr_engine  # ASMR VAD + Haas-ja（纯 onnxruntime）
from .fingerprint import fingerprint_audio, has_fpcalc, normalize_stem, probe_duration

AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"}
SUB_EXTS = {".srt", ".vtt", ".ass", ".ssa", ".sub", ".smi", ".lrc"}
MAX_DEPTH = 8
ZH_VTT = layout.LEGACY_SUFFIX
GENERATED_DIR = layout.GENERATED_DIR
PENDING_TRANSLATE = "pending_translate"


# --------------------------------------------------------------------------
# 文件扫描工具
# --------------------------------------------------------------------------

def collect_audio(root: str) -> list[str]:
    """返回作品目录下所有音频相对路径（有序）。"""
    out: list[str] = []
    if not root or not os.path.isdir(root):
        return out
    base = os.path.realpath(root)
    for cur, dirs, files in os.walk(base):
        rel_dir = os.path.relpath(cur, base)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        if depth > MAX_DEPTH:
            dirs[:] = []
            continue
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() in AUDIO_EXTS:
                full = os.path.join(cur, name)
                out.append(os.path.relpath(full, base).replace("\\", "/"))
    out.sort(key=lambda r: [int(x) if x.isdigit() else x.lower()
                            for x in re.split(r"(\d+)", r)])
    return out


def _scan_local_subtitles(root: str, audio_rels: list[str]) -> dict[str, str]:
    """为每个音频找最匹配的本地字幕（排除本工具生成的 .zh.vtt）。"""
    by_name: dict[str, str] = {}
    by_stem: dict[str, str] = {}
    for a in audio_rels:
        by_name[os.path.basename(a).lower()] = a
        by_stem[normalize_stem(a)] = a

    best: dict[str, tuple[float, str]] = {}
    for cur, dirs, files in os.walk(root):
        rel_dir = os.path.relpath(cur, root)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        if depth > MAX_DEPTH:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if os.path.splitext(name)[1].lower() not in SUB_EXTS:
                continue
            if name.lower().endswith(ZH_VTT):
                continue
            full = os.path.join(cur, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            # 本工具的输出目录不算「本地已有字幕」
            if rel.startswith(GENERATED_DIR + "/"):
                continue
            base = os.path.splitext(name)[0]
            target, score = "", 0.0
            if base.lower() in by_name:
                target, score = by_name[base.lower()], 0.95
            elif normalize_stem(base) in by_stem:
                target, score = by_stem[normalize_stem(base)], 0.9
            else:
                s = normalize_stem(name)
                for stem, arel in by_stem.items():
                    if s and stem and (s in stem or stem in s):
                        if 0.7 > score:
                            target, score = arel, 0.7
                # 同目录兜底
                if not target:
                    same = [a for a in audio_rels
                            if os.path.dirname(a) == os.path.dirname(rel)]
                    if len(same) == 1:
                        target, score = same[0], 0.4
            if target and score > best.get(target, (0.0, ""))[0]:
                best[target] = (score, rel)
    return {k: v[1] for k, v in best.items() if v[0] >= 0.4}


def _read_cues(path: str) -> tuple[list[dict], str]:
    try:
        text = vtt.read_text(path)
    except OSError:
        return [], "und"
    cues = vtt.normalize_cues(vtt.parse_any(text, os.path.splitext(path)[1]))
    return cues, vtt.lang_of(" ".join(c["text"] for c in cues))


def _cues_lang(cues: list[dict]) -> str:
    """cue 语言标记：中文原样用，其余（含无法判断）按日文交给翻译。"""
    text = " ".join(c.get("text") or "" for c in cues)
    return "zh" if vtt.lang_of(text) == "zh" else "ja"


# --------------------------------------------------------------------------
# 引擎
# --------------------------------------------------------------------------

class _PauseNow(Exception):
    """暂停信号：在 ASR / 翻译的中途抛出，用于立刻退回暂停状态。"""


class SubtitleEngine:
    def __init__(self, db, get_cfg):
        self.db = db
        self._get_cfg = get_cfg
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None        # ASR 线程
        self._tr_worker: threading.Thread | None = None     # 翻译线程
        self._stop = threading.Event()
        self._paused: set[int] = set()
        self._cancelled: set[int] = set()
        self._running_asr: int | None = None
        self._running_tr: int | None = None
        # 引擎级暂停：两个线程都停下，队列里的其它任务也不再启动
        self._engine_paused = False
        self._overview_cache: tuple[float, dict] = (0.0, {})
        self._last_translate_scan = 0.0
        self._tr_failed_at: dict[str, float] = {}
        # 空闲卸载模型：是否已释放
        self._models_released = False

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        with self._lock:
            self._stop.clear()
            if not (self._worker and self._worker.is_alive()):
                self._worker = threading.Thread(
                    target=self._loop_asr, name="subtitle-asr", daemon=True)
                self._worker.start()
            if not (self._tr_worker and self._tr_worker.is_alive()):
                self._tr_worker = threading.Thread(
                    target=self._loop_translate, name="subtitle-translate",
                    daemon=True)
                self._tr_worker.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop_asr(self) -> None:
        try:
            os.nice(10)
        except (AttributeError, OSError):
            pass
        try:
            while not self._stop.is_set():
                try:
                    if self._engine_paused:
                        # 暂停期间不取新任务，避免切换到队列里的其它作品
                        time.sleep(1.0)
                        continue
                    job = self.db.next_queued_subtitle_job()
                    if not job:
                        self._maybe_unload_idle()
                        time.sleep(2.0)
                        continue
                    self._models_released = False
                    self._run_job(job)
                except Exception:
                    traceback.print_exc()
                    time.sleep(3.0)
        finally:
            print("[subtitle-engine] ASR worker 退出", flush=True)

    def _loop_translate(self) -> None:
        """翻译线程：与识别并行，把「日文版」里积压的音轨译成中文/双语。"""
        try:
            while not self._stop.is_set():
                try:
                    if self._engine_paused:
                        time.sleep(1.0)
                        continue
                    if apischedule.is_api_blocked(self._get_cfg()):
                        # 禁用时段内不发起任何翻译请求，等时段结束再继续
                        time.sleep(5.0)
                        continue
                    if not asr_engine.api_ready(self._get_cfg()):
                        # 未配置翻译 API 时不反复创建必然失败的任务
                        time.sleep(5.0)
                        continue
                    job = self.db.next_queued_translate_job()
                    # 队列非空时也定期扫描：ASR 新缓冲的作品要按发布日期
                    # 插进队列，不能等队列清空才被发现
                    self._ensure_translate_jobs()
                    job = job or self.db.next_queued_translate_job()
                    if not job:
                        time.sleep(5.0)
                        continue
                    self._models_released = False
                    self._run_translate_job(job)
                except Exception:
                    traceback.print_exc()
                    time.sleep(3.0)
        finally:
            print("[subtitle-engine] 翻译 worker 退出", flush=True)

    def _maybe_unload_idle(self) -> None:
        """队列空闲立即释放 VAD/ASR 模型内存；0 表示不卸载（常驻内存）。"""
        sub = self._get_cfg().get("subtitle") or {}
        try:
            idle_on = float(sub.get("model_idle_minutes", 10)) > 0
        except (TypeError, ValueError):
            idle_on = True
        if not idle_on or self._models_released:
            return
        asr_engine.unload_models()
        self._models_released = True

    def _ensure_translate_jobs(self) -> None:
        """日文版里有识别结果、但还没翻译的作品，自动排一个翻译任务。"""
        now = time.monotonic()
        if now - self._last_translate_scan < 30:
            return
        self._last_translate_scan = now
        works, _ = self.db.list_works(page=1, page_size=100000, local_only=True)
        for w in works:
            root = w.get("local_path") or ""
            wid = w["source_id"]
            if not root:
                continue
            # 对应音频已删除的日文 vtt 无法再翻译，顺手清掉保持完整性
            durs = {r["rel_path"]: float(r["duration"] or 0)
                    for r in self.db.list_audio_index(wid)}
            if layout.cleanup_orphan_ja(root, wid, durations=durs):
                print(f"[subtitle-engine] 清理孤儿日文 {wid}", flush=True)
            # 逐个文件比对：日文版里还有没生成中文的才入队（跳过用户取消勾选的音轨）
            if not layout.has_pending_translation(root, wid, set(), durations=durs):
                continue
            if self.db.active_subtitle_job(wid, "translate"):
                continue
            failed_at = self._tr_failed_at.get(wid)
            if failed_at and time.monotonic() - failed_at < 900:
                # 刚失败过的作品冷却 15 分钟再重试，避免反复打 API
                continue
            self.db.create_subtitle_job(wid, "translate")
            print(f"[subtitle-engine] 待翻译入队 {wid}", flush=True)

    # ---------------- 控制 ----------------

    def pause(self, job_id: int) -> None:
        job = self.db.get_subtitle_job(job_id)
        if not job or job["status"] in ("done", "failed", "cancelled"):
            return
        self._paused.add(job_id)
        if job["status"] in ("queued", PENDING_TRANSLATE):
            self.db.update_subtitle_job(job_id, status="paused", message="已暂停")

    def _pause_requested(self, job_id: int) -> bool:
        return job_id in self._paused

    def pause_engine(self) -> int:
        """暂停整个生成器：识别与翻译线程当前任务立刻停下。"""
        self._engine_paused = True
        ids = [i for i in (self._running_asr, self._running_tr) if i]
        for job_id in ids:
            self._paused.add(job_id)
        return ids[0] if ids else 0

    def resume_engine(self) -> int:
        """恢复生成：先清掉暂停标记，再把被暂停的任务插到队首继续。"""
        self._engine_paused = False
        jobs = self.db.list_subtitle_jobs(100)
        paused = sorted([j for j in jobs if j["status"] == "paused"],
                        key=lambda j: j["id"])
        if not paused:
            return 0
        queued = [j for j in jobs if j["status"] == "queued"]
        base = min([float(j.get("sort_order") or 0) for j in queued], default=1.0)
        for i, job in enumerate(paused):
            self._paused.discard(job["id"])
            # 恢复的任务优先于已排队任务，保证接着被打断的工作继续
            self.db.update_subtitle_job(
                job["id"], status="queued",
                sort_order=base - 0.001 * (len(paused) - i),
                message="等待执行")
        return len(paused)

    def resume(self, job_id: int) -> None:
        job = self.db.get_subtitle_job(job_id)
        if not job or job["status"] in ("done", "failed", "cancelled"):
            return
        self._paused.discard(job_id)
        self._cancelled.discard(job_id)
        self.db.update_subtitle_job(job_id, status="queued", message="等待执行")

    def cancel(self, job_id: int) -> None:
        job = self.db.get_subtitle_job(job_id)
        if not job or job["status"] in ("done", "failed", "cancelled"):
            return
        self._cancelled.add(job_id)
        self._paused.discard(job_id)
        if job["status"] in ("queued", "paused", PENDING_TRANSLATE):
            self.db.update_subtitle_job(job_id, status="cancelled",
                                        message="已取消", finished_at=_now())
        # 引擎停在被取消的任务上时解除暂停，避免两个线程一直空转
        if self._engine_paused and not any(
                j["status"] == "paused" for j in self.db.list_subtitle_jobs(200)):
            self._engine_paused = False

    def pause_batch(self) -> int:
        n = 0
        for job in self.db.list_subtitle_jobs(500):
            if job["status"] in ("queued", "running", PENDING_TRANSLATE):
                self.pause(job["id"])
                n += 1
        return n

    def resume_batch(self) -> int:
        n = 0
        for job in self.db.list_subtitle_jobs(500):
            if job["status"] == "paused":
                self.resume(job["id"])
                n += 1
        return n

    def cancel_batch(self) -> int:
        n = 0
        for job in self.db.list_subtitle_jobs(500):
            if job["status"] in ("queued", "running", "paused", PENDING_TRANSLATE):
                self.cancel(job["id"])
                n += 1
        return n

    def _check_control(self, job_id: int) -> str:
        """返回 continue / pause / cancel。"""
        if job_id in self._cancelled:
            return "cancel"
        if job_id in self._paused:
            return "pause"
        job = self.db.get_subtitle_job(job_id)
        if not job:
            return "cancel"
        if job["status"] == "cancelled":
            return "cancel"
        if job["status"] == "paused":
            return "pause"
        return "continue"

    # ---------------- 提交 ----------------

    def submit_work(self, work_id: str, mode: str = "work") -> dict:
        active = self.db.active_subtitle_job(work_id)
        if active:
            return active
        job_id = self.db.create_subtitle_job(work_id, mode)
        self.start()
        return self.db.get_subtitle_job(job_id) or {"id": job_id, "work_id": work_id}

    def enqueue_works(self, work_ids: list[str]) -> list[dict]:
        """按给定顺序把作品追加到队列尾部（已排队的直接复用）。"""
        jobs: list[dict] = []
        for wid in work_ids:
            work = self.db.get_work(wid)
            if not work or not work.get("local_path"):
                continue
            active = self.db.active_subtitle_job(wid)
            if active:
                if active["status"] in ("queued", "paused"):
                    jobs.append(active)
                continue
            job_id = self.db.create_subtitle_job(wid, "queue")
            jobs.append(self.db.get_subtitle_job(job_id)
                        or {"id": job_id, "work_id": wid, "status": "queued"})
        if jobs:
            self.start()
        return jobs

    def submit_batch(self, *, scope: str = "missing", max_minutes: int = 60,
                     limit: int = 0) -> int:
        cfg = self._get_cfg()
        max_seconds = max_minutes * 60 if max_minutes else 0
        works = self._local_works()
        queued = 0
        for w in works:
            if limit and queued >= limit:
                break
            if max_seconds and (w.get("duration") or 0) > max_seconds:
                continue
            if self.db.active_subtitle_job(w["source_id"]):
                continue
            root = w.get("local_path") or ""
            if not os.path.isdir(root):
                continue
            disabled = self.db.disabled_tracks(w["source_id"])
            audios = [a for a in collect_audio(root) if a not in disabled]
            if not audios:
                continue
            if scope == "missing" and not any(
                    not layout.exists_translated(root, w["source_id"], a)
                    for a in audios):
                continue
            self.db.create_subtitle_job(w["source_id"], "batch", len(audios))
            queued += 1
        if queued:
            self.start()
        return queued

    def _local_works(self) -> list[dict]:
        out = []
        page = 1
        while True:
            batch, total = self.db.list_works(page=page, page_size=200,
                                              order="duration", sort="asc",
                                              local_only=True)
            out.extend(batch)
            if len(out) >= total or not batch:
                break
            page += 1
        return [w for w in out if w.get("local_path")]

    # ---------------- 任务执行 ----------------

    def _run_job(self, job: dict) -> None:
        job_id = job["id"]
        work_id = job["work_id"]
        cfg = self._get_cfg()
        self._running_asr = job_id
        self.db.update_subtitle_job(job_id, status="running",
                                    started_at=_now(), message="准备中")
        try:
            self._execute(job_id, work_id, cfg)
        except Exception as ex:
            traceback.print_exc()
            self.db.update_subtitle_job(job_id, status="failed", error=str(ex),
                                        message="执行失败", finished_at=_now())
        finally:
            self._running_asr = None
            self._paused.discard(job_id)
            self._cancelled.discard(job_id)
            gc.collect()

    def _run_translate_job(self, job: dict) -> None:
        job_id = job["id"]
        work_id = job["work_id"]
        self._running_tr = job_id
        self.db.update_subtitle_job(job_id, status="running", route="translate",
                                    started_at=_now(), message="准备翻译")
        try:
            self._execute_translate(job_id, work_id, self._get_cfg())
        except Exception as ex:
            traceback.print_exc()
            self.db.update_subtitle_job(job_id, status="failed", error=str(ex),
                                        message="翻译失败", finished_at=_now())
        finally:
            self._running_tr = None
            self._paused.discard(job_id)
            self._cancelled.discard(job_id)
            done = self.db.get_subtitle_job(job_id) or {}
            if done.get("status") == "failed":
                self._tr_failed_at[done.get("work_id") or work_id] = time.monotonic()
            gc.collect()

    # ── 辅助：三类字幕输出 ──────────────────────────────────────────────
    def _emit(self, root: str, work_id: str, group: list[str], text: str,
              *, category: str | None = None) -> None:
        """把一份字幕按类别写入 日文版 / 中文版 / 双语版。

        category 为空时按内容自动判断：含「日文\\n中文」的行视为双语，
        同时拆出纯日文与纯中文各存一份。
        """
        primary = group[0]
        text = text or ""
        if not text.strip():
            return
        if category:
            layout.write(root, work_id, primary, category, text)
        else:
            pair = vtt.split_bilingual(text)
            if pair:
                layout.write(root, work_id, primary, "bi", text)
                layout.write(root, work_id, primary, "ja", pair[0])
                layout.write(root, work_id, primary, "zh", pair[1])
            else:
                layout.write(root, work_id, primary, "zh", text)
        layout.cleanup_group(root, work_id, group)

    def _translate_ja(self, cues: list[dict], cfg: dict,
                      work_id: str, session,
                      stop=None) -> list[dict]:
        """纯日文 cue → 双语 cue（text = 日文\\n中文）。"""
        merged, _route, _model = self._translate_cues(
            cues, cfg, work_id=work_id, session=session, stop=stop,
            guard=lambda: not apischedule.is_api_blocked(cfg))
        return merged

    def _execute(self, job_id: int, work_id: str, cfg: dict) -> None:
        sub = cfg.get("subtitle") or {}
        work = self.db.get_work(work_id)
        if not work or not work.get("local_path"):
            self.db.update_subtitle_job(job_id, status="failed", error="作品不在本地",
                                        message="作品不在本地", finished_at=_now())
            return
        root = work["local_path"]
        if not os.path.isdir(root):
            self.db.update_subtitle_job(job_id, status="failed", error="目录不存在",
                                        message="目录不存在", finished_at=_now())
            return

        # 旧版 <rel>.zh.vtt 迁移到「日文版/中文版/双语版」布局
        try:
            layout.migrate_work(root, work_id)
        except Exception:
            traceback.print_exc()

        # 无 SE 音源优先分组（时长取自 audio_index，缺失时 ffprobe 补测）
        groups = sources.collect_work_tracks(
            root, AUDIO_EXTS,
            durations={r["rel_path"]: float(r["duration"] or 0)
                       for r in self.db.list_audio_index(work_id)},
            probe=True)
        # 按音轨自然序号排序：保证送进同一翻译会话的顺序与作品内一致
        def _nat(rel: str):
            return [int(t) if t.isdigit() else t.lower()
                    for t in re.split(r"(\d+)", rel)]
        groups.sort(key=lambda g: _nat(g[0]))
        # 识别服从音轨勾选：整组都被取消勾选的音轨不参与 ASR。
        # 开局按当前勾选过滤一次；运行中每组开始前再读一次，弹窗里改勾选可实时生效
        prefs0 = self.db.get_track_prefs(work_id)
        if prefs0:
            groups = [g for g in groups
                      if not all(prefs0.get(rel, True) is False for rel in g)]
        if not groups:
            self.db.update_subtitle_job(
                job_id, status="done", message="没有需要识别的音频",
                progress=1.0, finished_at=_now())
            return
        audio_rels = [rel for g in groups for rel in g]  # 扁平列表用于台本/字幕映射

        # 台本扫描（和原来一样）
        script_list: list[dict] = []
        if sub.get("enable_scripts", True):
            self.db.update_subtitle_job(job_id, message="扫描台本")
            script_list = scripts.scan_scripts(
                root,
                enable_pdf=bool(sub.get("parse_pdf", True)),
                enable_docx=bool(sub.get("parse_docx", True)),
            )
            for s in script_list:
                self.db.put_script_text(
                    vtt.hash_text(s.get("text") or s.get("rel_path") or ""),
                    s.get("lang") or "und", s.get("text") or "")
            mapping = scripts.assign_scripts(script_list, audio_rels)
            for s in script_list:
                matched = next((a for a, lst in mapping.items()
                                if any(x["rel_path"] == s["rel_path"] for x in lst)), "")
                self.db.upsert_script_asset(
                    work_id, s["rel_path"],
                    vtt.hash_text(s.get("text") or s.get("rel_path") or ""),
                    s.get("lang") or "und", s.get("kind") or "plain",
                    matched, s.get("confidence") or 0, s.get("cues") or [])
        else:
            mapping = {a: [] for a in audio_rels}

        local_subs = _scan_local_subtitles(root, audio_rels)
        api_subs = self._api_subtitle_map(work_id, audio_rels, cfg) \
            if sub.get("use_api_sources", True) else {}

        total = len(groups)
        self.db.update_subtitle_job(job_id, total_tracks=total,
                                    message=f"0/{total}")
        done = 0
        reused = 0
        failed: list[str] = []

        for gi, group in enumerate(groups):
            control = self._check_control(job_id)
            if control == "cancel":
                self.db.update_subtitle_job(job_id, status="cancelled",
                                            message="已取消", finished_at=_now())
                return
            if control == "pause":
                self.db.update_subtitle_job(job_id, status="paused",
                                            message=f"已暂停 {done}/{total}")
                return
            # 实时勾选：跑任务期间在弹窗里取消勾选的组，轮到它时直接跳过
            live = self.db.get_track_prefs(work_id)
            if live and all(live.get(rel, True) is False for rel in group):
                done += 1
                self.db.update_subtitle_job(
                    job_id, done_tracks=done, route="skipped",
                    progress=done / max(1, total),
                    message=f"{done}/{total} · 已取消勾选")
                continue
            ok, reason = self._resources_ok(cfg, root, job_id, check_idle=(gi == 0))
            if not ok:
                self.db.update_subtitle_job(job_id, status="paused",
                                            message=reason,
                                            progress=done / max(1, total))
                return

            primary = group[0]
            # 中文版 / 双语版已存在 → 完成
            if layout.exists_translated(root, work_id, primary):
                layout.cleanup_group(root, work_id, group)
                done += 1
                reused += 1
                self._set_generated_flag(work_id)
                self.db.update_subtitle_job(
                    job_id, done_tracks=done, reused_tracks=reused,
                    route="skipped",
                    progress=done / max(1, total),
                    message=f"{done}/{total} · 已存在")
                continue

            # 日文版已有识别结果 → 交给翻译线程，识别任务不再重复处理
            if layout.exists_ja(root, work_id, primary):
                layout.cleanup_group(root, work_id, group)
                done += 1
                reused += 1
                self.db.update_subtitle_job(
                    job_id, done_tracks=done, reused_tracks=reused,
                    route="skipped", progress=done / max(1, total),
                    message=f"{done}/{total} · 已有日文，等待翻译")
                continue

            try:
                result = self._process_group(
                    job_id=job_id, work_id=work_id, root=root, group=group,
                    mapping=mapping, local_subs=local_subs, api_subs=api_subs,
                    cfg=cfg, force=False, base=done, total=total)
            except _PauseNow:
                self.db.update_subtitle_job(
                    job_id, status="paused",
                    message=f"已暂停 {done}/{total}")
                return
            except Exception as ex:
                traceback.print_exc()
                for rel in group:
                    failed.append(f"{rel}: {ex}")
                result = {"route": "failed", "message": str(ex)}

            cues = result.get("cues") or []
            lang = result.get("lang") or ""
            if result.get("route") == "failed" or (not cues and not result.get("empty")):
                done += 1
                self.db.update_subtitle_job(
                    job_id, done_tracks=done, reused_tracks=reused,
                    route=result.get("route", "failed"),
                    progress=done / max(1, total),
                    message=f"{done}/{total} · {_route_label(result.get('route'))}"
                            + (f" · {result['message']}" if result.get("message") else ""))
                continue

            raw = result.get("raw")
            if raw:
                self._emit(root, work_id, group, raw)
                self._set_generated_flag(work_id)
                self.db.clear_track_prefs(work_id, group)
                done += 1
                reused += 1
                self.db.update_subtitle_job(
                    job_id, done_tracks=done, reused_tracks=reused,
                    route=result.get("route", ""),
                    progress=done / max(1, total),
                    message=f"{done}/{total} · {_route_label(result.get('route'))}"
                            + (f" · {result['message']}" if result.get("message") else ""))
                continue

            out_text = vtt.to_vtt_raw(cues)
            if not out_text.strip():
                # 无语音音轨：写空 VTT 占位，保证该组被视为已生成
                out_text = "WEBVTT\n\n"
            if lang == "ja":
                # 识别结果只落「日文版」；翻译由独立线程并行处理
                self._emit(root, work_id, group, out_text, category="ja")
            else:
                self._emit(root, work_id, group, out_text)
                self._set_generated_flag(work_id)
                self._cache_subtitle(work_id, primary, out_text)
            # 识别完成 → 清掉勾选记录，弹窗回归「已有日文/中文则不勾选」
            self.db.clear_track_prefs(work_id, group)
            done += 1
            if result.get("route") in ("cache", "local-sub", "official",
                                       "script-timed", "script-align",
                                       "script-estimate"):
                reused += 1
            self.db.update_subtitle_job(
                job_id, done_tracks=done, reused_tracks=reused,
                route=result.get("route", ""),
                progress=done / max(1, total),
                message=f"{done}/{total} · {_route_label(result.get('route'))}"
                        + (" · 已识别，待翻译" if lang == "ja" else ""))

        # 识别任务以「日文版或中文/双语已落盘」判定完成，翻译交给翻译线程
        covered = sum(1 for g in groups
                      if layout.exists_ja(root, work_id, g[0])
                      or layout.exists_translated(root, work_id, g[0]))
        if covered < total:
            # 有勾选的音轨没产出字幕时不能算完成，标记失败并保留原因
            msg = f"完成 {covered}/{total}"
            msg += f"，{len(failed)} 个失败" if failed else "，部分音轨未生成字幕"
            self.db.update_subtitle_job(
                job_id, status="failed", message=msg,
                progress=covered / max(1, total),
                error="; ".join(failed[:5]) or msg, finished_at=_now())
            return
        msg = f"完成 {covered}/{total}"
        if failed:
            msg += f"，{len(failed)} 个失败"
        self.db.update_subtitle_job(job_id, status="done", progress=1.0,
                                    message=msg, error="; ".join(failed[:5]) or None,
                                    finished_at=_now())

    # ---------------- 翻译任务执行 ----------------

    def _execute_translate(self, job_id: int, work_id: str, cfg: dict) -> None:
        """把「日文版」里的识别结果翻译成中文/双语，与 ASR 线程并行。"""
        work = self.db.get_work(work_id)
        if not work or not work.get("local_path"):
            self.db.update_subtitle_job(job_id, status="failed", error="作品不在本地",
                                        message="作品不在本地", finished_at=_now())
            return
        root = work["local_path"]
        if not os.path.isdir(root):
            self.db.update_subtitle_job(job_id, status="failed", error="目录不存在",
                                        message="目录不存在", finished_at=_now())
            return
        if not asr_engine.api_ready(cfg):
            self.db.update_subtitle_job(job_id, status="failed", error="未配置翻译 API",
                                        message="未配置翻译 API", finished_at=_now())
            return

        groups = sources.collect_work_tracks(
            root, AUDIO_EXTS,
            durations={r["rel_path"]: float(r["duration"] or 0)
                       for r in self.db.list_audio_index(work_id)},
            probe=True)

        def _nat(rel: str):
            return [int(t) if t.isdigit() else t.lower()
                    for t in re.split(r"(\d+)", rel)]

        # 只处理「已有日文、还没有中文」的组，按作品内自然顺序送入同一会话
        # 日文可能落在同 stem 的任意音源上（SE 有/无），译文统一写回主音源
        def _ja_rel(group: list[str]) -> str:
            for rel in group:
                if layout.exists_ja(root, work_id, rel):
                    return rel
            return ""

        groups = [g for g in groups
                  if _ja_rel(g)
                  and not layout.exists_translated(root, work_id, g[0])]
        groups.sort(key=lambda g: _nat(g[0]))
        disabled = set()  # 翻译不服从音轨勾选：日文已生成就必须翻译
        if disabled:
            groups = [g for g in groups if not all(rel in disabled for rel in g)]
        total = len(groups)
        if not total:
            self.db.update_subtitle_job(
                job_id, status="done", message="没有待翻译的音轨",
                progress=1.0, finished_at=_now())
            return

        sub = cfg.get("subtitle") or {}
        session = asr_engine.TranslationSession(
            int(sub.get("api_history_chars") or 12000))
        self.db.update_subtitle_job(job_id, total_tracks=total, route="translate",
                                    message=f"0/{total}")
        done = 0
        failed: list[str] = []

        for group in groups:
            control = self._check_control(job_id)
            if control == "cancel":
                self.db.update_subtitle_job(job_id, status="cancelled",
                                            message="已取消", finished_at=_now())
                return
            if control == "pause":
                self.db.update_subtitle_job(job_id, status="paused",
                                            message=f"已暂停 {done}/{total}")
                return

            primary = group[0]
            ja_text = layout.read(root, work_id, _ja_rel(group) or primary, "ja")
            if not ja_text:
                done += 1
                self.db.update_subtitle_job(
                    job_id, done_tracks=done, progress=done / max(1, total),
                    message=f"{done}/{total} · 无日文缓冲")
                continue
            ja_cues = vtt.normalize_cues(vtt.parse_any(ja_text, ".vtt"))
            if not ja_cues:
                # 空日文字幕（无语音音轨）：写空译文占位，避免一直挂在待翻译
                layout.write(root, work_id, primary, "bi", "WEBVTT\n\n")
                layout.write(root, work_id, primary, "zh", "WEBVTT\n\n")
                layout.cleanup_group(root, work_id, group)
                self._set_generated_flag(work_id)
                done += 1
                self.db.update_subtitle_job(
                    job_id, done_tracks=done, progress=done / max(1, total),
                    message=f"{done}/{total} · 无语音")
                continue
            self.db.update_subtitle_job(
                job_id, stage=primary, route="translate",
                progress=done / max(1, total),
                message=f"{min(done + 1, total)}/{total} · 翻译")
            try:
                bi = self._translate_ja(ja_cues, cfg, work_id, session,
                                        stop=lambda: self._pause_requested(job_id))
            except _PauseNow:
                self.db.update_subtitle_job(
                    job_id, status="paused",
                    message=f"已暂停 {done}/{total}")
                return
            except asr_engine.ApiWindowClosed:
                self.db.update_subtitle_job(
                    job_id, status="pending_translate",
                    progress=done / max(1, total),
                    message="等待翻译（API 禁用时段）")
                return
            except Exception as ex:
                traceback.print_exc()
                failed.append(f"{primary}: {ex}")
                done += 1
                self.db.update_subtitle_job(
                    job_id, done_tracks=done, progress=done / max(1, total),
                    message=f"{done}/{total} · 翻译失败")
                continue

            out_text = vtt.to_vtt_raw(bi)
            pair = vtt.split_bilingual(out_text)
            layout.write(root, work_id, primary, "bi", out_text)
            if pair:
                layout.write(root, work_id, primary, "zh", pair[1])
            layout.cleanup_group(root, work_id, group)
            self._set_generated_flag(work_id)
            self._cache_subtitle(work_id, primary, out_text)
            done += 1
            self.db.update_subtitle_job(
                job_id, done_tracks=done, progress=done / max(1, total),
                route="translate", message=f"{done}/{total} · 已翻译")

        if failed:
            msg = f"完成 {done - len(failed)}/{total}，{len(failed)} 个失败"
            self.db.update_subtitle_job(
                job_id, status="failed", message=msg,
                progress=done / max(1, total),
                error="; ".join(failed[:5]), finished_at=_now())
            return
        self.db.update_subtitle_job(job_id, status="done", progress=1.0,
                                    message=f"翻译完成 {done}/{total}",
                                    finished_at=_now())

    # ---------------- 组处理（同 stem 派发）----------------

    def _process_group(self, *, job_id: int, work_id: str, root: str,
                       group: list[str], mapping: dict[str, list],
                       local_subs: dict[str, str], api_subs: dict[str, str],
                       cfg: dict, force: bool,
                       base: int = 0, total: int = 1) -> dict:
        """找出一组同 stem 音频可用的字幕（台本/本地/官方/ASR）。

        只负责产出 cue 与语言标记，不写文件、不翻译：落盘由 ``_execute``
        处理，翻译由独立的翻译线程接管。
        返回 ``{route, message, cues, lang, fingerprint}``，lang ∈ ja/zh/bi。
        """
        primary = group[0]
        audio_path = os.path.join(root, primary)
        self.db.update_subtitle_job(job_id, stage=primary)

        # 指纹与时长（同 stem 音频指纹相同，只算一次）
        idx = self.db.get_audio_index(work_id, primary)
        duration = float((idx or {}).get("duration") or 0)
        fingerprint = (idx or {}).get("fingerprint") or ""
        fp_method = (idx or {}).get("fp_method") or ""
        if not fingerprint:
            if not duration:
                duration = probe_duration(audio_path)
            fingerprint, fp_method = fingerprint_audio(audio_path, duration)
            self.db.upsert_audio_index(work_id, primary, fingerprint, fp_method,
                                       duration, normalize_stem(primary))

        # 指纹缓存 → 命中则全组共享
        asset = self.db.find_subtitle_asset(fingerprint, "zh")
        if asset:
            blob = self.db.get_subtitle_blob(asset["vtt_hash"])
            if blob and blob.get("vtt_text"):
                raw = blob["vtt_text"]
                cues = vtt.normalize_cues(vtt.parse_any(raw, ".vtt"))
                return {"route": "cache", "message": f"指纹命中 {primary[:40]}",
                        "cues": cues, "raw": raw, "fingerprint": fingerprint}

        sub = cfg.get("subtitle") or {}
        min_ratio = float(sub.get("script_min_ratio") or 0.45)

        # 台本优先（primary 的台本结果派发给全组）
        for s in sorted(mapping.get(primary, []), key=lambda x: (
                0 if x.get("kind") == "timed" else 1,
                0 if x.get("lang") == "zh" else 1,
                -float(x.get("confidence") or 0))):
            if s.get("kind") == "scanned":
                continue
            result = self._from_script(work_id, primary, audio_path, s, cfg,
                                       duration, min_ratio)
            if result:
                return {"route": result["route"],
                        "message": f"台本 {primary[:40]}",
                        "cues": result["cues"], "lang": result["lang"],
                        "fingerprint": fingerprint}

        # 本地已有字幕（primary 的本地字幕派发给全组）
        local_sub = local_subs.get(primary, "")
        if local_sub:
            local_path = os.path.join(root, local_sub)
            cues, _lang = _read_cues(local_path)
            if cues:
                return {"route": "local-sub",
                        "message": f"本地字幕 {primary[:40]}",
                        "cues": cues, "lang": _cues_lang(cues),
                        "fingerprint": fingerprint}

        # 官方字幕（API 字幕同样派发给全组）
        api_sub = api_subs.get(primary, "")
        if api_sub:
            cues, _lang, _provider = self._official_cues(work_id, api_sub, cfg, duration)
            if cues:
                return {"route": "official",
                        "message": f"官方字幕 {primary[:40]}",
                        "cues": cues, "lang": _cues_lang(cues),
                        "fingerprint": fingerprint}

        # ASR — asmr-translator 兼容模式（Silero VAD + Haas-ja），只识别不翻译
        try:
            cues = self._run_asr_cues(primary, audio_path, cfg, job_id,
                                      base=base, total=total)
        except asr_engine.AsrInterrupted:
            raise _PauseNow()
        except FileNotFoundError as ex:
            return {"route": "failed", "message": str(ex)}
        except Exception as ex:
            return {"route": "failed", "message": f"ASR {primary[:40]}: {ex}"}

        if cues:
            return {"route": "asr", "message": f"ASR {primary[:40]}",
                    "cues": cues, "lang": "ja", "fingerprint": fingerprint}
        # 无语音音轨（寝息/环境音循环等）仍算成功：上层会写入空字幕占位，
        # 避免整部作品因这一条音轨反复失败
        return {"route": "asr-empty", "message": f"无语音 {primary[:40]}",
                "cues": [], "lang": "ja", "fingerprint": fingerprint,
                "empty": True}

    def _run_asr_cues(self, primary: str, audio_path: str, cfg: dict,
                      job_id: int, base: int = 0, total: int = 1) -> list[dict]:
        """调 asmr_translator.run_asr_cues，返回纯日文 cue 列表。"""
        import tempfile
        out_dir = tempfile.mkdtemp(prefix="asmr_sub_")
        # 进入识别阶段：卡片回到「生成中」
        self.db.update_subtitle_job(job_id, route="asr", stage=primary)

        def _progress(stage: str, p: float, detail: str = ""):
            try:
                # 进度按整部作品推进：已完成的音轨 + 当前音轨内的比例
                frac = (base + max(0.0, min(1.0, p))) / max(1, total)
                self.db.update_subtitle_job(
                    job_id, stage=primary, progress=min(0.99, frac),
                    message=f"{min(base + 1, total)}/{total} · [{stage}] {detail[:60]}")
            except Exception:
                pass

        try:
            return asr_engine.run_asr_cues(
                audio_path, out_dir, cfg, progress=_progress,
                stop=lambda: self._pause_requested(job_id))
        finally:
            import shutil as _sh
            try:
                _sh.rmtree(out_dir, ignore_errors=True)
            except Exception:
                pass

    def _cache_subtitle(self, work_id: str, rel: str, text: str) -> None:
        """把最终字幕写入指纹缓存，供同类音频复用。"""
        try:
            idx = self.db.get_audio_index(work_id, rel) or {}
            fingerprint = idx.get("fingerprint") or ""
            if not fingerprint or not text:
                return
            vtt_hash = self.db.put_subtitle_blob(text)
            self.db.upsert_subtitle_asset(
                fingerprint, "zh", "generated", "asmr-translator",
                "", vtt_hash)
        except Exception:
            pass

    def _set_generated_flag(self, work_id: str) -> None:
        """works.generated_zh 置 1（蓝色中字标签）。"""
        try:
            self.db.set_work_generated_zh(work_id, 1)
        except Exception:
            pass

    def _from_script(self, work_id: str, rel: str, audio_path: str, s: dict,
                     cfg: dict, duration: float,
                     min_ratio: float, align_mode: str = "estimate") -> dict | None:
        """台本 → cue（不翻译，语言标记交给调用方决定）。"""
        if s.get("lang") not in ("ja", "zh"):
            return None
        if s.get("kind") == "timed":
            cues = s.get("cues") or []
            if len(cues) < 2:
                return None
            text_hash = vtt.hash_text(s.get("text") or s["rel_path"])
            cached = self.db.get_script_alignment(work_id, rel, text_hash, "timed")
            if cached:
                blob = self.db.get_subtitle_blob(cached["vtt_hash"])
                if blob:
                    parsed = vtt.parse_any(blob["vtt_text"])
                    return {"cues": parsed, "route": "cache",
                            "lang": _cues_lang(parsed)}
            return {"cues": cues, "route": "script-timed",
                    "lang": "zh" if s.get("lang") == "zh" else "ja"}

        lines = s.get("lines") or []
        if len(lines) < 3:
            return None
        text_hash = vtt.hash_text(s.get("text") or "")
        # align_mode="asr" 需要 Haas-ja ASR（段内对齐）；没装时降级成 estimate
        mode = "estimate"
        cached = self.db.get_script_alignment(work_id, rel, text_hash, mode)
        if cached:
            blob = self.db.get_subtitle_blob(cached["vtt_hash"])
            if blob:
                parsed = vtt.parse_any(blob["vtt_text"])
                return {"cues": parsed, "route": "cache",
                        "lang": _cues_lang(parsed)}

        if not duration:
            return None
        cues = scripts.estimate_cues(lines, duration)
        if not cues:
            return None
        confidence = 0.3

        self.db.put_script_alignment(
            work_id, rel, text_hash, mode,
            self.db.put_subtitle_blob(vtt.to_vtt(cues)), confidence)
        return {"cues": cues, "route": "script-estimate",
                "lang": "zh" if s.get("lang") == "zh" else "ja"}

    def _translate_cues(self, cues: list[dict], cfg: dict, *, work_id: str = "",
                        session: asr_engine.TranslationSession | None = None,
                        guard=None, stop=None) -> tuple[list[dict], str, str]:
        # 按时间排序，确保送入模型的顺序与作品内一致
        cues = sorted(cues, key=lambda c: float(c.get("start") or 0))
        # 混排台本里中文行直接用，只翻译日文行
        targets = [i for i, c in enumerate(cues) if vtt.is_ja_text(c["text"])]
        if not targets:
            return cues, "script", ""
        sub = cfg.get("subtitle") or {}
        if not asr_engine.api_ready(cfg):
            raise RuntimeError("未配置翻译 API")
        # 沿用作品级会话，保证与 ASR 路线同样的上下文连续性
        work = (self.db.get_work(work_id) or {}) if work_id else {}
        info = {"title": work.get("title") or "",
                "subtitle": work.get("title") or "",
                "tags": work.get("tags") or []}
        try:
            merged = asr_engine.translate_cues_llm(
                cues, cfg, work_info=info, session=session, guard=guard, stop=stop)
        except asr_engine.AsrInterrupted:
            raise _PauseNow()
        return merged, "api", (sub.get("api_model") or "")

    # ---------------- 官方字幕 ----------------

    def _api_subtitle_map(self, work_id: str, audio_rels: list[str],
                          cfg: dict) -> dict[str, str]:
        """按文件名主干把官方字幕映射到音频。"""
        try:
            from web.routes.player import _fetch_api_subtitles
            subs = _fetch_api_subtitles(cfg, work_id)
        except Exception:
            return {}
        if not subs:
            return {}
        by_stem = {normalize_stem(a): a for a in audio_rels}
        out: dict[str, str] = {}
        for s in subs:
            stem = normalize_stem(s["filename"])
            target = by_stem.get(stem)
            if not target:
                for k, a in by_stem.items():
                    if k and stem and (k in stem or stem in k):
                        target = a
                        break
            if target and target not in out:
                out[target] = s["filename"]
        return out

    def _official_cues(self, work_id: str, filename: str, cfg: dict,
                       duration: float) -> tuple[list[dict], str, str]:
        try:
            from web.routes.player import _download_api_subtitle
            text, ext = _download_api_subtitle(cfg, work_id, filename)
        except Exception:
            return [], "", ""
        if not text:
            return [], "", ""
        cues = vtt.normalize_cues(vtt.parse_any(text, ext))
        if len(cues) < 2:
            return [], "", ""
        end = max(c["end"] for c in cues)
        if duration and abs(end - duration) > max(1.5, duration * 0.02):
            return [], "", ""
        return cues, vtt.lang_of(" ".join(c["text"] for c in cues)), "official"

    # ---------------- 资源保护 ----------------

    def _resources_ok(self, cfg: dict, root: str, job_id: int,
                      check_idle: bool = True) -> tuple[bool, str]:
        sub = cfg.get("subtitle") or {}
        min_mem = int(sub.get("min_free_mem_mb") or 900) * 1024 * 1024
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                info = {}
                for line in f:
                    k, _, v = line.partition(":")
                    info[k.strip()] = v.strip()
            avail = int(info.get("MemAvailable", "0 kB").split()[0]) * 1024
            if avail and avail < min_mem:
                return False, "可用内存不足，已暂停"
        except OSError:
            pass

        min_disk = int(sub.get("min_free_disk_gb") or 2) * 1024 ** 3
        try:
            free = shutil.disk_usage(root).free
            if free < min_disk:
                return False, "磁盘空间不足，已暂停"
        except OSError:
            pass

        # 只在任务开跑前等空闲；跑起来后 ASR 自身的负载会让 loadavg 一直偏高，
        # 逐音轨再等会把识别拖成十分钟一条。
        if check_idle and sub.get("idle_only", True):
            try:
                waited = 0
                while os.getloadavg()[0] > 2.0 and waited < 600:
                    if self._check_control(job_id) != "continue":
                        return False, "已暂停"
                    time.sleep(20)
                    waited += 20
            except (OSError, AttributeError):
                pass
        return True, ""

    # ---------------- 台本报告 / 概览 ----------------

    def script_report(self, work_id: str) -> list[dict]:
        cfg = self._get_cfg()
        sub = cfg.get("subtitle") or {}
        work = self.db.get_work(work_id)
        if not work or not work.get("local_path"):
            return []
        root = work["local_path"]
        audio_rels = collect_audio(root)
        found = scripts.scan_scripts(root,
                                     enable_pdf=bool(sub.get("parse_pdf", True)),
                                     enable_docx=bool(sub.get("parse_docx", True)))
        mapping = scripts.assign_scripts(found, audio_rels)
        out = []
        for s in found:
            matched = [a for a, lst in mapping.items()
                       if any(x["rel_path"] == s["rel_path"] for x in lst)]
            out.append({
                "rel_path": s["rel_path"],
                "kind": s.get("kind"),
                "lang": s.get("lang"),
                "line_count": s.get("line_count") or 0,
                "confidence": s.get("confidence") or 0,
                "matched_audio": matched[:20],
                "status": s.get("status"),
            })
        return out

    def overview(self) -> dict:
        now = time.time()
        ts, cached = self._overview_cache
        if cached and now - ts < 60:
            return cached
        stats = self.db.subtitle_coverage()
        works = self._local_works()
        audio_total = covered = 0
        missing_works = 0
        for w in works:
            root = w.get("local_path") or ""
            if not os.path.isdir(root):
                continue
            audios = collect_audio(root)
            audio_total += len(audios)
            cov = sum(1 for a in audios
                      if layout.exists_translated(root, w["source_id"], a))
            covered += cov
            if cov < len(audios):
                missing_works += 1
        running = None
        try:
            running = self.db.get_running_subtitle_job()
        except Exception:
            running = None
        vad_ok = os.path.isfile(asr_engine.DEFAULT_VAD_PATH)
        haas_ok = os.path.isfile(
            os.path.join(asr_engine.DEFAULT_HAAS_DIR, "encoder.quantized.onnx"))
        data = {
            "local_works": len(works),
            "audio_total": audio_total,
            "audio_covered": covered,
            "audio_missing": max(0, audio_total - covered),
            "works_missing": missing_works,
            "scripts": stats["scripts"],
            "cache_assets": stats["assets"],
            "active_jobs": stats["active_jobs"],
            "running": running,
            "asr_available": bool(vad_ok and haas_ok),
            "vad_model_ready": vad_ok,
            "haas_model_ready": haas_ok,
            "engine": "asmr-translator v1.30 兼容（ASMR VAD + Haas-ja + 大模型双语翻译）",
            "worker_alive": bool(self._worker and self._worker.is_alive()
                                 and self._tr_worker and self._tr_worker.is_alive()),
            "fpcalc": has_fpcalc(),
        }
        self._overview_cache = (now, data)
        return data

    def clear_generated(self) -> dict:
        """删除所有 生成字幕 目录下的自生成字幕，并清 works.generated_zh 标记。"""
        removed = 0
        works = self._local_works()
        for w in works:
            root = w.get("local_path") or ""
            if not os.path.isdir(root):
                continue
            removed += layout.remove_work(root)
            try:
                self.db.set_work_generated_zh(w["source_id"], 0)
            except Exception:
                pass
        self._overview_cache = (0, {})
        return {"removed": removed}


def _route_label(route: str | None) -> str:
    return {
        "script-timed": "台本带轴",
        "script-align": "台本对齐",
        "script-estimate": "台本估算",
        "official": "官方字幕",
        "local-sub": "本地字幕",
        "cache": "缓存复用",
        "asr": "ASR 翻译",
        "asr-empty": "无语音",
        "translate": "翻译缓冲",
        "pending_translate": "等待翻译",
        "skipped": "已有",
        "failed": "失败",
    }.get(route or "", "")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# --------------------------------------------------------------------------
# 单例
# --------------------------------------------------------------------------

def get_engine(app) -> SubtitleEngine:
    # 路由里传入的可能是 current_app 代理，后台线程不能持有它
    if hasattr(app, "_get_current_object"):
        app = app._get_current_object()
    engines = app.extensions.setdefault("subtitle_engines", {})
    db = app.config["DB"]
    cfg = app.config["APP_CONFIG"]
    key = id(db)
    engine = engines.get(key)
    if engine is None:
        engine = SubtitleEngine(db, lambda: cfg)
        engines[key] = engine
        try:
            db.reconcile_subtitle_jobs()
        except Exception:
            pass
    # 每次取引擎都确保 worker 存活：线程异常退出后能自动拉起
    engine.start()
    return engine
