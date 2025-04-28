import os
import json
import time
import threading
from typing import Optional

import requests
from flask import Flask, request, jsonify, abort

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TRANSKRIPTOR_API_KEY = os.getenv("TRANSKRIPTOR_API_KEY")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 30))

if not TELEGRAM_BOT_TOKEN:
    raise EnvironmentError("TELEGRAM_BOT_TOKEN is required")
if not TRANSKRIPTOR_API_KEY:
    raise EnvironmentError("TRANSKRIPTOR_API_KEY is required")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TRANSKRIPTOR_BASE = "https://api.tor.app"

# --------------------------------------------------------------------------- #
# Helpers: Telegram
# --------------------------------------------------------------------------- #
def send_telegram_request(method: str, **params):
    """Send an API request to Telegram and return the JSON response"""
    url = f"{TELEGRAM_API}/{method}"
    resp = requests.post(url, json=params, timeout=30)
    if not resp.ok:
        print(f"[Telegram] {method} failed: {resp.status_code} – {resp.text}")
    return resp.json()


def send_message(chat_id: int, text: str, reply_to: Optional[int] = None):
    """Send a (potentially long) text message, splitting if >4096 chars"""
    max_len = 4096
    for i in range(0, len(text), max_len):
        part = text[i : i + max_len]
        send_telegram_request(
            "sendMessage", chat_id=chat_id, text=part, reply_to_message_id=reply_to
        )


def send_document(chat_id: int, file_name: str, file_bytes: bytes, caption: str = ""):
    """Upload a document to Telegram"""
    files = {"document": (file_name, file_bytes)}
    data = {"chat_id": chat_id, "caption": caption}
    url = f"{TELEGRAM_API}/sendDocument"
    resp = requests.post(url, data=data, files=files, timeout=60)
    if not resp.ok:
        print(f"[Telegram] sendDocument failed: {resp.status_code} – {resp.text}")


# --------------------------------------------------------------------------- #
# Helpers: Transkriptor
# --------------------------------------------------------------------------- #
TRANS_HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {TRANSKRIPTOR_API_KEY}",
    "Accept": "application/json",
}

def create_transcription(youtube_url: str, language: str = "en-US", service: str = "Standard") -> str:
    """Create a transcription order and return order_id"""
    endpoint = f"{TRANSKRIPTOR_BASE}/developer/transcription/url"
    payload = {
        "url": youtube_url,
        "service": service,
        "language": language,
    }
    resp = requests.post(endpoint, headers=TRANS_HEADERS, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data["order_id"]


def export_transcription(order_id: str):
    """Attempt to export the transcription. Returns (status_code, json)"""
    endpoint = f"{TRANSKRIPTOR_BASE}/developer/files/{order_id}/content/export"
    payload = {
        "export_type": "txt",
        "include_speaker_names": True,
        "include_timestamps": True,
        "merge_same_speaker_segments": False,
        "is_single_paragraph": False,
        "paragraph_size": 4,
    }
    resp = requests.post(endpoint, headers=TRANS_HEADERS, json=payload, timeout=30)
    return resp.status_code, (resp.json() if resp.content else {})


# --------------------------------------------------------------------------- #
# Background job
# --------------------------------------------------------------------------- #
def poll_and_send(order_id: str, chat_id: int):
    """Poll Transkriptor until done and send the result to Telegram"""
    while True:
        status_code, data = export_transcription(order_id)
        if status_code == 200:
            content = data.get("content")
            presigned_url = data.get("presigned_url")  # fallback
            if content:
                # send as text file
                file_bytes = content.encode("utf-8")
                send_document(chat_id, f"{order_id}.txt", file_bytes, caption="Here is your transcript!")
            elif presigned_url:
                send_message(chat_id, f"Transcription is ready: {presigned_url}")
            else:
                send_message(chat_id, "Transcription completed but no content was returned.")
            break
        elif status_code == 202:
            print(f"[Poll] Order {order_id} still processing...")
            time.sleep(POLL_INTERVAL)
        else:
            send_message(chat_id, f"Failed to retrieve transcript (status: {status_code}).")
            break


# --------------------------------------------------------------------------- #
# Flask App
# --------------------------------------------------------------------------- #
app = Flask(__name__)

@app.route(f"/webhook/{TELEGRAM_BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    if request.method == "POST":
        update = request.get_json(force=True)
        if not update:
            return jsonify({}), 200

        message = update.get("message") or update.get("edited_message")
        if not message:
            return jsonify({}), 200

        chat_id = message["chat"]["id"]
        text = message.get("text", "").strip()

        # Expect command /transcribe <url>
        if text.startswith("/transcribe"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                send_message(chat_id, "Usage: /transcribe <YouTube URL>")
                return jsonify({}), 200
            youtube_url = parts[1]
            try:
                order_id = create_transcription(youtube_url)
                send_message(chat_id, f"✅ Transcription started! Order ID: {order_id}\nI'll notify you when it's ready.")
                threading.Thread(
                    target=poll_and_send, args=(order_id, chat_id), daemon=True
                ).start()
            except requests.HTTPError as e:
                send_message(chat_id, f"❌ Failed to create transcription: {e.response.text}")
        else:
            send_message(chat_id, "Send /transcribe <YouTube URL> to start a transcription.")

    return jsonify({}), 200


# --------------------------------------------------------------------------- #
# Local entry point
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5050)))
