"""
NEXT SYSTEM 1.0 — MVP Telegram Bot (stable, sanitized)
------------------------------------------------------
• Поддержка polling и webhook (FastAPI). Для Replit: MODE=webhook и WEBHOOK_URL.
• Интеграция Google Sheets (gspread + сервис‑аккаунт):
  - ключ через переменную GCP_CREDENTIALS (весь JSON одной строкой)
    ИЛИ через файл + переменную GCP_CREDENTIALS_FILE=service_account.json
• Команды: /start, /offers, /proof, /admin, /pin <code>, /gscheck.
• Флоу: офферы → очередь (с cap/day) → пруф → админ-решение (Approve/Reject/Repeat).
"""
from __future__ import annotations

import os
import json
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request

from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, KeyboardButton,
    ReplyKeyboardMarkup, Update
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

import gspread
from google.oauth2.service_account import Credentials

# ============================ ENV / CONFIG ============================
load_dotenv()

# Режимы запуска
MODE = os.getenv("MODE", "polling").lower()  # polling | webhook
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")   # https://<host>/webhook
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # опционально
PORT = int(os.getenv("PORT", "8080"))

# Telegram / Sheets
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PIN_CODE = os.getenv("PIN_CODE", "1588").strip()

# ID таблицы по умолчанию (вшит твой ID)
DEFAULT_SPREADSHEET_ID = "16f3xq1PZlrGERsvLRNju55Dl-pDd1daK1LSpWNola3A"
SPREADSHEET_ID = os.getenv("GSPREAD_SPREADSHEET_ID", DEFAULT_SPREADSHEET_ID).strip()

_admin_env = os.getenv("ADMIN_IDS", "").strip()
ADMIN_IDS = {int(x) for x in _admin_env.split(",") if x.strip().isdigit()} if _admin_env else set()

if not BOT_TOKEN:
    raise RuntimeError("Missing required envs: BOT_TOKEN. Set it via .env or hosting secrets.")

# ----- Google credentials -----
_creds_info: Optional[Dict[str, Any]] = None
_creds_raw = os.getenv("GCP_CREDENTIALS")
if _creds_raw:
    try:
        _creds_info = json.loads(_creds_raw)
    except json.JSONDecodeError as e:
        raise RuntimeError("GCP_CREDENTIALS must be valid JSON (service account)") from e
else:
    creds_path = os.getenv("GCP_CREDENTIALS_FILE")
    if creds_path and os.path.exists(creds_path):
        with open(creds_path, "r", encoding="utf-8") as f:
            _creds_info = json.load(f)

if not _creds_info:
    raise RuntimeError("Provide GCP_CREDENTIALS (JSON string) or GCP_CREDENTIALS_FILE (path to JSON key)")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
creds = Credentials.from_service_account_info(_creds_info, scopes=SCOPES)
_gs_client = gspread.authorize(creds)

# ============================ SHEETS LAYER ============================
class Sheets:
    def __init__(self, client: gspread.Client, spreadsheet_id: str) -> None:
        self.ss = client.open_by_key(spreadsheet_id)
        self._ensure_ws("Offers", ["offer_id", "name", "cap_daily", "is_active"])
        self._ensure_ws("Drops",  ["tg_user_id", "username", "created_at", "status"])
        self._ensure_ws("Queue",  ["queue_id", "tg_user_id", "offer_id", "queued_at", "status"])
        self._ensure_ws("Proofs", [
            "proof_id", "queue_id", "tg_user_id", "offer_id",
            "file_id", "file_type", "submitted_at", "manager_note", "decision"
        ])

    def _ensure_ws(self, title: str, headers: List[str]):
        import gspread
        try:
            ws = self.ss.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self.ss.add_worksheet(title=title, rows=2000, cols=max(5, len(headers)))
            ws.append_row(headers)
        else:
            first_row = ws.row_values(1)
            if [h.strip() for h in first_row] != headers:
                ws.update("A1", [headers])
        return ws

    def ws(self, title: str):
        return self.ss.worksheet(title)

    # ----- Offers -----
    def list_active_offers(self) -> List[Dict[str, Any]]:
        rows = self.ws("Offers").get_all_records()
        out: List[Dict[str, Any]] = []
        for r in rows:
            active = str(r.get("is_active", "")).strip().upper() in {"TRUE", "1", "YES", "Y"}
            if active:
                out.append({
                    "offer_id": str(r.get("offer_id", "")).strip(),
                    "name": str(r.get("name", "")).strip(),
                    "cap_daily": int(r.get("cap_daily", 0) or 0),
                })
        return out

    # ----- Drops -----
    def ensure_drop(self, tg_user_id: int, username: Optional[str]) -> None:
        ws = self.ws("Drops")
        rows = ws.get_all_records()
        uid = str(tg_user_id)
        for r in rows:
            if str(r.get("tg_user_id", "")) == uid:
                return
        ws.append_row([uid, username or "", datetime.now(timezone.utc).isoformat(), "active"])

    # ----- Queue helpers -----
    def _next_id(self, ws_title: str, id_field: str) -> int:
        rows = self.ws(ws_title).get_all_records()
        max_id = 0
        for r in rows:
            try:
                max_id = max(max_id, int(r.get(id_field, 0) or 0))
            except Exception:
                pass
        return max_id + 1

    def today_assigned_count(self, offer_id: str) -> int:
        rows = self.ws("Queue").get_all_records()
        today = datetime.now(timezone.utc).date().isoformat()
        cnt = 0
        for r in rows:
            if str(r.get("offer_id", "")) == str(offer_id):
                q_at = r.get("queued_at", "")
                if q_at and q_at[:10] == today:
                    if str(r.get("status", "")) in {
                        "IN_QUEUE", "ASSIGNED", "PROOF_REQUIRED", "PROOF_SENT", "REPEAT_REQUIRED"
                    }:
                        cnt += 1
        return cnt

    def join_queue(self, tg_user_id: int, offer_id: str) -> Dict[str, Any]:
        ws = self.ws("Queue")
        qid = self._next_id("Queue", "queue_id")
        ws.append_row([qid, str(tg_user_id), str(offer_id), datetime.now(timezone.utc).isoformat(), "IN_QUEUE"])
        return {"queue_id": qid, "status": "IN_QUEUE"}

    def update_queue_status(self, queue_id: int, status: str) -> None:
        ws = self.ws("Queue")
        rows = ws.get_all_records()
        for idx, r in enumerate(rows, start=2):
            if int(r.get("queue_id", 0) or 0) == queue_id:
                ws.update_cell(idx, 5, status)  # статус — 5-й столбец
                return

    # ----- Proofs -----
    def add_proof(self, queue_id: int, tg_user_id: int, offer_id: str, file_id: str, file_type: str) -> int:
        ws = self.ws("Proofs")
        pid = self._next_id("Proofs", "proof_id")
        ws.append_row([
            pid, queue_id, str(tg_user_id), str(offer_id), file_id, file_type,
            datetime.now(timezone.utc).isoformat(), "", "PENDING"
        ])
        self.update_queue_status(queue_id, "PROOF_SENT")
        return pid

    def decide_proof(self, proof_id: int, decision: str, note: str = "") -> Optional[Dict[str, Any]]:
        ws = self.ws("Proofs")
        rows = ws.get_all_records()
        for idx, r in enumerate(rows, start=2):
            if int(r.get("proof_id", 0) or 0) == proof_id:
                ws.update_cell(idx, 9, decision)
                if note:
                    ws.update_cell(idx, 8, note)
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

# ============================== BOT LAYER =============================
bot = Bot(BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
router = Router()

elevated_admins: set[int] = set()  # /pin выдаёт временные права

# ----- Keyboards -----

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

# ----- Helpers -----

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS or user_id in elevated_admins

async def send_to_admins(text: str, reply_markup: Optional[InlineKeyboardMarkup] = None, photo_file_id: Optional[str] = None):
    targets = ADMIN_IDS.union(elevated_admins)
    for admin_id in targets:
        try:
            if photo_file_id:
                await bot.send_photo(admin_id, photo=photo_file_id, caption=text, reply_markup=reply_markup)
            else:
                await bot.send_message(admin_id, text, reply_markup=reply_markup)
        except Exception:
            pass

# ----- Handlers -----

@router.message(Command("start"))
async def cmd_start(msg: Message):
    sheets.ensure_drop(msg.from_user.id, msg.from_user.username)
    text = (
        "Привет! Я — NEXT SYSTEM бот.
"
        "1) Посмотри активные офферы.
"
        "2) Встань в очередь.
"
        "3) Отправь скрин для модерации через /proof.

"
        "Важно: перед выплатой возможен повторный визит."
    )
    await msg.answer(text, reply_markup=main_menu_kb())

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
    offers = {o["offer_id"]: o for o in sheets.list_active_offers()}
    off = offers.get(offer_id)
    if not off:
        return await cb.answer("Оффер недоступен", show_alert=True)

    # проверяем дневной лимит
    today_count = sheets.today_assigned_count(offer_id)
    if today_count >= off["cap_daily"]:
        return await cb.message.edit_text(
            f"Лимит по офферу <b>{off['name']}</b> на сегодня исчерпан. Попробуй завтра."
        )

    q = sheets.join_queue(cb.from_user.id, offer_id)
    await cb.message.edit_text(
        (
            f"Ты встал в очередь по офферу <b>{off['name']}</b> (queue_id: {q['queue_id']}).
"
            "Следуй инструкциям менеджера и пришли скрины через /proof."
        )
    )

@router.message(F.text == "Встать в очередь")
async def action_queue(msg: Message):
    await list_offers(msg)

@router.message(F.text == "Отправить скрин / доказательство")
@router.message(Command("proof"))
async def prompt_proof(msg: Message):
    await msg.answer(
        (
            "Отправь фото/скрин <b>ответным сообщением на это</b> с подписью:
"
            "queue_id=<номер> offer_id=<id> (без скобок).
"
            "Например: queue_id=12 offer_id=1"
        )
    )

@router.message(F.photo | (F.document & (F.document.mime_type.contains("image"))))
async def receive_proof(msg: Message):
    cap = msg.caption or ""

    def _parse_pair(key: str) -> Optional[str]:
        key_eq = key + "="
        for token in cap.replace("
", " ").split():
            if token.lower().startswith(key_eq):
                return token.split("=", 1)[1]
        return None

    queue_id = _parse_pair("queue_id")
    offer_id = _parse_pair("offer_id")
    if not queue_id or not offer_id:
        return await msg.reply("Добавь подпись к фото: queue_id=<номер> offer_id=<id>")

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

    await send_to_admins(
        text=(
            f"<b>Новый proof</b>
"
            f"proof_id: <code>{pid}</code> | queue_id: <code>{queue_id}</code> | offer_id: <code>{offer_id}</code>
"
            f"from: <a href='tg://user?id={msg.from_user.id}'>{msg.from_user.username or msg.from_user.id}</a>"
        ),
        reply_markup=proof_review_kb(pid),
        photo_file_id=file_id,
    )

    await msg.reply("Скрин получен. Ожидай проверки менеджером.")

# ----- Admin -----
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
    caps = "
".join([f"• {o['name']} — cap/day: {o['cap_daily']} (id={o['offer_id']})" for o in offers]) or "нет"
    await msg.reply(
        (
            "<b>Админ-панель</b>
"
            f"Активные офферы:
{caps}

"
            "Модерируй proofs из уведомлений."
        )
    )

@router.message(Command("gscheck"))
async def cmd_gscheck(msg: Message):
    if not is_admin(msg.from_user.id):
        return await msg.reply("Недостаточно прав.")
    try:
        offers = sheets.list_active_offers()
        names = ", ".join(o["name"] for o in offers) or "нет активных"
        text = (
            "✅ Доступ к Google Sheets OK
"
            f"Spreadsheet ID: <code>{SPREADSHEET_ID}</code>
"
            f"Активные офферы: {names}"
        )
        await msg.reply(text)
    except Exception as e:
        await msg.reply(f"❌ Не удалось обратиться к таблице: {e}")

@router.callback_query(F.data.startswith("prf:"))
async def cb_proof_action(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("Нет прав", show_alert=True)
    try:
        _, proof_id, action = cb.data.split(":", 2)
        proof_id = int(proof_id)
    except Exception:
        return await cb.answer("Ошибка данных", show_alert=True)

    decision = {"ok": "APPROVED", "no": "REJECTED", "rep": "REPEAT_REQUIRED"}.get(action)
    if not decision:
        return await cb.answer("Неизвестное действие", show_alert=True)

    rec = sheets.decide_proof(proof_id, decision)
    if rec is None:
        return await cb.answer("Proof не найден", show_alert=True)

    try:
        await cb.message.edit_caption((cb.message.caption or "") + f"

<b>Решение:</b> {decision}", reply_markup=None)
    except Exception:
        try:
            await cb.message.edit_text((cb.message.text or "") + f"

<b>Решение:</b> {decision}", reply_markup=None)
        except Exception:
            pass
    await cb.answer("Сделано ✅")

# ============================ WEBHOOK APP ============================
dp.include_router(router)

@asynccontextmanager
async def lifespan(app: FastAPI):
    if MODE == "webhook" and WEBHOOK_URL:
        try:
            await bot.set_webhook(WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
        except Exception:
            pass
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

# ============================== ENTRY ===============================

def main() -> None:
    if MODE == "polling":
        asyncio.run(dp.start_polling(bot))
    else:
        import uvicorn
        uvicorn.run("app:app", host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
