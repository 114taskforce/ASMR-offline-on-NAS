"""音源分组：同内容判定 = 名称归一相似度 ≥95% 且音频时长几乎一致。

旧方案按「归一化父目录 + 归一化文件名」精确分桶，每遇到一种新命名变体
（SE 有/无、含音效、早期购入版、`【TrN】`、截断哈希…）就要补一条正则。

现改为内容级判定：
  1. 名称预处理：剥扩展名、下载截断哈希、括号/开头序号、SE 与格式 tag，
     只留纯标题（`_VARIANT_TOKEN` 仍是剥离规则表）
  2. 标题编辑距离 < 5 字符（绝对字符差异，不用百分比）
  3. 时长差 ≤ max(2s, min(25s, 长度 × 5%))
     · wav/mp3 同源副本：实测差 0~1.7s
     · SE 有/无版本：实测最多差 ~20s
     · 不同章节/体验版片段：差几十秒到几十分钟，天然被排除
  4. 时长缺失（未识别过、也探测不到）时退化为「标题完全相同」才合并

主音源仍按 `_priority` 选择：非反转 > 无 SE > 未知 > SEあり，无损优先。

`durations` 由调用方从 audio_index 批量传入；`probe=True` 时缺失时长会用
ffprobe 补测并缓存到进程内（按 路径+mtime+size 校验失效）。判定/轮询路径
保持 `probe=False`，避免反复 spawn ffprobe。
"""
from __future__ import annotations

import os
import re
import threading
import unicodedata
from collections import Counter
from typing import Iterable

# SE / 音量 / 容器 / 格式：属于「同一段声音的不同封装」，可安全剥离
_SE_MARK = (
    r"(?:"
    r"se\s*音?\s*[_\-\s]*(?:なし|無し|無|off|cut|あり|有り|有|付き|付|无)"
    r"|(?:無|无)\s*se"
    r"|(?:no|without)[_\-\s]*se"
    r"|効果音\s*[_\-\s]*(?:なし|無し|無|カット|off|cut|あり|有り|有)"
    r"|效果音\s*[_\-\s]*(?:无|無|切除|剪|有)"
    r"|射精音\s*[_\-\s]*(?:なし|無し|無|无)"
    r"|含\s*(?:se|音效|効果音|音声)"
    r")"
)
_CONTAINER_MARK = r"(?:完全版|完全収録|フルバージョン|full[_\-\s]*ver(?:sion)?)"
_FORMAT_MARK = (
    r"(?:wav|mp3|flac|m4a|aac|ogg|opus|ハイレゾ|hires|24bit|16bit"
    r"|音声データ|音声数据|音声|voice|audio)"
    r"(?:版)?"
)
# 早期购入版：与正式版同一段声音的先行发售包装；
# 不含「特典」字样的组合（早期購入特典是不同内容，仍作为独立章节保留）
_EARLY_BIRD_MARK = r"(?:(?:早期|初期)[購购]入(?:版)?|正式版|通常版|通常盤)"
_VARIANT_TOKEN = re.compile(
    "|".join([_SE_MARK, _CONTAINER_MARK, _FORMAT_MARK, _EARLY_BIRD_MARK]),
    re.IGNORECASE)

# 括号包裹的混音/包装补充说明，只剥「纯标记」括号：
#   `（ルームver）` `(おまけ)` `（SE無し）` `（完全版）`
# 不能无条件剥所有括号——`（耳元ごっくんver）` `（飲尿ver）` `（前編）` 是内容
# 标识，剥掉会让不同音轨撞名、被误并成同一组（少生成一份字幕）。
_PAREN_TAG = re.compile(
    r"[\(\（]\s*(?:"
    r"ルーム(?:ver|バージョン)?|room[_\-\s]*ver(?:sion)?"
    r"|おまけ|オマケ|完全版|フルバージョン"
    r"|se\s*[無な无しあり有]?\s*[しな無有无]?"
    r"|効果音[^\(\)（）]{0,6}|效果音[^\(\)（）]{0,6}"
    r")\s*[\)\）]",
    re.IGNORECASE)

# 括号内序号：`【Tr1】` / `【01】` / `[Track02]` / `（第1話）`
_SEQ_BR = re.compile(
    r"[【\[（(]\s*(?:tr|track|トラック|no\.?|第)?\s*0*\d{1,3}[a-c]?\s*"
    r"(?:話|话|轨|曲)?\s*[\.\-_\s]*[】\]）)]",
    re.IGNORECASE)
# 开头序号：`01 序幕` / `02_xxx` / `■02_xxx` / `SEなし_7 xxx`（剥 tag 后可能残留 `_`）
# 数字后必须有分隔符，避免把 `3P` 之类的标题吃掉
_SEQ_LEAD = re.compile(
    r"^[^\d]{0,8}?\d{1,3}[a-c]?\s*(?:話|话|轨|曲)?\s*[\.\-_\s、,，:：·・]+",
    re.IGNORECASE)

# 保留：数字、拉丁字母、平假名、片假名、CJK、々〆ー
_NOISE = re.compile(r"[^0-9a-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff々〆ー]+")

# 下载保名截断尾巴：-702e3c / _A4ADAD（6 位十六进制）
_TRUNC_HASH = re.compile(r"[-_\s]?[0-9a-f]{6}$", re.IGNORECASE)

# 主音源优先级判断（基于原始相对路径，不做归一）
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
    r"|効果音\s*[_\-\s]*(?:あり|有り|有)"
    r"|含\s*(?:se|音效|効果音|音声))",
    re.IGNORECASE,
)
_REVERSED_RE = re.compile(
    r"反転|逆再生|リバース|reversed?|ぎゃくさいせい|左右反転", re.IGNORECASE)
_DERIVED_RE = re.compile(r"即抜き|切り抜き|抜粋|ダイジェスト|体験版|体验版|お試し|short"
                         r"|ルームエフェクト|ルームver|room[_\-\s]*ver",
                         re.IGNORECASE)
_LOSSLESS = {".wav", ".flac", ".aiff", ".aif"}

# 相似度与时长容差
# 不用百分比：短标题差 1 个字 ratio 就跌破 95%，长标题差 3 个字却仍 0.99。
# 改成绝对字符差异——归一标题的编辑距离 < 5 且时长一致即视为同一段声音。
_MAX_DIFF_CHARS = 5
_DUR_ABS_MIN = 2.0       # 秒
_DUR_ABS_MAX = 25.0      # 秒（长音频最多放宽到 25s）
_DUR_REL = 0.05

_DUR_CACHE: dict[str, tuple[float, int, float]] = {}
_DUR_LOCK = threading.Lock()


def _sim_key(rel: str) -> str:
    """归一化标题：剥扩展名 / 截断哈希 / 变体 tag / 序号 / 符号。"""
    stem = os.path.splitext(os.path.basename(rel))[0]
    s = unicodedata.normalize("NFKC", _TRUNC_HASH.sub("", stem))
    s = _PAREN_TAG.sub(" ", s)       # 先剥纯混音/包装括号标记（ルームver / おまけ）
    s = _VARIANT_TOKEN.sub(" ", s)   # 再剥 SE/格式/版本
    s = _SEQ_BR.sub(" ", s)          # 再剥括号序号
    s = _SEQ_LEAD.sub(" ", s)        # 再剥开头序号
    return _NOISE.sub("", s).lower()


def _duration_tol(d: float) -> float:
    return max(_DUR_ABS_MIN, min(_DUR_ABS_MAX, d * _DUR_REL))


def _duration_of(root: str, rel: str, durations: dict | None,
                 probe: bool) -> float:
    if durations:
        v = durations.get(rel)
        if v and v > 0:
            return float(v)
    path = os.path.join(root, rel) if root else rel
    try:
        st = os.stat(path)
    except OSError:
        return 0.0
    with _DUR_LOCK:
        hit = _DUR_CACHE.get(path)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            return hit[2]
    if not probe:
        return 0.0
    try:
        from .fingerprint import probe_duration
    except ImportError:            # 以脚本方式直接 import（非包上下文）
        from fingerprint import probe_duration
    dur = probe_duration(path)
    with _DUR_LOCK:
        _DUR_CACHE[path] = (st.st_mtime, st.st_size, dur)
    return dur


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein 距离（插入/删除/替换均计 1）。"""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1,          # 删除
                           cur[j - 1] + 1,        # 插入
                           prev[j - 1] + (ca != cb)))  # 替换
        prev = cur
    return prev[-1]


def _char_diff_lb(a: str, b: str) -> int:
    """编辑距离的 O(n) 下界：两标题字符多重集的差集大小。

    完全相同字符构成的对下界为 0，进入 DP；差异明显的一步排除，
    避免全库轮询时对数 × O(n²) 的 Levenshtein 把 CPU 吃满。
    """
    ca = Counter(a)
    cb = Counter(b)
    return sum((ca - cb).values()) + sum((cb - ca).values())


def _same_content(ka: str, kb: str, da: float, db: float) -> bool:
    if not ka or not kb:
        return False
    # 长度差已 ≥ 阈值时编辑距离必然超限，直接剪枝
    if abs(len(ka) - len(kb)) >= _MAX_DIFF_CHARS:
        return False
    if _char_diff_lb(ka, kb) >= _MAX_DIFF_CHARS:
        return False
    if _edit_distance(ka, kb) >= _MAX_DIFF_CHARS:
        return False
    if da > 0 and db > 0:
        return abs(da - db) <= _duration_tol(max(da, db))
    # 时长缺失：只接受标题完全一致，避免误并
    return ka == kb


def _priority(rel: str, index: int) -> tuple:
    ext = os.path.splitext(rel)[1].lower()
    if _SELESS_RE.search(rel):
        se_rank = 0
    elif _SE_PRESENT_RE.search(rel):
        se_rank = 2
    else:
        se_rank = 1
    return (
        1 if _REVERSED_RE.search(rel) else 0,   # 反转版永不做主音源
        se_rank,                                 # 无 SE > 未知 > SE あり
        1 if _DERIVED_RE.search(rel) else 0,     # 派生片段靠后
        0 if ext in _LOSSLESS else 1,            # 无损优先（ASR 更准）
        index,
    )


def group_audio_tracks(rel_paths: Iterable[str], durations: dict | None = None,
                       probe: bool = False, root: str = "") -> list[list[str]]:
    """把 rel_paths 聚类成分组，组内第一项为推荐做 ASR 的主音源。

    durations: {rel: 秒}，通常来自 audio_index；缺失项在 probe=True 时用 ffprobe
    补测并缓存。组顺序 = 组内首个音轨在输入顺序中的位置。
    """
    paths = list(rel_paths)
    n = len(paths)
    keys = [_sim_key(p) for p in paths]
    durs = [_duration_of(root, p, durations, probe) for p in paths]

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        if not keys[i]:
            continue
        for j in range(i + 1, n):
            if not keys[j]:
                continue
            if not _same_content(keys[i], keys[j], durs[i], durs[j]):
                continue
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[rj] = ri

    buckets: dict[int, list[int]] = {}
    for i in range(n):
        buckets.setdefault(find(i), []).append(i)

    out: list[list[str]] = []
    for idxs in buckets.values():
        ordered = sorted(idxs, key=lambda k: _priority(paths[k], k))
        out.append([paths[k] for k in ordered])
    # 组顺序：按组内最小输入下标，保持作品原本的音轨顺序
    first_seen = {p: i for i, p in enumerate(paths)}
    out.sort(key=lambda g: min(first_seen[x] for x in g))
    return out


def pick_asr_source(group: list[str]) -> str:
    """组内第一项即主音源（无 SE / 非反转 / 无损优先）。"""
    if not group:
        raise ValueError("empty group")
    return group[0]


def collect_work_tracks(work_root: str, audio_exts: tuple[str, ...],
                        durations: dict | None = None,
                        probe: bool = False) -> list[list[str]]:
    """扫描作品根目录，返回去重后的音频分组。"""
    root = os.path.realpath(work_root)
    tracks: list[str] = []
    for dp, _dirs, names in os.walk(root):
        for name in names:
            if os.path.splitext(name)[1].lower() in audio_exts:
                full = os.path.join(dp, name)
                tracks.append(os.path.relpath(full, root).replace(os.sep, "/"))
    tracks.sort()
    return group_audio_tracks(tracks, durations=durations, probe=probe, root=root)
