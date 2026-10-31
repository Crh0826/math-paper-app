# 启动与环境降级实测（t12）

由 `python app/docs/capture.py` 自动重放；截图见同目录 `08-*`、`09-*`。

| 场景 | 实测结果 |
|---|---|
| Word 缺失 | 预览页出现「卷面 PDF 未生成（本机未检测到 Word）」降级条；/pdf=409、/print.pdf=409 均为 409 友好页，/card.pdf=200 仍可下载 |
| DB 缺失 | GET / → 503 友好错误页（含「怎么办」与 build_db.py 提示，截图 09）；GET /healthz → ok=False、status=bad |
| 端口占用 | 8710 已被本实例占用，二次 bind 失败：[WinError 10048] 通常每个套接字地址(协议/网络地址/端口)只允许使用一次。；start.bat 退出时会提示「端口 8710 被占用，先关掉旧的黑色窗口」 |

> 端口占用：`start.bat` 会在窗口退出时打印三条常见原因并 `pause`；直接命令行启动会看到 uvicorn 的 `address already in use`。
