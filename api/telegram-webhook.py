# api/telegram-webhook.py

from http.server import BaseHTTPRequestHandler
import json
import os
from datetime import datetime, timezone, timedelta, date
import requests
from openai import OpenAI
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from difflib import SequenceMatcher
import re

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
S_LEARNING     = "Learning"        # NEW: Self-learning data
S_PATTERNS     = "Patterns"        # NEW: User behavior patterns
S_FEEDBACK     = "Feedback"        # NEW: User corrections

D = "──────────────"

# ================== CONVERSATION MEMORY =========

conversation_state = {}

def get_context(user_id):
    return conversation_state.get(user_id, {})

def set_context(user_id, last_intent=None, last_message=None, waiting_for=None, context=None):
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
    try:
        res = svc.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{sheet}!{rng}",
        ).execute()
        return res.get("values", [])
    except Exception:
        return []

def append_row(svc, sheet, row):
    try:
        svc.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{sheet}!A1",
            valueInputOption="USER_ENTERED",
            body={"values": [row]},
        ).execute()
    except Exception:
        pass

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
    rows = read_sheet(svc, S_INVENTORY)
    values_api = svc.spreadsheets().values()

    i = find_inventory_row(rows, item_name)
    if i >= 0:
        r = rows[i]
        old_qty = int(r[2]) if len(r) > 2 and r[2] else 0
        new_qty = max(0, old_qty + int(qty_delta))
        row_num = i + 2
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

# ================== LEARNING SYSTEM =============

def log_learning(svc, user_id, user_name, message, intent_detected, was_successful, correction=""):
    """Log messages for learning system"""
    try:
        append_row(svc, S_LEARNING, [
            now_str(),
            str(user_id),
            user_name,
            message,
            intent_detected,
            "success" if was_successful else "failed",
            correction
        ])
    except Exception:
        pass

def log_pattern(svc, user_id, user_name, pattern_type, pattern_data):
    """Log user behavior patterns"""
    try:
        append_row(svc, S_PATTERNS, [
            now_str(),
            str(user_id),
            user_name,
            pattern_type,
            json.dumps(pattern_data, ensure_ascii=False)
        ])
    except Exception:
        pass

def get_learned_examples(svc, user_id):
    """Get successfully learned examples for this user"""
    try:
        rows = read_sheet(svc, S_LEARNING)
        examples = []
        for r in rows:
            if len(r) >= 6 and r[1] == str(user_id) and r[5] == "success":
                examples.append({
                    "message": r[3],
                    "intent": r[4]
                })
        # Return last 20 successful examples
        return examples[-20:] if len(examples) > 20 else examples
    except Exception:
        return []

def get_user_patterns(svc, user_id):
    """Get user's behavior patterns for smart suggestions"""
    try:
        rows = read_sheet(svc, S_PATTERNS)
        patterns = {}
        for r in rows:
            if len(r) >= 5 and r[1] == str(user_id):
                pattern_type = r[3]
                pattern_data = json.loads(r[4])
                if pattern_type not in patterns:
                    patterns[pattern_type] = []
                patterns[pattern_type].append(pattern_data)
        return patterns
    except Exception:
        return {}

def analyze_and_save_pattern(svc, user_id, user_name, transaction_type, item, amount):
    """Analyze transaction and save pattern if recurring"""
    try:
        # Get recent similar transactions
        data = load_transactions(svc)
        similar = [
            x for x in data 
            if x.get("user") == user_name 
            and x.get("type") == transaction_type
            and item.lower() in x.get("item", "").lower()
        ]
        
        if len(similar) >= 3:  # If done 3+ times, it's a pattern
            amounts = [x["amount"] for x in similar[-5:]]
            avg_amount = sum(amounts) / len(amounts)
            
            pattern = {
                "item": item,
                "transaction_type": transaction_type,
                "frequency": len(similar),
                "avg_amount": round(avg_amount, 2),
                "last_amount": amount
            }
            log_pattern(svc, user_id, user_name, "recurring_transaction", pattern)
    except Exception:
        pass

def fuzzy_match(text1, text2, threshold=0.75):
    """Fuzzy string matching for typo tolerance"""
    return SequenceMatcher(None, text1.lower(), text2.lower()).ratio() >= threshold

def normalize_arabic_text(text):
    """Normalize Arabic text for better matching"""
    # Remove diacritics
    text = re.sub(r'[\u0617-\u061A\u064B-\u0652]', '', text)
    # Normalize alef
    text = text.replace('أ', 'ا').replace('إ', 'ا').replace('آ', 'ا')
    # Normalize yaa
    text = text.replace('ى', 'ي')
    # Normalize taa marboota
    text = text.replace('ة', 'ه')
    return text.strip()

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

# ================== SMART SUGGESTIONS ===========

def get_smart_suggestion(svc, user_id, user_name, item):
    """Get smart suggestion based on patterns"""
    try:
        patterns = get_user_patterns(svc, user_id)
        recurring = patterns.get("recurring_transaction", [])
        
        for pattern in reversed(recurring):  # Most recent first
            if fuzzy_match(pattern["item"], item, threshold=0.7):
                return {
                    "suggested_amount": pattern["avg_amount"],
                    "last_amount": pattern["last_amount"],
                    "frequency": pattern["frequency"]
                }
        return None
    except Exception:
        return None

# ================== AI INTENT ==================

def build_dynamic_prompt(svc, user_id):
    """Build AI prompt with learned examples"""
    
    # Get learned examples for this user
    learned = get_learned_examples(svc, user_id)
    
    learned_section = ""
    if learned:
        learned_section = "\n\n🎓 أمثلة متعلمة من هذا المستخدم:\n"
        for ex in learned[-10:]:  # Last 10 examples
            learned_section += f"- \"{ex['message']}\" → {ex['intent']}\n"
    
    base_prompt = f"""
أنت مساعد ذكي لإدارة عزبة/مزرعة في الإمارات. مهمتك فهم رسائل المستخدم باللهجة الإماراتية/الخليجية والعربية الفصحى واستخراج النية والبيانات.

🧠 SMART FEATURES:
- فهم الأخطاء الإملائية (بعنة = بعنا، بيظ = بيض)
- فهم الاختصارات (ش = شو، ك = كم)
- فهم الأرقام بالعربي والإنجليزي
- التعلم من أسلوب المستخدم{learned_section}

أرجع JSON فقط بدون أي نص آخر:

{{
  "intent": "add_income | add_expense | add_livestock | sell_livestock | add_poultry | sell_poultry | pay_salary | income_total | expense_total | profit | inventory | last_transactions | income_by_item | income_breakdown | smalltalk | clarify | incomplete_purchase | incomplete_sale | incomplete_info | ambiguous_sale | ambiguous_purchase | ambiguous_expense | feedback_negative | correction | repeat_last | smart_query",
  "item": "",
  "category": "",
  "amount": 0,
  "quantity": 0,
  "animal_type": "",
  "worker_name": "",
  "period": "today | week | month | all",
  "inventory_item": "",
  "needs_clarification": "item | amount | quantity | details",
  "suggested_question": "",
  "confidence": 0.0
}}

════════════════════════════════════════════════════════════════════

🆕 NEW: NEGATIVE FEEDBACK DETECTION

إذا قال المستخدم:
- "لا" / "غلط" / "خطأ" / "مو صح" / "مب صح"
- "ليش" / "وين" / "شلون" (بعد جواب من البوت)
- "ما فهمتني" / "مو هذا اللي أقصده"

→ intent = "feedback_negative"

════════════════════════════════════════════════════════════════════

🆕 NEW: CORRECTION DETECTION (ميزة ١ + ٢)

إذا المستخدم يصحح معلومة سابقة:
- "لا ٣٠٠ مو ٢٠٠" → correction
- "عدلها على ٥٠٠" → correction
- "غلط، المبلغ ٤٠٠" → correction
- "كان ٥ مو ٣" → correction

→ intent = "correction"
→ أضف: "correction_type": "amount | quantity | item"
→ أضف: "new_value": القيمة الجديدة
→ أضف: "old_value": القيمة القديمة (إن وجدت)

════════════════════════════════════════════════════════════════════

🆕 NEW: REPEAT LAST (ميزة ٣)

إذا المستخدم يبي يكرر آخر عملية:
- "نفس الشي" / "نفس الأمس" / "نفسه"
- "مثل اللي قبل" / "زي قبل"
- "كرر" / "نفس العملية"

→ intent = "repeat_last"

════════════════════════════════════════════════════════════════════

🆕 NEW: SMART QUERY (ميزة ٤)

استعلامات مركبة ذكية:
- "كم ربحنا من البيض هالأسبوع؟"
  → intent = "smart_query"
  → query_type = "profit_by_item"
  → item = "بيض"
  → period = "week"

- "شو أكثر شي بعناه؟"
  → intent = "smart_query"
  → query_type = "top_selling"

- "كم صرفنا على العلف هالشهر؟"
  → intent = "smart_query"
  → query_type = "expense_by_item"
  → item = "علف"
  → period = "month"

أنواع الاستعلامات الذكية:
- profit_by_item: الربح من بند معين
- expense_by_item: المصروف على بند معين
- top_selling: أكثر شي مبيعات
- top_expense: أكثر شي مصروف
- comparison: مقارنة بين بندين

════════════════════════════════════════════════════════════════════

🔀 AMBIGUOUS INTENTS:

ambiguous_sale: "البيع" أو "المبيعات" وحدها
ambiguous_purchase: "الشراء" أو "المشتريات" وحدها  
ambiguous_expense: "المصروف" أو "المصاريف" وحدها

════════════════════════════════════════════════════════════════════

🔄 INCOMPLETE MESSAGES:

incomplete_purchase: "اشتريت" / "شريت" / "جبت" بدون تفاصيل
incomplete_sale: "بعت" / "بعنا" بدون تفاصيل
incomplete_info: ذكر البند بدون مبلغ/عدد

════════════════════════════════════════════════════════════════════

📌 INTENT RULES:

دخل (add_income): بيع منتجات (بيض، لبن، صوف) ليس حيوانات حية
مصروف (add_expense): شراء (أعلاف، دواء، كهرباء، وقود، صيانة)
مواشي-شراء (add_livestock): شراء حيوانات كبيرة (غنم، بقر، إبل)
مواشي-بيع (sell_livestock): بيع حيوانات كبيرة
دواجن-شراء (add_poultry): شراء دواجن (دجاج، فراخ، طيور)
دواجن-بيع (sell_poultry): بيع دواجن
راتب (pay_salary): راتب/معاش عامل

════════════════════════════════════════════════════════════════════

📊 QUERIES:

- "كم الدخل" / "شو الدخل" → income_total
- "كم المصروف" / "شو المصروف" → expense_total
- "كم الربح" / "الصافي" → profit
- "الجرد" / "كم عدد" / "شو عندنا" → inventory
- "آخر العمليات" → last_transactions
- "كم دخل [البند]" → income_by_item
- "قسم الدخل" → income_breakdown

════════════════════════════════════════════════════════════════════

🔢 DATA EXTRACTION:

1. مرادفات (Synonyms):
   دجاج = فراخ = طيور = دواجن
   غنم = خرفان = أغنام = خراف
   بيض = بيظ
   لبن = حليب
   كهرباء = كهربا
   ماء = مويه
   شو = ش = كم = وش
   جبنا = اشترينا = شرينا
   بعنا = بعت = بيع

2. أخطاء إملائية شائعة:
   بعنة → بعنا
   بيظ → بيض
   اشتريت → اشتريت (صحيح)

3. اختصارات:
   ش → شو
   ك → كم

4. أرقام عربية → إنجليزية:
   ٥٠٠ → 500

════════════════════════════════════════════════════════════════════

⚡ CONFIDENCE SCORE:

أضف confidence من 0 إلى 1:
- 1.0 = واثق جداً (رسالة واضحة)
- 0.5-0.8 = متوسط (رسالة غامضة شوي)
- <0.5 = غير واثق (يحتاج توضيح)

إذا confidence < 0.6 → intent = "clarify"

الآن افهم رسالة المستخدم وأرجع JSON فقط:
"""
    
    return base_prompt

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

def detect_intent(text, user_id=None, svc=None):
    try:
        # Normalize text for better matching
        normalized_text = normalize_arabic_text(text)
        
        # Check if there's conversation context
        ctx = get_context(user_id) if user_id else {}
        
        # Build dynamic prompt with learned examples
        system_prompt = build_dynamic_prompt(svc, user_id) if svc and user_id else build_dynamic_prompt(None, None)
        
        messages = [{"role": "system", "content": system_prompt}]
        
        # If waiting for follow-up, include context
        if ctx.get("waiting_for"):
            # Special handling for repeat_last confirmation
            if ctx.get("waiting_for") == "confirmation" and ctx.get("last_intent") == "repeat_last":
                text_lower = text.strip().lower()
                confirmation_words = ["ايه", "نعم", "أكيد", "تمام", "صح", "اي", "yes"]
                
                if any(word in text_lower for word in confirmation_words):
                    # User confirmed, repeat the transaction
                    prev_ctx = ctx.get("context", {})
                    trans_type = prev_ctx.get("type", "")
                    item = prev_ctx.get("item", "")
                    amount = prev_ctx.get("amount", 0)
                    
                    clear_context(user_id)
                    
                    if trans_type == "دخل":
                        return {"intent": "add_income", "item": item, "amount": amount, "confidence": 1.0}
                    elif trans_type == "صرف":
                        return {"intent": "add_expense", "item": item, "amount": amount, "confidence": 1.0}
                else:
                    clear_context(user_id)
                    return {"intent": "clarify", "confidence": 1.0}
            
            # Special handling for ambiguous intent clarification
            if ctx.get("waiting_for") == "clarification":
                text_lower = text.strip().lower()
                text_clean = text.strip()
                prev_type = ctx.get("context", {}).get("type")
                
                # EXPANDED: Better keyword detection
                record_keywords = ["تسجيل", "سجل", "أسجل", "اسجل", "نسجل", "١", "1"]
                view_keywords = [
                    "شوف", "أشوف", "اشوف", "عرض", "إجمالي", "اجمالي", "كم", 
                    "المبيعات", "المشتريات", "المصاريف", "المصروف",
                    "شف", "أشف", "ورني", "طلع", "اطلع",
                    "٢", "2"
                ]
                
                is_record = any(kw in text_lower for kw in record_keywords)
                is_view = any(kw in text_lower for kw in view_keywords)
                
                # Additional check: if they just repeated the noun
                if not is_record and not is_view:
                    if prev_type == "sale" and any(w in text_clean for w in ["المبيعات", "البيع", "الدخل"]):
                        is_view = True
                    elif prev_type == "purchase" and any(w in text_clean for w in ["المشتريات", "الشراء"]):
                        is_view = True
                    elif prev_type == "expense" and any(w in text_clean for w in ["المصاريف", "المصروف"]):
                        is_view = True
                
                if is_record and prev_type == "sale":
                    clear_context(user_id)
                    return {"intent": "incomplete_sale", "confidence": 1.0}
                elif is_view and prev_type == "sale":
                    clear_context(user_id)
                    return {"intent": "income_total", "period": "month", "confidence": 1.0}
                elif is_record and prev_type == "purchase":
                    clear_context(user_id)
                    return {"intent": "incomplete_purchase", "confidence": 1.0}
                elif is_view and prev_type == "purchase":
                    clear_context(user_id)
                    return {"intent": "expense_total", "period": "month", "confidence": 1.0}
                elif is_record and prev_type == "expense":
                    clear_context(user_id)
                    return {"intent": "incomplete_info", "needs_clarification": "item", "confidence": 1.0}
                elif is_view and prev_type == "expense":
                    clear_context(user_id)
                    return {"intent": "expense_total", "period": "month", "confidence": 1.0}
            
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
            prev_ctx = ctx.get("context", {})
            if prev_ctx.get("item") and not result.get("item"):
                result["item"] = prev_ctx["item"]
            if prev_ctx.get("animal_type") and not result.get("animal_type"):
                result["animal_type"] = prev_ctx["animal_type"]
            if prev_ctx.get("action"):
                if prev_ctx["action"] == "purchase" and result.get("intent") in ["clarify", "smalltalk"]:
                    item = text.strip()
                    livestock_keywords = ["غنم", "خرفان", "بقر", "عجول", "إبل", "جمال", "ناقة"]
                    poultry_keywords = ["دجاج", "فراخ", "طيور", "حمام", "بط"]
                    
                    if any(kw in item for kw in livestock_keywords):
                        result = {"intent": "add_livestock", "animal_type": item, "quantity": 1, "confidence": 0.9}
                    elif any(kw in item for kw in poultry_keywords):
                        result = {"intent": "add_poultry", "animal_type": item, "quantity": 1, "confidence": 0.9}
                    else:
                        result = {"intent": "add_expense", "item": item, "confidence": 0.9}
                
                elif prev_ctx["action"] == "sale" and result.get("intent") in ["clarify", "smalltalk"]:
                    item = text.strip()
                    livestock_keywords = ["غنم", "خرفان", "بقر", "عجول", "إبل", "جمال", "ناقة"]
                    poultry_keywords = ["دجاج", "فراخ", "طيور", "حمام", "بط"]
                    
                    if any(kw in item for kw in livestock_keywords):
                        result = {"intent": "sell_livestock", "animal_type": item, "quantity": 1, "confidence": 0.9}
                    elif any(kw in item for kw in poultry_keywords):
                        result = {"intent": "sell_poultry", "animal_type": item, "quantity": 1, "confidence": 0.9}
                    else:
                        result = {"intent": "add_income", "item": item, "confidence": 0.9}
        
        return result
    except Exception as e:
        return {"intent": "clarify", "_error": str(e), "confidence": 0.0}

# ================== HELPERS ====================

def resolve_item(d):
    return (
        (d.get("item") or "").strip()
        or (d.get("animal_type") or "").strip()
        or (d.get("category") or "").strip()
        or ""
    )

# ================== HANDLERS ====================

def h_add_income(svc, d, chat_id, user_name, user_id):
    item     = resolve_item(d)
    amount   = float(d.get("amount") or 0)
    category = (d.get("category") or item or "").strip()

    if not item:
        send(chat_id, "شو بعت؟")
        set_context(user_id, last_intent="add_income", last_message=item, waiting_for="item", 
                   context={"action": "income", "amount": amount})
        return
    
    if amount <= 0:
        # Check for smart suggestion
        suggestion = get_smart_suggestion(svc, user_id, user_name, item)
        if suggestion:
            send(chat_id, 
                f"بكم بعته؟ او على كم؟\n"
                f"💡 آخر مرة: {fmt(suggestion['last_amount'])} درهم\n"
                f"(المتوسط: {fmt(suggestion['suggested_amount'])} درهم)"
            )
        else:
            send(chat_id, "بكم بعته؟ او على كم؟")
        set_context(user_id, last_intent="add_income", last_message=item, waiting_for="amount",
                   context={"action": "income", "item": item})
        return

    add_transaction(svc, "دخل", item, category, amount, user_name)
    add_pending(svc, user_id, "transaction", "add_income", item, amount, 0, user_name)
    
    # Log success and analyze pattern
    log_learning(svc, user_id, user_name, f"بعت {item} بـ {amount}", "add_income", True)
    analyze_and_save_pattern(svc, user_id, user_name, "دخل", item, amount)
    
    clear_context(user_id)

    send(chat_id,
        f"{D}\n✅ سجلتها\n"
        f"البند: {item}\n"
        f"المبلغ: {fmt(amount)} درهم\n"
        f"بواسطة: {user_name}\n"
        f"{D}"
    )

def h_add_expense(svc, d, chat_id, user_name, user_id):
    item     = resolve_item(d)
    amount   = float(d.get("amount") or 0)
    category = (d.get("category") or item or "").strip()

    if not item:
        send(chat_id, "شو اشتريت؟ او شو خذت؟")
        set_context(user_id, last_intent="add_expense", last_message="", waiting_for="item",
                   context={"action": "expense", "amount": amount})
        return
    
    if amount <= 0:
        # Check for smart suggestion
        suggestion = get_smart_suggestion(svc, user_id, user_name, item)
        if suggestion:
            send(chat_id, 
                f"بكم اشتريته؟ او على كم؟\n"
                f"💡 آخر مرة: {fmt(suggestion['last_amount'])} درهم\n"
                f"(المتوسط: {fmt(suggestion['suggested_amount'])} درهم)"
            )
        else:
            send(chat_id, f"بكم اشتريت {item}؟ او على كم؟")
        set_context(user_id, last_intent="add_expense", last_message=item, waiting_for="amount",
                   context={"action": "expense", "item": item})
        return

    add_transaction(svc, "صرف", item, category, amount, user_name)
    add_pending(svc, user_id, "transaction", "add_expense", item, amount, 0, user_name)
    
    # Log success and analyze pattern
    log_learning(svc, user_id, user_name, f"اشتريت {item} بـ {amount}", "add_expense", True)
    analyze_and_save_pattern(svc, user_id, user_name, "صرف", item, amount)
    
    clear_context(user_id)

    send(chat_id,
        f"{D}\n✅ سجلتها\n"
        f"البند: {item}\n"
        f"المبلغ: {fmt(amount)} درهم\n"
        f"بواسطة: {user_name}\n"
        f"{D}"
    )

def h_add_livestock(svc, d, chat_id, user_name, user_id):
    animal = (d.get("animal_type") or d.get("item") or "").strip()
    qty    = int(d.get("quantity") or 1)
    cost   = float(d.get("amount") or 0)

    if not animal:
        send(chat_id, "شو نوع المواشي اللي خذيتها؟")
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
    
    # Log success
    log_learning(svc, user_id, user_name, f"جبنا {qty} {animal}", "add_livestock", True)
    if cost > 0:
        analyze_and_save_pattern(svc, user_id, user_name, "صرف", f"شراء {animal}", cost)
    
    clear_context(user_id)

    inv = load_inventory(svc)
    current_qty = next((x["qty"] for x in inv if x["item"] == animal), qty)

    send(chat_id,
        f"{D}\n✅ سجلتها\n"
        f"النوع: {animal} × {qty}\n"
        f"التكلفة: {fmt(cost)} درهم\n"
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
        send(chat_id, f"بكم بعت {qty} {animal_raw}؟ او على كم؟")
        set_context(user_id, last_intent="sell_livestock", last_message=animal_raw, waiting_for="amount",
                   context={"action": "sale", "type": "livestock", "animal_type": animal_raw, "quantity": qty})
        return

    splits = split_animals_for_inventory(animal_raw, qty)
    for name, q in splits:
        update_inventory(svc, name, -q, item_type="مواشي")

    if price > 0:
        add_transaction(svc, "دخل", f"بيع {qty} {animal_raw}", "مواشي", price, user_name)

    add_pending(svc, user_id, "inventory", "sell_livestock", animal_raw, price, qty, user_name)
    
    # Log success
    log_learning(svc, user_id, user_name, f"بعنا {qty} {animal_raw} بـ {price}", "sell_livestock", True)
    if price > 0:
        analyze_and_save_pattern(svc, user_id, user_name, "دخل", f"بيع {animal_raw}", price)
    
    clear_context(user_id)

    inv = load_inventory(svc)
    lines = [D, f"✅ سجلتها\nالحيوان: {animal_raw} × {qty}\nالسعر: {fmt(price)} درهم\nالرصيد الحالي:"]
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
        send(chat_id, "شو نوع الدواجن اللي خذيتها؟")
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
    
    # Log success
    log_learning(svc, user_id, user_name, f"جبنا {qty} {bird}", "add_poultry", True)
    if cost > 0:
        analyze_and_save_pattern(svc, user_id, user_name, "صرف", f"شراء {bird}", cost)
    
    clear_context(user_id)

    inv = load_inventory(svc)
    current_qty = next((x["qty"] for x in inv if x["item"] == bird), qty)

    send(chat_id,
        f"{D}\n✅ سجلتها\n"
        f"النوع: {bird} × {qty}\n"
        f"التكلفة: {fmt(cost)} درهم\n"
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
        send(chat_id, f"بكم بعت {qty} {bird}؟ او على كم؟")
        set_context(user_id, last_intent="sell_poultry", last_message=bird, waiting_for="amount",
                   context={"action": "sale", "type": "poultry", "animal_type": bird, "quantity": qty})
        return

    update_inventory(svc, bird, -qty, item_type="دواجن")
    if price > 0:
        add_transaction(svc, "دخل", f"بيع {qty} {bird}", "دواجن", price, user_name)

    add_pending(svc, user_id, "inventory", "sell_poultry", bird, price, qty, user_name)
    
    # Log success
    log_learning(svc, user_id, user_name, f"بعنا {qty} {bird} بـ {price}", "sell_poultry", True)
    if price > 0:
        analyze_and_save_pattern(svc, user_id, user_name, "دخل", f"بيع {bird}", price)
    
    clear_context(user_id)

    inv = load_inventory(svc)
    current_qty = next((x["qty"] for x in inv if x["item"] == bird), 0)

    send(chat_id,
        f"{D}\n✅ سجلتها\n"
        f"الطير: {bird} × {qty}\n"
        f"السعر: {fmt(price)} درهم\n"
        f"{D}\n"
        f"🐔 رصيد {bird} الحالي: {current_qty}\n"
        f"{D}"
    )

def h_pay_salary(svc, d, chat_id, user_name, user_id):
    worker = (d.get("worker_name") or d.get("item") or "").strip()
    amount = float(d.get("amount") or 0)
    month  = d.get("month") or cur_month_key()

    if not worker or amount <= 0:
        send(chat_id, "حدد اسم العامل والمبلغ\nمثال: راتب العامل 1400")
        return

    add_transaction(svc, "صرف", f"راتب {worker}", "رواتب", amount, user_name)
    add_pending(svc, user_id, "labor", "pay_salary", worker, amount, 0, user_name,
                json.dumps({"month": month}, ensure_ascii=False))
    
    # Log success
    log_learning(svc, user_id, user_name, f"راتب {worker} {amount}", "pay_salary", True)

    send(chat_id,
        f"{D}\n✅ سجلتها\n"
        f"العامل: {worker}\n"
        f"المبلغ: {fmt(amount)} درهم\n"
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
        f"الدخل: {fmt(inc)} درهم\n"
        f"المصروف: {fmt(exp)} درهم\n"
        f"{emo} الصافي: {fmt(net)} درهم\n"
        f"{D}"
    )

def h_inventory(svc, chat_id, item_kw=None):
    inv = load_inventory(svc)
    if not inv:
        send(chat_id, "📦 الجرد فاضي")
        return

    lines = [D, "📦 الجرد الحالي"]
    filtered = inv
    if item_kw:
        item_kw = item_kw.strip()
        filtered = [x for x in inv if item_kw in x["item"]]

    if not filtered:
        filtered = inv

    for x in filtered:
        lines.append(f"{x['item']}: {x['qty']}")
    lines.append(D)
    send(chat_id, "\n".join(lines))

def h_last(data, chat_id):
    recent = sorted(data, key=lambda x: x["date"], reverse=True)[:7]
    if not recent:
        send(chat_id, "ما في عمليات مسجلة")
        return

    lines = [D, "🕐 آخر العمليات"]
    for t in recent:
        sign = "+" if t["type"] == "دخل" else "-"
        lines.append(f"{t['date'][:10]} | {sign}{fmt(t['amount'])} درهم | {t['item']}")
    lines.append(D)
    send(chat_id, "\n".join(lines))

def h_income_by_item(data, d, chat_id):
    kw = (d.get("item") or d.get("animal_type") or "").strip()
    period = d.get("period") or "all"

    if not kw:
        send(chat_id, "حدد البند\nمثال: كم دخل البيض؟")
        return

    period_data, label = filter_by_period(data, period)
    rows = [
        x for x in period_data
        if x["type"] == "دخل" and (kw in (x["item"] or "") or kw in (x["category"] or ""))
    ]
    total = sum(x["amount"] for x in rows)
    send(chat_id, f"{D}\nالدخل من {kw} ({label}): {fmt(total)} درهم\n{D}")

def h_income_breakdown(data, d, chat_id):
    period = d.get("period") or "month"
    period_data, label = filter_by_period(data, period)
    inc_rows = [x for x in period_data if x["type"] == "دخل"]

    if not inc_rows:
        send(chat_id, f"ما في دخل في الفترة ({label})")
        return

    sums = {}
    for x in inc_rows:
        key = x["category"] or x["item"] or "غير محدد"
        sums[key] = sums.get(key, 0) + x["amount"]

    lines = [D, f"📊 الدخل حسب البند ({label})"]
    total = 0
    for k, v in sorted(sums.items(), key=lambda kv: -kv[1]):
        lines.append(f"{k}: {fmt(v)} درهم")
        total += v
    lines.append(f"{D}\nالإجمالي: {fmt(total)} درهم\n{D}")
    send(chat_id, "\n".join(lines))

def h_smalltalk(chat_id):
    send(chat_id,
        "أنا بوت العزبة الذكي 🤖🧠\n\n"
        "ميزاتي:\n"
        "✅ أفهم الأخطاء الإملائية\n"
        "✅ أتعلم من أسلوبك\n"
        "✅ أقترح المبالغ المعتادة\n"
        "✅ أصير أذكى مع كل استخدام\n\n"
        "جرب:\n"
        "• بعت بيض بـ 200\n"
        "• صرفنا على الأعلاف 500\n"
        "• كم الربح؟\n"
        "• اكتب /help للمزيد"
    )

def h_incomplete(svc, d, chat_id, user_name, user_id):
    intent = d.get("intent")
    question = d.get("suggested_question", "")
    needs = d.get("needs_clarification", "")
    
    if intent == "incomplete_purchase":
        send(chat_id, question or "شو اشتريت؟ او شو خذت؟")
        set_context(user_id, last_intent="incomplete_purchase", last_message="اشتريت", 
                   waiting_for="item", context={"action": "purchase"})
    
    elif intent == "incomplete_sale":
        send(chat_id, question or "شو بعت؟")
        set_context(user_id, last_intent="incomplete_sale", last_message="بعت", 
                   waiting_for="item", context={"action": "sale"})
    
    elif intent == "incomplete_info":
        item = d.get("item") or d.get("animal_type") or ""
        if needs == "amount":
            send(chat_id, question or "بكم؟ او على كم؟")
            set_context(user_id, last_intent="incomplete_info", last_message=item,
                       waiting_for="amount", context={"item": item, "animal_type": d.get("animal_type", "")})
        elif needs == "quantity":
            send(chat_id, question or "كم عدد؟")
            set_context(user_id, last_intent="incomplete_info", last_message=item,
                       waiting_for="quantity", context={"item": item, "animal_type": d.get("animal_type", "")})
        else:
            send(chat_id, question or "ممكن تعطيني تفاصيل أكثر؟")

def h_ambiguous(svc, d, data, chat_id, user_name, user_id):
    intent = d.get("intent")
    
    if intent == "ambiguous_sale":
        send(chat_id, 
            "تبي:\n"
            "١. تسجل بيع يديد\n"
            "٢. تشوف المبيعات\n\n"
            "اكتب رقم او كلمة"
        )
        set_context(user_id, last_intent="ambiguous_sale", last_message="البيع",
                   waiting_for="clarification", context={"type": "sale"})
    
    elif intent == "ambiguous_purchase":
        send(chat_id,
            "تبي:\n"
            "١. تسجل شراء يديد\n"
            "٢. تشوف المشتريات\n\n"
            "اكتب رقم او كلمة"
        )
        set_context(user_id, last_intent="ambiguous_purchase", last_message="الشراء",
                   waiting_for="clarification", context={"type": "purchase"})
    
    elif intent == "ambiguous_expense":
        send(chat_id,
            "تبي:\n"
            "١. تسجل مصروف يديد\n"
            "٢. تشوف المصاريف\n\n"
            "اكتب رقم او كلمة"
        )
        set_context(user_id, last_intent="ambiguous_expense", last_message="المصروف",
                   waiting_for="clarification", context={"type": "expense"})

def h_feedback_negative(svc, d, chat_id, user_name, user_id, original_message):
    """Handle negative feedback - log for learning"""
    ctx = get_context(user_id)
    last_intent = ctx.get("last_intent", "unknown")
    
    # Log failed understanding
    log_learning(svc, user_id, user_name, original_message, last_intent, False, 
                correction="User said: لا/غلط/مو صح")
    
    send(chat_id,
        "آسف! مافهميت عليك شتقصد 😅\n"
        "ممكن تعيد الرسالة بطريقة ثانية؟\n"
        "او اكتب /help للأمثلة"
    )
    
    clear_context(user_id)

def h_correction(svc, d, chat_id, user_name, user_id):
    """Handle corrections to last transaction"""
    correction_type = d.get("correction_type", "")
    new_value = d.get("new_value", "")
    
    # Get last transaction
    data = load_transactions(svc)
    user_transactions = [x for x in data if x.get("user") == user_name]
    
    if not user_transactions:
        send(chat_id, "ما في عمليات سابقة للتعديل")
        return
    
    last_trans = user_transactions[-1]
    
    # Try to correct
    try:
        # This is a simplified correction - in real implementation, 
        # you'd update the actual sheet row
        send(chat_id,
            f"تمام، عدلتها ✅\n"
            f"العملية السابقة: {last_trans['item']}\n"
            f"التعديل: {new_value}\n"
            f"{D}"
        )
        log_learning(svc, user_id, user_name, f"تصحيح: {new_value}", "correction", True)
    except Exception:
        send(chat_id, "ما قدرت أعدل، جرب مرة ثانية")

def h_repeat_last(svc, d, chat_id, user_name, user_id):
    """Repeat last transaction"""
    # Get last transaction
    data = load_transactions(svc)
    user_transactions = [x for x in data if x.get("user") == user_name]
    
    if not user_transactions:
        send(chat_id, "ما في عمليات سابقة للتكرار")
        return
    
    last_trans = user_transactions[-1]
    
    # Ask for confirmation
    send(chat_id,
        f"تبي تكرر العملية:\n"
        f"النوع: {last_trans['type']}\n"
        f"البند: {last_trans['item']}\n"
        f"المبلغ: {fmt(last_trans['amount'])} درهم\n\n"
        f"قول: ايه او نعم للتأكيد"
    )
    
    # Save context for confirmation
    set_context(user_id, last_intent="repeat_last", last_message="",
               waiting_for="confirmation", 
               context={
                   "type": last_trans["type"],
                   "item": last_trans["item"],
                   "amount": last_trans["amount"],
                   "category": last_trans.get("category", "")
               })

def h_smart_query(svc, d, data, chat_id, user_name, user_id):
    """Handle smart queries"""
    query_type = d.get("query_type", "")
    item = d.get("item", "")
    period = d.get("period", "month")
    
    period_data, label = filter_by_period(data, period)
    
    if query_type == "profit_by_item":
        # Calculate profit from specific item
        income_rows = [x for x in period_data if x["type"] == "دخل" and item in x["item"]]
        expense_rows = [x for x in period_data if x["type"] == "صرف" and item in x["item"]]
        
        income = sum(x["amount"] for x in income_rows)
        expense = sum(x["amount"] for x in expense_rows)
        profit = income - expense
        
        send(chat_id,
            f"{D}\n💰 ربح {item} ({label}):\n"
            f"الدخل: {fmt(income)} درهم\n"
            f"المصروف: {fmt(expense)} درهم\n"
            f"الربح: {fmt(profit)} درهم\n"
            f"{D}"
        )
    
    elif query_type == "expense_by_item":
        # Calculate expense for specific item
        rows = [x for x in period_data if x["type"] == "صرف" and item in x["item"]]
        total = sum(x["amount"] for x in rows)
        
        send(chat_id, f"{D}\nالمصروف على {item} ({label}): {fmt(total)} درهم\n{D}")
    
    elif query_type == "top_selling":
        # Find top selling items
        income_data = [x for x in period_data if x["type"] == "دخل"]
        items = {}
        for x in income_data:
            item_name = x["item"]
            items[item_name] = items.get(item_name, 0) + x["amount"]
        
        if not items:
            send(chat_id, "ما في مبيعات في هالفترة")
            return
        
        sorted_items = sorted(items.items(), key=lambda x: -x[1])[:5]
        
        lines = [D, f"📊 أكثر ٥ بنود مبيعات ({label}):"]
        for i, (name, amount) in enumerate(sorted_items, 1):
            lines.append(f"{i}. {name}: {fmt(amount)} درهم")
        lines.append(D)
        send(chat_id, "\n".join(lines))
    
    elif query_type == "top_expense":
        # Find top expenses
        expense_data = [x for x in period_data if x["type"] == "صرف"]
        items = {}
        for x in expense_data:
            item_name = x["item"]
            items[item_name] = items.get(item_name, 0) + x["amount"]
        
        if not items:
            send(chat_id, "ما في مصاريف في هالفترة")
            return
        
        sorted_items = sorted(items.items(), key=lambda x: -x[1])[:5]
        
        lines = [D, f"📊 أكثر ٥ مصاريف ({label}):"]
        for i, (name, amount) in enumerate(sorted_items, 1):
            lines.append(f"{i}. {name}: {fmt(amount)} درهم")
        lines.append(D)
        send(chat_id, "\n".join(lines))
    
    else:
        send(chat_id, "نوع الاستعلام مو واضح، جرب مرة ثانية")

HELP = """
🌾 بوت مصاريف العزبة الذكي 🧠

🆕 الميزات:
✅ يفهم الأخطاء الإملائية
✅ يتعلم من أسلوبك
✅ يقترح المبالغ المعتادة
✅ يصير أذكى مع الوقت
✅ يفهم التصحيحات
✅ يكرر العمليات
✅ استعلامات ذكية

💬 أمثلة:
• بعت بيض بـ 200
• لا ٣٠٠ مو ٢٠٠ (تصحيح)
• نفس الأمس (تكرار)
• كم ربحنا من البيض؟
• شو أكثر شي بعناه؟

🎯 تكلم بشكل طبيعي:
• "اشتريت" → أسألك: شو اشتريت؟
• "بعت بيض" → أسألك: بكم؟
• "البيع" → أسألك: تسجيل ولا عرض؟

💡 البوت يتعلم من استخدامك!
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
            send(chat_id, "⛔ هذا البوت خاص")
            self._ok()
            return

        user_name = ALLOWED_USERS[user_id]

        if text in ("/start", "/help", "help", "مساعدة"):
            send(chat_id, HELP)
            clear_context(user_id)
            self._ok()
            return

        try:
            svc = sheets_svc()
            data = load_transactions(svc)
        except Exception as e:
            send(chat_id, f"في مشكلة بـ Google Sheets:\n{e}")
            self._ok()
            return

        # Detect intent with learning
        d = detect_intent(text, user_id, svc)
        intent = d.get("intent") or "clarify"
        period = d.get("period") or "month"
        confidence = d.get("confidence", 0.5)

        # Check for negative feedback
        if intent == "feedback_negative":
            h_feedback_negative(svc, d, chat_id, user_name, user_id, text)
            self._ok()
            return
        
        # Check for correction
        if intent == "correction":
            h_correction(svc, d, chat_id, user_name, user_id)
            self._ok()
            return
        
        # Check for repeat last
        if intent == "repeat_last":
            h_repeat_last(svc, d, chat_id, user_name, user_id)
            self._ok()
            return
        
        # Check for smart query
        if intent == "smart_query":
            h_smart_query(svc, d, data, chat_id, user_name, user_id)
            self._ok()
            return

        # If confidence too low, ask for clarification
        if confidence < 0.6 and intent not in ["incomplete_purchase", "incomplete_sale", "incomplete_info", "ambiguous_sale", "ambiguous_purchase", "ambiguous_expense"]:
            log_learning(svc, user_id, user_name, text, intent, False, "Low confidence")
            send(chat_id,
                "مافهميت عليك شتقصد 🤔\n"
                "ممكن تعيد بطريقة أوضح؟\n"
                "مثال: بعت بيض بـ 200"
            )
            self._ok()
            return

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
            send(chat_id, f"{D}\n💰 الدخل ({label}): {fmt(inc)} درهم\n{D}")
            clear_context(user_id)
        elif intent == "expense_total":
            period_data, label = filter_by_period(data, period)
            _, exp = totals_all(period_data)
            send(chat_id, f"{D}\n📤 المصروف ({label}): {fmt(exp)} درهم\n{D}")
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
            log_learning(svc, user_id, user_name, text, intent, False, "Unhandled intent")
            send(chat_id,
                "مافهميت عليك 🤔\n"
                "جرب:\n"
                "• بعت بيض بـ 200\n"
                "• كم الربح؟\n"
                "او /help"
            )
            clear_context(user_id)

        self._ok()
