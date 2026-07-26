"""桌面版入口：把 Flask 装进一个原生窗口。

    pip install -r requirements-desktop.txt
    python desktop.py

Flask 在后台线程上监听一个随机空闲端口，pywebview 开一个系统 webview 窗口
（Windows 用 WebView2）指向它。没有地址栏，没有浏览器标签，关窗即退出。

数据库与入库文件的位置见 corpus.db._default_data_dir()：
打包后是 %LOCALAPPDATA%\\murakami-corpus，未打包时是项目下的 data/。
想让桌面版直接用现成的语料库，设 CORPUS_DATA_DIR 指过去即可。
"""

from __future__ import annotations

import secrets
import socket
import sqlite3
import sys
import threading
import time

import webview

import app as webapp
from corpus import db

WINDOW_TITLE = "羊男的图书馆 · 村上春树研究语料库"


def free_port() -> int:
    """让内核挑一个空闲端口，避免和已在跑的本地服务撞车。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def check_fts5() -> None:
    """全文检索依赖 SQLite 的 FTS5 模块，缺了要早点说清楚。"""
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE _probe USING fts5(x)")
    except sqlite3.OperationalError as exc:
        raise SystemExit(
            "这个 Python 的 SQLite 没有编译 FTS5 模块，全文检索无法工作。\n"
            f"（原始错误：{exc}）\n"
            "Windows 上请改用 python.org 的官方发行版重新打包。"
        ) from exc
    finally:
        con.close()


def serve(port: int) -> None:
    """跑 WSGI 服务。

    优先用 waitress：纯 Python、Windows 友好、打包干净，适合长时间开着。
    Werkzeug 自带的是开发服务器，只当兜底。
    """
    try:
        from waitress import serve as waitress_serve
    except ImportError:
        webapp.app.run(
            host="127.0.0.1", port=port,
            debug=False, use_reloader=False, threaded=True,
        )
    else:
        waitress_serve(webapp.app, host="127.0.0.1", port=port, threads=4)


def wait_until_serving(port: int, timeout: float = 15.0) -> None:
    """等 Flask 真正开始监听再开窗，否则窗口可能抢在前面加载出连接失败页。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as s:
            s.settimeout(0.25)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise SystemExit(f"本地服务在 {timeout:.0f} 秒内没有启动，端口 {port}。")


def main() -> None:
    check_fts5()

    # 桌面版是本地个人工具：上传与删除免口令。
    # （公开部署走 wsgi.py，那边 LOCAL_DEV 保持 False，写操作必须凭口令。）
    webapp.LOCAL_DEV = True
    webapp.app.config["SECRET_KEY"] = secrets.token_hex(32)

    db.bootstrap()

    port = free_port()
    threading.Thread(target=serve, args=(port,), daemon=True).start()
    wait_until_serving(port)

    webview.create_window(
        WINDOW_TITLE,
        f"http://127.0.0.1:{port}/",
        width=1280,
        height=860,
        min_size=(940, 600),
    )
    webview.start()  # 阻塞到窗口关闭；守护线程随进程退出


if __name__ == "__main__":
    sys.exit(main())
