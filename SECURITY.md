# 安全说明

## 不要提交这些

- `config.json`：内含云端 OCR Token（PaddleOCR-VL），已在 `.gitignore`
- `data/app.db`、`out/`：题库与判卷产物，含学生作答内容
- 真实答题卡照片、学生姓名/学校

## 数据流向

- 题库、卷子、判卷记录**全部在本机**（SQLite + 本地文件）
- 唯一的外部请求：填空/解答的裁切图会发到 PaddleOCR-VL 云端识别（https://paddleocr.aistudio-app.com）
  - 只发**答题格的裁切小图**，不含姓名、卷号以外的信息
  - 不想用云端：把 `config.json` 的 `ocr.backend` 改成 `manual`，全部走人工确认

## 局域网模式

`start_lan.bat` 会把服务绑到 `0.0.0.0`，同一 WiFi 的设备都能访问。此时**强制要求 4 位访问码**（自动生成、写进 `config.json` 的 `server.pin`，本机 127.0.0.1 免码）。
请只在自己的家庭/办公网络里开，不要在公共 WiFi 上开。

## 报告问题

发现安全问题时请不要开公开 Issue，直接邮件联系仓库作者。
