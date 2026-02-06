# api/telegram-webhook.py
from http.server import BaseHTTPRequestHandler
import json
import os
import re
from datetime import datetime, timezone, timedelta, date

import requests
from openai import OpenAI
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# =============== CONFIG =====================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")

# Authorized users (update as needed)
ALLOWED_USERS = {
    47329648: "Khaled",
    6894180427: "Hamad",
}

UAE_TZ = timezone(timedelta(hours=4))
openai_client = OpenAI(api_key=OPENAI_API_KEY)


# =============== BASIC HELPERS ==============

def now_timestamp():
    return datetime.now(UAE_TZ).strftime("%Y-%m-%d %H:%M")


def send_telegram_message(chat_id, text):
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=15)
    except Exception:
        pass


def get_sheets_service():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds)


def resolve_timestamp(date_str):
    """
    date_str: 'YYYY-MM-DD' or None
    returns timestamp string 'YYYY-MM-DD HH:MM' (00:00 if only date provided)
    """
    if date_str:
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            return dt.strftime("%Y-%m-%d 00:00")
        except Exception:
            pass
    return now_timestamp()


# =============== TRANSACTIONS SHEET =========
# A Timestamp, B Type(AR), C Item, D Amount, E Person, F Note, G Balance, H Quantity

def load_all_transactions(service):
    res = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="Transactions!A2:H",
    ).execute()
    rows = res.get("values", [])
    txs = []
    for r in rows:
        if len(r) < 4:
            continue
        ts_str = r[0]
        try:
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
        except Exception:
            try:
                ts = datetime.strptime(ts_str, "%Y-%m-%d")
            except Exception:
                continue
        type_ar = r[1] if len(r) > 1 else ""
        item = r[2] if len(r) > 2 else ""
        try:
            amount = float(r[3])
        except Exception:
            amount = 0.0
        person = r[4] if len(r) > 4 else ""
        note = r[5] if len(r) > 5 else ""
        try:
            balance = float(r[6]) if len(r) > 6 else None
        except Exception:
            balance = None
        try:
            quantity = float(r[7]) if len(r) > 7 else 0.0
        except Exception:
            quantity = 0.0
        txs.append(
            {
                "timestamp": ts,
                "type_ar": type_ar,
                "item": item,
                "amount": amount,
                "person": person,
                "note": note,
                "balance": balance,
                "quantity": quantity,
            }
        )
    return txs


def summarize_transactions(txs):
    income = sum(t["amount"] for t in txs if t["type_ar"] == "بيع")
    expense = sum(t["amount"] for t in txs if t["type_ar"] == "شراء")
    net = income - expense
    return income, expense, net


# (موجود فقط لو احتجناه لاحقاً)
def get_last_balance(service):
    txs = load_all_transactions(service)
    _, _, net = summarize_transactions(txs)
    return net


def append_transaction_row(service, timestamp, type_ar, item, amount, quantity, person, notes):
    """
    Write a transaction row using provided timestamp (YYYY-MM-DD HH:MM).
    لا نحسب رصيد تراكمي في العمود G، نخليه فاضي.
    """
    values = [[
        timestamp,    # A
        type_ar,      # B
        item,         # C
        amount,       # D
        person,       # E
        notes,        # F
        "",           # G (no live balance)
        quantity,     # H
    ]]

    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range="Transactions!A1:H1",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()

    if quantity and quantity != 0:
        delta_qty = quantity if type_ar == "شراء" else -quantity
        update_inventory_quantity_delta(service, item, delta_qty)


def undo_last_transaction(service):
    """Remove last transaction and revert inventory delta."""
    res = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="Transactions!A2:H",
    ).execute()
    rows = res.get("values", [])
    if not rows:
        return None

    last_index = len(rows) + 1  # +1 for header
    last_row = rows[-1]

    ts = last_row[0] if len(last_row) > 0 else ""
    type_ar = last_row[1] if len(last_row) > 1 else ""
    item = last_row[2] if len(last_row) > 2 else ""
    amt_str = last_row[3] if len(last_row) > 3 else "0"
    try:
        amount = float(amt_str)
    except Exception:
        amount = 0.0
    qty_str = last_row[7] if len(last_row) > 7 else "0"
    try:
        quantity = float(qty_str)
    except Exception:
        quantity = 0.0

    if quantity and quantity != 0:
        tx_delta_qty = quantity if type_ar == "شراء" else -quantity
        update_inventory_quantity_delta(service, item, -tx_delta_qty)

    service.spreadsheets().values().clear(
        spreadsheetId=SPREADSHEET_ID,
        range=f"Transactions!A{last_index}:H{last_index}",
        body={},
    ).execute()

    return {"timestamp": ts, "type_ar": type_ar, "item": item, "amount": amount}


# =============== INVENTORY SHEET ==============
# A Item, B Type, C Quantity, D Notes

def update_inventory_quantity_delta(service, item, delta_qty):
    values_api = service.spreadsheets().values()
    res = values_api.get(
        spreadsheetId=SPREADSHEET_ID,
        range="Inventory!A2:D",
    ).execute()
    rows = res.get("values", [])

    row_index = None
    current_qty = 0.0
    item_type = ""
    note_val = ""

    for i, row in enumerate(rows, start=2):
        if row and row[0] == item:
            row_index = i
            item_type = row[1] if len(row) > 1 else ""
            try:
                current_qty = float(row[2]) if len(row) > 2 and row[2] else 0.0
            except Exception:
                current_qty = 0.0
            note_val = row[3] if len(row) > 3 else ""
            break

    if row_index is not None:
        new_qty = current_qty + delta_qty
        if new_qty < 0:
            new_qty = 0.0
        values_api.update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"Inventory!A{row_index}:D{row_index}",
            valueInputOption="USER_ENTERED",
            body={"values": [[item, item_type, new_qty, note_val]]},
        ).execute()
    else:
        if delta_qty <= 0:
            return
        new_qty = delta_qty
        values_api.append(
            spreadsheetId=SPREADSHEET_ID,
            range="Inventory!A1:D1",
            valueInputOption="USER_ENTERED",
            body={"values": [[item, "", new_qty, ""]]},
        ).execute()


def set_inventory_quantity(service, item, target_qty):
    values_api = service.spreadsheets().values()
    res = values_api.get(
        spreadsheetId=SPREADSHEET_ID,
        range="Inventory!A2:D",
    ).execute()
    rows = res.get("values", [])

    row_index = None
    item_type = ""
    note_val = ""

    for i, row in enumerate(rows, start=2):
        if row and row[0] == item:
            row_index = i
            item_type = row[1] if len(row) > 1 else ""
            note_val = row[3] if len(row) > 3 else ""
            break

    if row_index is not None:
        values_api.update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"Inventory!A{row_index}:D{row_index}",
            valueInputOption="USER_ENTERED",
            body={"values": [[item, item_type, target_qty, note_val]]},
        ).execute()
    else:
        values_api.append(
            spreadsheetId=SPREADSHEET_ID,
            range="Inventory!A1:D1",
            valueInputOption="USER_ENTERED",
            body={"values": [[item, "", target_qty, ""]]},
        ).execute()


# =============== PENDING SHEET ===============
# A UserId, B Timestamp, C OpType, D Action, E Item, F Amount, G Quantity, H Person, I NotesJson

def save_pending_transaction(service, user_id, action, type_ar, item, amount, quantity, person, notes_json):
    values = [[
        str(user_id),
        now_timestamp(),
        "transaction",
        action or "",
        item,
        amount,
        quantity,
        person,
        notes_json,  # {"notes":"...", "date":"YYYY-MM-DD" or null}
    ]]
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range="Pending!A1:I1",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()


def save_pending_inventory_snapshot(service, user_id, snapshot_list):
    values = [[
        str(user_id),
        now_timestamp(),
        "inventory_snapshot",
        "",
        "",
        "",
        "",
        "",
        json.dumps(snapshot_list, ensure_ascii=False),
    ]]
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range="Pending!A1:I1",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()


def get_last_pending_for_user(service, user_id):
    res = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range="Pending!A2:I",
    ).execute()
    rows = res.get("values", [])
    if not rows:
        return None, None

    last_row_index = None
    last_row = None
    for i, r in enumerate(rows, start=2):
        if r and r[0] == str(user_id):
            last_row_index = i
            last_row = r

    return last_row, last_row_index


def clear_pending_row(service, row_index):
    if row_index is None:
        return
    service.spreadsheets().values().clear(
        spreadsheetId=SPREADSHEET_ID,
        range=f"Pending!A{row_index}:I{row_index}",
        body={},
    ).execute()


# =============== AI PARSING ==================

def call_ai_to_parse(text):
    completion = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": """
أنت مساعد لتسجيل عمليات العزبة.

أعد دائماً JSON فقط بدون أي نص آخر.

الصيغة:

{
  "operation_type": "transaction | inventory_snapshot | report | other",

  "transaction": {
    "action": "buy | sell",
    "item": "وصف مختصر",
    "amount": رقم أو 0,
    "quantity": عدد صحيح أو 0,
    "date": "YYYY-MM-DD أو null",
    "notes": "ملاحظات مختصرة"
  },

  "inventory_snapshot": [
    { "item": "نوع الحيوان أو الشيء", "quantity": عدد صحيح }
  ],

  "report": {
    "kind": "day | week | month | all",
    "date": "YYYY-MM-DD أو null",
    "metric": "sales | purchases | net | all"
  }
}

قواعد مهمة:
- أي سؤال عن "كم" أو "إجمالي" المبيعات أو المشتريات أو الصافي (مثال: كم اجمالي المبيعات؟ كم صرفنا؟ كم الربح؟) → اعتبره report وليس transaction.
- إذا لم يذكر فترة، اعتبر الفترة = all (كل العمليات).
- إذا قال اليوم، هاليوم، اليوم فقط → kind = "day" و date = تاريخ اليوم أو التاريخ المذكور.
- إذا قال هالأسبوع، آخر أسبوع، آخر ٧ أيام → kind = "week".
- إذا قال هالشهر، هذا الشهر، الشهر الحالي → kind = "month".
- metric:
    * أسئلة عن المبيعات فقط → "sales"
    * أسئلة عن الصرف أو المشتريات → "purchases"
    * أسئلة عن الربح أو العجز أو الصافي → "net"
    * إذا طلب "ملخص" عام بدون تحديد (مثال: اعطني ملخص اليوم) → metric = "all".

- transaction: استخدم فقط عندما تكون جملة بيع أو شراء حقيقية (شريت، بعت، دفعنا، قبضنا...).
- Inventory snapshot هو جرد كامل (عدد الحيوانات الآن).
- إذا لم تفهم → operation_type = "other".
""".strip(),
            },
            {"role": "user", "content": text},
        ],
    )
    raw = completion.choices[0].message.content
    return json.loads(raw)


# =============== MAIN HANDLER ===============

class handler(BaseHTTPRequestHandler):
    def _ok(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_GET(self):
        self._ok()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        update = json.loads(body)

        message = update.get("message") or update.get("edited_message")
        if not message or "text" not in message:
            self._ok()
            return

        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        text = message["text"].strip()

        if user_id not in ALLOWED_USERS:
            send_telegram_message(chat_id, "⛔ هذا البوت خاص.")
            self._ok()
            return

        person = ALLOWED_USERS[user_id]
        service = get_sheets_service()

        # --------- Basic commands (no AI) ---------
        if text == "/start":
            send_telegram_message(
                chat_id,
                f"مرحباً {person} 👋\n"
                "أنا بوت تسجيل عمليات العزبة.\n"
                "أسجل عمليات الشراء والبيع فقط، والحساب (كم صرفنا / كم دخلنا / الصافي) يكون من التقارير مثل /day و /week و /balance.\n"
                "اكتب /help لعرض الأوامر.",
            )
            self._ok()
            return

        if text == "/help":
            msg = (
                "📋 الأوامر المتاحة:\n"
                "/help - عرض هذه القائمة\n"
                "/day - ملخص اليوم (بيع، شراء، صافي)\n"
                "/week - ملخص آخر ٧ أيام\n"
                "/balance - ملخص كامل لكل العمليات المسجلة\n"
                "/undo - حذف آخر عملية مسجلة (مع تعديل المخزون)\n"
                "/confirm - تأكيد آخر عملية معلّقة\n"
                "/cancel - إلغاء آخر عملية معلّقة\n\n"
                "تقدر بعد تسألني نصياً مثل:\n"
                "  - كم اجمالي المبيعات؟\n"
                "  - كم صرفنا هالشهر؟\n"
                "  - كم الربح هذا الاسبوع؟\n\n"
                "بعد ما تكتب عملية بيع أو شراء، البوت يعرض تفاصيلها ويسألك تأكيد.\n"
                "استخدم /confirm للتسجيل أو /cancel للإلغاء.\n"
                "ملاحظة: ما في رصيد ينقص أو يزيد داخل الشيت، كله حساب لحظي من العمليات."
            )
            send_telegram_message(chat_id, msg)
            self._ok()
            return

        if text == "/balance":
            txs = load_all_transactions(service)
            income, expense, net = summarize_transactions(txs)
            send_telegram_message(
                chat_id,
                "💰 ملخص الصندوق لكل الفترة المسجلة (لا يغيّر أي أرقام في الدفتر):\n"
                f"إجمالي المبيعات (الداخل): {income}\n"
                f"إجمالي المشتريات (المصروف): {expense}\n"
                f"الصافي (البيع - الشراء): {net}",
            )
            self._ok()
            return

        if text == "/undo":
            last = undo_last_transaction(service)
            if not last:
                send_telegram_message(chat_id, "لا توجد عمليات لحذفها.")
            else:
                send_telegram_message(
                    chat_id,
                    "↩️ تم حذف آخر عملية (مع تعديل المخزون):\n"
                    f"{last['timestamp']} | {last['type_ar']} | {last['item']} | {last['amount']}",
                )
            self._ok()
            return

        if text == "/day":
            today = datetime.now(UAE_TZ).date()
            txs = load_all_transactions(service)
            todays = [t for t in txs if t["timestamp"].date() == today]
            msg = self._build_summary_message(todays, f"ملخص اليوم {today}")
            send_telegram_message(chat_id, msg)
            self._ok()
            return

        if text == "/week":
            today = datetime.now(UAE_TZ).date()
            start = today - timedelta(days=6)
            txs = load_all_transactions(service)
            week_txs = [t for t in txs if start <= t["timestamp"].date() <= today]
            msg = self._build_summary_message(
                week_txs, f"ملخص آخر ٧ أيام من {start} إلى {today}"
            )
            send_telegram_message(chat_id, msg)
            self._ok()
            return

        if text == "/cancel":
            pending, row_idx = get_last_pending_for_user(service, user_id)
            if not pending:
                send_telegram_message(chat_id, "لا توجد عملية معلّقة لإلغائها.")
            else:
                clear_pending_row(service, row_idx)
                send_telegram_message(chat_id, "❌ تم إلغاء العملية المعلّقة.")
            self._ok()
            return

        if text == "/confirm":
            pending, row_idx = get_last_pending_for_user(service, user_id)
            if not pending:
                send_telegram_message(chat_id, "لا توجد عملية معلّقة للتأكيد.")
                self._ok()
                return

            op_type = (pending + [""] * 3)[2]

            if op_type == "transaction":
                _, _, _, action, item, amount_str, qty_str, person_name, notes_json = (
                    (pending + [""] * 9)[:9]
                )
                try:
                    meta = json.loads(notes_json) if notes_json else {}
                except Exception:
                    meta = {}
                notes_txt = meta.get("notes", "")
                date_str = meta.get("date")
                timestamp = resolve_timestamp(date_str)

                try:
                    amount = float(amount_str)
                except Exception:
                    amount = 0.0
                try:
                    quantity = int(float(qty_str)) if qty_str else 0
                except Exception:
                    quantity = 0

                type_ar = "شراء" if action == "buy" else "بيع"
                append_transaction_row(
                    service, timestamp, type_ar, item, amount, quantity, person_name, notes_txt
                )
                clear_pending_row(service, row_idx)

                sign = "+" if type_ar == "بيع" else "-"
                qty_text = f"\nالكمية: {quantity}" if quantity else ""
                send_telegram_message(
                    chat_id,
                    "✅ تم تأكيد العملية وتسجيلها في الدفتر:\n"
                    f"التاريخ: {timestamp}\n"
                    f"النوع: {type_ar}\n"
                    f"البند: {item}\n"
                    f"المبلغ: {amount} ({sign})\n"
                    f"الشخص: {person_name}{qty_text}\n"
                    "الحساب الكلي (كم صرفنا وكم دخلنا والصافي) يكون من أوامر التقرير.",
                )
                self._ok()
                return

            elif op_type == "inventory_snapshot":
                snapshot_json = (pending + [""] * 9)[8]
                try:
                    snapshot = json.loads(snapshot_json)
                except Exception:
                    snapshot = []

                for row in snapshot:
                    item = (row.get("item") or "").strip()
                    qty = row.get("quantity", 0)
                    if not item:
                        continue
                    try:
                        qty_val = int(qty)
                    except Exception:
                        qty_val = 0
                    if qty_val < 0:
                        qty_val = 0
                    set_inventory_quantity(service, item, qty_val)

                clear_pending_row(service, row_idx)

                lines = ["✅ تم تحديث المخزون حسب الأعداد التالية:"]
                for row in snapshot:
                    item = (row.get("item") or "").strip()
                    qty = row.get("quantity", 0)
                    if item:
                        lines.append(f"- {item}: {qty}")
                send_telegram_message(chat_id, "\n".join(lines))
                self._ok()
                return

            else:
                send_telegram_message(chat_id, "نوع العملية المعلّقة غير معروف.")
                self._ok()
                return

        # --------- Everything else → AI decides ---------
        try:
            parsed = call_ai_to_parse(text)
        except Exception:
            send_telegram_message(chat_id, "❌ لم أفهم العملية. حاول تكتبها بشكل أوضح.")
            self._ok()
            return

        op_type = parsed.get("operation_type")

        # ----- Transaction flow -----
        if op_type == "transaction":
            tx = parsed.get("transaction", {}) or {}
            action = tx.get("action")
            item = (tx.get("item") or "").strip()
            try:
                amount = float(tx.get("amount", 0))
            except Exception:
                amount = 0.0
            try:
                quantity = int(tx.get("quantity", 0) or 0)
            except Exception:
                quantity = 0
            notes = tx.get("notes", "") or ""
            date_str = tx.get("date")

            if action not in ("buy", "sell") or amount <= 0 or not item:
                send_telegram_message(chat_id, "❌ العملية غير واضحة. مثال: بعت خروفين بـ 1200")
                self._ok()
                return

            type_ar = "شراء" if action == "buy" else "بيع"

            notes_json = json.dumps({"notes": notes, "date": date_str}, ensure_ascii=False)
            save_pending_transaction(
                service, user_id, action, type_ar, item, amount, quantity, person, notes_json
            )

            sign = "+" if type_ar == "بيع" else "-"
            qty_text = f"\nالكمية: {quantity}" if quantity else ""
            display_date = date_str if date_str else now_timestamp()
            msg = (
                "🔍 تفاصيل العملية المقترحة:\n"
                f"التاريخ (المقترح): {display_date}\n"
                f"النوع: {type_ar}\n"
                f"البند: {item}\n"
                f"المبلغ: {amount} ({sign})\n"
                f"الشخص: {person}{qty_text}\n\n"
                "سيتم فقط تسجيل هذه العملية في الدفتر.\n"
                "لرؤية كم صرفت أو كم دخلت استخدم الأوامر مثل /day أو /week أو /balance "
                "أو اسألني: كم اجمالي المبيعات؟ كم صرفنا هالشهر؟\n\n"
                "هل أنت متأكد أنك تريد تسجيل هذه العملية؟\n"
                "اكتب /confirm للتأكيد أو /cancel للإلغاء."
            )
            send_telegram_message(chat_id, msg)
            self._ok()
            return

        # ----- Inventory snapshot flow -----
        if op_type == "inventory_snapshot":
            snapshot = parsed.get("inventory_snapshot") or []
            if not snapshot:
                send_telegram_message(chat_id, "❌ لم أستطع قراءة الأعداد من الرسالة.")
                self._ok()
                return

            save_pending_inventory_snapshot(service, user_id, snapshot)

            lines = ["🔍 سيتم تحديث المخزون بالأعداد التالية (بعد التأكيد):"]
            for row in snapshot:
                item = (row.get("item") or "").strip()
                qty = row.get("quantity", 0)
                if item:
                    lines.append(f"- {item}: {qty}")
            lines.append("\nهل تريد اعتماد هذه الأعداد كعدد حالي؟\nاكتب /confirm للتأكيد أو /cancel للإلغاء.")
            send_telegram_message(chat_id, "\n".join(lines))
            self._ok()
            return

        # ----- Report flow -----
        if op_type == "report":
            rep = parsed.get("report", {}) or {}
            kind = (rep.get("kind") or "all").lower()
            metric = (rep.get("metric") or "all").lower()
            date_str = rep.get("date")

            txs = load_all_transactions(service)
            today = datetime.now(UAE_TZ).date()

            # حدد الفترة
            if kind == "day":
                if date_str:
                    try:
                        target = datetime.strptime(date_str, "%Y-%m-%d").date()
                    except Exception:
                        target = today
                else:
                    target = today
                period_txs = [t for t in txs if t["timestamp"].date() == target]
                period_label = f"يوم {target}"
            elif kind == "week":
                start = today - timedelta(days=6)
                end = today
                period_txs = [t for t in txs if start <= t["timestamp"].date() <= end]
                period_label = f"من {start} إلى {end}"
            elif kind == "month":
                if date_str:
                    try:
                        target = datetime.strptime(date_str, "%Y-%m-%d").date()
                    except Exception:
                        target = today
                else:
                    target = today
                month_start = date(target.year, target.month, 1)
                if target.month == 12:
                    next_month = date(target.year + 1, 1, 1)
                else:
                    next_month = date(target.year, target.month + 1, 1)
                month_end = next_month - timedelta(days=1)
                period_txs = [t for t in txs if month_start <= t["timestamp"].date() <= month_end]
                period_label = f"شهر {target.year}-{target.month:02d}"
            else:  # all
                period_txs = txs
                period_label = "لكل الفترة المسجلة"

            income, expense, net = summarize_transactions(period_txs)

            if metric == "sales":
                msg = (
                    f"📈 إجمالي المبيعات في الفترة ({period_label}): {income}\n"
                    "هذا حساب فقط من العمليات المسجلة، لا يغيّر أي رصيد في الدفتر."
                )
            elif metric == "purchases":
                msg = (
                    f"💸 إجمالي المشتريات (المصروف) في الفترة ({period_label}): {expense}\n"
                    "هذا حساب فقط من العمليات المسجلة."
                )
            elif metric == "net":
                msg = (
                    f"📊 الصافي (البيع - الشراء) في الفترة ({period_label}): {net}\n"
                    "موجب = ربح، سالب = عجز."
                )
            else:  # all
                title = f"ملخص {period_label}"
                msg = self._build_summary_message(period_txs, title)

            send_telegram_message(chat_id, msg)
            self._ok()
            return

        # ----- Unknown / other -----
        send_telegram_message(
            chat_id,
            "❌ ما قدرت أفهم الرسالة كبيع/شراء أو جرد مخزون أو طلب تقرير.\nحاول تكتبها بشكل أوضح.",
        )
        self._ok()

    # ---------- Summary helper ----------
    def _build_summary_message(self, txs, title):
        if not txs:
            return f"{title}\nلا توجد عمليات في هذه الفترة."

        income, expense, net = summarize_transactions(txs)
        lines = [
            f"📊 {title}",
            f"عدد العمليات: {len(txs)}",
            f"إجمالي البيع: {income}",
            f"إجمالي الشراء: {expense}",
            f"الصافي (البيع - الشراء): {net}",
            "",
            "تفاصيل:",
        ]
        for t in txs[:20]:
            time_str = t["timestamp"].strftime("%H:%M")
            lines.append(
                f"- {time_str} | {t['type_ar']} | {t['item']} | {t['amount']} | {t['person']} | كمية: {int(t['quantity'])}"
            )
        if len(txs) > 20:
            lines.append(f"... وأكثر ({len(txs) - 20}) عملية أخرى")
        return "\n".join(lines)
