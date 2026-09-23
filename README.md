# CS2 每日扫盘

已确认 SteamDT K 线数组顺序：`[timestamp, open, close, high, low]`。平台使用 `YOUPIN`，`type=1`，`specialStyle` 为空。脚本排除 `knife_FN`，仅推送至少两个独立买点家族同时出现的信号，并以当前最新收盘价 30–13000 作为当日预筛；历史回测的严格入场价过滤仍需次日开盘确认。

## GitHub Secrets

- `STEAMDT_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

如果 API 返回 401/403，把 workflow 中 `STEAMDT_AUTH_MODE` 改成 `x-api-key`。

## 使用

把本目录内容复制到 GitHub 仓库根目录，提交并推送；在 Actions 页面手动运行一次 `CS2 daily scan` 测试，然后等待定时运行。
