# -*- coding: utf-8 -*-
"""
big_holder.py — 大戶持股 (≥400 張) 週資料, 來源: 集保結算所「集保戶股權分散表」

為什麼這樣抓:
  • 集保 opendata CSV 只保留「當週」, 算不出週變動
  • 集保網站逐檔查詢頁 (qryStock) 可查約 1 年 (51 週) 歷史, 無驗證碼
    - 先 GET 頁面拿 SYNCHRONIZER_TOKEN + 可查日期清單
    - 每次 POST 查詢後, 回應會帶「下一個」token (token 只能用一次)
  • 歷史週資料不會再變 → 存進本地快取 (workflow 用 actions/cache 保存),
    之後每檔通常只需補抓「最新一週」

大戶定義: 持股分級 12~15 (400,001 股以上 = 400 張以上)
大戶持股% = 該四級股數合計 / 集保總股數 × 100
週變動    = 最新一週大戶持股% − 前一週大戶持股% (百分點, pp)
"""
import os
import re
import json
import time
import random

import requests

URL = "https://www.tdcc.com.tw/portal/zh/smWeb/qryStock"
URI = "/portal/zh/smWeb/qryStock"
BIG_LEVELS = (12, 13, 14, 15)          # 400,001 股以上
TOTAL_LEVEL = 17                        # 合計

_TOKEN_TAG_RE = re.compile(r'<input[^>]*name="SYNCHRONIZER_TOKEN"[^>]*>', re.I)
_VALUE_RE = re.compile(r'value="([^"]*)"')
_SELECT_RE = re.compile(r'<select[^>]*id="scaDate"[^>]*>(.*?)</select>', re.S | re.I)
_OPTION_RE = re.compile(r'value="(\d{8})"')
_ROW_RE = re.compile(r'<tr[^>]*>(.*?)</tr>', re.S | re.I)
_TD_RE = re.compile(r'<td[^>]*>(.*?)</td>', re.S | re.I)
_TAG_RE = re.compile(r'<[^>]+>')

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9",
    "Referer": URL,
}


class BigHolderUnavailable(Exception):
    """集保網站整體無法使用 (連不上 / 被擋 / 頁面改版)"""


def _num(s: str) -> float:
    s = (s or "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_table(html: str):
    """解析查詢結果表格 → {level: (人數, 股數, 比例%)}; 無表格回傳 {}"""
    levels = {}
    for row in _ROW_RE.findall(html):
        tds = [_TAG_RE.sub("", c).strip() for c in _TD_RE.findall(row)]
        if len(tds) == 5 and tds[0].isdigit():
            levels[int(tds[0])] = (_num(tds[2]), _num(tds[3]), _num(tds[4]))
    return levels


def big_ratio(levels: dict):
    """由分級表算 大戶持股% 與 大戶人數。資料不足回傳 None。"""
    if not all(lv in levels for lv in BIG_LEVELS):
        return None
    big_shares = sum(levels[lv][1] for lv in BIG_LEVELS)
    big_people = sum(levels[lv][0] for lv in BIG_LEVELS)
    total = levels.get(TOTAL_LEVEL, (0, 0, 0))[1]
    if total > 0:
        ratio = big_shares / total * 100
    else:  # 沒有合計列就用各級比例加總
        ratio = sum(levels[lv][2] for lv in BIG_LEVELS)
    return round(ratio, 3), int(big_people)


class BigHolderFetcher:
    def __init__(self, cache_dir: str = None, sleep: float = 0.6,
                 timeout: int = 20):
        self.cache_dir = cache_dir or os.environ.get("TDCC_CACHE_DIR",
                                                     ".tdcc_cache")
        self.sleep = sleep
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.token = None
        self.dates = []          # 可查日期, 新 → 舊 (YYYYMMDD)
        self.requests_made = 0
        os.makedirs(self.cache_dir, exist_ok=True)

    # ---------- 連線 ----------
    def open(self):
        """取得 token 與可查日期清單; 失敗丟 BigHolderUnavailable"""
        try:
            r = self.s.get(URL, timeout=self.timeout)
        except Exception as e:
            raise BigHolderUnavailable(f"連線失敗: {e}")
        if r.status_code != 200:
            raise BigHolderUnavailable(f"HTTP {r.status_code}")
        self._take_token(r.text)
        m = _SELECT_RE.search(r.text)
        dates = _OPTION_RE.findall(m.group(1)) if m else []
        if not self.token or not dates:
            raise BigHolderUnavailable("頁面找不到 token 或日期清單 (可能改版)")
        self.dates = sorted(set(dates), reverse=True)

    def _take_token(self, html: str):
        tag = _TOKEN_TAG_RE.search(html or "")
        if tag:
            v = _VALUE_RE.search(tag.group(0))
            self.token = v.group(1) if v else None
        else:
            self.token = None

    def _query(self, code: str, date: str):
        """查單檔單週。回傳 dict / {"missing": True} / None(失敗)"""
        for attempt in range(3):
            if not self.token:
                try:
                    self.open()
                except BigHolderUnavailable:
                    time.sleep(2 + attempt * 2)
                    continue
            data = {
                "SYNCHRONIZER_TOKEN": self.token,
                "SYNCHRONIZER_URI": URI,
                "method": "submit",
                "firDate": self.dates[0],
                "scaDate": date,
                "sqlMethod": "StockNo",
                "stockNo": code,
                "stockName": "",
            }
            try:
                r = self.s.post(URL, data=data, timeout=self.timeout)
                self.requests_made += 1
            except Exception:
                self.token = None
                time.sleep(2 + attempt * 2)
                continue
            html = r.text if r.status_code == 200 else ""
            self._take_token(html)               # token 只能用一次, 換下一個
            time.sleep(self.sleep + random.uniform(0, 0.4))
            levels = parse_table(html)
            if levels:
                res = big_ratio(levels)
                if res is None:
                    return {"date": date, "missing": True}
                ratio, people = res
                return {"date": date, "ratio": ratio, "people": people}
            if "查無此資料" in html:
                return {"date": date, "missing": True}
            # 其他情況 (token 失效 / 被擋 / 回應異常) → 重開 session 再試
            self.token = None
            time.sleep(2 + attempt * 2)
        return None

    # ---------- 快取 ----------
    def _cache_path(self, code: str) -> str:
        return os.path.join(self.cache_dir, f"{code}.json")

    def _load(self, code: str) -> dict:
        try:
            with open(self._cache_path(code), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self, code: str, data: dict):
        try:
            with open(self._cache_path(code), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            print(f"[big_holder] 快取寫入失敗 {code}: {e}")

    # ---------- 對外 ----------
    def history(self, code: str, weeks: int = 26) -> list:
        """最近 weeks 週的大戶持股 (舊 → 新)。每筆 {date, ratio, people}"""
        if not self.dates:
            self.open()
        want = self.dates[:max(1, min(weeks, len(self.dates)))]
        cache = self._load(code)
        changed = False
        for d in want:
            if d in cache:
                continue
            rec = self._query(code, d)
            if rec is None:          # 這週抓不到 (暫時性), 不寫快取, 下次再補
                continue
            cache[d] = rec
            changed = True
        if changed:
            self._save(code, cache)
        rows = [cache[d] for d in want if d in cache and not cache[d].get("missing")]
        return sorted(rows, key=lambda x: x["date"])

    def weekly_change(self, code: str):
        """回傳 {date, prev_date, ratio, prev_ratio, chg_pp} 或 None (資料不足)。
        只接受「最新一週 vs 緊鄰前一週」, 缺任一週就回 None。"""
        if not self.dates:
            self.open()
        rows = self.history(code, 2)
        if len(rows) < 2 or len(self.dates) < 2:
            return None
        prev, cur = rows[-2], rows[-1]
        if cur["date"] != self.dates[0] or prev["date"] != self.dates[1]:
            return None
        return {
            "date": cur["date"], "prev_date": prev["date"],
            "ratio": cur["ratio"], "prev_ratio": prev["ratio"],
            "chg_pp": round(cur["ratio"] - prev["ratio"], 2),
        }
