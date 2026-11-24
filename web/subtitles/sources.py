"""音源选择：同内容变体合并 + 无 SE 版本优先。

DLsite 作品常见的「同一段声音的多份副本」：
  · 格式副本：`wav/01.xxx.wav` ↔ `mp3/01.xxx.mp3` ↔ `01_wav版/…` ↔ `02_mp3版/…`
  · SE 变体：`SEあり/` ↔ `SEなし/`，`01a-【SE音 有り】` ↔ `01b-【SE音 無し】`
  · 文件名后缀变体：`01.xxx(完全版).wav` ↔ `01.xxx(射精音なし).wav` ↔ `01.xxx(SEなし).wav`
  · 反转版：`反転01_xxx.mp3`（内容为倒放，不能拿去识别）

去重算法：
  1. 名称 key：剥离变体标记（SE有/无、効果音、射精音、完全版、格式词）与序号前缀
  2. 目录上下文：每级目录同样剥离变体/格式词与序号，得到归一化父路径
  3. 分组 key = 归一目录 + 归一文件名；上下文必须一致，避免把
     `【2】梅花篇/玩法1` 和 `【3】方块篇/玩法1` 这类同名的不同章节错并
  4. 组内主音源：非反转 > 明确无 SE > 普通 > 明确 SE あり；派生片段靠后；wav/flac 优于 mp3

不剥离 `差分 / 即抜き / 切り抜き / 体験版`：这些是不同内容或片段，不能共用字幕。
"""
from __future__ import annotations

import os
import re
import unicodedata
from typing import Iterable

# SE / 音量 / 容器 / 格式：属于「同一段声音的不同封装」，可以安全剥离
_SE_MARK = (
    r"(?:"
    r"se\s*音?\s*[_\-\s]*(?:なし|無し|無|off|cut|あり|有り|有|付き|付|无)"
    r"|(?:無|无)\s*se"
    r"|(?:no|without)[_\-\s]*se"
    r"|効果音\s*[_\-\s]*(?:なし|無し|無|カット|off|cut|あり|有り|有)"
    r"|效果音\s*[_\-\s]*(?:无|無|切除|剪|有)"
    r"|射精音\s*[_\-\s]*(?:なし|無し|無|无)"
    r")"
)
_CONTAINER_MARK = r"(?:完全版|完全収録|フルバージョン|full[_\-\s]*ver(?:sion)?)"
_FORMAT_MARK = (
    r"(?:wav|mp3|flac|m4a|aac|ogg|opus|ハイレゾ|hires|24bit|16bit"
    r"|音声データ|音声数据|音声|voice|audio)"
    r"(?:版)?"
)
_VARIANT_TOKEN = re.compile(
    "|".join([_SE_MARK, _CONTAINER_MARK, _FORMAT_MARK]), re.IGNORECASE)

# 序号前缀：01. / 01_ / 01a- / Track01： / 反転01_ / おまけ01_ …
# 要求数字后面必须有分隔符，避免把 "3P" 之类的标题吃掉
_LEADING_SEQ = re.compile(
    r"^(?:反転|逆再生|リバース|reversed?)?"
    r"[^\d]{0,6}?\d{1,3}[a-c]?\s*(?:[\.\-_、,，:：·・\s]+)",
    re.IGNORECASE,
)

# 保留：数字、拉丁字母、平假名、片假名、CJK、々〆ー
_NOISE = re.compile(r"[^0-9a-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff々〆ー]+")

# 优先级判断（基于原始相对路径，不做归一）
_SELESS_RE = re.compile(
    r"(?:"
    r"se\s*音?\s*[_\-\s]*(?:なし|無し|無|off|cut|无)"
    r"|(?:無|无)\s*se"
    r"|(?:no|without)[_\-\s]*se"
    r"|効果音\s*[_\-\s]*(?:なし|無し|無|カット|off|cut)"
    r"|效果音\s*[_\-\s]*(?:无|無|切除|剪)"
    r")",
    re.IGNORECASE,
)
_SE_PRESENT_RE = re.compile(
    r"(?:se\s*音?\s*[_\-\s]*(?:あり|有り|有|付き|付)"
    r"|効果音\s*[_\-\s]*(?:あり|有り|有))",
    re.IGNORECASE,
)
_REVERSED_RE = re.compile(r"反転|逆再生|リバース|reversed?|ぎゃくさいせい", re.IGNORECASE)
_DERIVED_RE = re.compile(r"即抜き|切り抜き|抜粋|ダイジェスト|体験版|お試し|short", re.IGNORECASE)
_LOSSLESS = {".wav", ".flac", ".aiff", ".aif"}
# 目录段剥离变体词后只剩这些时视为空段（如 `03_SEなし版` → `版`）
_EMPTY_SEGMENTS = {"版", "フォルダ", "ファイル", "file", "files", "ver", "version"}


def _clean(text: str) -> str:
    """NFKC + 去变体标记 + 去序号 + 去符号，得到稳定的比较 key。"""
    s = unicodedata.normalize("NFKC", text or "")
    s = _VARIANT_TOKEN.sub(" ", s)
    s = _LEADING_SEQ.sub("", s, count=1)
    s = _NOISE.sub("", s)
    return s.lower()


def _name_key(rel: str) -> str:
    name = os.path.basename(rel)
    key = _clean(os.path.splitext(name)[0])
    # 名字被剥空时退回原始 stem，避免所有异常文件挤进同一组
    if not key:
        key = _NOISE.sub("", unicodedata.normalize("NFKC", os.path.splitext(name)[0])).lower()
    return key


def _context_key(rel: str) -> str:
    """归一化父目录：格式/SE/序号目录段变空，只保留真正的章节结构。"""
    parts = [p for p in os.path.dirname(rel).replace("\\", "/").split("/") if p]
    segs = [_clean(x) for x in parts]
    return "/".join(s for s in segs if s and s not in _EMPTY_SEGMENTS)


def _priority(rel: str, index: int) -> tuple:
    lowered = rel
    ext = os.path.splitext(rel)[1].lower()
    if _SELESS_RE.search(lowered):
        se_rank = 0
    elif _SE_PRESENT_RE.search(lowered):
        se_rank = 2
    else:
        se_rank = 1
    return (
        1 if _REVERSED_RE.search(lowered) else 0,   # 反转版永不做主音源
        se_rank,                                     # 无 SE > 未知 > SE あり
        1 if _DERIVED_RE.search(lowered) else 0,     # 派生片段靠后
        0 if ext in _LOSSLESS else 1,                # 无损优先（ASR 更准）
        index,
    )


def group_audio_tracks(rel_paths: Iterable[str]) -> list[list[str]]:
    """把 rel_paths 按「归一目录 + 归一文件名」分组。

    返回 [[rel1, rel2], ...]，组内第一项为推荐做 ASR 的主音源；
    组顺序 = 该组第一次出现的顺序（保持作品原本的音轨顺序）。
    """
    paths = list(rel_paths)
    buckets: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for i, rel in enumerate(paths):
        buckets.setdefault((_context_key(rel), _name_key(rel)), []).append((i, rel))

    out: list[list[str]] = []
    for items in buckets.values():
        ordered = sorted(items, key=lambda t: _priority(t[1], t[0]))
        out.append([rel for _, rel in ordered])
    return out


def pick_asr_source(group: list[str]) -> str:
    """组内第一项即主音源（无 SE / 非反转 / 无损优先）。"""
    if not group:
        raise ValueError("empty group")
    return group[0]


def collect_work_tracks(work_root: str, audio_exts: tuple[str, ...]) -> list[list[str]]:
    """扫描作品根目录，返回去重后的音频分组。

    返回 [[rel, ...], ...]，每个子列表是同一段声音的不同封装；
    组内第一项就是推荐做 ASR 的音源。
    """
    root = os.path.realpath(work_root)
    tracks: list[str] = []
    for dp, _dirs, names in os.walk(root):
        for name in names:
            if os.path.splitext(name)[1].lower() in audio_exts:
                full = os.path.join(dp, name)
                tracks.append(os.path.relpath(full, root).replace(os.sep, "/"))
    tracks.sort()
    return group_audio_tracks(tracks)
