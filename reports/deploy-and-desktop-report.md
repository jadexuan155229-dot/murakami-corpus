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
| 源码 | https://github.com/jadexuan155229-dot/murakami-corpus | 公开，不含任何正文 |
| 演示站 | https://murakami-corpus.onrender.com | 在线，空语料库，写操作关闭 |
| 桌面版 | `dist\羊男的图书馆.exe` | 35MB，已接上完整语料 |
| 桌面版数据 | `%LOCALAPPDATA%\murakami-corpus\` | 70 部作品 / 13 版本 / 48748 片段 |
| 本地库 | `data/corpus.db` | 未受影响，内容同上 |

> 表格反映的是 7 节增补完成后的状态。3–5 节记录的是当时的验证过程，
> 其中的"69 部作品"是彼时的事实，未追改。

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

- **图标未做**。[murakami-corpus.spec](../murakami-corpus.spec) 里
  `icon=` 一行已留好，放个 .ico 进 `static/` 取消注释即可。

（原列的"桌面版尚未接上语料"已完成，见 7.1。）

### 6.2 一个已知的设计缺口 —— 已修复

`bootstrap()` 原先只在 works 表**为空**时才载入 `works_metadata.csv`，因此从
Notion 重新导出一份含新作品的 CSV 后，即使重新打包 exe，新作品也不会出现在
书架上。**已在 7.2 修复。**

### 6.3 Render 免费档的固有限制

- 闲置 15 分钟休眠，冷启动约 30 秒；
- 无持久磁盘，容器重启后上传内容清空，书架每次从 CSV 重新载入。

对"空语料库演示站"这个定位没有实际影响。要持久化需升级付费档并挂磁盘，
render.yaml 里配置已备好。

---

## 7. 后续增补（2026-07-26）

三件事，都已在线上和桌面端验证通过。

### 7.1 桌面版接上语料

`data\corpus.db` 与 `data\files\` 已复制到 `%LOCALAPPDATA%\murakami-corpus\`。
exe 现在显示完整的 70 部作品 / 13 个已索引版本 / 48748 个可检索片段，
FTS 索引随单文件数据库一同迁移，未受损。

留一条坑的记录：PowerShell 的 `Copy-Item` 在目标目录**不存在**时会把路径当成
目标文件名，静默地建出一个无扩展名的文件而不是目录，之后程序会因路径被占而
启动失败。本次未踩到，因为在复制前已跑过一次 exe，目录由
`DATA_DIR.mkdir(parents=True, exist_ok=True)` 建好了。换机器时若先复制再运行，
需先 `New-Item -ItemType Directory -Force -Path $dst`。

### 7.2 新增书目《夏帆》，并修掉书架同步的缺口

`works_metadata.csv` 新增《夏帆》（2026，小说，日文名同为夏帆），共 70 部。

`bootstrap()` 改为**每次启动都跑一遍导入**，不再只在空库时跑。`import_works_csv()`
本就按中文书名去重，对已有作品是彻底的 no-op，只有 CSV 新增的行会插进来。
代价：手动从库里删掉的作品会在下次启动时回来，想永久移除需同时删 CSV 里的行。

新增 [tests/test_bootstrap.py](../tests/test_bootstrap.py)：空库播种、幂等、
非空库收新作品（修复的核心）、元数据完整、CSV 缺失时不崩。

### 7.3 返回导航

桌面版是纯 webview，没有浏览器外壳因而没有后退按钮——但历史栈是在的。
在 [templates/base.html](../templates/base.html) 页眉加了一个接 `history.back()`
的按钮，并绑定 Alt+←（`preventDefault` 抑制 Chromium 自带的同名快捷键，
避免叠加成退两页）。网页版与桌面版共用同一套行为。

**过程中修了一个只在桌面版暴露的 bug**：初版用 `history.length > 1` 判断
"有没有可回退的地方"，这在浏览器里成立，但 WebView2 初始化时会先停在
`about:blank` 再跳转到应用地址，历史栈凭空多一条，于是首页也显示了按钮——
按下去会退进空白页，而桌面版没有前进按钮，只能关掉重开。

改用 `document.referrer` 是否同源判断，即"我是不是从本站另一个页面跳来的"，
这比"历史栈里有几条"更贴近真正要问的问题。

这类问题在 Linux 侧的模拟环境里测不出来，只能靠桌面端实机截图发现。

---

## 8. 一句话总结

这轮工作把项目从"一个只能在本机跑的脚本"变成了**三种可分发形态**——公开源码、
线上演示站、桌面程序——而三者共用同一套代码，靠 `LOCAL_DEV` 与 `CORPUS_DATA_DIR`
两个开关区分行为；同时用失败关闭的写权限守卫和彻底的数据隔离，
确保公开的部分永远只有工具，不含一个字的作品正文。
