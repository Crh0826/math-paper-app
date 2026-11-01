## 修的是什么

设置页里「PaddleOCR-VL」和「视觉模型」各有一个 `name="model"` 输入框，保存时取到的是同一个字段：
把 DeepSeek 的 `deepseek-flash` 填进视觉模型那一栏，保存却写进了 PaddleOCR-VL 的 model，
于是云端一直返回「没有可用的块」——表现就是「识别服务暂时无响应、判 0 分」。

现在字段名彻底分开（`pv_model` / `vlm_model` 等），物理上不可能串；
模型名护栏只校验 PaddleOCR-VL 那一栏，并且会提示「用 DeepSeek 请把上面的后端切到视觉模型再填」。

## 实测

| 操作 | 结果 |
|---|---|
| paddle_vl 存 `deepseek-flash` | HTTP 400，配置不变（仍是 PaddleOCR-VL-1.6） |
| vlm 存 `deepseek-flash` | 保存成功，paddle 的 model 不受影响 |
| paddle 存 `PaddleOCR-VL-1.7` | 保存成功，vlm 的 model 不受影响 |
| 页面里重复的 `name="model"` / `name="token"` | 0 个 |

`tests.test_settings` 24 条 OK。

## 顺带

- DeepSeek 视觉链路的另外两个坑上一版已修：`base_url` 会拼成 `/v1/v1/...`（404）、
  `max_tokens=128` 被思考模式吃光（正文空）、小作答框需放大 2 倍
- 便携版与本地版共用同一份 `app/data/app.db` 与 `app/config.json`
