// telegram-webhook.js
import OpenAI from "openai";
import { google } from "googleapis";

const openai = new OpenAI({
  apiKey: process.env.OPENAI_API_KEY,
});

/* ---------------- Google Sheets ---------------- */

function getSheetsClient() {
  const raw = process.env.GOOGLE_SERVICE_ACCOUNT_JSON;
  if (!raw) throw new Error("Missing GOOGLE_SERVICE_ACCOUNT_JSON");

  const serviceAccount = JSON.parse(raw);

  const auth = new google.auth.GoogleAuth({
    credentials: {
      client_email: serviceAccount.client_email,
      private_key: serviceAccount.private_key,
    },
    scopes: ["https://www.googleapis.com/auth/spreadsheets"],
  });

  return google.sheets({ version: "v4", auth });
}

async function appendTransactionRow(parsed) {
  const spreadsheetId = process.env.SPREADSHEET_ID;
  if (!spreadsheetId) throw new Error("Missing SPREADSHEET_ID");

  const sheets = getSheetsClient();

  const values = [
    [
      new Date().toISOString(),
      parsed.action,
      parsed.item,
      parsed.amount ?? "",
      parsed.person,
      parsed.notes,
    ],
  ];

  await sheets.spreadsheets.values.append({
    spreadsheetId,
    range: "Transactions!A1",
    valueInputOption: "USER_ENTERED",
    requestBody: { values },
  });
}

/* ---------------- Telegram ---------------- */

async function sendTelegramMessage(chatId, text) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  const url = `https://api.telegram.org/bot${token}/sendMessage`;

  await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      chat_id: chatId,
      text,
    }),
  });
}

/* ---------------- OpenAI ---------------- */

async function callAiToParse(text, fromName) {
  const completion = await openai.chat.completions.create({
    model: "gpt-4o-mini",
    messages: [
      {
        role: "system",
        content: `
أنت مساعد لتسجيل عمليات مزرعة (عزبة).
أجب دائماً بصيغة JSON فقط بدون أي نص إضافي.

حدد نوع العملية:
- expense = مصروف
- income = دخل / بيع
- inventory = تعديل عدد الحيوانات

الصيغة المطلوبة:

{
  "action": "expense | income | inventory",
  "item": "وصف مختصر",
  "amount": رقم أو null,
  "person": "اسم الشخص",
  "notes": "ملاحظات مختصرة"
}

تعليمات:
- افهم العربية الطبيعية
- حوّل المبالغ إلى أرقام
- إذا لم يُذكر الشخص استخدم "${fromName}"
- لا تضف أي شرح خارج JSON
        `.trim(),
      },
      { role: "user", content: text },
    ],
    response_format: {
      type: "json_schema",
      json_schema: {
        name: "ezba_transaction",
        strict: true,
        schema: {
          type: "object",
          properties: {
            action: {
              type: "string",
              enum: ["expense", "income", "inventory"],
            },
            item: { type: "string" },
            amount: { anyOf: [{ type: "number" }, { type: "null" }] },
            person: { type: "string" },
            notes: { type: "string" },
          },
          required: ["action", "item", "amount", "person", "notes"],
          additionalProperties: false,
        },
      },
    },
  });

  return JSON.parse(completion.choices[0].message.content);
}

/* ---------------- Main Handler ---------------- */

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.status(200).send("OK");
    return;
  }

  const message = req.body.message || req.body.edited_message;
  if (!message || !message.text) {
    res.status(200).send("no message");
    return;
  }

  const chatId = message.chat.id;
  const text = message.text.trim();
  const fromName =
    message.from?.first_name || message.from?.username || "غير معروف";

  /* ---------- Commands ---------- */

  if (text === "/start") {
    await sendTelegramMessage(
      chatId,
      "مرحباً 👋\nأنا مساعد تسجيل عمليات العزبة.\nاكتب /help لمعرفة طريقة الاستخدام."
    );
    res.status(200).send("ok");
    return;
  }

  if (text === "/help") {
    await sendTelegramMessage(
      chatId,
      `
📌 *طريقة الاستخدام*

اكتب العملية بشكل طبيعي، أمثلة:

• اشتريت علف بـ 500
• بعت خروف بـ 1200
• دخل 300 من بيع حليب
• زاد عدد الغنم 5
• نقص عدد الغنم 2

📊 سيتم:
- فهم العملية
- تسجيلها تلقائياً
- تأكيدها لك

لا تحتاج أوامر خاصة، فقط اكتب بالعربي 👍
      `.trim()
    );
    res.status(200).send("ok");
    return;
  }

  /* ---------- Normal Message ---------- */

  try {
    const parsed = await callAiToParse(text, fromName);

    let saved = true;
    try {
      await appendTransactionRow(parsed);
    } catch (e) {
      saved = false;
      console.error("Sheets error:", e);
    }

    const amountText =
      parsed.amount !== null ? `${parsed.amount} درهم` : "بدون مبلغ";

    const typeText =
      parsed.action === "expense"
        ? "مصروف"
        : parsed.action === "income"
        ? "دخل"
        : "تعديل مخزون";

    let reply = `
تم فهم العملية ✅
النوع: ${typeText}
البند: ${parsed.item}
المبلغ: ${amountText}
الشخص: ${parsed.person}
    `.trim();

    if (!saved) {
      reply += `\n\n⚠️ لم يتم الحفظ في Google Sheets (تحقق من الإعدادات)`;
    } else {
      reply = reply.replace("تم فهم العملية", "تم تسجيل العملية");
    }

    await sendTelegramMessage(chatId, reply);
    res.status(200).json({ ok: true });
  } catch (err) {
    console.error("Fatal error:", err);
    await sendTelegramMessage(
      chatId,
      "صار خطأ في فهم الرسالة. حاول كتابتها بجملة واحدة واضحة."
    );
    res.status(500).json({ ok: false });
  }
}
