# 私人书库跨电脑同步 / Private Library Sync

**代码通过 GitHub 同步，文学语料不通过 GitHub 同步。** 本功能只同步 `corpus.db`（包括 FTS 索引）与 `data/files/` 原始文件，不包含历史 OCR/章节修复备份。历史备份保留在原设备。

## 安全规则

- Mac、Windows **轮流**增删语料。不要两边各改完再相互同步；此工具不是数据库合并器。
- 使用前**关闭全部网页与桌面程序**（尤其是桌面版随机端口）；关闭进程后再导出/导入。
- 使用私人存储位置传输 ZIP。文件含受版权保护的正文。**不要上传公开 GitHub，分享链接也不要公开。**
- 仅导入自己生成并信任的 ZIP。首次把某台设备纳入同步需要 `--force`，命令会把旧 `data` 目录保留到 `.murakami-sync-backups/`，请先确认目标机没有尚未迁移的新增文本。
- 后续自动拒绝检测到的本地更改或版本分叉。若两台电脑各有新增内容，停止操作，先人工整理，**不要用 `--force` 解决冲突**。
- 同步包不会自动删除。使用含日期时间的独立文件名，别覆盖旧包。传输后确认 ZIP 完整，再删除过期备份。

## 命令

在项目根目录，进入原本项目使用的 Python 环境，例如 Mac 上 `conda activate murakami` 或 Windows WSL 内使用 `.venv/bin/python`。

导出（ZIP 保存到项目 data 目录之外，路径可为已同步完成的私人文件夹）：

```sh
python -m corpus.sync export "/path/to/private-sync/murakami-2026-10-24-1900.zip"
```

另一台电脑接收时，在停止程序后：

```sh
python -m corpus.sync import "/path/to/private-sync/murakami-2026-10-24-1900.zip"
```

**仅第一次迁移、人工确认目标机数据不需要保留后：**

```sh
python -m corpus.sync import "/path/to/private-sync/murakami-2026-10-24-1900.zip" --force
```

Windows WSL 文件路径应使用 Linux 路径（例如 `/mnt/c/Users/solus/OneDrive/...`），不是 PowerShell 的 `C:\...`。

导出过程使用 SQLite Backup API 创建一致的数据库快照，包含 WAL 未写回的修改；每个文件有 SHA-256 校验。接收会先解包到临时目录、校验数据库，然后备份旧书库并替换。导入后的原始文件依然在本地 `data/`；存放 ZIP 的共享云盘**只承担传送角色**，不要把活动数据库直接放进云盘。

**重要限制：** 当前只检测常规网页版的 5731 端口。桌面版使用随机端口，无法自动检测，必须手动退出。不要在两个实例运行时同步。同样，书库文件可能在导出时被其他程序改动，遇到警告应退出程序再尝试。

测试（不要使用真正书库）：

```sh
python -m unittest tests.test_sync
```

## VS Code 一键任务（Mac / Windows WSL）

切换到 `feature/private-library-sync` 分支并在 VS Code 中通过 **Python: Select Interpreter** 选择项目的 Python（Mac 用 `murakami` Conda 环境，WSL 用项目原有 `.venv`）。然后按 `Cmd+Shift+P`（Windows 为 `Ctrl+Shift+P`）→ **Tasks: Run Task**，可选择：

- **书库同步：导出私人快照**：输入新的 ZIP 路径。必须使用一个还不存在的文件名；建议附上日期、时间和设备名。
- **书库同步：接收并备份（仅已建立同步关系）**：输入已收到的 ZIP 文件路径。遇到首次导入或冲突时任务会拒绝覆盖，不能靠反复点击解决。

这两项任务不会自动执行 `--force`。如果是**第一次建立两台设备的同步关系**，必须先确认目标设备的本地书库有无后续新入库内容，再人工备份与执行首次导入流程。切勿在同步进行时打开网页版或桌面版；Windows WSL 中的路径要使用 `/mnt/c/...` 而非 `C:\\...`。输入路径可用 `~/...` 表示当前系统的用户目录。

同步任务不安装依赖，也不改变正在使用的解释器。源代码及说明更新后需要在另一台机器执行 `git pull`；真实语料库始终通过私人 ZIP 传输。

**备份目录位置：** 为避免把受版权保护的备份放进 Git 项目，自动回滚备份保存在目标 `data` 文件夹所属项目的**上一级目录**。例如 Mac 使用 `~/projects/.murakami-sync-backups/`，Windows WSL 使用 `/mnt/c/Users/solus/Downloads/.murakami-sync-backups/`。此目录位于项目仓库之外，不参与 Git 提交；不要随意删除。

## 2026-10：从旧的字节指纹迁移到逻辑指纹

旧版同步标记只有 `fingerprint`，它计算 SQLite 备份文件的物理 SHA-256。即使数据库表和内容不变，文件字节也可能不同，造成假的「已修改」警告。新版使用数据库用户表的逻辑数据、数据库结构以及原始文件的 SHA-256，仍保留原快照 ID 与原始 ZIP 格式。

**首次更新同步工具后，必须在两台电脑分别运行一次 `adopt-baseline`，并使用与当前同步标记对应的旧 ZIP。** 它将比较快照和本地内容，不替换 `corpus.db`、不移动 `files/`；只有一致才会给 `.sync-state.json` 增加 `logical_fingerprint` 字段。没有对应 ZIP、检查不一致、程序正在运行或数据库有未同步修改时，停止操作，不要强制覆盖或手动编辑标记。

Mac（在 `~/projects/murakami-corpus`，使用 `conda activate murakami`）：

```sh
python -m corpus.sync adopt-baseline "$HOME/Downloads/murakami-20261024-mac.zip"
```

Windows WSL（在 `/mnt/c/Users/solus/Downloads/murakami-corpus`）：

```sh
./.venv/bin/python -B -m corpus.sync adopt-baseline "/mnt/c/Users/solus/Downloads/murakami-20261024-mac.zip"
```

成功后可只读检查本机标记：

```sh
python -c "from corpus import db; from corpus.sync import _read_state, _logical_fingerprint; s=_read_state(db.DATA_DIR); print('Snapshot:',s['snapshot_id']); print('逻辑书库一致:',s.get('logical_fingerprint')==_logical_fingerprint(db.DATA_DIR))"
```

Windows 上如使用特定解释器，将命令里的 `python` 换为 `./.venv/bin/python`。

正常应显示 `逻辑书库一致: True`。**这个步骤不重新导出，不重复导入，不使用 `--force`。** 完成两机验证后再通过菜单使用常规导出/接收；数据包仍需私人传输。
