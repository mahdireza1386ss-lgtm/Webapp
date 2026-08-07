# -*- coding: utf-8 -*-
import logging
import json
import requests
import os
import uuid
import asyncio
import redis
import io
import time
import urllib3
import random
from datetime import datetime
from typing import Optional
from curl_cffi import requests as cffi_requests
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import JSONResponse
import uvicorn

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

# --- تولید لایسنس اختصاصی ---
def generate_license_key():
    return f"BARANLINK-{str(uuid.uuid4())[:8].upper()}-{str(uuid.uuid4())[:8].upper()}"

# --- غیرفعال‌سازی هشدارهای امنیتی SSL ---
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- لاگ‌ها ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- متغیرهای محیطی ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
allowed_users_env = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = [int(x.strip()) for x in allowed_users_env.split(",") if x.strip().isdigit()]

REDIS_URL = os.getenv("REDIS_URL")
PORT      = int(os.getenv("PORT", 8000))

# کلید امنیتی API — در Railway به عنوان Secret تنظیم کنید
API_SECRET_KEY = os.getenv("API_SECRET_KEY", "")

IRAN_PROXY = os.getenv("IRAN_PROXY", "")
SNAPPFOOD_PROXIES = {
    "http": IRAN_PROXY,
    "https": IRAN_PROXY
}

# --- اتصال به ردیس ---
try:
    if REDIS_URL:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        redis_client.ping()
        logger.info("✅ اتصال به ردیس موفق بود.")
    else:
        redis_client = None
except Exception as e:
    redis_client = None
    logger.error(f"❌ خطا در ردیس: {e}")

BASE_HEADERS = {
    'accept': 'application/json, text/plain, */*',
    'accept-language': 'fa',
    'content-type': 'application/json',
    'origin': 'https://snappfood.ir',
    'referer': 'https://snappfood.ir/',
    'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36'
}

# ======================== وب‌سرور FastAPI ========================

app = FastAPI(title="Baran Token API", docs_url=None, redoc_url=None)


@app.get("/api/BaranToken/{license_key}")
async def get_token(license_key: str, x_api_key: Optional[str] = Header(default=None)):
    """
    دریافت توکن‌های یک شماره از دیتابیس Redis با استفاده از لایسنس.
    هدر امنیتی: X-Api-Key
    """
    if API_SECRET_KEY and x_api_key != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not license_key.startswith("BARANLINK"):
        raise HTTPException(status_code=400, detail="Invalid license key format")

    if not redis_client:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        raw = redis_client.get(f"snappfood:license:{license_key}")
    except Exception as e:
        logger.error(f"خطای ردیس در API: {e}")
        raise HTTPException(status_code=503, detail="Database error")

    if not raw:
        raise HTTPException(status_code=404, detail="License key not found")

    data = json.loads(raw)
    return JSONResponse(content={
        "success": True,
        "phone_number": data.get("phone_number"),
        "access_token": data.get("access_token"),
        "refresh_token": data.get("refresh_token"),
        "updated_at": data.get("updated_at")
    })


@app.get("/health")
async def health_check():
    db_ok = False
    if redis_client:
        try:
            redis_client.ping()
            db_ok = True
        except Exception:
            pass
    return {"status": "ok", "database": "connected" if db_ok else "disconnected"}

# =================================================================


# ======================== سیستم رفرش توکن کوتاه ========================

def refresh_short_token(short_refresh_token: str) -> dict:
    """بازسازی توکن با اندپوینت کوتاه یکبار مصرف."""
    device_uid = str(uuid.uuid4())

    headers = {
        'authority': 'user.snappfood.ir',
        'accept': 'application/json, text/plain, */*',
        'accept-language': 'fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7',
        'content-type': 'application/json',
        'origin': 'https://snappfood.ir',
        'referer': 'https://snappfood.ir/',
        'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36'
    }

    payload = {
        "refreshToken": short_refresh_token,
        "grantType": "RefreshToken",
        "data": {
            "time": int(time.time()),
            "device_uid": device_uid,
            "client_id": "snappfood_pwa",
            "client_secret": "snappfood_pwa_secret",
            "scopes": ["mobile_v2", "mobile_v1", "webview"]
        }
    }

    try:
        res = cffi_requests.post(
            "https://user.snappfood.ir/v1/auth/token",
            json=payload,
            headers=headers,
            proxies=SNAPPFOOD_PROXIES,
            impersonate="chrome116",
            timeout=20,
            verify=False
        )

        if res.status_code == 200:
            data = res.json().get("data", {}) or {}
            new_access  = data.get("accessToken")
            new_refresh = data.get("refreshToken") or short_refresh_token
            if new_access:
                return {
                    'status': True,
                    'data': {'accessToken': new_access, 'refreshToken': new_refresh}
                }
            return {'status': False, 'error': 'accessToken در پاسخ وجود ندارد'}

        logger.error(f"رفرش توکن — HTTP {res.status_code}: {res.text[:200]}")
        return {'status': False, 'error': f"HTTP {res.status_code}: {res.text[:150]}"}

    except Exception as e:
        logger.error(f"خطای رفرش توکن: {e}")
        return {'status': False, 'error': str(e)}

# =================================================================


# --- توابع API اسنپ‌فود ---

def send_verification_code(phone_number: str) -> dict:
    url = "https://user.snappfood.ir/v1/auth/otp/send"
    payload = {"mobile_number": phone_number, "type": "Customer"}
    try:
        response = requests.post(url, json=payload, headers=BASE_HEADERS,
                                 proxies=SNAPPFOOD_PROXIES, verify=False, timeout=15)
        if response.status_code == 200:
            return response.json()
        return {'status': False, 'error': f"HTTP {response.status_code}"}
    except Exception as e:
        return {'status': False, 'error': str(e)}


def verify_code_otp(phone_number: str, code: str, device_uid: str) -> dict:
    """تأیید کد OTP — با بازگرداندن وضعیت HTTP برای تشخیص دقیق ثبت نام."""
    url = "https://user.snappfood.ir/v1/auth/token"
    payload = {
        "cellphone": phone_number,
        "otpCode": int(code),
        "grantType": "Otp",
        "data": {
            "time": int(datetime.now().timestamp()),
            "device_uid": device_uid,
            "client_id": "snappfood_pwa",
            "client_secret": "snappfood_pwa_secret",
            "scopes": ["mobile_v2", "mobile_v1", "webview"]
        }
    }
    try:
        response = requests.post(url, json=payload, headers=BASE_HEADERS,
                                 proxies=SNAPPFOOD_PROXIES, verify=False, timeout=15)
        data = response.json()
        data['http_status'] = response.status_code
        return data
    except Exception as e:
        return {'http_status': 500, 'error': str(e)}


def register_new_user(phone_number: str, code: str, device_uid: str,
                      first_name: str, last_name: str) -> dict:
    """ثبت‌نام کاربر جدید."""
    url = "https://user.snappfood.ir/v1/auth/token"
    payload = {
        "cellphone": phone_number,
        "otpCode": int(code),
        "grantType": "Otp",
        "firstName": first_name,
        "lastName": last_name,
        "data": {
            "time": int(datetime.now().timestamp()),
            "device_uid": device_uid,
            "client_id": "snappfood_pwa",
            "client_secret": "snappfood_pwa_secret",
            "scopes": ["mobile_v2", "mobile_v1", "webview"]
        }
    }
    try:
        response = requests.post(url, json=payload, headers=BASE_HEADERS,
                                 proxies=SNAPPFOOD_PROXIES, verify=False, timeout=15)
        return response.json()
    except Exception as e:
        return {'status': False, 'error': str(e)}


# ======================== کیبوردهای آماده ========================

def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫  لغو عملیات", callback_data='cancel')]
    ])


def kb_resend_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄  ارسال مجدد کد", callback_data='resend_code')],
        [InlineKeyboardButton("🚫  لغو عملیات",    callback_data='cancel')]
    ])


def kb_next_or_finish() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕  ثبت اکانت جدید",        callback_data='next_line')],
        [InlineKeyboardButton("✅  پایان",                  callback_data='finish_session')],
        [InlineKeyboardButton("🚫  لغو عملیات",             callback_data='cancel')]
    ])


def kb_admin_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊  آمار دیتابیس",        callback_data='admin_stats'),
         InlineKeyboardButton("🔄  بازسازی توکن‌ها",      callback_data='admin_rebuild')],
        [InlineKeyboardButton("📥  استخراج فایل بکاپ",   callback_data='admin_extract')],
        [InlineKeyboardButton("🔑  استخراج توکن‌ها",      callback_data='admin_extract_tokens')],
        [InlineKeyboardButton("🗑  حذف لایسنس",            callback_data='admin_delete_hint')]
    ])


def kb_back_to_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙  بازگشت به پنل",        callback_data='admin_back')]
    ])

# =================================================================


# --- تابع لغو ---
async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer("عملیات لغو شد.")
        await update.callback_query.edit_message_text(
            "🚫 عملیات لغو شد.\n\nبرای شروع مجدد دستور /start را ارسال کنید."
        )
    elif update.message:
        await update.message.reply_text(
            "🚫 عملیات لغو شد.\n\nبرای شروع مجدد دستور /start را ارسال کنید.",
            reply_markup=ReplyKeyboardRemove()
        )
    return ConversationHandler.END


# --- وضعیت‌های مکالمه ---
ASK_PHONE, ASK_CODE, ASK_NEXT_ACTION = range(3)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.message.from_user
    if user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔️ شما دسترسی به این ربات ندارید.")
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data['session_phones'] = []

    text = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🌧  *ربات تولید لایسنس Baran*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📱  ابتدا شماره موبایل مشتری را وارد کنید:\n"
        "_(فرمت صحیح: `09XXXXXXXXX`)_"
    )
    await update.message.reply_text(text, reply_markup=kb_cancel(), parse_mode='Markdown')
    return ASK_PHONE


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔️ شما دسترسی به این ربات ندارید.")
        return
    text = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📖  *راهنمای ربات*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔹 /start  —  تولید لایسنس جدید\n"
        "🔹 /admin  —  پنل مدیریت\n"
        "🔹 /delete `BARANLINK-XXXX`  —  حذف لایسنس از دیتابیس\n"
        "🔹 /cancel  —  لغو عملیات جاری\n"
        "🔹 /help   —  نمایش این راهنما\n\n"
        "📌 در هر مرحله دکمه *لغو عملیات* را می‌توانید بزنید."
    )
    await update.message.reply_text(text, parse_mode='Markdown')


async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone_number = update.message.text.strip()
    if not (phone_number.startswith("09") and len(phone_number) == 11 and phone_number.isdigit()):
        await update.message.reply_text(
            "⚠️  *شماره وارد شده نامعتبر است.*\n\n"
            "لطفاً با فرمت صحیح وارد کنید:\n`09XXXXXXXXX`",
            reply_markup=kb_cancel(),
            parse_mode='Markdown'
        )
        return ASK_PHONE

    context.user_data['phone_number'] = phone_number
    wait_msg = await update.message.reply_text(
        f"⏳  درحال ارسال کد تأیید به `{phone_number}` ...",
        parse_mode='Markdown'
    )

    res = await asyncio.to_thread(send_verification_code, phone_number)

    if res.get('status') or res.get('success'):
        await wait_msg.delete()
        await update.message.reply_text(
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅  *کد تأیید ارسال شد*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📲  کد ۵ رقمی ارسال‌شده به `{phone_number}` را وارد کنید:",
            reply_markup=kb_resend_cancel(),
            parse_mode='Markdown'
        )
        return ASK_CODE
    else:
        err_msg = res.get('error', 'ارتباط با سرور برقرار نشد.')
        await wait_msg.delete()
        await update.message.reply_text(
            f"❌  *خطا در ارسال کد تأیید*\n\n"
            f"جزئیات: `{err_msg}`\n\n"
            f"دستور /start را مجدداً ارسال کنید.",
            parse_mode='Markdown'
        )
        return ConversationHandler.END


async def resend_code_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    phone = context.user_data.get('phone_number')
    if not phone:
        await query.answer("⚠️ شماره‌ای ثبت نشده است.", show_alert=True)
        return ASK_CODE

    await query.answer("درحال ارسال مجدد کد...")
    res = await asyncio.to_thread(send_verification_code, phone)

    if res.get('status') or res.get('success'):
        await query.edit_message_text(
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔄  *کد جدید ارسال شد*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📲  کد جدید ارسال‌شده به `{phone}` را وارد کنید:",
            reply_markup=kb_resend_cancel(),
            parse_mode='Markdown'
        )
    else:
        await query.edit_message_text(
            "❌  ارسال مجدد با خطا مواجه شد.\nدوباره تلاش کنید یا عملیات را لغو کنید.",
            reply_markup=kb_resend_cancel()
        )
    return ASK_CODE


async def ask_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = update.message.text.strip()
    phone_number = context.user_data.get('phone_number')

    if not code.isdigit():
        await update.message.reply_text(
            "⚠️  لطفاً فقط اعداد کد تأیید را وارد کنید.",
            reply_markup=kb_resend_cancel()
        )
        return ASK_CODE

    wait_msg = await update.message.reply_text("⏳  درحال اعتبارسنجی کد...")

    device_uid = str(uuid.uuid4())
    context.user_data['device_uid'] = device_uid
    context.user_data['otp_code']   = code

    res = await asyncio.to_thread(verify_code_otp, phone_number, code, device_uid)

    http_status   = res.get('http_status', 500)
    data_dict     = res.get('data') or {}
    access_token  = data_dict.get('accessToken')
    refresh_token = data_dict.get('refreshToken')

    if http_status == 200:
        if access_token:
            # ✅ ورود موفق
            await wait_msg.delete()
            return await _save_and_reply(update, context, phone_number, device_uid, access_token, refresh_token)
        else:
            # 🆕 کاربر جدید: ساخت اسم رندوم و ثبت‌نام خودکار
            await wait_msg.edit_text("⏳ کاربر جدید تشخیص داده شد. درحال ساخت اکانت...")
            
            first_names = ["علی", "محمد", "یوسف", "امیر", "حسین", "رضا", "مهدی", "سارا", "زهرا", "مریم", "علیرضا"]
            last_names = ["راد", "تهرانی", "حسینی", "پارسا", "دانش", "آریا", "محمدی", "کریمی", "احمدی"]
            f_name = random.choice(first_names)
            l_name = random.choice(last_names)
            
            reg_res = await asyncio.to_thread(register_new_user, phone_number, code, device_uid, f_name, l_name)
            
            reg_data = reg_res.get('data') or {}
            new_access = reg_data.get('accessToken')
            new_refresh = reg_data.get('refreshToken')
            
            if new_access:
                await wait_msg.delete()
                return await _save_and_reply(update, context, phone_number, device_uid, new_access, new_refresh)
            else:
                err = reg_res.get('error') or reg_res.get('message') or 'خطا در ثبت‌نام خودکار.'
                await wait_msg.edit_text(f"❌ *خطا در ساخت اکانت*\nجزئیات: `{err}`", parse_mode='Markdown')
                return ConversationHandler.END
    else:
        # ❌ کد اشتباه یا منقضی
        err_msg = res.get('error') or res.get('message') or 'کد نامعتبر یا منقضی شده است.'
        await wait_msg.delete()
        await update.message.reply_text(
            f"⚠️  *خطا در اعتبارسنجی*\n\n"
            f"جزئیات: `{err_msg}`\n\n"
            f"کد جدید دریافت کنید یا عملیات را لغو کنید:",
            reply_markup=kb_resend_cancel(),
            parse_mode='Markdown'
        )
        return ASK_CODE


async def _save_and_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    phone_number: str,
    device_uid: str,
    access_token: str,
    refresh_token: str
) -> int:
    """ذخیره توکن در ردیس و ارسال پیام نهایی با تولید لایسنس."""
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    license_key = generate_license_key()

    if redis_client:
        created_at = now_str
        redis_data = {
            "phone_number":  phone_number,
            "device_uid":    device_uid,
            "access_token":  access_token,
            "refresh_token": refresh_token,
            "created_at":    created_at,
            "updated_at":    now_str,
            "license_key":   license_key
        }
        try:
            redis_client.set(
                f"snappfood:license:{license_key}",
                json.dumps(redis_data, ensure_ascii=False)
            )
        except Exception as e:
            logger.error(f"خطا در ذخیره ردیس: {e}")

    context.user_data.setdefault('session_phones', []).append(f"`{license_key}`")

    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "✅  *لایسنس با موفقیت ساخته شد!*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔑  لایسنس مشتری: `{license_key}`\n\n"
        "✅ این لایسنس اختصاصی ساخته و در سیستم ثبت شد. می‌توانید آن را تحویل مشتری دهید.\n\n"
        "🔽  مرحله بعد را انتخاب کنید:",
        reply_markup=kb_next_or_finish(),
        parse_mode='Markdown'
    )
    return ASK_NEXT_ACTION


async def next_line_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    count = len(context.user_data.get('session_phones', []))
    await query.edit_message_text(
        f"✅  لایسنس {count} ذخیره شد.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📱  شماره موبایل مشتری بعدی را وارد کنید:\n"
        f"_(فرمت صحیح: `09XXXXXXXXX`)_",
        reply_markup=kb_cancel(),
        parse_mode='Markdown'
    )
    return ASK_PHONE


async def finish_session_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("جلسه پایان یافت.")
    await query.edit_message_reply_markup(reply_markup=None)

    phones = context.user_data.get('session_phones', [])
    context.user_data.clear()

    if phones:
        count = len(phones)
        phones_text = "\n".join(phones)
        msg = (
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦  *لایسنس‌های صادر شده در این جلسه*\n"
            f"🔢  تعداد: {count} لایسنس\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{phones_text}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅  برای تولید لایسنس جدید: /start"
        )
        await query.message.reply_text(msg, parse_mode='Markdown')
    else:
        await query.message.reply_text(
            "ℹ️  هیچ لایسنسی در این جلسه تولید نشد.\n\nبرای شروع: /start"
        )
    return ConversationHandler.END


# --- پنل مدیریت ادمین ---
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ALLOWED_USER_IDS:
        return

    db_status    = "🟢 متصل" if redis_client else "🔴 قطع"
    record_count = len(redis_client.keys("snappfood:license:*")) if redis_client else 0

    text = (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️  *پنل مدیریت*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🗄  وضعیت دیتابیس: {db_status}\n"
        f"📊  تعداد رکوردها: `{record_count}`\n\n"
        f"یک گزینه را انتخاب کنید:"
    )
    await update.message.reply_text(text, reply_markup=kb_admin_main(), parse_mode='Markdown')


async def delete_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ALLOWED_USER_IDS:
        return
    if not context.args:
        await update.message.reply_text(
            "⚠️  *نحوه استفاده:*\n`/delete BARANLINK-XXXX-XXXX`",
            parse_mode='Markdown'
        )
        return
    license_key = context.args[0].strip()
    if not license_key.startswith("BARANLINK"):
        await update.message.reply_text("⚠️  فرمت لایسنس نامعتبر است.", parse_mode='Markdown')
        return
    if redis_client and redis_client.delete(f"snappfood:license:{license_key}"):
        await update.message.reply_text(
            f"✅  لایسنس `{license_key}` با موفقیت از دیتابیس حذف شد.", parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            f"⚠️  لایسنس `{license_key}` در دیتابیس یافت نشد.", parse_mode='Markdown'
        )


async def process_database_rebuild(chat_id: int, bot):
    """بازسازی توکن‌ها در پس‌زمینه."""
    if not redis_client:
        await bot.send_message(chat_id=chat_id, text="❌  دیتابیس ردیس متصل نیست!")
        return

    keys = redis_client.keys("snappfood:license:*")
    total = len(keys)
    if total == 0:
        await bot.send_message(chat_id=chat_id, text="ℹ️  هیچ رکوردی در دیتابیس یافت نشد.")
        return

    success_count, fail_count = 0, 0
    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔄  *شروع بازسازی توکن‌ها*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📊  مجموع رکوردها: `{total}`\n"
            f"⏳  لطفاً صبر کنید..."
        ),
        parse_mode='Markdown'
    )

    for key in keys:
        try:
            raw = redis_client.get(key)
            if not raw:
                fail_count += 1
                continue
            data    = json.loads(raw)
            phone   = data.get("phone_number")
            r_token = data.get("refresh_token")

            if not phone or not r_token:
                fail_count += 1
                continue

            res = await asyncio.to_thread(refresh_short_token, r_token)
            new_data_dict = res.get('data') or {}
            new_access    = new_data_dict.get('accessToken')
            new_refresh   = new_data_dict.get('refreshToken')

            if res.get('status') and new_access:
                data["access_token"]  = new_access
                data["refresh_token"] = new_refresh or r_token
                data["updated_at"]    = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                redis_client.set(key, json.dumps(data, ensure_ascii=False))
                success_count += 1
            else:
                logger.warning(f"رفرش ناموفق برای {phone}: {res.get('error')}")
                fail_count += 1

        except Exception as ex:
            logger.error(f"خطا در بازسازی {key}: {ex}")
            fail_count += 1

        await asyncio.sleep(2)

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅  *بازسازی پایان یافت*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📊  مجموع: `{total}`\n"
            f"🟢  موفق: `{success_count}`\n"
            f"🔴  ناموفق: `{fail_count}`\n\n"
            f"💡  می‌توانید بکاپ بگیرید."
        ),
        parse_mode='Markdown'
    )


async def admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not redis_client:
        await query.answer("❌ دیتابیس ردیس متصل نیست!", show_alert=True)
        return

    if query.data == 'admin_stats':
        await query.answer()
        keys  = redis_client.keys("snappfood:license:*")
        count = len(keys)
        await query.edit_message_text(
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊  *آمار دیتابیس*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🗄  تعداد کل لایسنس‌های ذخیره‌شده: `{count}`\n\n"
            f"🗑  برای حذف یک لایسنس:\n`/delete BARANLINK-XXXX-XXXX`",
            parse_mode='Markdown',
            reply_markup=kb_back_to_admin()
        )

    elif query.data == 'admin_extract':
        keys = redis_client.keys("snappfood:license:*")
        if not keys:
            await query.answer("⚠️ دیتابیس خالی است!", show_alert=True)
            return
        await query.answer("درحال آماده‌سازی فایل بکاپ...")
        lines = [
            "فایل بکاپ دیتابیس (لایسنس‌ها)",
            f"تاریخ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "-" * 40, ""
        ]
        for k in keys:
            try:
                data = json.loads(redis_client.get(k))
                lines.append(f"لایسنس:          {data.get('license_key', k.split(':')[-1])}")
                lines.append(f"شماره موبایل:    {data.get('phone_number', 'نامشخص')}")
                lines.append(f"آخرین بروزرسانی: {data.get('updated_at', 'نامشخص')}")
                lines.append("-" * 40)
            except Exception:
                continue
        content = "\n".join(lines)
        doc = io.BytesIO(content.encode('utf-8'))
        doc.seek(0)
        doc.name = f"Backup_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
        await query.message.reply_document(
            doc,
            caption=(
                f"📥  *فایل بکاپ دیتابیس*\n"
                f"📊  تعداد رکوردها: `{len(keys)}`\n"
                f"🕐  زمان: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
            ),
            parse_mode='Markdown'
        )

    elif query.data == 'admin_extract_tokens':
        keys = redis_client.keys("snappfood:license:*")
        if not keys:
            await query.answer("⚠️ دیتابیس خالی است!", show_alert=True)
            return
        await query.answer("درحال آماده‌سازی فایل توکن‌ها...")
        lines = [
            "لیست کامل توکن‌ها به همراه لایسنس‌ها",
            f"تاریخ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "=" * 60, ""
        ]
        for k in keys:
            try:
                data = json.loads(redis_client.get(k))
                lines.append(f"لایسنس:        {data.get('license_key', k.split(':')[-1])}")
                lines.append(f"شماره موبایل:  {data.get('phone_number', 'نامشخص')}")
                lines.append(f"Access Token:  {data.get('access_token', 'ندارد')}")
                lines.append(f"Refresh Token: {data.get('refresh_token', 'ندارد')}")
                lines.append(f"آخرین بروزرسانی: {data.get('updated_at', 'نامشخص')}")
                lines.append("-" * 60)
            except Exception:
                continue
        content = "\n".join(lines)
        doc = io.BytesIO(content.encode('utf-8'))
        doc.seek(0)
        doc.name = f"Tokens_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
        await query.message.reply_document(
            doc,
            caption=(
                f"🔑  *فایل جامع توکن‌ها و لایسنس‌ها*\n"
                f"📊  تعداد رکوردها: `{len(keys)}`\n"
                f"🕐  زمان: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n\n"
                f"⚠️  این فایل حساس است. مراقب باشید."
            ),
            parse_mode='Markdown'
        )

    elif query.data == 'admin_rebuild':
        await query.answer("عملیات بازسازی شروع شد...")
        await query.edit_message_text(
            "🔄  *عملیات بازسازی در پس‌زمینه آغاز شد...*\n\nبه محض پایان نتیجه ارسال می‌شود.",
            parse_mode='Markdown'
        )
        asyncio.ensure_future(process_database_rebuild(query.message.chat_id, context.bot))

    elif query.data == 'admin_delete_hint':
        await query.answer()
        await query.message.reply_text(
            "🗑  *حذف لایسنس از دیتابیس:*\n\n"
            "دستور زیر را ارسال کنید:\n`/delete BARANLINK-XXXX-XXXX`",
            parse_mode='Markdown'
        )

    elif query.data == 'admin_back':
        await query.answer()
        db_status    = "🟢 متصل" if redis_client else "🔴 قطع"
        record_count = len(redis_client.keys("snappfood:license:*")) if redis_client else 0
        text = (
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚙️  *پنل مدیریت*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🗄  وضعیت دیتابیس: {db_status}\n"
            f"📊  تعداد رکوردها: `{record_count}`\n\n"
            f"یک گزینه را انتخاب کنید:"
        )
        await query.edit_message_text(text, reply_markup=kb_admin_main(), parse_mode='Markdown')
    else:
        await query.answer()


# ======================== اجرای همزمان ربات + وب‌سرور ========================

async def run_bot():
    """اجرای ربات تلگرام به صورت async."""
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("❌ TELEGRAM_BOT_TOKEN تنظیم نشده است!")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone),
                CallbackQueryHandler(cancel_action, pattern='^cancel$')
            ],
            ASK_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_code),
                CallbackQueryHandler(resend_code_callback, pattern='^resend_code$'),
                CallbackQueryHandler(cancel_action,         pattern='^cancel$')
            ],
            ASK_NEXT_ACTION: [
                CallbackQueryHandler(next_line_callback,      pattern='^next_line$'),
                CallbackQueryHandler(finish_session_callback,  pattern='^finish_session$'),
                CallbackQueryHandler(cancel_action,            pattern='^cancel$')
            ]
        },
        fallbacks=[
            CommandHandler("cancel", cancel_action),
            CommandHandler("start",  start),
        ],
        allow_reentry=True,
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("admin",  admin_command))
    application.add_handler(CommandHandler("delete", delete_number))
    application.add_handler(CommandHandler("help",   help_command))
    application.add_handler(CallbackQueryHandler(admin_callbacks, pattern="^admin_"))

    logger.info("🤖 ربات در حال راه‌اندازی...")
    await application.initialize()
    await application.start()
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("🤖 ربات با موفقیت راه‌اندازی شد.")

    # منتظر می‌ماند تا سیگنال توقف بیاید
    await asyncio.Event().wait()


async def run_webserver():
    """اجرای وب‌سرور FastAPI."""
    config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=PORT,
        log_level="info"
    )
    server = uvicorn.Server(config)
    logger.info(f"🌐 وب‌سرور روی پورت {PORT} در حال راه‌اندازی...")
    await server.serve()


async def main():
    """اجرای همزمان ربات تلگرام و وب‌سرور."""
    await asyncio.gather(
        run_bot(),
        run_webserver()
    )


if __name__ == "__main__":
    asyncio.run(main())
