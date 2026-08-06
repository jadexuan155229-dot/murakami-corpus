# 2026-08-06 主题、阅读体验与开发环境更新报告

## 概览

本次更新集中在本地开发一致性与阅读界面的可切换视觉体验。未修改数据库结构、`data/`、作品元数据、上传、索引或检索逻辑。

## 开发环境

- VS Code 默认解释器固定为项目根目录的 `.venv`，终端自动激活该环境。
- 新增“运行全部测试”任务，执行 `${command:python.interpreterPath} -m pytest`。
- 新增 `AGENTS.md`，明确 WSL Ubuntu、`.venv/bin/python`、测试命令和数据保护规则。
- `.gitignore` 补充递归 `**pycache**/` 忽略规则。

## 复古纸页主题

- 新增独立的 `static/vintage-paper.css`，通过 `body.theme-vintage-paper` 覆盖基础样式。
- 纸张大范围明暗、细颗粒和纤维分层处理；大尺度背景固定且不重复，小尺度纹理才平铺，修复长阅读页的纵向背景接缝。
- 首页书架、筛选标签、检索结果、表单、目录、阅读正文和返回顶部均采用低饱和旧纸/墨蓝灰/砖红配色。
- 移除了统计区的套色色块，并修正当前筛选标签的粗底边。

## 阅读器

- 目录改为较深的索引纸签，正文采用更干净的浅纸，压缩标题与章节导航的垂直留白。
- 阅读页正文纸色在桌面端向左右外层纸页自然过渡；移动端保持稳定纯色。
- 标题下方短线根据当前章节在 `chapter['toc']` 中的相对位置移动着墨重心：首尾限制在 15%–85%，单章节回退至中点。
- 不改变目录跳转、前后章、搜索命中、高亮或锚点定位行为。

## 双主题与字体

- 新增本地持久化主题切换：`murakami-corpus-theme` 只保存 `classic` 或 `vintage`，默认复古纸页；初始化脚本在页眉渲染前同步应用，避免闪烁。
- 新增 `static/theme-switcher.js`，负责无刷新切换、保存选择及按钮的文字、标题和 ARIA 状态。
- 接入项目内 Courier Prime 四个字体面；仅复古主题的英文阅读正文使用它。
- 新增 `Vintage Digits` unicode-range 字体层，仅覆盖半角与全角数字，使复古主题下的年份、统计、目录、计数与正文数字保持等宽风格，而中日文字和英文字母继续使用各自原字体。
- Courier Prime 按 SIL Open Font License 1.1 随项目本地分发，许可证保留在 `static/fonts/courier-prime/OFL.txt`。

## 验证

- 执行：`.venv/bin/python -m pytest`
- 结果：89 项通过。
- 自行维护的文本与 CSS/JavaScript 文件已通过格式检查；Courier Prime 附带的原始 OFL.txt 保持其 CRLF 行尾。主题切换脚本已通过 `node --check` 语法检查。
