# PyInstaller 打包配置。在 Windows 的 PowerShell 里构建：
#
#     py -m venv .venv-win
#     .venv-win\Scripts\pip install -r requirements-desktop.txt
#     .venv-win\Scripts\pyinstaller murakami-corpus.spec
#
# 产物：dist\羊男的图书馆.exe（单文件）
# 注意：PyInstaller 不能交叉编译，在 WSL/Linux 里构建不出 Windows 程序。

block_cipher = None

a = Analysis(
    ["desktop.py"],
    pathex=[],
    binaries=[],
    # 模板、样式和书架元数据要一起打进包里；运行时由 db.RESOURCE_ROOT 定位。
    datas=[
        ("templates", "templates"),
        ("static", "static"),
        ("works_metadata.csv", "."),
    ],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    # 这些是 Flask/pywebview 拖进来但完全用不到的重量级依赖。
    excludes=["tkinter", "unittest", "pytest", "numpy", "PIL"],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="羊男的图书馆",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    runtime_tmpdir=None,
    console=False,        # 不弹黑框命令行窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon="static/icon.ico",   # 有图标的话取消注释
)
