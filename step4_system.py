import sys
import time
import sqlite3
import threading
import random
from datetime import datetime, timedelta
import requests

# تنظیم خروجی ترمینال برای نمایش صحیح فارسی
sys.stdout.reconfigure(encoding='utf-8')

# ================= تنظیمات اختصاصی شما =================
BOT_TOKEN = "8909840239:AAETYsWYRS54el4oyl1yUi20V2ge3uCE9_o"
CHANNEL_ID = "@Kanseli_Ghatar"
BOT_USERNAME = "BilitKanseliBot"
ADMIN_SUPPORT_ID = "@tahoori"
SUBSCRIPTION_PRICE = "۱۴۰,۰۰۰ تومان"
CHECK_INTERVAL = 90                                 # فاصله زمانی اسکن رادار (ثانیه)
COOLDOWN_MINUTES = 15                               # جلوگیری از ارسال تکراری در کانال
# =======================================================

DB_FILE = "radar_data.db"
TG_BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
MRBILIT_API = "https://train.mrbilit.com/api/GetAvailable/v2"
MRBILIT_STATIONS_API = "https://train.mrbilit.com/api/Stations"

# دو سشن مجزا: یکی با پروکسی/VPN ویندوز برای تلگرام، یکی مستقیم برای سایت داخلی مستربلیط
tg_session = requests.Session()
ir_session = requests.Session()
ir_session.trust_env = False

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://mrbilit.com/"
}

# لیست شهرهای پرتردد ریلی (کدهای پایه)
CITIES = {
    1: ("تهران", "tehran"),
    191: ("مشهد", "mashhad"),
    163: ("قم", "qom"),
    25: ("اصفهان", "isfahan"),
    129: ("شیراز", "shiraz"),
    56: ("تبریز", "tabriz"),
    31: ("اهواز", "ahvaz"),
    249: ("یزد", "yazd"),
    37: ("بندرعباس", "bandarabbas"),
    171: ("کرمان", "kerman"),
    110: ("ساری", "sari"),
    98: ("رشت", "rasht"),
    168: ("کرج", "karaj"),
    161: ("قزوین", "qazvin"),
    106: ("زنجان", "zanjan"),
    122: ("شاهرود", "shahrood")
}

def sync_station_codes():
    """دریافت کدهای دقیق ایستگاه‌ها از سرور مستربلیط در صورت در دسترس بودن"""
    try:
        r = ir_session.get(MRBILIT_STATIONS_API, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            data = r.json()
            stations = data if isinstance(data, list) else data.get("stations", data.get("data", []))
            name_to_slug = {v[0]: v[1] for v in CITIES.values()}
            updated = {}
            for st in stations:
                st_id = st.get("id") or st.get("code") or st.get("stationCode")
                st_name = (st.get("name") or st.get("title") or st.get("persianName") or "").strip()
                st_en = (st.get("englishName") or st.get("enName") or st.get("slug") or "").strip().lower().replace(" ", "-")
                if st_id and st_name in name_to_slug:
                    updated[int(st_id)] = (st_name, st_en if st_en else name_to_slug[st_name])
            if len(updated) >= 5:
                CITIES.clear()
                CITIES.update(updated)
                print(f"✅ کد {len(CITIES)} ایستگاه راه‌آهن با سرور مستربلیط همگام‌سازی شد.")
    except Exception:
        pass

# حافظه وضعیت قطارها
previous_capacities = {}
last_alert_time = {}
active_channel_messages = {}


# ----------------- ۱. توابع تاریخ شمسی -----------------
def gregorian_to_jalali(gy, gm, gd):
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + (365 * gy) + ((gy2 + 3) // 4) - ((gy2 + 99) // 100) + ((gy2 + 399) // 400) + gd + g_d_m[gm - 1]
    jy = -1595 + (33 * (days // 12053))
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + (days // 31)
        jd = 1 + (days % 31)
    else:
        jm = 7 + ((days - 186) // 30)
        jd = 1 + ((days - 186) % 30)
    return jy, jm, jd

JALALI_MONTHS = ["", "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
WEEKDAYS_FA = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنج‌شنبه", "جمعه", "شنبه", "یکشنبه"]

def get_next_7_days():
    days_list = []
    today = datetime.now()
    for i in range(7):
        dt = today + timedelta(days=i)
        jy, jm, jd = gregorian_to_jalali(dt.year, dt.month, dt.day)
        w_name = WEEKDAYS_FA[dt.weekday()]
        label = f"{w_name} {jd} {JALALI_MONTHS[jm]}"
        greg_str = dt.strftime("%Y-%m-%d")
        days_list.append((greg_str, label))
    return days_list


# ----------------- ۲. دیتابیس SQLite -----------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            invited_by INTEGER DEFAULT 0,
            invites_count INTEGER DEFAULT 0,
            is_vip INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS watches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            from_code INTEGER,
            to_code INTEGER,
            travel_date TEXT,
            date_label TEXT,
            corporation TEXT DEFAULT 'ALL',
            created_at TEXT
        )
    """)
    cols = [row[1] for row in c.execute("PRAGMA table_info(watches)").fetchall()]
    if "corporation" not in cols:
        c.execute("ALTER TABLE watches ADD COLUMN corporation TEXT DEFAULT 'ALL'")
    conn.commit()
    conn.close()

def register_user(user_id, first_name, ref_id=0):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    exists = c.fetchone()
    
    inviter_upgraded = False
    inviter_new_count = 0
    
    if not exists:
        valid_ref = ref_id if (ref_id and ref_id != user_id) else 0
        c.execute(
            "INSERT INTO users (user_id, first_name, invited_by, invites_count, is_vip, created_at) VALUES (?, ?, ?, 0, 0, ?)",
            (user_id, first_name, valid_ref, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        if valid_ref > 0:
            c.execute("UPDATE users SET invites_count = invites_count + 1 WHERE user_id = ?", (valid_ref,))
            c.execute("SELECT invites_count, is_vip FROM users WHERE user_id = ?", (valid_ref,))
            row = c.fetchone()
            if row:
                inviter_new_count = row[0]
                if inviter_new_count >= 3 and row[1] == 0:
                    c.execute("UPDATE users SET is_vip = 1 WHERE user_id = ?", (valid_ref,))
                    inviter_upgraded = True
    conn.commit()
    conn.close()
    return (not exists and ref_id > 0 and ref_id != user_id), inviter_new_count, inviter_upgraded

def get_user_info(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT invites_count, is_vip FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row if row else (0, 0)

def get_city_name(code):
    return CITIES.get(int(code), (f"ایستگاه {code}", "tehran"))[0]

def get_city_slug(code):
    return CITIES.get(int(code), (f"ایستگاه {code}", "tehran"))[1]


# ----------------- ۳. توابع ارتباط با تلگرام و کیبوردها -----------------
def tg_send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = tg_session.post(f"{TG_BASE_URL}/sendMessage", json=payload, timeout=15)
        return r.json()
    except Exception as e:
        print(f"⚠️ خطای شبکه در ارسال پیام تلگرام: {e}")
        return {}

def tg_edit_message(chat_id, message_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        tg_session.post(f"{TG_BASE_URL}/editMessageText", json=payload, timeout=15)
    except Exception:
        pass

def tg_answer_callback(callback_id, text=""):
    try:
        tg_session.post(f"{TG_BASE_URL}/answerCallbackQuery", json={"callback_query_id": callback_id, "text": text}, timeout=10)
    except Exception:
        pass

def main_menu_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "🔍 انتخاب دلخواه شهر مبدأ و مقصد", "callback_data": "choose_origin"}
            ],
            [
                {"text": "⚡ سریع: تهران ➔ مشهد", "callback_data": "selroute_1_191"},
                {"text": "⚡ سریع: مشهد ➔ تهران", "callback_data": "selroute_191_1"}
            ],
            [
                {"text": "📋 لیست پیگیری‌های من", "callback_data": "my_watches"},
                {"text": "🎁 اشتراک رایگان (دعوت دوستان)", "callback_data": "free_vip"}
            ],
            [
                {"text": "💎 فعال‌سازی اشتراک ویژه (VIP)", "callback_data": "buy_vip"}
            ]
        ]
    }

def build_cities_keyboard(prefix, exclude_code=None):
    """ساخت کیبورد ۲ ستونی از تمام شهرها برای انتخاب مبدأ یا مقصد"""
    kb = []
    row = []
    for code, (name, _) in CITIES.items():
        if exclude_code is not None and int(code) == int(exclude_code):
            continue
        row.append({"text": f"📍 {name}", "callback_data": f"{prefix}_{code}"})
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([{"text": "🔙 بازگشت به منوی اصلی", "callback_data": "main_menu"}])
    return {"inline_keyboard": kb}


# ----------------- ۴. منوی ربات و دریافت پیام‌ها -----------------
def bot_polling_worker():
    print("🤖 بخش منوی مسافران فعال شد و در حال تست اتصال به تلگرام است...")
    
    try:
        tg_session.get(f"{TG_BASE_URL}/deleteWebhook?drop_pending_updates=True", timeout=10)
        me_resp = tg_session.get(f"{TG_BASE_URL}/getMe", timeout=10).json()
        if me_resp.get("ok"):
            print(f"✅ اتصال به تلگرام کاملاً برقرار است! ربات فعال: @{me_resp['result'].get('username')}")
        else:
            print(f"❌ توکن ربات مشکل دارد: {me_resp}")
    except Exception as e:
        print(f"⚠️ هشدار اولیه اتصال به تلگرام (فیلترشکن را بررسی کنید): {e}")

    offset = 0
    while True:
        try:
            r = tg_session.get(f"{TG_BASE_URL}/getUpdates", params={"offset": offset, "timeout": 20}, timeout=25)
            data = r.json()
            if not data.get("ok"):
                time.sleep(3)
                continue
                
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                
                if "message" in upd:
                    msg = upd["message"]
                    chat_id = msg["chat"]["id"]
                    first_name = msg.get("from", {}).get("first_name", "مسافر")
                    text = msg.get("text", "").strip()
                    print(f"📩 پیام از {first_name} ({chat_id}): {text}")
                    
                    if text.startswith("/start"):
                        parts = text.split()
                        ref_id = 0
                        if len(parts) > 1 and parts[1].startswith("ref_"):
                            try:
                                ref_id = int(parts[1].replace("ref_", ""))
                            except ValueError:
                                ref_id = 0
                                
                        is_new_ref, new_count, upgraded = register_user(chat_id, first_name, ref_id)
                        
                        if is_new_ref:
                            if upgraded:
                                tg_send_message(ref_id, "🎉 <b>تبریک! سومین دوست شما وارد ربات شد و حساب شما به صورت خودکار به «VIP ویژه» ارتقا یافت!</b>")
                            else:
                                tg_send_message(ref_id, f"👏 یک نفر با لینک دعوت شما وارد ربات شد!\n📊 تعداد دعوت‌های شما: <b>{new_count} از 3 نفر</b>")
                        
                        invites, is_vip = get_user_info(chat_id)
                        status_badge = "💎 ویژه (VIP - ارسال در ثانیه اول)" if is_vip else "⚪ عادی (با تأخیر)"
                        
                        welcome = (
                            f"سلام <b>{first_name}</b> عزیز؛ به <b>رادار هوشمند بلیط کنسلی قطار</b> خوش آمدید! 🚆\n\n"
                            f"این ربات ظرفیت تمام قطارهای تکمیل‌شده را به صورت ۲۴ ساعته رصد می‌کند و به محض باز شدن صندلی کنسلی، لینک خرید مستقیم را برایتان می‌فرستد.\n\n"
                            f"👤 وضعیت حساب شما: <b>{status_badge}</b>\n\n"
                            f"👇 لطفاً از منوی زیر مبدأ و مقصد خود را انتخاب کنید:"
                        )
                        tg_send_message(chat_id, welcome, main_menu_keyboard())
                        
                    elif text == "/stats":
                        conn = sqlite3.connect(DB_FILE)
                        c = conn.cursor()
                        u = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                        v = c.execute("SELECT COUNT(*) FROM users WHERE is_vip=1").fetchone()[0]
                        w = c.execute("SELECT COUNT(*) FROM watches").fetchone()[0]
                        conn.close()
                        tg_send_message(chat_id, f"📊 <b>آمار لحظه‌ای سیستم:</b>\n👥 کل کاربران: {u}\n💎 کاربران VIP: {v}\n🔍 مسیرهای فعال: {w}")
                        
                    elif text.startswith("/vip"):
                        parts = text.split()
                        target_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else chat_id
                        conn = sqlite3.connect(DB_FILE)
                        register_user(target_id, first_name, 0)
                        conn.execute("UPDATE users SET is_vip = 1 WHERE user_id = ?", (target_id,))
                        conn.commit()
                        conn.close()
                        tg_send_message(chat_id, f"✅ حساب کاربری <code>{target_id}</code> با موفقیت به VIP ارتقا یافت.")
                        if target_id != chat_id:
                            tg_send_message(target_id, "💎 <b>حساب شما به نسخه ویژه (VIP) ارتقا یافت!</b>")
                    else:
                        tg_send_message(chat_id, "👇 لطفاً از دکمه‌های منوی زیر استفاده کنید:", main_menu_keyboard())
                        
                elif "callback_query" in upd:
                    cb = upd["callback_query"]
                    cb_id = cb["id"]
                    chat_id = cb["message"]["chat"]["id"]
                    data_str = cb.get("data", "")
                    print(f"🔘 کلیک دکمه توسط {chat_id}: {data_str}")
                    
                    tg_answer_callback(cb_id)
                    
                    if data_str == "main_menu":
                        tg_send_message(chat_id, "👇 مسیر یا بخش موردنظر را انتخاب کنید:", main_menu_keyboard())
                        
                    elif data_str == "choose_origin":
                        kb = build_cities_keyboard("orig")
                        tg_send_message(chat_id, "🛫 <b>گام اول:</b> لطفاً <b>شهر مبدأ</b> حرکت خود را از لیست زیر انتخاب کنید:", kb)
                        
                    elif data_str.startswith("orig_"):
                        f_code = int(data_str.split("_")[1])
                        f_name = get_city_name(f_code)
                        kb = build_cities_keyboard(f"selroute_{f_code}", exclude_code=f_code)
                        tg_send_message(
                            chat_id,
                            f"🛫 شهر مبدأ: <b>{f_name}</b>\n\n🛬 <b>گام دوم:</b> حالا لطفاً <b>شهر مقصد</b> خود را انتخاب کنید:",
                            kb
                        )
                        
                    elif data_str.startswith("selroute_"):
                        _, f_code, t_code = data_str.split("_")
                        f_name, t_name = get_city_name(f_code), get_city_name(t_code)
                        
                        days = get_next_7_days()
                        kb = []
                        for greg_d, jalali_lbl in days:
                            kb.append([{"text": f"📅 {jalali_lbl}", "callback_data": f"seldate_{f_code}_{t_code}_{greg_d}_{jalali_lbl}"}])
                        kb.append([{"text": "🔄 تغییر شهر مبدأ و مقصد", "callback_data": "choose_origin"}])
                        kb.append([{"text": "🔙 بازگشت به منوی اصلی", "callback_data": "main_menu"}])
                        
                        tg_send_message(
                            chat_id,
                            f"🚆 مسیر انتخابی: <b>{f_name} ➔ {t_name}</b>\n\n📅 <b>گام سوم:</b> تاریخ حرکت را انتخاب کنید:",
                            {"inline_keyboard": kb}
                        )
                        
                    elif data_str.startswith("seldate_"):
                        parts = data_str.split("_", 4)
                        f_code, t_code, greg_d, jalali_lbl = parts[1], parts[2], parts[3], parts[4]
                        f_name, t_name = get_city_name(f_code), get_city_name(t_code)
                        
                        kb = [
                            [{"text": "🚆 همه قطارها و شرکت‌ها (بدون محدودیت)", "callback_data": f"addwatch_{f_code}_{t_code}_{greg_d}_{jalali_lbl}_ALL"}],
                            [{"text": "✨ فقط قطارهای ۵ ستاره (فدک، نور، زندگی)", "callback_data": f"addwatch_{f_code}_{t_code}_{greg_d}_{jalali_lbl}_LUXURY"}],
                            [{"text": "🔙 بازگشت به انتخاب تاریخ", "callback_data": f"selroute_{f_code}_{t_code}"}]
                        ]
                        tg_send_message(
                            chat_id,
                            f"🚆 مسیر: <b>{f_name} ➔ {t_name}</b>\n📅 تاریخ: <b>{jalali_lbl}</b>\n\n🛋 <b>گام چهارم:</b> کدام دسته از قطارها را می‌خواهید رصد کنید؟",
                            {"inline_keyboard": kb}
                        )
                        
                    elif data_str.startswith("addwatch_"):
                        parts = data_str.split("_", 5)
                        f_code, t_code, greg_d, jalali_lbl, corp_type = int(parts[1]), int(parts[2]), parts[3], parts[4], parts[5]
                        f_name, t_name = get_city_name(f_code), get_city_name(t_code)
                        
                        register_user(chat_id, "مسافر", 0)
                        _, is_vip = get_user_info(chat_id)
                        
                        conn = sqlite3.connect(DB_FILE)
                        c = conn.cursor()
                        existing_count = c.execute("SELECT COUNT(*) FROM watches WHERE user_id = ?", (chat_id,)).fetchone()[0]
                        
                        max_allowed = 5 if is_vip else 1
                        dup = c.execute("SELECT id FROM watches WHERE user_id=? AND from_code=? AND to_code=? AND travel_date=?", (chat_id, f_code, t_code, greg_d)).fetchone()
                        
                        if not dup and existing_count >= max_allowed:
                            conn.close()
                            tg_send_message(chat_id, f"⚠️ شما در حساب فعلی امکان ثبت حداکثر <b>{max_allowed} مسیر هم‌زمان</b> را دارید.\nابتدا از بخش «لیست پیگیری‌های من» مسیر قبلی را حذف کنید یا حساب خود را VIP نمایید.", main_menu_keyboard())
                            continue
                            
                        if dup:
                            c.execute("UPDATE watches SET corporation=? WHERE id=?", (corp_type, dup[0]))
                        else:
                            c.execute(
                                "INSERT INTO watches (user_id, from_code, to_code, travel_date, date_label, corporation, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (chat_id, f_code, t_code, greg_d, jalali_lbl, corp_type, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                            )
                        conn.commit()
                        conn.close()
                        
                        corp_desc = "فقط قطارهای ۵ ستاره لوکس" if corp_type == "LUXURY" else "همه قطارها و شرکت‌ها"
                        confirm_txt = (
                            f"✅ <b>رادار شما با موفقیت قفل شد!</b>\n\n"
                            f"🚆 مسیر: <b>{f_name} به {t_name}</b>\n"
                            f"📅 تاریخ: <b>{jalali_lbl}</b>\n"
                            f"🛋 فیلتر قطار: <b>{corp_desc}</b>\n\n"
                            f"به محض باز شدن صندلی کنسلی در این مسیر، ربات در همین‌جا به شما اطلاع می‌دهد."
                        )
                        tg_send_message(chat_id, confirm_txt, main_menu_keyboard())
                        
                    elif data_str == "my_watches":
                        conn = sqlite3.connect(DB_FILE)
                        c = conn.cursor()
                        rows = c.execute("SELECT id, from_code, to_code, date_label, corporation FROM watches WHERE user_id = ?", (chat_id,)).fetchall()
                        conn.close()
                        
                        if not rows:
                            tg_send_message(chat_id, "📭 شما در حال حاضر هیچ مسیر فعالی ثبت نکرده‌اید.", main_menu_keyboard())
                        else:
                            txt = "📋 <b>مسیرهای در حال پیگیری شما:</b>\n\n"
                            kb = []
                            for w_id, fc, tc, d_lbl, corp in rows:
                                fn, tn = get_city_name(fc), get_city_name(tc)
                                c_desc = " (فقط ۵ ستاره)" if corp == "LUXURY" else " (همه قطارها)"
                                txt += f"🔹 {fn} به {tn} | 📅 {d_lbl}{c_desc}\n"
                                kb.append([{"text": f"🗑 حذف: {fn}-{tn} ({d_lbl})", "callback_data": f"delwatch_{w_id}"}])
                            kb.append([{"text": "➕ افزودن مسیر جدید", "callback_data": "choose_origin"}])
                            kb.append([{"text": "🔙 بازگشت به منوی اصلی", "callback_data": "main_menu"}])
                            tg_send_message(chat_id, txt, {"inline_keyboard": kb})
                            
                    elif data_str.startswith("delwatch_"):
                        w_id = int(data_str.split("_")[1])
                        conn = sqlite3.connect(DB_FILE)
                        conn.execute("DELETE FROM watches WHERE id = ? AND user_id = ?", (w_id, chat_id))
                        conn.commit()
                        conn.close()
                        tg_send_message(chat_id, "🗑 مسیر موردنظر از لیست رادار شما حذف شد.", main_menu_keyboard())
                        
                    elif data_str == "free_vip":
                        invites, _ = get_user_info(chat_id)
                        ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{chat_id}"
                        free_txt = (
                            f"🎁 <b>دریافت اشتراک VIP کاملاً رایگان!</b>\n\n"
                            f"کافی است لینک اختصاصی زیر را برای <b>۳ نفر از دوستان یا گروه‌های مسافرتی</b> بفرستید. به محض اینکه ۳ نفر ربات را استارت بزنند، حساب شما خودکار VIP می‌شود!\n\n"
                            f"📊 دعوت‌های فعلی شما: <b>{invites} از 3 نفر</b>\n\n"
                            f"🔗 <b>لینک اختصاصی شما (بزنید روی لینک تا کپی شود):</b>\n"
                            f"<code>{ref_link}</code>"
                        )
                        tg_send_message(chat_id, free_txt, main_menu_keyboard())
                        
                    elif data_str == "buy_vip":
                        vip_txt = (
                            f"💎 <b>فعال‌سازی فوری اشتراک ویژه (VIP)</b>\n\n"
                            f"✅ ارسال هشدار باز شدن بلیط در <b>ثانیه اول</b> به پی‌وی شما\n"
                            f"✅ امکان رصد هم‌زمان تا <b>۵ تاریخ و مسیر مختلف در کل کشور</b>\n"
                            f"✅ فیلتر اختصاصی قطارهای ۵ ستاره\n\n"
                            f"💳 <b>هزینه اشتراک ۱۵ روزه:</b> {SUBSCRIPTION_PRICE}\n"
                            f"🆔 <b>کد کاربری شما:</b> <code>{chat_id}</code>\n\n"
                            f"👈 برای فعال‌سازی آنی، کد کاربری بالا را به آیدی پشتیبانی ارسال فرمایید:\n"
                            f"👨‍💻 پشتیبانی: {ADMIN_SUPPORT_ID}"
                        )
                        tg_send_message(chat_id, vip_txt, main_menu_keyboard())
                        
        except Exception as e:
            print(f"⚠️ خطای ارتباط با تلگرام (بررسی فیلترشکن): {e}")
            time.sleep(5)


# ----------------- ۵. موتور رادار و فیلتر نوع قطار -----------------
def fetch_route_trains(from_code, to_code, greg_date):
    params = {
        "from": from_code, "to": to_code, "date": greg_date,
        "genderCode": 3, "adultCount": 1, "childCount": 0, "infantCount": 0,
        "exclusive": "false", "availableStatus": "Both"
    }
    r = ir_session.get(MRBILIT_API, params=params, headers=HEADERS, timeout=15)
    r.raise_for_status()
    data = r.json()
    
    results = []
    for train in data.get("trains", []):
        train_num = train.get("trainNumber")
        corp_name = train.get("corporationName", "")
        dep_time = train.get("departureTime", "")[-8:-3]
        date_str = train.get("dateString", "")
        weekday = train.get("weekday", "")
        from_name = train.get("fromName") or get_city_name(from_code)
        to_name = train.get("toName") or get_city_name(to_code)
        
        for pg in train.get("prices", []):
            for cls in pg.get("classes", []):
                u_key = f"{train_num}_{cls.get('wagonID')}_{cls.get('id')}"
                wagon_name = cls.get("wagonName", "").strip()
                capacity = int(cls.get("capacity", 0))
                raw_price = int(cls.get("price", 0)) or int(cls.get("discount", 0))
                
                full_title = f"{corp_name} {wagon_name}"
                is_luxury = any(x in full_title for x in ["5 ستاره", "پنج ستاره", "فدک", "زندگی", "نورالرضا", "نور"])
                
                results.append({
                    "key": u_key,
                    "from_code": from_code, "to_code": to_code, "greg_date": greg_date,
                    "train_num": train_num, "corp_name": corp_name, "wagon_name": wagon_name,
                    "dep_time": dep_time, "date_str": date_str, "weekday": weekday,
                    "from_name": from_name, "to_name": to_name,
                    "capacity": capacity, "price_toman": raw_price // 10,
                    "is_luxury": is_luxury
                })
    return results

def build_alert_message(info, closed=False):
    f_slug, t_slug = get_city_slug(info["from_code"]), get_city_slug(info["to_code"])
    buy_url = f"https://mrbilit.com/trains/{f_slug}-{t_slug}?departureDate={info['greg_date']}"
    bot_url = f"https://t.me/{BOT_USERNAME}"
    
    if not closed:
        text = (
            f"🚨 <b>شکار ظرفیت کنسلی قطار ({info['from_name']} به {info['to_name']})</b>\n\n"
            f"🚆 <b>قطار:</b> شماره {info['train_num']} ({info['corp_name']})\n"
            f"🛋 <b>نوع واگن:</b> {info['wagon_name']}\n"
            f"📅 <b>تاریخ حرکت:</b> {info['weekday']} {info['date_str']}\n"
            f"⏰ <b>ساعت حرکت:</b> {info['dep_time']}\n"
            f"💺 <b>ظرفیت بازشده:</b> <b>{info['capacity']} صندلی خالی</b>\n"
            f"💳 <b>قیمت بلیط:</b> {info['price_toman']:,} تومان\n\n"
            f"⚡ <i>این پیام ابتدا برای اعضای VIP ربات ارسال گردید.</i>"
        )
        kb = {
            "inline_keyboard": [
                [{"text": "🛒 ورود مستقیم به صفحه خرید بلیط", "url": buy_url}],
                [{"text": "🔔 ثبت مسیر دلخواه در ربات رادار", "url": bot_url}]
            ]
        }
    else:
        text = (
            f"❌ <b>[بسته شد] ظرفیت کنسلی قطار ({info['from_name']} به {info['to_name']})</b>\n\n"
            f"🚆 <b>قطار:</b> شماره {info['train_num']} ({info['corp_name']} - {info['wagon_name']})\n"
            f"📅 <b>تاریخ حرکت:</b> {info['weekday']} {info['date_str']} | ساعت {info['dep_time']}\n\n"
            f"⚠️ <b>به‌روزرسانی:</b> صندلی‌های بازشده این قطار رزرو و ظرفیت مجدداً تکمیل شد!\n"
            f"💎 <i>برای دریافت هشدار در ثانیه اول باز شدن ظرفیت، مسیر خود را در ربات ثبت کنید:</i>"
        )
        kb = {"inline_keyboard": [[{"text": "🤖 ثبت مسیر در ربات", "url": bot_url}]]}
    return text, kb

def notify_watching_users(info):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    rows = c.execute("""
        SELECT w.user_id, u.is_vip, w.corporation 
        FROM watches w
        JOIN users u ON w.user_id = u.user_id
        WHERE w.from_code = ? AND w.to_code = ? AND w.travel_date = ?
    """, (info["from_code"], info["to_code"], info["greg_date"])).fetchall()
    conn.close()
    
    f_slug, t_slug = get_city_slug(info["from_code"]), get_city_slug(info["to_code"])
    buy_url = f"https://mrbilit.com/trains/{f_slug}-{t_slug}?departureDate={info['greg_date']}"
    
    for uid, is_vip, corp_pref in rows:
        if corp_pref == "LUXURY" and not info["is_luxury"]:
            continue
            
        if is_vip:
            pv_text = (
                f"🎯 <b>هشدار اختصاصی VIP (ثانیه اول)!</b>\n\n"
                f"ظرفیت مسیر انتخابی شما <b>({info['from_name']} به {info['to_name']} - {info['date_str']})</b> همین الان باز شد:\n\n"
                f"🚆 قطار {info['train_num']} ({info['corp_name']} - {info['wagon_name']})\n"
                f"⏰ ساعت حرکت: <b>{info['dep_time']}</b>\n"
                f"💺 ظرفیت خالی: <b>{info['capacity']} صندلی</b>\n"
                f"💳 قیمت: {info['price_toman']:,} تومان\n\n"
                f"👇 همین ثانیه قبل از پر شدن کلیک کنید:"
            )
            tg_send_message(uid, pv_text, {"inline_keyboard": [[{"text": "🛒 خرید فوری از سایت رسمی", "url": buy_url}]]})
        else:
            fomo_pv = (
                f"🔔 <b>یک ظرفیت کنسلی در مسیر انتخابی شما ({info['from_name']} به {info['to_name']} - {info['date_str']}) باز شد!</b>\n\n"
                f"⚠️ از آنجا که حساب شما «عادی» است، لینک مستقیم خرید در ثانیه اول فقط برای <b>اعضای VIP</b> ارسال گردید."
            )
            tg_send_message(uid, fomo_pv, main_menu_keyboard())

def radar_scanner_worker():
    print("🚀 موتور رادار سراسری با قابلیت انتخاب مبدأ/مقصد و سیستم ضدبلاک ۴۲۹ فعال شد...")
    while True:
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            watched_routes = c.execute("SELECT DISTINCT from_code, to_code, travel_date FROM watches").fetchall()
            conn.close()
            
            tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            default_route = (1, 191, tomorrow_str)
            if default_route not in watched_routes:
                watched_routes.append(default_route)
                
            for f_code, t_code, g_date in watched_routes:
                route_key = f"{f_code}_{t_code}_{g_date}"
                now_dt = datetime.now()
                now_str = now_dt.strftime("%H:%M:%S")
                
                try:
                    time.sleep(random.uniform(5.0, 9.0))
                    trains = fetch_route_trains(f_code, t_code, g_date)
                except requests.exceptions.HTTPError as he:
                    if he.response.status_code == 429:
                        print(f"[{now_str}] ⏳ هشدار ۴۲۹ مستربلیط؛ استراحت ۶۰ ثانیه‌ای...")
                        time.sleep(60)
                        continue
                    else:
                        print(f"[{now_str}] ⚠️ خطای HTTP در مسیر {route_key}: {he}")
                        continue
                except Exception as e:
                    print(f"[{now_str}] ⚠️ خطا در بررسی مسیر {route_key}: {e}")
                    continue
                    
                for item in trains:
                    pair_key = (route_key, item["key"])
                    new_cap = item["capacity"]
                    
                    if pair_key not in previous_capacities:
                        previous_capacities[pair_key] = new_cap
                        continue
                        
                    old_cap = previous_capacities[pair_key]
                    
                    if old_cap == 0 and new_cap > 0:
                        print(f"🎯 [{now_str}] شکار کنسلی! ({item['from_name']}-{item['to_name']}) قطار {item['train_num']} ({item['date_str']}) -> {new_cap} صندلی")
                        notify_watching_users(item)
                        
                        last_sent = last_alert_time.get(pair_key)
                        if not last_sent or (now_dt - last_sent).total_seconds() >= (COOLDOWN_MINUTES * 60):
                            msg_text, msg_kb = build_alert_message(item, closed=False)
                            res = tg_send_message(CHANNEL_ID, msg_text, msg_kb)
                            if res.get("ok"):
                                active_channel_messages[pair_key] = {"msg_id": res["result"]["message_id"], "info": item}
                                last_alert_time[pair_key] = now_dt
                                
                    elif old_cap > 0 and new_cap == 0:
                        if pair_key in active_channel_messages:
                            saved = active_channel_messages.pop(pair_key)
                            closed_txt, closed_kb = build_alert_message(saved["info"], closed=True)
                            tg_edit_message(CHANNEL_ID, saved["msg_id"], closed_txt, closed_kb)
                            print(f"🔒 [{now_str}] ظرفیت قطار {item['train_num']} بسته شد؛ پیام کانال آپدیت گردید.")
                            
                    previous_capacities[pair_key] = new_cap
                    
        except Exception as e:
            print(f"⚠️ خطای کلی در حلقه رادار: {e}")
            
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    init_db()
    sync_station_codes()
    print("✅ دیتابیس و لیست شهرها آماده شد.")
    t_bot = threading.Thread(target=bot_polling_worker, daemon=True)
    t_bot.start()
    radar_scanner_worker()