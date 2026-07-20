# -*- coding: utf-8 -*-
"""
run_screener.py — 每晚一次: 全市場掃「即時飆股篩選 = ALL」三策略,
                  依策略把命中個股的 K 線技術圖每 6 檔拼成一張大圖, 推到 Telegram。

流程:
  1. 抓全市場清單 (上市 + 上櫃)
  2. 併發跑三策略 (standard 帶量突破 / channel 通道突破 / rs_strong 抗跌)
  3. 依策略分組
  4. 每組: 逐檔畫 K 線技術圖 → 每 6 張拼成一張大圖 → sendPhoto 給 Telegram

環境變數:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   (必填)
  MOMENTUM_PRESET   conservative/standard/aggressive  (預設 standard)
  INCLUDE_OTC       "1"=含上櫃(預設) / "0"=只上市
  SCAN_WORKERS      併發數 (預設 10)
  CHART_PERIOD      畫圖抓取期間 (預設 1y)
  SEND_SLEEP        每張圖之間的間隔秒數 (預設 3, 避免 Telegram 限流)
  MAX_PER_STRATEGY  每策略最多推幾檔 (預設 0 = 不限, 全推)
"""
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import screener_core as core
from screener_core import (StrategyParams, MarketUniverseFetcher,
                           StockDataFetcher, MomentumScreener)
import charting
import notify_telegram as tg


LEVEL_LABEL = {
    "standard":  "🟡 帶量突破 (多頭)",
    "channel":   "📐 通道突破",
    "rs_strong": "🛡️ 抗跌續強",
}
LEVEL_ORDER = ["standard", "channel", "rs_strong"]


def _env(k, d=""):
    return os.environ.get(k, d)


def _log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def scan_one(ticker, name, crash_dates, latest_crash) -> list:
    """對單一檔股票跑三策略, 回傳命中清單 (每筆含 level)。"""
    hits = []
    try:
        r1 = MomentumScreener.check_stock(ticker, name, level="standard")
        if r1:
            hits.append(r1)
    except Exception as e:
        print(f"[scan] {ticker} standard 例外: {e}")
    try:
        r2 = MomentumScreener.check_channel_breakout(ticker, name)
        if r2:
            hits.append(r2)
    except Exception as e:
        print(f"[scan] {ticker} channel 例外: {e}")
    try:
        if crash_dates:
            r3 = MomentumScreener.check_rs_strong(
                ticker, name, crash_dates, latest_crash)
            if r3:
                hits.append(r3)
    except Exception as e:
        print(f"[scan] {ticker} rs_strong 例外: {e}")
    return hits


def main():
    t0 = time.time()

    # ---- 參數 ----
    preset = _env("MOMENTUM_PRESET", "standard")
    StrategyParams.apply_preset(preset)
    include_otc = _env("INCLUDE_OTC", "1") != "0"
    workers = int(_env("SCAN_WORKERS", "10"))
    chart_period = _env("CHART_PERIOD", "1y")
    send_sleep = float(_env("SEND_SLEEP", "3"))
    max_per = int(_env("MAX_PER_STRATEGY", "0"))

    _log(f"啟動: preset={preset} 上櫃={'含' if include_otc else '不含'} "
         f"併發={workers}")

    # ---- 全市場清單 ----
    pool = MarketUniverseFetcher.get_universe(include_otc=include_otc)
    if not pool:
        tg.send_message("❌ 飆股篩選: 無法取得全市場清單 (證交所/TPEx 皆失敗且無快取)")
        _log("無清單, 中止")
        sys.exit(1)
    _log(f"全市場 {len(pool)} 檔, 開始掃描三策略...")

    # ---- 抗跌策略需要的大盤大跌日 ----
    crash_dates, latest_crash = MomentumScreener.get_market_crash_dates()
    if not crash_dates:
        _log("警告: 取不到大盤大跌日, 抗跌策略此次略過")

    # ---- 併發掃描 ----
    groups = {lv: [] for lv in LEVEL_ORDER}
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(scan_one, t, n, crash_dates, latest_crash): (t, n)
                for t, n in pool.items()}
        for fut in as_completed(futs):
            done += 1
            if done % 200 == 0:
                _log(f"  進度 {done}/{len(pool)}")
            for hit in fut.result():
                lv = hit.get("level")
                if lv in groups:
                    groups[lv].append(hit)

    total_hits = sum(len(v) for v in groups.values())
    _log(f"掃描完成: 命中 {total_hits} 筆 "
         f"(標準 {len(groups['standard'])} / 通道 {len(groups['channel'])} / "
         f"抗跌 {len(groups['rs_strong'])}), 耗時 {time.time()-t0:.0f}s")

    # ---- 摘要文字先推 ----
    today = datetime.now().strftime("%Y-%m-%d")
    summary = [f"📊 台股飆股篩選 (ALL)  {today}",
               f"掃描全市場 {len(pool)} 檔  preset={preset}",
               ""]
    for lv in LEVEL_ORDER:
        summary.append(f"{LEVEL_LABEL[lv]}: {len(groups[lv])} 檔")
    if total_hits == 0:
        summary.append("\n今日三策略皆無符合個股。")
    tg.send_message("\n".join(summary))

    if total_hits == 0:
        _log("無命中, 結束")
        return

    # ---- 依策略: 畫圖 + 每 6 檔拼一張大圖 + 推播 ----
    # 圖表用 matplotlib, 非執行緒安全 → 主執行緒逐檔畫
    for lv in LEVEL_ORDER:
        hits = groups[lv]
        if not hits:
            continue
        # 排序: standard 依成交量; channel 依突破幅度; rs_strong 依抗跌次數
        if lv == "standard":
            hits.sort(key=lambda h: h.get("volume", 0), reverse=True)
        elif lv == "channel":
            hits.sort(key=lambda h: h.get("extra", {}).get("r_squared", 0),
                      reverse=True)
        elif lv == "rs_strong":
            hits.sort(key=lambda h: h.get("extra", {}).get("rs_count", 0),
                      reverse=True)
        if max_per > 0:
            hits = hits[:max_per]

        _log(f"{LEVEL_LABEL[lv]}: 產圖 {len(hits)} 檔...")

        # 先把每檔畫成單張 PNG
        rendered = []  # (code_name, png_bytes)
        for h in hits:
            ticker, name = h["ticker"], h["name"]
            df = StockDataFetcher.fetch_history(ticker, period=chart_period)
            if df is None or df.empty:
                continue
            code = ticker.split(".")[0]
            title = f"{code} {name}  收{h.get('close','')}"
            extra = h.get("extra")  # channel/rs_strong 帶標記; standard 為 None
            png = charting.render_stock_png(df, title, extra=extra)
            if png:
                rendered.append((f"{code}{name}", png))

        if not rendered:
            continue

        # 每 6 張拼成一張大圖推出
        batches = list(charting.chunk(rendered, 6))
        for bi, batch in enumerate(batches, 1):
            big = charting.montage([p for _, p in batch], cols=2)
            if not big:
                continue
            names = "、".join(nm for nm, _ in batch)
            caption = (f"{LEVEL_LABEL[lv]}  ({today})  "
                       f"第 {bi}/{len(batches)} 張\n{names}")
            ok = tg.send_photo(big, caption=caption)
            _log(f"  推送 {lv} 第 {bi}/{len(batches)} 張: "
                 f"{'OK' if ok else '失敗'} ({len(batch)} 檔)")
            time.sleep(send_sleep)   # 避免 Telegram 限流

    _log(f"全部完成, 總耗時 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        err = f"❌ 飆股篩選程式異常: {e}"
        print(err)
        print(traceback.format_exc())
        try:
            tg.send_message(err)
        except Exception:
            pass
        sys.exit(1)
