"""
NEXT SYSTEM 1.0 — MVP Telegram Bot (single-file)
=================================================

Stack:
- Python 3.10+
- aiogram==3.* (Telegram Bot API)
- gspread==5.* + google-auth (Google Sheets)
- python-dotenv (optional, for local .env)

Environment variables (required):
- BOT_TOKEN                 -> Telegram Bot token
- ADMIN_IDS                 -> comma-separated Telegram user IDs with admin rights, e.g. "123,456"
- PIN_CODE                  -> numeric PIN to elevate to admin from chat (default: 1588)
- GSPREAD_SPREADSHEET_ID    -> target Google Spreadsheet ID
- GCP_CREDENTIALS           -> **JSON string** of a Google Service Account key

Google Spreadsheet structure (create worksheets with these headers):
1) Sheet: Offers
   Columns: [offer_id, name, cap_daily, is_active]
   Example rows:
     1, Casino-X, 10, TRUE
     2, Crypto-Y, 5, TRUE

2) Sheet: Drops
   Columns: [tg_user_id, username, created_at, status]
   status ∈ {new, active, banned}

3) Sheet: Queue
   Columns: [queue_id, tg_user_id, offer_id, queued_at, status]
   status ∈ {IN_QUEUE, ASSIGNED, PROOF_REQUIRED, PROOF_SENT, REPEAT_REQUIRED, APPROVED, REJECTED}

4) Sheet: Proofs
   Columns: [proof_id, queue_id, tg_user_id, offer_id, file_id, file_type, submitted_at, manager_note, decision]
   decision ∈ {APPROVED, REJECTED, PENDING}

MVP Flow:
- /start — регистрация дропа (если новый) + меню
- «Список офферов» — читает Offers (is_active=TRUE)
- «Встать в очередь» — создаёт запись в Queue (status=IN_QUEUE) с проверкой дневного лимита
- /proof — отправь фото/скрин → уходит админам на модерацию (кнопки Approve/Reject/Need repeat)
- Админ видит карточку proof с инлайн-кнопками, решение пишется в Sheets
- Если Need repeat → статус очереди REPEAT_REQUIRED, дропу приходит указание сделать повторный визит

Admin:
- /admin — быстрые метрики + ссылка на список офферов
- /pin <code> — временно повысить права, если твой ID не в ADMIN_IDS (PIN_CODE по умолчанию 1588)

NOTE: это MVP; в проде стоит добавить кэширование, ретраи к Sheets, и полноценные ролей/прав.
"""
from __future__ import annotations

import os
import json
import asyncio
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ContentType
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from fastapi import FastAPI, Request
from contextlib import asynccontextmanager
from aiogram.types import Update
import os as _os_for_mode

import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

load_dotenv()
# ------------------------------ Config ------------------------------
MODE = os.getenv("MODE", "polling").lower()  # polling | webhook
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # e.g. https://your-domain/webhook
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # optional shared secret

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DEFAULT_SPREADSHEET_ID = "16f3xq1PZlrGERsvLRNju55Dl-pDd1daK1LSpWNola3A"  # from user link
SPREADSHEET_ID = os.getenv("GSPREAD_SPREADSHEET_ID", DEFAULT_SPREADSHEET_ID)
PIN_CODE = os.getenv("PIN_CODE", "1588")

_admin_env = os.getenv("ADMIN_IDS", "").strip()
ADMIN_IDS = {int(x) for x in _admin_env.split(",") if x.strip().isdigit()} if _admin_env else set()

if not BOT_TOKEN:
    raise RuntimeError("Missing required envs: BOT_TOKEN. Set it via .env or hosting secrets.") and PIN_CODE."
    )

# Google credentials from JSON string env (or file)
_creds_info = None
_creds_raw = os.getenv("GCP_CREDENTIALS")
if _creds_raw:
    try:
        _creds_info = json.loads(_creds_raw)
    except json.JSONDecodeError as e:
        raise RuntimeError("GCP_CREDENTIALS must be a valid JSON string of the service account key") from e
else:
    creds_path = os.getenv("GCP_CREDENTIALS_FILE")
    if creds_path and os.path.exists(creds_path):
        with open(creds_path, "r", encoding="utf-8") as f:
            _creds_info = json.load(f)

if not _creds_info:
    raise RuntimeError("Provide GCP_CREDENTIALS (JSON string) or GCP_CREDENTIALS_FILE (path to JSON key)")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
creds = Credentials.from_service_account_info(_creds_info, scopes=SCOPES)
_gs_client = gspread.authorize(creds)

# Open sheets (lazy wrappers)
class Sheets:
    def __init__(self, client: gspread.Client, spreadsheet_id: str) -> None:
        self.client = client
        self.ss = client.open_by_key(spreadsheet_id)
        # Ensure worksheets exist
        self._ensure_ws("Offers", ["offer_id", "name", "cap_daily", "is_active"])
        self._ensure_ws("Drops", ["tg_user_id", "username", "created_at", "status"])
        self._ensure_ws("Queue", ["queue_id", "tg_user_id", "offer_id", "queued_at", "status"])
        self._ensure_ws(
            "Proofs",
            ["proof_id", "queue_id", "tg_user_id", "offer_id", "file_id", "file_type", "submitted_at", "manager_note", "decision"]
        )

    def _ensure_ws(self, title: str, headers: List[str]):
        try:
            ws = self.ss.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self.ss.add_worksheet(title=title, rows=2000, cols=max(5, len(headers)))
            ws.append_row(headers)
        else:
            # Ensure headers exist
            first_row = ws.row_values(1)
            if [h.strip() for h in first_row] != headers:
                # Overwrite first row to expected headers (idempotent)
                ws.update("A1", [headers])
        return ws

    # Helpers
    def ws(self, title: str):
        return self.ss.worksheet(title)

    # Offers
    def list_active_offers(self) -> List[Dict[str, Any]]:
        ws = self.ws("Offers")
        rows = ws.get_all_records()
        out = []
        for r in rows:
            if str(r.get("is_active", "")).strip().upper() in {"TRUE", "1", "YES", "Y"}:
                out.append({
                    "offer_id": str(r.get("offer_id", "")).strip(),
                    "name": str(r.get("name", "")).strip(),
                    "cap_daily": int(r.get("cap_daily", 0) or 0),
                })
        return out

    # Drops
    def ensure_drop(self, tg_user_id: int, username: str | None) -> None:
        ws = self.ws("Drops")
        rows = ws.get_all_records()
        uid = str(tg_user_id)
        for idx, r in enumerate(rows, start=2):
            if str(r.get("tg_user_id", "")) == uid:
                return
        ws.append_row([uid, username or "", datetime.now(timezone.utc).isoformat(), "active"])

    # Queue
    def _next_id(self, ws_title: str, id_field: str) -> int:
        ws = self.ws(ws_title)
        rows = ws.get_all_records()
        max_id = 0
        for r in rows:
            try:
                max_id = max(max_id, int(r.get(id_field, 0) or 0))
            except Exception:
                pass
        return max_id + 1

    def today_assigned_count(self, offer_id: str) -> int:
        ws = self.ws("Queue")
        rows = ws.get_all_records()
        today = datetime.now(timezone.utc).date().isoformat()
        cnt = 0
        for r in rows:
            if str(r.get("offer_id", "")) == str(offer_id):
                q_at = r.get("queued_at", "")
                if q_at and q_at[:10] == today:
                    if str(r.get("status", "")) in {"IN_QUEUE", "ASSIGNED", "PROOF_REQUIRED", "PROOF_SENT", "REPEAT_REQUIRED"}:
                        cnt += 1
        return cnt

    def join_queue(self, tg_user_id: int, offer_id: str) -> Dict[str, Any]:
        ws = self.ws("Queue")
        qid = self._next_id("Queue", "queue_id")
        row = [qid, str(tg_user_id), str(offer_id), datetime.now(timezone.utc).isoformat(), "IN_QUEUE"]
        ws.append_row(row)
        return {"queue_id": qid, "status": "IN_QUEUE"}

    def update_queue_status(self, queue_id: int, status: str) -> None:
        ws = self.ws("Queue")
        rows = ws.get_all_records()
        for idx, r in enumerate(rows, start=2):
            if int(r.get("queue_id", 0) or 0) == queue_id:
                ws.update_cell(idx, 5, status)  # status col is 5
                return

    # Proofs
    def add_proof(self, queue_id: int, tg_user_id: int, offer_id: str, file_id: str, file_type: str) -> int:
        ws = self.ws("Proofs")
        pid = self._next_id("Proofs", "proof_id")
        ws.append_row([
            pid, queue_id, str(tg_user_id), str(offer_id), file_id, file_type,
            datetime.now(timezone.utc).isoformat(), "", "PENDING"
        ])
        # also mark queue status
        self.update_queue_status(queue_id, "PROOF_SENT")
        return pid

    def decide_proof(self, proof_id: int, decision: str, note: str = "") -> Optional[Dict[str, Any]]:
        ws = self.ws("Proofs")
        rows = ws.get_all_records()
        for idx, r in enumerate(rows, start=2):
            if int(r.get("proof_id", 0) or 0) == proof_id:
                ws.update_cell(idx, 9, decision)          # decision
                if note:
                    ws.update_cell(idx, 8, note)          # manager_note
                # Update queue status accordingly
                qid = int(r.get("queue_id", 0) or 0)
                if decision == "APPROVED":
                    self.update_queue_status(qid, "APPROVED")
                elif decision == "REJECTED":
                    self.update_queue_status(qid, "REJECTED")
                elif decision == "REPEAT_REQUIRED":
                    self.update_queue_status(qid, "REPEAT_REQUIRED")
                return r
        return None

sheets = Sheets(_gs_client, SPREADSHEET_ID)

# ---------------------------- Bot setup -----------------------------
bot = Bot(BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
router = Router()

elevated_admins: set[int] = set()  # granted via /pin during runtime

# -------------------------- UI Components --------------------------

def main_menu_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.add(KeyboardButton(text="Список офферов"))
    kb.add(KeyboardButton(text="Встать в очередь"))
    kb.add(KeyboardButton(text="Отправить скрин / доказательство"))
    return kb.as_markup(resize_keyboard=True)


def offers_inline_kb(offers: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for off in offers:
        b.button(text=f"{off['name']} (лимит/день: {off['cap_daily']})", callback_data=f"offer:{off['offer_id']}")
    b.adjust(1)
    return b.as_markup()


def proof_review_kb(proof_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Approve", callback_data=f"prf:{proof_id}:ok")
    b.button(text="❌ Reject", callback_data=f"prf:{proof_id}:no")
    b.button(text="🔁 Need repeat", callback_data=f"prf:{proof_id}:rep")
    b.adjust(3)
    return b.as_markup()

# -------------------------- Helpers/Guards -------------------------

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS or user_id in elevated_admins

async def send_to_admins(text: str, reply_markup: Optional[InlineKeyboardMarkup] = None, photo_file_id: Optional[str] = None):
    for admin_id in ADMIN_IDS.union(elevated_admins):
        try:
            if photo_file_id:
                await bot.send_photo(admin_id, photo=photo_file_id, caption=text, reply_markup=reply_markup)
            else:
                await bot.send_message(admin_id, text, reply_markup=reply_markup)
        except Exception:
            pass

# --------------------------- Handlers ------------------------------

@router.message(Command("start"))
async def cmd_start(msg: Message):
    sheets.ensure_drop(msg.from_user.id, msg.from_user.username)
    await msg.answer(
        "Привет! Я — NEXT SYSTEM бот.\n" \
        "1) Посмотри активные офферы.\n" \
        "2) Встань в очередь.\n" \
        "3) Отправь скрин для модерации.\n\n" \
        "Важно: перед выплатой может потребоваться повторный визит.",
        reply_markup=main_menu_kb()
    )

@router.message(F.text == "Список офферов")
@router.message(Command("offers"))
async def list_offers(msg: Message):
    offers = sheets.list_active_offers()
    if not offers:
        return await msg.answer("Пока нет активных офферов. Залетай позже.")
    await msg.answer("Выбери оффер:", reply_markup=offers_inline_kb(offers))

@router.callback_query(F.data.startswith("offer:"))
async def offer_selected(cb: CallbackQuery):
    offer_id = cb.data.split(":", 1)[1]
    # check cap per day
    offers = {o["offer_id"]: o for o in sheets.list_active_offers()}
    off = offers.get(offer_id)
    if not off:
        return await cb.answer("Оффер недоступен", show_alert=True)
    today_count = sheets.today_assigned_count(offer_id)
    if today_count >= off["cap_daily"]:
        return await cb.message.edit_text(f"Лимит по офферу <b>{off['name']}</b> на сегодня исчерпан. Попробуй завтра.")

    q = sheets.join_queue(cb.from_user.id, offer_id)
    await cb.message.edit_text(
        f"Ты встал в очередь по офферу <b>{off['name']}</b> (queue_id: {q['queue_id']}).\n" \
        "Следуй инструкциям менеджера и пришли скрины через /proof."
    )

@router.message(F.text == "Встать в очередь")
async def action_queue(msg: Message):
    await list_offers(msg)

@router.message(F.text == "Отправить скрин / доказательство")
@router.message(Command("proof"))
async def prompt_proof(msg: Message):
    await msg.answer(
        "Отправь фото/скрин <b>ответным сообщением на это</b> с подписью: \n"
        "queue_id=<номер> offer_id=<id> (без скобок).\n"
        "Например: queue_id=12 offer_id=1"
    )

@router.message(F.photo | (F.document & (F.document.mime_type.contains("image"))))
async def receive_proof(msg: Message):
    # Expect caption like: queue_id=12 offer_id=1
    cap = msg.caption or ""
    def _parse_pair(key: str) -> Optional[str]:
        key_eq = key + "="
        for token in cap.replace("\n", " ").split():
            if token.lower().startswith(key_eq):
                return token.split("=", 1)[1]
        return None

    queue_id = _parse_pair("queue_id")
    offer_id = _parse_pair("offer_id")
    if not queue_id or not offer_id:
        return await msg.reply("Добавь подпись к фото: queue_id=<номер> offer_id=<id>")

    # Grab file id
    file_id = None
    file_type = "photo"
    if msg.photo:
        file_id = msg.photo[-1].file_id
    elif msg.document and msg.document.mime_type and "image" in msg.document.mime_type:
        file_id = msg.document.file_id
        file_type = msg.document.mime_type

    if not file_id:
        return await msg.reply("Не вижу изображения. Пришли фото или image-документ.")

    try:
        pid = sheets.add_proof(int(queue_id), msg.from_user.id, str(offer_id), file_id, file_type)
    except Exception as e:
        return await msg.reply(f"Ошибка записи в таблицу: {e}")

    # Notify admins
    await send_to_admins(
        text=(
            f"<b>Новый proof</b>\n"
            f"proof_id: <code>{pid}</code> | queue_id: <code>{queue_id}</code> | offer_id: <code>{offer_id}</code>\n"
            f"from: <a href='tg://user?id={msg.from_user.id}'>{msg.from_user.username or msg.from_user.id}</a>"
        ),
        reply_markup=proof_review_kb(pid),
        photo_file_id=file_id,
    )

    await msg.reply("Скрин получен. Ожидай проверки менеджером.")

# -------------------------- Admin Handlers -------------------------

@router.message(Command("gscheck"))
async def cmd_gscheck(msg: Message):
    if not is_admin(msg.from_user.id):
        return await msg.reply("Недостаточно прав.")
    try:
        offers = sheets.list_active_offers()
        names = ", ".join(o["name"] for o in offers) or "нет активных"
        await msg.reply(
            "✅ Доступ к Google Sheets OK
"
            f"Spreadsheet ID: <code>{SPREADSHEET_ID}</code>
"
            f"Активные офферы: {names}"
        )
    except Exception as e:
        await msg.reply(f"❌ Не удалось обратиться к таблице: {e}")


@router.message(Command("pin"))
async def cmd_pin(msg: Message, command: CommandObject):
    code = (command.args or "").strip()
    if not code:
        return await msg.reply("Используй: /pin 1234")
    if code == PIN_CODE:
        elevated_admins.add(msg.from_user.id)
        await msg.reply("Права админа выданы на текущую сессию. ✅")
    else:
        await msg.reply("Неверный PIN.")

@router.message(Command("admin"))
async def cmd_admin(msg: Message):
    if not is_admin(msg.from_user.id):
        return await msg.reply("Недостаточно прав.")
    offers = sheets.list_active_offers()
    caps = "\n".join([f"• {o['name']} — cap/day: {o['cap_daily']} (id={o['offer_id']})" for o in offers]) or "нет"
    await msg.reply(
        "<b>Админ-панель</b>\n" \
        f"Активные офферы:\n{caps}\n\n" \
        "Модерируй proofs из уведомлений. Команды будут добавлены позже."
    )

@router.callback_query(F.data.startswith("prf:"))
async def cb_proof_action(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет прав", show_alert=True)
    # data: prf:<proof_id>:<action>
    try:
        _, proof_id, action = cb.data.split(":", 2)
        proof_id = int(proof_id)
    except Exception:
        return await cb.answer("Ошибка данных", show_alert=True)

    decision = {
        "ok": "APPROVED",
        "no": "REJECTED",
        "rep": "REPEAT_REQUIRED"
    }.get(action)
    if not decision:
        return await cb.answer("Неизвестное действие", show_alert=True)

    rec = sheets.decide_proof(proof_id, decision)
    if rec is None:
        return await cb.answer("Proof не найден", show_alert=True)

    await cb.message.edit_caption(
        (cb.message.caption or "") + f"\n\n<b>Решение:</b> {decision}",
        reply_markup=None
    )
    await cb.answer("Сделано ✅")

# ---------------------------- Webhook App ---------------------------

dp.include_router(router)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # set webhook on startup if URL provided
    if MODE == "webhook" and WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
    yield
    if MODE == "webhook":
        try:
            await bot.delete_webhook(drop_pending_updates=True)
        except Exception:
            pass

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def health():
    return {"ok": True, "mode": MODE}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.model_validate(data)
    await dp.feed_update(bot, update)
    return {"ok": True}

# ---------------------------- Entrypoint ---------------------------

def main() -> None:
    if MODE == "polling":
        asyncio.run(dp.start_polling(bot))
    else:
        # For local webhook testing
        import uvicorn
        uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")))

if __name__ == "__main__":
    main()

# ---------------------------- Dockerfile ----------------------------
# (Save as Dockerfile if deploying to Cloud Run)
#
# FROM python:3.11-slim
# WORKDIR /app
# COPY requirements.txt ./
# RUN pip install --no-cache-dir -r requirements.txt
# COPY . .
# ENV MODE=webhook
# ENV PORT=8080
# CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]

# -------------------------- requirements.txt ------------------------
# aiogram==3.*
# gspread==5.*
# google-auth==2.*
# python-dotenv
# fastapi
# uvicorn
