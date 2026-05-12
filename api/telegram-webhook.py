# api/telegram-webhook.py

from http.server import BaseHTTPRequestHandler
import json
import os
from datetime import datetime, timezone, timedelta, date
import requests
from openai import OpenAI
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ================== CONFIG ======================

TELEGRAM_BOT_TOKEN          = os.environ.get("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY              = os.environ.get("OPENAI_API_KEY")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
SPREADSHEET_ID              = os.environ.get("SPREADSHEET_ID")

ALLOWED_USERS = {
    47329648:   "Khaled",
    6894180427: "Hamad",
}

UAE_TZ = timezone(timedelta(hours=4))
openai_client = OpenAI(api_key=OPENAI_API_KEY)

S_TRANSACTIONS = "Transactions"
S_INVENTORY    = "Inventory"
S_PENDING      = "Pending"
S_CONVERSATION = "Conversation"  # NEW: Store conversation context

D = "──────────────"

# ================== CONVERSATION MEMORY =========

# In-memory conversation context (per user)
# Format: {user_id: {"last_intent": "...", "last_message": "...", "waiting_for": "...", "context": {...}}}
conversation_state = {}

def get_context(user_id):
    """Get user's conversation context"""
    return conversation_state.get(user_id, {})

def set_context(user_id, last_intent=None, last_message=None, waiting_for=None, context=None):
    """Save user's conversation context"""
    if user_id not in conversation_state:
        conversation_state[user_id] = {}
    
    if last_intent is not None:
        conversation_state[user_id]["last_intent"] = last_intent
    if last_message is not None:
        conversation_state[user_id]["last_message"] = last_message
    if waiting_for is not None:
        conversation_state[user_id]["waiting_for"] = waiting_for
    if context is not None:
        conversation_state[user_id]["context"] = context

def clear_context(user_id):
    """Clear user's conversation context"""
    if user_id in conversation_state:
        del conversation_state[user_id]

# ================== TELEGRAM ====================

def send(chat_id, text):
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
    except Exception:
        pass

# ================== GOOGLE SHEETS ===============

def sheets_svc():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds)

def read_sheet(svc, sheet, rng="A2:Z"):
    res = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet}!{rng}",
    ).execute()
    return res.get("values", [])

def append_row(svc, sheet, row):
    svc.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [row]},
    ).execute()

def find_inventory_row(rows, item_name):
    """
    Find the best matching row index for item_name.
    1. Exact match
    2. item_name contains the row item (e.g. "غنم حري" found in "غنم حري، غنم نعيمي")
    3. Row item contains item_name
    Returns index in rows list (0-based), or -1 if not found.
    """
    name = item_name.strip()
    # Pass 1: exact
    for i, r in enumerate(rows):
        if r and r[0].strip() == name:
            return i
    # Pass 2: row cell contains our name as substring
    for i, r in enumerate(rows):
        if r and name in r[0]:
            return i
    # Pass 3: our name contains the row cell (e.g. searching "غنم حري" when cell is "غنم")
    for i, r in enumerate(rows):
        if r and r[0].strip() and r[0].strip() in name:
            return i
    return -1

def update_inventory(svc, item_name, qty_delta, item_type="", notes=""):
    rows = read_sheet(svc, S_INVENTORY)
    values_api = svc.spreadsheets().values()

    i = find_inventory_row(rows, item_name)
    if i >= 0:
        r = rows[i]
        old_qty = int(r[2]) if len(r) > 2 and r[2] else 0
        new_qty = max(0, old_qty + int(qty_delta))
        row_num = i + 2  # +1 for header, +1 for 1-based
        values_api.update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{S_INVENTORY}!A{row_num}:D{row_num}",
            valueInputOption="USER_ENTERED",
            body={"values": [[r[0], r[1] if len(r) > 1 else item_type, new_qty, r[3] if len(r) > 3 else notes]]},
        ).execute()
        return

    if qty_delta > 0:
        values_api.append(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{S_INVENTORY}!A1:D1",
            valueInputOption="USER_ENTERED",
            body={"values": [[item_name, item_type, int(qty_delta), notes]]},
        ).execute()

# ================== UTILS =======================

def now_str():
    return datetime.now(UAE_TZ).strftime("%Y-%m-%d %H:%M")

def today_str():
    return datetime.now(UAE_TZ).strftime("%Y-%m-%d")

def cur_month_key():
    return datetime.now(UAE_TZ).strftime("%Y-%m")

def fmt(x):
    try:
        f = float(x)
        return int(f) if f.is_integer() else round(f, 2)
    except Exception:
        return x

def filter_by_period(data, period):
    if not period:
        period = "month"

    now = datetime.now(UAE_TZ)

    if period == "all":
        return data, "لكل الفترة المسجلة"

    if period == "today":
        key = now.strftime("%Y-%m-%d")
        return [x for x in data if x["date"].startswith(key)], f"اليوم {key}"

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
        return out, f"آخر ٧ أيام"

    key = now.strftime("%Y-%m")
    return [x for x in data if x["date"].startswith(key)], "هذا الشهر"

def split_animals_for_inventory(animal_str, qty):
    s = (animal_str or "").strip()
    if not s:
        return [("غنم", qty)]

    s = s.replace(" و", "،").replace("، و", "،")
    parts = [p.strip() for p in s.split("،") if p.strip()]
    if not parts:
        return [("غنم", qty)]

    if len(parts) == 1:
        return [(parts[0], qty)]

    base = qty // len(parts)
    extra = qty % len(parts)
    out = []
    for i, p in enumerate(parts):
        q = base + (1 if i < extra else 0)
        out.append((p, q))
    return out

# ================== TRANSACTIONS ===============

def load_transactions(svc):
    rows = read_sheet(svc, S_TRANSACTIONS)
    out = []
    for r in rows:
        if len(r) < 5:
            continue
        try:
            amount = float(r[4])
        except Exception:
            continue
        out.append({
            "date":     r[0],
            "type":     r[1],
            "item":     r[2],
            "category": r[3] if len(r) > 3 else "",
            "amount":   amount,
            "user":     r[5] if len(r) > 5 else "",
        })
    return out

def add_transaction(svc, ttype, item, category, amount, user):
    append_row(svc, S_TRANSACTIONS, [now_str(), ttype, item, category, amount, user])

def totals_all(data):
    inc = sum(x["amount"] for x in data if x["type"] == "دخل")
    exp = sum(x["amount"] for x in data if x["type"] == "صرف")
    return inc, exp

# ================== INVENTORY ==================

def load_inventory(svc):
    rows = read_sheet(svc, S_INVENTORY)
    out = []
    for r in rows:
        if not r or not r[0]:
            continue
        out.append({
            "item":  r[0],
            "type":  r[1] if len(r) > 1 else "",
            "qty":   int(r[2]) if len(r) > 2 and r[2] else 0,
            "notes": r[3] if len(r) > 3 else "",
        })
    return out

# ================== PENDING ====================

def add_pending(svc, user_id, op_type, action, item, amount, qty, person, notes=""):
    append_row(svc, S_PENDING, [
        str(user_id), now_str(), op_type, action, item, amount, qty, person, notes,
    ])

# ================== AI INTENT ==================

SYSTEM_PROMPT = """
أنت مساعد ذكي لإدارة عزبة/مزرعة في الإمارات. مهمتك فهم رسائل المستخدم باللهجة الإماراتية/الخليجية والعربية الفصحى واستخراج النية والبيانات.

أرجع JSON فقط بدون أي نص آخر:

{
  "intent": "add_income | add_expense | add_livestock | sell_livestock | add_poultry | sell_poultry | pay_salary | income_total | expense_total | profit | inventory | last_transactions | income_by_item | income_breakdown | smalltalk | clarify | incomplete_purchase | incomplete_sale | incomplete_info | ambiguous_sale | ambiguous_purchase | ambiguous_expense",
  "item": "",
  "category": "",
  "amount": 0,
  "quantity": 0,
  "animal_type": "",
  "worker_name": "",
  "period": "today | week | month | all",
  "inventory_item": "",
  "needs_clarification": "item | amount | quantity | details",
  "suggested_question": ""
}

════════════════════════════════════════════════════════════════════

🔄 NEW: INCOMPLETE MESSAGES HANDLING

إذا قال المستخدم جملة ناقصة، أرجع intent مناسب مع needs_clarification:

incomplete_purchase (شراء ناقص):
- "اشتريت" / "شريت" / "جبت" / "جبنا" بدون تفاصيل
- أمثلة:
  * "اشتريت" → intent=incomplete_purchase, needs_clarification="item", suggested_question="شو اشتريت؟"
  * "جبنا" → intent=incomplete_purchase, needs_clarification="item", suggested_question="شو جبتو؟"

incomplete_sale (بيع ناقص):
- "بعت" / "بعنا" بدون تفاصيل (ليس "البيع" أو "المبيعات" لأنها غامضة)
- أمثلة:
  * "بعت" → intent=incomplete_sale, needs_clarification="item", suggested_question="شو بعت؟"
  * "بعنا" → intent=incomplete_sale, needs_clarification="item", suggested_question="شو بعتو؟"

incomplete_info (معلومات ناقصة):
- ذكر البند لكن بدون مبلغ أو عدد
- أمثلة:
  * "بعت بيض" (بدون مبلغ) → intent=incomplete_info, item="بيض", needs_clarification="amount", suggested_question="بكم؟"
  * "جبنا غنم" (بدون عدد) → intent=incomplete_info, animal_type="غنم", needs_clarification="quantity", suggested_question="كم عدد؟"

🔀 NEW: AMBIGUOUS INTENTS (كلمات لها أكثر من معنى):

ambiguous_sale (البيع - غير واضح):
- "البيع" أو "المبيعات" فقط بدون سياق
- ممكن يقصد: تسجيل بيع جديد أو عرض إجمالي المبيعات
- أمثلة:
  * "البيع" → intent=ambiguous_sale, suggested_question="تبي تسجل بيع ولا تشوف المبيعات؟"
  * "المبيعات" → intent=ambiguous_sale, suggested_question="تبي تسجل بيع ولا تشوف المبيعات؟"

ambiguous_purchase (الشراء - غير واضح):
- "الشراء" أو "المشتريات" فقط بدون سياق
- ممكن يقصد: تسجيل شراء جديد أو عرض إجمالي المشتريات
- أمثلة:
  * "الشراء" → intent=ambiguous_purchase, suggested_question="تبي تسجل شراء ولا تشوف المشتريات؟"
  * "المشتريات" → intent=ambiguous_purchase, suggested_question="تبي تسجل شراء ولا تشوف المشتريات؟"

ambiguous_expense (المصروف - غير واضح):
- "المصروف" أو "المصاريف" فقط بدون سياق
- ممكن يقصد: تسجيل مصروف جديد أو عرض إجمالي المصاريف
- أمثلة:
  * "المصروف" → intent=ambiguous_expense, suggested_question="تبي تسجل مصروف ولا تشوف المصاريف؟"
  * "المصاريف" → intent=ambiguous_expense, suggested_question="تبي تسجل مصروف ولا تشوف المصاريف؟"

⚠️ ملاحظة مهمة: الكلمات الواضحة تبقى زي ما هي:
- "بعت" → واضح إنه تسجيل بيع (incomplete_sale)
- "كم المبيعات" / "شو المبيعات" → واضح إنه استعلام (income_total)
- "اشتريت" → واضح إنه تسجيل شراء (incomplete_purchase)
- "كم المصروف" → واضح إنه استعلام (expense_total)

════════════════════════════════════════════════════════════════════

📌 قواعد تحديد النية (Intent Rules):

دخل (add_income):
- بيع أي منتج ليس حيوان حي: بيض، بيظ، لبن، حليب، صوف، زبدة، جبن، خضار، محاصيل
- كلمات مفتاحية: "بعت"، "بعنا"، "وردنا"، "دخل"، "إيراد"، "استلمنا"، "بيع"
- أمثلة:
  * "بعت بيض بـ 200" → intent=add_income, item="بيض", amount=200
  * "وردنا من اللبن 150 درهم" → intent=add_income, item="لبن", amount=150
  * "بعنا البيظ ب ٣٠٠" → intent=add_income, item="بيض", amount=300
- ⚠️ مهم: إذا كان البيع حيوان حي (غنم، بقر، إبل، ناقة) → ليس add_income بل sell_livestock

مواشي-شراء (add_livestock):
- شراء حيوانات كبيرة: غنم، خرفان، أغنام، بقر، عجول، إبل، جمال، نوق، حمير، خيل
- كلمات: "اشترينا"، "جبنا"، "شرينا"، "وصل لنا"، "جاء"، "اشتريت"
- أمثلة:
  * "اشترينا ٥ غنم حري" → intent=add_livestock, animal_type="غنم حري", quantity=5
  * "جبنا بقر عدد 2" → intent=add_livestock, animal_type="بقر", quantity=2
  * "وصلنا ٣ خرفان" → intent=add_livestock, animal_type="غنم", quantity=3
- animal_type = نوع الحيوان بالتفصيل، quantity = العدد، amount = التكلفة إن ذكرت

مواشي-بيع (sell_livestock):
- بيع حيوانات كبيرة: غنم، خرفان، بقر، إبل، ناقة، جمل
- كلمات: "بعنا"، "بيع"، "تم بيع"، "بعت"، "بعنا غنم"
- ⚠️ قاعدة ذهبية: أي جملة فيها (غنم أو بقر أو إبل) + (بيع أو بعت أو بعنا) → دائماً sell_livestock وليس add_income
- أمثلة:
  * "بعنا غنم عدد 2" → intent=sell_livestock, animal_type="غنم", quantity=2
  * "تم بيع غنم عدد 2 واحد حري وواحد نعيمي بمبلغ 1510" → intent=sell_livestock, animal_type="غنم حري،غنم نعيمي", quantity=2, amount=1510
  * "بعت ٣ خرفان بـ ٢٥٠٠" → intent=sell_livestock, animal_type="غنم", quantity=3, amount=2500
- إذا ذكر أكثر من نوع، افصل بفاصلة في animal_type مثل: "غنم حري،غنم نعيمي"

دواجن-شراء (add_poultry):
- شراء دواجن صغيرة: دجاج، فراخ، طيور، حمام، بط، ديك رومي، رومي
- كلمات: "اشترينا دجاج"، "جبنا فراخ"، "شرينا طيور"
- أمثلة:
  * "اشترينا ١٠ دجاج" → intent=add_poultry, animal_type="دجاج", quantity=10
  * "جبنا فراخ عدد ٢٠ بـ ٥٠٠" → intent=add_poultry, animal_type="دجاج", quantity=20, amount=500

دواجن-بيع (sell_poultry):
- بيع دواجن حية (ليس بيض)
- كلمات: "بعت دجاج"، "بعنا فراخ"، "بيع طيور"
- ملاحظة: "بعت بيض" → add_income وليس sell_poultry
- أمثلة:
  * "بعت دجاج ٣ بـ ٧٥" → intent=sell_poultry, animal_type="دجاج", quantity=3, amount=75
  * "بعنا ٥ فراخ" → intent=sell_poultry, animal_type="دجاج", quantity=5

مصروف (add_expense):
- أي صرف ليس شراء حيوان ولا راتب
- بنود: أعلاف، علف، دواء، أدوية، كهرباء، كهربا، وقود، بنزين، ديزل، صيانة، مستلزمات، ماء، مويه
- كلمات: "صرفنا"، "اشترينا"، "دفعنا"، "فاتورة"، "دفعت"
- ⚠️ ملاحظة: "المصروف" أو "المصاريف" وحدها → ambiguous_expense
- أمثلة:
  * "صرفنا على الأعلاف ٨٠٠" → intent=add_expense, item="أعلاف", amount=800
  * "اشترينا دواء بـ ١٥٠" → intent=add_expense, item="دواء", amount=150
  * "دفعنا فاتورة الكهربا ٤٥٠" → intent=add_expense, item="كهرباء", amount=450

راتب (pay_salary):
- راتب أو معاش عامل أو شغال
- كلمات: "راتب"، "معاش"، "أجرة"، "مرتب"
- أمثلة:
  * "راتب العامل ١٤٠٠" → intent=pay_salary, worker_name="العامل", amount=1400
  * "معاش أحمد ١٢٠٠" → intent=pay_salary, worker_name="أحمد", amount=1200
  * "أجرة الشغال ١٠٠٠" → intent=pay_salary, worker_name="الشغال", amount=1000

════════════════════════════════════════════════════════════════════

📊 استعلامات (Queries):

- "كم الدخل" / "كم دخلنا" / "إجمالي الدخل" / "شو الدخل" → income_total
- "كم صرفنا" / "كم المصاريف" / "إجمالي المصروف" / "شو المصروف" → expense_total
- "كم الربح" / "الصافي" / "كم ربحنا" / "شو الربح" / "كم الصافي" → profit
- "الجرد" / "ورني الجرد" / "كم عدد الغنم" / "كم الدواجن" / "شو عندنا" / "ش عندنا" → inventory
- "آخر العمليات" / "آخر المعاملات" / "ورني آخر شي" → last_transactions
- "كم دخل البيض" / "كم دخل الغنم" / "ربح البيض" → income_by_item
- "قسم الدخل" / "توزيع الدخل" / "فصل الدخل" / "قسم لي الدخل" → income_breakdown
- حديث عام / سؤال عن البوت / تحية → smalltalk
- غير واضح / لا تفهم → clarify

════════════════════════════════════════════════════════════════════

📅 الفترة الزمنية (Period):

- "اليوم" / "اليومة" / "today" → "today"
- "هالأسبوع" / "الأسبوع" / "آخر أسبوع" / "هذا الأسبوع" → "week"
- "هالشهر" / "الشهر" / "هذا الشهر" / "this month" → "month"
- "إجمالي" / "الكلي" / "كل شي" / "من الأول" / "من زمان" / "all" → "all"
- افتراضي (بدون فترة محددة) → "month"

════════════════════════════════════════════════════════════════════

🔢 قواعد استخراج البيانات:

1. amount: استخرج الرقم بأي شكل:
   - "بمبلغ 500" → 500
   - "بـ 500" → 500
   - "500 درهم" → 500
   - "٥٠٠" → 500 (أرقام عربية تحول لإنجليزية)

2. quantity: 
   - "عدد 5" → 5
   - "٥ غنم" → 5
   - إذا لم يذكر عدد صراحة → 1

3. إذا قال "عدد 2 واحد حري وواحد نعيمي":
   - quantity = 2
   - animal_type = "غنم حري،غنم نعيمي"

4. مرادفات شائعة (Synonyms):
   - دجاج = فراخ = طيور = دواجن
   - غنم = خرفان = أغنام = خراف
   - بيض = بيظ
   - لبن = حليب
   - كهرباء = كهربا
   - ماء = مويه
   - شو = ش = كم = وش
   - جبنا = اشترينا = شرينا
   - بعنا = بعت = بيع

الآن افهم رسالة المستخدم وأرجع JSON فقط:
"""

CONTEXT_PROMPT = """
السياق من الرسالة السابقة:
- الرسالة السابقة: {last_message}
- النية السابقة: {last_intent}
- ننتظر معلومات: {waiting_for}
- سياق إضافي: {context}

الرسالة الحالية: {current_message}

إذا كانت الرسالة الحالية إجابة على سؤال من السياق السابق، استخدم السياق لفهمها.

مثال:
- سابقاً: "اشتريت" → سألناه "شو اشتريت؟"
- الآن: "غنم" → يعني add_livestock, animal_type="غنم"
- الآن: "علف" → يعني add_expense, item="علف"

مثال 2:
- سابقاً: "بعت بيض" (بدون مبلغ) → سألناه "بكم؟"
- الآن: "٢٠٠" → يعني add_income, item="بيض", amount=200

افهم الرسالة مع السياق وأرجع JSON:
"""

def detect_intent(text, user_id=None):
    try:
        # Check if there's conversation context
        ctx = get_context(user_id) if user_id else {}
        
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        
        # If waiting for follow-up, include context
        if ctx.get("waiting_for"):
            # Special handling for ambiguous intent clarification
            if ctx.get("waiting_for") == "clarification":
                text_lower = text.strip().lower()
                prev_type = ctx.get("context", {}).get("type")
                
                # Check if user wants to record or view
                record_keywords = ["تسجيل", "سجل", "أسجل", "اسجل", "نسجل"]
                view_keywords = ["شوف", "أشوف", "اشوف", "عرض", "إجمالي", "اجمالي", "كم"]
                
                is_record = any(kw in text_lower for kw in record_keywords)
                is_view = any(kw in text_lower for kw in view_keywords)
                
                if is_record and prev_type == "sale":
                    clear_context(user_id)
                    return {"intent": "incomplete_sale"}
                elif is_view and prev_type == "sale":
                    clear_context(user_id)
                    return {"intent": "income_total", "period": "month"}
                elif is_record and prev_type == "purchase":
                    clear_context(user_id)
                    return {"intent": "incomplete_purchase"}
                elif is_view and prev_type == "purchase":
                    clear_context(user_id)
                    return {"intent": "expense_total", "period": "month"}
                elif is_record and prev_type == "expense":
                    clear_context(user_id)
                    return {"intent": "incomplete_info", "needs_clarification": "item"}
                elif is_view and prev_type == "expense":
                    clear_context(user_id)
                    return {"intent": "expense_total", "period": "month"}
            
            context_info = CONTEXT_PROMPT.format(
                last_message=ctx.get("last_message", ""),
                last_intent=ctx.get("last_intent", ""),
                waiting_for=ctx.get("waiting_for", ""),
                context=json.dumps(ctx.get("context", {}), ensure_ascii=False),
                current_message=text
            )
            messages.append({"role": "user", "content": context_info})
        else:
            messages.append({"role": "user", "content": text})
        
        completion = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            response_format={"type": "json_object"},
            messages=messages,
        )
        result = json.loads(completion.choices[0].message.content)
        
        # If we had context and got a result, merge context data
        if ctx.get("context") and result.get("intent") not in ["incomplete_purchase", "incomplete_sale", "incomplete_info", "clarify", "ambiguous_sale", "ambiguous_purchase", "ambiguous_expense"]:
            # Merge previous context into current result
            prev_ctx = ctx.get("context", {})
            if prev_ctx.get("item") and not result.get("item"):
                result["item"] = prev_ctx["item"]
            if prev_ctx.get("animal_type") and not result.get("animal_type"):
                result["animal_type"] = prev_ctx["animal_type"]
            if prev_ctx.get("action"):
                # If previous action was purchase and user just gave item name
                if prev_ctx["action"] == "purchase" and result.get("intent") in ["clarify", "smalltalk"]:
                    # User probably just said the item name
                    item = text.strip()
                    # Detect if it's livestock or expense
                    livestock_keywords = ["غنم", "خرفان", "بقر", "عجول", "إبل", "جمال", "ناقة"]
                    poultry_keywords = ["دجاج", "فراخ", "طيور", "حمام", "بط"]
                    
                    if any(kw in item for kw in livestock_keywords):
                        result = {"intent": "add_livestock", "animal_type": item, "quantity": 1}
                    elif any(kw in item for kw in poultry_keywords):
                        result = {"intent": "add_poultry", "animal_type": item, "quantity": 1}
                    else:
                        result = {"intent": "add_expense", "item": item}
                
                elif prev_ctx["action"] == "sale" and result.get("intent") in ["clarify", "smalltalk"]:
                    # User probably just said the item name for sale
                    item = text.strip()
                    livestock_keywords = ["غنم", "خرفان", "بقر", "عجول", "إبل", "جمال", "ناقة"]
                    poultry_keywords = ["دجاج", "فراخ", "طيور", "حمام", "بط"]
                    
                    if any(kw in item for kw in livestock_keywords):
                        result = {"intent": "sell_livestock", "animal_type": item, "quantity": 1}
                    elif any(kw in item for kw in poultry_keywords):
                        result = {"intent": "sell_poultry", "animal_type": item, "quantity": 1}
                    else:
                        result = {"intent": "add_income", "item": item}
        
        return result
    except Exception as e:
        return {"intent": "clarify", "_error": str(e)}

# ================== HELPERS ====================

def resolve_item(d):
    """
    FIX: Extract item from any available field.
    GPT sometimes puts the value in animal_type or category instead of item.
    """
    return (
        (d.get("item") or "").strip()
        or (d.get("animal_type") or "").strip()
        or (d.get("category") or "").strip()
        or ""
    )

# ================== HANDLERS ====================

def h_add_income(svc, d, chat_id, user_name, user_id):
    # FIX: use resolve_item to avoid empty item when GPT puts value in animal_type
    item     = resolve_item(d)
    amount   = float(d.get("amount") or 0)
    category = (d.get("category") or item or "").strip()

    if not item:
        send(chat_id, "شو البند اللي بعته؟")
        set_context(user_id, last_intent="add_income", last_message=item, waiting_for="item", 
                   context={"action": "income", "amount": amount})
        return
    
    if amount <= 0:
        send(chat_id, "بكم بعته؟")
        set_context(user_id, last_intent="add_income", last_message=item, waiting_for="amount",
                   context={"action": "income", "item": item})
        return

    add_transaction(svc, "دخل", item, category, amount, user_name)
    add_pending(svc, user_id, "transaction", "add_income", item, amount, 0, user_name)
    
    clear_context(user_id)  # Clear after successful transaction

    send(chat_id,
        f"{D}\n✅ دخل مسجل\n"
        f"البند: {item}\n"
        f"المبلغ: {fmt(amount)} د.إ\n"
        f"بواسطة: {user_name}\n"
        f"{D}"
    )

def h_add_expense(svc, d, chat_id, user_name, user_id):
    # FIX: use resolve_item
    item     = resolve_item(d)
    amount   = float(d.get("amount") or 0)
    category = (d.get("category") or item or "").strip()

    if not item:
        send(chat_id, "شو اشتريت؟")
        set_context(user_id, last_intent="add_expense", last_message="", waiting_for="item",
                   context={"action": "expense", "amount": amount})
        return
    
    if amount <= 0:
        send(chat_id, f"بكم اشتريت {item}؟")
        set_context(user_id, last_intent="add_expense", last_message=item, waiting_for="amount",
                   context={"action": "expense", "item": item})
        return

    add_transaction(svc, "صرف", item, category, amount, user_name)
    add_pending(svc, user_id, "transaction", "add_expense", item, amount, 0, user_name)
    
    clear_context(user_id)

    send(chat_id,
        f"{D}\n✅ صرف مسجل\n"
        f"البند: {item}\n"
        f"المبلغ: {fmt(amount)} د.إ\n"
        f"بواسطة: {user_name}\n"
        f"{D}"
    )

def h_add_livestock(svc, d, chat_id, user_name, user_id):
    animal = (d.get("animal_type") or d.get("item") or "").strip()
    qty    = int(d.get("quantity") or 1)
    cost   = float(d.get("amount") or 0)

    if not animal:
        send(chat_id, "شو نوع المواشي اللي جبتها؟")
        set_context(user_id, last_intent="add_livestock", last_message="", waiting_for="animal_type",
                   context={"action": "purchase", "type": "livestock", "quantity": qty, "amount": cost})
        return
    
    if qty <= 0:
        send(chat_id, f"كم عدد {animal}؟")
        set_context(user_id, last_intent="add_livestock", last_message=animal, waiting_for="quantity",
                   context={"action": "purchase", "type": "livestock", "animal_type": animal, "amount": cost})
        return

    update_inventory(svc, animal, qty, item_type="مواشي")
    if cost > 0:
        add_transaction(svc, "صرف", f"شراء {qty} {animal}", "مواشي", cost, user_name)

    add_pending(svc, user_id, "inventory", "add_livestock", animal, cost, qty, user_name)
    
    clear_context(user_id)

    inv = load_inventory(svc)
    current_qty = next((x["qty"] for x in inv if x["item"] == animal), qty)

    send(chat_id,
        f"{D}\n✅ تم إضافة المواشي\n"
        f"النوع: {animal} × {qty}\n"
        f"التكلفة: {fmt(cost)} د.إ\n"
        f"{D}\n"
        f"🐄 رصيد {animal} الحالي: {current_qty}\n"
        f"{D}"
    )

def h_sell_livestock(svc, d, chat_id, user_name, user_id):
    animal_raw = (d.get("animal_type") or d.get("item") or "").strip()
    qty        = int(d.get("quantity") or 1)
    price      = float(d.get("amount") or 0)

    if not animal_raw:
        send(chat_id, "شو نوع المواشي اللي بعتها؟")
        set_context(user_id, last_intent="sell_livestock", last_message="", waiting_for="animal_type",
                   context={"action": "sale", "type": "livestock", "quantity": qty, "amount": price})
        return
    
    if qty <= 0:
        send(chat_id, f"كم عدد {animal_raw} بعت؟")
        set_context(user_id, last_intent="sell_livestock", last_message=animal_raw, waiting_for="quantity",
                   context={"action": "sale", "type": "livestock", "animal_type": animal_raw, "amount": price})
        return
    
    if price <= 0:
        send(chat_id, f"بكم بعت {qty} {animal_raw}؟")
        set_context(user_id, last_intent="sell_livestock", last_message=animal_raw, waiting_for="amount",
                   context={"action": "sale", "type": "livestock", "animal_type": animal_raw, "quantity": qty})
        return

    splits = split_animals_for_inventory(animal_raw, qty)
    for name, q in splits:
        update_inventory(svc, name, -q, item_type="مواشي")

    if price > 0:
        add_transaction(svc, "دخل", f"بيع {qty} {animal_raw}", "مواشي", price, user_name)

    add_pending(svc, user_id, "inventory", "sell_livestock", animal_raw, price, qty, user_name)
    
    clear_context(user_id)

    inv = load_inventory(svc)
    lines = [D, f"✅ تم تسجيل بيع المواشي\nالحيوان: {animal_raw} × {qty}\nالسعر: {fmt(price)} د.إ\nالرصيد الحالي:"]
    for name, _q in splits:
        current = next((x["qty"] for x in inv if x["item"] == name), 0)
        lines.append(f"  {name}: {current}")
    lines.append(D)
    send(chat_id, "\n".join(lines))

def h_add_poultry(svc, d, chat_id, user_name, user_id):
    bird = (d.get("animal_type") or d.get("item") or "").strip()
    qty  = int(d.get("quantity") or 1)
    cost = float(d.get("amount") or 0)

    if not bird:
        send(chat_id, "شو نوع الدواجن اللي جبتها؟")
        set_context(user_id, last_intent="add_poultry", last_message="", waiting_for="animal_type",
                   context={"action": "purchase", "type": "poultry", "quantity": qty, "amount": cost})
        return
    
    if qty <= 0:
        send(chat_id, f"كم عدد {bird}؟")
        set_context(user_id, last_intent="add_poultry", last_message=bird, waiting_for="quantity",
                   context={"action": "purchase", "type": "poultry", "animal_type": bird, "amount": cost})
        return

    update_inventory(svc, bird, qty, item_type="دواجن")
    if cost > 0:
        add_transaction(svc, "صرف", f"شراء {qty} {bird}", "دواجن", cost, user_name)

    add_pending(svc, user_id, "inventory", "add_poultry", bird, cost, qty, user_name)
    
    clear_context(user_id)

    inv = load_inventory(svc)
    current_qty = next((x["qty"] for x in inv if x["item"] == bird), qty)

    send(chat_id,
        f"{D}\n✅ تم إضافة الدواجن\n"
        f"النوع: {bird} × {qty}\n"
        f"التكلفة: {fmt(cost)} د.إ\n"
        f"{D}\n"
        f"🐔 رصيد {bird} الحالي: {current_qty}\n"
        f"{D}"
    )

def h_sell_poultry(svc, d, chat_id, user_name, user_id):
    bird  = (d.get("animal_type") or d.get("item") or "").strip()
    qty   = int(d.get("quantity") or 1)
    price = float(d.get("amount") or 0)

    if not bird:
        send(chat_id, "شو نوع الدواجن اللي بعتها؟")
        set_context(user_id, last_intent="sell_poultry", last_message="", waiting_for="animal_type",
                   context={"action": "sale", "type": "poultry", "quantity": qty, "amount": price})
        return
    
    if qty <= 0:
        send(chat_id, f"كم عدد {bird} بعت؟")
        set_context(user_id, last_intent="sell_poultry", last_message=bird, waiting_for="quantity",
                   context={"action": "sale", "type": "poultry", "animal_type": bird, "amount": price})
        return
    
    if price <= 0:
        send(chat_id, f"بكم بعت {qty} {bird}؟")
        set_context(user_id, last_intent="sell_poultry", last_message=bird, waiting_for="amount",
                   context={"action": "sale", "type": "poultry", "animal_type": bird, "quantity": qty})
        return

    update_inventory(svc, bird, -qty, item_type="دواجن")
    if price > 0:
        add_transaction(svc, "دخل", f"بيع {qty} {bird}", "دواجن", price, user_name)

    add_pending(svc, user_id, "inventory", "sell_poultry", bird, price, qty, user_name)
    
    clear_context(user_id)

    inv = load_inventory(svc)
    current_qty = next((x["qty"] for x in inv if x["item"] == bird), 0)

    send(chat_id,
        f"{D}\n✅ تم تسجيل بيع\n"
        f"الطير: {bird} × {qty}\n"
        f"السعر: {fmt(price)} د.إ\n"
        f"{D}\n"
        f"🐔 رصيد {bird} الحالي: {current_qty}\n"
        f"{D}"
    )

def h_pay_salary(svc, d, chat_id, user_name, user_id):
    worker = (d.get("worker_name") or d.get("item") or "").strip()
    amount = float(d.get("amount") or 0)
    month  = d.get("month") or cur_month_key()

    if not worker or amount <= 0:
        send(chat_id, "❌ حدد اسم العامل والمبلغ.\nمثال: راتب العامل 1400")
        return

    add_transaction(svc, "صرف", f"راتب {worker}", "رواتب", amount, user_name)
    add_pending(svc, user_id, "labor", "pay_salary", worker, amount, 0, user_name,
                json.dumps({"month": month}, ensure_ascii=False))

    send(chat_id,
        f"{D}\n✅ تم صرف الراتب\n"
        f"العامل: {worker}\n"
        f"المبلغ: {fmt(amount)} د.إ\n"
        f"الشهر: {month}\n"
        f"{D}"
    )

def h_profit(data, period, chat_id):
    period_data, label = filter_by_period(data, period)
    inc, exp = totals_all(period_data)
    net = inc - exp
    emo = "📈" if net >= 0 else "📉"
    send(chat_id,
        f"{D}\n💰 الصافي ({label}):\n"
        f"الدخل: {fmt(inc)} د.إ\n"
        f"المصروف: {fmt(exp)} د.إ\n"
        f"{emo} الصافي: {fmt(net)} د.إ\n"
        f"{D}"
    )

def h_inventory(svc, chat_id, item_kw=None):
    inv = load_inventory(svc)
    if not inv:
        send(chat_id, "📦 الجرد فارغ.")
        return

    lines = [D, "📦 الجرد الحالي"]
    filtered = inv
    if item_kw:
        item_kw = item_kw.strip()
        filtered = [x for x in inv if item_kw in x["item"]]

    if not filtered:
        # FIX: if keyword filter returns nothing, show all instead of empty
        filtered = inv

    for x in filtered:
        lines.append(f"{x['item']}: {x['qty']}")
    lines.append(D)
    send(chat_id, "\n".join(lines))

def h_last(data, chat_id):
    recent = sorted(data, key=lambda x: x["date"], reverse=True)[:7]
    if not recent:
        send(chat_id, "لا توجد عمليات مسجلة.")
        return

    lines = [D, "🕐 آخر العمليات"]
    for t in recent:
        sign = "+" if t["type"] == "دخل" else "-"
        lines.append(f"{t['date'][:10]} | {sign}{fmt(t['amount'])} د.إ | {t['item']}")
    lines.append(D)
    send(chat_id, "\n".join(lines))

def h_income_by_item(data, d, chat_id):
    # FIX: also check animal_type if item is empty
    kw = (d.get("item") or d.get("animal_type") or "").strip()
    period = d.get("period") or "all"

    if not kw:
        send(chat_id, "❌ حدد البند.\nمثال: كم دخل البيض؟")
        return

    period_data, label = filter_by_period(data, period)
    rows = [
        x for x in period_data
        if x["type"] == "دخل" and (kw in (x["item"] or "") or kw in (x["category"] or ""))
    ]
    total = sum(x["amount"] for x in rows)
    send(chat_id, f"{D}\nالدخل من {kw} ({label}): {fmt(total)} د.إ\n{D}")

def h_income_breakdown(data, d, chat_id):
    period = d.get("period") or "month"
    period_data, label = filter_by_period(data, period)
    inc_rows = [x for x in period_data if x["type"] == "دخل"]

    if not inc_rows:
        send(chat_id, f"لا يوجد دخل في الفترة ({label}).")
        return

    sums = {}
    for x in inc_rows:
        key = x["category"] or x["item"] or "غير محدد"
        sums[key] = sums.get(key, 0) + x["amount"]

    lines = [D, f"📊 الدخل حسب البند ({label})"]
    total = 0
    for k, v in sorted(sums.items(), key=lambda kv: -kv[1]):
        lines.append(f"{k}: {fmt(v)} د.إ")
        total += v
    lines.append(f"{D}\nالإجمالي: {fmt(total)} د.إ\n{D}")
    send(chat_id, "\n".join(lines))

def h_smalltalk(chat_id):
    send(chat_id,
        "أنا بوت العزبة 🤖 أساعدك في:\n"
        "- تسجيل الدخل والمصروف\n"
        "- حركة المواشي والدواجن في الجرد\n"
        "- حساب الإجمالي والربح\n"
        "- عرض آخر العمليات والجرد\n\n"
        "جرب:\n"
        "• بعت بيض بـ 200\n"
        "• صرفنا على الأعلاف 500\n"
        "• تم بيع غنم عدد 2 بمبلغ 1510\n"
        "• كم الربح هذا الشهر؟\n"
        "• كم عدد الغنم في الجرد؟\n"
        "أو اكتب /help"
    )

def h_incomplete(svc, d, chat_id, user_name, user_id):
    """Handle incomplete messages - ask for clarification"""
    intent = d.get("intent")
    question = d.get("suggested_question", "")
    needs = d.get("needs_clarification", "")
    
    if intent == "incomplete_purchase":
        send(chat_id, question or "شو اشتريت؟")
        set_context(user_id, last_intent="incomplete_purchase", last_message="اشتريت", 
                   waiting_for="item", context={"action": "purchase"})
    
    elif intent == "incomplete_sale":
        send(chat_id, question or "شو بعت؟")
        set_context(user_id, last_intent="incomplete_sale", last_message="بعت", 
                   waiting_for="item", context={"action": "sale"})
    
    elif intent == "incomplete_info":
        item = d.get("item") or d.get("animal_type") or ""
        if needs == "amount":
            send(chat_id, question or "بكم؟")
            set_context(user_id, last_intent="incomplete_info", last_message=item,
                       waiting_for="amount", context={"item": item, "animal_type": d.get("animal_type", "")})
        elif needs == "quantity":
            send(chat_id, question or "كم عدد؟")
            set_context(user_id, last_intent="incomplete_info", last_message=item,
                       waiting_for="quantity", context={"item": item, "animal_type": d.get("animal_type", "")})
        else:
            send(chat_id, question or "ممكن تعطيني تفاصيل أكثر؟")

def h_ambiguous(svc, d, data, chat_id, user_name, user_id):
    """Handle ambiguous intents - ask if they want to record or view"""
    intent = d.get("intent")
    question = d.get("suggested_question", "")
    
    if intent == "ambiguous_sale":
        send(chat_id, question or "تبي تسجل بيع ولا تشوف المبيعات؟")
        set_context(user_id, last_intent="ambiguous_sale", last_message="البيع",
                   waiting_for="clarification", context={"type": "sale"})
    
    elif intent == "ambiguous_purchase":
        send(chat_id, question or "تبي تسجل شراء ولا تشوف المشتريات؟")
        set_context(user_id, last_intent="ambiguous_purchase", last_message="الشراء",
                   waiting_for="clarification", context={"type": "purchase"})
    
    elif intent == "ambiguous_expense":
        send(chat_id, question or "تبي تسجل مصروف ولا تشوف المصاريف؟")
        set_context(user_id, last_intent="ambiguous_expense", last_message="المصروف",
                   waiting_for="clarification", context={"type": "expense"})

HELP = """
🌾 بوت مصاريف العزبة

أمثلة:
• بعت بيض بـ 200
• صرفنا على الأعلاف 500
• تم بيع غنم عدد 2 واحد حري وواحد نعيمي بمبلغ 1510
• راتب العامل 1400
• كم دخل البيض الكلي؟
• كم الربح هذا الشهر؟
• كم عدد الغنم في الجرد؟
• قسم لي الدخل حسب التصنيف
• آخر العمليات

💬 تكلم معي بشكل طبيعي:
• "اشتريت" → أسألك: شو اشتريت؟
• "بعت بيض" → أسألك: بكم؟
"""

# ================== MAIN HTTP HANDLER ==========

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
        text = msg.get("text")
        if not text:
            self._ok()
            return

        chat_id = msg["chat"]["id"]
        user_id = msg["from"]["id"]
        text = text.strip()

        if user_id not in ALLOWED_USERS:
            send(chat_id, "⛔ هذا البوت خاص.")
            self._ok()
            return

        user_name = ALLOWED_USERS[user_id]

        if text in ("/start", "/help", "help", "مساعدة"):
            send(chat_id, HELP)
            clear_context(user_id)  # Clear context on help
            self._ok()
            return

        try:
            svc = sheets_svc()
            data = load_transactions(svc)
        except Exception as e:
            send(chat_id, f"❌ خطأ في Google Sheets:\n{e}")
            self._ok()
            return

        # Detect intent with conversation context
        d = detect_intent(text, user_id)
        intent = d.get("intent") or "clarify"
        period = d.get("period") or "month"

        if intent in ["incomplete_purchase", "incomplete_sale", "incomplete_info"]:
            h_incomplete(svc, d, chat_id, user_name, user_id)
        elif intent in ["ambiguous_sale", "ambiguous_purchase", "ambiguous_expense"]:
            h_ambiguous(svc, d, data, chat_id, user_name, user_id)
        elif intent == "add_income":
            h_add_income(svc, d, chat_id, user_name, user_id)
        elif intent == "add_expense":
            h_add_expense(svc, d, chat_id, user_name, user_id)
        elif intent == "add_livestock":
            h_add_livestock(svc, d, chat_id, user_name, user_id)
        elif intent == "sell_livestock":
            h_sell_livestock(svc, d, chat_id, user_name, user_id)
        elif intent == "add_poultry":
            h_add_poultry(svc, d, chat_id, user_name, user_id)
        elif intent == "sell_poultry":
            h_sell_poultry(svc, d, chat_id, user_name, user_id)
        elif intent == "pay_salary":
            h_pay_salary(svc, d, chat_id, user_name, user_id)
        elif intent == "income_total":
            period_data, label = filter_by_period(data, period)
            inc, _ = totals_all(period_data)
            send(chat_id, f"{D}\n💰 الدخل ({label}): {fmt(inc)} د.إ\n{D}")
            clear_context(user_id)
        elif intent == "expense_total":
            period_data, label = filter_by_period(data, period)
            _, exp = totals_all(period_data)
            send(chat_id, f"{D}\n📤 المصروف ({label}): {fmt(exp)} د.إ\n{D}")
            clear_context(user_id)
        elif intent == "profit":
            h_profit(data, period, chat_id)
            clear_context(user_id)
        elif intent == "inventory":
            h_inventory(svc, chat_id, d.get("inventory_item") or d.get("item"))
            clear_context(user_id)
        elif intent == "last_transactions":
            h_last(data, chat_id)
            clear_context(user_id)
        elif intent == "income_by_item":
            h_income_by_item(data, d, chat_id)
            clear_context(user_id)
        elif intent == "income_breakdown":
            h_income_breakdown(data, d, chat_id)
            clear_context(user_id)
        elif intent == "smalltalk":
            h_smalltalk(chat_id)
            clear_context(user_id)
        else:
            send(chat_id,
                "❓ ما فهمت.\n"
                "جرب:\n"
                "• بعت بيض بـ 200\n"
                "• كم دخل البيض الكلي؟\n"
                "• كم الربح هذا الشهر؟\n"
                "أو اكتب /help"
            )
            clear_context(user_id)

        self._ok()
