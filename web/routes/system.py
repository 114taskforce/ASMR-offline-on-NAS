"""系统状态 API — 存储空间、IPv6 环境、应用信息"""
from __future__ import annotations

import os
import platform
import shutil
import threading
import time

from flask import Blueprint, current_app, jsonify, request
from werkzeug.security import generate_password_hash

from web.config import save_config, storage_entries

system_bp = Blueprint("system", __name__)

APP_VERSION = "1.1.0"
_START_TS = time.time()


def _asmr_usage(db) -> dict[str, int]:
    try:
        return db.storage_usage()
    except Exception:
        return {}


def _storage_payload(cfg: dict, db) -> list[dict]:
    """存储空间列表 + 用量（按配置顺序 = 下载优先级）。"""
    usage = _asmr_usage(db)
    out = []
    for idx, r in enumerate(storage_entries(cfg)):
        p = r["path"]
        item = {
            "path": p,
            "order": idx,
            "min_free_gb": int(r.get("min_free_gb", 0) or 0),
            "exists": os.path.isdir(p),
            "writable": os.access(p, os.W_OK) if os.path.isdir(p) else False,
            "total": 0, "free": 0, "used": 0,
            "asmr_used": 0, "other_used": 0,
        }
        asmr = int(usage.get(p) or usage.get(p.rstrip("/")) or 0)
        if os.path.isdir(p):
            try:
                st = os.statvfs(p)
                total = st.f_frsize * st.f_blocks
                free = st.f_frsize * st.f_bavail
                used = total - free
                item.update({"total": total, "free": free, "used": used})
                item["asmr_used"] = min(max(asmr, 0), used)
                item["other_used"] = max(0, used - item["asmr_used"])
            except OSError:
                item["exists"] = False
        out.append(item)
    return out


@system_bp.route("/system/status", methods=["GET"])
def system_status():
    cfg = current_app.config["APP_CONFIG"]
    db = current_app.config["DB"]

    import resource
    ru = resource.getrusage(resource.RUSAGE_SELF)
    mem_mb = ru.ru_maxrss / 1024  # Linux 上 ru_maxrss 为 KB
    stats = db.stats()

    configured = {r["path"] for r in storage_entries(cfg)}
    try:
        seen = db.storage_roots_seen()
    except Exception:
        seen = []
    suggestions = [p for p in seen if p not in configured]

    return jsonify({
        "app": {
            "name": "asmr-webui",
            "version": APP_VERSION,
            "pid": os.getpid(),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_START_TS)),
            "uptime_seconds": int(time.time() - _START_TS),
            "cpu_seconds": round(ru.ru_utime + ru.ru_stime, 1),
            "memory_mb": round(mem_mb, 1),
            "python": platform.python_version(),
            "platform": platform.system(),
        },
        "stats": stats,
        "config": {
            "db_path": cfg.get("db_path"),
            "web_host": cfg.get("web_host"),
            "web_port": cfg.get("web_port"),
            "workers": cfg.get("workers"),
            "qps": cfg.get("qps"),
        },
        "storages": _storage_payload(cfg, db),
        "storage_suggestions": suggestions,
        "delete_password_set": bool(cfg.get("delete_password_hash")),
        "safe_mode": bool(cfg.get("safe_mode")),
    })


@system_bp.route("/system/safe-mode", methods=["POST"])
def set_safe_mode():
    """安全模式开关：开启后前端把所有封面渲染为纯黑。"""
    cfg = current_app.config["APP_CONFIG"]
    data = request.get_json(silent=True) or {}
    cfg["safe_mode"] = bool(data.get("enabled"))
    save_config(cfg)
    return jsonify({"ok": True, "safe_mode": cfg["safe_mode"]})


@system_bp.route("/system/delete-password", methods=["POST"])
def set_delete_password():
    """设置/重置删除密码；传空字符串表示清除。"""
    cfg = current_app.config["APP_CONFIG"]
    data = request.get_json(silent=True) or {}
    pw = str(data.get("password") or "")
    if len(pw) > 128:
        return jsonify({"error": "密码过长"}), 400
    if pw == "":
        cfg.pop("delete_password_hash", None)
    else:
        cfg["delete_password_hash"] = generate_password_hash(pw)
    save_config(cfg)
    return jsonify({"ok": True, "set": bool(cfg.get("delete_password_hash"))})


@system_bp.route("/system/storages", methods=["GET"])
def list_storages():
    cfg = current_app.config["APP_CONFIG"]
    db = current_app.config["DB"]
    return jsonify({"storages": _storage_payload(cfg, db)})


@system_bp.route("/system/storages", methods=["POST"])
def add_storage():
    """添加存储空间：校验路径可用、去重，追加到优先级末尾。"""
    cfg = current_app.config["APP_CONFIG"]
    db = current_app.config["DB"]
    data = request.get_json(silent=True) or {}
    path = str(data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "请填写存储路径"}), 400
    if not os.path.isabs(path):
        return jsonify({"error": "请填写绝对路径，例如 /vol2/asmr-downloads"}), 400

    for r in storage_entries(cfg):
        if r["path"].rstrip("/") == path.rstrip("/"):
            return jsonify({"error": "该存储路径已存在"}), 400

    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        return jsonify({"error": f"无法创建目录: {e}"}), 400

    if not os.path.isdir(path):
        return jsonify({"error": "路径不是目录"}), 400
    if not os.access(path, os.W_OK):
        return jsonify({"error": "目录不可写，请检查权限"}), 400

    roots = [dict(r) for r in (cfg.get("storage_roots") or [])]
    roots.append({
        "path": path,
        "min_free_gb": int(data.get("min_free_gb") or 0),
    })
    cfg["storage_roots"] = roots
    try:
        save_config(cfg)
    except OSError as e:
        return jsonify({"error": f"配置保存失败: {e}"}), 500
    return jsonify({"ok": True, "storages": _storage_payload(cfg, db)})


@system_bp.route("/system/storages", methods=["DELETE"])
def remove_storage():
    """移除存储空间（只解除绑定，不删除硬盘上的文件）。"""
    cfg = current_app.config["APP_CONFIG"]
    db = current_app.config["DB"]
    data = request.get_json(silent=True) or {}
    path = str(data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "缺少存储路径"}), 400

    entries = storage_entries(cfg)
    if path not in [r["path"] for r in entries]:
        return jsonify({"error": "存储路径不存在"}), 404

    cfg["storage_roots"] = [
        dict(r) for r in (cfg.get("storage_roots") or [])
        if (r.get("path") if isinstance(r, dict) else str(r)) != path
    ]
    if (cfg.get("download_dir") or "").rstrip("/") == path.rstrip("/"):
        cfg["download_dir"] = ""
    try:
        save_config(cfg)
    except OSError as e:
        return jsonify({"error": f"配置保存失败: {e}"}), 500
    return jsonify({"ok": True, "storages": _storage_payload(cfg, db)})


@system_bp.route("/system/storages", methods=["PUT"])
def update_storage():
    """修改某个存储空间的保留空间（其余字段不变）。"""
    cfg = current_app.config["APP_CONFIG"]
    db = current_app.config["DB"]
    data = request.get_json(silent=True) or {}
    path = str(data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "缺少存储路径"}), 400
    try:
        reserve = max(0, int(data.get("min_free_gb") or 0))
    except (TypeError, ValueError):
        return jsonify({"error": "保留空间必须是非负整数"}), 400

    roots = [dict(r) for r in (cfg.get("storage_roots") or [])]
    matched = False
    for r in roots:
        if (r.get("path") or "").rstrip("/") == path.rstrip("/"):
            r["min_free_gb"] = reserve
            matched = True
            break

    if not matched:
        dd = (cfg.get("download_dir") or "").strip()
        if dd and dd.rstrip("/") == path.rstrip("/"):
            roots.insert(0, {"path": dd, "min_free_gb": reserve})
            cfg["download_dir"] = ""
            matched = True
    if not matched:
        return jsonify({"error": "存储路径不存在"}), 404

    cfg["storage_roots"] = roots
    try:
        save_config(cfg)
    except OSError as e:
        return jsonify({"error": f"配置保存失败: {e}"}), 500
    return jsonify({"ok": True, "storages": _storage_payload(cfg, db)})


@system_bp.route("/system/storages/order", methods=["PUT"])
def reorder_storages():
    """保存存储空间顺序（第 1 个为下载首选）。"""
    cfg = current_app.config["APP_CONFIG"]
    db = current_app.config["DB"]
    data = request.get_json(silent=True) or {}
    paths = [str(p).strip() for p in (data.get("paths") or []) if str(p).strip()]

    entries = storage_entries(cfg)
    known = [r["path"] for r in entries]
    if sorted(paths) != sorted(known):
        return jsonify({"error": "存储列表不一致，请刷新后重试"}), 400

    by_path = {r["path"]: dict(r) for r in entries}
    cfg["storage_roots"] = [by_path[p] for p in paths]
    cfg["download_dir"] = ""
    try:
        save_config(cfg)
    except OSError as e:
        return jsonify({"error": f"配置保存失败: {e}"}), 500
    return jsonify({"ok": True, "storages": _storage_payload(cfg, db)})


@system_bp.route("/system/ipv6", methods=["GET"])
def system_ipv6():
    """测试 IPv6 环境（复用 ipv6_net）。"""
    try:
        import ipv6_net as net
        net.enable_strict_ipv6()

        result = {}
        try:
            # 出口探测
            trace = net.egress_trace(net.NetConfig(timeout=10, retries=2))
            result["egress"] = trace
        except Exception as e:
            result["egress_error"] = str(e)

        try:
            # API 登录测试
            cfg = current_app.config["APP_CONFIG"]
            client = __import__("asmr_client").AsmrClient(
                net.NetConfig(
                    pins=cfg.get("pins") or {},
                    sni_policy=cfg.get("sni_policy", "auto"),
                    timeout=15, retries=2,
                ),
                account=cfg.get("account", "guest"),
                password=cfg.get("password", "guest"),
            )
            token = client.login()
            result["api_login"] = "ok"
            result["token_len"] = len(token)
        except Exception as e:
            result["api_login_error"] = str(e)

        # AAAA 解析测试
        hosts = ["api.asmr.one", "asmr.one", "raw.kiko-play-niptan.one"]
        result["aaaa"] = {}
        for h in hosts:
            try:
                addrs = net.resolve_aaaa(h, 443,
                    (cfg.get("pins") or {}).get(h.lower()))
                result["aaaa"][h] = [a[0] for a in addrs[:2]]
            except Exception as e:
                result["aaaa"][h] = {"error": str(e)}

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------- 未入库文件扫描 ----------------

_orphan_lock = threading.Lock()
_orphan_state: dict = {
    "running": False,
    "finished_at": None,
    "items": [],
    "files": 0,
    "bytes": 0,
    "error": "",
}


def _is_known(real: str, known: set[str]) -> bool:
    return any(real == k or real.startswith(k + os.sep) or k.startswith(real + os.sep)
               for k in known)


def _scan_orphans(cfg: dict, db) -> list[dict]:
    """存储根下一层目录中，未被 works.local_path 引用的目录即未入库残留。"""
    known = {os.path.realpath(p) for p in db.local_paths()}
    items: list[dict] = []
    for entry in storage_entries(cfg):
        base = entry["path"]
        if not os.path.isdir(base):
            continue
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            if name.startswith("."):
                continue
            path = os.path.join(base, name)
            if not os.path.isdir(path):
                continue
            real = os.path.realpath(path)
            if _is_known(real, known):
                continue
            files = size = 0
            for dirpath, _dirnames, filenames in os.walk(path):
                for fn in filenames:
                    try:
                        size += os.path.getsize(os.path.join(dirpath, fn))
                        files += 1
                    except OSError:
                        continue
            items.append({"path": path, "root": base, "name": name,
                          "files": files, "size": size})
    items.sort(key=lambda x: -x["size"])
    return items


def _scan_orphans_worker(cfg: dict, db) -> None:
    try:
        items = _scan_orphans(cfg, db)
        with _orphan_lock:
            _orphan_state.update(
                items=items,
                files=sum(i["files"] for i in items),
                bytes=sum(i["size"] for i in items),
                error="",
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            )
    except Exception as e:
        with _orphan_lock:
            _orphan_state.update(error=str(e),
                                 finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    finally:
        with _orphan_lock:
            _orphan_state["running"] = False


@system_bp.route("/system/orphans", methods=["GET"])
def orphan_status():
    with _orphan_lock:
        return jsonify(dict(_orphan_state))


@system_bp.route("/system/orphans/scan", methods=["POST"])
def orphan_scan():
    with _orphan_lock:
        if _orphan_state["running"]:
            return jsonify(dict(_orphan_state))
        _orphan_state.update(running=True, error="", finished_at=None)
    cfg = current_app.config["APP_CONFIG"]
    db = current_app.config["DB"]
    threading.Thread(target=_scan_orphans_worker, args=(cfg, db), daemon=True).start()
    return jsonify({"running": True})


@system_bp.route("/system/orphans/clean", methods=["POST"])
def orphan_clean():
    """删除指定残留目录；只允许删除存储根下一层且未入库的目录。"""
    cfg = current_app.config["APP_CONFIG"]
    db = current_app.config["DB"]
    data = request.get_json(silent=True) or {}
    paths = [str(p).strip() for p in (data.get("paths") or []) if str(p).strip()]
    if not paths:
        return jsonify({"error": "没有要清理的目录"}), 400

    known = {os.path.realpath(p) for p in db.local_paths()}
    roots = {os.path.realpath(e["path"]) for e in storage_entries(cfg)
             if os.path.isdir(e["path"])}
    removed = 0
    freed = 0
    failed: list[dict] = []
    for p in paths:
        real = os.path.realpath(p)
        if os.path.dirname(real) not in roots:
            failed.append({"path": p, "error": "不在存储根目录下"})
            continue
        if _is_known(real, known):
            failed.append({"path": p, "error": "已在数据库登记"})
            continue
        if not os.path.isdir(real):
            removed += 1
            continue
        size = 0
        for dirpath, _dirnames, filenames in os.walk(real):
            for fn in filenames:
                try:
                    size += os.path.getsize(os.path.join(dirpath, fn))
                except OSError:
                    continue
        try:
            shutil.rmtree(real)
        except OSError as e:
            failed.append({"path": p, "error": str(e)})
            continue
        removed += 1
        freed += size

    gone = {os.path.realpath(p) for p in paths}
    with _orphan_lock:
        remain = [i for i in _orphan_state["items"]
                  if os.path.realpath(i["path"]) not in gone]
        _orphan_state.update(items=remain,
                             files=sum(i["files"] for i in remain),
                             bytes=sum(i["size"] for i in remain))
    return jsonify({"ok": True, "removed": removed, "freed": freed, "failed": failed})
