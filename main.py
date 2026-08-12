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

# --- اسامی رندوم برای ثبت‌نام خودکار ---
FIRST_NAMES = ["علی", "محمد", "یوسف", "امیر", "حسین", "رضا", "مهدی", "سارا", "زهرا", "مریم", "علیرضا", "عرفان", "نیما"]
LAST_NAMES = ["راد", "تهرانی", "حسینی", "پارسا", "دانش", "آریا", "محمدی", "کریمی", "احمدی", "ت زاده", "کمالی", "مجیدی"]

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
API_SECRET_KEY = os.getenv("API_SECRET_KEY", "")

IRAN_PROXY = os.getenv("IRAN_PROXY", "")
SNAPPFOOD_PROXIES = {
    "http": IRAN_PROXY,
    "https": IRAN_PROXY
}

# --- تنظیمات عمومی اسنپ ---
SNAPP_MARKET_BASE_URL = "https://svc.snapp.market"
SNAPP_MARKET_CLIENT = os.getenv("SNAPP_MARKET_CLIENT", "PWA")
SNAPP_MARKET_DEVICE_TYPE = os.getenv("SNAPP_MARKET_DEVICE_TYPE", "PWA")
SNAPP_MARKET_APP_VERSION = os.getenv("SNAPP_MARKET_APP_VERSION", "1.397.62")
SNAPP_MARKET_LAT = os.getenv("SNAPP_MARKET_LAT", "35.773643")
SNAPP_MARKET_LONG = os.getenv("SNAPP_MARKET_LONG", "51.418311")
SNAPP_MARKET_SSO_CHANNEL = os.getenv("SNAPP_MARKET_SSO_CHANNEL", "food")
SNAPP_MARKET_VERIFY_TLS = False

# فاصله‌ی عمدی و زیاد بین درخواست‌های چکر برای کاهش ریسک مسدودی
DISCOUNT_CHECK_MIN_DELAY = 8.0
DISCOUNT_CHECK_MAX_DELAY = 15.0
DISCOUNT_CHECK_MAX_PAGES = max(1, min(20, int(os.getenv("DISCOUNT_CHECK_MAX_PAGES", "5"))))

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

EXPRESS_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fa-IR, fa;q=0.9,en;q=0.8",
    "User-Agent": BASE_HEADERS["user-agent"],
    "Origin": "https://snapp.market",
    "Referer": "https://snapp.market/"
}

discount_check_lock = asyncio.Lock()

# ======================== وب‌سرور FastAPI ========================
app = FastAPI(title="Baran Token API", docs_url=None, redoc_url=None)

@app.get("/api/BaranToken/{license_key}")
async def get_token(license_key: str, x_api_key: Optional[str] = Header(default=None)):
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
# --- توابع API اسنپ‌اکسپرس (مرحله اول - ایمن‌سازی اکانت) ---

def _get_express_params(device_uid: str) -> dict:
    return {
        "client": SNAPP_MARKET_CLIENT,
        "deviceType": SNAPP_MARKET_DEVICE_TYPE,
        "appVersion": SNAPP_MARKET_APP_VERSION,
        "UDID": device_uid,
        "lat": SNAPP_MARKET_LAT,
        "long": SNAPP_MARKET_LONG
    }

def send_express_code(phone_number: str, device_uid: str) -> dict:
    url = f"{SNAPP_MARKET_BASE_URL}/mobile/v4/user/loginMobileWithNoPass"
    payload = {"captcha": "", "cellphone": phone_number, "optionalLoginToken": "true"}
    params = _get_express_params(device_uid)
    for attempt in range(3):
        try:
            res = requests.post(url, params=params, data=payload, headers=EXPRESS_HEADERS, proxies=SNAPPFOOD_PROXIES, verify=False, timeout=15)
            try:
                return res.json()
            except ValueError:
                if attempt < 2: time.sleep(1.5); continue
                return {'status': False, 'error': f'مسدود شده (کد {res.status_code})'}
        except Exception:
            if attempt < 2: time.sleep(1.5); continue
            return {'status': False, 'error': 'خطای ارتباط'}

def verify_express_code(phone_number: str, code: str, device_uid: str) -> dict:
    url = f"{SNAPP_MARKET_BASE_URL}/mobile/v2/user/loginMobileWithToken"
    payload = {"cellphone": phone_number, "code": code}
    params = _get_express_params(device_uid)
    for attempt in range(3):
        try:
            res = requests.post(url, params=params, data=payload, headers=EXPRESS_HEADERS, proxies=SNAPPFOOD_PROXIES, verify=False, timeout=15)
            try:
                data = res.json()
                data['http_status'] = res.status_code
                return data
            except ValueError:
                if attempt < 2: time.sleep(1.5); continue
                return {'http_status': res.status_code, 'status': False, 'error': 'خطای ارتباط'}
        except Exception:
            if attempt < 2: time.sleep(1.5); continue
            return {'http_status': 500, 'status': False, 'error': 'خطای ارتباط'}

def register_express_user(phone_number: str, code: str, device_uid: str, first_name: str, last_name: str) -> dict:
    url = f"{SNAPP_MARKET_BASE_URL}/mobile/v1/user/registerWithOptionalPass"
    payload = {"firstname": first_name, "lastname": last_name, "cellphone": phone_number, "code": code}
    params = _get_express_params(device_uid)
    for attempt in range(3):
        try:
            res = requests.post(url, params=params, data=payload, headers=EXPRESS_HEADERS, proxies=SNAPPFOOD_PROXIES, verify=False, timeout=15)
            try:
                return res.json()
            except ValueError:
                if attempt < 2: time.sleep(1.5); continue
                return {'status': False, 'error': 'خطای ارتباط'}
        except Exception:
            if attempt < 2: time.sleep(1.5); continue
            return {'status': False, 'error': 'خطای ارتباط'}

# =================================================================
# --- توابع API اسنپ‌فود (مرحله دوم - استخراج توکن) ---

def send_food_code(phone_number: str) -> dict:
    url = "https://user.snappfood.ir/v1/auth/otp/send"
    payload = {"mobile_number": phone_number, "type": "Customer"}
    for attempt in range(3):
        try:
            response = requests.post(url, json=payload, headers=BASE_HEADERS, proxies=SNAPPFOOD_PROXIES, verify=False, timeout=15)
            try:
                return response.json()
            except ValueError:
                if attempt < 2: time.sleep(1.5); continue
                return {'status': False, 'error': f"مسدود شده (کد {response.status_code})"}
        except Exception:
            if attempt < 2: time.sleep(1.5); continue
            return {'status': False, 'error': "خطای ارتباط"}

def verify_food_code(phone_number: str, code: str, device_uid: str) -> dict:
    url = "https://user.snappfood.ir/v1/auth/token"
    payload = {
        "cellphone": phone_number, "otpCode": int(code), "grantType": "Otp",
        "data": {
            "time": int(datetime.now().timestamp()), "device_uid": device_uid,
            "client_id": "snappfood_pwa", "client_secret": "snappfood_pwa_secret",
            "scopes": ["mobile_v2", "mobile_v1", "webview"]
        }
    }
    for attempt in range(3):
        try:
            response = requests.post(url, json=payload, headers=BASE_HEADERS, proxies=SNAPPFOOD_PROXIES, verify=False, timeout=15)
            try:
                data = response.json()
                data['http_status'] = response.status_code
                return data
            except ValueError:
                if attempt < 2: time.sleep(1.5); continue
                return {'http_status': response.status_code, 'error': f"مسدود شده"}
        except Exception:
            if attempt < 2: time.sleep(1.5); continue
            return {'http_status': 500, 'error': "خطای ارتباط"}

def register_food_user(phone_number: str, code: str, device_uid: str, first_name: str, last_name: str) -> dict:
    url = "https://user.snappfood.ir/v1/auth/token"
    payload = {
        "cellphone": phone_number, "otpCode": int(code), "grantType": "Otp",
        "firstName": first_name, "lastName": last_name,
        "data": {
            "time": int(datetime.now().timestamp()), "device_uid": device_uid,
            "client_id": "snappfood_pwa", "client_secret": "snappfood_pwa_secret",
            "scopes": ["mobile_v2", "mobile_v1", "webview"]
        }
    }
    for attempt in range(3):
        try:
            response = requests.post(url, json=payload, headers=BASE_HEADERS, proxies=SNAPPFOOD_PROXIES, verify=False, timeout=15)
            try:
                return response.json()
            except ValueError:
                if attempt < 2: time.sleep(1.5); continue
                return {'status': False, 'error': f"مسدود شده"}
        except Exception:
            if attempt < 2: time.sleep(1.5); continue
            return {'status': False, 'error': "خطای ارتباط"}

def refresh_short_token(short_refresh_token: str) -> dict:
    device_uid = str(uuid.uuid4())
    headers = BASE_HEADERS.copy()
    headers.update({'authority': 'user.snappfood.ir'})
    payload = {
        "refreshToken": short_refresh_token, "grantType": "RefreshToken",
        "data": {
            "time": int(time.time()), "device_uid": device_uid,
            "client_id": "snappfood_pwa", "client_secret": "snappfood_pwa_secret",
            "scopes": ["mobile_v2", "mobile_v1", "webview"]
        }
    }
    for attempt in range(3):
        try:
            res = requests.post("https://user.snappfood.ir/v1/auth/token", json=payload, headers=headers, proxies=SNAPPFOOD_PROXIES, verify=False, timeout=20)
            try:
                data = res.json()
            except ValueError:
                if attempt < 2: time.sleep(1.5); continue
                return {'status': False, 'error': f"مسدود (کد {res.status_code})"}
            if res.status_code == 200:
                resp_data = data.get("data", {}) or {}
                new_access  = resp_data.get("accessToken")
                new_refresh = resp_data.get("refreshToken") or short_refresh_token
                if new_access: return {'status': True, 'data': {'accessToken': new_access, 'refreshToken': new_refresh}}
                return {'status': False, 'error': 'عدم دریافت لایسنس جدید.'}
            err_msg = data.get("error") or data.get("message") or "نامشخص"
            return {'status': False, 'error': f"بروز مشکل: {err_msg}"}
        except Exception as e:
            if attempt < 2: time.sleep(1.5); continue
            return {'status': False, 'error': str(e)}

# ======================== توابع چکر تخفیف ========================

def exchange_food_token_for_market_token(access_token: str, device_uid: str) -> dict:
    params = {"token": access_token, "sso_channel": SNAPP_MARKET_SSO_CHANNEL, **_get_express_params(device_uid)}
    try:
        response = requests.get(f"{SNAPP_MARKET_BASE_URL}/mobile/v2/user/snapp-sso", params=params, headers=EXPRESS_HEADERS, proxies=SNAPPFOOD_PROXIES, verify=False, timeout=20)
        if response.status_code != 200: return {"status": False, "retryable": response.status_code in {401, 403, 502}, "error_code": f"sso_http_{response.status_code}"}
        try:
            payload = response.json() or {}
        except ValueError:
            return {"status": False, "retryable": True, "error_code": "sso_invalid_json"}
        market_token = payload.get("data", {}).get("oauth2_token", {}).get("access_token")
        if not market_token: return {"status": False, "retryable": False, "error_code": "sso_token_missing"}
        return {"status": True, "access_token": market_token}
    except requests.RequestException:
        return {"status": False, "retryable": True, "error_code": "sso_network_error"}

def fetch_market_vouchers(market_access_token: str, device_uid: str) -> dict:
    headers = EXPRESS_HEADERS.copy()
    headers["Authorization"] = f"Bearer {market_access_token}"
    vouchers = []
    try:
        for page in range(1, DISCOUNT_CHECK_MAX_PAGES + 1):
            params = {"filterType": "all", "page": page, "pageSize": 10}
            response = requests.get(f"{SNAPP_MARKET_BASE_URL}/belladonna/api/v1/vouchers", params=params, headers=headers, proxies=SNAPPFOOD_PROXIES, verify=False, timeout=20)
            if response.status_code != 200: return {"status": False, "retryable": response.status_code in {401, 403, 502}, "error_code": f"voucher_http_{response.status_code}"}
            try: payload = response.json() or {}
            except ValueError: return {"status": False, "retryable": True, "error_code": "voucher_invalid_json"}
            if isinstance(payload, dict):
                page_items = payload.get("vouchers") or []
                if isinstance(page_items, list): vouchers.extend(item for item in page_items if isinstance(item, dict))
                if not payload.get("hasMore"): break
            else: return {"status": False, "retryable": False, "error_code": "voucher_invalid_response"}
        return {"status": True, "vouchers": vouchers}
    except requests.RequestException:
        return {"status": False, "retryable": True, "error_code": "voucher_network_error"}

def check_account_discounts(record: dict) -> dict:
    access_token = record.get("access_token")
    refresh_token = record.get("refresh_token")
    device_uid = record.get("device_uid") or str(uuid.uuid4())
    if not access_token: return {"status": False, "error_code": "access_token_missing"}
    for attempt in range(2):
        sso_result = exchange_food_token_for_market_token(access_token, device_uid)
        if sso_result.get("status"):
            voucher_result = fetch_market_vouchers(sso_result["access_token"], device_uid)
            if voucher_result.get("status"): return {"status": True, "vouchers": voucher_result.get("vouchers", []), "device_uid": device_uid, "refreshed": attempt == 1}
            should_refresh = voucher_result.get("retryable", False)
        else:
            should_refresh = sso_result.get("retryable", False)
        if not should_refresh or attempt != 0 or not refresh_token:
            return {"status": False, "error_code": (voucher_result.get("error_code") if sso_result.get("status") else sso_result.get("error_code"))}
        refresh_result = refresh_short_token(refresh_token)
        refresh_data = refresh_result.get("data") or {}
        new_access = refresh_data.get("accessToken")
        new_refresh = refresh_data.get("refreshToken") or refresh_token
        if not refresh_result.get("status") or not new_access:
            err = refresh_result.get("error", "refresh_failed")
            return {"status": False, "error_code": f"Refresh Error: {err}"}
        access_token, refresh_token = new_access, new_refresh
        record["access_token"], record["refresh_token"], record["device_uid"] = new_access, new_refresh, device_uid
    return {"status": False, "error_code": "check_failed"}

def get_account_type(record: dict) -> str:
    """رکوردهای قدیمی که این فیلد را ندارند، خام محسوب می‌شوند."""
    return "old" if record.get("account_type") == "old" else "raw"

def get_database_account_stats() -> dict:
    stats = {"total": 0, "raw": 0, "old": 0}
    if not redis_client:
        return stats

    for key in redis_client.keys("snappfood:license:*"):
        try:
            raw = redis_client.get(key)
            record = json.loads(raw) if raw else {}
            account_type = get_account_type(record)
            stats["total"] += 1
            stats[account_type] += 1
        except Exception:
            # داده‌های قدیمی یا خراب برای حفظ رفتار سازگار، خام در نظر گرفته می‌شوند.
            stats["total"] += 1
            stats["raw"] += 1
    return stats

def _text_value(value, default="نامشخص") -> str:
    if value is None or value == "": return default
    return str(value).replace("\r", " ").replace("\n", " ").strip()

def build_discount_report(results: list[dict], account_type: str) -> str:
    account_title = "اکانت‌های جدید/خام (raw)" if account_type == "raw" else "اکانت‌های قدیمی (old)"
    lines = [
        "گزارش چکر تخفیف اسنپ‌مارکت",
        f"بخش: {account_title}",
        f"تاریخ بررسی: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 72, ""
    ]
    total_discounts = 0
    for index, result in enumerate(results, start=1):
        phone = _text_value(result.get("phone_number"))
        license_key = _text_value(result.get("license_key"))
        vouchers = result.get("vouchers") or []
        total_discounts += len(vouchers)
        lines.extend([f"اکانت {index}", f"لایسنس: {license_key}", f"شماره موبایل: {phone}"])
        if result.get("status") != "ok": lines.append(f"وضعیت: خطا در بررسی ({_text_value(result.get('error_code'))})")
        elif not vouchers: lines.append("وضعیت: تخفیف اختصاصی فعالی پیدا نشد")
        else:
            lines.append(f"وضعیت: {len(vouchers)} تخفیف پیدا شد")
            for v_idx, voucher in enumerate(vouchers, start=1):
                lines.extend([
                    f"  تخفیف {v_idx}:", f"  کد تخفیف: {_text_value(voucher.get('code'))}",
                    f"  عنوان: {_text_value(voucher.get('title'))}", f"  توضیحات: {_text_value(voucher.get('description'))}",
                    f"  انقضا: {_text_value(voucher.get('expiryDateFormatted') or voucher.get('expiryDate'))}"
                ])
        lines.extend(["-" * 72, ""])
    lines.extend([
        f"تعداد اکانت‌های بررسی‌شده: {len(results)}",
        f"تعداد کل تخفیف‌های پیدا‌شده: {total_discounts}", "",
        "نکته امنیتی: برای حفظ محرمانگی، کدهای ارتباطی در این گزارش درج نشده‌اند."
    ])
    return "\n".join(lines)

async def safe_edit_discount_progress(progress_message, text: str) -> None:
    """به‌روزرسانی وضعیت زنده بدون ایجاد اختلال در فرآیند اصلی چکر."""
    try:
        await progress_message.edit_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.warning(f"به‌روزرسانی گزارش زنده چکر ناموفق بود: {e}")

async def process_discount_check(chat_id: int, bot, account_type: str) -> None:
    async with discount_check_lock:
        if not redis_client:
            await bot.send_message(chat_id=chat_id, text="❌ دیتابیس ردیس متصل نیست!")
            return
        all_keys = redis_client.keys("snappfood:license:*")
        keys = []
        for key in all_keys:
            try:
                raw = redis_client.get(key)
                record = json.loads(raw) if raw else {}
                if get_account_type(record) == account_type:
                    keys.append(key)
            except Exception:
                if account_type == "raw":
                    keys.append(key)
        if not keys:
            title = "خام" if account_type == "raw" else "قدیمی"
            await bot.send_message(chat_id=chat_id, text=f"ℹ️ هیچ لایسنس اکانت {title} برای بررسی در دیتابیس نیست.")
            return
        title = "خطوط خام" if account_type == "raw" else "خطوط قدیمی"
        progress_message = await bot.send_message(
            chat_id=chat_id,
            text=(
                f"🔎 *گزارش زنده چکر تخفیف ({title})*\n\n"
                "وضعیت: در صف بررسی\n"
                f"پیشرفت: `0/{len(keys)}`\n"
                "درخواست‌ها با فاصله‌ی ۸ تا ۱۵ ثانیه ارسال می‌شوند."
            ),
            parse_mode="Markdown"
        )
        results = []
        checked_count = 0
        for key in keys:
            raw = None
            record = {}
            license_key = key.split(":")[-1]
            await safe_edit_discount_progress(
                progress_message,
                (
                    f"🔎 *گزارش زنده چکر تخفیف ({title})*\n\n"
                    f"وضعیت: در حال بررسی `#{checked_count + 1}`\n"
                    f"پیشرفت: `{checked_count}/{len(keys)}`\n"
                    f"لایسنس فعلی: `{license_key}`"
                )
            )
            try:
                raw = redis_client.get(key)
                record = json.loads(raw) if raw else {}
                result = await asyncio.to_thread(check_account_discounts, record)
                refreshed = bool(result.get("refreshed"))
                if refreshed and result.get("status") and record.get("access_token"):
                    record["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    redis_client.set(key, json.dumps(record, ensure_ascii=False))
                results.append({
                    "status": "ok" if result.get("status") else "error", "error_code": result.get("error_code"),
                    "vouchers": result.get("vouchers", []), "phone_number": record.get("phone_number"),
                    "license_key": record.get("license_key") or key.split(":")[-1],
                })
            except Exception:
                results.append({"status": "error", "error_code": "unexpected_check_error", "vouchers": [], "phone_number": "نامشخص", "license_key": license_key})

            checked_count += 1
            latest_result = results[-1]
            if latest_result.get("status") == "ok":
                latest_status = f"✅ بررسی انجام شد؛ تخفیف پیدا شده: `{len(latest_result.get('vouchers', []))}`"
            else:
                latest_status = f"⚠️ خطا در بررسی: `{_text_value(latest_result.get('error_code'))}`"
            await safe_edit_discount_progress(
                progress_message,
                (
                    f"🔎 *گزارش زنده چکر تخفیف ({title})*\n\n"
                    f"وضعیت: {latest_status}\n"
                    f"پیشرفت: `{checked_count}/{len(keys)}`\n"
                    f"آخرین لایسنس: `{_text_value(latest_result.get('license_key'))}`\n"
                    f"شماره: `{_text_value(latest_result.get('phone_number'))}`\n"
                    f"تعداد تخفیف‌های پیدا شده تا این لحظه: `{sum(len(item.get('vouchers', [])) for item in results)}`"
                )
            )
            if key != keys[-1]:
                delay = random.uniform(DISCOUNT_CHECK_MIN_DELAY, DISCOUNT_CHECK_MAX_DELAY)
                await safe_edit_discount_progress(
                    progress_message,
                    (
                        f"🔎 *گزارش زنده چکر تخفیف ({title})*\n\n"
                        f"پیشرفت: `{checked_count}/{len(keys)}`\n"
                        f"آخرین لایسنس: `{_text_value(latest_result.get('license_key'))}`\n"
                        f"⏳ مکث امنیتی حدود `{int(delay)} ثانیه` تا درخواست بعدی..."
                    )
                )
                await asyncio.sleep(delay)
        content = build_discount_report(results, account_type)
        doc = io.BytesIO(content.encode("utf-8"))
        doc.name = f"Discount_Check_{account_type}_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
        await bot.send_document(chat_id=chat_id, document=doc, caption=f"✅ *بررسی تخفیف‌های {title} پایان یافت*\nلایسنس‌های بررسی‌شده: `{len(results)}`\nتعداد تخفیف‌ها: `{sum(len(item.get('vouchers', [])) for item in results)}`", parse_mode="Markdown")
        await bot.send_message(chat_id=chat_id, text="⚙️ *پنل مدیریت*\n\nپنل برای دسترسی سریع دوباره در دسترس است:", reply_markup=kb_admin_main(), parse_mode="Markdown")

async def process_database_rebuild(chat_id: int, bot):
    """بازسازی توکن‌ها در پس‌زمینه بر اساس لایسنس‌ها."""
    if not redis_client:
        await bot.send_message(chat_id=chat_id, text="❌ دیتابیس ردیس متصل نیست!")
        return

    keys = redis_client.keys("snappfood:license:*")
    total = len(keys)
    if total == 0:
        await bot.send_message(chat_id=chat_id, text="ℹ️ هیچ لایسنسی در دیتابیس یافت نشد.")
        return

    success_count, fail_count = 0, 0
    await bot.send_message(
        chat_id=chat_id,
        text=f"🔄 *شروع بازسازی توکن‌ها*\n\nمجموع رکوردها: `{total}`\n⏳ لطفاً صبر کنید...",
        parse_mode='Markdown'
    )

    for key in keys:
        try:
            raw = redis_client.get(key)
            if not raw:
                fail_count += 1
                continue
            data = json.loads(raw)
            phone = data.get("phone_number")
            r_token = data.get("refresh_token")

            if not phone or not r_token:
                fail_count += 1
                continue

            res = await asyncio.to_thread(refresh_short_token, r_token)
            new_data_dict = res.get('data') or {}
            new_access = new_data_dict.get('accessToken')
            new_refresh = new_data_dict.get('refreshToken')

            if res.get('status') and new_access:
                data["access_token"] = new_access
                data["refresh_token"] = new_refresh or r_token
                data["updated_at"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                redis_client.set(key, json.dumps(data, ensure_ascii=False))
                success_count += 1
            else:
                fail_count += 1
        except Exception as ex:
            logger.error(f"خطا در بازسازی {key}: {ex}")
            fail_count += 1
        
        await asyncio.sleep(2)

    await bot.send_message(
        chat_id=chat_id,
        text=f"✅ *بازسازی پایان یافت*\n\nمجموع: `{total}`\n🟢 موفق: `{success_count}`\n🔴 ناموفق: `{fail_count}`",
        parse_mode='Markdown'
    )

# ======================== کیبوردهای آماده ========================

def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⚙️  پنل مدیریت", callback_data='admin_open')], [InlineKeyboardButton("🚫  لغو عملیات", callback_data='cancel')]])

def kb_resend_step1() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔄  ارسال مجدد کد مرحله اول", callback_data='resend_code_1')], [InlineKeyboardButton("⚙️  پنل مدیریت", callback_data='admin_open')], [InlineKeyboardButton("🚫  لغو عملیات", callback_data='cancel')]])

def kb_resend_step2() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔄  ارسال مجدد کد مرحله دوم", callback_data='resend_code_2')], [InlineKeyboardButton("⚙️  پنل مدیریت", callback_data='admin_open')], [InlineKeyboardButton("🚫  لغو عملیات", callback_data='cancel')]])

def kb_next_or_finish() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("➕  تولید لایسنس جدید", callback_data='next_line')], [InlineKeyboardButton("✅  پایان", callback_data='finish_session')], [InlineKeyboardButton("⚙️  پنل مدیریت", callback_data='admin_open')], [InlineKeyboardButton("🚫  لغو عملیات", callback_data='cancel')]])

def kb_admin_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊  آمار دیتابیس", callback_data='admin_stats'), InlineKeyboardButton("🔑  استخراج گزارش کامل", callback_data='admin_extract_tokens')],
        [InlineKeyboardButton("➕  تولید لایسنس جدید", callback_data='admin_new_license')],
        [InlineKeyboardButton("➕  ثبت لایسنس اکانت قدیمی", callback_data='admin_old_license')],
        [InlineKeyboardButton("🔄  بازسازی اتصال‌ها", callback_data='admin_rebuild')],
        [InlineKeyboardButton("🎁  چکر تخفیف (خطوط خام)", callback_data='admin_discount_check_raw')],
        [InlineKeyboardButton("🎁  چکر تخفیف (خطوط قدیمی)", callback_data='admin_discount_check_old')],
        [InlineKeyboardButton("📥  استخراج فایل بکاپ", callback_data='admin_extract'), InlineKeyboardButton("🗑  حذف لایسنس", callback_data='admin_delete_hint')]
    ])

def kb_back_to_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙  بازگشت به پنل", callback_data='admin_back')]])

def kb_old_resend_step() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄  ارسال مجدد کد اسنپ‌فود", callback_data='old_resend_code')],
        [InlineKeyboardButton("⚙️  پنل مدیریت", callback_data='admin_open')],
        [InlineKeyboardButton("🚫  لغو عملیات", callback_data='cancel')]
    ])

# =================================================================

async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer("عملیات لغو شد.")
        await update.callback_query.edit_message_text("🚫 عملیات لغو شد.\n\nبرای شروع مجدد دستور /start را ارسال کنید.")
    elif update.message:
        await update.message.reply_text("🚫 عملیات لغو شد.\n\nبرای شروع مجدد دستور /start را ارسال کنید.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def exit_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if update.callback_query:
        query = update.callback_query
        await query.answer("به پنل مدیریت منتقل شدید.")
        await query.edit_message_text("⚙️ *پنل مدیریت*\n\nعملیات جاری متوقف شد.\nیک گزینه را انتخاب کنید:", reply_markup=kb_admin_main(), parse_mode="Markdown")
    elif update.message:
        await update.message.reply_text("⚙️ *پنل مدیریت*\n\nیک گزینه را انتخاب کنید:", reply_markup=kb_admin_main(), parse_mode="Markdown")
    return ConversationHandler.END

# --- مراحل ربات تلگرام ---
ASK_PHONE, ASK_CODE_STEP_1, ASK_CODE_STEP_2, ASK_NEXT_ACTION = range(4)
OLD_ASK_PHONE, OLD_ASK_CODE = range(4, 6)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.message.from_user
    if user.id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔️ شما دسترسی به این ربات ندارید.")
        return ConversationHandler.END

    context.user_data.clear()
    stats = get_database_account_stats()
    text = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚙️  *پنل اصلی مدیریت Baran*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🗄  وضعیت دیتابیس: {'🟢 متصل' if redis_client else '🔴 قطع'}\n"
        f"📊  مجموع رکوردها: `{stats['total']}`\n"
        f"🟠  اکانت‌های خام: `{stats['raw']}`\n"
        f"🔵  اکانت‌های قدیمی: `{stats['old']}`\n\n"
        "یک گزینه را انتخاب کنید:"
    )
    await update.message.reply_text(text, reply_markup=kb_admin_main(), parse_mode='Markdown')
    return ConversationHandler.END

async def start_raw_license_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    context.user_data.clear()
    context.user_data['session_phones'] = []
    await query.answer()
    await query.edit_message_text(
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "➕  *تولید لایسنس جدید*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📱  شماره موبایل مشتری را وارد کنید:\n"
        "_(فرمت صحیح: `09XXXXXXXXX`)_",
        reply_markup=kb_cancel(),
        parse_mode='Markdown'
    )
    return ASK_PHONE

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ALLOWED_USER_IDS:
        return
    text = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📖  *راهنمای ربات*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔹 /start  —  نمایش پنل اصلی مدیریت\n"
        "🔹 /admin  —  پنل مدیریت\n"
        "🔹 /delete `BARANLINK-XXXX`  —  حذف لایسنس از دیتابیس\n"
        "🔹 /cancel  —  لغو عملیات جاری\n"
    )
    await update.message.reply_text(text, parse_mode='Markdown')

async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone_number = update.message.text.strip()
    if not (phone_number.startswith("09") and len(phone_number) == 11 and phone_number.isdigit()):
        await update.message.reply_text("⚠️  شماره نامعتبر است. مجدداً ارسال کنید:", reply_markup=kb_cancel())
        return ASK_PHONE

    context.user_data['phone_number'] = phone_number
    device_uid = str(uuid.uuid4())
    context.user_data['device_uid'] = device_uid

    wait_msg = await update.message.reply_text("⏳  درحال آماده‌سازی بستر ایمن و ارسال کد مرحله اول...", parse_mode='Markdown')
    res = await asyncio.to_thread(send_express_code, phone_number, device_uid)

    if res.get('status') or res.get('success'):
        await wait_msg.delete()
        await update.message.reply_text(
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅  *کد مرحله اول ارسال شد*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📲  کد ۵ رقمی ارسال‌شده به `{phone_number}` را وارد کنید:",
            reply_markup=kb_resend_step1(), parse_mode='Markdown'
        )
        return ASK_CODE_STEP_1
    else:
        err_msg = res.get('error', 'ارتباط برقرار نشد.')
        await wait_msg.delete()
        await update.message.reply_text(f"❌  *بروز مشکل در مرحله اول*\n\nجزئیات: `{err_msg}`\n\nعملیات لغو شد. مجددا /start بزنید.", parse_mode='Markdown')
        return ConversationHandler.END

async def old_license_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    context.user_data.clear()
    await query.answer()
    await query.edit_message_text(
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "➕  *ثبت لایسنس اکانت قدیمی*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📱  شماره موبایل اکانت قدیمی را وارد کنید:\n"
        "_(فرمت صحیح: `09XXXXXXXXX`)_",
        reply_markup=kb_cancel(),
        parse_mode='Markdown'
    )
    return OLD_ASK_PHONE

async def old_ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone_number = update.message.text.strip()
    if not (phone_number.startswith("09") and len(phone_number) == 11 and phone_number.isdigit()):
        await update.message.reply_text("⚠️  شماره نامعتبر است. مجدداً ارسال کنید:", reply_markup=kb_cancel())
        return OLD_ASK_PHONE

    context.user_data['phone_number'] = phone_number
    context.user_data['device_uid'] = str(uuid.uuid4())
    wait_msg = await update.message.reply_text("⏳  درحال ارسال کد ورود مستقیم اسنپ‌فود...")
    res = await asyncio.to_thread(send_food_code, phone_number)

    if res.get('status') or res.get('success'):
        await wait_msg.delete()
        await update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅  *کد ورود اسنپ‌فود ارسال شد*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📲  کد ارسال‌شده به `{phone_number}` را وارد کنید:",
            reply_markup=kb_old_resend_step(),
            parse_mode='Markdown'
        )
        return OLD_ASK_CODE

    await wait_msg.edit_text(
        f"❌  *ارسال کد ورود با مشکل مواجه شد*\n\nجزئیات: `{res.get('error', 'ارتباط برقرار نشد.')}`\n\nعملیات لغو شد.",
        parse_mode='Markdown'
    )
    return ConversationHandler.END

async def old_resend_code_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    phone = context.user_data.get('phone_number')
    if not phone:
        await query.answer("⚠️ داده‌ای یافت نشد.", show_alert=True)
        return OLD_ASK_PHONE

    await query.answer("درحال ارسال مجدد کد اسنپ‌فود...")
    res = await asyncio.to_thread(send_food_code, phone)
    if res.get('status') or res.get('success'):
        await query.edit_message_text(
            "🔄  *کد اسنپ‌فود مجدداً ارسال شد*\n\n📲  کد جدید را وارد کنید:",
            reply_markup=kb_old_resend_step(),
            parse_mode='Markdown'
        )
    else:
        await query.edit_message_text("❌  ارسال مجدد با مشکل مواجه شد.", reply_markup=kb_old_resend_step())
    return OLD_ASK_CODE

async def old_ask_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = update.message.text.strip()
    phone_number = context.user_data.get('phone_number')
    device_uid = context.user_data.get('device_uid')

    if not code.isdigit():
        await update.message.reply_text("⚠️  لطفاً فقط اعداد را وارد کنید.", reply_markup=kb_old_resend_step())
        return OLD_ASK_CODE

    wait_msg = await update.message.reply_text("⏳  درحال ورود مستقیم به اسنپ‌فود و صدور لایسنس...")
    res = await asyncio.to_thread(verify_food_code, phone_number, code, device_uid)
    data_dict = res.get('data') or {}
    access_token, refresh_token = data_dict.get('accessToken'), data_dict.get('refreshToken')

    if res.get('http_status', 500) != 200 or not access_token:
        await wait_msg.delete()
        await update.message.reply_text(
            "⚠️  کد اسنپ‌فود نامعتبر است یا ورود انجام نشد. مجدداً تلاش کنید:",
            reply_markup=kb_old_resend_step()
        )
        return OLD_ASK_CODE

    if not redis_client:
        await wait_msg.edit_text("❌ دیتابیس ردیس متصل نیست؛ لایسنس ذخیره نشد.")
        return ConversationHandler.END

    license_key = generate_license_key()
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    redis_data = {
        "phone_number": phone_number,
        "device_uid": device_uid,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "created_at": now_str,
        "updated_at": now_str,
        "license_key": license_key,
        "account_type": "old"
    }
    try:
        redis_client.set(
            f"snappfood:license:{license_key}",
            json.dumps(redis_data, ensure_ascii=False)
        )
    except Exception as e:
        logger.error(f"خطا در ذخیره لایسنس قدیمی در ردیس: {e}")
        await wait_msg.edit_text("❌ خطا در ذخیره لایسنس در دیتابیس رخ داد.")
        return ConversationHandler.END

    context.user_data.clear()
    await wait_msg.edit_text(
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "✅  *لایسنس اکانت قدیمی با موفقیت ساخته شد!*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔑  لایسنس: `{license_key}`\n"
        "🏷  نوع اکانت: `old`\n\n"
        "لایسنس در دیتابیس ذخیره شد.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⚙️  بازگشت به پنل مدیریت", callback_data='admin_back')]
        ]),
        parse_mode='Markdown'
    )
    return ConversationHandler.END

async def resend_code_1_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    phone = context.user_data.get('phone_number')
    device_uid = context.user_data.get('device_uid')
    if not phone:
        await query.answer("⚠️ داده‌ای یافت نشد.", show_alert=True)
        return ASK_CODE_STEP_1

    await query.answer("درحال ارسال مجدد کد مرحله اول...")
    res = await asyncio.to_thread(send_express_code, phone, device_uid)

    if res.get('status') or res.get('success'):
        await query.edit_message_text(f"🔄  *کد مرحله اول مجدداً ارسال شد*\n\n📲  کد جدید را وارد کنید:", reply_markup=kb_resend_step1(), parse_mode='Markdown')
    else:
        await query.edit_message_text("❌  ارسال مجدد با مشکل مواجه شد.", reply_markup=kb_resend_step1())
    return ASK_CODE_STEP_1

async def ask_code_step_1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = update.message.text.strip()
    phone_number = context.user_data.get('phone_number')
    device_uid = context.user_data.get('device_uid')

    if not code.isdigit():
        await update.message.reply_text("⚠️  لطفاً فقط اعداد را وارد کنید.", reply_markup=kb_resend_step1())
        return ASK_CODE_STEP_1

    wait_msg = await update.message.reply_text("⏳  درحال بررسی کد مرحله اول...")
    res = await asyncio.to_thread(verify_express_code, phone_number, code, device_uid)

    http_status = res.get('http_status', 500)
    if http_status == 200:
        is_registered = res.get('data', {}).get('is_registered', False)
        
        # اگر اکانت خام بود، در پس‌زمینه با اسم رندوم تاییدش می‌کنیم تا سپر بلا شود
        if not is_registered:
            f_name, l_name = random.choice(FIRST_NAMES), random.choice(LAST_NAMES)
            reg_res = await asyncio.to_thread(register_express_user, phone_number, code, device_uid, f_name, l_name)
            if not reg_res.get('status') and not reg_res.get('success'):
                await wait_msg.edit_text("❌ ایمن‌سازی اولیه با مشکل مواجه شد. لغو عملیات.")
                return ConversationHandler.END

        # حالا که اکانت ایمن شد، وارد مرحله دوم (دریافت اطلاعات اصلی) می‌شویم
        await wait_msg.edit_text("✅ بستر ایمن شد. ⏳ در حال ارسال کد تایید نهایی...")
        food_res = await asyncio.to_thread(send_food_code, phone_number)
        
        if food_res.get('status') or food_res.get('success'):
            await wait_msg.delete()
            await update.message.reply_text(
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🔐  *کد تایید نهایی ارسال شد*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📲  لطفا آخرین کدی که برای `{phone_number}` پیامک شد را وارد کنید:",
                reply_markup=kb_resend_step2(), parse_mode='Markdown'
            )
            return ASK_CODE_STEP_2
        else:
            await wait_msg.delete()
            await update.message.reply_text("❌ ارسال کد نهایی با مشکل روبرو شد.", reply_markup=kb_resend_step2())
            return ASK_CODE_STEP_2
    else:
        await wait_msg.delete()
        await update.message.reply_text("⚠️  کد وارد شده نامعتبر است. مجددا تلاش کنید:", reply_markup=kb_resend_step1())
        return ASK_CODE_STEP_1

async def resend_code_2_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    phone = context.user_data.get('phone_number')
    if not phone:
        await query.answer("⚠️ داده‌ای یافت نشد.", show_alert=True)
        return ASK_CODE_STEP_2

    await query.answer("درحال ارسال مجدد کد نهایی...")
    res = await asyncio.to_thread(send_food_code, phone)
    if res.get('status') or res.get('success'):
        await query.edit_message_text(f"🔄  *کد نهایی مجدداً ارسال شد*\n\n📲  کد جدید را وارد کنید:", reply_markup=kb_resend_step2(), parse_mode='Markdown')
    else:
        await query.edit_message_text("❌  ارسال مجدد با مشکل مواجه شد.", reply_markup=kb_resend_step2())
    return ASK_CODE_STEP_2

async def ask_code_step_2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = update.message.text.strip()
    phone_number = context.user_data.get('phone_number')
    device_uid = context.user_data.get('device_uid')

    if not code.isdigit():
        await update.message.reply_text("⚠️  فقط اعداد مجاز است.", reply_markup=kb_resend_step2())
        return ASK_CODE_STEP_2

    wait_msg = await update.message.reply_text("⏳  درحال صدور لایسنس...")
    res = await asyncio.to_thread(verify_food_code, phone_number, code, device_uid)
    http_status = res.get('http_status', 500)
    data_dict = res.get('data') or {}
    access_token, refresh_token = data_dict.get('accessToken'), data_dict.get('refreshToken')

    if http_status == 200:
        if access_token:
            await wait_msg.delete()
            return await _save_and_reply(update, context, phone_number, device_uid, access_token, refresh_token)
        else:
            # بررسی تکمیلی در صورت خام بودن غیرمنتظره در سیستم مرکزی
            f_name, l_name = random.choice(FIRST_NAMES), random.choice(LAST_NAMES)
            reg_res = await asyncio.to_thread(register_food_user, phone_number, code, device_uid, f_name, l_name)
            reg_data = reg_res.get('data') or {}
            new_access, new_refresh = reg_data.get('accessToken'), reg_data.get('refreshToken')
            
            if new_access:
                await wait_msg.delete()
                return await _save_and_reply(update, context, phone_number, device_uid, new_access, new_refresh)
            else:
                await wait_msg.edit_text("❌ صدور لایسنس با مشکل مواجه شد.")
                return ConversationHandler.END
    else:
        await wait_msg.delete()
        await update.message.reply_text("⚠️  کد نهایی نامعتبر است. مجددا بررسی کنید:", reply_markup=kb_resend_step2())
        return ASK_CODE_STEP_2

async def _save_and_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE, phone_number: str, device_uid: str, access_token: str, refresh_token: str
) -> int:
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    license_key = generate_license_key()

    if redis_client:
        redis_data = {
            "phone_number": phone_number, "device_uid": device_uid,
            "access_token": access_token, "refresh_token": refresh_token,
            "created_at": now_str, "updated_at": now_str, "license_key": license_key,
            "account_type": "raw"
        }
        try:
            redis_client.set(f"snappfood:license:{license_key}", json.dumps(redis_data, ensure_ascii=False))
        except Exception as e:
            logger.error(f"خطا در ذخیره ردیس: {e}")

    context.user_data.setdefault('session_phones', []).append(f"`{license_key}`")

    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "✅  *لایسنس با موفقیت ساخته شد!*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔑  لایسنس مشتری: `{license_key}`\n\n"
        "✅ این لایسنس اختصاصی صادر شد. می‌توانید آن را تحویل مشتری دهید.\n\n"
        "🔽  مرحله بعد را انتخاب کنید:",
        reply_markup=kb_next_or_finish(), parse_mode='Markdown'
    )
    return ASK_NEXT_ACTION

async def next_line_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    count = len(context.user_data.get('session_phones', []))
    await query.edit_message_text(f"✅  لایسنس {count} ذخیره شد.\n\n━━━━━━━━━━━━━━━━━━━━━━\n📱  شماره موبایل مشتری بعدی را وارد کنید:\n_(فرمت صحیح: `09XXXXXXXXX`)_", reply_markup=kb_cancel(), parse_mode='Markdown')
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
        msg = f"━━━━━━━━━━━━━━━━━━━━━━\n📦  *لایسنس‌های صادر شده*\n🔢  تعداد: {count} لایسنس\n━━━━━━━━━━━━━━━━━━━━━━\n\n{phones_text}\n\n━━━━━━━━━━━━━━━━━━━━━━\n✅  تولید لایسنس جدید: /start"
        await query.message.reply_text(msg, parse_mode='Markdown')
    else:
        await query.message.reply_text("ℹ️  هیچ لایسنسی تولید نشد.\n\nشروع: /start")
    return ConversationHandler.END


# --- پنل مدیریت ادمین ---
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ALLOWED_USER_IDS: return
    db_status = "🟢 متصل" if redis_client else "🔴 قطع"
    stats = get_database_account_stats()
    text = f"━━━━━━━━━━━━━━━━━━━━━━\n⚙️  *پنل مدیریت*\n━━━━━━━━━━━━━━━━━━━━━━\n\n🗄  وضعیت دیتابیس: {db_status}\n📊  مجموع رکوردها: `{stats['total']}`\n🟠  اکانت‌های خام: `{stats['raw']}`\n🔵  اکانت‌های قدیمی: `{stats['old']}`\n\nیک گزینه را انتخاب کنید:"
    await update.message.reply_text(text, reply_markup=kb_admin_main(), parse_mode='Markdown')

async def delete_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ALLOWED_USER_IDS: return
    if not context.args:
        await update.message.reply_text("⚠️  *نحوه استفاده:*\n`/delete BARANLINK-XXXX-XXXX`", parse_mode='Markdown')
        return
    license_key = context.args[0].strip()
    if not license_key.startswith("BARANLINK"):
        await update.message.reply_text("⚠️  فرمت لایسنس نامعتبر است.", parse_mode='Markdown')
        return
    if redis_client and redis_client.delete(f"snappfood:license:{license_key}"):
        await update.message.reply_text(f"✅  لایسنس `{license_key}` حذف شد.", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"⚠️  لایسنس `{license_key}` یافت نشد.", parse_mode='Markdown')

async def admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query.from_user or query.from_user.id not in ALLOWED_USER_IDS:
        await query.answer("⛔️ شما دسترسی ندارید.", show_alert=True)
        return

    if query.data == 'admin_open':
        await query.answer()
        db_status = "🟢 متصل" if redis_client else "🔴 قطع"
        stats = get_database_account_stats()
        text = f"━━━━━━━━━━━━━━━━━━━━━━\n⚙️  *پنل مدیریت*\n━━━━━━━━━━━━━━━━━━━━━━\n\n🗄  وضعیت دیتابیس: {db_status}\n📊  مجموع رکوردها: `{stats['total']}`\n🟠  اکانت‌های خام: `{stats['raw']}`\n🔵  اکانت‌های قدیمی: `{stats['old']}`\n\nیک گزینه را انتخاب کنید:"
        await query.edit_message_text(text, reply_markup=kb_admin_main(), parse_mode='Markdown')
    elif not redis_client:
        await query.answer("❌ دیتابیس متصل نیست!", show_alert=True)
    elif query.data == 'admin_stats':
        await query.answer()
        stats = get_database_account_stats()
        await query.edit_message_text(
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊  *آمار دیتابیس*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🗄  مجموع لایسنس‌ها: `{stats['total']}`\n"
            f"🟠  اکانت‌های خام (raw): `{stats['raw']}`\n"
            f"🔵  اکانت‌های قدیمی (old): `{stats['old']}`\n\n"
            f"🗑  برای حذف:\n`/delete BARANLINK-XXXX-XXXX`",
            parse_mode='Markdown',
            reply_markup=kb_back_to_admin()
        )
    elif query.data == 'admin_extract':
        keys = redis_client.keys("snappfood:license:*")
        if not keys:
            await query.answer("⚠️ دیتابیس خالی است!", show_alert=True)
            return
        await query.answer("درحال آماده‌سازی بکاپ...")
        records_by_type = {"raw": [], "old": []}
        for k in keys:
            try:
                raw = redis_client.get(k)
                data = json.loads(raw) if raw else {}
                records_by_type[get_account_type(data)].append((k, data))
            except Exception:
                records_by_type["raw"].append((k, {}))

        lines = [
            "فایل بکاپ (لایسنس‌ها)",
            f"تاریخ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "-" * 40,
            ""
        ]
        for account_type, title in (("raw", "اکانت‌های جدید/خام (raw)"), ("old", "اکانت‌های قدیمی (old)")):
            records = records_by_type[account_type]
            lines.extend([
                f"########## {title} ##########",
                f"تعداد: {len(records)}",
                ""
            ])
            for k, data in records:
                lines.append(f"لایسنس:          {data.get('license_key', k.split(':')[-1])}")
                lines.append(f"شماره موبایل:    {data.get('phone_number', 'نامشخص')}")
                lines.append(f"آخرین بروزرسانی: {data.get('updated_at', 'نامشخص')}")
                lines.append("-" * 40)
            lines.append("")
        doc = io.BytesIO("\n".join(lines).encode('utf-8'))
        doc.name = f"Backup_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
        await query.message.reply_document(doc, caption=f"📥  *فایل بکاپ*\n📊  رکوردها: `{len(keys)}`", parse_mode='Markdown')
    elif query.data == 'admin_extract_tokens':
        keys = redis_client.keys("snappfood:license:*")
        if not keys:
            await query.answer("⚠️ دیتابیس خالی است!", show_alert=True)
            return
        await query.answer("درحال آماده‌سازی گزارش...")
        records_by_type = {"raw": [], "old": []}
        for k in keys:
            try:
                raw = redis_client.get(k)
                data = json.loads(raw) if raw else {}
                records_by_type[get_account_type(data)].append((k, data))
            except Exception:
                records_by_type["raw"].append((k, {}))

        lines = [
            "گزارش ارتباطات سیستمی",
            f"تاریخ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "=" * 60,
            ""
        ]
        for account_type, title in (("raw", "اکانت‌های جدید/خام (raw)"), ("old", "اکانت‌های قدیمی (old)")):
            records = records_by_type[account_type]
            lines.extend([
                f"########## {title} ##########",
                f"تعداد: {len(records)}",
                ""
            ])
            for k, data in records:
                lines.append(f"لایسنس:        {data.get('license_key', k.split(':')[-1])}")
                lines.append(f"شماره موبایل:  {data.get('phone_number', 'نامشخص')}")
                lines.append(f"Access Token:  {data.get('access_token', 'ندارد')}")
                lines.append(f"Refresh Token: {data.get('refresh_token', 'ندارد')}")
                lines.append(f"آخرین بروزرسانی: {data.get('updated_at', 'نامشخص')}")
                lines.append("-" * 60)
            lines.append("")
        doc = io.BytesIO("\n".join(lines).encode('utf-8'))
        doc.name = f"System_Report_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
        await query.message.reply_document(doc, caption=f"🔑  *گزارش کامل ارتباطات*\n📊  رکوردها: `{len(keys)}`\n\n⚠️ مراقب این فایل باشید.", parse_mode='Markdown')
    elif query.data == 'admin_rebuild':
        await query.answer("عملیات بازسازی شروع شد...")
        await query.edit_message_text(
            "🔄  *عملیات بازسازی در پس‌زمینه آغاز شد...*\n\nبه محض پایان نتیجه ارسال می‌شود.",
            parse_mode='Markdown'
        )
        asyncio.ensure_future(process_database_rebuild(query.message.chat_id, context.bot))
    elif query.data in {'admin_discount_check_raw', 'admin_discount_check_old'}:
        account_type = "old" if query.data.endswith("_old") else "raw"
        title = "خطوط قدیمی" if account_type == "old" else "خطوط خام"
        await query.answer("چکر تخفیف شروع شد.")
        await query.edit_message_text(f"🎁  *بررسی تخفیف {title} فعال شد*\nپس از پایان فایل گزارش ارسال خواهد شد.", parse_mode='Markdown')
        asyncio.ensure_future(process_discount_check(query.message.chat_id, context.bot, account_type))
    elif query.data == 'admin_delete_hint':
        await query.answer()
        await query.message.reply_text("🗑  *حذف لایسنس:*\n\n`/delete BARANLINK-XXXX-XXXX`", parse_mode='Markdown')
    elif query.data == 'admin_back':
        await query.answer()
        db_status = "🟢 متصل" if redis_client else "🔴 قطع"
        stats = get_database_account_stats()
        text = f"━━━━━━━━━━━━━━━━━━━━━━\n⚙️  *پنل مدیریت*\n━━━━━━━━━━━━━━━━━━━━━━\n\n🗄  وضعیت دیتابیس: {db_status}\n📊  مجموع رکوردها: `{stats['total']}`\n🟠  اکانت‌های خام: `{stats['raw']}`\n🔵  اکانت‌های قدیمی: `{stats['old']}`\n\nیک گزینه را انتخاب کنید:"
        await query.edit_message_text(text, reply_markup=kb_admin_main(), parse_mode='Markdown')

# ======================== اجرای همزمان ربات + وب‌سرور ========================
async def run_bot():
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("❌ TELEGRAM_BOT_TOKEN تنظیم نشده است!")
        return
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    old_license_conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(old_license_start, pattern='^admin_old_license$')
        ],
        states={
            OLD_ASK_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, old_ask_phone),
                CallbackQueryHandler(cancel_action, pattern='^cancel$')
            ],
            OLD_ASK_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, old_ask_code),
                CallbackQueryHandler(old_resend_code_callback, pattern='^old_resend_code$'),
                CallbackQueryHandler(cancel_action, pattern='^cancel$')
            ]
        },
        fallbacks=[
            CommandHandler("cancel", cancel_action),
            CommandHandler("start", start),
            CallbackQueryHandler(exit_to_admin, pattern='^admin_open$')
        ],
        allow_reentry=True,
    )
    application.add_handler(old_license_conv_handler)

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(start_raw_license_callback, pattern='^admin_new_license$')
        ],
        states={
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone), CallbackQueryHandler(cancel_action, pattern='^cancel$')],
            ASK_CODE_STEP_1: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_code_step_1), CallbackQueryHandler(resend_code_1_callback, pattern='^resend_code_1$'), CallbackQueryHandler(cancel_action, pattern='^cancel$')],
            ASK_CODE_STEP_2: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_code_step_2), CallbackQueryHandler(resend_code_2_callback, pattern='^resend_code_2$'), CallbackQueryHandler(cancel_action, pattern='^cancel$')],
            ASK_NEXT_ACTION: [CallbackQueryHandler(next_line_callback, pattern='^next_line$'), CallbackQueryHandler(finish_session_callback, pattern='^finish_session$'), CallbackQueryHandler(cancel_action, pattern='^cancel$')]
        },
        fallbacks=[CommandHandler("cancel", cancel_action), CommandHandler("start", start), CallbackQueryHandler(exit_to_admin, pattern='^admin_open$')],
        allow_reentry=True,
    )
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("delete", delete_number))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(admin_callbacks, pattern="^admin_"))
    logger.info("🤖 ربات در حال راه‌اندازی...")
    await application.initialize()
    await application.start()
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("🤖 ربات با موفقیت راه‌اندازی شد.")
    await asyncio.Event().wait()

async def run_webserver():
    config = uvicorn.Config(app=app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    logger.info(f"🌐 وب‌سرور روی پورت {PORT} در حال راه‌اندازی...")
    await server.serve()

async def main():
    await asyncio.gather(run_bot(), run_webserver())

if __name__ == "__main__":
    asyncio.run(main())
