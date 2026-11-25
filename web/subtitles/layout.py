"""「生成字幕」目录布局。

只保留三个子目录，字幕文件直接放在里面、不建作品子目录：

    生成字幕/日文版/日文_RJ01426915_01_本編.vtt
    生成字幕/中文版/中文_RJ01426915_01_本編.vtt
    生成字幕/双语版/双语_RJ01426915_01_本編.vtt

文件名 = ``<类别>_<RJ号>_<展平后的音频相对路径>.vtt``。文件名按 UTF-8 字节
长度截断并追加哈希，避免超过文件系统的 NAME_MAX。
"""
from __future__ import annotations

import hashlib
import os
import re

from . import vtt

GENERATED_DIR = "生成字幕"
LEGACY_SUFFIX = ".zh.vtt"
LEGACY_SUFFIXES = (LEGACY_SUFFIX, ".ja.vtt")

CATS = ("ja", "zh", "bi")
LABEL = {"ja": "日文", "zh": "中文", "bi": "双语"}
DIRNAME = {"ja": "日文版", "zh": "中文版", "bi": "双语版"}
LABEL_TO_CAT = {v: k for k, v in LABEL.items()}

MAX_NAME_BYTES = 180
_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _sanitize(text: str) -> str:
    out = _BAD.sub("_", text or "").strip().strip(".")
    return out or "sub"


def _truncate(name: str, max_bytes: int = MAX_NAME_BYTES) -> str:
    raw = name.encode("utf-8")
    if len(raw) <= max_bytes:
        return name
    digest = hashlib.sha1(raw).hexdigest()[:8]
    suffix = "_" + digest
    # 掐头去尾会丢掉音轨名，导致同版本不同音轨的文件看起来一样：
    # 保留开头（类别/RJ/版本）+ 结尾（音轨名）+ 哈希
    budget = max(16, max_bytes - len(suffix.encode("utf-8")) - 3)  # 3 = "…"
    head_n = max(8, int(budget * 0.45))
    tail_n = max(8, budget - head_n)
    head = raw[:head_n].decode("utf-8", "ignore")
    tail = raw[-tail_n:].decode("utf-8", "ignore")
    return head + "…" + tail + suffix


def work_tag(work_id: str) -> str:
    tag = _sanitize(str(work_id or "")).replace(" ", "")
    return tag.upper() or "WORK"


def filename(work_id: str, rel: str, cat: str) -> str:
    if cat not in LABEL:
        raise ValueError(f"unknown category: {cat}")
    stem = os.path.splitext((rel or "").replace("\\", "/"))[0]
    flat = _sanitize(stem.replace("/", "_"))
    base = f"{LABEL[cat]}_{work_tag(work_id)}_{flat}"
    return _truncate(base) + ".vtt"


def dir_path(root: str, cat: str) -> str:
    return os.path.join(root, GENERATED_DIR, DIRNAME[cat])


def path(root: str, work_id: str, rel: str, cat: str) -> str:
    return os.path.join(dir_path(root, cat), filename(work_id, rel, cat))


def find(root: str, work_id: str, rel: str, cat: str) -> str:
    p = path(root, work_id, rel, cat)
    return p if os.path.isfile(p) else ""


def exists_translated(root: str, work_id: str, rel: str) -> bool:
    """中文版或双语版任一存在即视为已翻译。"""
    return bool(find(root, work_id, rel, "zh") or find(root, work_id, rel, "bi"))


def exists_ja(root: str, work_id: str, rel: str) -> bool:
    return bool(find(root, work_id, rel, "ja"))


def write(root: str, work_id: str, rel: str, cat: str, text: str) -> str:
    out = path(root, work_id, rel, cat)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    vtt.atomic_write(out, text)
    return out


def read(root: str, work_id: str, rel: str, cat: str) -> str:
    p = find(root, work_id, rel, cat)
    return vtt.read_text(p) if p else ""


def detect_category(text: str) -> str:
    """按内容判断一份字幕属于哪一类。"""
    if vtt.detect_bilingual(text or ""):
        return "bi"
    cues = vtt.parse_any(text or "", ".vtt")
    joined = " ".join((c.get("text") or "") for c in cues)
    if vtt.is_zh_text(joined) and not vtt.is_ja_text(joined):
        return "zh"
    return "ja"


def remove(root: str, work_id: str, rel: str, cat: str | None = None) -> None:
    targets = [path(root, work_id, rel, c) for c in (CATS if cat is None else (cat,))]
    for p in targets:
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass


def cleanup_group(root: str, work_id: str, group: list[str]) -> None:
    """一组同 stem 音频只保留主音源的字幕：清掉其余成员的新旧文件。

    注意：文件名按「去扩展名后的相对路径」生成，同组不同格式（mp3/wav）
    会落到同一个文件名，此时不能删除，否则会把主音源刚写的文件删掉。
    """
    if not group:
        return
    keep = {filename(work_id, rel, c) for rel in group[:1] for c in CATS}
    for rel in group[1:]:
        for cat in CATS:
            if filename(work_id, rel, cat) in keep:
                continue
            p = path(root, work_id, rel, cat)
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass
        legacy = os.path.join(root, GENERATED_DIR, rel + LEGACY_SUFFIX)
        try:
            if os.path.isfile(legacy):
                os.remove(legacy)
        except OSError:
            pass


def has_generated(root: str, work_id: str = "") -> bool:
    """生成字幕 目录下是否存在任意字幕文件（三个类目目录）。"""
    for cat in CATS:
        d = dir_path(root, cat)
        try:
            if os.path.isdir(d) and any(f.lower().endswith(".vtt") for f in os.listdir(d)):
                return True
        except OSError:
            continue
    return False


def has_translated(root: str, work_id: str = "") -> bool:
    """是否已有中文版或双语版（纯日文缓冲不算「已生成」）。"""
    for cat in ("zh", "bi"):
        d = dir_path(root, cat)
        try:
            if os.path.isdir(d) and any(f.lower().endswith(".vtt") for f in os.listdir(d)):
                return True
        except OSError:
            continue
    return False


def has_ja(root: str) -> bool:
    """日文版里是否有识别结果。"""
    d = dir_path(root, "ja")
    try:
        return os.path.isdir(d) and any(f.lower().endswith(".vtt")
                                        for f in os.listdir(d))
    except OSError:
        return False


def has_pending_translation(root: str, work_id: str,
                            disabled: set[str] | None = None,
                            durations: dict | None = None) -> bool:
    """是否有音轨已识别出日文、但还没生成中文版/双语版。

    逐个音轨比对三个类目下的实际文件，而不是看目录里有没有文件：
    日文版有对应文件、中文版和双语版都没有时才算待翻译。
    ``disabled`` 里被用户取消勾选的音轨组不算待翻译。
    """
    return analyze_tracks(root, work_id, disabled, durations)["buffered"]


def analyze_tracks(root: str, work_id: str,
                   disabled: set[str] | None = None,
                   durations: dict | None = None) -> dict[str, bool]:
    """一次扫描得出音轨翻译状态。

    ``buffered``：日文版里还留着没有译文的识别结果 → 待翻译。
    ``complete``：没有待翻译的日文，且至少有一份中文/双语译文。

    判定按「日文版实际文件」逐个比对：无论是一整条音轨没译，还是
    新识别出了日文、或中文文件被删掉，只要还有日文没译文就不算完成。
    同一 stem 的多个音源（SE 有/无）共用同一份译文，比对主音源即可。

    ``durations`` 来自 audio_index；本路径 probe=False，避免轮询时 spawn ffprobe。
    """
    from . import AUDIO_EXTS
    from .sources import collect_work_tracks

    groups = collect_work_tracks(root, AUDIO_EXTS, durations=durations, probe=False)
    main_of: dict[str, str] = {}
    group_of: dict[str, list[str]] = {}
    ja_names: dict[str, str] = {}
    for group in groups:
        for rel in group:
            main_of[rel] = group[0]
            group_of[rel] = group
            ja_names[filename(work_id, rel, "ja")] = rel

    try:
        names = os.listdir(dir_path(root, "ja"))
    except OSError:
        names = []

    buffered = False
    seen: set[str] = set()
    for name in names:
        if not name.lower().endswith(".vtt"):
            continue
        rel = ja_names.get(name)
        if not rel:
            continue  # 对应音频已不存在，旧文件不参与判定
        main = main_of[rel]
        if main in seen:
            continue
        seen.add(main)
        if disabled and all(m in disabled for m in group_of[rel]):
            continue
        if not exists_translated(root, work_id, main):
            buffered = True

    return {"buffered": buffered, "complete": has_translated(root) and not buffered}


def cleanup_orphan_ja(root: str, work_id: str,
                      durations: dict | None = None) -> int:
    """删除日文版里对应音频已不存在的 vtt（孤儿文件）。

    音源没了就永远不可能再生成译文，留着会破坏「日文版所有文件都有
    对应译文」的完整性目标，也干扰待翻译判定。返回删除数量。
    """
    from . import AUDIO_EXTS
    from .sources import collect_work_tracks

    valid = {filename(work_id, rel, "ja")
             for group in collect_work_tracks(root, AUDIO_EXTS,
                                              durations=durations, probe=False)
             for rel in group}
    d = dir_path(root, "ja")
    try:
        names = os.listdir(d)
    except OSError:
        return 0
    removed = 0
    for name in names:
        if not name.lower().endswith(".vtt") or name in valid:
            continue
        try:
            os.remove(os.path.join(d, name))
            removed += 1
        except OSError:
            pass
    return removed


def ja_mtime(root: str) -> float:
    """日文版目录下最新的文件修改时间，没有则 0。"""
    d = dir_path(root, "ja")
    newest = 0.0
    try:
        for name in os.listdir(d):
            if not name.lower().endswith(".vtt"):
                continue
            try:
                newest = max(newest, os.path.getmtime(os.path.join(d, name)))
            except OSError:
                continue
    except OSError:
        return 0.0
    return newest


def scan(root: str) -> list[dict]:
    """列出三个类目目录下的全部字幕，返回 {cat, name, rel_path, local_path}。"""
    out: list[dict] = []
    for cat in CATS:
        d = dir_path(root, cat)
        if not os.path.isdir(d):
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if not name.lower().endswith(".vtt"):
                continue
            full = os.path.join(d, name)
            out.append({
                "cat": cat,
                "name": name,
                "rel_path": os.path.relpath(full, root).replace("\\", "/"),
                "local_path": full,
            })
    return out


def remove_work(root: str) -> int:
    """删除作品下的整个 生成字幕 目录，返回删除的文件数。"""
    import shutil
    gdir = os.path.join(root, GENERATED_DIR)
    if not os.path.isdir(gdir):
        return 0
    n = 0
    for _dp, _dn, fns in os.walk(gdir):
        n += len(fns)
    shutil.rmtree(gdir, ignore_errors=True)
    return n


def migrate_work(root: str, work_id: str) -> int:
    """把旧版 ``生成字幕/<rel>.zh.vtt`` / ``.ja.vtt`` 迁移到新的三目录布局。"""
    gdir = os.path.join(root, GENERATED_DIR)
    if not os.path.isdir(gdir):
        return 0
    cat_dirs = {os.path.realpath(dir_path(root, c)) for c in CATS}
    moved = 0
    for cur, dirs, files in os.walk(gdir, topdown=False):
        if os.path.realpath(cur) in cat_dirs:
            dirs[:] = []
            continue
        for fn in files:
            low = fn.lower()
            suf = next((s for s in LEGACY_SUFFIXES if low.endswith(s)), "")
            if not suf:
                continue
            full = os.path.join(cur, fn)
            rel = os.path.relpath(full, gdir).replace("\\", "/")
            rel = rel[: -len(suf)]
            try:
                text = vtt.read_text(full)
            except OSError:
                continue
            cat = detect_category(text)
            try:
                if cat == "bi":
                    pair = vtt.split_bilingual(text)
                    write(root, work_id, rel, "bi", text)
                    if pair:
                        write(root, work_id, rel, "ja", pair[0])
                        write(root, work_id, rel, "zh", pair[1])
                else:
                    write(root, work_id, rel, cat, text)
                os.remove(full)
                moved += 1
            except OSError:
                continue
        # 清掉空目录（不含三个类目目录）
        if os.path.realpath(cur) not in cat_dirs and os.path.realpath(cur) != os.path.realpath(gdir):
            try:
                if not os.listdir(cur):
                    os.rmdir(cur)
            except OSError:
                pass
    return moved
