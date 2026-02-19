// api/data.js
// GET  /api/data  → returns Transactions + Inventory as JSON for the HTML app
// POST /api/data  → adds a new transaction from the HTML app

import { google } from "googleapis";

const SPREADSHEET_ID    = process.env.SPREADSHEET_ID;
const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const ALLOWED_CHAT_IDS  = [47329648, 6894180427];

// ── CORS ──────────────────────────────────────────────────────────────────────
const CORS = {
  "Access-Control-Allow-Origin":  "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
  "Content-Type": "application/json",
};

// ── Sheets client ─────────────────────────────────────────────────────────────
function getSheetsClient() {
  const sa   = JSON.parse(process.env.GOOGLE_SERVICE_ACCOUNT_JSON);
  const auth = new google.auth.GoogleAuth({
    credentials: { client_email: sa.client_email, private_key: sa.private_key },
    scopes: ["https://www.googleapis.com/auth/spreadsheets"],
  });
  return google.sheets({ version: "v4", auth });
}

async function readSheet(sheets, name, range = "A1:Z") {
  const res = await sheets.spreadsheets.values.get({
    spreadsheetId: SPREADSHEET_ID,
    range: `${name}!${range}`,
  });
  return res.data.values || [];
}

async function appendRow(sheets, name, row) {
  await sheets.spreadsheets.values.append({
    spreadsheetId: SPREADSHEET_ID,
    range: `${name}!A1`,
    valueInputOption: "USER_ENTERED",
    requestBody: { values: [row] },
  });
}

// ── Parse Transactions ────────────────────────────────────────────────────────
// Real sheet columns (from the bot):
//   A=التاريخ | B=النوع(دخل/صرف) | C=البند | D=المبلغ | E=المستخدم | F=ملاحظات
function parseTransactions(rows) {
  // Detect header row dynamically from first row
  if (!rows || rows.length === 0) return [];

  // Figure out which column index holds the amount by checking header
  const header = rows[0].map(h => (h || "").trim());
  const amtIdx  = header.indexOf("المبلغ")  !== -1 ? header.indexOf("المبلغ")  : 3;
  const userIdx = header.indexOf("المستخدم") !== -1 ? header.indexOf("المستخدم") : 4;
  const itemIdx = header.indexOf("البند")    !== -1 ? header.indexOf("البند")    : 2;
  const typeIdx = header.indexOf("النوع")    !== -1 ? header.indexOf("النوع")    : 1;
  const catIdx  = header.indexOf("التصنيف")  !== -1 ? header.indexOf("التصنيف")  : -1;

  const out = [];
  for (let i = 1; i < rows.length; i++) {   // skip header row
    const r = rows[i];
    if (!r || !r[0]) continue;

    const typeRaw  = (r[typeIdx] || "").trim();
    const isIncome = typeRaw === "دخل" || typeRaw.toLowerCase() === "income";
    const amount   = parseFloat(r[amtIdx]) || 0;

    out.push({
      date:     r[0] || "",
      type:     isIncome ? "دخل" : "صرف",
      item:     r[itemIdx] || "",
      category: catIdx !== -1 ? (r[catIdx] || r[itemIdx] || "") : (r[itemIdx] || ""),
      amount,
      user:     r[userIdx] || "",
    });
  }
  return out;
}

// ── Parse Inventory ───────────────────────────────────────────────────────────
function parseInventory(rows) {
  const out = [];
  for (const r of rows) {
    if (!r || !r[0]) continue;
    const first = r[0].trim();
    if (first === "Item" || first === "البند" || first === "") continue;
    out.push({
      item:  r[0],
      type:  r[1] || "",
      qty:   parseInt(r[2]) || 0,
      notes: r[3] || "",
    });
  }
  return out;
}

// ── Telegram notify ───────────────────────────────────────────────────────────
async function notifyTelegram(type, item, amount, user) {
  if (!TELEGRAM_BOT_TOKEN) return;
  const emoji     = type === "دخل" ? "💰" : "📤";
  const text      = `${emoji} [من التطبيق]\n${type}: ${item}\nالمبلغ: ${amount} د.إ\nبواسطة: ${user}`;
  for (const chatId of ALLOWED_CHAT_IDS) {
    try {
      await fetch(`https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id: chatId, text }),
      });
    } catch (_) {}
  }
}

// ── Main handler ──────────────────────────────────────────────────────────────
export default async function handler(req, res) {
  Object.entries(CORS).forEach(([k, v]) => res.setHeader(k, v));

  if (req.method === "OPTIONS") return res.status(204).end();

  // GET — return all data
  if (req.method === "GET") {
    try {
      const sheets = getSheetsClient();
      const [tRows, iRows] = await Promise.all([
        readSheet(sheets, "Transactions", "A1:F"),
        readSheet(sheets, "Inventory",    "A1:D"),
      ]);

      const transactions = parseTransactions(tRows);
      const inventory    = parseInventory(iRows);
      const income  = transactions.filter(x => x.type === "دخل").reduce((s, x) => s + x.amount, 0);
      const expense = transactions.filter(x => x.type === "صرف").reduce((s, x) => s + x.amount, 0);

      return res.status(200).json({
        ok: true,
        transactions,
        inventory,
        summary: { income, expense, profit: income - expense },
      });
    } catch (e) {
      return res.status(500).json({ ok: false, error: e.message });
    }
  }

  // POST — add transaction from HTML app
  if (req.method === "POST") {
    try {
      const { type, item, category, amount, user = "App" } = req.body;
      if (!type || !item || !amount) {
        return res.status(400).json({ ok: false, error: "type, item, amount required" });
      }
      const now    = new Date().toLocaleString("ar-AE", { timeZone: "Asia/Dubai" });
      const sheets = getSheetsClient();
      await appendRow(sheets, "Transactions", [now, type, item, amount, user, category || ""]);
      await notifyTelegram(type, item, amount, user);
      return res.status(200).json({ ok: true, message: "تم التسجيل" });
    } catch (e) {
      return res.status(500).json({ ok: false, error: e.message });
    }
  }

  return res.status(405).json({ ok: false, error: "Method not allowed" });
}
