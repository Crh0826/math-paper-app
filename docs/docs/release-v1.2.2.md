## 这一版只做两件事

### 1. 便携版和本地版共用一套配置与数据

便携版启动时自动找本机的开发版目录（往上最多 4 层，找同时含 `config.json` 与 `data/app.db` 的 `app/`），找到就直接用：

- 数据库 → `E:\OCR\app\data\app.db`（和网页版同一个库）
- 配置 → `E:\OCR\app\config.json`（同一份，含 OCR token）
- 题库 → `E:\OCR\qbank`

也可以显式指定 `MATH_APP_HOME`。换一台没有开发版目录的电脑时自动回落成包内副本，U 盘拷走照常能用。

**好处**：判卷记录、卷子、设置只有一份，不会再出现「exe 里判过的卷子在网页版看不到」。

### 2. 清理

删掉 6.9 GB 历史产物：旧判卷输出 5.2GB、PyInstaller 中间件 54MB、依赖缓存 101MB、旧安装包与重复 zip ~880MB、去重/计划等一次性产物。

## 验证

`数学题库工作台.exe --check` 实际输出：

```
版本     v1.2.2
题库     E:\OCR\qbank
数据库   E:\OCR\app\data\app.db
配置     E:\OCR\app\config.json
PDF 后端 word（Word COM）
结论     可以运行
```
