"""配置加载 — 支持 JSON 文件 + 环境变量覆盖"""
from __future__ import annotations

import json
import os
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)

DEFAULT_CONFIG: dict[str, Any] = {
    # WebUI
    "web_host": "::",           # IPv6 socket 监听，AF_INET6，不被 strict IPv6 拦截
    "web_port": 8787,
    "web_host_v4_fallback": "0.0.0.0",  # 如果 :: 不行的备选

    # asmr.one API
    "api_base": "https://api.asmr.one",
    "account": "guest",
    "password": "guest",
    "sni_policy": "auto",

    # 下载
    "download_dir": "",          # 留空则自动选 storage_roots
    "storage_roots": [           # 多存储根，按剩余空间自动选
        {"path": "/vol1/asmr-downloads", "min_free_gb": 50},
    ],
    "workers": 4,
    "qps": 1.0,
    "retries": 3,
    "timeout": 30.0,
    "prefer_media": "all",
    "include_ext": "",
    "exclude_ext": "",  # 默认全量下载（含 mp4/png 等），需要时再按扩展名排除
    "include_keyword": "",
    "exclude_keyword": "",
    "force": False,
    "proxy": "",
    "bind_ipv6": "",
    "pins": {
        # DNS 劫持防护 — 固定 Cloudflare IPv6
        "api.asmr.one": "2606:4700:3034::6815:2686",
        "asmr.one": "2606:4700:3034::6815:2686",
        "raw.kiko-play-niptan.one": "2606:4700:20::681a:48c",
    },
    "verify_hostname": True,

    # 数据库
    "db_path": os.path.join(PROJECT_ROOT, "data", "library.db"),

    # 字幕生成（Silero VAD + Haas-ja 识别 + 翻译 API）
    "subtitle": {
        "enabled": True,
        "engine": "asmr_translator",   # asmr_translator（兼容模式，CPU 友好）
        "enable_scripts": False,       # 旧台本优先流程：默认关闭，统一走 ASR + 大模型翻译
        "parse_pdf": True,
        "parse_docx": True,
        "script_align_mode": "asr",    # asr | estimate
        "script_min_ratio": 0.45,      # 台本与 ASR 的最低匹配率

        # asmr-translator 兼容模式（ASMR VAD + Haas-ja + 本地翻译，纯 onnxruntime）
        "runtime_dir": "",             # 临时目录，留空用系统 temp
        "haas_model_dir": "",          # Haas-ja-v1.0 ONNX 目录，留空用 data/models/Haas-ja-v1.0
        "vad_model_path": "",          # ASMR VAD 模型，留空用 data/models/vad_asmr_encdec.onnx
        "vad_threshold": 0.4,          # ASMR VAD 判决阈值（双阈值滞回，负阈值 = 该值 -0.15）
        "vad_min_speech": 0.25,        # 最短语音段（秒）
        "vad_min_silence": 0.2,        # 段间最短静音（秒）
        "asr_max_segment": 25.0,       # 单段最长秒数，超出拆分子段
        "asr_min_conf": 0.12,          # 识别平均置信度低于此值丢弃（滤幻觉）

        # 线程
        "asr_threads": 4,

        # 翻译环节（日文 ASR 结果 → 大模型翻译 → 双语字幕）
        "api_base": "",
        "api_key": "",
        "api_model": "",
        "api_timeout": 90,
        "api_thinking": "off",         # 思考强度：off | low | medium | high
        "api_history_chars": 12000,    # 同一作品翻译会话保留的历史上下文字数上限

        # 旧 workfow 遗留字段（暂保留兼容）
        "script_model": "base",
        "asr_model": "small",
        "asr_model_dir": "",
        "asr_beam": 1,

        # 其它
        "use_api_sources": False,      # 默认不复用官方字幕，统一走 ASR + 大模型翻译
        "idle_only": True,
        "min_free_mem_mb": 900,
        "min_free_disk_gb": 2,
        "batch_max_minutes": 60,
        "drop_generated_chinese": "ask",  # ask | keep | auto_drop — 出现官方中文时如何处理自生成字幕

        # API 分时禁用：命中时间窗时只做 ASR，翻译留到时段结束后自动补
        "api_offpeak": {
            "enabled": False,
            "windows": [
                {"days": [1, 2, 3, 4, 5], "start": "09:00", "end": "12:00"},
                {"days": [1, 2, 3, 4, 5], "start": "14:00", "end": "18:00"},
            ],
        },
    },
}


def load_config(path: str | None = None) -> dict[str, Any]:
    """加载配置，JSON 文件覆盖默认值，环境变量覆盖 JSON。"""
    cfg = dict(DEFAULT_CONFIG)

    # 1. JSON 文件
    config_files = []
    if path:
        config_files.append(path)
    else:
        config_files.append(os.path.join(PROJECT_ROOT, "config.json"))
        config_files.append(os.path.join(HERE, "..", "config.json"))

    for fp in config_files:
        fp = os.path.abspath(fp)
        if os.path.exists(fp):
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    user_cfg = json.load(f)
                if isinstance(user_cfg.get("subtitle"), dict):
                    merged = dict(DEFAULT_CONFIG.get("subtitle") or {})
                    merged.update(user_cfg["subtitle"])
                    user_cfg["subtitle"] = merged
                cfg.update(user_cfg)
                cfg["_config_file_used"] = fp
                break
            except Exception as e:
                print(f"[config] failed to load {fp}: {e}")

    # 2. 环境变量覆盖（WEBUI_ 前缀）
    for k, v in os.environ.items():
        if k.startswith("WEBUI_"):
            key = k[6:].lower()
            try:
                cfg[key] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                cfg[key] = v

    # 3. 确保路径存在
    if cfg.get("db_path"):
        os.makedirs(os.path.dirname(cfg["db_path"]), exist_ok=True)

    # 4. 确保 storage_roots 有效
    roots = cfg.get("storage_roots") or []
    valid_roots = []
    for r in roots:
        p = r.get("path", "")
        if p:
            os.makedirs(p, exist_ok=True)
            valid_roots.append(r)
    cfg["storage_roots"] = valid_roots

    return cfg


def config_file_path(cfg: dict[str, Any]) -> str:
    """当前实际使用的配置文件路径（没有则用项目根 config.json）。"""
    fp = cfg.get("_config_file_used") or os.path.join(PROJECT_ROOT, "config.json")
    return os.path.abspath(fp)


def save_config(cfg: dict[str, Any]) -> str:
    """把当前内存配置写回配置文件（保留文件里已有的其他键）。"""
    fp = config_file_path(cfg)
    data: dict[str, Any] = {}
    if os.path.exists(fp):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    for k, v in cfg.items():
        if not k.startswith("_"):
            data[k] = v
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, fp)
    return fp


def storage_entries(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """按优先级返回存储空间列表（含旧版 download_dir 兼容）。"""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    dd = (cfg.get("download_dir") or "").strip()
    if dd:
        out.append({"path": dd, "min_free_gb": 0})
        seen.add(dd)

    for r in cfg.get("storage_roots") or []:
        p = (r.get("path") or "").strip() if isinstance(r, dict) else str(r).strip()
        if not p or p in seen:
            continue
        item = dict(r) if isinstance(r, dict) else {"path": p}
        item["path"] = p
        item.setdefault("min_free_gb", 0)
        out.append(item)
        seen.add(p)
    return out


def pick_storage_root(cfg: dict[str, Any], estimated_size: int = 0) -> str | None:
    """按优先级选第一个放得下的存储空间；全部不足返回 None。

    estimated_size 为待下载预估大小（字节），min_free_gb 为该存储的保留空间。
    """
    entries = storage_entries(cfg)
    if not entries:
        fallback = os.path.dirname(cfg.get("db_path", "./library.db"))
        try:
            os.makedirs(fallback, exist_ok=True)
        except OSError:
            return None
        return fallback

    need_size = max(0, int(estimated_size or 0))
    for r in entries:
        p = r["path"]
        reserve = int(r.get("min_free_gb", 0) or 0) * 1024**3
        try:
            stat = os.statvfs(p)
        except OSError:
            continue
        free = stat.f_frsize * stat.f_bavail
        if free >= need_size + reserve:
            return p
    return None
