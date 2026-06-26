# api/telegram-webhook.py

from http.server import BaseHTTPRequestHandler
import json
import os
import re
from datetime import datetime, timezone, timedelta
import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")

ALLOWED_USERS = {
    47329648: "Khaled",
    6894180427: "Hamad",
}

UAE_TZ = timezone(timedelta(hours=4))

S_TRANSACTIONS = "Transactions"
S_INVENTORY = "Inventory"
S_PENDING = "Pending"
S_STATE = "BotState"

D = "──────────────"
CANCEL_WORDS = {"الغاء", "إلغاء", "❌ إلغاء", "/cancel", "cancel"}
SKIP_WORDS = {"تخطي", "➡️ تخطي", "skip", "-"}
CONFIRM_WORDS = {"✅ تأكيد", "تأكيد", "confirm", "/confirm"}
BACK_WORDS = {"↩️ رجوع", "رجوع", "back"}

MAIN_MENU = [
    ["💰 بيع", "🛒 شراء"],
    ["⚡ فاتورة كهرباء", "👷 عمالة"],
    ["📦 الجرد", "📊 التقرير"],
    ["🕐 آخر العمليات", "↩️ تراجع آخر عملية"],
]

SELL_ITEMS = [
    ["🥚 بيض", "🐐 ماعز"],
    ["🐑 غنم", "🐓 دجاج"],
    ["🐥 صيصان", "🥛 حليب"],
    ["🧺 أخرى", "↩️ رجوع"],
]

BUY_ITEMS = [
    ["🌾 علف", "💊 أدوية"],
    ["🧰 معدات", "🔧 صيانة"],
    ["💧 ماء", "🚚 نقل"],
    ["🧾 أخرى", "↩️ رجوع"],
]

PAYMENT_METHODS = [
    ["💵 كاش", "🏦 تحويل"],
    ["🧾 آجل", "↩️ رجوع"],
]

REPORT_MENU = [
    ["📆 تقرير اليوم", "🗓 تقرير الأسبوع"],
    ["📅 تقرير الشهر", "📊 تقرير كامل"],
    ["↩️ رجوع"],
]

CONFIRM_MENU = [["✅ تأكيد", "❌ إلغاء"]]
SKIP_MENU = [["➡️ تخطي", "❌ إلغاء"]]


def now_str():
    return datetime.now(UAE_TZ).strftime("%Y-%m-%d %H:%M")


def today_str():
    return datetime.now(UAE_TZ).strftime("%Y-%m-%d")


def fmt(x):
    try:
        f = float(x)
        return str(int(f)) if f.is_integer() else str(round(f, 2))
    except Exception:
        return str(x)


def clean_label(text):
    text = (text or "").strip()
    text = re.sub(r"^[^\w\u0600-\u06FF]+\s*", "", text)
    return text.strip()


def normalize_amount(text):
    if not text:
        return 0.0
    arabic_digits = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    cleaned = text.translate(arabic_digits)
    cleaned = cleaned.replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    return float(match.group(0)) if match else 0.0


def normalize_qty(text):
    amount = normalize_amount(text)
    return int(amount) if amount > 0 else 0


def send(chat_id, text, keyboard=None, remove_keyboard=False):
    if not TELEGRAM_BOT_TOKEN:
        return
    payload = {"chat_id": chat_id, "text": text}
    if keyboard:
        payload["reply_markup"] = {
            "keyboard": keyboard,
            "resize_keyboard": True,
            "one_time_keyboard": False,
        }
    elif remove_keyboard:
        payload["reply_markup"] = {"remove_keyboard": True}
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=15,
        )
    except Exception:
        pass


def sheets_svc():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds)


def read_sheet(svc, sheet, rng="A1:Z"):
    try:
        res = svc.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{sheet}!{rng}",
        ).execute()
        return res.get("values", [])
    except Exception:
        return []


def append_row(svc, sheet, row):
    ensure_sheet(svc, sheet)
    svc.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [row]},
    ).execute()


def ensure_sheet(svc, sheet_name):
    try:
        meta = svc.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
        sheets = [s["properties"]["title"] for s in meta.get("sheets", [])]
        if sheet_name in sheets:
            return
        svc.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]},
        ).execute()
    except Exception:
        pass


def ensure_state_sheet(svc):
    ensure_sheet(svc, S_STATE)
    rows = read_sheet(svc, S_STATE, "A1:C1")
    if not rows:
        svc.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{S_STATE}!A1:C1",
            valueInputOption="USER_ENTERED",
            body={"values": [["User_ID", "State_JSON", "Updated_At"]]},
        ).execute()


def get_state(svc, user_id):
    ensure_state_sheet(svc)
    rows = read_sheet(svc, S_STATE, "A2:C")
    for r in rows:
        if r and r[0] == str(user_id):
            try:
                return json.loads(r[1]) if len(r) > 1 and r[1] else {}
            except Exception:
                return {}
    return {}


def set_state(svc, user_id, state):
    ensure_state_sheet(svc)
    rows = read_sheet(svc, S_STATE, "A2:C")
    body = [[str(user_id), json.dumps(state, ensure_ascii=False), now_str()]]
    for i, r in enumerate(rows, start=2):
        if r and r[0] == str(user_id):
            svc.spreadsheets().values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"{S_STATE}!A{i}:C{i}",
                valueInputOption="USER_ENTERED",
                body={"values": body},
            ).execute()
            return
    svc.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{S_STATE}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": body},
    ).execute()


def clear_state(svc, user_id):
    set_state(svc, user_id, {})


def add_transaction(svc, ttype, item, category, amount, user):
    append_row(svc, S_TRANSACTIONS, [now_str(), ttype, item, category, amount, user])


def add_pending(svc, user_id, op_type, action, item, amount, qty, person, notes=""):
    append_row(svc, S_PENDING, [str(user_id), now_str(), op_type, action, item, amount, qty, person, notes])


def load_transactions(svc):
    rows = read_sheet(svc, S_TRANSACTIONS, "A2:Z")
    out = []
    for i, r in enumerate(rows, start=2):
        if len(r) < 5:
            continue
        try:
            amount = float(str(r[4]).replace(",", ""))
        except Exception:
            continue
        out.append({
            "row": i,
            "date": r[0],
            "type": r[1],
            "item": r[2],
            "category": r[3] if len(r) > 3 else "",
            "amount": amount,
            "user": r[5] if len(r) > 5 else "",
        })
    return out


def delete_transaction_row(svc, row_num):
    meta = svc.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    sheet_id = None
    for sheet in meta.get("sheets", []):
        if sheet["properties"]["title"] == S_TRANSACTIONS:
            sheet_id = sheet["properties"]["sheetId"]
            break
    if sheet_id is None:
        return False
    svc.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": [{"deleteDimension": {"range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": row_num - 1, "endIndex": row_num}}}]},
    ).execute()
    return True


def find_inventory_row(rows, item_name):
    name = item_name.strip()
    for i, r in enumerate(rows):
        if r and r[0].strip() == name:
            return i
    for i, r in enumerate(rows):
        if r and name in r[0]:
            return i
    for i, r in enumerate(rows):
        if r and r[0].strip() and r[0].strip() in name:
            return i
    return -1


def update_inventory(svc, item_name, qty_delta, item_type="", notes=""):
    ensure_sheet(svc, S_INVENTORY)
    rows = read_sheet(svc, S_INVENTORY, "A2:D")
    values_api = svc.spreadsheets().values()
    i = find_inventory_row(rows, item_name)
    if i >= 0:
        r = rows[i]
        old_qty = int(float(r[2])) if len(r) > 2 and r[2] else 0
        new_qty = max(0, old_qty + int(qty_delta))
        row_num = i + 2
        values_api.update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{S_INVENTORY}!A{row_num}:D{row_num}",
            valueInputOption="USER_ENTERED",
            body={"values": [[r[0], r[1] if len(r) > 1 else item_type, new_qty, r[3] if len(r) > 3 else notes]]},
        ).execute()
        return new_qty
    if qty_delta > 0:
        values_api.append(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{S_INVENTORY}!A1:D1",
            valueInputOption="USER_ENTERED",
            body={"values": [[item_name, item_type, int(qty_delta), notes]]},
        ).execute()
        return int(qty_delta)
    return 0


def load_inventory(svc):
    rows = read_sheet(svc, S_INVENTORY, "A2:D")
    out = []
    for r in rows:
        if not r or not r[0]:
            continue
        try:
            qty = int(float(r[2])) if len(r) > 2 and r[2] else 0
        except Exception:
            qty = 0
        out.append({"item": r[0], "type": r[1] if len(r) > 1 else "", "qty": qty, "notes": r[3] if len(r) > 3 else ""})
    return out


def filter_by_period(data, period):
    now = datetime.now(UAE_TZ)
    if period == "today":
        key = now.strftime("%Y-%m-%d")
        return [x for x in data if x["date"].startswith(key)], "اليوم"
    if period == "week":
        start = (now - timedelta(days=6)).date()
        out = []
        for x in data:
            try:
                d = datetime.strptime(x["date"][:10], "%Y-%m-%d").date()
            except Exception:
                continue
            if start <= d <= now.date():
                out.append(x)
        return out, "آخر ٧ أيام"
    if period == "all":
        return data, "كل الفترة"
    key = now.strftime("%Y-%m")
    return [x for x in data if x["date"].startswith(key)], "هذا الشهر"


def report_text(svc, period):
    data, label = filter_by_period(load_transactions(svc), period)
    income = sum(x["amount"] for x in data if x["type"] == "دخل")
    expense = sum(x["amount"] for x in data if x["type"] == "صرف")
    net = income - expense
    by_category = {}
    for x in data:
        key = x["category"] or x["item"] or "غير محدد"
        signed = x["amount"] if x["type"] == "دخل" else -x["amount"]
        by_category[key] = by_category.get(key, 0) + signed
    lines = [D, f"📊 التقرير - {label}", f"الدخل: +{fmt(income)} درهم", f"المصروف: -{fmt(expense)} درهم", f"الصافي: {fmt(net)} درهم", D]
    if by_category:
        lines.append("حسب البند:")
        for k, v in sorted(by_category.items(), key=lambda kv: abs(kv[1]), reverse=True)[:10]:
            sign = "+" if v >= 0 else ""
            lines.append(f"{k}: {sign}{fmt(v)}")
        lines.append(D)
    return "\n".join(lines)


def menu_text():
    return "🌾 بوت مصاريف العزبة\nاختر العملية:"


def start_sale(svc, user_id, chat_id):
    set_state(svc, user_id, {"flow": "sale", "step": "item", "data": {"type": "دخل"}})
    send(chat_id, "💰 بيع\nاختر الشي اللي بعته:", SELL_ITEMS)


def start_purchase(svc, user_id, chat_id):
    set_state(svc, user_id, {"flow": "purchase", "step": "item", "data": {"type": "صرف"}})
    send(chat_id, "🛒 شراء\nاختر الشي اللي اشتريته:", BUY_ITEMS)


def start_fixed_expense(svc, user_id, chat_id, item, category):
    set_state(svc, user_id, {"flow": "expense", "step": "amount", "data": {"type": "صرف", "item": item, "category": category, "qty": 1}})
    send(chat_id, f"{item}\nاكتب المبلغ:", [["❌ إلغاء"]])


def ask_quantity(svc, user_id, chat_id, state):
    set_state(svc, user_id, state)
    send(chat_id, f"اكتب الكمية لـ {state['data']['item']}:", [["❌ إلغاء"]])


def ask_amount(svc, user_id, chat_id, state):
    set_state(svc, user_id, state)
    send(chat_id, "اكتب المبلغ الإجمالي بالدرهم:", [["❌ إلغاء"]])


def ask_payment(svc, user_id, chat_id, state):
    set_state(svc, user_id, state)
    send(chat_id, "اختر طريقة الدفع:", PAYMENT_METHODS)


def ask_notes(svc, user_id, chat_id, state):
    set_state(svc, user_id, state)
    send(chat_id, "اكتب ملاحظة اختيارية أو اضغط تخطي:", SKIP_MENU)


def confirmation_text(data):
    sign = "+" if data.get("type") == "دخل" else "-"
    lines = [
        D,
        "تأكيد العملية؟",
        f"النوع: {data.get('type')}",
        f"البند: {data.get('item')}",
        f"الكمية: {data.get('qty', 1)}",
        f"المبلغ: {sign}{fmt(data.get('amount', 0))} درهم",
        f"الدفع: {data.get('payment_method', '-')}",
        f"الملاحظة: {data.get('notes') or '-'}",
        D,
    ]
    return "\n".join(lines)


def ask_confirm(svc, user_id, chat_id, state):
    state["step"] = "confirm"
    set_state(svc, user_id, state)
    send(chat_id, confirmation_text(state["data"]), CONFIRM_MENU)


def item_type_for_inventory(item):
    if item in {"ماعز", "غنم"}:
        return "مواشي"
    if item in {"دجاج", "صيصان"}:
        return "دواجن"
    if item == "بيض":
        return "منتجات"
    return "عام"


def should_update_inventory(item):
    return item in {"ماعز", "غنم", "دجاج", "صيصان", "بيض"}


def save_flow(svc, user_id, chat_id, user_name, state):
    data = state.get("data", {})
    ttype = data.get("type")
    item = data.get("item", "")
    category = data.get("category") or item
    amount = float(data.get("amount") or 0)
    qty = int(data.get("qty") or 1)
    payment = data.get("payment_method") or ""
    notes = data.get("notes") or ""
    full_item = item
    if qty > 1 and item not in {"فاتورة كهرباء", "عمالة"}:
        full_item = f"{item} × {qty}"
    if payment or notes:
        extra = " | ".join([x for x in [payment, notes] if x])
        full_item = f"{full_item} ({extra})"
    add_transaction(svc, ttype, full_item, category, amount, user_name)
    add_pending(svc, user_id, "transaction", state.get("flow", "menu"), item, amount, qty, user_name, notes)
    if should_update_inventory(item):
        delta = qty if ttype == "صرف" else -qty
        update_inventory(svc, item, delta, item_type_for_inventory(item), notes)
    clear_state(svc, user_id)
    sign = "+" if ttype == "دخل" else "-"
    send(chat_id, f"{D}\n✅ تم التسجيل\nالبند: {item}\nالكمية: {qty}\nالمبلغ: {sign}{fmt(amount)} درهم\n{D}", MAIN_MENU)


def handle_flow(svc, user_id, chat_id, user_name, text, state):
    if text in CANCEL_WORDS:
        clear_state(svc, user_id)
        send(chat_id, "تم إلغاء العملية.", MAIN_MENU)
        return
    if text in BACK_WORDS:
        clear_state(svc, user_id)
        send(chat_id, menu_text(), MAIN_MENU)
        return
    step = state.get("step")
    data = state.setdefault("data", {})
    flow = state.get("flow")

    if step == "item":
        item = clean_label(text)
        if item == "أخرى":
            state["step"] = "custom_item"
            set_state(svc, user_id, state)
            send(chat_id, "اكتب اسم البند:", [["❌ إلغاء"]])
            return
        data["item"] = item
        data["category"] = item
        state["step"] = "quantity"
        ask_quantity(svc, user_id, chat_id, state)
        return

    if step == "custom_item":
        item = clean_label(text)
        if not item:
            send(chat_id, "اكتب اسم واضح للبند:", [["❌ إلغاء"]])
            return
        data["item"] = item
        data["category"] = item
        state["step"] = "quantity"
        ask_quantity(svc, user_id, chat_id, state)
        return

    if step == "quantity":
        qty = normalize_qty(text)
        if qty <= 0:
            send(chat_id, "اكتب الكمية رقم فقط. مثال: 12", [["❌ إلغاء"]])
            return
        data["qty"] = qty
        state["step"] = "amount"
        ask_amount(svc, user_id, chat_id, state)
        return

    if step == "amount":
        amount = normalize_amount(text)
        if amount <= 0:
            send(chat_id, "اكتب المبلغ رقم فقط. مثال: 250", [["❌ إلغاء"]])
            return
        data["amount"] = amount
        state["step"] = "payment"
        ask_payment(svc, user_id, chat_id, state)
        return

    if step == "payment":
        data["payment_method"] = clean_label(text)
        state["step"] = "notes"
        ask_notes(svc, user_id, chat_id, state)
        return

    if step == "notes":
        data["notes"] = "" if text in SKIP_WORDS else text.strip()
        ask_confirm(svc, user_id, chat_id, state)
        return

    if step == "confirm":
        if text in CONFIRM_WORDS:
            save_flow(svc, user_id, chat_id, user_name, state)
            return
        clear_state(svc, user_id)
        send(chat_id, "تم إلغاء العملية.", MAIN_MENU)
        return

    clear_state(svc, user_id)
    send(chat_id, menu_text(), MAIN_MENU)


def handle_report_choice(svc, user_id, chat_id, text):
    mapping = {
        "📆 تقرير اليوم": "today",
        "🗓 تقرير الأسبوع": "week",
        "📅 تقرير الشهر": "month",
        "📊 تقرير كامل": "all",
    }
    period = mapping.get(text)
    if not period:
        set_state(svc, user_id, {"flow": "report", "step": "choose", "data": {}})
        send(chat_id, "اختر نوع التقرير:", REPORT_MENU)
        return
    clear_state(svc, user_id)
    send(chat_id, report_text(svc, period), MAIN_MENU)


def send_inventory(svc, chat_id):
    inv = load_inventory(svc)
    if not inv:
        send(chat_id, "📦 الجرد فاضي", MAIN_MENU)
        return
    lines = [D, "📦 الجرد الحالي"]
    for x in inv:
        lines.append(f"{x['item']}: {x['qty']}")
    lines.append(D)
    send(chat_id, "\n".join(lines), MAIN_MENU)


def send_last(svc, chat_id):
    data = sorted(load_transactions(svc), key=lambda x: x["row"], reverse=True)[:7]
    if not data:
        send(chat_id, "ما في عمليات مسجلة", MAIN_MENU)
        return
    lines = [D, "🕐 آخر العمليات"]
    for t in data:
        sign = "+" if t["type"] == "دخل" else "-"
        lines.append(f"{t['date'][:10]} | {sign}{fmt(t['amount'])} | {t['item']}")
    lines.append(D)
    send(chat_id, "\n".join(lines), MAIN_MENU)


def undo_last(svc, chat_id, user_name):
    data = [x for x in load_transactions(svc) if x.get("user") == user_name]
    if not data:
        send(chat_id, "ما في عملية سابقة للتراجع.", MAIN_MENU)
        return
    last = sorted(data, key=lambda x: x["row"])[-1]
    ok = delete_transaction_row(svc, last["row"])
    if ok:
        sign = "+" if last["type"] == "دخل" else "-"
        send(chat_id, f"✅ تم حذف آخر عملية\n{last['item']} | {sign}{fmt(last['amount'])} درهم", MAIN_MENU)
    else:
        send(chat_id, "ما قدرت أحذف آخر عملية.", MAIN_MENU)


HELP = """
🌾 بوت مصاريف العزبة

استخدم الأزرار بدل الكتابة لتقليل الأخطاء:
• بيع
• شراء
• فاتورة كهرباء
• عمالة
• الجرد
• التقرير

الأوامر:
/start أو /menu لفتح القائمة
/cancel لإلغاء العملية الحالية
/help للمساعدة
""".strip()


class handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _ok(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_GET(self):
        self._ok()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            update = json.loads(raw)
        except Exception:
            self._ok()
            return

        msg = update.get("message") or {}
        text = (msg.get("text") or "").strip()
        if not text:
            self._ok()
            return

        chat_id = msg.get("chat", {}).get("id")
        user_id = msg.get("from", {}).get("id")
        if user_id not in ALLOWED_USERS:
            send(chat_id, "⛔ هذا البوت خاص")
            self._ok()
            return

        user_name = ALLOWED_USERS[user_id]

        try:
            svc = sheets_svc()
        except Exception as e:
            send(chat_id, f"في مشكلة بـ Google Sheets:\n{e}")
            self._ok()
            return

        if text in ("/start", "/menu", "menu", "القائمة", "مساعدة", "/help", "help"):
            clear_state(svc, user_id)
            send(chat_id, HELP if text in ("/help", "help", "مساعدة") else menu_text(), MAIN_MENU)
            self._ok()
            return

        if text in CANCEL_WORDS:
            clear_state(svc, user_id)
            send(chat_id, "تم إلغاء العملية.", MAIN_MENU)
            self._ok()
            return

        state = get_state(svc, user_id)
        if state.get("flow") == "report":
            handle_report_choice(svc, user_id, chat_id, text)
            self._ok()
            return
        if state.get("flow"):
            handle_flow(svc, user_id, chat_id, user_name, text, state)
            self._ok()
            return

        if text == "💰 بيع":
            start_sale(svc, user_id, chat_id)
        elif text == "🛒 شراء":
            start_purchase(svc, user_id, chat_id)
        elif text == "⚡ فاتورة كهرباء":
            start_fixed_expense(svc, user_id, chat_id, "فاتورة كهرباء", "كهرباء")
        elif text == "👷 عمالة":
            start_fixed_expense(svc, user_id, chat_id, "عمالة", "رواتب")
        elif text == "📦 الجرد":
            send_inventory(svc, chat_id)
        elif text == "📊 التقرير":
            set_state(svc, user_id, {"flow": "report", "step": "choose", "data": {}})
            send(chat_id, "اختر نوع التقرير:", REPORT_MENU)
        elif text == "🕐 آخر العمليات":
            send_last(svc, chat_id)
        elif text == "↩️ تراجع آخر عملية":
            undo_last(svc, chat_id, user_name)
        else:
            send(chat_id, menu_text(), MAIN_MENU)

        self._ok()
