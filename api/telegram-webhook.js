# app.py
import os
import json
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify
from openai import OpenAI
from google.oauth2 import service_account
from googleapiclient.discovery import build

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]

ALLOWED_USERS = {
    47329648: "Khaled",
    6894180427: "Hamad",
}

app = Flask(__name__)
openai_client = OpenAI(api_key=OPENAI_API_KEY)
_sheets_service = None


def get_sheets_service():
    global _sheets_service
    if _sheets_service is not None:
        return _sheets_service
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    _sheets_service = build("sheets", "v4", credentials=creds)
    return _sheets_service


def send_telegram_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    requests.post(url, json=payload, timeout=10)


def call_ai_to_parse(text, person_name):
    completion = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": """
أنت مساعد لتسجيل عمليات مزرعة (عزبة).

أجب بصيغة JSON فقط بدون أي نص إضافي.

الصيغة المطلوبة:

{
  "action": "expense | income | inventory",
  "item": "وصف مختصر",
  "amount": رقم أو null,
  "person": "اسم الشخص",
  "notes": "ملاحظات مختصرة"
}

تعليمات:
- افهم العربية
- حوّل المبالغ إلى أرقام
- لا تخمّن
- استخدم اسم الشخص التالي في الحقل person: """ + person_name,
            },
            {"role": "user", "content": text},
        ],
    )
    raw = completion.choices[0].message.content
    return json.loads(raw)


def append_transaction_row(parsed):
    service = get_sheets_service()
    values = [
        [
            datetime.now(timezone.utc).isoformat(),
            parsed.get("action", ""),
            parsed.get("item", ""),
            parsed.get("amount", ""),
            parsed.get("person", ""),
            parsed.get("notes", ""),
        ]
    ]
    body = {"values": values}
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range="Transactions!A1",
        valueInputOption="USER_ENTERED",
        body=body,
    ).execute()


@app.route("/telegram-webhook", methods=["GET", "POST"])
def telegram_webhook():
    if request.method == "GET":
        return "OK"

    update = request.get_json(silent=True) or {}
    message = update.get("message") or update.get("edited_message")
    if not message or "text" not in message:
        return jsonify({"ok": True})

    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    text = message["text"].strip()

    if user_id not in ALLOWED_USERS:
        send_telegram_message(chat_id, "⛔ هذا البوت خاص.")
        return jsonify({"ok": True})

    person_name = ALLOWED_USERS[user_id]

    if text == "/start":
        send_telegram_message(
            chat_id,
            f"مرحباً {person_name} 👋\nأنا بوت تسجيل عمليات العزبة.\nاكتب /help لمعرفة الاستخدام.",
        )
        return jsonify({"ok": True})

    if text == "/help":
        send_telegram_message(
            chat_id,
            (
                "📌 طريقة الاستخدام\n\n"
                "✍️ اكتب العملية بشكل طبيعي، أمثلة:\n"
                "• اشتريت علف بـ 500\n"
                "• بعت خروف بـ 1200\n"
                "• دخل 300 من بيع حليب\n"
                "• زاد عدد الغنم 5\n"
                "• نقص عدد الغنم 2\n\n"
                "🔒 هذا البوت خاص بالعائلة فقط"
            ),
        )
        return jsonify({"ok": True})

    try:
        parsed = call_ai_to_parse(text, person_name)
    except Exception as e:
        print("AI error:", e)
        send_telegram_message(
            chat_id,
            "صار خطأ في فهم الرسالة. حاول تكتبها بجملة أوضح مثل: اشتريت علف بـ 500",
        )
        return jsonify({"ok": False})

    action = parsed.get("action")
    if action not in {"expense", "income", "inventory"}:
        send_telegram_message(
            chat_id,
            "ما فهمت العملية 🤔\nحاول تكتبها مثل:\nاشتريت علف بـ 500",
        )
        return jsonify({"ok": True})

    saved = True
    try:
        append_transaction_row(parsed)
    except Exception as e:
        saved = False
        print("Sheets error:", e)

    amount = parsed.get("amount")
    amount_text = f"{amount} درهم" if amount is not None else "بدون مبلغ"

    if action == "expense":
        type_text = "مصروف"
    elif action == "income":
        type_text = "دخل"
    else:
        type_text = "تعديل مخزون"

    reply = (
        "تم تسجيل العملية ✅\n"
        f"النوع: {type_text}\n"
        f"البند: {parsed.get('item','')}\n"
        f"المبلغ: {amount_text}\n"
        f"الشخص: {parsed.get('person','')}"
    )

    if not saved:
        reply += "\n\n⚠️ لم يتم الحفظ في Google Sheets"

    send_telegram_message(chat_id, reply)
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
