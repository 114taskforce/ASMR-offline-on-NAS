"""音频指纹 — chromaprint（fpcalc）优先，ffprobe 时长兜底。"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess

FP_METHOD_CHROMA = "chromaprint"
FP_METHOD_DURATION = "duration"

_SEQ_RE = re.compile(r"(?:track|音轨|第)?\s*0*(\d{1,3})\s*(?:話|话|轨|曲)?", re.I)
_BRACKET_RE = re.compile(r"[【\[（(][^】\]）)]*[】\]）)]")


def normalize_stem(name: str) -> str:
    """去掉扩展名、序号、括号内容与符号，用于跨版本匹配同一音轨。"""
    stem = os.path.splitext(os.path.basename(name or ""))[0]
    stem = _BRACKET_RE.sub("", stem)
    stem = re.sub(r"\.(mp3|wav|flac|m4a|aac|ogg|opus)$", "", stem, flags=re.I)
    stem = re.sub(r"[\s♡♪♫♬♩・·_\-—–]+", "", stem)
    return stem.lower()


def track_number(name: str) -> int:
    """从文件名提取音轨序号，识别不了返回 0。"""
    stem = os.path.splitext(os.path.basename(name or ""))[0]
    m = _SEQ_RE.search(stem)
    if not m:
        return 0
    try:
        return int(m.group(1))
    except ValueError:
        return 0


def _which(name: str) -> str | None:
    return shutil.which(name)


def probe_duration(path: str) -> float:
    ffprobe = _which("ffprobe")
    if not ffprobe:
        return 0.0
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30,
        )
        return float((out.stdout or "0").strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def fingerprint_audio(path: str, duration: float = 0.0) -> tuple[str, str]:
    """返回 (fingerprint, method)。fpcalc 缺失时用时长+主干名兜底。"""
    fpcalc = _which("fpcalc")
    if fpcalc:
        try:
            out = subprocess.run(
                [fpcalc, "-raw", "-length", "120", path],
                capture_output=True, text=True, timeout=120,
            )
            raw = ""
            for line in (out.stdout or "").splitlines():
                if line.startswith("FINGERPRINT="):
                    raw = line.split("=", 1)[1].strip()
                    break
            if raw:
                return hashlib.sha1(raw.encode("utf-8")).hexdigest(), FP_METHOD_CHROMA
        except (OSError, subprocess.SubprocessError):
            pass

    if not duration:
        duration = probe_duration(path)
    basis = f"{round(duration, 1)}|{normalize_stem(path)}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest(), FP_METHOD_DURATION


def has_fpcalc() -> bool:
    return bool(_which("fpcalc"))
