# -*- coding: utf-8 -*-
"""
===================================
WebUI 启动脚本
===================================

用于启动 Web 服务界面。
直接运行 `python webui.py` 将启动 Web 后端服务。

等效命令：
    python main.py --webui-only

Usage:
  python webui.py
  WEBUI_HOST=0.0.0.0 WEBUI_PORT=8000 python webui.py
"""

from __future__ import annotations

import os
import logging

logger = logging.getLogger(__name__)


def main() -> int:
    """
    启动 Web 服务
    """
    # 兼容旧版环境变量名
    host = os.getenv("WEBUI_HOST", os.getenv("API_HOST", "127.0.0.1"))
    port = int(os.getenv("WEBUI_PORT", os.getenv("API_PORT", "8000")))

    print(f"正在启动 Web 服务: http://{host}:{port}")
    print(f"API 文档: http://{host}:{port}/docs")
    print()

    try:
        import uvicorn
        from src.config import setup_env
        from src.logging_config import setup_logging
        from src.webui_frontend import prepare_webui_frontend_assets

        setup_env()
        setup_logging(log_prefix="web_server")
        if not prepare_webui_frontend_assets():
            logger.warning("WebUI 前端静态资源未准备完成，将继续启动 API 服务")

        uvicorn.run(
            "api.app:app",
            host=host,
            port=port,
            log_level="info",
        )
    except KeyboardInterrupt:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
