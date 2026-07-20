# -*- coding: utf-8 -*-
"""
notify_telegram.py — Telegram 推播 (文字 + 圖片)

Telegram 可直接 multipart 上傳圖片位元組, 不需要圖床 / 公開 URL。

需要環境變數:
  TELEGRAM_BOT_TOKEN — BotFather 給的 token
  TELEGRAM_CHAT_ID   — 要推播的對象 (個人/群組/頻道) chat id
"""
import os
import io
import time
import requests

API = "https://api.telegram.org/bot{token}/{method}"


def _cfg():
    return (os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            os.environ.get("TELEGRAM_CHAT_ID", ""))


def send_message(text: str) -> bool:
    token, chat_id = _cfg()
    if not token or not chat_id:
        print("[tg] 缺 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        return False
    url = API.format(token=token, method="sendMessage")
    try:
        r = requests.post(url, data={
            "chat_id": chat_id,
            "text": text[:4000],
            "disable_web_page_preview": True,
        }, timeout=30)
        if r.status_code == 200:
            return True
        print(f"[tg] sendMessage 失敗 HTTP {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        print(f"[tg] sendMessage 例外: {e}")
        return False


def send_photo(image_bytes: bytes, caption: str = "") -> bool:
    """上傳一張圖片 (bytes)。caption 上限 1024 字。"""
    token, chat_id = _cfg()
    if not token or not chat_id:
        print("[tg] 缺 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        return False
    if not image_bytes:
        return False
    url = API.format(token=token, method="sendPhoto")
    files = {"photo": ("chart.png", io.BytesIO(image_bytes), "image/png")}
    data = {"chat_id": chat_id, "caption": caption[:1024]}
    try:
        r = requests.post(url, data=data, files=files, timeout=60)
        if r.status_code == 200:
            return True
        # 圖片太大/尺寸超限時, 改用 sendDocument 當退路
        print(f"[tg] sendPhoto 失敗 HTTP {r.status_code}: {r.text[:200]} → 改試 sendDocument")
        return _send_document(token, chat_id, image_bytes, caption)
    except Exception as e:
        print(f"[tg] sendPhoto 例外: {e}")
        return False


def _send_document(token: str, chat_id: str, image_bytes: bytes,
                   caption: str = "") -> bool:
    url = API.format(token=token, method="sendDocument")
    files = {"document": ("chart.png", io.BytesIO(image_bytes), "image/png")}
    data = {"chat_id": chat_id, "caption": caption[:1024]}
    try:
        r = requests.post(url, data=data, files=files, timeout=90)
        if r.status_code == 200:
            return True
        print(f"[tg] sendDocument 失敗 HTTP {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        print(f"[tg] sendDocument 例外: {e}")
        return False
