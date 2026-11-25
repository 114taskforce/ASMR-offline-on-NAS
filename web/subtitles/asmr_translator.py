"""asmr-translator v1.30 兼容模式流水线（纯 onnxruntime，零 torch / 零 sherpa）。

与 asmr-translator 项目对齐的三段式：
  1. ASMR 专用 VAD（model/vad_asmr_encdec.onnx，119MB，日文 ASMR 500h 训练）
  2. Haas-ja-v1.0 日语识别（NeMo TDT transducer，3 个 ONNX + tokens.txt）
  3. 翻译由用户配置的大模型 API 完成（日文 SRT → 中文字幕）

不跑它那套 Windows 脚本（子进程 + DLL 目录 + llama-server），
只复用它的模型与算法参数，全部在 Flask 进程内用 onnxruntime CPU 跑。

要点（子代理实测结论，改代码前请先读）：
  · VAD ONNX 输出是 **logits**，必须 sigmoid；且对绝对响度敏感，
    送进去前必须做响度归一化（100ms RMS 中位数对齐 -20dBFS）。
  · Haas joiner 输出维度 = vocab(含 <blk>) + 4~5 个 duration bin → TDT 解码。
  · Haas encoder 要 NeMo 风格 128 维 log-mel（n_fft=512 / win=400 / hop=160 /
    preemph=0.97 / log(x+2^-24) / per_feature 归一化）。
  · blank = tokens.txt 里 <blk> 的行号（3072），有效 token 为 0..3071。
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable

ProgressCb = Callable[[str, float, str], None]  # stage, progress 0~1, detail


class AsrInterrupted(Exception):
    """识别中途收到暂停请求，立刻退出当前音轨。"""

APP_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_MODEL_DIR = os.path.join(APP_ROOT, "data", "models")
DEFAULT_HAAS_DIR = os.path.join(DEFAULT_MODEL_DIR, "Haas-ja-v1.0")
DEFAULT_VAD_PATH = os.path.join(DEFAULT_MODEL_DIR, "vad_asmr_encdec.onnx")

SR = 16000

# 每个并行识别 worker 预留的内存（模型常驻 + 单段音频 + ORT 临时缓冲）
_WORKER_MEM_MB = 3000


def worker_limit() -> int:
    """并行识别上限：物理核心数与总内存共同决定。"""
    cores = os.cpu_count() or 1
    total_mb = 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total_mb = int(line.split()[1]) // 1024
                    break
    except OSError:
        total_mb = 0
    if total_mb:
        cores = min(cores, max(1, total_mb // _WORKER_MEM_MB))
    return max(1, cores)


def asr_workers(cfg: dict) -> int:
    """配置里的并行线程数，未设置时按核心数取 2，并夹到硬上限内。"""
    sub = cfg.get("subtitle") or {}
    try:
        n = int(sub.get("asr_workers") or 0)
    except (TypeError, ValueError):
        n = 0
    limit = worker_limit()
    if n <= 0:
        n = min(2, limit)
    return max(1, min(n, limit))



@dataclass
class AsrSegment:
    start: float
    end: float
    text_ja: str
    conf: float


# ══════════════════════════════════════════════════════════════════════
# 1. NeMo 风格 log-mel（Haas encoder 用，128 维）
# ══════════════════════════════════════════════════════════════════════

def _hz_to_mel(f):
    import numpy as np
    f = np.atleast_1d(np.asarray(f, dtype=np.float64))
    mels = f / (200.0 / 3)
    logstep = np.log(6.4) / 27.0
    hi = f >= 1000.0
    return np.where(hi, 15.0 + np.log(np.maximum(f, 1e-10) / 1000.0) / logstep, mels)


def _mel_to_hz(m):
    import numpy as np
    m = np.atleast_1d(np.asarray(m, dtype=np.float64))
    freqs = 200.0 / 3 * m
    logstep = np.log(6.4) / 27.0
    hi = m >= 15.0
    return np.where(hi, 1000.0 * np.exp(logstep * (m - 15.0)), freqs)


def _mel_filterbank(n_mels: int, n_fft: int, sr: int = SR,
                    fmin: float = 0.0, fmax: float | None = None):
    import numpy as np
    fmax = sr / 2.0 if fmax is None else fmax
    freqs = np.linspace(0, sr / 2.0, 1 + n_fft // 2)
    m0 = float(_hz_to_mel(fmin)[0])
    m1 = float(_hz_to_mel(fmax)[0])
    hz = _mel_to_hz(np.linspace(m0, m1, n_mels + 2))
    fdiff = np.diff(hz)
    ramps = np.subtract.outer(hz, freqs)
    w = np.zeros((n_mels, 1 + n_fft // 2), dtype=np.float32)
    for i in range(n_mels):
        w[i] = np.maximum(0.0, np.minimum(-ramps[i] / fdiff[i],
                                          ramps[i + 2] / fdiff[i + 1]))
    w *= (2.0 / (hz[2:n_mels + 2] - hz[:n_mels]))[:, None]
    return w


_NEMO_MEL = None
_VAD_MEL = None


def _nemo_mel_fb():
    global _NEMO_MEL
    if _NEMO_MEL is None:
        _NEMO_MEL = _mel_filterbank(128, 512)
    return _NEMO_MEL


def _vad_mel_fb():
    global _VAD_MEL
    if _VAD_MEL is None:
        _VAD_MEL = _mel_filterbank(80, 400)
    return _VAD_MEL


def nemo_log_mel(wave, n_fft: int = 512, win: int = 400, hop: int = 160,
                 preemph: float = 0.97):
    """→ [128, T] float32（与 NeMo AudioToMelSpectrogramPreprocessor 对齐）。"""
    import numpy as np
    x = np.asarray(wave, dtype=np.float32)
    if preemph > 0:
        x = np.concatenate([x[:1], x[1:] - preemph * x[:-1]])
    x = np.pad(x, (n_fft // 2, n_fft // 2), mode="constant")
    w = np.hanning(win + 1)[:-1].astype(np.float32)
    n = 1 + (len(x) - win) // hop
    fr = np.lib.stride_tricks.as_strided(
        x, shape=(n, win), strides=(x.strides[0] * hop, x.strides[0]))
    sp = np.fft.rfft(fr * w, n=n_fft)
    p = (sp.real ** 2 + sp.imag ** 2).astype(np.float32)
    mel = np.log(p @ _nemo_mel_fb().T + 2.0 ** -24)
    mel = (mel - mel.mean(0, keepdims=True)) / (mel.std(0, keepdims=True) + 1e-5)
    return mel.T.astype(np.float32)


# ══════════════════════════════════════════════════════════════════════
# 2. ASMR 专用 VAD（whisper 风格 80 维 log-mel + 119MB ONNX）
# ══════════════════════════════════════════════════════════════════════

_VAD_CHUNK = 30 * SR
_VAD_FRAMES = 3000


def _vad_log_mel(a):
    """whisper 风格 log-mel → [1, 80, T]。"""
    import numpy as np
    if len(a) < 400:
        a = np.pad(a, (0, 400 - len(a)))
    n = 1 + (len(a) - 400) // 160
    idx = np.arange(400)[None, :] + 160 * np.arange(n)[:, None]
    fr = a[idx] * np.hanning(400)[None, :]
    spec = np.abs(np.fft.rfft(fr, n=400)) ** 2
    ls = np.log10(np.maximum(spec @ _vad_mel_fb().T, 1e-10))
    ls = np.maximum(ls, ls.max() - 8.0)
    ls = (ls + 4.0) / 4.0
    return ls.T[None, :, :].astype(np.float32)


def _normalize_loudness(a, target_dbfs: float = -20.0, peak_limit: float = 0.99):
    """100ms RMS 中位数对齐到 target；VAD 对绝对电平敏感，必须做。"""
    import numpy as np
    if a.size == 0:
        return a
    win = int(0.1 * SR)
    n = len(a) // win
    if n >= 1:
        rms = np.sqrt((a[: n * win].reshape(n, win) ** 2).mean(1) + 1e-12)
        med = float(np.median(rms))
    else:
        med = float(np.sqrt((a ** 2).mean() + 1e-12))
    if med <= 1e-8:
        return a
    out = a * (10 ** (target_dbfs / 20.0) / med)
    pk = float(np.abs(out).max())
    if pk > peak_limit:
        out = out * (peak_limit / pk)
    return out.astype(np.float32)


class AsmrVad:
    def __init__(self, model_path: str, threads: int = 4):
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, int(threads))
        so.inter_op_num_threads = 1
        so.log_severity_level = 3
        self.device = "CPU"
        self.sess = ort.InferenceSession(model_path, sess_options=so,
                                         providers=["CPUExecutionProvider"])
        self.iname = self.sess.get_inputs()[0].name

    def frame_probs(self, audio, workers: int = 1):
        import numpy as np
        a = _normalize_loudness(np.asarray(audio, dtype=np.float32).ravel())

        # 预处理各块：记录真实长度，末尾 pad 段只取对应的输出帧
        chunks: list[tuple] = []
        for st in range(0, len(a), _VAD_CHUNK):
            ch = a[st: st + _VAD_CHUNK]
            raw_len = len(ch)
            if raw_len < _VAD_CHUNK:
                ch = np.pad(ch, (0, _VAD_CHUNK - raw_len))
            m = _vad_log_mel(ch)
            if m.shape[2] < _VAD_FRAMES:
                m = np.pad(m, ((0, 0), (0, 0), (0, _VAD_FRAMES - m.shape[2])))
            else:
                m = m[:, :, :_VAD_FRAMES]
            chunks.append((m, raw_len))

        def _run(item):
            m, raw_len = item
            lg = np.asarray(self.sess.run(None, {self.iname: m})[0]).reshape(-1)
            prob = 1.0 / (1.0 + np.exp(-lg))  # ★ 必须 sigmoid
            if raw_len < _VAD_CHUNK and prob.size:
                # 模型输出帧率低于输入，按块内真实时长占比截断
                keep = max(1, int(round(prob.size * raw_len / _VAD_CHUNK)))
                prob = prob[:keep]
            return prob

        if workers > 1 and len(chunks) > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=workers) as pool:
                parts = list(pool.map(_run, chunks))
        else:
            parts = [_run(c) for c in chunks]
        return (np.concatenate(parts) if parts else np.zeros(0, np.float32)), len(a) / SR

    def segments(self, audio, threshold: float = 0.5, min_speech: float = 0.25,
                 min_silence: float = 0.2, workers: int = 1):
        """双阈值滞回 + 最短段过滤 → [(start, end)]。"""
        p, dur = self.frame_probs(audio, workers=workers)
        if p.size == 0:
            return []
        neg = threshold - 0.15
        dt = dur / p.size
        segs, ins, st = [], False, 0.0
        for i, v in enumerate(p):
            t = i * dt
            if not ins and v >= threshold:
                ins, st = True, t
            elif ins and v < neg:
                ins = False
                segs.append([st, t])
        if ins:
            segs.append([st, dur])
        mg: list[list[float]] = []
        for x, y in segs:
            if y - x < min_speech:
                continue
            if mg and x - mg[-1][1] < min_silence:
                mg[-1][1] = y
            else:
                mg.append([x, y])
        return [(x, y) for x, y in mg]


_vad_cache: dict = {}


def load_asmr_vad(path: str | None = None, threads: int = 4):
    path = path or DEFAULT_VAD_PATH
    if not os.path.isfile(path):
        raise FileNotFoundError(f"ASMR VAD 模型缺失：{path}")
    key = (path, int(threads))
    if key not in _vad_cache:
        _vad_cache.clear()
        _vad_cache[key] = AsmrVad(path, threads)
    return _vad_cache[key]


# ══════════════════════════════════════════════════════════════════════
# 3. Haas-ja-v1.0 TDT 识别（纯 onnxruntime 贪心解码）
# ══════════════════════════════════════════════════════════════════════

@dataclass
class AsrResult:
    text: str
    tokens: list[str]
    times: list[float]   # 每 token 起始秒（相对段首）
    conf: float


class HaasTransducer:
    def __init__(self, model_dir: str, threads: int = 4):
        import onnxruntime as ort
        files = ("encoder.quantized.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt")
        missing = [f for f in files if not os.path.isfile(os.path.join(model_dir, f))]
        if missing:
            raise FileNotFoundError(f"Haas-ja 模型缺失 {missing}（目录 {model_dir}）")
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, int(threads))
        so.inter_op_num_threads = 1
        so.log_severity_level = 3

        def _s(name):
            return ort.InferenceSession(os.path.join(model_dir, name),
                                        sess_options=so,
                                        providers=["CPUExecutionProvider"])

        # 纯 CPU 推理（int8 量化 encoder 在核显上输出 NaN，暂不用 GPU）
        self.enc, self.dec, self.jn = (_s("encoder.quantized.onnx"),
                                       _s("decoder.onnx"),
                                       _s("joiner.onnx"))
        self.tokens: list[str] = []
        with open(os.path.join(model_dir, "tokens.txt"), encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if line:
                    self.tokens.append(line.rsplit(" ", 1)[0])
        self.blank = len(self.tokens) - 1
        self.vocab = self.blank
        n_out = self.jn.get_outputs()[0].shape[-1]
        self.n_dur = max(1, int(n_out) - (self.blank + 1))
        self.durs = [0, 1, 2, 3, 4][: self.n_dur] or [0]
        st = self.dec.get_inputs()
        self._state_shape = tuple(st[2].shape) if len(st) > 2 else (2, 1, 20480)

    def _dstep(self, tok, s1, s2):
        import numpy as np
        o = self.dec.run(None, {
            "targets": np.array([[tok]], dtype=np.int32),
            "target_length": np.array([1], dtype=np.int32),
            "states.1": s1, "states.2": s2,
        })
        return o[0], o[2], o[3]

    def transcribe(self, wave) -> AsrResult:
        import numpy as np
        if len(wave) < SR // 10:
            return AsrResult("", [], [], 0.0)
        f = nemo_log_mel(wave)
        eo = self.enc.run(None, {
            "audio_signal": f[None],
            "length": np.array([f.shape[1]], dtype=np.int64),
        })
        e, ln = eo[0], int(eo[1][0])
        s1 = np.zeros(self._state_shape, np.float32)
        s2 = np.zeros(self._state_shape, np.float32)
        prev, s1, s2 = self._dstep(self.blank, s1, s2)

        ids, times, lps = [], [], []
        t = 0
        while t < ln:
            skip, u = 1, 0
            while u < 10:
                lg = self.jn.run(None, {
                    "encoder_outputs": e[:, :, t:t + 1],
                    "decoder_outputs": prev,
                })[0].reshape(-1)
                tok_lg = lg[: self.blank + 1]
                k = int(np.argmax(tok_lg))
                skip = self.durs[int(np.argmax(lg[self.blank + 1:]))] if self.n_dur else 1
                if k == self.blank or k >= self.vocab:
                    break
                m = float(tok_lg.max())
                lse = m + float(np.log(np.exp(tok_lg - m).sum()))
                lps.append(m - lse)
                ids.append(k)
                times.append(t * 0.08)
                prev, s1, s2 = self._dstep(k, s1, s2)
                u += 1
            t += max(1, skip)

        toks = [self.tokens[i] for i in ids]
        text = "".join(toks).replace("▁", " ").strip()
        conf = float(np.exp(np.mean(lps))) if lps else 0.0
        return AsrResult(text, toks, times, conf)


_haas_cache: dict = {}


def load_haas_engine(num_threads: int = 4, model_dir: str | None = None):
    base = model_dir or DEFAULT_HAAS_DIR
    key = (base, int(num_threads))
    if key not in _haas_cache:
        _haas_cache.clear()
        _haas_cache[key] = HaasTransducer(base, num_threads)
    return _haas_cache[key]


def unload_asmr_vad() -> None:
    _vad_cache.clear()


def unload_haas_engine() -> None:
    _haas_cache.clear()


def unload_models() -> None:
    """空闲时释放 VAD 与 ASR 模型，占用的内存立即归还；下次使用时重新加载。"""
    _vad_cache.clear()
    _haas_cache.clear()
    import gc
    gc.collect()


# ══════════════════════════════════════════════════════════════════════
# 4. 工具
# ══════════════════════════════════════════════════════════════════════

def run_ffmpeg_convert(input_path: str, out_wav: str, sr: int = SR) -> None:
    p = subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path, "-vn", "-ac", "1", "-ar", str(sr),
        "-sample_fmt", "s16", out_wav,
    ], capture_output=True, text=True, timeout=7200)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败: {(p.stderr or '').strip()[:300]}")


def probe_duration(path: str) -> float:
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=30)
        return float((p.stdout or "").strip() or 0)
    except Exception:
        return 0.0


def _merge_short(cues: list[dict], max_chars: int = 6,
                 max_join_gap: float = 1.6) -> list[dict]:
    """把过短（≤max_chars）且紧邻的碎句并到下一句，避免「作品」「は」这种单字字幕。"""
    out: list[dict] = []
    for c in cues:
        if out and len(out[-1]["text"]) <= max_chars \
                and (c["start"] - out[-1]["end"]) <= max_join_gap:
            out[-1]["text"] = out[-1]["text"] + c["text"]
            out[-1]["end"] = c["end"]
        else:
            out.append(dict(c))
    return out


def _tokens_to_cues(tokens: list[str], times: list[float], offset: float,
                    seg_end: float, max_gap: float = 1.0,
                    max_len: int = 28) -> list[dict]:
    """把一段的 token 序列按时间间隙 / 长度切成分句 cue。"""
    cues: list[dict] = []
    buf_txt, buf_start, last_t = [], None, None
    for tok, tm in zip(tokens, times):
        txt = tok.replace("▁", " ")
        if buf_start is None:
            buf_start = tm
        elif last_t is not None and (tm - last_t) > max_gap:
            cues.append(_mk(buf_txt, offset + buf_start, offset + last_t + 0.35))
            buf_txt, buf_start = [], tm
        buf_txt.append(txt)
        last_t = tm
        joined = "".join(buf_txt).strip()
        if len(joined) >= max_len and joined and joined[-1] in "。、！？…,.!?":
            cues.append(_mk(buf_txt, offset + buf_start, offset + tm + 0.3))
            buf_txt, buf_start = [], None
    if buf_txt:
        end = min(offset + (last_t or seg_end) + 0.45, seg_end)
        cues.append(_mk(buf_txt, offset + (buf_start or 0.0), end))
    return _merge_short([c for c in cues if c["text"] and _meaningful(c["text"])])


def _mk(tokens: list[str], start: float, end: float) -> dict:
    text = "".join(tokens).replace("▁", " ").strip()
    text = re.sub(r"^[\s,、，.。…·]+", "", text)
    return {"start": round(float(start), 3),
            "end": round(max(float(end), float(start) + 0.3), 3),
            "text": text}


_MEANINGFUL = re.compile(r"[0-9A-Za-z\u3040-\u30ff\u3400-\u9fff\uff66-\uff9f]")


def _meaningful(text: str) -> bool:
    """至少含一个假名 / 汉字 / 字母 / 数字，用来丢弃纯标点的 ASR 噪声。"""
    return bool(_MEANINGFUL.search(text or ""))


# ══════════════════════════════════════════════════════════════════════
# 4.5 大模型翻译（OpenAI 兼容 /chat/completions）
# ══════════════════════════════════════════════════════════════════════

PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "llm_prompt_ja_zh.md")
_BATCH_CHARS = 900
_BATCH_CUES = 30
_HISTORY_CHARS = 12000


def load_prompt_template() -> str:
    """读取日译中提示词模板（{title}/{subtitle}/{tags}）。"""
    with open(PROMPT_PATH, encoding="utf-8") as f:
        return f.read()


def api_ready(cfg: dict) -> bool:
    sub = cfg.get("subtitle") or {}
    return bool((sub.get("api_base") or "").strip()
                and (sub.get("api_model") or "").strip()
                and (sub.get("api_key") or "").strip())


class TranslationSession:
    """同一作品的翻译会话：system 固定，历史按字数上限滚动保留。

    多个音轨/多集共用同一个实例，模型能看到前文译文，保证称谓与剧情连贯；
    超过 max_chars 时丢弃最早的一问一答，避免上下文无限膨胀。
    """

    def __init__(self, max_chars: int = _HISTORY_CHARS):
        self.max_chars = max(1000, int(max_chars or _HISTORY_CHARS))
        self.system = ""
        self.messages: list[dict] = []

    def start(self, system_prompt: str) -> None:
        if not self.system:
            self.system = (system_prompt or "").strip()

    def add(self, user: str, assistant: str) -> None:
        self.messages.append({"role": "user", "content": user})
        self.messages.append({"role": "assistant", "content": assistant})
        self._trim()

    def _trim(self) -> None:
        if not self.messages:
            return
        total = sum(len(m["content"]) for m in self.messages)
        if total <= self.max_chars:
            return
        # 一步降到低水位（60%）而不是每次只丢一对：每次裁剪都会让服务商的
        # 前缀缓存整段失效，频繁小幅裁剪会显著拉低缓存命中率
        low = max(1000, int(self.max_chars * 0.6))
        while total > low and len(self.messages) >= 2:
            total -= len(self.messages[0]["content"]) + len(self.messages[1]["content"])
            del self.messages[:2]

    def payload(self, user: str) -> list[dict]:
        head = [{"role": "system", "content": self.system}] if self.system else []
        return head + list(self.messages) + [{"role": "user", "content": user}]


_NO_THINKING: dict[tuple[str, str], bool] = {}
_NO_REASONING: dict[tuple[str, str], bool] = {}

# 思考强度 → 推理预算 token
_THINK_BUDGET = {"low": 1024, "medium": 2048, "high": 4096}


def _thinking_level(sub: dict) -> str:
    """读取思考强度：off / low / medium / high（兼容旧的开关字段）。"""
    level = str(sub.get("api_thinking") or "").strip().lower()
    if level in _THINK_BUDGET or level == "off":
        return level
    return "off" if sub.get("api_disable_thinking", True) else "medium"


_RAW_NET: tuple | None = None


def _raw_net() -> tuple:
    """取未被 strict-IPv6 护栏替换的 getaddrinfo / socket。

    ipv6_net.enable_strict_ipv6() 会全局改写 socket.getaddrinfo，只保留 AAAA
    记录，使任何 IPv4-only 的服务解析失败（api.deepseek.com 只有 A 记录，
    于是翻译一直报 Name or service not known）。翻译 API 与 asmr.one 无关，
    这里缓存护栏安装前的原始实现；解析顺序仍是系统默认（一般 IPv6 优先），
    不影响下载侧的 IPv6-only 约束。
    """
    global _RAW_NET
    if _RAW_NET is None:
        import socket as _socket
        gi, sk = _socket.getaddrinfo, _socket.socket
        try:
            import ipv6_net  # 项目根目录，web/app.py 已加入 sys.path
            gi = getattr(ipv6_net, "_ORIG_GETADDRINFO", gi)
            sk = getattr(ipv6_net, "_ORIG_SOCKET", sk)
        except Exception:
            pass
        _RAW_NET = (gi, sk)
    return _RAW_NET


def _https_post(scheme: str, host: str, port: int | None, path: str,
                body: bytes, headers: dict, timeout: float) -> tuple:
    """最小 HTTP(S) 客户端：用原始 socket 建连，绕过进程级 IPv6 护栏。"""
    import http.client

    gi, sk = _raw_net()
    port = port or (443 if scheme == "https" else 80)
    if scheme == "http":
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    else:
        conn = http.client.HTTPSConnection(host, port, timeout=timeout)

    def _connect():
        last_error = None
        sock = None
        import socket as _socket
        for fam, typ, proto, _cn, addr in gi(host, port, 0, _socket.SOCK_STREAM):
            try:
                sock = sk(fam, typ, proto)
                sock.settimeout(timeout)
                sock.connect(addr)
                break
            except OSError as ex:
                last_error = ex
                if sock is not None:
                    sock.close()
                sock = None
        if sock is None:
            raise last_error or OSError(f"connect failed: {host}:{port}")
        if scheme == "http":
            conn.sock = sock
        else:
            conn.sock = conn._context.wrap_socket(sock, server_hostname=host)

    conn.connect = _connect
    try:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.read(), resp.status, resp.reason, resp.headers
    finally:
        conn.close()


def _llm_chat(cfg: dict, messages: list[dict], timeout: float = 120.0) -> str:
    """调用 OpenAI 兼容接口，返回助手消息文本。

    推理型模型（deepseek 等）默认会额外生成大量 reasoning_content，费用和耗时
    远超译文本身。思考强度为「关闭」时不发送思考参数（不支持的话自动去掉）；
    低/中/高时按预算开启思考并附带 reasoning_effort，接口拒绝该参数时依次降级
    并记住结果。
    """
    import io
    import json
    import urllib.error
    import urllib.parse

    sub = cfg.get("subtitle") or {}
    base = (sub.get("api_base") or "").strip().rstrip("/")
    key = (sub.get("api_key") or "").strip()
    model = (sub.get("api_model") or "").strip()
    if not base or not model:
        raise RuntimeError("翻译 API 未配置（api_base / api_model）")
    if not key:
        raise RuntimeError("翻译 API Key 未配置")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    parts = urllib.parse.urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    flag = (base, model)
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "stream": False,
    }
    level = _thinking_level(sub)
    if level == "off":
        if _NO_THINKING.get(flag, True):
            payload["thinking"] = {"type": "disabled"}
    else:
        payload["thinking"] = {"type": "enabled",
                               "budget_tokens": _THINK_BUDGET[level]}
        if not _NO_REASONING.get(flag, False):
            payload["reasoning_effort"] = level

    def _post(body: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        raw, status, reason, headers = _https_post(
            parts.scheme or "https", parts.hostname or "", parts.port, path, data,
            {"Content-Type": "application/json",
             "Authorization": f"Bearer {key}",
             "Content-Length": str(len(data))}, timeout)
        if status >= 400:
            raise urllib.error.HTTPError(url, status, reason, headers, io.BytesIO(raw))
        return json.loads(raw.decode("utf-8", "replace"))

    data = None
    for _ in range(3):
        try:
            data = _post(payload)
            break
        except urllib.error.HTTPError as ex:
            if ex.code != 400:
                raise
            # 依次去掉最可能不被支持的参数后重试
            if "reasoning_effort" in payload:
                _NO_REASONING[flag] = True
                payload.pop("reasoning_effort", None)
                continue
            if "thinking" in payload:
                _NO_THINKING[flag] = False
                payload.pop("thinking", None)
                continue
            raise
    if data is None:
        raise RuntimeError("翻译 API 拒绝请求参数")
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"翻译 API 返回异常：{str(data)[:200]}")
    msg = choices[0].get("message") or {}
    return (msg.get("content") or "").strip()


_NUM_LINE = re.compile(r"^\s*(\d{1,3})\s*[.．、):：]\s*(.*)$")
_NUM_PREFIX = re.compile(r"^\s*(?:第?\s*)?\d{1,3}\s*[.．、):：\-]\s*")


def _parse_lines(text: str, n: int) -> dict[int, str]:
    """解析逐行译文：优先按序号对齐，其次按非空行顺序对齐。"""
    body = re.sub(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$", "", text or "", flags=re.M)
    raw = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if not raw:
        return {}
    got: dict[int, str] = {}
    for line in raw:
        m = _NUM_LINE.match(line)
        if not m:
            continue
        idx = int(m.group(1)) - 1
        val = m.group(2).strip()
        if 0 <= idx < n and val:
            got[idx] = val
    if len(got) >= max(1, int(n * 0.6)):
        return got
    return {i: _NUM_PREFIX.sub("", ln).strip()
            for i, ln in enumerate(raw[:n])}


class ApiWindowClosed(RuntimeError):
    """进入 API 禁用时段，主动中止后续批次（已翻好的批次保留）。"""


def translate_cues_llm(cues: list[dict], cfg: dict,
                       work_info: dict | None = None,
                       progress: ProgressCb | None = None,
                       session: TranslationSession | None = None,
                       guard: Callable[[], bool] | None = None,
                       stop: Callable[[], bool] | None = None) -> list[dict]:
    """给每条 cue 追加中文译文（text = `日文\\n中文`），失败时原样返回日文。

    session 传入同一作品共享的 TranslationSession 时，各批次按顺序在同一会话内
    续接上下文，保证人物称谓与剧情连贯；为空则为本文件单独开一个会话。
    """
    sub = cfg.get("subtitle") or {}
    if not cues:
        return cues
    template = load_prompt_template()
    info = work_info or {}
    tags = info.get("tags") or []
    if isinstance(tags, str):
        tags = [t for t in re.split(r"[\s,，/]+", tags) if t]
    timeout = float(sub.get("api_timeout") or 120)

    # system 里不含任何作品信息，保证跨作品前缀一致、可被服务商缓存复用；
    # 作品信息（标题/标签）改为拼在会话的第一条 user 消息里
    _pre, _rest = template.split("# 本次作品信息", 1)
    _info_block, _post = _rest.split("# 翻译规则", 1)
    system = (_pre + "# 翻译规则" + _post.split("# 待翻译内容", 1)[0]).rstrip()
    info_head = ("# 本次作品信息" + _info_block).format(
        title=info.get("title") or "未知作品",
        subtitle=info.get("subtitle") or info.get("title") or "无",
        tags=" / ".join(tags) or "无",
    )
    if session is None:
        session = TranslationSession(int(sub.get("api_history_chars") or _HISTORY_CHARS))
    session.start(system)

    batches: list[tuple[int, int]] = []
    start, chars = 0, 0
    for i, c in enumerate(cues):
        ln = len(c["text"])
        if i > start and (chars + ln > _BATCH_CHARS
                          or i - start >= _BATCH_CUES):
            batches.append((start, i))
            start, chars = i, 0
        chars += ln
    if start < len(cues):
        batches.append((start, len(cues)))

    out = [dict(c) for c in cues]
    ok_batches = 0
    for bi, (a, b) in enumerate(batches):
        if stop is not None and stop():
            raise AsrInterrupted()
        if guard is not None and not guard():
            raise ApiWindowClosed("进入 API 禁用时段，暂停翻译")
        part = cues[a:b]
        numbered = "\n".join(f"{i + 1}. {c['text']}"
                             for i, c in enumerate(part))
        # 会话历史为空 = 该作品第一批请求，此时才携带作品信息；
        # 若首批失败未入历史，下一批会重新携带，保证模型始终能拿到作品背景
        head = "" if session.messages else (info_head + "\n\n")
        user = (head + f"# 本批次（第 {bi + 1}/{len(batches)} 批，按作品内顺序）\n"
                f"共 {len(part)} 行，请严格逐行输出 {len(part)} 行中文译文，"
                "不要编号、不要空行、不要解释。\n\n" + numbered)
        try:
            raw = _llm_chat(cfg, session.payload(user), timeout)
            zh = _parse_lines(raw, len(part))
            if zh:
                ok_batches += 1
                session.add(user, "\n".join(
                    (zh.get(i) or part[i]["text"]).replace("\n", " ")
                    for i in range(len(part))))
        except Exception as ex:
            print(f"[asmr_translator] 翻译批次 {bi + 1} 失败：{ex}", flush=True)
            zh = {}
        for i, c in enumerate(part):
            val = (zh.get(i) or "").strip()
            if not val or val == c["text"].strip():
                continue
            out[a + i]["text"] = f"{c['text']}\n{val}"
        if progress:
            try:
                progress("translate", 0.8 + 0.15 * (bi + 1) / len(batches),
                         f"翻译 {bi + 1}/{len(batches)} 批")
            except Exception:
                pass
    if not ok_batches:
        raise RuntimeError("大模型翻译全部失败，请检查 API 地址 / 模型 / Key")
    return out


# ══════════════════════════════════════════════════════════════════════
# 5. 主流水线
# ══════════════════════════════════════════════════════════════════════

def run_asr_cues(input_path: str, out_dir: str, cfg: dict,
                 progress: ProgressCb | None = None,
                 stop: Callable[[], bool] | None = None) -> list[dict]:
    """转码 → ASMR VAD 切段 → Haas-ja 识别 → 分句，只做识别、不翻译。"""
    import numpy as np
    import soundfile as sf

    os.makedirs(out_dir, exist_ok=True)
    sub = cfg.get("subtitle") or {}
    threads = int(sub.get("asr_threads") or cfg.get("asr_threads") or 4)
    vad_thr = float(sub.get("vad_threshold") or 0.5)
    vad_min_speech = float(sub.get("vad_min_speech") or 0.25)
    vad_min_silence = float(sub.get("vad_min_silence") or 0.2)
    max_seg = float(sub.get("asr_max_segment") or 25.0)
    min_conf = float(sub.get("asr_min_conf") or 0.12)

    def _p(stage: str, p: float, detail: str = ""):
        if progress:
            try:
                progress(stage, p, detail)
            except Exception:
                pass

    def _halt() -> bool:
        return bool(stop and stop())

    _p("convert", 0.02, "ffmpeg 转码 16k")
    if _halt():
        raise AsrInterrupted()
    wav = os.path.join(out_dir, "audio16k.wav")
    run_ffmpeg_convert(input_path, wav)
    if _halt():
        raise AsrInterrupted()
    if not os.path.isfile(wav) or os.path.getsize(wav) < 2048:
        raise RuntimeError("ffmpeg 输出为空（音频损坏或格式不支持）")
    samples, sr = sf.read(wav, dtype="float32", always_2d=False)
    if getattr(samples, "ndim", 1) > 1:
        samples = samples.mean(axis=1)
    duration = len(samples) / float(sr or SR)
    if duration <= 0.2:
        return []

    workers = asr_workers(cfg)
    _p("vad", 0.08, f"ASMR VAD 切段（{workers} 并行）")
    vad_threads = max(1, (os.cpu_count() or 1) // workers) if workers > 1 else threads
    vad = load_asmr_vad(sub.get("vad_model_path") or cfg.get("vad_model_path"),
                        vad_threads)
    segs = vad.segments(samples, threshold=vad_thr,
                        min_speech=vad_min_speech, min_silence=vad_min_silence,
                        workers=workers)
    if not segs:
        _p("done", 1.0, "VAD 未检出语音")
        return []

    # 多段并行时压缩单次推理的内部线程数，避免 workers × intra_op 超订
    eng_threads = max(1, (os.cpu_count() or 1) // workers) if workers > 1 else threads
    _p("asr_load", 0.15, f"加载 Haas-ja（{workers} 并行）")
    eng = load_haas_engine(eng_threads,
                           sub.get("haas_model_dir") or cfg.get("haas_model_dir"))

    # 展开：超长 VAD 段按 max_seg 切成子段，全部子段排队识别
    pieces_all: list[tuple[float, float]] = []
    for (a, b) in segs:
        cur = a
        while b - cur > max_seg:
            pieces_all.append((cur, cur + max_seg))
            cur += max_seg
        pieces_all.append((cur, b))
    pieces_all = [(pa, pb) for (pa, pb) in pieces_all
                  if min(int(pb * sr), len(samples)) - int(pa * sr) >= sr // 8]
    if not pieces_all:
        _p("done", 1.0, "没有可识别的语音段")
        return []

    total_pieces = len(pieces_all)
    _p("asr", 0.2, f"识别 {total_pieces} 段 · {workers} 并行")
    asr_segs: list[AsrSegment] = []
    all_cues: list[dict] = []

    def _run_piece(idx: int, pa: float, pb: float):
        s = int(pa * sr)
        e = min(int(pb * sr), len(samples))
        try:
            return idx, pa, pb, eng.transcribe(samples[s:e]), None
        except Exception as ex:  # 单段异常不影响整条音轨
            return idx, pa, pb, None, ex

    # 按批提交：批间响应暂停（最多等一批），批内多段并行吃满多核
    from concurrent.futures import ThreadPoolExecutor
    results: list = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for start in range(0, total_pieces, workers):
            if _halt():
                raise AsrInterrupted()
            batch = pieces_all[start:start + workers]
            futs = [pool.submit(_run_piece, start + k, pa, pb)
                    for k, (pa, pb) in enumerate(batch)]
            for fut in futs:
                results.append(fut.result())
            done_n = min(total_pieces, start + len(batch))
            _p("asr", 0.2 + 0.55 * done_n / total_pieces,
               f"{done_n}/{total_pieces} 段 · {len(all_cues)} 句")

    # 按子段顺序归并，保证字幕时间顺序与识别顺序一致
    results.sort(key=lambda x: x[0])
    for _idx, pa, pb, r, ex in results:
        if ex is not None:
            print(f"[asmr_translator] ASR 异常 {pa:.1f}-{pb:.1f}: {ex}", flush=True)
            continue
        if not r.text or r.conf < min_conf:
            continue
        asr_segs.append(AsrSegment(pa, pb, r.text, r.conf))
        all_cues.extend(_tokens_to_cues(r.tokens, r.times, pa, pb))

    try:
        os.remove(wav)
    except OSError:
        pass

    if not all_cues:
        _p("done", 1.0, "识别结果为空")
        return []

    _p("vtt", 0.78, f"整理 {len(all_cues)} 句日文字幕")
    cues: list[dict] = []
    for c in all_cues:
        t = (c["text"] or "").strip()
        if not _meaningful(t):
            continue
        cues.append({"start": c["start"], "end": c["end"], "text": t})

    _p("asr_done", 0.8, f"识别完成 {len(cues)} 句")
    return cues
