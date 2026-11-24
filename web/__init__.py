"""Flask app factory — 创建应用实例，注册蓝图，初始化 db。"""
from __future__ import annotations

import os
import sys

# 让路由模块能 import 同项目的 ipv6_net / asmr_client
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 可选依赖（faster-whisper/pypdf 等）装在 vendor 目录时优先加载
_vendor = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor")
if os.path.isdir(_vendor) and _vendor not in sys.path:
    sys.path.insert(0, _vendor)

from flask import Flask, jsonify

from web.config import load_config
from web.db import Database


def create_app(config_path: str | None = None) -> Flask:
    cfg = load_config(config_path)

    app = Flask(
        __name__,
        static_folder="static",
        static_url_path="",
    )
    app.config["APP_CONFIG"] = cfg

    # 初始化数据库
    db_path = cfg.get("db_path") or os.path.join(os.path.dirname(__file__), "data", "library.db")
    db = Database(db_path)
    app.config["DB"] = db

    # 对账上次进程遗留的 pending/running 任务（重启后 worker 线程已不存在）
    try:
        fixed = db.reconcile_orphan_tasks()
        if fixed["done"] or fixed["failed"]:
            app.logger.warning("孤儿下载任务对账：补记完成 %s 个，标记失败 %s 个",
                               fixed["done"], fixed["failed"])
    except Exception:
        app.logger.exception("孤儿下载任务对账失败")

    # 简单的根路由（健康检查）
    @app.route("/api/health")
    def health():
        return jsonify({"status": "ok", "app": "asmr-webui"})

    @app.route("/")
    def index():
        return app.send_static_file("index.html")

    # 本地作品文件浏览页（二级页面，前端用 History API 渲染，刷新/直达都可用）
    @app.route("/library/<source_id>")
    def library_page(source_id):
        return app.send_static_file("index.html")

    @app.route("/player")
    def player_page():
        return app.send_static_file("player.html")

    # 注册蓝图
    from web.routes.search import search_bp
    from web.routes.download import download_bp
    from web.routes.library import library_bp
    from web.routes.player import player_bp
    from web.routes.system import system_bp
    from web.routes.cover import cover_bp
    from web.routes.subtitles import subtitles_bp

    app.register_blueprint(search_bp, url_prefix="/api")
    app.register_blueprint(download_bp, url_prefix="/api")
    app.register_blueprint(library_bp, url_prefix="/api")
    app.register_blueprint(player_bp, url_prefix="/api")
    app.register_blueprint(system_bp, url_prefix="/api")
    app.register_blueprint(cover_bp, url_prefix="/api")
    app.register_blueprint(subtitles_bp, url_prefix="/api")

    # 后台销量刷新：超过 7 天未更新的作品每小时尝试刷新
    try:
        from web.routes.download import start_dl_refresh_scheduler
        start_dl_refresh_scheduler(db, cfg)
    except Exception:
        app.logger.exception("销量刷新调度启动失败")

    # 后台补齐作品总时长：只补齐 duration=0 的本地作品，避免统计漏算
    try:
        import threading
        from web.routes.library import backfill_work_durations

        def _run_backfill():
            try:
                n = backfill_work_durations(db)
                app.logger.info("作品总时长回填完成：%s 个", n)
            except Exception:
                app.logger.exception("作品总时长回填失败")

        threading.Thread(target=_run_backfill, name="duration-backfill",
                         daemon=True).start()
    except Exception:
        app.logger.exception("作品总时长回填线程启动失败")

    # 字幕队列：服务启动即拉起 worker，自动续跑队列与重启中断的任务
    try:
        from web.subtitles import get_engine
        get_engine(app)
        resumed = db.resume_restart_interrupted_jobs()
        if resumed:
            app.logger.warning("字幕队列：续跑 %s 个重启中断任务", resumed)
        app.logger.info("字幕队列 worker 已启动")
    except Exception:
        app.logger.exception("字幕队列 worker 启动失败")

    # 错误处理
    @app.errorhandler(404)
    def not_found(_):
        return jsonify({"error": "not_found"}), 404

    @app.errorhandler(500)
    def server_error(e):
        import traceback
        traceback.print_exc()
        return jsonify({"error": "server_error", "detail": str(e)}), 500

    return app
