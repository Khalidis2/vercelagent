// api/telegram-webhook.js
import OpenAI from "openai";
import { google } from "googleapis";

const openai = new OpenAI({
  apiKey: process.env.OPENAI_API_KEY,
});

function getSheetsClient() {
  const serviceAccount = JSON.parse(process.env.GOOGLE_SERVICE_ACCOUNT_JSON);
  const auth = new google.auth.GoogleAuth({
    credentials: {
      client_email: serviceAccount.client_email,
      private_key: serviceAccount.private_key,
    },
    scopes: ["https://www.googleapis.com/auth/spreadsheets"],
  });
  const sheets = google.sheets({ version: "v4", auth });
  return sheets;
}

async function appendTransactionRow(parsed) {
  const sheets = getSheetsClient();
  const values = [
    [
      new Date().toISOString(),
      parsed.action || "",
      parsed.item || "",
      parsed.amount ?? "",
      parsed.person || "",
      parsed.notes || "",
    ],
  ];

  await sheets.spreadsheets.values.append({
    spreadsheetId: process.env.SPREADSHEET_ID,
    range: "Transactions!A1",
    valueInputOption: "USER_ENTERED",
    requestBody: { values },
  });
}

async function sendTelegramMessage(chatId, text) {
  const url = `https://api.telegram.org/bot${process.env.TELEGRAM_BOT_TOKEN}/sendMessage`;
  await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      chat_id: chatId,
      text,
    }),
  });
}

async function callAiToParse(text, fromName) {
  const response = await openai.responses.create({
    model: "gpt-5.1-mini",
    input: [
      {
        role: "developer",
        content: `
أنت مساعد لحسابات عائلة لمزرعة (عزبة). رسالتك ستكون دائما بصيغة JSON فقط بدون أي نص إضافي.

هدفك:
- فهم الرسالة بالعربي وتحديد هل هي:
  - مصروف (expense)
  - دخل / بيع (income)
  - تحديث مخزون (inventory)
- استخراج المعلومات المهمة.

صيغة JSON المطلوبة (دائماً بهذا الشكل):

{
  "action": "expense" | "income" | "inventory",
  "item": "وصف مختصر للبند",
  "amount": رقم بالمبلغ بالدرهم (أو null إذا غير معروف),
  "person": "اسم الشخص الذي دفع أو استلم (إن وجد)",
  "notes": "أي ملاحظات إضافية مختصرة"
}

تعليمات:
- إذا كان المبلغ مكتوب بالحروف حوّله إلى رقم إن أمكن.
- إذا لم يتضح الشخص، استخدم الاسم المرسل: "${fromName}".
- لا تضف أي حقول أخرى.
- لا تكتب أي نص خارج JSON.
        `.trim(),
      },
      {
        role: "user",
        content: text,
      },
    ],
    response_format: {
      type: "json_schema",
      json_schema: {
        name: "ezba_transaction",
        schema: {
          type: "object",
          properties: {
            action: {
              type: "string",
              enum: ["expense", "income", "inventory"],
            },
            item: { type: "string" },
            amount: {
              anyOf: [{ type: "number" }, { type: "null" }],
            },
            person: { type: "string" },
            notes: { type: "string" },
          },
          required: ["action", "item", "amount", "person", "notes"],
          additionalProperties: false,
        },
        strict: true,
      },
    },
  });

  const outputText = response.output[0].content[0].text;
  return JSON.parse(outputText);
}

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.status(200).send("OK");
    return;
  }

  try {
    const update = req.body;

    const message = update.message || update.edited_message;
    if (!message || !message.text) {
      res.status(200).send("no message");
      return;
    }

    const chatId = message.chat.id;
    const text = message.text;
    const fromName =
      (message.from && (message.from.first_name || message.from.username)) ||
      "غير معروف";

    const parsed = await callAiToParse(text, fromName);
    await appendTransactionRow(parsed);

    const humanAmount =
      parsed.amount !== null && parsed.amount !== undefined
        ? `${parsed.amount} درهم`
        : "بدون مبلغ محدد";

    let typeText = "";
    if (parsed.action === "expense") typeText = "مصروف";
    else if (parsed.action === "income") typeText = "دخل";
    else if (parsed.action === "inventory") typeText = "تحديث مخزون";

    const reply = [
      `تم تسجيل العملية ✅`,
      `النوع: ${typeText}`,
      `البند: ${parsed.item}`,
      `المبلغ: ${humanAmount}`,
      `الشخص: ${parsed.person}`,
      parsed.notes ? `ملاحظات: ${parsed.notes}` : "",
    ]
      .filter(Boolean)
      .join("\n");

    await sendTelegramMessage(chatId, reply);

    res.status(200).json({ ok: true });
  } catch (err) {
    console.error("Error:", err);
    if (req.body && req.body.message && req.body.message.chat) {
      try {
        await sendTelegramMessage(
          req.body.message.chat.id,
          "صار خطأ في تسجيل العملية. حاول مرة ثانية."
        );
      } catch {}
    }
    res.status(500).json({ ok: false });
  }
}
