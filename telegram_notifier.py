import os
import requests


def send_telegram(message: str, photo_path: str | None = None):
    token = os.getenv("TG_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TG_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[Telegram] 未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过通知")
        return

    base_url = f"https://api.telegram.org/bot{token}"
    try:
        if photo_path and os.path.exists(photo_path):
            with open(photo_path, "rb") as img:
                resp = requests.post(
                    f"{base_url}/sendPhoto",
                    data={
                        "chat_id": chat_id,
                        "caption": message[:1000],
                        "parse_mode": "HTML",
                    },
                    files={"photo": img},
                    timeout=30,
                )
        else:
            resp = requests.post(
                f"{base_url}/sendMessage",
                data={
                    "chat_id": chat_id,
                    "text": message[:4000],
                    "parse_mode": "HTML",
                },
                timeout=30,
            )

        if resp.status_code != 200:
            print(f"[Telegram] 推送失败: {resp.status_code} {resp.text[:200]}")
        else:
            print("[Telegram] 推送成功")
    except Exception as e:
        print(f"[Telegram] 推送异常: {e}")
