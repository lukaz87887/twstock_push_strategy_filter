# -*- coding: utf-8 -*-
"""
screener_core.py — 台股飆股選股核心 (headless, 無 GUI)

由桌面版 tw_stock_app 原樣移植:
  • StrategyParams        參數 (含 conservative/standard/aggressive 三組 preset)
  • MarketUniverseFetcher 全市場清單 (上市 .TW + 上櫃 .TWO)
  • StockDataFetcher      yfinance K 線抓取 (記憶體快取, 跨日失效)
  • MomentumScreener      三策略: check_stock / check_channel_breakout / check_rs_strong

「即時飆股篩選 = ALL」等同於同時跑:
  1. check_stock(level="standard")   帶量突破多頭
  2. check_channel_breakout()        下降通道突破
  3. check_rs_strong()               大盤大跌抗跌
一檔命中多個策略時, 各自獨立記錄。
"""
import os
import json
import time
import platform
from datetime import datetime, timedelta

import random
import requests
import numpy as np
import pandas as pd
from scipy.stats import linregress
import yfinance as yf


# ------------------------------------------------------------------
#   yfinance 抗限流層
#   1) 用 curl_cffi 瀏覽器指紋 session, 大幅降低 Yahoo 429
#   2) 帶指數退避重試的 history() 包裝
# ------------------------------------------------------------------
try:
    from curl_cffi import requests as _cffi_requests
    _YF_SESSION = _cffi_requests.Session(impersonate="chrome")
    print("[INFO] 已啟用 curl_cffi 瀏覽器指紋 session (降低 yfinance 限流)")
except Exception as _e:  # 沒裝 curl_cffi 也能跑, 只是較易被限流
    _YF_SESSION = None
    print(f"[WARN] curl_cffi 不可用, 改用預設連線: {_e}")


def _yf_ticker(ticker: str):
    if _YF_SESSION is not None:
        try:
            return yf.Ticker(ticker, session=_YF_SESSION)
        except TypeError:      # 舊版 yfinance 不吃 session 參數
            return yf.Ticker(ticker)
    return yf.Ticker(ticker)


def _history_retry(ticker: str, retries: int = 5, base_sleep: float = 2.0,
                   **kwargs) -> pd.DataFrame:
    """帶重試/指數退避的 history(); 專門對付 'Too Many Requests'。"""
    last_exc = None
    for attempt in range(retries):
        try:
            return _yf_ticker(ticker).history(**kwargs)
        except Exception as e:
            last_exc = e
            msg = str(e).lower()
            if ("too many requests" in msg or "rate limit" in msg
                    or "429" in msg):
                time.sleep(base_sleep * (2 ** attempt) + random.uniform(0, 1.5))
                continue
            raise
    if last_exc:
        raise last_exc
    return pd.DataFrame()


class StrategyParams:
    """
    全域參數管理器 (Singleton 模式)。
    所有篩選器讀同一份參數, 在「參數設定」分頁修改即可全部生效。

    使用方式:
        params = StrategyParams.get()
        params["vol_multiplier"]          # 讀取
        StrategyParams.set("vol_multiplier", 2.5)  # 修改
        StrategyParams.apply_preset("conservative")  # 套用預設組合
    """

    # ====== 預設參數 (對應 v7 原始邏輯) ======
    _defaults = {
        # ---- 共用 / 量能類 ----
        "vol_multiplier":         2.0,        # 量能放大倍數 (突破日)
        "min_vol_lots":           1000,       # 最小 20 日均量 (張)
        "channel_vol_multiplier": 1.5,        # 通道突破量門檻

        # ---- 突破期間類 (Basic / Standard / Strict) ----
        "breakout_window":        20,         # 新高回看天數
        "short_ma":               5,
        "mid_ma":                 20,
        "long_ma":                60,

        # ---- 動能類 (Standard) ----
        "rsi_min":                50,
        "rsi_max":                75,
        "require_macd_positive":  True,
        "max_extension_pct":      25,         # 離 50MA 最大延伸 (%)

        # ---- 通道型態類 (Channel Breakout) ----
        "channel_lookback":       120,        # 旗桿頂端搜尋範圍
        "channel_min_len":        30,         # 通道整理最短長度
        "channel_min_r2":         0.3,        # 線性擬合 R²
        "channel_min_slope":      -0.02,      # 斜率必須小於

        # ---- 抗跌類 (RS Strong) ----
        "rs_lookback":            90,         # 大盤回看天數
        "market_drop_threshold":  -0.02,      # 大盤大跌門檻
        "rs_min_count":           3,          # 最少抗跌次數
        "rs_max_count":           15,         # 最多抗跌次數

        # ---- 長線趨勢類 (Strict / Minervini) ----
        "high_52w_min_pct":       0.75,       # 收盤需 ≥ 52 週高 × N
        "low_52w_min_mult":       1.30,       # 收盤需 ≥ 52 週低 × N
        "rs_rating_min":          70,         # RS Rating 門檻
    }

    # ====== 預設組合 (Preset Bundles) ======
    _presets = {
        "conservative": {
            # 🛡️ 保守: 嚴格門檻, 訊號少但品質高
            "vol_multiplier":         2.5,
            "min_vol_lots":           2000,
            "channel_vol_multiplier": 2.0,
            "breakout_window":        30,
            "rsi_min":                55,
            "rsi_max":                70,
            "require_macd_positive":  True,
            "max_extension_pct":      15,
            "channel_min_len":        40,
            "channel_min_r2":         0.4,
            "channel_min_slope":      -0.03,
            "market_drop_threshold":  -0.025,
            "rs_min_count":           5,
            "high_52w_min_pct":       0.85,
            "low_52w_min_mult":       1.40,
            "rs_rating_min":          80,
        },
        "standard": {
            # ⚖️ 標準: v7 預設值 (平衡)
            # 全部用 _defaults
        },
        "aggressive": {
            # 🔥 寬鬆: 訊號多, 適合廣撒網
            "vol_multiplier":         1.5,
            "min_vol_lots":           500,
            "channel_vol_multiplier": 1.2,
            "breakout_window":        15,
            "rsi_min":                45,
            "rsi_max":                85,
            "require_macd_positive":  False,
            "max_extension_pct":      35,
            "channel_min_len":        20,
            "channel_min_r2":         0.2,
            "channel_min_slope":      -0.01,
            "market_drop_threshold":  -0.015,
            "rs_min_count":           2,
            "high_52w_min_pct":       0.65,
            "low_52w_min_mult":       1.20,
            "rs_rating_min":          60,
        },
    }

    _current: dict = None  # 當前生效參數
    _observers: list = []  # 變更時通知的回呼函式

    @classmethod
    def get(cls) -> dict:
        """取得當前生效的參數字典 (回傳 copy 避免外部誤改)"""
        if cls._current is None:
            cls._current = cls._defaults.copy()
        return cls._current.copy()

    @classmethod
    def get_value(cls, key: str, default=None):
        """取得單一參數值"""
        if cls._current is None:
            cls._current = cls._defaults.copy()
        return cls._current.get(key, default)

    @classmethod
    def set(cls, key: str, value):
        """設定單一參數"""
        if cls._current is None:
            cls._current = cls._defaults.copy()
        cls._current[key] = value
        cls._notify()

    @classmethod
    def set_batch(cls, updates: dict):
        """批次更新多個參數"""
        if cls._current is None:
            cls._current = cls._defaults.copy()
        cls._current.update(updates)
        cls._notify()

    @classmethod
    def reset(cls):
        """還原到 v7 預設值"""
        cls._current = cls._defaults.copy()
        cls._notify()

    @classmethod
    def apply_preset(cls, name: str):
        """套用預設組合 (conservative / standard / aggressive)"""
        if cls._current is None:
            cls._current = cls._defaults.copy()
        # 先 reset 再 overlay 預設組合 (避免遺漏的 key 殘留前次設定)
        cls._current = cls._defaults.copy()
        if name in cls._presets:
            cls._current.update(cls._presets[name])
        cls._notify()

    @classmethod
    def register_observer(cls, callback):
        """註冊變更通知 (UI 即時刷新用)"""
        if callback not in cls._observers:
            cls._observers.append(callback)

    @classmethod
    def _notify(cls):
        for cb in cls._observers:
            try:
                cb()
            except Exception as e:
                print(f"[WARN] params observer 錯誤: {e}")

    @classmethod
    def export_summary(cls) -> str:
        """輸出當前參數摘要 (用於回測報告)"""
        p = cls.get()
        return (f"量倍={p['vol_multiplier']}× "
                f"均量≥{p['min_vol_lots']}張 "
                f"突破={p['breakout_window']}日 "
                f"RSI={p['rsi_min']}-{p['rsi_max']} "
                f"延伸≤{p['max_extension_pct']}% "
                f"通道R²≥{p['channel_min_r2']} "
                f"抗跌≥{p['rs_min_count']}次")


class MarketUniverseFetcher:
    """
    動態抓「全市場」普通股清單 (不再使用任何寫死的股票池)。

    來源:
      • 上市: 證交所 STOCK_DAY_ALL (單一請求, 全上市當日行情)  → 後綴 .TW
      • 上櫃: TPEx openapi 每日收盤行情 (全上櫃當日行情)        → 後綴 .TWO

    過濾:
      • 只保留普通股: 代號 4 碼數字、且非 "00" 開頭
        (排除 ETF / 權證 / 受益證券 / 存託憑證等)

    回傳:
      {ticker: name}, ticker 已含 yfinance 後綴 (例: "2330.TW", "6488.TWO")

    快取 / 容錯:
      • 記憶體 + 磁碟快取, 每日一檔 (~/.tw_stock_universe_cache/YYYYMMDD.json)
      • 跨日自動失效重抓
      • 抓取失敗時 fallback 到「上次成功的清單」(last_good.json),
        連 last_good 都沒有才回傳空 (呼叫端據此中止並提示, 不會偷掃小清單)
    """
    TWSE_OPENAPI_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    TWSE_URL = "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL"
    TPEX_URL = ("https://www.tpex.org.tw/openapi/v1/"
                "tpex_mainboard_daily_close_quotes")
    CACHE_DIR = os.path.join(os.path.expanduser("~"),
                             ".tw_stock_universe_cache")
    TIMEOUT = 30
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'zh-TW,zh;q=0.9',
    }

    _mem_cache = None        # (date_str, {ticker: name})
    _last_error = ""

    # ---------- 對外主入口 ----------
    @classmethod
    def get_universe(cls, force_refresh: bool = False,
                     include_otc: bool = True) -> dict[str, str]:
        today = datetime.now().strftime("%Y%m%d")

        # 1) 記憶體快取
        if (not force_refresh and cls._mem_cache
                and cls._mem_cache[0] == today):
            return dict(cls._mem_cache[1])

        # 2) 磁碟快取 (當日)
        cls._ensure_dir()
        cache_file = os.path.join(cls.CACHE_DIR, f"{today}.json")
        if not force_refresh and os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    pool = json.load(f)
                if pool:
                    cls._mem_cache = (today, pool)
                    return dict(pool)
            except Exception:
                pass

        # 3) 實際抓取
        cls._last_error = ""
        pool: dict[str, str] = {}
        n_twse = n_tpex = 0
        try:
            twse = cls._fetch_twse()            # 上市
            n_twse = len(twse)
            pool.update(twse)
        except Exception as e:
            cls._last_error += f"[上市] {e}  "
            print(f"[WARN] 上市清單抓取失敗: {e}")
        if include_otc:
            try:
                tpex = cls._fetch_tpex()        # 上櫃
                n_tpex = len(tpex)
                pool.update(tpex)
            except Exception as e:
                cls._last_error += f"[上櫃] {e}  "
                print(f"[WARN] 上櫃清單抓取失敗: {e}")
        print(f"[INFO] 清單來源: 上市 {n_twse} 檔, 上櫃 {n_tpex} 檔")

        # 4) 成功 → 存快取 + last_good
        if pool:
            try:
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(pool, f, ensure_ascii=False)
                with open(os.path.join(cls.CACHE_DIR, "last_good.json"),
                          "w", encoding="utf-8") as f:
                    json.dump(pool, f, ensure_ascii=False)
            except Exception as e:
                print(f"[WARN] 全市場清單快取寫入失敗: {e}")
            cls._mem_cache = (today, pool)
            print(f"[INFO] 全市場清單: 上市+上櫃共 {len(pool)} 檔")
            return dict(pool)

        # 5) 抓取失敗 → fallback 上次成功清單
        last_good = cls._load_last_good()
        if last_good:
            print(f"[WARN] 全市場清單抓取失敗, 沿用上次成功清單 "
                  f"({len(last_good)} 檔). 原因: {cls._last_error}")
            cls._mem_cache = (today, last_good)
            return dict(last_good)

        print(f"[ERROR] 全市場清單抓取失敗且無快取可用: {cls._last_error}")
        return {}

    @classmethod
    def get_last_error(cls) -> str:
        return cls._last_error

    # ---------- 工具 ----------
    @classmethod
    def _ensure_dir(cls):
        try:
            os.makedirs(cls.CACHE_DIR, exist_ok=True)
        except Exception as e:
            print(f"[WARN] 建立全市場清單快取目錄失敗: {e}")

    @classmethod
    def _load_last_good(cls) -> dict[str, str]:
        path = os.path.join(cls.CACHE_DIR, "last_good.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    @staticmethod
    def _is_common_stock(code: str) -> bool:
        """普通股: 4 碼數字且非 00 開頭 (排除 ETF/權證/受益證券)"""
        code = str(code).strip()
        return len(code) == 4 and code.isdigit() and not code.startswith("00")

    @staticmethod
    def _find_idx(fields: list, *keywords) -> int | None:
        """在欄位名列表中找出第一個含任一關鍵字的索引 (容錯不同版本)"""
        for i, f in enumerate(fields):
            s = str(f)
            if any(k in s for k in keywords):
                return i
        return None

    # ---------- 上市 (證交所 STOCK_DAY_ALL) ----------
    @classmethod
    def _fetch_twse(cls) -> dict[str, str]:
        # 先試 OpenAPI (list of dict, 較適合自動化)
        try:
            pool = cls._fetch_twse_openapi()
            if pool:
                return pool
        except Exception as e:
            print(f"[WARN] 上市 OpenAPI 失敗, 改試舊端點: {e}")
        # 退回 www STOCK_DAY_ALL
        r = requests.get(cls.TWSE_URL, params={"response": "json"},
                         headers=cls.HEADERS, timeout=cls.TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        payload = r.json()

        # 相容兩種格式: 舊版 {fields, data} / 新版 {tables:[{fields,data}]}
        fields, rows = None, None
        if isinstance(payload.get("data"), list) and payload.get("fields"):
            fields, rows = payload["fields"], payload["data"]
        else:
            for t in payload.get("tables", []):
                if t.get("data") and t.get("fields"):
                    fields, rows = t["fields"], t["data"]
                    break
        if not rows:
            raise RuntimeError(f"無資料表 (stat={payload.get('stat','?')})")

        ci = cls._find_idx(fields, "證券代號", "股票代號", "代號")
        ni = cls._find_idx(fields, "證券名稱", "股票名稱", "名稱")
        if ci is None or ni is None:
            raise RuntimeError(f"找不到代號/名稱欄位: {fields}")

        pool = {}
        for row in rows:
            try:
                code = str(row[ci]).strip()
                name = str(row[ni]).strip()
            except (IndexError, TypeError):
                continue
            if cls._is_common_stock(code):
                pool[f"{code}.TW"] = name
        if not pool:
            raise RuntimeError("解析後 0 檔 (格式可能已變動)")
        return pool

    @classmethod
    def _fetch_twse_openapi(cls) -> dict[str, str]:
        """上市: TWSE OpenAPI (回傳 list of dict, 每筆含 Code/Name)。"""
        r = requests.get(cls.TWSE_OPENAPI_URL, headers=cls.HEADERS,
                         timeout=cls.TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        data = r.json()
        if not isinstance(data, list) or not data:
            raise RuntimeError("OpenAPI 回應非預期 (空或非列表)")
        code_key = cls._find_key(data[0], "Code", "代號")
        name_key = cls._find_key(data[0], "Name", "名稱")
        if code_key is None or name_key is None:
            raise RuntimeError(f"找不到代號/名稱鍵: {list(data[0].keys())}")
        pool = {}
        for item in data:
            code = str(item.get(code_key, "")).strip()
            name = str(item.get(name_key, "")).strip()
            if cls._is_common_stock(code):
                pool[f"{code}.TW"] = name
        return pool

    # ---------- 上櫃 (TPEx openapi) ----------
    @classmethod
    def _fetch_tpex(cls) -> dict[str, str]:
        r = requests.get(cls.TPEX_URL, headers=cls.HEADERS,
                         timeout=cls.TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        data = r.json()
        if not isinstance(data, list) or not data:
            raise RuntimeError("openapi 回應非預期 (空或非列表)")

        # openapi 每筆是 dict; 欄位名可能因版本不同, 動態偵測含 code/name 的鍵
        sample = data[0]
        code_key = cls._find_key(sample, "SecuritiesCompanyCode", "Code",
                                 "代號")
        name_key = cls._find_key(sample, "CompanyName", "Name", "名稱")
        if code_key is None or name_key is None:
            raise RuntimeError(f"找不到代號/名稱鍵: {list(sample.keys())}")

        pool = {}
        for item in data:
            code = str(item.get(code_key, "")).strip()
            name = str(item.get(name_key, "")).strip()
            if cls._is_common_stock(code):
                pool[f"{code}.TWO"] = name
        if not pool:
            raise RuntimeError("解析後 0 檔 (格式可能已變動)")
        return pool

    @staticmethod
    def _find_key(sample: dict, *keywords):
        for k in sample.keys():
            if any(kw.lower() in str(k).lower() for kw in keywords):
                return k
        return None


class StockDataFetcher:
    """
    封裝 yfinance 資料抓取。
    內建記憶體快取: 同一支股票同一抓取期間不重複下載, 大幅加速回測。
    未來如要換 FinMind 只需改這個類別內部, 對外介面不變。

    ★ v9.1 修正:
      1. period 模式改為「明確 start/end 日期」確保抓到當天最新 K 棒
      2. 快取 key 加入「日期戳」, 跨日自動失效, 隔天會重抓
      3. 加上 prepost=False (僅看正規盤) 避免盤中試撮造成異常值
    """
    _cache: dict[str, pd.DataFrame] = {}

    # 期間字串 → 天數的對照表 (用來轉成明確日期)
    _PERIOD_DAYS = {
        "1mo": 35, "3mo": 100, "6mo": 190, "9mo": 285,
        "1y": 380, "2y": 760, "5y": 1900, "10y": 3800,
    }

    @classmethod
    def fetch_history(cls, ticker: str, period: str = "6mo",
                      force_refresh: bool = False) -> pd.DataFrame:
        """
        以相對期間抓取歷史資料 (例: 6mo, 1y)。

        ★ v9.1: 用「明確 end 日期 = 明天」確保涵蓋今天的最新 K 棒,
                並用「當天日期」當快取 key 的一部分, 跨日自動失效。
        """
        # 快取 key 加入今天日期, 跨日後會自動產生新 key
        today_str = datetime.now().strftime("%Y%m%d")
        cache_key = f"{ticker}::period::{period}::{today_str}"

        if not force_refresh and cache_key in cls._cache:
            return cls._cache[cache_key].copy()

        try:
            # 用明確日期取代 period 字串
            days = cls._PERIOD_DAYS.get(period, 190)  # 預設 6mo
            end_dt = datetime.now() + timedelta(days=1)    # 明天 = 確保涵蓋今天
            start_dt = end_dt - timedelta(days=days)

            df = _history_retry(
                ticker,
                start=start_dt.strftime("%Y-%m-%d"),
                end=end_dt.strftime("%Y-%m-%d"),
                auto_adjust=False,
                prepost=False,        # 只看正規盤
                actions=False,        # 不需要股利/分割欄位
            )
            if df.empty:
                return pd.DataFrame()
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
            cls._cache[cache_key] = df.copy()
            return df
        except Exception as e:
            print(f"[ERROR] 抓取 {ticker} 失敗: {e}")
            return pd.DataFrame()

    @classmethod
    def fetch_range(cls, ticker: str, start: str, end: str) -> pd.DataFrame:
        """
        以明確起訖日抓取資料。
        為了讓回測時能正確計算 60MA, 自動往前多抓 100 個日曆日。
        """
        # 跨日仍會重新抓 (因為 end 若是今天會多抓到當天最新 K)
        today_str = datetime.now().strftime("%Y%m%d")
        cache_key = f"{ticker}::range::{start}::{end}::{today_str}"
        if cache_key in cls._cache:
            return cls._cache[cache_key].copy()
        try:
            start_dt = pd.to_datetime(start) - timedelta(days=100)
            end_dt   = pd.to_datetime(end) + timedelta(days=2)
            df = _history_retry(
                ticker,
                start=start_dt.strftime("%Y-%m-%d"),
                end=end_dt.strftime("%Y-%m-%d"),
                auto_adjust=False,
                prepost=False,
                actions=False,
            )
            if df.empty:
                return pd.DataFrame()
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
            cls._cache[cache_key] = df.copy()
            return df
        except Exception as e:
            print(f"[ERROR] 抓取 {ticker} 區間失敗: {e}")
            return pd.DataFrame()

    @classmethod
    def clear_cache(cls):
        """手動清空快取 (例如使用者按「強制重新整理」按鈕)"""
        cls._cache.clear()




class MomentumScreener:
    """
    三層級進場條件 (entry_level 參數控制嚴格度):

    ┌─────────────┬──────────────────────────────────────────────┐
    │  Basic      │  1. 多頭排列: 收盤 > 5MA > 20MA > 60MA       │
    │             │  2. 帶量突破: 收盤創 20 日新高, 量 ≥ 2× 均量 │
    │             │  3. 流動性 : 20 日均量 ≥ 1,000 張            │
    ├─────────────┼──────────────────────────────────────────────┤
    │  Standard   │  Basic 全部 +                                 │
    │             │  4. RSI(14) 介於 50 ~ 75 (有動能但不超買)    │
    │             │  5. MACD 柱狀體 > 0 (動能向上)               │
    │             │  6. 收盤離 50MA 不可超過 +25% (避免追高)     │
    │             │  7. 突破日量增, 過去 5 日的下跌日量縮         │
    ├─────────────┼──────────────────────────────────────────────┤
    │  Strict     │  Standard 全部 +                              │
    │             │  Minervini Trend Template 8 條件:             │
    │             │  8. 收盤 > 150MA 且 > 200MA                   │
    │             │  9. 150MA > 200MA                            │
    │             │ 10. 200MA 向上趨勢 (近 30 日斜率為正)         │
    │             │ 11. 50MA > 150MA > 200MA                     │
    │             │ 12. 收盤 ≥ 52 週低點 × 1.3 (至少漲 30%)      │
    │             │ 13. 收盤 ≥ 52 週高點 × 0.75 (在新高附近)     │
    │             │ 14. RS Rating ≥ 70 (相對大盤 6 個月強勢)     │
    └─────────────┴──────────────────────────────────────────────┘
    """
    SHORT_MA = 5
    MID_MA = 20
    LONG_MA = 60
    BREAKOUT_WINDOW = 20
    VOLUME_MULTIPLIER = 2.0
    MIN_AVG_VOLUME_SHARES = 1_000 * 1_000  # 1,000 張

    # --------- 計算所有技術指標 (一次到位, 給三個等級共用) ---------
    @classmethod
    def compute_indicators(cls, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        # Basic 需要
        out["MA5"]   = out["Close"].rolling(cls.SHORT_MA).mean()
        out["MA20"]  = out["Close"].rolling(cls.MID_MA).mean()
        out["MA60"]  = out["Close"].rolling(cls.LONG_MA).mean()
        out["VolMA20"]    = out["Volume"].rolling(cls.MID_MA).mean()
        out["VolMA5"]     = out["Volume"].rolling(5).mean()
        out["High20Prev"] = out["High"].rolling(cls.BREAKOUT_WINDOW).max().shift(1)

        # Standard 需要
        out["RSI14"] = cls._calc_rsi(out["Close"], 14)
        macd, signal, hist = cls._calc_macd(out["Close"])
        out["MACD"], out["MACDsig"], out["MACDhist"] = macd, signal, hist
        out["MA50"]  = out["Close"].rolling(50).mean()

        # Strict 需要
        out["MA150"] = out["Close"].rolling(150).mean()
        out["MA200"] = out["Close"].rolling(200).mean()
        # 200MA 30 日前的值, 用來判斷斜率
        out["MA200_30dAgo"] = out["MA200"].shift(30)
        # 52 週 (約 252 個交易日) 高低
        out["High52w"] = out["High"].rolling(252, min_periods=60).max()
        out["Low52w"]  = out["Low"].rolling(252, min_periods=60).min()

        return out

    # --------- RSI ----------
    @staticmethod
    def _calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1.0/period, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1.0/period, adjust=False).mean()
        rs = gain / loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    # --------- MACD ----------
    @staticmethod
    def _calc_macd(close: pd.Series, fast: int = 12, slow: int = 26,
                   signal: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        macd = ema_fast - ema_slow
        sig  = macd.ewm(span=signal, adjust=False).mean()
        hist = macd - sig
        return macd, sig, hist

    # =====================================================
    #   三個等級的條件判斷 (對 DataFrame 一個 row)
    # =====================================================
    @classmethod
    def _check_basic(cls, row: pd.Series) -> bool:
        """v1 原始條件 (參數從 StrategyParams 讀取)"""
        try:
            close, vol = row["Close"], row["Volume"]
            ma5, ma20, ma60 = row["MA5"], row["MA20"], row["MA60"]
            vol_ma20, high20_prev = row["VolMA20"], row["High20Prev"]
        except KeyError:
            return False
        if pd.isna([ma5, ma20, ma60, vol_ma20, high20_prev]).any():
            return False

        # ★ v8: 從 StrategyParams 讀取
        p = StrategyParams.get()
        vol_mult     = p["vol_multiplier"]
        min_vol_shr  = p["min_vol_lots"] * 1000

        return (
            close > ma5 > ma20 > ma60
            and close > high20_prev
            and vol > vol_ma20 * vol_mult
            and vol_ma20 >= min_vol_shr
        )

    @classmethod
    def _check_standard(cls, row: pd.Series) -> bool:
        """Standard = Basic + 動能 + 不追高 (參數可調)"""
        if not cls._check_basic(row):
            return False
        try:
            close, ma50 = row["Close"], row["MA50"]
            rsi, macd_hist = row["RSI14"], row["MACDhist"]
        except KeyError:
            return False
        if pd.isna([ma50, rsi, macd_hist]).any():
            return False

        # ★ v8: 從 StrategyParams 讀取
        p = StrategyParams.get()

        # 4. RSI 介於設定區間 (預設 50-75)
        if not (p["rsi_min"] <= rsi <= p["rsi_max"]):
            return False
        # 5. MACD 柱狀體 (可選關閉)
        if p["require_macd_positive"] and macd_hist <= 0:
            return False
        # 6. 不可離 50MA 超過設定百分比 (預設 25%)
        extension = (close - ma50) / ma50
        if extension > p["max_extension_pct"] / 100:
            return False
        return True

    @classmethod
    def _check_strict(cls, row: pd.Series, rs_rating: float = None) -> bool:
        """Strict = Standard + Minervini Trend Template 8 條件"""
        if not cls._check_standard(row):
            return False
        try:
            close = row["Close"]
            ma50, ma150, ma200 = row["MA50"], row["MA150"], row["MA200"]
            ma200_30d = row["MA200_30dAgo"]
            high52, low52 = row["High52w"], row["Low52w"]
        except KeyError:
            return False
        if pd.isna([ma150, ma200, ma200_30d, high52, low52]).any():
            return False

        # ★ v8: 從 StrategyParams 讀取
        p = StrategyParams.get()

        # 8. 收盤 > 150MA 且 > 200MA
        if not (close > ma150 and close > ma200):
            return False
        # 9. 150MA > 200MA
        if not (ma150 > ma200):
            return False
        # 10. 200MA 向上 (今日值 > 30 日前的值)
        if not (ma200 > ma200_30d):
            return False
        # 11. 50MA > 150MA > 200MA (完美多頭排列)
        if not (ma50 > ma150 > ma200):
            return False
        # 12. 收盤 ≥ 52 週低點 × N (預設 1.3, 可調)
        if not (close >= low52 * p["low_52w_min_mult"]):
            return False
        # 13. 收盤在 52 週高點 N% 之上 (預設 75%, 可調)
        if not (close >= high52 * p["high_52w_min_pct"]):
            return False
        # 14. RS Rating ≥ N (預設 70, 可調)
        if rs_rating is not None and rs_rating < p["rs_rating_min"]:
            return False
        return True

    # =====================================================
    #   公用 row_passes (向下相容 + 等級分派)
    # =====================================================
    @classmethod
    def _row_passes(cls, row: pd.Series, level: str = "basic",
                    rs_rating: float = None) -> bool:
        if level == "basic":
            return cls._check_basic(row)
        if level == "standard":
            return cls._check_standard(row)
        if level == "strict":
            return cls._check_strict(row, rs_rating)
        return cls._check_basic(row)

    # =====================================================
    #   即時模式: 檢查最新交易日 (給分頁一用)
    # =====================================================
    @classmethod
    def check_stock(cls, ticker: str, name: str,
                    level: str = "basic",
                    benchmark_df: pd.DataFrame = None) -> dict | None:
        """檢查單檔股票最新一個交易日是否符合飆股條件"""
        df = StockDataFetcher.fetch_history(ticker, period="2y" if level == "strict" else "6mo")
        min_required = 250 if level == "strict" else cls.LONG_MA + 5
        if df.empty or len(df) < min_required:
            return None

        df = cls.compute_indicators(df)
        last = df.iloc[-1]

        # 計算 RS Rating (相對於 benchmark)
        rs_rating = None
        if level == "strict" and benchmark_df is not None:
            rs_rating = cls.calc_rs_rating(df, benchmark_df)

        if cls._row_passes(last, level, rs_rating) and cls.check_chip_concentration(ticker):
            return {
                "ticker":   ticker,
                "name":     name,
                "close":    round(float(last["Close"]), 2),
                "volume":   int(last["Volume"]),
                "vol_ma20": int(last["VolMA20"]),
                "ma5":      round(float(last["MA5"]), 2),
                "ma20":     round(float(last["MA20"]), 2),
                "ma60":     round(float(last["MA60"]), 2),
                "rsi":      round(float(last.get("RSI14", 0)), 1),
                "rs_rating": round(rs_rating, 1) if rs_rating else None,
                "level":    level,
            }
        return None

    # =====================================================
    #   RS Rating 計算 (簡化版 Minervini 風格)
    #   公式: 標的 6 個月報酬 vs 大盤 6 個月報酬 的百分位
    # =====================================================
    @staticmethod
    def calc_rs_rating(df: pd.DataFrame, benchmark_df: pd.DataFrame,
                       lookback: int = 126) -> float:
        """
        計算 Relative Strength Rating (0~100)。
        簡化作法: 標的近 6 個月報酬 - 大盤近 6 個月報酬,
        然後映射為 0~100 分。
        > 70 表示明顯強過大盤。
        """
        if len(df) < lookback + 1 or len(benchmark_df) < lookback + 1:
            return 50.0  # 資料不足時給中性分

        try:
            stock_ret = df["Close"].iloc[-1] / df["Close"].iloc[-lookback] - 1
            bench_ret = benchmark_df["Close"].iloc[-1] / benchmark_df["Close"].iloc[-lookback] - 1
        except (IndexError, KeyError):
            return 50.0

        diff = stock_ret - bench_ret
        # 經驗映射: diff > 30% → 90 分, diff = 0 → 50 分, diff < -30% → 10 分
        score = 50 + diff * 100
        return float(max(0, min(100, score)))

    # =====================================================
    #   回測模式: 找出區間內所有觸發訊號 (給分頁二用)
    # =====================================================
    @classmethod
    def find_signals_in_range(cls, ticker: str, name: str,
                              start_date: str, end_date: str,
                              level: str = "basic",
                              benchmark_df: pd.DataFrame = None) -> list[dict]:
        df = StockDataFetcher.fetch_range(ticker, start_date, end_date)
        if df.empty or len(df) < cls.LONG_MA + 5:
            return []

        df = cls.compute_indicators(df)

        start_ts = pd.to_datetime(start_date)
        end_ts   = pd.to_datetime(end_date)
        mask = (df.index >= start_ts) & (df.index <= end_ts)
        target = df[mask]

        signals = []
        for date, row in target.iterrows():
            # 計算這一天的 RS Rating
            rs_rating = None
            if level == "strict" and benchmark_df is not None:
                try:
                    idx = df.index.get_loc(date)
                    sub_df = df.iloc[: idx + 1]
                    sub_bench = benchmark_df.loc[:date]
                    rs_rating = cls.calc_rs_rating(sub_df, sub_bench)
                except (KeyError, IndexError):
                    rs_rating = 50.0

            if cls._row_passes(row, level, rs_rating):
                signals.append({
                    "ticker":      ticker,
                    "name":        name,
                    "signal_date": date,
                    "entry_price": round(float(row["Close"]), 2),
                })
        return signals

    # --------- 預留: 籌碼面 ---------
    @staticmethod
    def check_chip_concentration(ticker: str) -> bool:
        """Placeholder: 未來接 TDCC 千張大戶資料"""
        return True

    # =====================================================
    #   📐 Channel Breakout (下降通道突破)
    #   來源: 使用者經典策略 (Cup-with-Handle / 旗形整理)
    # =====================================================
    LOOKBACK_PEAK = 120          # 在過去 120 天內找旗桿頂端
    MIN_CHANNEL_LEN = 30         # 下降通道至少 30 個交易日
    MIN_R_SQUARED = 0.3          # 線性擬合最小 R² (v7 強化)
    MIN_SLOPE = -0.02            # 斜率必須 < 此值 (確實下降)
    BREAKOUT_VOL_MULTIPLIER = 1.5  # 突破日量能門檻 (v7 從 1.2 提高)

    @classmethod
    def _check_channel_breakout_row(cls, df: pd.DataFrame,
                                    end_idx: int = None) -> dict | None:
        """
        檢查 df 在 end_idx 這一天是否符合通道突破。
        ★ v8: 所有閾值從 StrategyParams 讀取
        """
        if end_idx is None:
            end_idx = len(df) - 1

        # ★ v8: 從 StrategyParams 讀取
        p = StrategyParams.get()
        lookback_peak = p["channel_lookback"]
        min_channel_len = p["channel_min_len"]
        min_r2 = p["channel_min_r2"]
        min_slope = p["channel_min_slope"]
        breakout_vol_mult = p["channel_vol_multiplier"]
        min_vol_shr = p["min_vol_lots"] * 1000

        if end_idx < lookback_peak:
            return None

        sub = df.iloc[max(0, end_idx - lookback_peak):end_idx + 1]
        if len(sub) < max(100, min_channel_len + 10):
            return None

        # 流動性: 20 日均量
        vol_ma20 = sub["Volume"].tail(20).mean()
        if vol_ma20 < min_vol_shr:
            return None

        # 大趨勢: 90MA 向上 (今日 > 20 日前)
        ma90 = sub["Close"].rolling(90).mean()
        if pd.isna(ma90.iloc[-1]) or pd.isna(ma90.iloc[-20]):
            return None
        if ma90.iloc[-1] <= ma90.iloc[-20]:
            return None

        # 在 sub 內找旗桿頂端 (最高點)
        peak_local_idx = sub["High"].argmax()
        if peak_local_idx >= len(sub) - 5:
            return None

        # 取「最高點 → 倒數第二天」的整理區間
        correction = sub.iloc[peak_local_idx:-1]
        if len(correction) < min_channel_len:
            return None

        # 線性回歸壓力線
        x = np.arange(len(correction))
        y = correction["High"].values
        try:
            slope, intercept, r_value, _, _ = linregress(x, y)
        except Exception:
            return None
        r_squared = r_value ** 2

        # 必須是有意義的下降通道
        if slope >= min_slope:
            return None
        if r_squared < min_r2:
            return None

        # 突破判定: 今天收盤 > 壓力線延伸值, 且帶量
        today = sub.iloc[-1]
        today_resistance = slope * len(correction) + intercept

        if today["Close"] <= today_resistance:
            return None
        if today["Volume"] < vol_ma20 * breakout_vol_mult:
            return None

        global_peak_idx = end_idx - len(sub) + 1 + peak_local_idx

        return {
            "pattern":          "channel_breakout",
            "slope":            float(slope),
            "intercept":        float(intercept),
            "r_squared":        float(r_squared),
            "peak_global_idx":  global_peak_idx,
            "channel_days":     len(correction),
            "today_resistance": round(float(today_resistance), 2),
            "today_close":      round(float(today["Close"]), 2),
            "today_volume":     int(today["Volume"]),
            "vol_ma20":         int(vol_ma20),
        }

    @classmethod
    def check_channel_breakout(cls, ticker: str, name: str) -> dict | None:
        """即時模式: 通道突破篩選"""
        df = StockDataFetcher.fetch_history(ticker, period="1y")
        if df.empty or len(df) < 120:
            return None

        result = cls._check_channel_breakout_row(df, end_idx=len(df) - 1)
        if result is None:
            return None

        return {
            "ticker":   ticker,
            "name":     name,
            "close":    result["today_close"],
            "volume":   result["today_volume"],
            "vol_ma20": result["vol_ma20"],
            "ma5":      0, "ma20": 0, "ma60": 0,  # 通道突破不靠均線
            "rsi":      0, "rs_rating": None,
            "level":    "channel",
            "extra":    result,  # 額外資訊
        }

    # =====================================================
    #   🛡️ Relative Strength Strong (大盤大跌抗跌)
    #   來源: 使用者經典策略 (Minervini RS 進階版)
    # =====================================================
    RS_LOOKBACK_DAYS = 90          # 回看 90 天找大跌日
    MARKET_DROP_THRESHOLD = -0.02  # 大盤大跌門檻 (-2%)
    MIN_RS_COUNT = 3               # 最少抗跌次數
    MAX_RS_COUNT = 15              # 最多抗跌次數 (避免異常)

    @classmethod
    def get_market_crash_dates(cls, lookback: int = None,
                               end_date: str = None) -> tuple[list, str]:
        """
        取得大盤 (^TWII) 在指定回看期內的大跌日清單。
        ★ v8: 大盤大跌門檻從 StrategyParams 讀取
        """
        p = StrategyParams.get()
        lookback = lookback or p["rs_lookback"]
        threshold = p["market_drop_threshold"]

        try:
            if end_date:
                end_dt = pd.to_datetime(end_date)
                start_dt = end_dt - timedelta(days=lookback * 2)
                twii = _history_retry(
                    "^TWII",
                    start=start_dt.strftime("%Y-%m-%d"),
                    end=(end_dt + timedelta(days=1)).strftime("%Y-%m-%d"))
            else:
                twii = _history_retry("^TWII", period="6mo")
            if twii.empty:
                return set(), None
            if twii.index.tz is not None:
                twii.index = twii.index.tz_localize(None)

            twii["PctChg"] = twii["Close"].pct_change()
            crashes = twii[twii["PctChg"] <= threshold].tail(lookback)
            dates = sorted(set(crashes.index.strftime("%Y-%m-%d")))
            latest = dates[-1] if dates else None
            return set(dates), latest
        except Exception as e:
            print(f"[WARN] 取得大盤資料失敗: {e}")
            return set(), None

    @classmethod
    def check_rs_strong(cls, ticker: str, name: str,
                        crash_dates: set = None,
                        latest_crash: str = None) -> dict | None:
        """即時模式: 大盤大跌抗跌篩選 (★ v8 參數可調)"""
        if crash_dates is None or latest_crash is None:
            crash_dates, latest_crash = cls.get_market_crash_dates()
            if not crash_dates:
                return None

        # ★ v8: 從 StrategyParams 讀取
        p = StrategyParams.get()
        rs_lookback = p["rs_lookback"]
        min_count = p["rs_min_count"]
        max_count = p["rs_max_count"]
        min_vol_shr = p["min_vol_lots"] * 1000

        df = StockDataFetcher.fetch_history(ticker, period="6mo")
        if df.empty or len(df) < rs_lookback + 5:
            return None

        # 流動性
        vol_ma5 = df["Volume"].tail(5).mean()
        if vol_ma5 < min_vol_shr:
            return None

        # 30MA > 60MA 趨勢過濾
        ma30 = df["Close"].tail(30).mean()
        ma60 = df["Close"].tail(60).mean()
        if ma30 <= ma60:
            return None

        # 找抗跌日: 大盤大跌日, 但個股收紅
        recent = df.tail(rs_lookback).copy()
        recent["PrevClose"] = df["Close"].shift(1).loc[recent.index]
        rs_dates = []
        for date, row in recent.iterrows():
            date_str = date.strftime("%Y-%m-%d")
            if date_str not in crash_dates:
                continue
            if row["Close"] > row["PrevClose"]:
                rs_dates.append(date_str)

        count = len(rs_dates)
        if not (min_count <= count <= max_count):
            return None
        if latest_crash and latest_crash not in rs_dates:
            return None

        last = df.iloc[-1]
        return {
            "ticker":   ticker,
            "name":     name,
            "close":    round(float(last["Close"]), 2),
            "volume":   int(last["Volume"]),
            "vol_ma20": int(df["Volume"].tail(20).mean()),
            "ma5":      0, "ma20": 0, "ma60": 0,
            "rsi":      0, "rs_rating": None,
            "level":    "rs_strong",
            "extra":    {
                "rs_count":      count,
                "rs_dates":      rs_dates,
                "latest_crash":  latest_crash,
            },
        }

    # =====================================================
    #   回測模式: 通道突破在歷史區間找訊號
    # =====================================================
    @classmethod
    def find_channel_signals_in_range(cls, ticker: str, name: str,
                                      start_date: str,
                                      end_date: str) -> list[dict]:
        """掃描歷史區間, 找出所有觸發通道突破的日期"""
        df = StockDataFetcher.fetch_range(ticker, start_date, end_date)
        if df.empty or len(df) < 130:
            return []

        start_ts = pd.to_datetime(start_date)
        end_ts   = pd.to_datetime(end_date)

        signals = []
        for i in range(120, len(df)):
            date = df.index[i]
            if not (start_ts <= date <= end_ts):
                continue
            result = cls._check_channel_breakout_row(df, end_idx=i)
            if result:
                signals.append({
                    "ticker":      ticker,
                    "name":        name,
                    "signal_date": date,
                    "entry_price": result["today_close"],
                })
        return signals

    # =====================================================
    #   回測模式: 大盤抗跌策略
    # =====================================================
    @classmethod
    def find_rs_signals_in_range(cls, ticker: str, name: str,
                                 start_date: str, end_date: str,
                                 crash_dates_by_day: dict = None) -> list[dict]:
        """歷史區間找抗跌訊號 (★ v8 參數可調)"""
        # ★ v8: 從 StrategyParams 讀取
        p = StrategyParams.get()
        rs_lookback = p["rs_lookback"]
        min_count = p["rs_min_count"]
        max_count = p["rs_max_count"]
        min_vol_shr = p["min_vol_lots"] * 1000

        df = StockDataFetcher.fetch_range(ticker, start_date, end_date)
        if df.empty or len(df) < rs_lookback + 5:
            return []

        df = cls.compute_indicators(df)

        start_ts = pd.to_datetime(start_date)
        end_ts   = pd.to_datetime(end_date)

        signals = []
        for i in range(rs_lookback, len(df)):
            date = df.index[i]
            if not (start_ts <= date <= end_ts):
                continue

            date_str = date.strftime("%Y-%m-%d")
            if crash_dates_by_day and date_str in crash_dates_by_day:
                crash_set, latest_crash = crash_dates_by_day[date_str]
            else:
                crash_set, latest_crash = cls.get_market_crash_dates(
                    rs_lookback, end_date=date_str)
            if not crash_set:
                continue

            past = df.iloc[max(0, i - rs_lookback):i + 1]
            past = past.copy()
            past["PrevClose"] = df["Close"].shift(1).loc[past.index]
            rs_dates = []
            for d2, row in past.iterrows():
                d2_str = d2.strftime("%Y-%m-%d")
                if d2_str in crash_set and row["Close"] > row["PrevClose"]:
                    rs_dates.append(d2_str)

            count = len(rs_dates)
            if not (min_count <= count <= max_count):
                continue
            if latest_crash and latest_crash not in rs_dates:
                continue

            row_today = df.iloc[i]
            vol_ma5 = df["Volume"].iloc[max(0, i-4):i+1].mean()
            if vol_ma5 < min_vol_shr:
                continue
            ma30 = df["Close"].iloc[max(0, i-29):i+1].mean()
            ma60 = df["Close"].iloc[max(0, i-59):i+1].mean()
            if ma30 <= ma60:
                continue

            signals.append({
                "ticker":      ticker,
                "name":        name,
                "signal_date": date,
                "entry_price": round(float(row_today["Close"]), 2),
            })
        return signals

    # =====================================================
    #   🛡️📈 抗跌續漲 (Resilient Rally) - v9.2 新增
    #   核心邏輯: 指定 D 日大盤大跌但個股抗跌, 隔日 D+1 個股續漲
    #   應用情境: 6/8 大盤崩跌 + 6/9 大盤反彈, 找出真正強勢個股
    # =====================================================
    @classmethod
    def check_resilient_rally(cls, ticker: str, name: str,
                              d_date: str, d1_date: str,
                              params: dict = None,
                              force_refresh: bool = False) -> dict | None:
        """
        檢查單一股票是否符合「D 日抗跌 + D+1 續漲」.

        Args:
            ticker: 股票代號 (含 .TW / .TWO)
            name:   股票名稱
            d_date: D 日 (大盤大跌日, YYYY-MM-DD)
            d1_date: D+1 日 (大盤反彈日, YYYY-MM-DD)
            params: 篩選參數 dict, 若 None 用預設
            force_refresh: 是否強制重新抓 (v9.2.2 新增, 避免快取過期)

        Returns:
            dict 或 None
        """
        # 預設參數
        defaults = {
            "stock_d_drop_max":   -1.5,   # D 日個股跌幅上限 (%) ≥-1.5%
            "stock_d_rise_min":   None,   # D 日個股漲幅下限 (None = 不要求漲, 抗跌即可)
            "stock_d1_rise_min":  2.0,    # D+1 個股漲幅下限 (%)
            "vol_surge_ratio":    1.0,    # D+1 量能/20日均量比 (1.0 = 不限制)
            "min_vol_lots":       300,    # D+1 最低成交張數
        }
        if params:
            defaults.update(params)
        p = defaults

        # 抓 6 個月資料 (包含 D 日與 D+1 日)
        # ★ v9.2.2: 把 force_refresh 傳給 StockDataFetcher
        df = StockDataFetcher.fetch_history(ticker, period="6mo",
                                            force_refresh=force_refresh)
        if df.empty or len(df) < 30:
            return None

        # ★ v9.2.1: 移除 tz + normalize, 並用字串匹配
        df = df.copy()
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df.index = df.index.normalize()
        date_strs = df.index.strftime("%Y-%m-%d").tolist()

        if d_date not in date_strs or d1_date not in date_strs:
            return None  # 該股票在這兩天沒有交易資料

        idx_d = date_strs.index(d_date)
        idx_d1 = date_strs.index(d1_date)
        if idx_d < 1 or idx_d1 != idx_d + 1:
            return None  # D+1 不是緊接著 D 日

        # 取出三天: D-1, D, D+1
        row_d_prev = df.iloc[idx_d - 1]
        row_d = df.iloc[idx_d]
        row_d1 = df.iloc[idx_d1]

        d_close = float(row_d["Close"])
        d_prev_close = float(row_d_prev["Close"])
        d1_close = float(row_d1["Close"])
        d1_volume = int(row_d1["Volume"])

        # D 日個股表現
        d_pct = (d_close - d_prev_close) / d_prev_close * 100

        # D+1 個股表現
        d1_pct = (d1_close - d_close) / d_close * 100

        # ---- 過濾條件 ----
        # 條件 1: D 日抗跌 (個股跌幅不大於設定值, 或收紅)
        if d_pct < p["stock_d_drop_max"]:
            return None

        # 條件 1b: D 日是否需要收紅?
        if p.get("stock_d_rise_min") is not None:
            if d_pct < p["stock_d_rise_min"]:
                return None

        # 條件 2: D+1 續漲
        if d1_pct < p["stock_d1_rise_min"]:
            return None

        # 條件 3: D+1 量能 (相對 20 日均量)
        vol_ma20 = float(df["Volume"].iloc[max(0, idx_d1 - 19):idx_d1 + 1].mean())
        vol_ratio = d1_volume / vol_ma20 if vol_ma20 > 0 else 0
        if vol_ratio < p["vol_surge_ratio"]:
            return None

        # 條件 4: 最低成交張數 (流動性)
        min_vol_shr = p["min_vol_lots"] * 1000
        if d1_volume < min_vol_shr:
            return None

        # 計算 2 日累積漲幅 (相對 D-1 日)
        two_day_pct = (d1_close - d_prev_close) / d_prev_close * 100

        return {
            "ticker":       ticker,
            "name":         name,
            "close":        round(d1_close, 2),
            "volume":       d1_volume,
            "vol_ma20":     int(vol_ma20),
            "ma5":          0, "ma20": 0, "ma60": 0,
            "rsi":          0, "rs_rating": None,
            "level":        "resilient_rally",
            "extra": {
                "d_date":         d_date,
                "d1_date":        d1_date,
                "d_close":        round(d_close, 2),
                "d_prev_close":   round(d_prev_close, 2),
                "d_pct":          round(d_pct, 2),
                "d1_pct":         round(d1_pct, 2),
                "two_day_pct":    round(two_day_pct, 2),
                "vol_ratio":      round(vol_ratio, 2),
            },
        }

    @classmethod
    def get_market_two_day_info(cls, d_date: str, d1_date: str,
                                 force_refresh: bool = False) -> dict:
        """
        取得大盤在 D 日與 D+1 日的漲跌幅, 供 UI 顯示參考
        v9.2.2: 加 force_refresh 參數, 避免 yfinance 快取造成資料未更新
        """
        try:
            start = pd.to_datetime(d_date) - timedelta(days=10)
            end = pd.to_datetime(d1_date) + timedelta(days=3)

            # ★ v9.2.2: 若 force_refresh, 直接抓新的並繞過任何快取
            twii = yf.Ticker("^TWII").history(
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                auto_adjust=False,
                prepost=False,
                actions=False)
            if twii.empty:
                return {"error": "yfinance 沒回傳任何大盤資料 (網路問題?)"}
            # 移除時區, 確保 naive
            if twii.index.tz is not None:
                twii.index = twii.index.tz_localize(None)
            # ★ v9.2.1: 把日期統一 normalize 成只剩日期 (時:分:秒 歸零)
            twii.index = twii.index.normalize()

            # ★ v9.2.1: 用字串匹配, 而非 Timestamp 物件匹配
            date_strs = twii.index.strftime("%Y-%m-%d").tolist()

            if d_date not in date_strs:
                # 列出附近的交易日方便診斷
                nearest = date_strs[-7:] if len(date_strs) >= 7 else date_strs
                return {"error": f"大盤資料中找不到 {d_date}。"
                                 f"附近可用交易日: {', '.join(nearest)}"}
            if d1_date not in date_strs:
                nearest = date_strs[-7:] if len(date_strs) >= 7 else date_strs
                return {"error": f"大盤資料中找不到 {d1_date}。"
                                 f"附近可用交易日: {', '.join(nearest)}"
                                 f"\n💡 yfinance 對台股有 6-24 小時延遲, "
                                 f"若 D+1 是今天或昨天, 可能尚未同步, 請晚點再試。"}

            # 找索引
            idx_d = date_strs.index(d_date)
            idx_d1 = date_strs.index(d1_date)
            if idx_d < 1:
                return {"error": f"{d_date} 是抓到的第一天, 沒有前一日資料可比較"}
            if idx_d1 != idx_d + 1:
                return {"error": f"{d1_date} 不是 {d_date} 的下一個交易日 "
                                 f"(中間相隔 {idx_d1 - idx_d} 個交易日)"}

            d_close = float(twii.iloc[idx_d]["Close"])
            d_prev_close = float(twii.iloc[idx_d - 1]["Close"])
            d1_close = float(twii.iloc[idx_d1]["Close"])

            d_pct = (d_close - d_prev_close) / d_prev_close * 100
            d1_pct = (d1_close - d_close) / d_close * 100

            return {
                "d_close":      round(d_close, 2),
                "d_prev_close": round(d_prev_close, 2),
                "d1_close":     round(d1_close, 2),
                "d_pct":        round(d_pct, 2),
                "d1_pct":       round(d1_pct, 2),
            }
        except Exception as e:
            import traceback
            return {"error": f"{e}\n{traceback.format_exc()[:300]}"}

    # =====================================================
    #   🚨 處置股 + 月線距離檢查 (v9.3 新增)
    # =====================================================
    @classmethod
    def check_disposal_ma20_proximity(cls, ticker: str, name: str,
                                       disposal_info: dict,
                                       force_refresh: bool = False) -> dict | None:
        """
        檢查單一處置股目前距月線多近, 並回傳完整資訊供分頁顯示。

        Args:
            ticker:        股票代號 (例 '3217', 函式內會自動加 .TW)
            name:          股票名稱
            disposal_info: DisposalStockFetcher 回傳的單筆 dict
            force_refresh: 強制重抓股價

        Returns:
            dict 或 None (股價資料不足時)
        """
        # 加上 .TW 後綴 (處置股都是上市)
        ticker_full = ticker if "." in ticker else f"{ticker}.TW"
        df = StockDataFetcher.fetch_history(ticker_full, period="3mo",
                                            force_refresh=force_refresh)
        if df.empty or len(df) < 20:
            return None

        # 移除時區
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)

        # 計算 MA20 (月線)
        df["MA20"] = df["Close"].rolling(window=20).mean()
        # 取最新一筆
        last = df.iloc[-1]
        close = float(last["Close"])
        ma20 = float(last["MA20"]) if not pd.isna(last["MA20"]) else None
        volume = int(last["Volume"]) if not pd.isna(last["Volume"]) else 0

        if ma20 is None or ma20 <= 0:
            return None

        # 距月線百分比 (正值 = 股價在月線上方; 負值 = 股價在月線下方)
        diff_pct = (close - ma20) / ma20 * 100
        abs_diff_pct = abs(diff_pct)

        # 顏色分級
        if abs_diff_pct <= 2.0:
            color_tag = "very_close"   # 紅色: ≤2%
        elif abs_diff_pct <= 5.0:
            color_tag = "close"        # 黃色: ≤5%
        else:
            color_tag = "far"          # 灰色: >5%

        # 計算 5MA / 60MA 供參考
        df["MA5"]  = df["Close"].rolling(window=5).mean()
        df["MA60"] = df["Close"].rolling(window=60).mean()
        ma5  = float(df["MA5"].iloc[-1])  if not pd.isna(df["MA5"].iloc[-1])  else None
        ma60 = float(df["MA60"].iloc[-1]) if not pd.isna(df["MA60"].iloc[-1]) else None

        # 量比 (今日量 / 20日均量)
        vol_ma20 = df["Volume"].iloc[-20:].mean()
        vol_ratio = volume / vol_ma20 if vol_ma20 > 0 else 0

        # 近 5 日漲跌幅
        if len(df) >= 6:
            change_5d_pct = (close - float(df["Close"].iloc[-6])) / float(df["Close"].iloc[-6]) * 100
        else:
            change_5d_pct = 0

        return {
            "ticker":      ticker,
            "name":        name,
            "close":       round(close, 2),
            "ma5":         round(ma5, 2) if ma5 else 0,
            "ma20":        round(ma20, 2),
            "ma60":        round(ma60, 2) if ma60 else 0,
            "diff_pct":    round(diff_pct, 2),
            "abs_diff_pct": round(abs_diff_pct, 2),
            "color_tag":   color_tag,
            "volume":      volume,
            "vol_ratio":   round(vol_ratio, 2),
            "change_5d_pct": round(change_5d_pct, 2),
            # 處置資訊
            "disposal":    disposal_info,
            "data_date":   df.index[-1].strftime("%Y-%m-%d"),
        }

