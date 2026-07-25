"""ASGI 入口；应用结构由 bootstrap composition root 负责。"""

from app.bootstrap.application import create_app
from app.config import settings  # 兼容既有测试/部署 patch 路径

app = create_app()

__all__ = ["app", "settings"]
