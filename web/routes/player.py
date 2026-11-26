"""播放页 API — 作品信息 / 字幕智能匹配 / 字幕内容服务 / Whisper ASR（可选）"""
from __future__ import annotations

import os
import re
import sys
import time

from flask import Blueprint, Response, current_app, jsonify, request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import ipv6_net as net
import asmr_client as api

player_bp = Blueprint("player", __name__)

# 远程字幕列表拉取失败后的熔断冷却（秒），期间弹窗不再等待 asmr.one
_API_SUB_COOLDOWN = 600.0
_api_sub_fail_at = 0.0


# 字幕扩展名
SUBTITLE_EXTS = {".srt", ".vtt", ".ass", ".ssa", ".sub", ".smi", ".lrc"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"}


def _make_client(cfg: dict):
    """创建 asmr.one 客户端。出站请求本身已强制 IPv6，无需全局 patch。"""
    return api.AsmrClient(
        net.NetConfig(
            pins=cfg.get("pins") or {},
            proxy=net.parse_proxy(cfg.get("proxy") or "") if cfg.get("proxy") else None,
            sni_policy=cfg.get("sni_policy", "auto"),
            verify_hostname=bool(cfg.get("verify_hostname", True)),
            timeout=float(cfg.get("timeout", 30)),
            retries=int(cfg.get("retries", 3)),
        ),
        account=cfg.get("account", "guest"),
        password=cfg.get("password", "guest"),
    )


# --------------------------------------------------------------------------
# 本地字幕扫描
# --------------------------------------------------------------------------

def _detect_language(text: str) -> str:
    """按内容粗略判断语言，用于字幕自动排序。"""
    if re.search(r"[\u3040-\u30ff]", text):
        return "ja"
    if re.search(r"[\u4e00-\u9fff]", text):
        return "zh"
    return "und"


def _read_text_any(path: str, limit: int | None = None) -> str:
    """按常见中日文编码依次尝试读取字幕文件。"""
    with open(path, "rb") as f:
        raw = f.read(limit) if limit else f.read()
    for enc in ("utf-8-sig", "utf-8", "cp932", "gbk", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _scan_subtitles(local_dir: str, work_id: str) -> list[dict]:
    """扫描本地目录找字幕文件。"""
    subs: list[dict] = []
    if not local_dir or not os.path.isdir(local_dir):
        return subs

    audio_names: set[str] = set()
    audio_stems: set[str] = set()
    for root, _dirs, files in os.walk(local_dir):
        for f in files:
            if os.path.splitext(f)[1].lower() in AUDIO_EXTS:
                audio_names.add(f.lower())
                audio_stems.add(os.path.splitext(f)[0].lower())

    for root, _dirs, files in os.walk(local_dir):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext not in SUBTITLE_EXTS:
                continue
            full = os.path.join(root, f)
            # 去掉字幕扩展名后的前缀，如 "音轨1.mp3"
            base = os.path.splitext(f)[0].lower()
            try:
                head = _read_text_any(full, 4000)
            except OSError:
                continue

            # 生成字幕：<类别>_<RJ号>_<展平音轨名>.vtt，前缀即语言
            category = ""
            m = re.match(r"^(日文|中文|双语)_", os.path.splitext(f)[0])
            if m:
                category = {"日文": "ja", "中文": "zh", "双语": "bi"}[m.group(1)]

            # 与音频同名（音轨1.mp3.vtt ← 音轨1.mp3）或同主名（音轨1.vtt ← 音轨1.mp3）
            if base in audio_names:
                confidence = 0.95
            elif os.path.splitext(base)[0] in audio_stems:
                confidence = 0.9
            elif category and any(
                    s and len(s) >= 3 and s in base for s in audio_stems):
                confidence = 0.92   # 生成字幕命中当前音轨（含音轨名）
            elif work_id.upper() in f.upper():
                confidence = 0.8
            else:
                confidence = 0.5

            subs.append({
                "filename": f,
                "rel_path": os.path.relpath(full, local_dir).replace("\\", "/"),
                "local_path": full,
                "ext": ext,
                "size": os.path.getsize(full),
                "language": category or _detect_language(head),
                "category": category,
                "source": "local",
                "confidence": confidence,
            })

    # 置信度相同时优先双语，其次中文，最后日文
    _rank = {"bi": 3, "zh": 2, "ja": 1, "": 0}
    subs.sort(key=lambda x: (-x["confidence"], -_rank.get(x.get("category") or "", 0)))
    return subs


# --------------------------------------------------------------------------
# 远程字幕（asmr.one tracks 里带的字幕文件）
# --------------------------------------------------------------------------

def _fetch_api_subtitles(cfg: dict, work_id: str) -> list[dict]:
    """列出 asmr.one 上该作品的字幕文件。

    该调用发生在弹窗请求路径上：asmr.one 故障时（如 522）登录重试会
    把响应拖到几十秒，因此用短超时客户端 + 失败熔断（10 分钟冷却）兜底。
    """
    global _api_sub_fail_at
    if time.monotonic() - _api_sub_fail_at < _API_SUB_COOLDOWN:
        return []
    try:
        _, num = api.AsmrClient.normalize_id(work_id)
    except ValueError:
        return []

    try:
        lite = dict(cfg)
        lite["timeout"] = 8.0
        lite["retries"] = 1
        client = _make_client(lite)
        client.login()
        entries = api.AsmrClient.flatten(client.tracks(num))
        _api_sub_fail_at = 0.0
    except Exception as ex:
        _api_sub_fail_at = time.monotonic()
        print(f"[subtitle] api subtitle list failed: {ex}", file=sys.stderr, flush=True)
        return []

    subs = []
    seen: set[str] = set()
    for e in entries:
        ext = os.path.splitext(e.name)[1].lower()
        if ext not in SUBTITLE_EXTS or e.name in seen:
            continue
        seen.add(e.name)
        subs.append({
            "filename": e.name,
            "ext": ext,
            "size": e.size,
            "language": "und",
            "source": "api",
            "confidence": 0.85 if work_id.upper() in e.name.upper() else 0.75,
        })
    return subs


def _download_api_subtitle(cfg: dict, work_id: str, filename: str) -> tuple[str | None, str]:
    """下载远程字幕并转成 VTT 文本，返回 (vtt 文本, 原始扩展名)。"""
    _, num = api.AsmrClient.normalize_id(work_id)
    client = _make_client(cfg)
    client.login()
    entries = api.AsmrClient.flatten(client.tracks(num))
    target = next((e for e in entries if e.name == filename), None)
    if target is None:
        return None, ""

    headers = {"Referer": "https://asmr.one/", "Origin": "https://asmr.one"}
    if client.token:
        headers["Authorization"] = "Bearer " + client.token

    conn, resp, status, _h = net.download_stream(
        target.url, cfg=client.net, headers=headers,
        timeout=float(cfg.get("timeout", 30)))
    try:
        if status not in (200, 206):
            return None, ""
        raw = resp.read()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    ext = os.path.splitext(filename)[1].lower()
    return _to_vtt(_decode(raw), ext), ext


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp932", "gbk", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _to_vtt(content: str, ext: str) -> str:
    """统一转成 WebVTT。"""
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    if ext == ".vtt":
        return content if content.lstrip().upper().startswith("WEBVTT") else "WEBVTT\n\n" + content
    if ext == ".srt":
        return _srt_to_vtt(content)
    if ext in (".ass", ".ssa"):
        return _ass_to_vtt(content)
    return "WEBVTT\n\n" + content


def _srt_to_vtt(srt: str) -> str:
    """SRT → VTT：时间戳逗号换点，去序号行。"""
    out = ["WEBVTT", ""]
    for line in srt.split("\n"):
        if re.fullmatch(r"\d+", line.strip()):
            continue  # SRT 的序号行
        if "-->" in line:
            line = line.replace(",", ".")
        out.append(line)
    return "\n".join(out)


_ASS_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[.,]?(\d{0,3})")


def _ass_to_vtt(ass: str) -> str:
    """ASS/SSA → VTT（只取 Dialogue 行）。"""
    out = ["WEBVTT", ""]
    in_events = False
    for line in ass.split("\n"):
        s = line.strip()
        if s.startswith("["):
            in_events = s.lower().startswith("[events]")
            continue
        if not in_events or not s.lower().startswith("dialogue:"):
            continue
        fields = s.split(",", 9)
        if len(fields) < 10:
            continue
        m1 = _ASS_TIME.match(fields[1].strip())
        m2 = _ASS_TIME.match(fields[2].strip())
        if not m1 or not m2:
            continue

        def fmt(m):
            h, mi, se, cs = m.group(1), m.group(2), m.group(3), (m.group(4) or "0").ljust(3, "0")[:3]
            return f"{int(h):02d}:{mi}:{se}.{cs}"

        text = re.sub(r"\{[^}]*\}", "", fields[9]).replace("\\N", "\n").strip()
        if not text:
            continue
        out.extend([f"{fmt(m1)} --> {fmt(m2)}", text, ""])
    return "\n".join(out)


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------

@player_bp.route("/player/work/<source_id>", methods=["GET"])
def get_player_work(source_id: str):
    """播放页/详情弹窗所需：作品信息 + 本地文件 + 字幕匹配。"""
    db = current_app.config["DB"]
    cfg = current_app.config["APP_CONFIG"]

    w = db.get_work(source_id)

    # 未入库（例如刚搜到还没下载）→ 直接从 API 取元信息
    if not w:
        try:
            _, num = api.AsmrClient.normalize_id(source_id)
            client = _make_client(cfg)
            client.login()
            info = client.work_info(num)
        except Exception as e:
            return jsonify({"error": "not_found", "detail": str(e)}), 404
        w = {
            "source_id": str(info.get("source_id") or source_id),
            "title": info.get("title") or info.get("name") or "",
            "release_date": info.get("release") or "",
            "tags": [t["name"] if isinstance(t, dict) else str(t) for t in (info.get("tags") or [])],
            "has_subtitle": bool(info.get("has_subtitle")),
            "dl_count": info.get("dl_count", 0),
            "rating": float(info.get("rating") or 0),
            "local_path": "",
        }

    files = db.get_work_files(source_id) if w.get("local_path") else []

    # 本地字幕
    local_path = w.get("local_path") or ""
    local_subs = _scan_subtitles(local_path, source_id) if local_path else []

    # 本地没有 → 问 API 要
    api_subs = _fetch_api_subtitles(cfg, source_id) if not local_subs else []

    return jsonify({
        "work": w,
        "files": files,
        "subtitles": local_subs + api_subs,
        "has_local": bool(local_path and os.path.exists(local_path)),
        "audio_files": [f for f in files
                        if f.get("status") == "downloaded"
                        and os.path.splitext(f.get("filename", ""))[1].lower() in AUDIO_EXTS],
    })


@player_bp.route("/player/api_subtitle/<source_id>", methods=["GET"])
def get_api_subtitle(source_id: str):
    """
    GET /api/player/api_subtitle/<source_id>?name=xxx.srt
    下载远程字幕 → 转 VTT 返回，并缓存到作品目录（下次直接当本地字幕用）。
    """
    cfg = current_app.config["APP_CONFIG"]
    db = current_app.config["DB"]
    name = os.path.basename((request.args.get("name") or "").strip())
    if not name:
        return jsonify({"error": "missing_name"}), 400

    # 已缓存过 → 直接读本地
    w = db.get_work(source_id)
    if w and w.get("local_path"):
        cached = os.path.join(w["local_path"], os.path.splitext(name)[0] + ".vtt")
        if os.path.exists(cached):
            return Response(_read_text_any(cached), mimetype="text/vtt; charset=utf-8")

    try:
        vtt, ext = _download_api_subtitle(cfg, source_id, name)
    except Exception as e:
        return jsonify({"error": "subtitle_fetch_failed", "detail": str(e)}), 502
    if vtt is None:
        return jsonify({"error": "subtitle_not_found"}), 404

    # 缓存到本地，后续离线也能用
    if w and w.get("local_path") and os.path.isdir(w["local_path"]):
        try:
            cached = os.path.join(w["local_path"], os.path.splitext(name)[0] + ".vtt")
            with open(cached, "w", encoding="utf-8") as f:
                f.write(vtt)
        except OSError:
            pass

    return Response(vtt, mimetype="text/vtt; charset=utf-8")


@player_bp.route("/player/subtitle_content/<path:subpath>", methods=["GET"])
def get_subtitle_content(subpath: str):
    """返回本地字幕内容（统一转 VTT）。subpath 形如 <source_id>/<相对路径或文件名>。"""
    db = current_app.config["DB"]

    if "/" not in subpath:
        return jsonify({"error": "subtitle_not_found"}), 404

    sid, rest = subpath.split("/", 1)
    w = db.get_work(sid)
    root = (w or {}).get("local_path") or ""
    if not root or not os.path.isdir(root):
        return jsonify({"error": "subtitle_not_found"}), 404

    base = os.path.realpath(root)
    rel = rest.replace("\\", "/").lstrip("/")
    target = ""

    # 1) 相对路径精确匹配（限制在作品目录内，防目录穿越）
    candidate = os.path.realpath(os.path.join(base, rel))
    if candidate.startswith(base + os.sep) and os.path.isfile(candidate):
        target = candidate

    # 2) 回退：按文件名在作品目录内递归查找（兼容只传 basename 的旧链接）
    if not target:
        name = os.path.basename(rel)
        for cur, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            if name in files:
                target = os.path.join(cur, name)
                break

    if not target:
        return jsonify({"error": "subtitle_not_found"}), 404

    ext = os.path.splitext(target)[1].lower()
    return Response(_to_vtt(_read_text_any(target), ext),
                    mimetype="text/vtt; charset=utf-8")
