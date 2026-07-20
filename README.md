# 台股飆股篩選 → Telegram 每晚推播

每個交易日晚上自動掃描**全市場（上市 + 上櫃）**，跑「即時飆股篩選 = ALL」三策略，
把命中個股的 **K 線技術圖每 6 檔拼成一張大圖**，推到 Telegram。只推 Telegram（不需 LINE / 圖床）。

三策略（沿用桌面版 `MomentumScreener` 邏輯，命中多個策略各自獨立）：

- 🟡 帶量突破（多頭）— `check_stock(level="standard")`
- 📐 通道突破 — `check_channel_breakout()`
- 🛡️ 抗跌續強 — `check_rs_strong()`

## 檔案

| 檔案 | 作用 |
| --- | --- |
| `screener_core.py` | 選股核心（參數 / 全市場清單 / K 線抓取 / 三策略）。由桌面版原樣移植 |
| `charting.py` | K 線技術圖（headless）+ 6 格拼圖 |
| `notify_telegram.py` | Telegram 文字 / 圖片推播 |
| `run_screener.py` | 主流程：掃描 → 分策略 → 拼圖 → 推播 |
| `.github/workflows/screener.yml` | 每晚 21:00（台北）排程 + 手動觸發 |

## 部署（GitHub Actions，免主機）

1. 開一個新的 GitHub repo，把這些檔案 push 上去。
2. Repo → **Settings → Secrets and variables → Actions → New repository secret**，新增兩個：
   - `TELEGRAM_BOT_TOKEN`：向 BotFather 申請 bot 拿到的 token（可沿用你現有的那組）
   - `TELEGRAM_CHAT_ID`：要推播的對象（個人 / 群組 / 頻道）的 chat id
3. 進 **Actions** 分頁，若提示啟用 workflow 就啟用。
4. 想立刻測試：Actions → 「飆股篩選 Telegram 推播」→ **Run workflow**。

排程預設台北時間 **平日 21:00**。要改時間改 `screener.yml` 裡的 cron。

## 可調參數（workflow 的 env，或本機 export）

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| `MOMENTUM_PRESET` | `standard` | `conservative` / `standard` / `aggressive`，越寬命中越多 |
| `INCLUDE_OTC` | `1` | `1` 含上櫃、`0` 只上市 |
| `SCAN_WORKERS` | `10` | 併發抓取數；太高易被 yfinance 限流 |
| `CHART_PERIOD` | `1y` | 畫圖抓取期間 |
| `SEND_SLEEP` | `3` | 每張圖之間間隔秒數（避免 Telegram 限流）|
| `MAX_PER_STRATEGY` | `0` | 每策略最多推幾檔，`0` = 全推 |

## 本機執行

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=xxxx
export TELEGRAM_CHAT_ID=xxxx
python run_screener.py
```

## 注意事項

- **不做流動性預篩、全市場全掃**（依需求）。約 2000+ 檔逐檔抓 K 線，首次會較久（數分鐘～十幾分鐘），屬正常；workflow timeout 設 180 分鐘。
- 命中很多時圖會分多張（每 6 檔一張），並在每張之間 `SEND_SLEEP` 秒，避免 Telegram 限流。若某策略常常上百檔，可用 `MAX_PER_STRATEGY` 設上限。
- 與你原本的「處置股推播」專案**完全獨立**，互不影響。Telegram token 可共用同一組（各 repo 各自設 secret）。
- 全市場清單來源：證交所 `STOCK_DAY_ALL`（上市）+ TPEx openapi（上櫃）。TPEx 端點官方偶爾改版，若上櫃抓不到看 log 的 `[上櫃]` 錯誤訊息再調整。
