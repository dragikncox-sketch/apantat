
import re

import asyncio
import logging
import random
import sqlite3
import os
import time
from pathlib import Path
import requests
from html import escape
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CopyTextButton,
)

from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from config import BOT_TOKEN, ADMIN_USERNAME
from database import (
    init_sms_database,
    save_number_owner,
    get_number_owner,
    normalize_number,
    init_vvip_database,
    get_open_vvip_payment,
    create_vvip_payment,
    get_vvip_payment,
    attach_vvip_proof,
    get_vvip_expiry,
    review_vvip_payment,
)
from sms_forwarder import sms_poll_loop
from augestel import close_client, get_cached_total_messages, refresh_statistics_cache


def make_button(text, **kwargs):
    """Buat tombol dengan warna konsisten di seluruh menu bot.

    Telegram mendukung style primary (biru), success (hijau), dan danger (merah).
    Warna dipilih berdasarkan fungsi/label tombol, bukan satu warna untuk semuanya.
    """
    explicit_style = kwargs.pop("style", None)
    label = str(text).strip().upper()
    callback = str(kwargs.get("callback_data", "")).lower()

    if explicit_style:
        style = explicit_style
    elif any(word in label for word in ("❌", "BATAL", "TOLAK", "REJECT", "CANCEL", "HAPUS", "DELETE")) or any(word in callback for word in ("reject", "cancel", "delete")):
        style = "danger"
    elif any(word in label for word in ("✅", "APPROVE", "TRANSFER", "COPY", "BELI", "AKTIFKAN", "GET NUMBER", "CHANGE NUMBER")) or any(word in callback for word in ("approve", "paid", "buy", "next_numbers")):
        style = "success"
    else:
        style = "primary"

    return InlineKeyboardButton(text, style=style, **kwargs)

# =====================================
# SETTINGS
# =====================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

NUMBERS_PER_REQUEST = 3
NUMBERS_DIR = Path(__file__).resolve().parent / "numbers"
DATABASE = "users.db"
REFERRAL_BONUS = 100
MIN_WITHDRAWAL = 5000
ADMIN_ID = int(os.getenv("ADMIN_ID", "1932008453"))

REQUIRED_GROUP_ID = int(
    os.getenv("REQUIRED_GROUP_ID", "-1004387643125")
)

REQUIRED_GROUP_LINK = os.getenv(
    "REQUIRED_GROUP_LINK",
    "https://t.me/+2xpSHUjQfmM3ZWM1",
)

REQUIRED_CHANNEL_ID = int(
    os.getenv("REQUIRED_CHANNEL_ID", "-1004363973467")
)

REQUIRED_CHANNEL_LINK = os.getenv(
    "REQUIRED_CHANNEL_LINK",
    "https://t.me/+hBeB1LSE3W4zZGM1",
)


COUNTRIES = {
    "indonesia": "INDONESIA",
    "malaysia": "MALAYSIA",
    "hong_kong": "HONG KONG",
    "cameroon": "CAMEROON",
    "benin": "BENIN",
    "sri_lanka": "SRI LANKA",
}

# =====================================
# SAFE TELEGRAM MESSAGE EDIT
# =====================================

async def safe_edit_message(query, text, **kwargs):
    try:
        return await query.edit_message_text(
            text=text,
            **kwargs
        )

    except BadRequest as error:
        if "Message is not modified" in str(error):
            logger.info("Pesan tidak mengalami perubahan.")
            return None

        raise


# =====================================
# GROUP + CHANNEL MEMBERSHIP
# =====================================

def membership_keyboard():
    return InlineKeyboardMarkup([
        [make_button("👥 JOIN GROUP", url=REQUIRED_GROUP_LINK)],
        [make_button("📢 JOIN CHANNEL", url=REQUIRED_CHANNEL_LINK)],
        [make_button("🔄 CEK KEANGGOTAAN", callback_data="check_membership")],
    ])


async def check_chat_membership(bot, chat_id, user_id, chat_label):
    try:
        member = await bot.get_chat_member(
            chat_id=chat_id,
            user_id=user_id,
        )

        if member.status in ("creator", "administrator", "member"):
            return True

        if member.status == "restricted":
            return bool(member.is_member)

        return False

    except TelegramError:
        logger.exception(
            "Gagal memeriksa keanggotaan %s (chat_id=%s).",
            chat_label,
            chat_id,
        )
        return None


async def check_required_memberships(bot, user_id):
    """True jika user join grup DAN channel; None jika pemeriksaan gagal."""
    group_member = await check_chat_membership(
        bot, REQUIRED_GROUP_ID, user_id, "grup"
    )
    channel_member = await check_chat_membership(
        bot, REQUIRED_CHANNEL_ID, user_id, "channel"
    )

    if group_member is False or channel_member is False:
        return False
    if group_member is None or channel_member is None:
        return None
    return True


async def send_membership_message(message):
    await message.reply_text(
        "🔒 <b>AKSES BOT TERKUNCI</b>\n\n"
        "Untuk menggunakan JING OTP BOT, kamu wajib bergabung "
        "ke grup DAN channel Telegram kami.\n\n"
        "1. Tekan JOIN GROUP.\n"
        "2. Tekan JOIN CHANNEL.\n"
        "3. Setelah bergabung ke keduanya, tekan CEK KEANGGOTAAN.",
        parse_mode="HTML",
        reply_markup=membership_keyboard(),
    )


async def verify_user_access(update, context):
    user = update.effective_user
    if not user:
        return False

    is_member = await check_required_memberships(context.bot, user.id)
    if is_member is True:
        return True

    if update.callback_query:
        text = (
            "⚠️ Pemeriksaan keanggotaan gagal. Pastikan bot menjadi admin "
            "channel dan anggota grup, lalu coba lagi."
            if is_member is None else
            "🔒 <b>AKSES BOT TERKUNCI</b>\n\n"
            "Kamu wajib bergabung ke grup DAN channel terlebih dahulu."
        )
        await safe_edit_message(
            update.callback_query,
            text,
            parse_mode="HTML",
            reply_markup=membership_keyboard(),
        )
    elif update.effective_message:
        if is_member is None:
            await update.effective_message.reply_text(
                "⚠️ Tidak dapat memeriksa keanggotaan grup/channel. "
                "Pastikan bot adalah admin channel dan anggota grup, lalu coba lagi.",
                reply_markup=membership_keyboard(),
            )
        else:
            await send_membership_message(update.effective_message)
    return False


# =====================================
# DATABASE
# =====================================

def init_database():
    with sqlite3.connect(DATABASE) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                joined_at TEXT
            )
        """)

        columns = [
            row[1]
            for row in db.execute(
                "PRAGMA table_info(users)"
            ).fetchall()
        ]

        if "username" not in columns:
            db.execute(
                "ALTER TABLE users ADD COLUMN username TEXT"
            )

        if "first_name" not in columns:
            db.execute(
                "ALTER TABLE users ADD COLUMN first_name TEXT"
            )

        if "joined_at" not in columns:
            db.execute(
                "ALTER TABLE users ADD COLUMN joined_at TEXT"
            )

        if "balance" not in columns:
            db.execute(
                "ALTER TABLE users ADD COLUMN balance INTEGER NOT NULL DEFAULT 0"
            )

        db.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                referred_user_id INTEGER PRIMARY KEY,
                referrer_id INTEGER NOT NULL,
                created_at TEXT
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS wallet_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                transaction_type TEXT NOT NULL,
                description TEXT,
                created_at TEXT
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS withdrawal_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                payout_details TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT,
                processed_at TEXT
            )
        """)

        db.commit()


def register_user(user, referrer_id=None):
    if not user:
        return

    with sqlite3.connect(DATABASE) as db:
        cursor = db.execute("""
            INSERT OR IGNORE INTO users
            (user_id, username, first_name, joined_at)
            VALUES (?, ?, ?, ?)
        """, (
            user.id,
            user.username or "",
            user.first_name or "",
            datetime.now().isoformat(),
        ))
        is_new_user = cursor.rowcount == 1

        db.execute("""
            UPDATE users
            SET username = ?, first_name = ?
            WHERE user_id = ?
        """, (
            user.username or "",
            user.first_name or "",
            user.id,
        ))

        # Attribute an invite only once, when the account first registers.
        if is_new_user and referrer_id:
            try:
                referrer_id = int(referrer_id)
            except (TypeError, ValueError):
                referrer_id = None

            if referrer_id and referrer_id != user.id:
                referrer_exists = db.execute(
                    "SELECT 1 FROM users WHERE user_id = ?",
                    (referrer_id,),
                ).fetchone()
                if referrer_exists:
                    referral_cursor = db.execute("""
                        INSERT OR IGNORE INTO referrals
                        (referred_user_id, referrer_id, created_at)
                        VALUES (?, ?, ?)
                    """, (
                        user.id,
                        referrer_id,
                        datetime.now().isoformat(),
                    ))

                    if referral_cursor.rowcount == 1:
                        db.execute(
                            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                            (REFERRAL_BONUS, referrer_id),
                        )
                        db.execute("""
                            INSERT INTO wallet_transactions
                            (user_id, amount, transaction_type, description, created_at)
                            VALUES (?, ?, 'referral_bonus', ?, ?)
                        """, (
                            referrer_id,
                            REFERRAL_BONUS,
                            f"Bonus undangan user {user.id}",
                            datetime.now().isoformat(),
                        ))

        db.commit()


def get_referral_count(user_id):
    with sqlite3.connect(DATABASE) as db:
        return db.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?",
            (user_id,),
        ).fetchone()[0]


def get_wallet_balance(user_id):
    with sqlite3.connect(DATABASE) as db:
        row = db.execute(
            "SELECT balance FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return int(row[0]) if row else 0


def create_withdrawal(user_id, amount, payout_details):
    """Reserve balance and create a pending withdrawal atomically."""
    with sqlite3.connect(DATABASE) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT balance FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        balance = int(row[0]) if row else 0

        if amount < MIN_WITHDRAWAL:
            return None, "minimum"
        if amount > balance:
            return None, "insufficient"

        db.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id = ?",
            (amount, user_id),
        )
        cursor = db.execute("""
            INSERT INTO withdrawal_requests
            (user_id, amount, payout_details, status, created_at)
            VALUES (?, ?, ?, 'pending', ?)
        """, (user_id, amount, payout_details, datetime.now().isoformat()))
        request_id = cursor.lastrowid

        db.execute("""
            INSERT INTO wallet_transactions
            (user_id, amount, transaction_type, description, created_at)
            VALUES (?, ?, 'withdraw_hold', ?, ?)
        """, (
            user_id, -amount, f"Penahanan saldo untuk penarikan #{request_id}",
            datetime.now().isoformat()
        ))
        db.commit()
    return request_id, None


def process_withdrawal(request_id, approve):
    """Approve as paid, or reject and refund the reserved balance."""
    with sqlite3.connect(DATABASE) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""
            SELECT user_id, amount, status
            FROM withdrawal_requests WHERE id = ?
        """, (request_id,)).fetchone()

        if not row or row[2] != "pending":
            return None

        user_id, amount, _ = row
        new_status = "paid" if approve else "rejected"
        db.execute("""
            UPDATE withdrawal_requests
            SET status = ?, processed_at = ?
            WHERE id = ? AND status = 'pending'
        """, (new_status, datetime.now().isoformat(), request_id))

        if not approve:
            db.execute(
                "UPDATE users SET balance = balance + ? WHERE user_id = ?",
                (amount, user_id),
            )
            db.execute("""
                INSERT INTO wallet_transactions
                (user_id, amount, transaction_type, description, created_at)
                VALUES (?, ?, 'withdraw_refund', ?, ?)
            """, (
                user_id, amount,
                f"Pengembalian penarikan ditolak #{request_id}",
                datetime.now().isoformat()
            ))
        db.commit()
    return (user_id, amount, new_status)


def get_total_users():
    with sqlite3.connect(DATABASE) as db:
        return db.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]


# =====================================
# STATISTICS
# =====================================

def get_total_sms():
    """Ambil statistik dari cache; tidak menunggu request API."""
    return get_cached_total_messages()


async def statistics_refresh_loop():
    """Perbarui statistik di background setiap 5 menit."""
    while True:
        try:
            await refresh_statistics_cache()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Gagal memperbarui cache statistik Augestel")
        await asyncio.sleep(300)


def clear_user_flow(context):
    """Reset proses sementara tanpa menghapus nomor terakhir dari GET NUMBER."""
    current_numbers = context.user_data.get("current_numbers")
    context.user_data.clear()
    if current_numbers:
        context.user_data["current_numbers"] = current_numbers


# =====================================
# MAIN MENU
# =====================================

def main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⭐ VVIP MEMBERSHIP",
                callback_data="vvip_menu",
                style="primary",
            )
        ],
        [
            InlineKeyboardButton(
                "📱 GET NUMBER",
                callback_data="get_number",
                style="success",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ CANCEL",
                callback_data="cancel",
                style="danger",
            )
        ],
        [
            InlineKeyboardButton(
                "🎁 UNDANGAN / REFERRAL",
                callback_data="referral",
                style="primary",
            )
        ],
        [
            InlineKeyboardButton(
                "🔎 CEK BIO (VVIP)",
                callback_data="whatsinfo",
                style="primary",
            )
        ],
        [
            InlineKeyboardButton(
                "💬 SUPPORT",
                url=f"https://t.me/{ADMIN_USERNAME.lstrip('@')}",
                style="primary",
            )
        ],
    ])


def service_menu():
    return InlineKeyboardMarkup([
        [
            make_button(
                "WhatsApp",
                callback_data="service_whatsapp",
            )
        ],
        [
            make_button(
                "⬅️ Kembali",
                callback_data="home",
            )
        ],
    ])


def country_menu():
    return InlineKeyboardMarkup([
        [make_button("🇮🇩 INDONESIA", callback_data="country:indonesia")],
        [make_button("🇲🇾 MALAYSIA", callback_data="country:malaysia")],
        [make_button("🇭🇰 HONG KONG", callback_data="country:hong_kong")],
        [make_button("🇨🇲 CAMEROON", callback_data="country:cameroon")],
        [make_button("🇧🇯 BENIN", callback_data="country:benin")],
        [make_button("🇱🇰 SRI LANKA", callback_data="country:sri_lanka")],
        [make_button("⬅️ KEMBALI", callback_data="get_number")],
    ])


# =====================================
# NUMBER KEYBOARD
# =====================================

def build_number_keyboard(numbers):
    keyboard = []

    for item in numbers:
        number = normalize_number(item.get("number", ""))
        if not number:
            continue

        formatted = f"+{number}"
        # Setiap nomor menjadi tombol copy satu per satu.
        keyboard.append([
            make_button(
                formatted,
                copy_text=CopyTextButton(text=formatted),
                style="primary",
            )
        ])

    # Tombol copy semua nomor dalam satu kali tekan.
    all_numbers_text = "\n".join(
        f"+{normalize_number(item.get('number', ''))}"
        for item in numbers
        if normalize_number(item.get('number', ''))
    )
    if all_numbers_text:
        keyboard.append([
            make_button(
                "📋 COPY ALL NUMBER",
                copy_text=CopyTextButton(text=all_numbers_text),
                style="success",
            )
        ])

    keyboard.append([
        make_button(
            "🔄 CHANGE NUMBER",
            callback_data="next_numbers",
            style="success",
        )
    ])
    keyboard.append([
        make_button(
            "🔎 CEK BIO (VVIP)",
            callback_data="whatsinfo",
            style="primary",
        )
    ])
    keyboard.append([
        make_button(
            "📢 OTP CHANNEL",
            url="https://t.me/+hBeB1LSE3W4zZGM1",
            style="primary",
        )
    ])
    keyboard.append([
        make_button(
            "🏠 MENU UTAMA",
            callback_data="home",
            style="primary",
        )
    ])

    return InlineKeyboardMarkup(keyboard)


# =====================================
# FILTER COUNTRY
# =====================================

def filter_numbers(numbers, country):
    keyword = COUNTRIES.get(country, "").upper()

    aliases = {
        "INDONESIA": ["INDONESIA"],
        "MALAYSIA": ["MALAYSIA"],
        "PHILIPPINES": [
            "PHILIPPINES",
            "PHILIPPINE",
        ],
        "MYANMAR": [
            "MYANMAR",
            "BURMA",
        ],
        "PAKISTAN": [
            "PAKISTAN",
        ],
    }

    keywords = aliases.get(keyword, [keyword])
    filtered = []

    for item in numbers:
        if not isinstance(item, dict):
            continue

        range_name = str(
            item.get("range_name", "")
        ).upper()

        if any(k in range_name for k in keywords):
            filtered.append(item)

    return filtered


# =====================================
# START
# =====================================

async def start(update, context):
    referrer_id = None
    if context.args:
        payload = context.args[0]
        if payload.startswith("ref_"):
            referrer_id = payload[4:]

    if not await verify_user_access(update, context):
        # Keep the inviter until the user passes the required membership check.
        if referrer_id:
            context.user_data["pending_referrer_id"] = referrer_id
        return

    user = update.effective_user
    register_user(user, referrer_id=referrer_id)

    total_users = get_total_users()
    total_sms = get_total_sms()

    text = (
        "👋 <b>SELAMAT DATANG DI JING OTP BOT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📊 <b>STATISTIK BOT</b>\n"
        f"👥 Total pengguna: <b>{total_users:,}</b>\n"
        f"💬 Total SMS: <b>{total_sms}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Silakan pilih menu di bawah."
    )

    await update.effective_message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


# =====================================
# CANCEL
# =====================================

async def cancel_command(update, context):
    if not await verify_user_access(update, context):
        return

    clear_user_flow(context)

    await update.effective_message.reply_text(
        "✅ Proses dibatalkan.",
        reply_markup=main_menu(),
    )


# =====================================
# SEND NUMBER LIST
# =====================================

def read_numbers_from_txt(country):
    """
    Baca ulang file TXT setiap kali pengguna meminta nomor.
    Format: satu nomor per baris. Baris kosong dan komentar # diabaikan.
    """
    filename = f"{COUNTRIES[country]}.txt"
    file_path = NUMBERS_DIR / filename

    if not file_path.is_file():
        raise FileNotFoundError(f"File stok tidak ditemukan: {file_path}")

    numbers = []
    seen = set()

    with file_path.open("r", encoding="utf-8-sig") as file:
        for line in file:
            value = line.strip()

            if not value or value.startswith("#"):
                continue

            number = normalize_number(value)
            if number and number not in seen:
                seen.add(number)
                numbers.append({"number": number})

    return numbers


def reserve_random_numbers(numbers, user_id, previous_numbers=None):
    """
    Mengunci nomor pada saat daftar dikirim.
    INSERT OR IGNORE di database menjadi pengaman ketika dua pengguna
    meminta nomor yang sama secara bersamaan.
    """
    previous_numbers = set(previous_numbers or [])
    candidates = list(numbers)
    random.shuffle(candidates)

    reserved = []

    for item in candidates:
        number = normalize_number(item.get("number", ""))

        if not number or number in previous_numbers:
            continue

        # Nomor yang sudah dimiliki siapa pun tidak ditawarkan kembali,
        # termasuk nomor yang sebelumnya pernah diberikan kepada user ini.
        if get_number_owner(number) is not None:
            continue

        owner_id = save_number_owner(user_id, number)

        # Jika pengguna lain lebih dulu mengunci nomor ini, lewati.
        if owner_id == user_id:
            reserved.append({"number": number})

        if len(reserved) >= NUMBERS_PER_REQUEST:
            break

    return reserved


async def send_number_list(
    update,
    context,
    country,
):
    query = update.callback_query

    try:
        await safe_edit_message(query, "⏳ Membaca stok nomor...")

        # Simpan negara saja. File TXT dibaca ulang pada setiap permintaan,
        # termasuk ketika pengguna menekan tombol GANTI NOMOR.
        context.user_data["selected_country"] = country
        context.user_data["current_numbers"] = []

        await send_next_three(query, context, edit_message=True)

    except FileNotFoundError as error:
        logger.error("%s", error)
        await safe_edit_message(
            query,
            f"⚠️ <b>FILE STOK TIDAK DITEMUKAN</b>\n\n"
            f"Negara: <b>{COUNTRIES[country]}</b>\n"
            f"File: <code>numbers/{COUNTRIES[country]}.txt</code>",
            parse_mode="HTML",
            reply_markup=country_menu(),
        )

    except Exception:
        logger.exception("Gagal membaca file nomor")
        try:
            await safe_edit_message(
                query,
                "❌ Gagal membaca stok nomor. Silakan coba kembali.",
                reply_markup=country_menu(),
            )
        except TelegramError:
            pass


async def send_next_three(
    query,
    context,
    edit_message=True,
):
    country = context.user_data.get("selected_country")

    if country not in COUNTRIES:
        await safe_edit_message(
            query,
            "Silakan pilih negara kembali.",
            reply_markup=country_menu(),
        )
        return

    try:
        # Baca file langsung setiap kali. Mengosongkan TXT berarti stok 0.
        all_numbers = read_numbers_from_txt(country)
    except FileNotFoundError:
        await safe_edit_message(
            query,
            f"⚠️ File <code>numbers/{COUNTRIES[country]}.txt</code> tidak ditemukan.",
            parse_mode="HTML",
            reply_markup=country_menu(),
        )
        return
    except OSError:
        logger.exception("Tidak bisa membaca file stok negara %s", country)
        await safe_edit_message(
            query,
            "❌ File stok tidak bisa dibaca. Periksa izin file TXT.",
            reply_markup=country_menu(),
        )
        return

    user_id = query.from_user.id
    previous = {
        normalize_number(item.get("number", ""))
        for item in context.user_data.get("current_numbers", [])
    }

    selected = reserve_random_numbers(
        all_numbers,
        user_id,
        previous_numbers=previous,
    )

    if not selected:
        await safe_edit_message(
            query,
            "🔴 <b>STOK NOMOR HABIS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🌍 Negara: <b>{COUNTRIES[country]}</b>\n"
            "📦 Nomor tersedia: <b>0</b>\n\n"
            "Silakan coba kembali nanti.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [make_button("🌍 PILIH NEGARA", callback_data="service_whatsapp")],
                [make_button("🔵 MENU UTAMA", callback_data="home")],
            ]),
        )
        return

    context.user_data["current_numbers"] = selected

    country_name = COUNTRIES[country]
    count = len(selected)

    message = (
        f"🌍 <b>STOK NOMOR {country_name}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📱 Nomor ditampilkan: <b>{count}</b>\n"
        "🔒 Status: <b>OTOMATIS TERKUNCI</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🟢 Nomor langsung ditautkan ke akun kamu saat ditampilkan.\n"
        "📋 Tekan tombol nomor untuk menyalinnya.\n"
        "📩 SMS yang masuk akan diteruskan ke akun Telegram pemilik."
    )

    await safe_edit_message(
        query,
        message,
        parse_mode="HTML",
        reply_markup=build_number_keyboard(selected),
    )


# =====================================
# MEMBERSHIP CHECK CALLBACK
# =====================================

async def handle_membership_check(update, context):
    query = update.callback_query
    user = update.effective_user

    is_member = await check_required_memberships(context.bot, user.id)

    if is_member is True:
        pending_referrer = context.user_data.pop("pending_referrer_id", None)
        register_user(user, referrer_id=pending_referrer)

        total_users = get_total_users()
        total_sms = get_total_sms()

        welcome_text = (
            "👋 <b>SELAMAT DATANG DI JING OTP BOT</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "📊 <b>STATISTIK BOT</b>\n"
            f"👥 Total pengguna: <b>{total_users:,}</b>\n"
            f"💬 Total SMS: <b>{total_sms}</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Silakan pilih menu di bawah."
        )

        await safe_edit_message(
            query,
            welcome_text,
            parse_mode="HTML",
            reply_markup=main_menu(),
        )

    elif is_member is False:
        await safe_edit_message(
            query,
            "🔒 <b>AKSES BOT TERKUNCI</b>\n\n"
            "Kamu belum bergabung ke grup dan/atau channel kami.\n"
            "Silakan bergabung ke keduanya, lalu cek kembali.",
            parse_mode="HTML",
            reply_markup=membership_keyboard(),
        )

    else:
        await safe_edit_message(
            query,
            "⚠️ Tidak dapat memeriksa keanggotaan.\n"
            "Pastikan bot menjadi admin channel dan anggota grup, lalu coba lagi.",
            reply_markup=membership_keyboard(),
        )


# =====================================
# REFERRAL / INVITATION
# =====================================

async def show_referral_page(query, context):
    user = query.from_user
    me = await context.bot.get_me()
    referral_link = f"https://t.me/{me.username}?start=ref_{user.id}"
    invited = get_referral_count(user.id)
    balance = get_wallet_balance(user.id)

    text = (
        "🎁 <b>PROGRAM UNDANGAN JING OTP</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 Total berhasil diundang: <b>{invited}</b> teman\n"
        f"💰 Saldo bonus: <b>Rp{balance:,}</b>\n"
        f"🎁 Bonus: <b>Rp{REFERRAL_BONUS:,}</b> per teman\n\n"
        "Bagikan link pribadi kamu. Bonus diberikan satu kali "
        "ketika pengguna baru berhasil terdaftar melalui link kamu.\n\n"
        "🔗 <b>LINK UNDANGAN KAMU:</b>\n"
        f"<code>{referral_link}</code>\n"
    )

    await safe_edit_message(
        query,
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [make_button("💸 WITHDRAW SALDO", callback_data="withdraw")],
            [make_button("⬅️ MENU UTAMA", callback_data="home")]
        ]),
    )


# =====================================
# WITHDRAWAL FLOW
# =====================================

async def begin_withdrawal(query, context):
    user_id = query.from_user.id
    balance = get_wallet_balance(user_id)
    if balance < MIN_WITHDRAWAL:
        await safe_edit_message(
            query,
            f"💰 Saldo kamu: <b>Rp{balance:,}</b>\n"
            f"Minimal penarikan: <b>Rp{MIN_WITHDRAWAL:,}</b>.\n"
            "Undang teman untuk mengumpulkan bonus.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [make_button("⬅️ KEMBALI", callback_data="referral")]
            ]),
        )
        return

    context.user_data["withdraw_state"] = "amount"
    await safe_edit_message(
        query,
        f"💸 <b>WITHDRAW SALDO</b>\n\n"
        f"Saldo tersedia: <b>Rp{balance:,}</b>\n"
        f"Minimal penarikan: <b>Rp{MIN_WITHDRAWAL:,}</b>\n\n"
        "Kirim jumlah penarikan dalam angka saja, contoh: <code>500</code>.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [make_button("❌ BATAL", callback_data="referral")]
        ]),
    )


async def handle_withdraw_text(update, context):
    if context.user_data is None:
        return

    if update.effective_user is None or update.effective_message is None:
        return

    state = context.user_data.get("withdraw_state")
    if not state:
        return

    user = update.effective_user
    message = update.effective_message
    raw = (message.text or "").strip()

    if state == "amount":
        try:
            amount = int(raw.replace(".", "").replace(",", "").replace("Rp", "").replace("rp", "").strip())
        except ValueError:
            await message.reply_text("❌ Masukkan nominal angka yang valid, contoh: 1000.")
            return

        if amount < MIN_WITHDRAWAL:
            await message.reply_text(f"❌ Minimal withdraw Rp{MIN_WITHDRAWAL:,}. Masukkan nominal lain.")
            return
        balance = get_wallet_balance(user.id)
        if amount > balance:
            await message.reply_text(f"❌ Saldo tidak cukup. Saldo kamu Rp{balance:,}. Masukkan nominal lain.")
            return

        context.user_data["withdraw_amount"] = amount
        context.user_data["withdraw_state"] = "details"
        await message.reply_text(
            "🏦 Kirim tujuan pencairan dengan format, misalnya:\n"
            "<code>DANA 081234567890 a.n. Nama</code>\n"
            "atau <code>BCA 1234567890 a.n. Nama</code>.\n\n"
            "Pastikan data benar. Admin akan memproses secara manual.",
            parse_mode="HTML",
        )
        return

    if state == "details":
        amount = int(context.user_data.get("withdraw_amount", 0))
        details = raw[:500]
        if len(details) < 6:
            await message.reply_text("❌ Detail rekening/e-wallet terlalu singkat. Kirim ulang dengan lengkap.")
            return

        request_id, error = create_withdrawal(user.id, amount, details)
        if error == "insufficient":
            context.user_data.pop("withdraw_state", None)
            context.user_data.pop("withdraw_amount", None)
            await message.reply_text("❌ Saldo berubah atau tidak mencukupi. Silakan mulai withdraw kembali.")
            return
        if error:
            await message.reply_text("❌ Nominal di bawah batas minimum.")
            return

        context.user_data.pop("withdraw_state", None)
        context.user_data.pop("withdraw_amount", None)
        await message.reply_text(
            f"✅ Permintaan withdraw <b>#{request_id}</b> berhasil dikirim.\n"
            f"Nominal: <b>Rp{amount:,}</b>\n"
            "Status: <b>MENUNGGU ADMIN</b>. Saldo sudah ditahan sementara "
            "dan akan dikembalikan jika permintaan ditolak.",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )

        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"💸 <b>PERMINTAAN WITHDRAW #{request_id}</b>\n"
                    f"User: <code>{user.id}</code> (@{user.username or '-'})\n"
                    f"Nama: {user.full_name}\n"
                    f"Nominal: <b>Rp{amount:,}</b>\n"
                    f"Tujuan: <code>{details}</code>\n"
                    "Silakan transfer secara manual sebelum menekan LUNAS."
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [
                        make_button("✅ SUDAH DITRANSFER", callback_data=f"withdraw_paid:{request_id}"),
                        make_button("❌ TOLAK", callback_data=f"withdraw_reject:{request_id}"),
                    ]
                ]),
            )
        except TelegramError:
            logger.exception("Gagal mengirim permintaan withdraw ke admin")


async def handle_withdraw_admin_callback(query, context, data):
    if query.from_user.id != ADMIN_ID:
        await query.answer("Khusus admin.", show_alert=True)
        return

    action, request_id_text = data.split(":", 1)
    try:
        request_id = int(request_id_text)
    except ValueError:
        await query.answer("ID permintaan tidak valid.", show_alert=True)
        return

    result = process_withdrawal(request_id, approve=(action == "withdraw_paid"))
    if not result:
        await query.answer("Permintaan sudah diproses atau tidak ditemukan.", show_alert=True)
        return

    user_id, amount, status = result
    if status == "paid":
        status_text = "LUNAS"
        user_text = f"✅ Withdraw #{request_id} sebesar Rp{amount:,} telah ditransfer admin."
    else:
        status_text = "DITOLAK"
        user_text = f"❌ Withdraw #{request_id} ditolak. Saldo Rp{amount:,} sudah dikembalikan."

    await query.edit_message_text(
        query.message.text + f"\n\nStatus: <b>{status_text}</b>",
        parse_mode="HTML",
    )
    try:
        await context.bot.send_message(chat_id=user_id, text=user_text)
    except TelegramError:
        logger.exception("Gagal memberi notifikasi status withdraw ke user")


# =====================================
# VVIP MANUAL PAYMENT
# =====================================

VVIP_PACKAGES = {
    1: 2000,
    3: 5000,
    7: 10000,
}
DANA_NUMBER = "082285086012"
DANA_ACCOUNT_NAME = "EKA PRXXXXXX"


def vvip_package_keyboard():
    return InlineKeyboardMarkup([
        [make_button("⭐ 1 HARI — Rp2.000", callback_data="vvip_buy:1")],
        [make_button("⭐ 3 HARI — Rp5.000", callback_data="vvip_buy:3")],
        [make_button("⭐ 7 HARI — Rp10.000", callback_data="vvip_buy:7")],
        [make_button("📋 STATUS VVIP", callback_data="vvip_status")],
        [make_button("⬅️ MENU UTAMA", callback_data="home")],
    ])


def format_vvip_expiry(expiry):
    if not expiry:
        return "Tidak aktif"
    return expiry.astimezone().strftime("%d-%m-%Y %H:%M:%S %Z")


async def show_vvip_menu(query):
    expiry = get_vvip_expiry(query.from_user.id)
    active = bool(expiry and expiry.timestamp() > datetime.now().astimezone().timestamp())
    status = (
        f"🟢 Aktif sampai: <b>{escape(format_vvip_expiry(expiry))}</b>"
        if active else "⚪ Belum aktif"
    )
    await safe_edit_message(
        query,
        "⭐ <b>JING OTP VVIP</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Status akun: {status}\n\n"
        "Pilih paket VVIP:\n"
        "• 1 hari: Rp2.000\n"
        "• 3 hari: Rp5.000\n"
        "• 7 hari: Rp10.000\n\n"
        "Pembayaran manual melalui DANA. VVIP aktif setelah admin menyetujui bukti transfer.",
        parse_mode="HTML",
        reply_markup=vvip_package_keyboard(),
    )


async def begin_vvip_payment(query, context, package_days):
    amount = VVIP_PACKAGES.get(package_days)
    if not amount:
        await query.answer("Paket tidak valid.", show_alert=True)
        return

    payment_id, created = create_vvip_payment(
        query.from_user.id, package_days, amount
    )
    payment = get_vvip_payment(payment_id)
    context.user_data["vvip_payment_id"] = payment_id

    if payment and payment["status"] == "pending":
        context.user_data.pop("vvip_payment_id", None)
        await query.message.reply_text(
            f"Pembayaran VVIP #{payment_id} sudah menunggu pemeriksaan admin. "
            "Kamu tidak perlu mengirim bukti lagi."
        )
        return

    if payment:
        # If an unfinished transaction already exists, always show its saved
        # package and amount instead of the newly clicked package.
        package_days = payment["package_days"]
        amount = payment["amount"]

    if not created:
        await query.message.reply_text(
            f"Kamu sudah memiliki transaksi VVIP #{payment_id} yang belum selesai. "
            "Gunakan detail pembayaran transaksi tersebut."
        )

    await query.message.reply_text(
        "💳 <b>PEMBAYARAN VVIP JING OTP</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📦 Paket: <b>{package_days} hari</b>\n"
        f"💰 Total: <b>Rp{amount:,}</b>\n\n"
        "Silakan transfer tepat sesuai nominal ke:\n"
        f"🏦 DANA: <code>{DANA_NUMBER}</code>\n"
        f"👤 A/N: <b>{escape(DANA_ACCOUNT_NAME)}</b>\n\n"
        f"🧾 ID transaksi: <code>#{payment_id}</code>\n\n"
        "Setelah transfer, kirim <b>foto bukti pembayaran</b> ke chat ini. "
        "VVIP baru aktif setelah admin memeriksa dan menyetujui bukti.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [make_button("⬅️ KEMBALI KE VVIP", callback_data="vvip_menu")]
        ]),
    )


async def handle_vvip_proof_photo(update, context):
    payment_id = context.user_data.get("vvip_payment_id")
    if not payment_id:
        return

    user = update.effective_user
    message = update.effective_message
    payment = get_vvip_payment(payment_id)

    if not payment or payment["user_id"] != user.id:
        context.user_data.pop("vvip_payment_id", None)
        await message.reply_text("Transaksi tidak ditemukan. Silakan mulai pembelian VVIP lagi.")
        return

    if payment["status"] != "awaiting_proof":
        context.user_data.pop("vvip_payment_id", None)
        await message.reply_text("Transaksi ini sudah dikirim atau sudah diproses.")
        return

    if not message.photo:
        return

    proof_file_id = message.photo[-1].file_id
    saved = attach_vvip_proof(payment_id, user.id, proof_file_id)
    if not saved:
        await message.reply_text("Bukti tidak dapat disimpan. Silakan coba lagi atau hubungi admin.")
        return

    context.user_data.pop("vvip_payment_id", None)
    await message.reply_text(
        f"✅ Bukti pembayaran untuk transaksi <b>#{payment_id}</b> sudah diterima.\n"
        "Status: <b>MENUNGGU KONFIRMASI ADMIN</b>.",
        parse_mode="HTML",
    )

    username = f"@{escape(user.username)}" if user.username else "-"
    caption = (
        f"🧾 <b>BUKTI PEMBAYARAN VVIP #{payment_id}</b>\n"
        f"User ID: <code>{user.id}</code>\n"
        f"Username: {username}\n"
        f"Nama: {escape(user.full_name)}\n"
        f"Paket: <b>{payment['package_days']} hari</b>\n"
        f"Nominal: <b>Rp{payment['amount']:,}</b>\n"
        "Status: <b>MENUNGGU ADMIN</b>"
    )
    try:
        await context.bot.send_photo(
            chat_id=ADMIN_ID,
            photo=proof_file_id,
            caption=caption,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    make_button("✅ APPROVE", callback_data=f"vvip_approve:{payment_id}"),
                    make_button("❌ REJECT", callback_data=f"vvip_reject:{payment_id}"),
                ]
            ]),
        )
    except TelegramError:
        logger.exception("Bukti VVIP tersimpan tetapi gagal dikirim ke admin")


async def handle_vvip_admin_callback(query, context, data):
    if query.from_user.id != ADMIN_ID:
        await query.answer("Khusus admin.", show_alert=True)
        return

    action, payment_id_text = data.split(":", 1)
    try:
        payment_id = int(payment_id_text)
    except ValueError:
        await query.answer("ID pembayaran tidak valid.", show_alert=True)
        return

    result = review_vvip_payment(
        payment_id,
        query.from_user.id,
        approve=(action == "vvip_approve"),
    )
    if not result:
        await query.answer("Transaksi tidak ditemukan atau sudah diproses.", show_alert=True)
        return

    if result["status"] == "approved":
        expiry = datetime.fromisoformat(result["expires_at"])
        user_text = (
            "🎉 <b>VVIP JING OTP AKTIF!</b>\n\n"
            f"Paket: <b>{result['package_days']} hari</b>\n"
            f"Masa aktif sampai: <b>{escape(format_vvip_expiry(expiry))}</b>\n\n"
            "Terima kasih atas pembayaran kamu."
        )
        admin_status = "✅ DISETUJUI"
    else:
        user_text = (
            f"❌ Pembayaran VVIP #{payment_id} ditolak oleh admin.\n"
            "Silakan hubungi SUPPORT jika kamu merasa ada kesalahan."
        )
        admin_status = "❌ DITOLAK"

    try:
        await context.bot.send_message(
            chat_id=result["user_id"],
            text=user_text,
            parse_mode="HTML",
        )
    except TelegramError:
        logger.exception("Gagal mengirim status pembayaran VVIP ke user %s", result["user_id"])

    old_caption = query.message.caption or ""
    try:
        await query.edit_message_caption(
            caption=old_caption + f"\n\n<b>Status: {admin_status}</b>",
            parse_mode="HTML",
            reply_markup=None,
        )
    except TelegramError:
        logger.exception("Gagal memperbarui caption bukti VVIP")


# =====================================
# WHATSINFO - RAPIDAPI (KHUSUS VVIP)
# =====================================

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "").strip()
RAPIDAPI_HOST = "whatsapp-number-validator3.p.rapidapi.com"
RAPIDAPI_URL = (
    "https://whatsapp-number-validator3.p.rapidapi.com/"
    "WhatsappNumberHasItWithToken"
)

# Anti-rate-limit WhatsInfo:
# - satu request global pada satu waktu
# - jeda antar nomor
# - retry terbatas jika HTTP 429
# - cache hasil sukses agar nomor yang sama tidak terus meminta API
WHATSINFO_REQUEST_LOCK = asyncio.Lock()
WHATSINFO_CACHE = {}
WHATSINFO_CACHE_TTL = 600
WHATSINFO_MIN_REQUEST_GAP = 4
WHATSINFO_USER_COOLDOWN = 15
WHATSINFO_LAST_REQUEST_AT = 0.0
WHATSINFO_LAST_RUN_BY_USER = {}


def vvip_is_active(user_id):
    expiry = get_vvip_expiry(user_id)
    return bool(
        expiry
        and expiry.timestamp() > datetime.now().astimezone().timestamp()
    )


async def whatsinfo_api_check(phone):
    """Return valid/invalid/unknown dengan retry aman saat API rate limit."""
    global WHATSINFO_LAST_REQUEST_AT

    cached = WHATSINFO_CACHE.get(phone)
    now = time.monotonic()
    if cached and now - cached["time"] < WHATSINFO_CACHE_TTL:
        return cached["status"], True

    async with WHATSINFO_REQUEST_LOCK:
        # Cek cache sekali lagi setelah memperoleh lock.
        cached = WHATSINFO_CACHE.get(phone)
        now = time.monotonic()
        if cached and now - cached["time"] < WHATSINFO_CACHE_TTL:
            return cached["status"], True

        for attempt in range(3):
            wait_for_gap = WHATSINFO_MIN_REQUEST_GAP - (
                time.monotonic() - WHATSINFO_LAST_REQUEST_AT
            )
            if wait_for_gap > 0:
                await asyncio.sleep(wait_for_gap)

            def send_request():
                return requests.post(
                    RAPIDAPI_URL,
                    headers={
                        "Content-Type": "application/json",
                        "x-rapidapi-key": RAPIDAPI_KEY,
                        "x-rapidapi-host": RAPIDAPI_HOST,
                    },
                    json={"phone_number": phone},
                    timeout=25,
                )

            try:
                response = await asyncio.to_thread(send_request)
                WHATSINFO_LAST_REQUEST_AT = time.monotonic()

                if response.status_code == 429:
                    retry_header = response.headers.get("Retry-After", "")
                    try:
                        retry_seconds = float(retry_header)
                    except (TypeError, ValueError):
                        retry_seconds = 15 * (attempt + 1)

                    retry_seconds = max(5, min(retry_seconds, 120))
                    logger.warning(
                        "WhatsInfo HTTP 429. Percobaan %s/3; tunggu %.1f detik.",
                        attempt + 1,
                        retry_seconds,
                    )
                    if attempt < 2:
                        await asyncio.sleep(retry_seconds)
                        continue
                    return "unknown", False

                response.raise_for_status()
                payload = response.json()
                status = str(payload.get("status", "")).strip().lower()

                if status in ("valid", "invalid"):
                    WHATSINFO_CACHE[phone] = {
                        "status": status,
                        "time": time.monotonic(),
                    }
                    return status, False

                logger.warning(
                    "Respons WhatsInfo tidak dikenali untuk nomor %s: %s",
                    phone,
                    str(payload)[:300],
                )
                return "unknown", False

            except requests.Timeout:
                logger.warning("WhatsInfo timeout untuk nomor %s.", phone)
                if attempt < 2:
                    await asyncio.sleep(5 * (attempt + 1))
                    continue
                return "unknown", False

            except requests.HTTPError as error:
                status_code = (
                    error.response.status_code
                    if error.response is not None else "?"
                )
                logger.warning(
                    "WhatsInfo HTTP error untuk nomor %s: %s",
                    phone,
                    status_code,
                )
                return "unknown", False

            except (requests.RequestException, ValueError, TypeError, AttributeError):
                logger.exception("WhatsInfo gagal memeriksa nomor %s.", phone)
                return "unknown", False

        return "unknown", False


async def check_whatsinfo_current_numbers(query, context):
    """Periksa nomor terakhir dari GET NUMBER dengan perlindungan rate limit."""
    user_id = query.from_user.id

    if not vvip_is_active(user_id):
        await query.message.reply_text(
            "🔒 <b>CEK BIO KHUSUS VVIP</b>\n\n"
            "Membership VVIP kamu tidak aktif. Silakan aktifkan VVIP terlebih dahulu.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [make_button("⭐ AKTIFKAN VVIP", callback_data="vvip_menu")],
                [make_button("🏠 MENU UTAMA", callback_data="home")],
            ]),
        )
        return

    if not RAPIDAPI_KEY:
        await query.message.reply_text(
            "❌ RAPIDAPI_KEY belum diatur di file .env."
        )
        return

    # Cegah pengguna menekan tombol berulang kali.
    now = time.monotonic()
    last_run = WHATSINFO_LAST_RUN_BY_USER.get(user_id, 0.0)
    remaining = WHATSINFO_USER_COOLDOWN - (now - last_run)
    if remaining > 0:
        await query.message.reply_text(
            f"⏳ Tunggu {int(remaining) + 1} detik sebelum menjalankan CEK BIO lagi."
        )
        return
    WHATSINFO_LAST_RUN_BY_USER[user_id] = now

    current = context.user_data.get("current_numbers") or []
    numbers = []
    for item in current:
        if isinstance(item, dict):
            raw = normalize_number(item.get("number", ""))
        else:
            raw = normalize_number(str(item))
        if raw and 7 <= len(raw) <= 15 and raw not in numbers:
            numbers.append(raw)

    if not numbers:
        await query.message.reply_text(
            "⚠️ Belum ada nomor yang diberikan bot untuk akun kamu.\n"
            "Silakan tekan GET NUMBER terlebih dahulu, lalu pilih CEK BIO (VVIP).",
            reply_markup=InlineKeyboardMarkup([
                [make_button("📱 GET NUMBER", callback_data="get_number")],
                [make_button("🏠 MENU UTAMA", callback_data="home")],
            ]),
        )
        return

    progress = await query.message.reply_text(
        f"⏳ CEK BIO sedang memeriksa {len(numbers)} nomor secara bertahap..."
    )

    copy_number_buttons = []
    registered_count = 0
    unregistered_count = 0
    unknown_count = 0

    for phone in numbers:
        if not vvip_is_active(user_id):
            unknown_count += 1
            copy_number_buttons.append([
                InlineKeyboardButton(
                    text=f"+{phone}",
                    copy_text=CopyTextButton(text=f"+{phone}"),
                    style="primary",
                )
            ])
            continue

        status, from_cache = await whatsinfo_api_check(phone)

        if status == "valid":
            registered_count += 1
            button_style = "danger"
        elif status == "invalid":
            unregistered_count += 1
            button_style = "primary"
        else:
            unknown_count += 1
            button_style = "primary"

        copy_number_buttons.append([
            InlineKeyboardButton(
                text=f"+{phone}",
                copy_text=CopyTextButton(text=f"+{phone}"),
                style=button_style,
            )
        ])

    try:
        await progress.delete()
    except TelegramError:
        pass

    header = (
        "🔎 <b>HASIL CEK BIO</b>\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )
    summary = (
        "\n━━━━━━━━━━━━━━━━━━━━\n"
        f"🟥 Terdaftar: <b>{registered_count}</b>\n"
        f"🟦 Belum terdaftar: <b>{unregistered_count}</b>\n"
        f"⬜ Tidak diketahui/gagal: <b>{unknown_count}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<i>CEK BIO VVIP • JING OTP</i>"
    )

    await query.message.reply_text(
        header + summary,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            copy_number_buttons + [
                [make_button("🔄 PERIKSA LAGI", callback_data="whatsinfo")],
                [make_button("🔄 CHANGE NUMBER", callback_data="next_numbers")],
                [make_button("🏠 MENU UTAMA", callback_data="home")],
            ]
        ),
    )


async def button_handler(update, context):
    query = update.callback_query
    data = query.data

    try:
        await query.answer()
    except TelegramError:
        logger.warning(
            "Callback sudah kedaluwarsa: %s",
            data,
        )

    # Admin dapat memproses pembayaran dan withdraw tanpa melewati gate user.
    if data.startswith("vvip_approve:") or data.startswith("vvip_reject:"):
        await handle_vvip_admin_callback(query, context, data)
        return

    if data.startswith("withdraw_paid:") or data.startswith("withdraw_reject:"):
        await handle_withdraw_admin_callback(query, context, data)
        return

    # Tombol pengecekan grup harus diproses sebelum gate.
    if data == "check_membership":
        await handle_membership_check(
            update,
            context,
        )
        return

    # Semua tombol lainnya wajib melewati pengecekan grup.
    if not await verify_user_access(update, context):
        return

    if data == "whatsinfo":
        await check_whatsinfo_current_numbers(query, context)

    elif data == "home":
        clear_user_flow(context)

        await safe_edit_message(
            query,
            "🏠 <b>MENU UTAMA</b>\n\n"
            "Silakan pilih menu:",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )


    elif data == "cancel":
        clear_user_flow(context)

        await safe_edit_message(
            query,
            "✅ Proses dibatalkan.",
            reply_markup=main_menu(),
        )

    elif data == "referral":
        await show_referral_page(query, context)

    elif data == "withdraw":
        await begin_withdrawal(query, context)

    elif data == "get_number":
        await safe_edit_message(
            query,
            "📱 SILAKAN PILIH LAYANAN:",
            reply_markup=service_menu(),
        )

    elif data == "service_whatsapp":
        await safe_edit_message(
            query,
            "🌍 PILIH NEGARA:",
            reply_markup=country_menu(),
        )

    elif data.startswith("country:"):
        country = data.split(":", 1)[1]

        if country not in COUNTRIES:
            return

        await send_number_list(
            update,
            context,
            country,
        )

    elif data.startswith("select_number:"):
        number = normalize_number(
            data.split(":", 1)[1]
        )

        user_id = update.effective_user.id

        owner_id = get_number_owner(number)

        if owner_id != user_id:
            await query.message.reply_text(
                "❌ Nomor ini bukan milik akun kamu atau sudah dikunci pengguna lain."
            )
            return

        await query.message.reply_text(
            f"✅ Nomor <b>+{number}</b> sudah ditautkan ke akun kamu.\n\n"
            "🔒 Nomor telah dikunci sejak ditampilkan.\n"
            "📩 SMS masuk untuk nomor ini akan diteruskan ke chat bot ini.\n"
            "Pastikan kamu sudah menekan /start.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    make_button(
                        "📋 SALIN NOMOR",
                        copy_text=CopyTextButton(text=f"+{number}"),
                    )
                ]
            ]),
        )

    elif data == "next_numbers":
        await send_next_three(
            query,
            context,
            edit_message=True,
        )

    elif data == "vvip_menu":
        await show_vvip_menu(query)

    elif data.startswith("vvip_buy:"):
        try:
            package_days = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await query.answer("Paket tidak valid.", show_alert=True)
            return
        await begin_vvip_payment(query, context, package_days)

    elif data == "vvip_status":
        expiry = get_vvip_expiry(query.from_user.id)
        if expiry and expiry.timestamp() > datetime.now().astimezone().timestamp():
            status_text = (
                "🟢 <b>VVIP AKTIF</b>\n\n"
                f"Masa aktif sampai:\n"
                f"<b>{escape(format_vvip_expiry(expiry))}</b>"
            )
        else:
            status_text = (
                "⚪ <b>VVIP BELUM AKTIF</b>\n\n"
                "Silakan pilih paket VVIP untuk berlangganan."
            )
        await safe_edit_message(
            query,
            status_text,
            parse_mode="HTML",
            reply_markup=vvip_package_keyboard(),
        )


# =====================================
# ERROR HANDLER
# =====================================

async def error_handler(update, context):
    logger.error(
        "Terjadi kesalahan:",
        exc_info=context.error,
    )


# =====================================
# POST INIT / SHUTDOWN
# =====================================

async def post_init(application):
    # ID grup wajib tersedia sebelum SMS forwarder mulai berjalan.
    application.bot_data["required_group_id"] = REQUIRED_GROUP_ID
    application.bot_data["required_channel_id"] = REQUIRED_CHANNEL_ID

    init_database()
    init_sms_database()
    init_vvip_database()

    application.bot_data["statistics_task"] = asyncio.create_task(
        statistics_refresh_loop(),
        name="statistics-refresh",
    )

    application.bot_data["sms_poll_task"] = asyncio.create_task(
        sms_poll_loop(application),
        name="sms-forwarder",
    )


async def post_shutdown(application):
    task = application.bot_data.get(
        "sms_poll_task"
    )

    if task is not None:
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

    stats_task = application.bot_data.get("statistics_task")
    if stats_task is not None:
        stats_task.cancel()
        try:
            await stats_task
        except asyncio.CancelledError:
            pass

    await close_client()


# =====================================
# MAIN
# =====================================

def main():
    if not BOT_TOKEN:
        raise ValueError(
            "BOT_TOKEN belum diatur di .env"
        )

    init_database()
    init_vvip_database()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("cancel", cancel_command)
    )

    application.add_handler(
        CallbackQueryHandler(button_handler)
    )

    application.add_handler(
        MessageHandler(filters.PHOTO, handle_vvip_proof_photo)
    )



    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_withdraw_text),
        group=1,
    )

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "JING OTP Bot berjalan..."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
