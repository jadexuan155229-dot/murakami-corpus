# PyInstaller 打包配置（PyInstaller 6.x 语法）。在 Windows 的 PowerShell 里构建：
#
#     py -m venv .venv-win
#     .venv-win\Scripts\pip install -r requirements-desktop.txt
#     .venv-win\Scripts\pyinstaller murakami-corpus.spec
#
# 产物：dist\羊男的图书馆.exe（单文件）
# 注意：PyInstaller 不能交叉编译，在 WSL/Linux 里构建不出 Windows 程序。

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
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],   # Flask 拖不进来，但 Python 标准库自带，排掉省体积
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

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
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,        # 不弹黑框命令行窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon="static/icon.ico",   # 有图标的话取消注释
)
