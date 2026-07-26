# 羊男的图书馆 · 村上春树研究语料库

把散落在各处、各种格式的村上文本集中入库，提供跨全部藏书的中/日/英全文检索
（KWIC 上下文并列展示）。

**演示站：https://murakami-corpus.onrender.com** ——
只有书架和检索界面，语料库是空的（原因见下）。免费主机闲置后会休眠，
首次打开可能要等半分钟冷启动。

数据模型直接对应 Notion 里的〖村上春树〗表：
**作品 (works) → 版本 (editions：语言 × 格式) → 文本片段 (segments) → 全文索引**。
`works_metadata.csv` 从该表导出，包含 69 部作品的书名、年份、文体、关键词。

## 关于文本与版权

**本仓库不包含任何村上春树作品的正文。** 仓库里只有代码和作品元数据
（书名、出版年份、文体分类）——村上的作品仍在版权保护期内。

`data/` 已在 [.gitignore](.gitignore) 中排除，你在本地入库的电子书不会被提交。
请只索引你自己合法拥有的副本，并把索引结果留在本地。

线上演示站因此是**空的**：书架和检索界面都在，但一段正文都没有，
上传与删除也整个关闭（未配置 `CORPUS_ADMIN_PASSWORD`，见「部署」一节）。
它展示的是工具，不是语料。要真正用起来，请在本地运行。

## 本地运行

```bash
pip install flask          # 解析 PDF 另需 pip install pymupdf
python app.py              # 打开 http://127.0.0.1:5731
```

VS Code 里也可以用 **Ctrl+Shift+P → Tasks: Run Task → 启动语料库网页** 起同一个服务，
集成终端里会出现可点击的链接。该任务使用 VS Code 当前选中的 Python 解释器；
若该环境未装依赖，先运行上面的安装命令，或在状态栏切换解释器。

（这个任务原先配了 `runOn: folderOpen` 会在打开文件夹时自动启动，现已改为手动触发——
桌面版才是日常入口，网页版服务不必每次开项目都占着端口。）

首页即书架（按原版年份排列的 69 部作品）。进入任一作品页，选择语言并上传
epub / txt / pdf，上传后自动解析、分段并建立全文索引，随后即可在顶部检索框
跨全部藏书检索。

## 桌面版

同一套代码也能装进一个原生窗口——没有地址栏、没有浏览器标签，双击就开。
[desktop.py](desktop.py) 在后台线程用 waitress 起 Flask，pywebview 开一个系统
webview 窗口指向它（Windows 走 WebView2，Win11 自带，不用装运行时）。

先跑起来看看：

```bash
pip install -r requirements-desktop.txt
python desktop.py
```

打包成单文件 .exe——**必须在 Windows 的 PowerShell 里做，PyInstaller 不能交叉编译**，
WSL 里的 Linux venv 打不出 Windows 程序：

```powershell
py -m venv .venv-win
.venv-win\Scripts\pip install -r requirements-desktop.txt
.venv-win\Scripts\pyinstaller murakami-corpus.spec
```

产物是 `dist\羊男的图书馆.exe`，约 20MB（含 pymupdf 则 60MB 左右）。
单文件包每次启动要解压，冷启动 2-4 秒；嫌慢可以在
[murakami-corpus.spec](murakami-corpus.spec) 里改成 onedir 模式。

### 桌面版的数据存哪

打包后代码跑在 PyInstaller 解出的**只读**临时目录里，数据库不能待在那儿，
所以落到 `%LOCALAPPDATA%\murakami-corpus\`。未打包时（`python desktop.py`）
仍用项目下的 `data/`。

想让 .exe 直接用你现成的语料库，设环境变量指过去即可：

```powershell
$env:CORPUS_DATA_DIR = "C:\Users\solus\Downloads\murakami-corpus\data"
```

桌面版把 `LOCAL_DEV` 置为 True，上传与删除免口令——本地个人工具本该如此。
公开部署走的是 [wsgi.py](wsgi.py)，那边不受影响，写操作仍需口令。

## 命令行（批量入库更方便）

```bash
python -m corpus.cli list                          # 列出作品与 ID
python -m corpus.cli add 挪威的森林.epub --work 18 --lang zh
python -m corpus.cli add norwegian_wood.pdf --work 18 --lang en
python -m corpus.cli search "羊男" --lang zh        # 终端检索
```

## 检索说明

- **中文 / 日文**：任意子串均可命中，包括二字词（羊男、井户、地震…）。
  实现方式是字符级 FTS5 索引（入库时在每个 CJK 字符间插入空格，
  查询转为字符短语），绕开了通用分词器不识别中日文的问题。
- **英文**：按单词匹配，多词为 AND，大小写不敏感。
- 结果按作品分组，KWIC 表格中关键词居中对齐，点击关键词可查看该段前后文。
- PDF 按页入库并记录页码——标注"带页码"的英文 PDF 检索结果可直接用于引用定位。

## 部署

生产环境走 [wsgi.py](wsgi.py) + gunicorn（**不要**用 `python app.py`，那是开发服务器且开了 debug）：

```bash
gunicorn wsgi:app --bind 0.0.0.0:$PORT --workers 2 --preload
```

`--preload` 让建库/载入元数据在主进程完成后再 fork worker，避免多个 worker
同时初始化同一个 SQLite 文件。

[render.yaml](render.yaml) 是 Render 的 Blueprint，在 Render 选 **New › Blueprint**
指向本仓库即可建好服务。[Procfile](Procfile) 供 Railway / Fly.io / Heroku 一类平台使用。

### 环境变量

| 变量 | 作用 |
|---|---|
| `CORPUS_DATA_DIR` | SQLite 与上传文件的存放目录。挂了持久磁盘就指向挂载点；不设则用项目下的 `data/` |
| `CORPUS_SECRET_KEY` | Flask session 密钥。生产必设（Render 已配成自动生成随机值） |
| `CORPUS_ADMIN_PASSWORD` | 上传/删除的口令（HTTP Basic，用户名任意）。**不设则整个写入面关闭** |

写权限的规则：本地 `python app.py` 直接放行；生产环境未设
`CORPUS_ADMIN_PASSWORD` 时上传与删除一律 403，页面上的表单也不渲染。
这是刻意的失败关闭设计——公开站点上任人上传，等于给陌生人一个往服务器
塞受版权保护文本的入口。行为由 [tests/test_write_guard.py](tests/test_write_guard.py) 覆盖。

注意 Render 免费档没有持久磁盘：容器重启后上传的内容会清空，
书架元数据每次启动都会从 `works_metadata.csv` 重新载入。要持久化需升级付费档
并挂磁盘，render.yaml 里已写好注释掉的配置。

## 测试

```bash
python -m pytest tests/          # 或逐个 python tests/test_xxx.py
```

## 目录结构

```
app.py                 # Flask 网页端（浏览 / 上传 / 检索）+ 写操作口令守卫
wsgi.py                # 生产入口（gunicorn wsgi:app）
desktop.py             # 桌面版入口（pywebview 原生窗口）
murakami-corpus.spec   # PyInstaller 打包配置（须在 Windows 上构建）
corpus/db.py           # 数据模型 + FTS 索引 + CJK 查询构造 + KWIC
corpus/ingest.py       # epub（标准库解包）/ txt（自动探测编码）/ pdf（可选 pymupdf）
corpus/cli.py          # 命令行：init / import-notion / add / list / search
works_metadata.csv     # 69 部作品的元数据；空库首次启动时自动载入
render.yaml Procfile   # 部署配置
data/                  # SQLite 与入库文件（.gitignore 排除，不进版本库）
templates/ static/     # 前端
```

## txt 编码

自动依次尝试 utf-8 / gb18030 / shift_jis / big5 / utf-16。
早年下载的中文 txt 多为 GBK/GB18030，日文 txt 多为 Shift_JIS，均可直接入库。

## 下一步可扩展的方向（框架已预留）

1. **跨语言对照**：同一作品的中/日/英版本已挂在同一 work 下，
   可加"对照视图"——在中文版命中后并排显示英/日版对应章节。
2. **意象词表**：把"井、羊男、图书馆、地震、大象"等研究关键词
   存为固定词表，一键生成跨全集的分布统计（哪部作品、哪个年代出现频次）。
3. **导出引用**：从命中片段直接生成带页码的引用条目（Chicago/MLA）。
4. **Notion 双向同步**：editions 的索引状态回写到 Notion 表的"格式"列。
5. **微信读书 / caj**：caj 可先用 caj2pdf 转换后按 PDF 入库。
