# L-Bot Modular

یک ربات تلگرام فارسی‌محور با گفت‌وگوی هوش مصنوعی، حافظه و خلاصه‌سازی، درک تصویر، ترجمه، تولید تصویر، مدیریت کارها و یادآورها، دانلود رسانه، پنل مدیریت، digest روزانه و رلهٔ اختیاری اینستاگرام است.

این پروژه برای Railway و Docker آماده شده، وضعیت پایدار را در SQLite نگه می‌دارد و ورودی قدیمی `python bot.py` را برای استقرارهای قبلی حفظ می‌کند.

## قابلیت‌ها

- گفت‌وگوی استریم‌شوندهٔ L با حافظه، خلاصه‌سازی، محدودکنندهٔ نرخ و ضداسپم.
- ربات اختیاری Light با `TELEGRAM_TOKEN_LIGHT` برای گفت‌وگو و مناظره.
- مسیر اختصاصی vision برای پیام‌های تصویری و ترجمهٔ تصویری `/tr`.
- تولید تصویر با `/pic` و ترجمهٔ متن یا تصویر با `/tr`.
- todo، یادآور تکرارشونده، تعویق/رد یادآور و پارس زمان ایران.
- دریافت لینک اینستاگرام و یوتیوب، انتخاب کیفیت و ارسال فایل‌های بزرگ با userbot اختیاری.
- Inline Mode، پاسخ به inline و Telegram Guest Mode.
- پنل مدیریت برای کاربران، گروه‌ها، بن‌ها، تنظیمات، jobها، broadcast و digest.
- digest خبری روزانه با حالت‌های استاندارد، خاموش و پروفایل سفارشی، بودجهٔ مشترک Tavily و ثبت تحویل.
- پشتیبان‌گیری، پاک‌سازی دوره‌ای، checkpoint کردن WAL، استخراج fact و پروفایل رفتاری در پس‌زمینه.

## پیش‌نیاز و اجرای محلی

- Python 3.13 و وابستگی‌های دقیق `requirements.txt`.
- توکن ربات از [@BotFather](https://t.me/BotFather).
- کلید API سازگار با chat (`CHAT_API_KEY` یا fallback قدیمی `AGNES_API_KEY`).

```bash
git clone <repository-url>
cd <repository-directory>
python3.13 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

حداقل تنظیمات `.env`:

```env
TELEGRAM_TOKEN=your_telegram_bot_token
CHAT_API_KEY=your_chat_api_key
ADMIN_IDS=123456789
```

`bot.py` فقط shim سازگاری است و `main.main()` را اجرا می‌کند. ثبت handlerها، workerهای پس‌زمینه و polling در `main.py` و `wiring.py` انجام می‌شود.

## تنظیمات مهم

فایل `.env.example` قالب عمومی و امن تنظیمات است. نام‌های اصلی:

- `TELEGRAM_TOKEN` و در صورت نیاز `TELEGRAM_TOKEN_LIGHT`
- `CHAT_API_KEY`، `CHAT_API_BASE` و `CHAT_MODEL`
- `VISION_API_KEY`، `VISION_API_BASE` و `VISION_MODEL`
- `IMAGE_API_KEY`، `AGNES_BASE` و `IMAGE_MODEL`
- `ADMIN_IDS` یا `ADMIN_ID`؛ اولین مقدار `ADMIN_IDS` مالک پنل است
- `DATA_DIR`؛ در Railway معمولاً `/data`
- `TAVILY_API_KEY` و `TAVILY_DAILY_LIMIT`
- `DIGEST_HOUR_IRAN` و `DIGEST_MINUTE_IRAN`
- تنظیمات رلهٔ اینستاگرام و userbot در صورت نیاز

هیچ token، کلید، cookie، فایل database یا session را در کد، README، log یا commit قرار ندهید.

## دستورها

| دستور | کاربرد |
| --- | --- |
| `/start` | معرفی ربات |
| `/new` و `/session` | ساخت، فهرست، انتخاب، تغییر نام یا حذف session خصوصی |
| `/reset` | پاک‌سازی حافظهٔ خصوصی یا تاریخچهٔ گروه طبق سطح دسترسی |
| `/stop` | لغو تولید فعال |
| `/pic <prompt>` | تولید تصویر |
| `/tr <language> <text>` | ترجمهٔ متن؛ برای ترجمهٔ عکس، caption را با `/tr` شروع کنید |
| `/todo [text]` و `/todos` | ساخت و مدیریت کارها |
| `/remind [text]` و `/reminders` | ساخت و مدیریت یادآورها |
| `/admin` | پنل خصوصی مدیریت |
| `/stats` | آمار مدیر |
| `/digest` | اجرای digest برای مالک |
| `/relay ...` | تنظیم رلهٔ اینستاگرام |
| `/stopall` | توقف مناظره‌های فعال برای مدیر |

برای Inline Mode باید `/setinline` و برای نتیجهٔ inline، `/setinlinefeedback 100%` را در BotFather فعال کنید. Guest Mode نیز باید برای هر bot جداگانه فعال شود.

## استقرار در Railway و Docker

1. سرویس Railway را از repository بسازید.
2. یک Volume پایدار اضافه و `DATA_DIR=/data` تنظیم کنید.
3. متغیرهای محیطی لازم را در داشبورد Railway قرار دهید.
4. تغییرات را از یک branch و پس از review به `main` merge کنید.

Railway از `Dockerfile` و `start.sh` استفاده می‌کند. در صورت وجود `TG_API_ID` و `TG_API_HASH`، Local Bot API اختیاری راه‌اندازی می‌شود و سپس `python3 bot.py` اجرا می‌گردد. سرویس polling است و health server جداگانه لازم ندارد.

```bash
docker build -t l-bot .
docker run --env-file .env -v l-bot-data:/data l-bot
```

## داده و پشتیبان

SQLite در `DATA_DIR/chat_history.db` با WAL نگهداری می‌شود. backup، دانلودها و sessionهای اختیاری نیز در همین مسیر هستند. راهنمای کامل در [RESTORE.md](RESTORE.md) است.

## تست و نقشهٔ پروژه

```bash
python -m pytest -q tests/
python tools/check_order.py
python -m pytest -q tests/test_dispatch.py
```

- [PROJECT_MAP.md](PROJECT_MAP.md): معماری و فهرست قابلیت‌ها
- [docs/map/modules.md](docs/map/modules.md): مسئولیت ماژول‌ها و APIهای عمومی
- [docs/map/config.md](docs/map/config.md): متغیرهای محیطی
- [docs/map/integrations.md](docs/map/integrations.md): سرویس‌های بیرونی
- [docs/map/data.md](docs/map/data.md): schema و state
- [RESTORE.md](RESTORE.md): بازیابی backup

ثبت handlerها به‌ترتیب انجام می‌شود چون dispatch در pyTelegramBotAPI first-match است. تغییرات را روی branch انجام دهید و مستقیم روی `main` push نکنید؛ merge به `main` باعث deploy در Railway می‌شود.
