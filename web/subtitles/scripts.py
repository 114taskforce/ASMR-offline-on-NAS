"""台本识别 — 扫描、内容嗅探、语言判定、PDF/DOCX 提取、与音频配对、时间轴对齐。"""
from __future__ import annotations

import os
import re
import subprocess

from . import vtt
from .fingerprint import normalize_stem, track_number

SCRIPT_EXTS = {".txt", ".md", ".html", ".htm", ".pdf", ".docx"}

_STRONG_DIRS = ("台本", "台本pdf", "台本pdf版", "script", "scripts", "せりふ", "セリフ",
                "台词", "台詞", "翻译", "翻譯", "大家一起来翻译")
_NAME_HINTS = ("台本", "セリフ", "せりふ", "台词", "台詞", "script", "transcript",
               "翻译", "翻譯", "對白", "对白")
_BLACKLIST = ("readme", "说明", "説明", "アンケート", "特典案内", "ご案内",
              "関連作品", "差分", "クレジット", "購入者", "お知らせ", "注意書き",
              "利用規約", "サイン", "頒布", "サークル", "作品紹介", "収録内容",
              "ファイル一覧", "tracklist")

_DIALOGUE_MARK_RE = re.compile(r"[「」『』：:（(]")
_SPEAKER_LINE_RE = re.compile(r"^\s*[【\[][^】\]]{1,16}[】\]]\s*[:：]?\s*")
_TAG_STRIP_RE = re.compile(r"<[^>]{1,200}>")
_STAGE_NOTE_RE = re.compile(r"^\s*[（(【\[].{0,60}[）)】\]]\s*$")
_ANNOTATION_RE = re.compile(r"^\s*[※＊*]|(?:翻訳|翻译|譯文|译文)\s*[:：]")
_HEAVY_SYMBOL_RE = re.compile(r"^[\W_♪♫♬♩☆★●○■□◆◇※→←↑↓〜～\s]+$")

MAX_DEPTH = 8


# --------------------------------------------------------------------------
# 内容嗅探
# --------------------------------------------------------------------------

def _is_blacklisted(rel: str) -> bool:
    low = rel.lower()
    return any(k in low for k in _BLACKLIST)


def _in_script_dir(rel: str) -> bool:
    parts = [p.lower() for p in rel.replace("\\", "/").split("/")[:-1]]
    return any(any(k in part for k in _STRONG_DIRS) for part in parts)


def _name_hinted(rel: str) -> bool:
    name = os.path.basename(rel).lower()
    return any(k in name for k in _NAME_HINTS)


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
    text = _TAG_STRIP_RE.sub("", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return text


def _extract_pdf(path: str) -> tuple[str, str]:
    """返回 (文本, 状态)；状态 text/empty/scanned/error。"""
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        PdfReader = None  # type: ignore

    text = ""
    if PdfReader is not None:
        try:
            reader = PdfReader(path)
            chunks = []
            for page in reader.pages:
                try:
                    chunks.append(page.extract_text() or "")
                except Exception:
                    continue
            text = "\n".join(chunks)
        except Exception:
            text = ""

    if len(text.strip()) < 40:
        # 兜底尝试 poppler 的 pdftotext
        if _which("pdftotext"):
            try:
                out = subprocess.run(["pdftotext", "-layout", path, "-"],
                                     capture_output=True, text=True, timeout=60)
                text = out.stdout or ""
            except (OSError, subprocess.SubprocessError):
                text = ""
    if not text.strip():
        return "", "scanned"
    return text, "text"


def _extract_docx(path: str) -> tuple[str, str]:
    try:
        import docx  # type: ignore
    except ImportError:
        return "", "unsupported"
    try:
        doc = docx.Document(path)
        parts = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    if cell.text and cell.text.strip():
                        parts.append(cell.text.strip())
        text = "\n".join(parts)
    except Exception:
        return "", "error"
    return (text, "text") if text.strip() else ("", "empty")


def _which(name: str) -> str | None:
    from shutil import which
    return which(name)


# --------------------------------------------------------------------------
# 台本行清洗
# --------------------------------------------------------------------------

def clean_script_lines(text: str) -> list[str]:
    """把纯文本台本拆成台词行，去掉说话人、括号说明与翻译注。"""
    lines: list[str] = []
    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        s = raw.strip()
        if not s:
            continue
        s = _TAG_STRIP_RE.sub("", s)
        s = _SPEAKER_LINE_RE.sub("", s)
        s = re.sub(r"^[^\s:：]{1,10}\s*[:：]\s*", "", s)  # 名字：台词
        if _STAGE_NOTE_RE.match(s) or _ANNOTATION_RE.search(s):
            continue
        s = vtt.clean_text(s)
        if not s or _HEAVY_SYMBOL_RE.match(s):
            continue
        if len(s) < 2:
            continue
        lines.append(s)
    return lines


def _content_is_script(text: str, rel: str) -> bool:
    """非台本目录/文件名时，用内容密度判断。"""
    sample = text[:8000]
    lines = [x.strip() for x in sample.splitlines() if x.strip()]
    if len(lines) < 8:
        return False
    dialogue = sum(1 for x in lines if _DIALOGUE_MARK_RE.search(x))
    return dialogue >= 4 and dialogue / len(lines) >= 0.25


# --------------------------------------------------------------------------
# 扫描
# --------------------------------------------------------------------------

def scan_scripts(work_dir: str, *, enable_pdf: bool = True,
                 enable_docx: bool = True) -> list[dict]:
    """递归扫描作品目录，返回识别到的台本列表。"""
    out: list[dict] = []
    if not work_dir or not os.path.isdir(work_dir):
        return out

    root = os.path.realpath(work_dir)
    for cur, dirs, files in os.walk(root):
        rel_dir = os.path.relpath(cur, root)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        if depth > MAX_DEPTH:
            dirs[:] = []
            continue
        dirs[:] = [d for d in sorted(dirs) if not d.startswith(".")]
        for name in sorted(files):
            if name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in SCRIPT_EXTS:
                continue
            full = os.path.join(cur, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            if _is_blacklisted(rel):
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if size < 40 or size > 60 * 1024 * 1024:
                continue

            hinted = _in_script_dir(rel) or _name_hinted(rel)
            status = "text"
            if ext == ".pdf":
                if not enable_pdf:
                    continue
                text, status = _extract_pdf(full)
            elif ext == ".docx":
                if not enable_docx:
                    continue
                text, status = _extract_docx(full)
            else:
                try:
                    text = vtt.read_text(full, 2 * 1024 * 1024)
                except OSError:
                    continue
                if ext in (".html", ".htm"):
                    text = _strip_html(text)

            if status in ("scanned", "empty", "unsupported", "error"):
                if not hinted:
                    continue
                out.append({
                    "rel_path": rel, "path": full, "ext": ext,
                    "kind": "scanned" if status == "scanned" else "unsupported",
                    "status": status, "lang": "und", "text": "", "cues": [],
                    "line_count": 0, "confidence": 0.2,
                })
                continue

            kind = vtt.detect_timed_kind(text)
            if kind:
                cues = vtt.parse_any(text, ext)
                cues = vtt.normalize_cues(cues)
                if len(cues) < 2:
                    continue
                joined = " ".join(c["text"] for c in cues)
                out.append({
                    "rel_path": rel, "path": full, "ext": ext,
                    "kind": "timed", "status": "text", "timed_kind": kind,
                    "lang": vtt.lang_of(joined), "text": text, "cues": cues,
                    "line_count": len(cues), "confidence": 0.95 if hinted else 0.8,
                })
                continue

            if not hinted and not _content_is_script(text, rel):
                continue
            lines = clean_script_lines(text)
            if len(lines) < 3:
                continue
            lang = vtt.lang_of("\n".join(lines))
            out.append({
                "rel_path": rel, "path": full, "ext": ext,
                "kind": "plain", "status": "text",
                "lang": lang, "text": "\n".join(lines), "lines": lines,
                "cues": [], "line_count": len(lines),
                "confidence": 0.9 if hinted else 0.65,
            })

    out.sort(key=lambda s: (-s["confidence"], s["rel_path"]))
    return out


# --------------------------------------------------------------------------
# 台本 ↔ 音频配对
# --------------------------------------------------------------------------

def pair_script(script: dict, audio_rels: list[str]) -> tuple[str, float]:
    """为台本找最匹配的音频 rel，返回 (rel, confidence)。"""
    if not audio_rels:
        return "", 0.0
    rel = script.get("rel_path") or ""
    stem = normalize_stem(rel)
    best_rel, best = "", 0.0
    for a in audio_rels:
        a_stem = normalize_stem(a)
        if not a_stem:
            continue
        score = 0.0
        if a_stem == stem:
            score = 0.98
        elif a_stem and (a_stem in stem or stem in a_stem):
            score = 0.8
        else:
            sn, an = track_number(rel), track_number(a)
            if sn and an and sn == an:
                score = 0.55
            elif os.path.dirname(rel) == os.path.dirname(a) and os.path.dirname(rel):
                score = 0.35
        if score > best:
            best, best_rel = score, a
    return best_rel, best


def assign_scripts(scripts: list[dict], audio_rels: list[str]) -> dict[str, list[dict]]:
    """把台本分配到音频：{audio_rel: [script, ...]}。

    单个带时间轴台本没有明确文件名匹配时，视为整部作品共用。
    """
    mapping: dict[str, list[dict]] = {a: [] for a in audio_rels}
    for s in scripts:
        if s.get("kind") not in ("timed", "plain"):
            continue
        rel, conf = pair_script(s, audio_rels)
        if rel and conf >= 0.5:
            mapping[rel].append(dict(s, match_confidence=conf))
            continue
        # 只有一个带轴台本 → 覆盖全部音频
        timed = [x for x in scripts if x.get("kind") == "timed"]
        if s.get("kind") == "timed" and len(timed) == 1:
            for a in audio_rels:
                mapping[a].append(dict(s, match_confidence=0.5))
    return mapping


# --------------------------------------------------------------------------
# 对齐
# --------------------------------------------------------------------------

def _norm_chars(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text or "", flags=re.UNICODE).lower()


def _bigrams(text: str) -> set[str]:
    s = _norm_chars(text)
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def similarity(a: str, b: str) -> float:
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return 2 * len(ga & gb) / (len(ga) + len(gb))


def estimate_cues(lines: list[str], duration: float,
                  speech_ranges: list[tuple[float, float]] | None = None) -> list[dict]:
    """快速估算：按字数把台词铺在语音段/总时长上。"""
    items = [x for x in lines if x.strip()]
    if not items:
        return []
    total_chars = sum(max(1, len(_norm_chars(x))) for x in items)
    ranges = speech_ranges or [(0.0, max(duration, 1.0))]
    total_time = sum(max(0.1, e - s) for s, e in ranges)
    if total_time <= 0:
        return []
    cues: list[dict] = []
    ri = 0
    cursor = ranges[0][0]
    for line in items:
        weight = max(1, len(_norm_chars(line))) / total_chars
        span = max(0.8, total_time * weight)
        # 找到能放下当前句子的语音段
        while ri < len(ranges) and cursor >= ranges[ri][1]:
            ri += 1
            if ri < len(ranges):
                cursor = max(cursor, ranges[ri][0])
        if ri >= len(ranges):
            break
        end = min(cursor + span, ranges[ri][1])
        if end - cursor < 0.5:
            end = min(cursor + 0.8, ranges[ri][1])
        if end <= cursor:
            continue
        cues.append({"start": round(cursor, 3), "end": round(end, 3), "text": line})
        cursor = end
        if cursor >= ranges[ri][1] - 0.01:
            ri += 1
            if ri < len(ranges):
                cursor = max(cursor, ranges[ri][0])
    return cues


def align_plain_script(lines: list[str], segments: list[dict], duration: float,
                       min_match_ratio: float = 0.45) -> tuple[list[dict], float]:
    """用 ASR 段的时间轴对齐纯文本台本，文本以台本为准。

    返回 (cues, 匹配率)。匹配率低于阈值时返回 ([], ratio) 供上层回退。
    """
    lines = [x for x in lines if x.strip()]
    segments = [s for s in segments if (s.get("text") or "").strip()]
    if not lines or not segments:
        return [], 0.0

    matched: dict[int, int] = {}
    sims: dict[int, float] = {}
    j = 0
    for i, line in enumerate(lines):
        best_k, best_sim = -1, 0.0
        limit = min(len(segments), j + 30)
        for k in range(j, limit):
            s = similarity(line, segments[k]["text"])
            if s > best_sim:
                best_sim, best_k = s, k
        if best_k >= 0 and best_sim >= 0.18:
            matched[i] = best_k
            sims[i] = best_sim
            j = best_k + 1

    strong = sum(1 for v in sims.values() if v >= 0.3)
    ratio = strong / max(1, len(lines))
    if ratio < min_match_ratio:
        return [], ratio

    cues: list[dict] = []
    matched_items = sorted(matched.items())

    # 首尾未匹配：按字数铺在 0..首个开始 / 末个结束..duration
    first_i, first_k = matched_items[0]
    first_start = float(segments[first_k]["start"])
    if first_i > 0:
        cues.extend(estimate_cues(lines[:first_i], first_start))

    for pos, (i, k) in enumerate(matched_items):
        seg = segments[k]
        start = float(seg["start"])
        end = float(seg["end"])
        cues.append({"start": round(start, 3),
                     "end": round(max(end, start + 0.6), 3),
                     "text": lines[i]})
        if pos + 1 < len(matched_items):
            ni, nk = matched_items[pos + 1]
            next_start = float(segments[nk]["start"])
            if ni > i + 1:
                gap_lines = lines[i + 1:ni]
                inner = estimate_cues(gap_lines, max(0.1, next_start - end))
                for c in inner:
                    c["start"] = round(c["start"] + end, 3)
                    c["end"] = round(c["end"] + end, 3)
                cues.extend(inner)

    last_i, last_k = matched_items[-1]
    tail_start = float(segments[last_k]["end"])
    if last_i + 1 < len(lines) and duration > tail_start:
        inner = estimate_cues(lines[last_i + 1:], duration - tail_start)
        for c in inner:
            c["start"] = round(c["start"] + tail_start, 3)
            c["end"] = round(c["end"] + tail_start, 3)
        cues.extend(inner)

    return vtt.normalize_cues(cues), ratio


def split_chapters(text: str) -> dict[int, list[str]]:
    """单个整部台本按「Track N / ■音轨N / 第N話」切章节。"""
    chapters: dict[int, list[str]] = {}
    current = 0
    header_re = re.compile(
        r"^\s*(?:[■□◆◇●○*※\-—]?\s*)?"
        r"(?:track|音轨|音聲|音声|第)?\s*0*(\d{1,3})\s*(?:話|话|轨|曲|章|track)?\s*[:：]?\s*$",
        re.I,
    )
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        m = header_re.match(line)
        if m:
            try:
                current = int(m.group(1))
            except ValueError:
                current = 0
            chapters.setdefault(current, [])
            continue
        if current:
            cleaned = vtt.clean_text(line)
            if cleaned:
                chapters.setdefault(current, []).append(cleaned)
    return {k: v for k, v in chapters.items() if v}
