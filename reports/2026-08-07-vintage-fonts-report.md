# 2026-08-07 复古纸页多语言字体更新报告

## 概览

本次更新为复古纸页皮肤建立中、日、英三套字体分工。未修改数据库、`data/`、作品元数据、上传数据、解析逻辑或后端路由；夜色皮肤保持原有样式。

## 日文阅读字体

- 新增 `static/fonts/HinaMincho-Regular.ttf`，并在 `static/vintage-paper.css` 注册为 `Hina Mincho`。
- 阅读器根容器使用已有 `edition['language']` 输出 `data-language` 属性。
- 仅在 `.theme-vintage-paper .reader-layout[data-language="ja"]` 范围内，将日文 edition 的正文、章节标题和左侧章节目录设为 Hina Mincho。
- 日文阅读器的作品中文标题、导航与其他通用界面文字不进入该专用规则。

## 中文、英文与数字字体

- 新增 `static/fonts/JingHuaLaoSong-Regular.ttf`，并注册为 `JingHua LaoSong`。
- 复古纸页的最终通用字体栈为：

  ```css
  "JingHua LaoSong", "Courier Prime", "Noto Serif CJK SC", "Songti SC", serif
  ```

- 因此复古皮肤中的中文、英文与数字均优先尝试京华老宋体；Courier Prime 作为后续英文/数字回退。
- `button`、`input`、`select` 与 `textarea` 显式使用该通用栈，避免浏览器原生控件不继承页面字体。
- 年份和英文阅读正文同样引用该通用栈；未改变字号、字重、行距、字间距、颜色或布局。
- Hina Mincho 的日文阅读器选择器更具体，且位于通用规则之后，继续优先于京华老宋体。

## 验证

- 执行：`.venv/bin/python -m pytest`
- 结果：89 项测试全部通过。
- 本地站点已验证以下字体请求均返回 HTTP 200：
  - `JingHuaLaoSong-Regular.ttf`
  - `HinaMincho-Regular.ttf`
  - `CourierPrime-Regular.ttf`

## 版本记录

- `b85aa29 feat: add JingHua LaoSong vintage font`
- `06ce16c 为复古皮肤配置中英日字体`
- 分支 `feature/vintage-theme-and-fonts` 已推送至 `origin`。
