"""生产入口：gunicorn wsgi:app

与 `python app.py` 的区别：
  - 不开 debug，不用 Flask 自带的开发服务器；
  - LOCAL_DEV 保持 False，上传/删除必须凭 CORPUS_ADMIN_PASSWORD；
  - 启动时确保库表就位；空库（首次部署到空白磁盘）自动载入书架元数据。
"""

from app import app
from corpus import db

db.bootstrap()

__all__ = ["app"]
