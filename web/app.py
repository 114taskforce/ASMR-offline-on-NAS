"""WebUI 入口：python3 web/app.py"""
from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web import create_app


def _lan_ip() -> str:
    """取本机对外 IPv4，仅用于启动日志展示。"""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("223.5.5.5", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return "127.0.0.1"


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--config", help="配置文件路径")
    p.add_argument("--host", default=None, help="监听地址（默认 ::）")
    p.add_argument("--port", type=int, default=None, help="端口（默认 8787）")
    args = p.parse_args()

    app = create_app(args.config)
    cfg = app.config["APP_CONFIG"]

    host = args.host or cfg.get("web_host", "::")
    port = args.port or int(cfg.get("web_port", 8787))

    print("=" * 60)
    print(f"ASMR WebUI 启动中...")
    print(f"  监听: [{host}]:{port}")
    print(f"  数据库: {cfg.get('db_path')}")
    print(f"  存储根: {[r['path'] for r in cfg.get('storage_roots', [])]}")
    print(f"  下载端口启用 strict IPv6（Flask HTTP 不启用）")
    print(f"  LAN 访问: http://{_lan_ip()}:{port}")
    print("=" * 60)

    # 后台按磁盘实际文件重算作品占用，修复旧库里 total_size 为 0 导致
    # 设置页把已下载数据全部算成「系统/其他」的问题
    def _refresh_sizes() -> None:
        with app.app_context():
            try:
                app.config["DB"].refresh_local_sizes()
            except Exception:
                pass

    threading.Thread(target=_refresh_sizes, daemon=True).start()

    # 后台预热「生成字幕」页的作品状态缓存，避免首次打开等全库扫描
    def _warm_subtitles() -> None:
        with app.app_context():
            try:
                from web.routes.subtitles import warm_works_cache
                warm_works_cache()
            except Exception:
                pass

    threading.Thread(target=_warm_subtitles, daemon=True).start()

    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
