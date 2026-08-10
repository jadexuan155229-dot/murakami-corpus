# 项目运行规则

- 当前项目在 WSL Ubuntu 中运行。
- Python 虚拟环境为项目根目录下的 `.venv`。
- 不使用系统 Python `/usr/bin/python3` 运行项目命令。
- 执行 Python 命令时优先使用：`.venv/bin/python`。
- 执行全部测试时必须使用：`.venv/bin/python -m pytest`。
- 启动项目时使用：`.venv/bin/python app.py`。
- 不使用 `.venv-win`，因为它属于 Windows 环境。
- 不修改数据库、`data` 目录或作品元数据，除非用户明确要求。
- 修改后必须运行测试，并准确报告结果。
