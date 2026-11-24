"""字幕格式处理 — VTT/SRT/LRC/ASS 解析、生成、清洗、内容哈希。"""
from __future__ import annotations

import hashlib
import os
import re

# --------------------------------------------------------------------------
# 编码与语言
# --------------------------------------------------------------------------

_ENCODINGS = ("utf-8-sig", "utf-8", "cp932", "shift_jis", "gb18030", "gbk", "utf-16")

_KANA_RE = re.compile(r"[\u3040-\u30ff\uff66-\uff9f]")
_HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def decode_bytes(raw: bytes) -> str:
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def read_text(path: str, limit: int | None = None) -> str:
    with open(path, "rb") as f:
        raw = f.read(limit) if limit else f.read()
    return decode_bytes(raw)


def is_ja_text(text: str) -> bool:
    """假名占比 > 15% 视为日文。"""
    plain = re.sub(r"\s+", "", text or "")
    if not plain:
        return False
    kana = len(_KANA_RE.findall(plain))
    return kana and kana / len(plain) > 0.15


def is_zh_text(text: str) -> bool:
    """有汉字且假名很少，视为中文。"""
    plain = re.sub(r"\s+", "", text or "")
    if not plain:
        return False
    han = len(_HAN_RE.findall(plain))
    kana = len(_KANA_RE.findall(plain))
    return han >= 2 and kana / max(1, len(plain)) < 0.05


def lang_of(text: str) -> str:
    if is_ja_text(text):
        return "ja"
    if is_zh_text(text):
        return "zh"
    return "und"


def hash_text(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# 时间
# --------------------------------------------------------------------------

_TS_RE = re.compile(
    r"(?:(\d{1,2}):)?(\d{1,3}):(\d{1,2})(?:[.,](\d{1,3}))?"
)


def parse_timestamp(raw: str) -> float:
    """支持 HH:MM:SS.mmm / MM:SS.mmm / H:MM:SS,mmm。"""
    m = _TS_RE.search(raw or "")
    if not m:
        return 0.0
    h = int(m.group(1) or 0)
    mi = int(m.group(2))
    s = int(m.group(3))
    frac = (m.group(4) or "0")
    frac = frac + "0" * (3 - len(frac))
    return h * 3600 + mi * 60 + s + int(frac[:3]) / 1000.0


def fmt_timestamp(seconds: float) -> str:
    if not seconds or seconds < 0:
        seconds = 0
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


# --------------------------------------------------------------------------
# 格式识别与解析
# --------------------------------------------------------------------------

_CUE_TIME_RE = re.compile(
    r"^\s*(?:\[)?(\d{1,2}:\d{1,3}:\d{1,2}(?:[.,]\d{1,3})?|\d{1,3}:\d{1,2}(?:[.,]\d{1,3})?)"
    r"\s*-->\s*"
    r"(\d{1,2}:\d{1,3}:\d{1,2}(?:[.,]\d{1,3})?|\d{1,3}:\d{1,2}(?:[.,]\d{1,3})?)(?:\s|$)"
)
_LRC_RE = re.compile(r"^\s*\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]\s*(.*)$")
_SRT_INDEX_RE = re.compile(r"^\s*\d{1,5}\s*$")


def detect_timed_kind(text: str) -> str:
    """判断文本是否自带时间轴，返回 vtt/srt/lrc/ass 或空串。"""
    if not text:
        return ""
    head = text[:6000]
    if "WEBVTT" in head[:200].upper():
        return "vtt"
    if re.search(r"^Dialogue\s*:", head, re.M):
        return "ass"
    cue_hits = 0
    for line in head.splitlines():
        if _CUE_TIME_RE.match(line):
            cue_hits += 1
            if cue_hits >= 2:
                return "srt"
    lrc_hits = sum(1 for line in head.splitlines() if _LRC_RE.match(line))
    if lrc_hits >= 3:
        return "lrc"
    return ""


def parse_any(text: str, ext: str = "") -> list[dict]:
    """解析字幕为 [{start, end, text}]，自动识别格式。"""
    kind = detect_timed_kind(text)
    ext = (ext or "").lower()
    if not kind:
        if ext == ".lrc":
            kind = "lrc"
        elif ext in (".ass", ".ssa"):
            kind = "ass"
        elif ext == ".vtt":
            kind = "vtt"
        else:
            kind = "srt"
    if kind == "lrc":
        return _parse_lrc(text)
    if kind == "ass":
        return _parse_ass(text)
    return _parse_cue_blocks(text)


def _parse_cue_blocks(text: str) -> list[dict]:
    """VTT/SRT 通用：按时间行切块。"""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cues: list[dict] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        m = _CUE_TIME_RE.match(line)
        if not m:
            i += 1
            continue
        start = parse_timestamp(m.group(1))
        end = parse_timestamp(m.group(2))
        i += 1
        body: list[str] = []
        while i < n and lines[i].strip() and not _CUE_TIME_RE.match(lines[i]):
            if not _SRT_INDEX_RE.match(lines[i]):
                body.append(lines[i].strip())
            i += 1
        body_text = " ".join(x for x in body if x).strip()
        body_text = clean_text(body_text)
        if body_text and end > start:
            cues.append({"start": start, "end": end, "text": body_text})
    return cues


def _parse_lrc(text: str) -> list[dict]:
    items: list[tuple[float, str]] = []
    for line in (text or "").splitlines():
        m = _LRC_RE.match(line)
        if not m:
            continue
        mi = int(m.group(1))
        s = int(m.group(2))
        frac = (m.group(3) or "0")
        t = mi * 60 + s + int(frac[:3].ljust(3, "0")) / 1000.0
        body = clean_text(m.group(4))
        if body:
            items.append((t, body))
    items.sort(key=lambda x: x[0])
    cues = []
    for idx, (t, body) in enumerate(items):
        end = items[idx + 1][0] if idx + 1 < len(items) else t + 4.0
        cues.append({"start": t, "end": max(end, t + 0.5), "text": body})
    return cues


_ASS_DIALOGUE_RE = re.compile(r"^Dialogue:\s*[^,]*,\s*([^,]+),\s*([^,]+),")


def _parse_ass(text: str) -> list[dict]:
    cues: list[dict] = []
    for line in (text or "").splitlines():
        if not line.strip().startswith("Dialogue:"):
            continue
        parts = line.split(",", 9)
        if len(parts) < 10:
            continue
        start = parse_timestamp(parts[1])
        end = parse_timestamp(parts[2])
        body = re.sub(r"\{[^}]*\}", "", parts[9]).replace(r"\N", " ")
        body = clean_text(body)
        if body and end > start:
            cues.append({"start": start, "end": end, "text": body})
    return cues


# --------------------------------------------------------------------------
# 文本清洗
# --------------------------------------------------------------------------

_SPEAKER_RE = re.compile(r"^\s*(?:[【\[（(][^】\]）)]{1,12}[】\]）)]\s*[:：]?\s*)+")
_NOTE_RE = re.compile(r"[（(][^）)]{0,30}(?:効果音|効果|音|笑|ため息|吐息|喘|舐|水音|キス|SE|BGM)[^）)]{0,30}[）)]", re.I)
_MUSIC_RE = re.compile(r"^\s*[♪♫♬♩\s]*$")


def clean_text(text: str) -> str:
    s = (text or "").strip()
    s = re.sub(r"<[^>]{1,80}>", "", s)          # vtt 标签
    s = s.replace(r"\N", " ").replace(r"\n", " ")
    s = _SPEAKER_RE.sub("", s)
    s = _NOTE_RE.sub("", s)
    s = re.sub(r"^\s*[-–—]\s*", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    if _MUSIC_RE.match(s):
        return ""
    return s


def normalize_cues(cues: list[dict], *, max_duration: float = 8.0,
                   min_duration: float = 0.8) -> list[dict]:
    """合并碎片、限制单条时长、丢弃空句与重叠。"""
    cleaned: list[dict] = []
    for c in cues or []:
        text = clean_text(c.get("text") or "")
        start = float(c.get("start") or 0)
        end = float(c.get("end") or 0)
        if not text or end <= start:
            continue
        if end - start > max_duration:
            end = start + max_duration
        cleaned.append({"start": start, "end": end, "text": text})

    cleaned.sort(key=lambda x: (x["start"], x["end"]))
    merged: list[dict] = []
    for c in cleaned:
        if merged:
            prev = merged[-1]
            too_short = (prev["end"] - prev["start"]) < min_duration
            near = c["start"] - prev["end"] < 0.35
            if too_short and near and len(prev["text"]) <= 24:
                prev["text"] = (prev["text"] + c["text"]).strip()
                prev["end"] = max(prev["end"], c["end"])
                continue
            if c["start"] < prev["end"]:
                c["start"] = prev["end"]
                if c["end"] <= c["start"]:
                    continue
        merged.append(c)
    return merged


def to_vtt(cues: list[dict]) -> str:
    out = ["WEBVTT", ""]
    for c in normalize_cues(cues):
        out.append(f"{fmt_timestamp(c['start'])} --> {fmt_timestamp(c['end'])}")
        out.append(c["text"])
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def to_vtt_raw(cues: list[dict]) -> str:
    """同 to_vtt，但保留 cue 内的换行（双语字幕「日文\\n中文」需要）。"""
    items: list[dict] = []
    for c in cues or []:
        parts = [clean_text(p) for p in str(c.get("text") or "").split("\n")]
        body = "\n".join(p for p in parts if p)
        if not body:
            continue
        start = float(c.get("start") or 0)
        end = float(c.get("end") or 0)
        if end <= start:
            continue
        if end - start > 8.0:
            end = start + 8.0
        items.append({"start": start, "end": end, "text": body})
    items.sort(key=lambda x: (x["start"], x["end"]))
    out = ["WEBVTT", ""]
    for c in items:
        out.append(f"{fmt_timestamp(c['start'])} --> {fmt_timestamp(c['end'])}")
        out.append(c["text"])
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _bilingual_block(body: list[str]) -> tuple[str, str] | None:
    """cue 的正文行 → (日文, 中文)；不像双语时返回 None。"""
    lines = [x for x in (y.strip() for y in body) if x]
    if len(lines) < 2:
        return None
    ja = lines[0]
    zh = " ".join(lines[1:])
    zh_ok = (not is_ja_text(zh)) and bool(_HAN_RE.search(zh))
    if is_ja_text(ja) and zh_ok:
        return ja, zh
    return None


def detect_bilingual(text: str) -> bool:
    """判断整份字幕是否是「日文一行 + 中文一行」的双语字幕。"""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    n = len(lines)
    while i < n:
        if not _CUE_TIME_RE.match(lines[i]):
            i += 1
            continue
        i += 1
        body: list[str] = []
        while i < n and lines[i].strip() and not _CUE_TIME_RE.match(lines[i]):
            body.append(lines[i])
            i += 1
        if _bilingual_block(body):
            return True
    return False


def split_bilingual(text: str) -> tuple[str, str] | None:
    """把双语字幕拆成纯日文 VTT 与纯中文 VTT；不是双语时返回 None。"""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ja_out = ["WEBVTT", ""]
    zh_out = ["WEBVTT", ""]
    found = False
    i = 0
    n = len(lines)
    while i < n:
        stamp = lines[i]
        if not _CUE_TIME_RE.match(stamp):
            i += 1
            continue
        i += 1
        body: list[str] = []
        while i < n and lines[i].strip() and not _CUE_TIME_RE.match(lines[i]):
            body.append(lines[i])
            i += 1
        pair = _bilingual_block(body)
        if not pair:
            continue
        found = True
        for out, line in ((ja_out, pair[0]), (zh_out, pair[1])):
            out.append(stamp)
            out.append(line)
            out.append("")
    if not found:
        return None
    return ("\n".join(ja_out).rstrip() + "\n",
            "\n".join(zh_out).rstrip() + "\n")


def atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)

