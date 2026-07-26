# 速查表

项目根目录：`C:\Users\solus\Downloads\murakami-corpus`
（WSL 里是同一个地方：`/mnt/c/Users/solus/Downloads/murakami-corpus`）

---

## 链接

| 用途 | 地址 |
|---|---|
| 线上演示站（公开，空语料库） | https://murakami-corpus.onrender.com |
| 源码仓库 | https://github.com/jadexuan155229-dot/murakami-corpus |
| 本地网页版（需先启动） | http://127.0.0.1:5731 |

---

## 启动

### 桌面版 —— 日常入口

双击 `dist\羊男的图书馆.exe`。带完整语料，上传删除免口令，关窗即退出。

### 桌面版（源码运行，改完代码快速验证，不必打包）

```powershell
.venv-win\Scripts\python desktop.py
```

### 本地网页版

Windows：

```powershell
.venv-win\Scripts\python app.py        # 然后开 http://127.0.0.1:5731
```

WSL：

```bash
.venv/bin/python app.py
```

VS Code 里也可以 **Ctrl+Shift+P → Tasks: Run Task → 启动语料库网页**。
（已取消随打开文件夹自动启动，需手动触发。）

> ⚠️ 一次只开一个。桌面版和网页版同时跑、又指向同一个数据目录时，
> SQLite 文件锁会打架。

---

## 入库语料

界面上传：进任一作品页 → 选语言 → 传 `.epub` / `.txt` / `.pdf`。

批量入库走命令行更快（先指向桌面版的数据目录）：

```powershell
$env:CORPUS_DATA_DIR = "$env:LOCALAPPDATA\murakami-corpus"

.venv-win\Scripts\python -m corpus.cli list                          # 查作品 ID
.venv-win\Scripts\python -m corpus.cli add "D:\books\舞舞舞.epub" --work 22 --lang zh
.venv-win\Scripts\python -m corpus.cli search "羊男" --lang zh        # 终端里检索
```

`add` 还接 `--notes`；`search` 接 `--limit`。
全部子命令：`init` / `import-notion` / `add` / `list` / `search` / `repair-chapters`。

---

## 改完之后要做什么

**判断标准：动了仓库里的文件就要发布，动了语料就不用。**

| 改动 | 线上 | 桌面版 |
|---|---|---|
| 代码 / 模板 / 样式 | `git push`，自动部署 | **重新打包** |
| `works_metadata.csv`（加书目） | `git push`，自动部署 | **重新打包**（新书目会自动同步进库） |
| 上传 / 删除语料 | 无关 | 不用管 |

重新打包：

```powershell
.venv-win\Scripts\pyinstaller murakami-corpus.spec
```

产物覆盖 `dist\羊男的图书馆.exe`，约 35MB，构建几分钟。

---

## 数据在哪

| 环境 | 位置 |
|---|---|
| 桌面版（exe） | `%LOCALAPPDATA%\murakami-corpus\` |
| 本地网页版 / 源码运行 | 项目下的 `data/` |
| 线上 | 空；每次重启从 `works_metadata.csv` 重建书架 |

两处都是 `corpus.db`（SQLite 单文件，含全文索引）+ `files/`（入库的原始文件）。
搬迁直接整个目录复制即可，索引跟着走。

> ⚠️ 用 PowerShell 复制到新机器时，若目标目录尚不存在，`Copy-Item` 会静默
> 建出一个**同名文件**而不是目录。先建目录，或先跑一次 exe 让它自建：
> ```powershell
> New-Item -ItemType Directory -Force -Path "$env:LOCALAPPDATA\murakami-corpus"
> ```

---

## 两个虚拟环境别搞混

| 目录 | 平台 | 用途 |
|---|---|---|
| `.venv` | Linux（WSL） | 在 WSL 里跑网页版、跑测试 |
| `.venv-win` | Windows | 跑桌面版、打包 exe |

打包**必须**用 `.venv-win`——PyInstaller 不能交叉编译。

---

## 测试

```bash
for f in tests/test_*.py; do PYTHONPATH=. .venv/bin/python "$f"; done
```

八个模块：`bootstrap` / `delete_edition` / `ingest` / `reader` /
`repair_chapters` / `search` / `upload` / `write_guard`。

---

## 环境变量

| 变量 | 作用 |
|---|---|
| `CORPUS_DATA_DIR` | 数据目录。不设则：打包后用 `%LOCALAPPDATA%`，否则用项目下 `data/` |
| `CORPUS_ADMIN_PASSWORD` | 线上上传/删除的口令。**不设则整个写入面 403**（刻意的失败关闭） |
| `CORPUS_SECRET_KEY` | Flask session 密钥，生产必设（Render 已配成自动生成） |

桌面版和本地运行不需要设任何变量，除非要指向别处的语料库。

---

## 发给别人

exe **不含任何语料**（35MB 的包里装不下 37MB 的库，已实测确认）。对方拿到的是
空书架 + 完整工具，需用自己合法拥有的电子书建索引。

**不要把 `corpus.db` 一起发**——那是 13 个版本的作品正文，转给第三方属于分发
受版权保护的作品。

对方首次运行会遇到 Windows SmartScreen 蓝框（未签名程序的常态），
点「更多信息」→「仍要运行」。另需系统有 WebView2（Win11 自带）。
