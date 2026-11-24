"""SQLite 数据访问层 — 作品库、文件索引、下载任务"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable, Iterator


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS works (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT UNIQUE NOT NULL,
    title TEXT,
    release_date TEXT,
    tags TEXT DEFAULT '[]',
    tags_circle TEXT DEFAULT '[]',
    tags_va TEXT DEFAULT '[]',
    has_subtitle INTEGER DEFAULT 0,
    work_count INTEGER DEFAULT 0,
    dl_count INTEGER DEFAULT 0,
    rating REAL DEFAULT 0.0,
    price INTEGER DEFAULT 0,
    local_path TEXT,
    storage_root TEXT,
    file_count INTEGER DEFAULT 0,
    total_size INTEGER DEFAULT 0,
    integrity_status TEXT DEFAULT '',
    integrity_checked_at TEXT,
    integrity_report TEXT DEFAULT '{}',
    dl_updated_at TEXT,
    duration REAL DEFAULT 0,
    age_level TEXT DEFAULT 'adult',
    cover_url TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id TEXT NOT NULL,
    track_url TEXT,
    rel_path TEXT,
    filename TEXT,
    size INTEGER DEFAULT 0,
    downloaded_size INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    local_path TEXT,
    download_speed REAL DEFAULT 0,
    last_update TEXT,
    FOREIGN KEY (work_id) REFERENCES works(source_id)
);

CREATE TABLE IF NOT EXISTS download_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_ids TEXT DEFAULT '[]',
    filter_options TEXT DEFAULT '{}',
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    finished_at TEXT,
    current_work TEXT,
    progress_json TEXT DEFAULT '{}',
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_works_release ON works(release_date DESC);
CREATE INDEX IF NOT EXISTS idx_works_dlcount ON works(dl_count DESC);
CREATE INDEX IF NOT EXISTS idx_works_title ON works(title);
CREATE INDEX IF NOT EXISTS idx_works_tags ON works(tags);
CREATE INDEX IF NOT EXISTS idx_files_work ON files(work_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON download_tasks(status);

-- 字幕生成：音频指纹索引
CREATE TABLE IF NOT EXISTS audio_index (
    work_id TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    fp_method TEXT DEFAULT 'duration',
    duration REAL DEFAULT 0,
    stem TEXT DEFAULT '',
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (work_id, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_audio_fp ON audio_index(fingerprint);

-- 字幕正文（内容寻址）
CREATE TABLE IF NOT EXISTS subtitle_blobs (
    vtt_hash TEXT PRIMARY KEY,
    lang TEXT DEFAULT 'zh',
    vtt_text TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- 字幕资产（同一指纹只生成一次）
CREATE TABLE IF NOT EXISTS subtitle_assets (
    fingerprint TEXT NOT NULL,
    lang TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT DEFAULT '',
    source_work TEXT DEFAULT '',
    vtt_hash TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (fingerprint, lang, provider, model)
);

-- ASR 分段缓存
CREATE TABLE IF NOT EXISTS asr_cache (
    fingerprint TEXT NOT NULL,
    lang TEXT NOT NULL,
    model TEXT NOT NULL,
    segments_json TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (fingerprint, lang, model)
);

-- 逐句翻译缓存
CREATE TABLE IF NOT EXISTS translation_cache (
    text_hash TEXT NOT NULL,
    src_lang TEXT NOT NULL,
    dst_lang TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT DEFAULT '',
    translated TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (text_hash, src_lang, dst_lang, provider, model)
);

-- 台本正文（内容寻址）
CREATE TABLE IF NOT EXISTS script_texts (
    text_hash TEXT PRIMARY KEY,
    lang TEXT DEFAULT 'ja',
    text TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- 台本文件与作品/音频的关联
CREATE TABLE IF NOT EXISTS script_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    lang TEXT DEFAULT 'ja',
    kind TEXT DEFAULT 'plain',
    matched_audio TEXT DEFAULT '',
    confidence REAL DEFAULT 0,
    cues_json TEXT DEFAULT '[]',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(work_id, rel_path)
);

-- 台本对齐结果（避免重复对齐）
CREATE TABLE IF NOT EXISTS script_alignments (
    work_id TEXT NOT NULL,
    audio_rel TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    align_mode TEXT NOT NULL,
    vtt_hash TEXT NOT NULL,
    confidence REAL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (work_id, audio_rel, text_hash, align_mode)
);

-- 字幕生成任务
CREATE TABLE IF NOT EXISTS subtitle_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id TEXT NOT NULL,
    mode TEXT DEFAULT 'work',
    engine TEXT DEFAULT 'asmr_translator',
    status TEXT DEFAULT 'queued',
    stage TEXT DEFAULT '',
    route TEXT DEFAULT '',
    message TEXT DEFAULT '',
    error TEXT,
    progress REAL DEFAULT 0,
    total_tracks INTEGER DEFAULT 0,
    done_tracks INTEGER DEFAULT 0,
    reused_tracks INTEGER DEFAULT 0,
    sort_order REAL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sub_jobs_status ON subtitle_jobs(status, id);

-- 逐音轨翻译开关（默认 1=翻译；0=不参与该作品翻译）
CREATE TABLE IF NOT EXISTS subtitle_track_prefs (
    work_id TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    translate INTEGER DEFAULT 1,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (work_id, rel_path)
);
"""


class Database:
    """线程安全的 SQLite 包装（每个线程自己的连接）。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        # 主线程初始化
        with self._conn() as conn:
            conn.executescript(SCHEMA_SQL)
            # 旧库补列：作品总时长（秒）
            cols = {r[1] for r in conn.execute("PRAGMA table_info(works)").fetchall()}
            if "duration" not in cols:
                conn.execute("ALTER TABLE works ADD COLUMN duration REAL DEFAULT 0")
            if "age_level" not in cols:
                conn.execute("ALTER TABLE works ADD COLUMN age_level TEXT DEFAULT 'adult'")
            if "integrity_status" not in cols:
                conn.execute("ALTER TABLE works ADD COLUMN integrity_status TEXT DEFAULT ''")
            if "integrity_checked_at" not in cols:
                conn.execute("ALTER TABLE works ADD COLUMN integrity_checked_at TEXT")
            if "integrity_report" not in cols:
                conn.execute("ALTER TABLE works ADD COLUMN integrity_report TEXT DEFAULT '{}'")
            if "dl_updated_at" not in cols:
                conn.execute("ALTER TABLE works ADD COLUMN dl_updated_at TEXT")
            if "generated_zh" not in cols:
                conn.execute("ALTER TABLE works ADD COLUMN generated_zh INTEGER DEFAULT 0")

            # subtitle_jobs 旧库补列
            job_cols = {r[1] for r in conn.execute("PRAGMA table_info(subtitle_jobs)").fetchall()}
            if "engine" not in job_cols:
                conn.execute("ALTER TABLE subtitle_jobs ADD COLUMN engine TEXT DEFAULT 'asmr_translator'")
            if "stage" not in job_cols:
                conn.execute("ALTER TABLE subtitle_jobs ADD COLUMN stage TEXT DEFAULT ''")
            if "sort_order" not in job_cols:
                conn.execute("ALTER TABLE subtitle_jobs ADD COLUMN sort_order REAL DEFAULT 0")
                conn.execute("UPDATE subtitle_jobs SET sort_order=id WHERE COALESCE(sort_order,0)=0")
            # 旧版「只识别待翻译」任务统一由翻译线程接管，避免卡在旧状态
            conn.execute(
                "UPDATE subtitle_jobs SET status='done', message='已转入翻译队列', "
                "finished_at=CURRENT_TIMESTAMP "
                "WHERE status='pending_translate' AND COALESCE(mode,'work')!='translate'")

            conn.commit()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------------- works ----------------

    def upsert_work(self, work: dict[str, Any]) -> None:
        with self._conn() as conn:
            tags = json.dumps(work.get("tags", []) if isinstance(work.get("tags"), list) else [])
            tags_circle = json.dumps(work.get("tags_circle", []) if isinstance(work.get("tags_circle"), list) else [])
            tags_va = json.dumps(work.get("tags_va", []) if isinstance(work.get("tags_va"), list) else [])
            conn.execute("""
                INSERT INTO works (source_id, title, release_date, tags, tags_circle, tags_va,
                                  has_subtitle, work_count, dl_count, rating, price, age_level, cover_url, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(source_id) DO UPDATE SET
                    title=excluded.title,
                    release_date=excluded.release_date,
                    tags=excluded.tags,
                    tags_circle=excluded.tags_circle,
                    tags_va=excluded.tags_va,
                    has_subtitle=excluded.has_subtitle,
                    work_count=excluded.work_count,
                    dl_count=excluded.dl_count,
                    rating=excluded.rating,
                    price=excluded.price,
                    age_level=excluded.age_level,
                    cover_url=excluded.cover_url,
                    updated_at=CURRENT_TIMESTAMP
            """, (
                work.get("source_id"),
                work.get("title", ""),
                work.get("release_date", ""),
                tags, tags_circle, tags_va,
                int(bool(work.get("has_subtitle", False))),
                work.get("work_count", 0),
                work.get("dl_count", 0),
                float(work.get("rating", 0)),
                work.get("price", 0),
                work.get("age_level", "adult") or "adult",
                work.get("cover_url", ""),
            ))

    def update_local_work(self, source_id: str, local_path: str, storage_root: str,
                          file_count: int, total_size: int) -> None:
        with self._conn() as conn:
            conn.execute("""
                UPDATE works SET local_path=?, storage_root=?, file_count=?, total_size=?, updated_at=CURRENT_TIMESTAMP
                WHERE source_id=?
            """, (local_path, storage_root, file_count, total_size, source_id))

    def set_work_duration(self, source_id: str, seconds: float) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE works SET duration=? WHERE source_id=?",
                         (float(seconds or 0), source_id))

    def set_work_generated_zh(self, source_id: str, flag: int = 1) -> None:
        """works.generated_zh 置 1（蓝色中字标签）。"""
        with self._conn() as conn:
            conn.execute("UPDATE works SET generated_zh=? WHERE source_id=?",
                         (int(flag or 0), source_id))

    def set_work_integrity(self, source_id: str, status: str,
                           checked_at: str, report: dict) -> None:
        with self._conn() as conn:
            conn.execute("""
                UPDATE works SET integrity_status=?, integrity_checked_at=?, integrity_report=?
                WHERE source_id=?
            """, (status, checked_at, json.dumps(report, ensure_ascii=False), source_id))

    def works_for_dl_refresh(self, *, days: int = 7, limit: int = 80) -> list[dict]:
        """选出销量数据为空或超过 days 天未更新的作品。"""
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%S",
                               time.localtime(time.time() - max(1, days) * 86400))
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT source_id FROM works
                WHERE dl_updated_at IS NULL OR dl_updated_at = '' OR dl_updated_at < ?
                ORDER BY (dl_updated_at IS NULL) DESC, dl_updated_at ASC
                LIMIT ?
            """, (cutoff, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def set_work_dl_refresh(self, source_id: str, dl_count: int,
                            rating: float | None = None,
                            price: int | None = None,
                            age_level: str | None = None) -> None:
        """更新销量数据并记录刷新时间，避免同一作品每小时被重复抓取。"""
        fields = ["dl_count=?", "dl_updated_at=?"]
        params: list[Any] = [int(dl_count or 0),
                             time.strftime("%Y-%m-%dT%H:%M:%S")]
        if rating is not None:
            fields.append("rating=?")
            params.append(float(rating))
        if price is not None:
            fields.append("price=?")
            params.append(int(price))
        if age_level in ("all", "r15", "adult"):
            fields.append("age_level=?")
            params.append(age_level)
        params.append(source_id)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE works SET {', '.join(fields)} WHERE source_id=?", params)

    def get_work(self, source_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM works WHERE source_id=?", (source_id,)).fetchone()
            return self._row_to_dict(row) if row else None

    def list_works(self, *, page: int = 1, page_size: int = 20,
                   order: str = "release", sort: str = "desc", seed: int = 0,
                   q: str = "", has_subtitle: int | None = None,
                   from_date: str = "", to_date: str = "",
                   author: str = "", tag: str = "",
                   age: str = "",
                   exclude_age: str = "",
                   local_only: bool = False,
                   local_available_only: bool = True) -> tuple[list[dict], int]:
        """列出作品，返回 (works, total)。

        q / author / tag 支持空格或逗号分隔的多个词：全部命中才保留；
        以 "-" 开头的词表示排除。走 Python 侧匹配，因为 tags_* 以 JSON
        文本存储且中文被转义，无法用 LIKE 直接匹配中文。
        """
        where_clauses: list[str] = []
        params: list[Any] = []

        if has_subtitle is not None:
            where_clauses.append("has_subtitle = ?")
            params.append(has_subtitle)

        if from_date:
            where_clauses.append("substr(release_date, 1, 10) >= ?")
            params.append(from_date)

        if to_date:
            where_clauses.append("substr(release_date, 1, 10) <= ?")
            params.append(to_date)

        if local_only:
            where_clauses.append("local_path IS NOT NULL")
            where_clauses.append("local_path != ''")

        if local_available_only:
            # 还不能完全确认文件都在，这里只检查有 local_path
            pass

        where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        order_map = {
            "release": "release_date",
            "duration": "duration",
            "dl_count": "dl_count",
            "rating": "rating",
            "price": "price",
            "created": "created_at",
            "id": "id",
        }
        if order == "random":
            try:
                seed_i = int(seed)
            except (TypeError, ValueError):
                seed_i = 0
            mult = (seed_i % 999983) + 1
            order_col = f"((((id * 2654435761) % 4294967296) * {mult}) % 1000003)"
            direction = "ASC"
        else:
            order_col = order_map.get(order, "release_date")
            direction = "DESC" if sort.lower() == "desc" else "ASC"

        q_tokens, q_neg = self._split_terms(q)
        author_terms, author_neg = self._split_terms(author)
        tag_terms, tag_neg = self._split_terms(tag)
        age_terms = [a for a in re.split(r"[\s,]+", age or "") if a in ("all", "r15", "adult")]
        excluded_terms = [a for a in re.split(r"[\s,]+", exclude_age or "") if a in ("all", "r15", "adult")]
        post_filter = bool(q_tokens or author_terms or tag_terms or q_neg or author_neg
                           or tag_neg or age_terms or excluded_terms)

        with self._conn() as conn:
            if post_filter:
                rows = conn.execute(
                    f"SELECT * FROM works {where} ORDER BY {order_col} {direction}", params
                ).fetchall()
                works = [self._row_to_dict(r) for r in rows]

                def haystack(w: dict) -> str:
                    parts = [w.get("title") or "", w.get("source_id") or ""]
                    parts += (w.get("tags") or []) + (w.get("tags_circle") or []) + (w.get("tags_va") or [])
                    return " / ".join(parts).lower()

                def keep(w: dict) -> bool:
                    text = haystack(w)
                    if any(t not in text for t in q_tokens + author_terms + tag_terms):
                        return False
                    if any(n in text for n in q_neg + author_neg + tag_neg):
                        return False
                    if age_terms and (w.get("age_level") or "adult") not in age_terms:
                        return False
                    if excluded_terms and (w.get("age_level") or "adult") in excluded_terms:
                        return False
                    return True

                works = [w for w in works if keep(w)]
                total = len(works)
                start = (page - 1) * page_size
                return works[start:start + page_size], total

            total = conn.execute(
                f"SELECT COUNT(*) FROM works {where}", params).fetchone()[0]

            rows = conn.execute(
                f"SELECT * FROM works {where} ORDER BY {order_col} {direction} LIMIT ? OFFSET ?",
                params + [page_size, (page - 1) * page_size]
            ).fetchall()

        return [self._row_to_dict(r) for r in rows], total

    def delete_work(self, source_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM files WHERE work_id=?", (source_id,))
            conn.execute("DELETE FROM works WHERE source_id=?", (source_id,))

    @staticmethod
    def _split_terms(raw: str) -> tuple[list[str], list[str]]:
        """空格/逗号分隔 → (包含词, 排除词)；"-" 前缀为排除，全部转小写。"""
        pos: list[str] = []
        neg: list[str] = []
        for tok in re.split(r"[\s,]+", raw or ""):
            tok = tok.strip().lower()
            if not tok:
                continue
            if tok.startswith("-") and len(tok) > 1:
                neg.append(tok[1:])
            else:
                pos.append(tok)
        return pos, neg

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        for k in ("tags", "tags_circle", "tags_va"):
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except (json.JSONDecodeError, TypeError):
                    d[k] = []
        if d.get("integrity_report"):
            try:
                d["integrity_report"] = json.loads(d["integrity_report"])
            except (json.JSONDecodeError, TypeError):
                d["integrity_report"] = {}
        else:
            d["integrity_report"] = {}
        return d

    # ---------------- files ----------------

    def upsert_file(self, work_id: str, filename: str, **kwargs) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM files WHERE work_id=? AND filename=?",
                (work_id, filename)).fetchone()
            if row:
                fid = row[0]
                sets = ", ".join(f"{k}=?" for k in kwargs)
                conn.execute(f"UPDATE files SET {sets} WHERE id=?",
                             list(kwargs.values()) + [fid])
                return fid
            # insert
            fields = ["work_id", "filename"] + list(kwargs.keys())
            values = [work_id, filename] + list(kwargs.values())
            placeholders = ",".join("?" * len(fields))
            cur = conn.execute(f"INSERT INTO files ({','.join(fields)}) VALUES ({placeholders})", values)
            return cur.lastrowid or 0

    def update_file_progress(self, file_id: int, downloaded: int, speed: float) -> None:
        with self._conn() as conn:
            conn.execute("""
                UPDATE files SET downloaded_size=?, download_speed=?, last_update=CURRENT_TIMESTAMP
                WHERE id=?
            """, (downloaded, speed, file_id))

    def update_file_status(self, file_id: int, status: str, local_path: str = "") -> None:
        with self._conn() as conn:
            if local_path:
                conn.execute("UPDATE files SET status=?, local_path=? WHERE id=?",
                             (status, local_path, file_id))
            else:
                conn.execute("UPDATE files SET status=? WHERE id=?", (status, file_id))

    def get_work_files(self, work_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM files WHERE work_id=?", (work_id,)).fetchall()
            return [dict(r) for r in rows]

    # ---------------- download_tasks ----------------

    def create_task(self, work_ids: list[str], filter_options: dict | None = None) -> int:
        with self._conn() as conn:
            cur = conn.execute("""
                INSERT INTO download_tasks (work_ids, filter_options, status, started_at)
                VALUES (?, ?, 'pending', CURRENT_TIMESTAMP)
            """, (json.dumps(work_ids), json.dumps(filter_options or {})))
            return cur.lastrowid or 0

    def update_task(self, task_id: int, **kwargs) -> None:
        with self._conn() as conn:
            sets = ", ".join(f"{k}=?" for k in kwargs)
            conn.execute(f"UPDATE download_tasks SET {sets} WHERE id=?",
                         list(kwargs.values()) + [task_id])

    def update_task_if(self, task_id: int, status: str,
                       allowed: tuple[str, ...], **kwargs) -> bool:
        """仅当当前状态在 allowed 内才更新，返回是否命中（避免覆盖终态）。"""
        with self._conn() as conn:
            sets = ", ".join(["status=?"] + [f"{k}=?" for k in kwargs])
            placeholders = ",".join("?" for _ in allowed)
            cur = conn.execute(
                f"UPDATE download_tasks SET {sets} WHERE id=? AND status IN ({placeholders})",
                [status] + list(kwargs.values()) + [task_id] + list(allowed))
            return cur.rowcount > 0

    def list_tasks(self, status_filter: str | None = None) -> list[dict]:
        with self._conn() as conn:
            if status_filter:
                rows = conn.execute(
                    "SELECT * FROM download_tasks WHERE status=? ORDER BY id DESC",
                    (status_filter,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM download_tasks ORDER BY id DESC").fetchall()
            out = []
            for r in rows:
                d = dict(r)
                try:
                    d["work_ids"] = json.loads(d.get("work_ids") or "[]")
                    d["progress_json"] = json.loads(d.get("progress_json") or "{}")
                    d["filter_options"] = json.loads(d.get("filter_options") or "{}")
                except (json.JSONDecodeError, TypeError):
                    pass
                out.append(d)
            return out

    def get_task(self, task_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM download_tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            try:
                d["work_ids"] = json.loads(d.get("work_ids") or "[]")
                d["progress_json"] = json.loads(d.get("progress_json") or "{}")
                d["filter_options"] = json.loads(d.get("filter_options") or "{}")
            except (json.JSONDecodeError, TypeError):
                pass
            return d

    def cancel_task(self, task_id: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE download_tasks SET status='cancelled', error='cancelled_by_user', finished_at=CURRENT_TIMESTAMP WHERE id=?",
                         (task_id,))

    def reconcile_orphan_tasks(self) -> dict[str, int]:
        """服务启动时对账。

        进程重启会带走后台 worker 线程，但数据库里的任务仍停在 pending/running。
        这里逐条检查作品是否已完整落盘：已完整则补记为 done，否则记为 failed。
        """
        fixed = {"done": 0, "failed": 0}
        with self._conn() as conn:
            # 重启前正在暂停中的任务，worker 已随进程退出，直接收尾为已暂停
            conn.execute("UPDATE download_tasks SET status='paused' WHERE status='pausing'")
            rows = conn.execute(
                "SELECT id, work_ids FROM download_tasks "
                "WHERE status IN ('pending','running')").fetchall()
            for row in rows:
                try:
                    work_ids = json.loads(row["work_ids"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    work_ids = []

                all_done = bool(work_ids)
                files = 0
                size = 0
                for wid in work_ids:
                    w = conn.execute(
                        "SELECT local_path, file_count, total_size FROM works WHERE source_id=?",
                        (wid,)).fetchone()
                    if (not w or not w["local_path"]
                            or not os.path.isdir(w["local_path"]) or not (w["file_count"] or 0)):
                        all_done = False
                        continue
                    files += w["file_count"] or 0
                    size += w["total_size"] or 0

                if all_done:
                    conn.execute(
                        "UPDATE download_tasks SET status='done', error=NULL, "
                        "finished_at=CURRENT_TIMESTAMP, progress_json=? WHERE id=?",
                        (json.dumps({
                            "summary": {"ok": len(work_ids), "skip": 0, "fail": 0,
                                        "files_ok": files, "total_size": size},
                            "note": "file_complete_after_restart",
                        }, ensure_ascii=False), row["id"]))
                    fixed["done"] += 1
                else:
                    conn.execute(
                        "UPDATE download_tasks SET status='failed', "
                        "error='服务重启导致下载中断，请重新下载', "
                        "finished_at=CURRENT_TIMESTAMP WHERE id=?",
                        (row["id"],))
                    fixed["failed"] += 1
        return fixed

    def storage_usage(self) -> dict[str, int]:
        """各存储根下 ASMR 作品占用（字节），key 为 storage_root。"""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT storage_root, COALESCE(SUM(total_size), 0) AS used
                FROM works
                WHERE storage_root IS NOT NULL AND storage_root != ''
                  AND local_path IS NOT NULL AND local_path != ''
                GROUP BY storage_root
            """).fetchall()
        return {r["storage_root"]: int(r["used"] or 0) for r in rows}

    def storage_roots_seen(self) -> list[str]:
        """历史作品使用过的存储根（用于“添加存储空间”时给出建议）。"""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT DISTINCT storage_root FROM works
                WHERE storage_root IS NOT NULL AND storage_root != ''
            """).fetchall()
        return [r["storage_root"] for r in rows]

    def local_paths(self) -> list[str]:
        """已入库作品的本地目录（用于识别未入库的残留目录）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT local_path FROM works WHERE local_path IS NOT NULL AND local_path != ''"
            ).fetchall()
        return [r["local_path"] for r in rows]

    # ---------------- subtitles ----------------

    @staticmethod
    def text_hash(text: str) -> str:
        return hashlib.sha1((text or "").encode("utf-8")).hexdigest()

    def upsert_audio_index(self, work_id: str, rel_path: str, fingerprint: str,
                           method: str, duration: float, stem: str) -> None:
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO audio_index (work_id, rel_path, fingerprint, fp_method, duration, stem, updated_at)
                VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(work_id, rel_path) DO UPDATE SET
                    fingerprint=excluded.fingerprint, fp_method=excluded.fp_method,
                    duration=excluded.duration, stem=excluded.stem,
                    updated_at=CURRENT_TIMESTAMP
            """, (work_id, rel_path, fingerprint, method, float(duration or 0), stem))

    def get_audio_index(self, work_id: str, rel_path: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM audio_index WHERE work_id=? AND rel_path=?",
                (work_id, rel_path)).fetchone()
            return dict(row) if row else None

    def list_audio_index(self, work_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM audio_index WHERE work_id=?", (work_id,)).fetchall()
            return [dict(r) for r in rows]

    def find_audio_by_fingerprint(self, fingerprint: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM audio_index WHERE fingerprint=? LIMIT 20",
                (fingerprint,)).fetchall()
            return [dict(r) for r in rows]

    def put_subtitle_blob(self, vtt_text: str, lang: str = "zh") -> str:
        h = self.text_hash(vtt_text)
        with self._conn() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO subtitle_blobs (vtt_hash, lang, vtt_text) VALUES (?,?,?)
            """, (h, lang, vtt_text))
        return h

    def get_subtitle_blob(self, vtt_hash: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM subtitle_blobs WHERE vtt_hash=?", (vtt_hash,)).fetchone()
            return dict(row) if row else None

    def upsert_subtitle_asset(self, fingerprint: str, lang: str, provider: str,
                              model: str, source_work: str, vtt_hash: str) -> None:
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO subtitle_assets (fingerprint, lang, provider, model, source_work, vtt_hash)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(fingerprint, lang, provider, model) DO UPDATE SET
                    source_work=excluded.source_work, vtt_hash=excluded.vtt_hash,
                    created_at=CURRENT_TIMESTAMP
            """, (fingerprint, lang, provider, model or "", source_work or "", vtt_hash))

    def find_subtitle_asset(self, fingerprint: str, lang: str = "zh") -> dict | None:
        with self._conn() as conn:
            row = conn.execute("""
                SELECT * FROM subtitle_assets WHERE fingerprint=? AND lang=?
                ORDER BY created_at ASC LIMIT 1
            """, (fingerprint, lang)).fetchone()
            return dict(row) if row else None

    def put_asr_cache(self, fingerprint: str, lang: str, model: str,
                      segments: list[dict]) -> None:
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO asr_cache (fingerprint, lang, model, segments_json)
                VALUES (?,?,?,?)
                ON CONFLICT(fingerprint, lang, model) DO UPDATE SET
                    segments_json=excluded.segments_json, created_at=CURRENT_TIMESTAMP
            """, (fingerprint, lang, model, json.dumps(segments, ensure_ascii=False)))

    def get_asr_cache(self, fingerprint: str, lang: str, model: str) -> list[dict] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT segments_json FROM asr_cache WHERE fingerprint=? AND lang=? AND model=?",
                (fingerprint, lang, model)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["segments_json"])
        except (json.JSONDecodeError, TypeError):
            return None

    def get_translations(self, text_hashes: list[str], src: str, dst: str,
                         provider: str, model: str) -> dict[str, str]:
        if not text_hashes:
            return {}
        out: dict[str, str] = {}
        with self._conn() as conn:
            for i in range(0, len(text_hashes), 400):
                chunk = text_hashes[i:i + 400]
                ph = ",".join("?" for _ in chunk)
                rows = conn.execute(f"""
                    SELECT text_hash, translated FROM translation_cache
                    WHERE src_lang=? AND dst_lang=? AND provider=? AND model=?
                      AND text_hash IN ({ph})
                """, [src, dst, provider, model or ""] + chunk).fetchall()
                for r in rows:
                    out[r["text_hash"]] = r["translated"]
        return out

    def put_translation(self, items: list[tuple[str, str, str, str, str, str]]) -> None:
        """items: [(text_hash, src, dst, provider, model, translated), ...]"""
        if not items:
            return
        with self._conn() as conn:
            conn.executemany("""
                INSERT INTO translation_cache
                    (text_hash, src_lang, dst_lang, provider, model, translated)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(text_hash, src_lang, dst_lang, provider, model) DO UPDATE SET
                    translated=excluded.translated, created_at=CURRENT_TIMESTAMP
            """, items)

    def put_script_text(self, text_hash: str, lang: str, text: str) -> None:
        with self._conn() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO script_texts (text_hash, lang, text) VALUES (?,?,?)
            """, (text_hash, lang, text))

    def get_script_text(self, text_hash: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM script_texts WHERE text_hash=?", (text_hash,)).fetchone()
            return dict(row) if row else None

    def upsert_script_asset(self, work_id: str, rel_path: str, text_hash: str,
                            lang: str, kind: str, matched_audio: str,
                            confidence: float, cues: list[dict]) -> None:
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO script_assets
                    (work_id, rel_path, text_hash, lang, kind, matched_audio, confidence, cues_json)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(work_id, rel_path) DO UPDATE SET
                    text_hash=excluded.text_hash, lang=excluded.lang, kind=excluded.kind,
                    matched_audio=excluded.matched_audio, confidence=excluded.confidence,
                    cues_json=excluded.cues_json, created_at=CURRENT_TIMESTAMP
            """, (work_id, rel_path, text_hash, lang, kind, matched_audio or "",
                  float(confidence or 0), json.dumps(cues or [], ensure_ascii=False)))

    def list_script_assets(self, work_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM script_assets WHERE work_id=? ORDER BY rel_path",
                (work_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["cues"] = json.loads(d.get("cues_json") or "[]")
            except (json.JSONDecodeError, TypeError):
                d["cues"] = []
            out.append(d)
        return out

    def put_script_alignment(self, work_id: str, audio_rel: str, text_hash: str,
                             mode: str, vtt_hash: str, confidence: float) -> None:
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO script_alignments
                    (work_id, audio_rel, text_hash, align_mode, vtt_hash, confidence)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(work_id, audio_rel, text_hash, align_mode) DO UPDATE SET
                    vtt_hash=excluded.vtt_hash, confidence=excluded.confidence,
                    created_at=CURRENT_TIMESTAMP
            """, (work_id, audio_rel, text_hash, mode, vtt_hash, float(confidence or 0)))

    def get_script_alignment(self, work_id: str, audio_rel: str,
                             text_hash: str, mode: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("""
                SELECT * FROM script_alignments
                WHERE work_id=? AND audio_rel=? AND text_hash=? AND align_mode=?
            """, (work_id, audio_rel, text_hash, mode)).fetchone()
            return dict(row) if row else None

    # ---------------- subtitle jobs ----------------

    def create_subtitle_job(self, work_id: str, mode: str = "work",
                            total_tracks: int = 0) -> int:
        with self._conn() as conn:
            cur = conn.execute("""
                INSERT INTO subtitle_jobs (work_id, mode, status, total_tracks, sort_order)
                VALUES (?,?,'queued',?,
                        COALESCE((SELECT MAX(sort_order) FROM subtitle_jobs),0)+1)
            """, (work_id, mode, int(total_tracks or 0)))
            return cur.lastrowid or 0

    def next_subtitle_sort_order(self) -> float:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(sort_order),0)+1 FROM subtitle_jobs").fetchone()
            return float(row[0] or 1)

    def set_job_sort_order(self, job_id: int, order: float) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE subtitle_jobs SET sort_order=? WHERE id=?",
                         (float(order), job_id))

    def queued_subtitle_jobs(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM subtitle_jobs WHERE status IN ('queued','paused','running')
                ORDER BY sort_order ASC, id ASC
            """).fetchall()
            return [dict(r) for r in rows]

    # ---------------- 逐音轨翻译开关 ----------------

    def get_track_prefs(self, work_id: str) -> dict[str, bool]:
        """rel_path -> 是否参与翻译（缺省 True）。"""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT rel_path, translate FROM subtitle_track_prefs WHERE work_id=?",
                (work_id,)).fetchall()
            return {r["rel_path"]: bool(r["translate"]) for r in rows}

    def set_track_translate(self, work_id: str, rel_path: str, on: bool) -> None:
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO subtitle_track_prefs (work_id, rel_path, translate, updated_at)
                VALUES (?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(work_id, rel_path) DO UPDATE SET
                    translate=excluded.translate, updated_at=CURRENT_TIMESTAMP
            """, (work_id, rel_path, 1 if on else 0))

    def disabled_tracks(self, work_id: str) -> set[str]:
        return {k for k, v in self.get_track_prefs(work_id).items() if not v}

    def cancel_queued_jobs(self, work_ids: list[str]) -> int:
        """把勾选作品的排队/暂停任务移出队列（标记 cancelled）。"""
        if not work_ids:
            return 0
        marks = ",".join("?" for _ in work_ids)
        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE subtitle_jobs SET status='cancelled', finished_at=CURRENT_TIMESTAMP "
                f"WHERE work_id IN ({marks}) AND status IN ('queued','paused')",
                list(work_ids))
            return cur.rowcount or 0

    def update_subtitle_job(self, job_id: int, **kwargs) -> None:
        if not kwargs:
            return
        sets = ", ".join(f"{k}=?" for k in kwargs)
        with self._conn() as conn:
            conn.execute(f"UPDATE subtitle_jobs SET {sets} WHERE id=?",
                         list(kwargs.values()) + [job_id])

    def get_subtitle_job(self, job_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM subtitle_jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def active_subtitle_job(self, work_id: str, mode: str = "asr") -> dict | None:
        """取作品的活跃任务。

        mode='asr' 只看识别类任务，mode='translate' 只看翻译任务，
        mode='any' 不过滤；识别与翻译可以同时存在，互不占用。
        """
        sql = ("SELECT * FROM subtitle_jobs WHERE work_id=? "
               "AND status IN ('queued','running','paused','pending_translate')")
        if mode == "translate":
            sql += " AND mode='translate'"
        elif mode == "asr":
            sql += " AND COALESCE(mode,'work')!='translate'"
        sql += " ORDER BY id DESC LIMIT 1"
        with self._conn() as conn:
            row = conn.execute(sql, (work_id,)).fetchone()
            return dict(row) if row else None

    def next_queued_subtitle_job(self) -> dict | None:
        """ASR 线程取任务：翻译任务由翻译线程单独消费。"""
        with self._conn() as conn:
            row = conn.execute("""
                SELECT * FROM subtitle_jobs
                WHERE status='queued' AND COALESCE(mode,'work')!='translate'
                ORDER BY sort_order ASC, id ASC LIMIT 1
            """).fetchone()
            return dict(row) if row else None

    def next_queued_translate_job(self) -> dict | None:
        """翻译线程取任务（含禁用时段中断后留下的待翻译任务）。

        执行顺序与「待翻译」抽屉的展示顺序一致（作品发布日期倒序），
        不再沿用 sort_order：sort_order 是生成队列拖动排序的语义，
        且与生成任务共用同一序列，会让翻译顺序看起来随机。
        """
        with self._conn() as conn:
            row = conn.execute("""
                SELECT j.* FROM subtitle_jobs j
                LEFT JOIN works w ON w.source_id = j.work_id
                WHERE j.mode='translate' AND j.status IN ('queued','pending_translate')
                ORDER BY (w.release_date IS NULL) ASC,
                         w.release_date DESC, j.id ASC
                LIMIT 1
            """).fetchone()
            return dict(row) if row else None

    def list_subtitle_jobs(self, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM subtitle_jobs ORDER BY id DESC LIMIT ?
            """, (int(limit),)).fetchall()
            return [dict(r) for r in rows]

    def get_running_subtitle_job(self) -> dict | None:
        """当前正在跑的字幕任务（含 stage 进度），没有则 None。"""
        with self._conn() as conn:
            r = conn.execute(
                "SELECT * FROM subtitle_jobs WHERE status='running' "
                "ORDER BY id DESC LIMIT 1").fetchone()
            return dict(r) if r else None

    def reconcile_subtitle_jobs(self) -> int:
        """服务重启后把 running 置为 paused，可续跑。"""
        with self._conn() as conn:
            cur = conn.execute("""
                UPDATE subtitle_jobs
                SET status='paused', message='服务重启中断', error=NULL
                WHERE status='running'
            """)
            return cur.rowcount or 0

    def resume_restart_interrupted_jobs(self) -> int:
        """服务启动时把「重启中断」的暂停任务重新入队，实现自动续跑。"""
        with self._conn() as conn:
            cur = conn.execute("""
                UPDATE subtitle_jobs
                SET status='queued', message='等待执行'
                WHERE status='paused' AND message='服务重启中断'
            """)
            return cur.rowcount or 0

    def subtitle_coverage(self) -> dict[str, int]:
        """粗略统计：已索引音频数、已有中文资产数、台本关联数。"""
        with self._conn() as conn:
            indexed = conn.execute("SELECT COUNT(*) FROM audio_index").fetchone()[0]
            assets = conn.execute(
                "SELECT COUNT(*) FROM subtitle_assets WHERE lang='zh'").fetchone()[0]
            scripts = conn.execute("SELECT COUNT(*) FROM script_assets").fetchone()[0]
            aligned = conn.execute("SELECT COUNT(*) FROM script_alignments").fetchone()[0]
            jobs = conn.execute(
                "SELECT COUNT(*) FROM subtitle_jobs WHERE status IN ('queued','running')").fetchone()[0]
        return {"indexed": indexed, "assets": assets, "scripts": scripts,
                "aligned": aligned, "active_jobs": jobs}

    # ---------------- stats ----------------

    def stats(self) -> dict[str, Any]:
        with self._conn() as conn:
            total_works = conn.execute("SELECT COUNT(*) FROM works").fetchone()[0]
            local_works = conn.execute("SELECT COUNT(*) FROM works WHERE local_path IS NOT NULL AND local_path != ''").fetchone()[0]
            total_files = conn.execute("SELECT COUNT(*) FROM files WHERE status='downloaded'").fetchone()[0]
            total_size = conn.execute("SELECT COALESCE(SUM(total_size),0) FROM works WHERE local_path IS NOT NULL").fetchone()[0]
            total_duration = conn.execute("SELECT COALESCE(SUM(duration),0) FROM works WHERE local_path IS NOT NULL AND local_path != ''").fetchone()[0]
            pending_tasks = conn.execute("SELECT COUNT(*) FROM download_tasks WHERE status IN ('pending','running')").fetchone()[0]
        return {
            "total_works": total_works,
            "local_works": local_works,
            "total_files": total_files,
            "total_size": total_size,
            "total_duration": total_duration,
            "pending_tasks": pending_tasks,
        }
