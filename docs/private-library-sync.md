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
