# 公开部署与桌面版打包报告

> 时间：2026-07-25 至 2026-07-26
> 起点：项目此前只能在本地 `python app.py` 运行，无版本库、无部署、无分发形式。
> 终点：GitHub 公开仓库 + Render 线上演示站 + Windows 单文件 .exe，三者共用一套代码。

---

## 1. 本次工作概览

两件事，按顺序完成：

1. **上线公开站**——建版本库、推 GitHub、部署到 Render，拿到正式网址。
2. **打包桌面版**——同一套 Flask 代码装进原生窗口，构建出 Windows .exe。

贯穿两件事的核心约束是**版权**：`data/` 里是 13 个版本、48,748 个片段的村上作品
正文，仍在版权保护期内。全部改动都围绕"代码可以公开、正文不可以"这条线展开。

### 成果

| 形态 | 位置 | 状态 |
|---|---|---|
| 源码 | https://github.com/jadexuan155229-dot/murakami-corpus | 公开，312KB，不含任何正文 |
| 演示站 | https://murakami-corpus.onrender.com | 在线，空语料库，写操作关闭 |
| 桌面版 | `dist\羊男的图书馆.exe` | 已构建，35MB，可运行 |
| 本地库 | `data/corpus.db` | 未受影响，69 部作品 / 13 版本 / 48748 片段 |

---

## 2. 版权边界的处理

这是整轮工作最先确定、也最影响后续每个决定的一点。

原 README 写着"仅限个人研究使用，请勿公开部署"。带正文的公开部署等于把完整作品
放到公网可读可检索，属于发行行为。因此明确了三条路：**A 公开站 + 空语料库**、
**B 带全文 + 密码保护的私有站**、C 全公开——C 不做。最终选 A。

由此产生的具体措施：

- **`data/` 整个排除在版本库外**（[.gitignore](../.gitignore)）。推送前逐项核对过：
  `works_metadata.csv` 最长字段 71 字符且全是书目信息；所有提交文件中没有超过
  240 字符的散文行；线上仓库树里搜不到 `data/`、`.db`、`.epub`、`.zip`。
- **写操作守卫**（见 3.2）。公开站上开放上传，等于给陌生人一个往服务器塞盗版
  电子书的入口，且传完全站可搜——那就绕回方案 C 了。
- **首页说明横幅**。空库时告诉访客为什么是空的，并指向 GitHub 源码。

---

## 3. 部署部分的改动

### 3.1 仓库清理

原目录 188MB。排除 `.venv/`（虚拟环境）、`__pycache__/`、两个 zip 备份（共 80MB）、
`data/`（数据库 36MB + 上传文件 39MB + 两个备份 db）后，实际提交 **312KB / 32 个文件**。

另删掉了 `{corpus,templates,static,data` 这个目录——它是一次 shell 大括号展开
写错留下的空壳。

### 3.2 写操作守卫（[app.py](../app.py)）

新增 `admin_required` 装饰器，套在 `/work/<id>/upload` 和
`/work/<id>/edition/<id>/delete` 两个 POST 路由上。规则：

- 本地 `python app.py` → `LOCAL_DEV = True` → 直接放行；
- 生产（gunicorn / wsgi.py）→ `LOCAL_DEV` 保持 False：
  - 设了 `CORPUS_ADMIN_PASSWORD` → 要 HTTP Basic 口令；
  - **没设 → 一律 403，且模板里的上传表单与删除按钮不渲染。**

最后一条是刻意的**失败关闭**设计：忘了配置的后果是功能不可用，而不是门户大开。

`LOCAL_DEV` 的判定方式是：模块级默认 False，只在 `if __name__ == "__main__"` 块里
置 True。gunicorn 以 `wsgi:app` 导入时那个块不执行，因此生产环境不可能误开。

### 3.3 生产入口与可配置数据目录

- 新增 [wsgi.py](../wsgi.py)：`gunicorn wsgi:app`，不走带 debug 的开发服务器。
  原代码 `app.run(..., debug=True)` 硬编码，暴露在公网上的 Werkzeug 调试器
  基本等于任意代码执行。
- [corpus/db.py](../corpus/db.py) 的 `DATA_DIR` 改为读 `CORPUS_DATA_DIR` 环境变量。
- 新增 `db.bootstrap()`：建表；若 works 表为空则从 `works_metadata.csv` 载入书架。
  这让首次部署到空白磁盘时就有 69 部作品可浏览。
- `--preload` 让 bootstrap 在主进程完成后再 fork worker，避免多个 worker
  同时初始化同一个 SQLite 文件。

顺带把 `cli.py` 里那份 CSV 导入逻辑收进了 `db.py`（原先 app 和 cli 各有一份）。

### 3.4 部署配置

- [render.yaml](../render.yaml)：Render Blueprint，`CORPUS_SECRET_KEY` 配成自动生成，
  `CORPUS_ADMIN_PASSWORD` 留 `sync: false` 由 Dashboard 手填。持久磁盘的配置
  已写好但注释掉（free 档没有磁盘）。
- [Procfile](../Procfile)：供 Railway / Fly.io / Heroku 一类平台使用。

---

## 4. 桌面版部分的改动

### 4.1 技术选型

**pywebview + PyInstaller**。不重写任何东西：Flask、模板、SQLite、FTS5 索引全部
原样保留，外面套一个系统 webview 窗口（Windows 走 WebView2，Win11 自带）。

排除了 Electron / Tauri：后端是 Python，那两者最后仍需把 Python 作为 sidecar
一起打包，体积和复杂度翻几倍而换不来任何东西。

### 4.2 只读资源与可写数据的拆分（[corpus/db.py](../corpus/db.py)）

打包后代码跑在 PyInstaller 解出的**只读**临时目录里，这是桌面化最关键的一处适配：

- `RESOURCE_ROOT` 认 `sys._MEIPASS`——模板、静态资源、`works_metadata.csv` 在包里；
- `DATA_DIR` 在 frozen 时落到 `%LOCALAPPDATA%\murakami-corpus`——数据库必须可写，
  待在解包目录里会在每次关闭程序时消失；
- `CORPUS_DATA_DIR` 仍可覆盖两者之外的选择。

[app.py](../app.py) 相应改为显式传 `template_folder` / `static_folder`，
Flask 默认按模块位置推断在包里会找不到。

### 4.3 桌面入口（[desktop.py](../desktop.py)）

- 端口由内核分配（`bind(('127.0.0.1', 0))`），不与已在跑的本地服务撞车；
- 用 **waitress** 而非 Werkzeug 开发服务器——这个程序会被长时间开着检索；
- `wait_until_serving()` 等端口真正开始监听再开窗，否则窗口可能抢在前面
  加载出连接失败页；
- 启动时探测 SQLite 是否带 FTS5，缺了给出可读的错误而不是建表时的原始 SQL 报错；
- 置 `LOCAL_DEV = True`（本地个人工具，上传删除免口令）并每次启动生成随机
  `SECRET_KEY`。

### 4.4 打包配置（[murakami-corpus.spec](../murakami-corpus.spec)）

单文件模式，`console=False`。构建产物 35MB。

**过程中修正了一处自己的错误**：初版 spec 用的是 PyInstaller 5.x 语法
（`cipher=` / `block_cipher` / `PYZ(a.pure, a.zipped_data)`），而 6.0 已移除
字节码加密功能，按原样会直接报错。改为 6.x 语法后构建通过。

---

## 5. 验证情况

### 5.1 已验证

**测试套件**：原有 7 个模块全部通过。其中 `test_upload.py` 与
`test_delete_edition.py` 因新增守卫需要显式模拟本地运行，已相应调整。

**新增** [tests/test_write_guard.py](../tests/test_write_guard.py)：8 个用例覆盖
守卫行为——未配口令时上传/删除 403 且表单不渲染、配了口令后无凭证 401 /
错误口令 401 / 正确口令放行、读取路径始终开放。

**模拟生产环境**：用 gunicorn 起真实进程，空数据目录 → 自动建库 69 部作品
0 片段，读取全 200、上传 403；配上口令后无凭证 401、错误口令 401、正确口令 302。

**模拟打包环境**：伪造 `_MEIPASS` 与 `%LOCALAPPDATA%` 目录，验证资源解析、
数据落点、首启建库、模板与静态资源全部正确。

**线上实测**：三个读取路由全 200，HTTPS 正常，上传与删除均 403。

**桌面版实机**：Windows 上构建成功并运行，窗口正常，读到完整语料
（69 部 / 13 版本 / 48748 片段），检索"羊男"命中 4 部作品，上传表单可见。

### 5.2 环境事实

Windows 侧 Python 3.14.5，SQLite 带 FTS5。pythonnet 有 cp314 预编译 wheel，
PyInstaller 6.21 支持 3.14——原本担心的新版本兼容问题没有发生。

---

## 6. 遗留事项

### 6.1 待办

- **桌面版尚未接上语料**。exe 已能运行但书架是空的，需把 `data\corpus.db` 与
  `data\files\` 复制到 `%LOCALAPPDATA%\murakami-corpus\`。
- **图标未做**。[murakami-corpus.spec](../murakami-corpus.spec) 里
  `icon=` 一行已留好，放个 .ico 进 `static/` 取消注释即可。

### 6.2 一个已知的设计缺口

`bootstrap()` 只在 works 表**为空**时才载入 `works_metadata.csv`。因此从 Notion
重新导出一份含新作品的 CSV 后，即使重新打包 exe，新作品也不会出现在书架上。

改法很小——让 `bootstrap()` 每次启动都跑一遍导入即可，`import_works_csv()`
本就按中文书名去重，对已有作品是彻底的 no-op。副作用是手动删掉的作品会在
下次启动时回来。**尚未实施，等确认。**

### 6.3 Render 免费档的固有限制

- 闲置 15 分钟休眠，冷启动约 30 秒；
- 无持久磁盘，容器重启后上传内容清空，书架每次从 CSV 重新载入。

对"空语料库演示站"这个定位没有实际影响。要持久化需升级付费档并挂磁盘，
render.yaml 里配置已备好。

---

## 7. 一句话总结

这轮工作把项目从"一个只能在本机跑的脚本"变成了**三种可分发形态**——公开源码、
线上演示站、桌面程序——而三者共用同一套代码，靠 `LOCAL_DEV` 与 `CORPUS_DATA_DIR`
两个开关区分行为；同时用失败关闭的写权限守卫和彻底的数据隔离，
确保公开的部分永远只有工具，不含一个字的作品正文。
