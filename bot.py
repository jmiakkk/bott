# -*- coding: utf-8 -*-
# bot.py — Patched.to Auth Key → Telegram (cookies.json) + нормализация cookies + админ-панель + @username
# ВЫДАЧА КОДА ТОЛЬКО ПО КНОПКЕ "Получить код"

import asyncio
import json
import time
import sqlite3
import hashlib
import re
from datetime import datetime, timezone
from typing import Optional, List
import logging

from aiogram import Bot, Dispatcher, Router, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.client.bot import DefaultBotProperties
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------- НАСТРОЙКИ ----------
BOT_TOKEN = "8051240302:AAHU0ZiNm-2jSJ-jTNDoLEq7SPuFNEm8CSQ"
ADMIN_ID = 7640123145

AUTH_URL = "https://patched.to/auth.php"   # страница, где находится AUTH KEY
COOKIES_FILE = "cookies.json"
PATCHED_URL = "https://patched.to"         # базовый URL для cookies

CHECK_EVERY_SEC = 60  # фон. проверка (не используется — мониторинг выключен)
DB_PATH = "monitor_bot.sqlite3"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Получить код")]],
    resize_keyboard=True
)

# ---------- БАЗА ДАННЫХ ----------
def db_init():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users(
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            expires_at INTEGER NOT NULL DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS kv(
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    con.commit(); con.close()

def db_set(key: str, val: str):
    con = sqlite3.connect(DB_PATH)
    con.execute("INSERT INTO kv(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, val))
    con.commit(); con.close()

def db_get(key: str) -> Optional[str]:
    con = sqlite3.connect(DB_PATH)
    cur = con.execute("SELECT value FROM kv WHERE key=?", (key,))
    row = cur.fetchone(); con.close()
    return row[0] if row else None

def db_add_or_update_user(chat_id: int, username: Optional[str]):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO users(chat_id,username) VALUES(?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET username=excluded.username",
        (chat_id, username or "")
    )
    con.commit(); con.close()

def db_set_subscription(chat_id: int, days: int) -> int:
    now = int(time.time()); add = days * 86400
    con = sqlite3.connect(DB_PATH)
    cur = con.execute("SELECT expires_at FROM users WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    if row:
        cur_exp = int(row[0] or 0)
        new_exp = (now + add) if cur_exp < now else (cur_exp + add)
        con.execute("UPDATE users SET expires_at=? WHERE chat_id=?", (new_exp, chat_id))
    else:
        new_exp = now + add
        con.execute("INSERT INTO users(chat_id,username,expires_at) VALUES(?,?,?)", (chat_id, "", new_exp))
    con.commit(); con.close()
    return new_exp

def db_get_subscription(chat_id: int) -> int:
    con = sqlite3.connect(DB_PATH)
    cur = con.execute("SELECT expires_at FROM users WHERE chat_id=?", (chat_id,))
    row = cur.fetchone(); con.close()
    return int(row[0]) if row else 0

def db_active_subscribers() -> List[int]:
    now = int(time.time())
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT chat_id FROM users WHERE expires_at>?", (now,)).fetchall()
    con.close()
    return [r[0] for r in rows]

def db_find_chat_id_by_username(username_or_at: str) -> Optional[int]:
    uname = (username_or_at or "").lstrip("@").lower()
    if not uname:
        return None
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT chat_id FROM users WHERE LOWER(username)=?", (uname,)).fetchone()
    con.close()
    return int(row[0]) if row else None

# ---------- УТИЛИТЫ ----------
def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()

def fmt_ts(ts: int) -> str:
    if ts <= 0: return "нет"
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")

# ---------- НОРМАЛИЗАЦИЯ COOKIES ДЛЯ PLAYWRIGHT ----------
def _normalize_same_site(v):
    if v is None:
        return "Lax"
    s = str(v).strip().lower()
    if s in ("none", "no_restriction"):
        return "None"
    if s in ("strict",):
        return "Strict"
    return "Lax"

def _load_and_sanitize_cookies(path="cookies.json"):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    raw = [data] if isinstance(data, dict) else list(data)
    cookies = []
    for c in raw:
        name = c.get("name")
        if not name:
            continue
        value = c.get("value", "")
        url = c.get("url") or PATCHED_URL

        exp = c.get("expires", c.get("expirationDate"))
        if exp is not None:
            try:
                exp = int(float(exp))
            except Exception:
                exp = None

        http_only = bool(c.get("httpOnly", c.get("http_only", False)))
        secure    = bool(c.get("secure", True))
        same_site = _normalize_same_site(c.get("sameSite"))

        cookie = {
            "name": name,
            "value": value,
            "url": url,
            "httpOnly": http_only,
            "secure": secure,
            "sameSite": same_site,
        }
        if exp is not None:
            cookie["expires"] = exp

        cookies.append(cookie)

    return cookies

# ---------- ВЗЯТЬ ИМЕННО AUTH KEY С auth.php ----------
async def get_auth_key_by_cookies() -> Optional[str]:
    try:
        cookies = _load_and_sanitize_cookies(COOKIES_FILE)
    except FileNotFoundError:
        logging.error("Файл cookies.json не найден рядом с bot.py")
        return None
    except Exception as e:
        logging.error("Ошибка чтения cookies.json: %s", e)
        return None

    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        await context.add_cookies(cookies)

        page = await context.new_page()
        await page.goto(AUTH_URL, wait_until="domcontentloaded")
        try:
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            # 1) #authKey
            key_val = await page.evaluate("""() => {
                const el = document.querySelector('#authKey');
                if (!el) return null;
                if ('value' in el && el.value) return el.value.trim();
                const dc = el.getAttribute?.('data-clipboard-text');
                if (dc) return dc.trim();
                if (el.textContent) return el.textContent.trim();
                return null;
            }""")

            # 2) name="authKey"
            if not key_val:
                key_val = await page.evaluate("""() => {
                    const el = document.querySelector('input[name="authKey"]');
                    if (!el) return null;
                    const v = (el.value || el.getAttribute?.('data-clipboard-text') || el.textContent || '').trim();
                    return v || null;
                }""")

            # 3) подпись AUTH KEY → ближайший элемент
            if not key_val:
                key_val = await page.evaluate("""() => {
                    const label = Array.from(document.querySelectorAll('label, div, span, b, strong, p, h1,h2,h3,h4'))
                        .find(n => /auth\\s*key/i.test(n.textContent || ''));
                    if (!label) return null;
                    const scopes = [label.closest('*'), document];
                    for (const sc of scopes) {
                        const cand = sc && sc.querySelector
                          ? sc.querySelector('#authKey, input[name="authKey"], input, [data-clipboard-text], span, code, pre')
                          : null;
                        if (cand) {
                            if ('value' in cand && cand.value) return cand.value.trim();
                            const dc = cand.getAttribute && cand.getAttribute('data-clipboard-text');
                            if (dc) return dc.trim();
                            if (cand.textContent) return cand.textContent.trim();
                        }
                    }
                    return null;
                }""")

            # 4) кнопка Copy
            if not key_val:
                key_val = await page.evaluate("""() => {
                    const btn = Array.from(document.querySelectorAll('[data-clipboard-text], button, a'))
                        .find(n => /copy/i.test(n.textContent || '') || n.hasAttribute?.('data-clipboard-text'));
                    if (!btn) return null;
                    const dc = btn.getAttribute && btn.getAttribute('data-clipboard-text');
                    return (dc && dc.trim()) || null;
                }""")

            # Валидация формата (длинная base62-подобная)
            def looks_like_auth(s: Optional[str]) -> bool:
                if not s: return False
                v = s.strip()
                if len(v) < 40:  # обычно длинная строка
                    return False
                return re.fullmatch(r"[A-Za-z0-9]+", v) is not None

            result = key_val if looks_like_auth(key_val) else None

            if not result:
                # fallback: собрать кандидатов и выбрать самый длинный валидный
                candidates = await page.evaluate("""() => {
                    const vals = new Set();
                    const add = v => { if (v && typeof v === 'string') vals.add(v.trim()); };

                    const el1 = document.querySelector('#authKey');
                    if (el1) { add(el1.value); add(el1.getAttribute?.('data-clipboard-text')); add(el1.textContent); }

                    const el2 = document.querySelector('input[name="authKey"]');
                    if (el2) { add(el2.value); add(el2.getAttribute?.('data-clipboard-text')); add(el2.textContent); }

                    const near = Array.from(document.querySelectorAll('input, [data-clipboard-text], span, code, pre'));
                    for (const n of near) {
                        add(n.getAttribute?.('data-clipboard-text'));
                        if ('value' in n) add(n.value);
                        add(n.textContent);
                    }
                    return Array.from(vals);
                }""")
                good = [c for c in candidates if looks_like_auth(c)]
                result = max(good, key=len) if good else None

            if not result:
                try:
                    await page.screenshot(path="last_page.png", full_page=True)
                    html = await page.content()
                    with open("last_page.html", "w", encoding="utf-8") as f:
                        f.write(html)
                    logging.warning("AUTH KEY не найден. Сохранены last_page.png и last_page.html")
                except Exception as e:
                    logging.warning("Не удалось сохранить отладочные файлы: %s", e)

        finally:
            await context.close()
            await browser.close()

        return (result or "").strip() if result else None

# ---------- TELEGRAM: bot/dispatcher/router ----------
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ---------- АДМИН-ПАНЕЛЬ ----------
def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Список активных", callback_data="admin_list")],
        [InlineKeyboardButton(text="➕ Выдать подписку", callback_data="admin_addsub")],
        [InlineKeyboardButton(text="➖ Удалить подписку", callback_data="admin_delsub")],
        [InlineKeyboardButton(text="🔄 Отправить код всем", callback_data="admin_sendcode")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="admin_close")]
    ])

@router.message(Command("admin"))
async def admin_cmd(m: types.Message):
    if not m.from_user or m.from_user.id != ADMIN_ID:
        return await m.answer("⛔ Доступ запрещён.")
    await m.answer("Админ-панель:", reply_markup=admin_kb())

@router.callback_query(lambda c: c.from_user.id == ADMIN_ID and c.data == "admin_list")
async def admin_list_cb(c: types.CallbackQuery):
    ids = db_active_subscribers()
    if not ids:
        await c.message.edit_text("Активных подписчиков нет.", reply_markup=admin_kb())
    else:
        users_list = "\n".join(str(uid) for uid in ids)
        await c.message.edit_text(f"📋 Активные подписчики:\n{users_list}", reply_markup=admin_kb())
    await c.answer()

@router.callback_query(lambda c: c.from_user.id == ADMIN_ID and c.data == "admin_addsub")
async def admin_addsub_cb(c: types.CallbackQuery):
    await c.message.edit_text("Отправь команду:\n<code>/addsub chat_id|@username дни</code>", reply_markup=admin_kb())
    await c.answer()

@router.callback_query(lambda c: c.from_user.id == ADMIN_ID and c.data == "admin_delsub")
async def admin_delsub_cb(c: types.CallbackQuery):
    await c.message.edit_text("Чтобы снять дни, передай отрицательное число:\n<code>/addsub chat_id|@username -7</code>", reply_markup=admin_kb())
    await c.answer()

@router.callback_query(lambda c: c.from_user.id == ADMIN_ID and c.data == "admin_sendcode")
async def admin_sendcode_cb(c: types.CallbackQuery):
    # По запросу админа можно отправить всем вручную (оставляем)
    code = await get_auth_key_by_cookies()
    if not code:
        await c.message.edit_text("❌ Не удалось получить AUTH KEY (проверь cookies.json).", reply_markup=admin_kb())
    else:
        sent = 0
        for uid in db_active_subscribers():
            try:
                await bot.send_message(uid, f"🔑 <b>Новый AUTH KEY:</b>\n<code>{code}</code>")
                sent += 1
            except Exception:
                pass
        await c.message.edit_text(f"✅ AUTH KEY отправлен {sent} подписчикам.", reply_markup=admin_kb())
    await c.answer()

@router.callback_query(lambda c: c.from_user.id == ADMIN_ID and c.data == "admin_close")
async def admin_close_cb(c: types.CallbackQuery):
    await c.message.delete()
    await c.answer()

# ---------- КОМАНДЫ ПОЛЬЗОВАТЕЛЕЙ ----------
@router.message(Command("start"))
async def start_cmd(m: types.Message):
    db_add_or_update_user(m.chat.id, (m.from_user.username if m.from_user else None))
    await m.answer(
        "Привет! Нажми «Получить код» (кнопка) — бот пришлёт текущий AUTH KEY, если у тебя есть подписка.\n"
        "Посмотреть подписку: /mysub\n"
        "Админ-панель: /admin (для администратора)",
        reply_markup=MAIN_KB
    )

@router.message(Command("myid"))
async def myid_cmd(m: types.Message):
    await m.answer(f"Твой chat_id: <code>{m.chat.id}</code>")

@router.message(Command("mysub"))
async def mysub_cmd(m: types.Message):
    exp = db_get_subscription(m.chat.id)
    if exp > int(time.time()):
        await m.answer(f"Подписка активна до: <b>{fmt_ts(exp)}</b>")
    else:
        await m.answer("Подписки нет или она закончилась.")

@router.message(Command("addsub"))
async def addsub_cmd(m: types.Message):
    if not m.from_user or m.from_user.id != ADMIN_ID:
        return await m.answer("Ты не админ.")
    parts = m.text.strip().split()
    if len(parts) != 3:
        return await m.answer("Формат: /addsub chat_id|@username дни")
    target = parts[1]
    try:
        days = int(parts[2])
    except ValueError:
        return await m.answer("Дни должны быть числом (можно отрицательное, чтобы снять дни).")

    if target.isdigit():
        chat_id = int(target)
    else:
        chat_id = db_find_chat_id_by_username(target)
        if chat_id is None:
            return await m.answer("Пользователь не найден в базе. Он должен сначала написать боту (/start).")

    new_exp = db_set_subscription(chat_id, days)
    await m.answer(f"✅ Подписка пользователю {chat_id} до: <b>{fmt_ts(new_exp)}</b>")

@router.message(Command("getcode"))
async def getcode_cmd(m: types.Message):
    # КОД ТЕПЕРЬ ВЫДАЁТСЯ ТОЛЬКО ПО КНОПКЕ
    await m.answer("Теперь код выдаётся только по кнопке «Получить код». Нажми кнопку ниже.", reply_markup=MAIN_KB)

@router.message()
async def generic_text(m: types.Message):
    if (m.text or "").strip().lower() == "получить код":
        # выдаём код ТОЛЬКО при нажатии кнопки
        if db_get_subscription(m.chat.id) < int(time.time()):
            return await m.answer("У вас нет активной подписки.")
        code = await get_auth_key_by_cookies()
        if code:
            await m.answer(f"<b>Ваш AUTH KEY:</b>\n<code>{code}</code>")
            # не обновляем last_code_hash и не рассылаем — выдача только по кнопке
        else:
            await m.answer("Не удалось получить AUTH KEY. Проверьте cookies.json.")

# ---------- ФОНОВЫЙ МОНИТОРИНГ (ОТКЛЮЧЕНО) ----------
async def monitor_loop():
    # оставлено на будущее — сейчас не используется
    while True:
        await asyncio.sleep(CHECK_EVERY_SEC)

# ---------- ЗАПУСК ----------
async def main():
    db_init()
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    # НЕ запускаем авто-мониторинг, чтобы код приходил только по кнопке
    # asyncio.create_task(monitor_loop())

    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
