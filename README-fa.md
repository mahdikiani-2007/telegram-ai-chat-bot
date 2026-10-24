# Telegram AI Chat Bot

ربات تلگرام فارسی‌محور با گفت‌وگوی استریم‌شونده، حافظهٔ پایدار، درک تصویر، ابزارهای تصویری، task و reminder، گردش‌کارهای رسانه‌ای، کنترل‌های مدیریتی، digest روزانه و استقرار روی Railway/Docker.

این repository نسخهٔ modular فعلی را نگه می‌دارد. ربات اصلی **L** با شخصیت کارآگاهی است و ربات اختیاری **Light** برای گفت‌وگو و مناظره فعال می‌شود. سرویس با long polling و SQLite/WAL کار می‌کند و وب‌سرور health جداگانه ندارد.

## فهرست

- [قابلیت‌ها](#قابلیت‌ها)
- [معماری و ساختار پروژه](#معماری-و-ساختار-پروژه)
- [پیش‌نیازها](#پیش‌نیازها)
- [نصب محلی](#نصب-محلی)
- [متغیرهای محیطی](#متغیرهای-محیطی)
- [اجرای ربات](#اجرای-ربات)
- [دستورها و workflowها](#دستورها-و-workflowها)
- [پایداری داده و امنیت](#پایداری-داده-و-امنیت)
- [Docker و Railway](#docker-و-railway)
- [تست و validation](#تست-و-validation)
- [backup و restore](#backup-و-restore)
- [رفع اشکال](#رفع-اشکال)
- [نکات deploy](#نکات-deploy)
- [مجوز](#مجوز)

## قابلیت‌ها

### گفت‌وگو و حافظه

- پاسخ استریم‌شونده در private chat و گروه‌هایی که L را mention/reply/call می‌کنند.
- تاریخچه و خلاصهٔ چرخشی گفتگو در SQLite.
- session خصوصی برای ساخت، انتخاب، تغییر نام و حذف گفتگو.
- factهای بلندمدت، پروفایل رفتاری و خلاصهٔ گروه با `/tldr`.
- جست‌وجوی اختیاری Tavily با بودجهٔ روزانهٔ مشترک برای chat و digest.
- rate limit، ضداسپم، لغو با `/stop` و اعلان خطا به مدیر با throttle.

### vision، ترجمه و تولید تصویر

- مسیر اختصاصی vision برای عکس؛ پیام تصویری به chat router متنی وابسته نیست.
- `/tr` برای ترجمهٔ متن و OCR/ترجمهٔ عکس با caption دارای `/tr`.
- `/pic` برای تولید تصویر با endpoint سازگار با OpenAI.

### task، reminder و debate

- todo با priority، category، deadline، delegation و recurrence.
- reminder با زمان نسبی، ساعت ایران، recurrence، mention target، snooze و dismiss.
- مناظرهٔ L و Light با `/stopall` و محدودیت قابل تنظیم برای round و timeout.

### رسانه و قابلیت‌های Telegram

- دانلود پست/ریل عمومی اینستاگرام با yt-dlp و cookie اختیاری.
- انتخاب کیفیت YouTube، پردازش ffmpeg، cookie اختیاری و Deno برای چالش‌های JavaScript.
- ارسال فایل بزرگ با userbot اختیاری Pyrogram.
- Inline mode، chosen-inline و Telegram Guest Mode.
- رلهٔ اختیاری پیام خصوصی اینستاگرام به یک chat تلگرام.

### عملیات

- پنل inline خصوصی برای user، group، ban، setting، job، broadcast، digest mode و profile.
- digest روزانه با حالت‌های standard، off و profile سفارشی، ثبت تحویل و محافظت بودجهٔ Tavily.
- backup روزانه، retention، checkpoint WAL، پاک‌سازی session، استخراج fact و پروفایل رفتاری.

## معماری و ساختار پروژه

```text
bot.py                  shim سازگاری و اجرای main.main()
main.py                 startup، diagnostics، workerها و polling
wiring.py               ترتیب handlerها، منوی command، polling و shutdown
config.py               env، defaultها، مسیرها و logging
bots.py                 نمونهٔ bot و اعلان خطای مدیر
db.py                   repository و migrationهای SQLite
ai.py                   clientهای chat، summary، vision و image
sessions.py             state موقت wizard و garbage collection
limits.py               rate limiter و anti-spam
ig_relay.py             سرویس و command رلهٔ اینستاگرام
handlers/               command، callback، panel و pipelineهای chat
services/               memory، streaming، media، digest، scheduling، backup، search
tools/                  ابزارهای تشخیص و verify restore
tests/                  تست‌های hermetic و regression
```

`wiring.register_all()` قرارداد dispatch است. چون pyTelegramBotAPI از first-match استفاده می‌کند، guard، admin/panel، wizardها، commandها، media، memory و در پایان catch-all chat به ترتیب ثبت می‌شوند.

### جریان startup

1. `start.sh` در صورت نیاز Local Bot API Server را روی پورت `8081` اجرا می‌کند.
2. `python3 bot.py` اجرا و `main.main()` فراخوانی می‌شود.
3. signal handler، handlerها، diagnostics، حذف webhook و sync منوی command انجام می‌شود.
4. workerهای reminder، session GC، digest، backup، fact، behavior، userbot و relay شروع می‌شوند.
5. polling ربات L و در صورت تنظیم `TELEGRAM_TOKEN_LIGHT` polling ربات Light شروع می‌شود.
6. در shutdown، polling متوقف، کارهای در حال اجرا جمع و WAL checkpoint می‌شود.

## پیش‌نیازها

- Python **3.13**.
- `pyTelegramBotAPI==4.36.0` و پکیج‌های دقیق [`requirements.txt`](requirements.txt).
- توکن Telegram از [@BotFather](https://t.me/BotFather).
- `CHAT_API_KEY` یا fallback قدیمی `AGNES_API_KEY`.
- کلیدهای اختیاری image، vision، Tavily، relay و userbot.
- Docker برای image production و Volume پایدار Railway.

## نصب محلی

```bash
git clone https://github.com/mahdikiani-2007/telegram-ai-chat-bot.git
cd telegram-ai-chat-bot
python3.13 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

حداقل `.env`:

```env
TELEGRAM_TOKEN=your_telegram_bot_token
CHAT_API_KEY=your_chat_api_key
ADMIN_IDS=123456789
```

`.env`، token، API key، cookie، database و Telegram session را commit نکنید. فایل [`.env.example`](.env.example) فقط template عمومی است.

## متغیرهای محیطی

همهٔ متغیرها از process environment خوانده می‌شوند. مرجع عمومی [`.env.example`](.env.example) است. تنظیمات runtime پنل در SQLite ذخیره می‌شوند، نه در `.env`.

### الزامی و هویت

| متغیر | پیش‌فرض | کاربرد |
| --- | --- | --- |
| `TELEGRAM_TOKEN` | ندارد | token ربات اصلی؛ الزامی |
| `AGNES_API_KEY` | ندارد | کلید قدیمی/مشترک AI و fallback |
| `CHAT_API_KEY` | `AGNES_API_KEY` | کلید chat؛ یکی از این دو لازم است |
| `ADMIN_IDS` | unset | شناسه‌های عددی مدیر؛ اولین مقدار owner است |
| `ADMIN_ID` | `0` | fallback قدیمی یک مدیر |
| `TELEGRAM_TOKEN_LIGHT` | unset | فعال‌سازی ربات Light |

### endpoint و رفتار AI

| متغیر | پیش‌فرض | کاربرد |
| --- | --- | --- |
| `CHAT_API_BASE` | `https://opencode.ai/zen/v1` | endpoint chat |
| `CHAT_MODEL` | `mimo-v2.5-free` | مدل chat |
| `IMAGE_API_KEY` | `AGNES_API_KEY` | کلید image |
| `AGNES_BASE` | `https://apihub.agnes-ai.com/v1` | base image و fallback vision |
| `IMAGE_MODEL` | `agnes-image-2.0-flash` | مدل image |
| `VISION_API_KEY` | `AGNES_API_KEY` | کلید vision |
| `VISION_API_BASE` | `AGNES_BASE` | endpoint vision |
| `VISION_MODEL` | `agnes-3.0-flash` | مدل vision |
| `CHAT_RETRY_ATTEMPTS` | `2` | تلاش اضافه پیش از stream |
| `RECENT_HISTORY` | `18` | تعداد turnهای اخیر |
| `SUMMARIZE_THRESHOLD` | `60` | آستانهٔ خلاصه‌سازی |
| `SUMMARIZE_BATCH` | `40` | تعداد ردیف‌های قدیمی برای خلاصه |
| `STREAM_EDIT_INTERVAL` | `1.2` | فاصلهٔ ویرایش پیام stream |

### storage، scheduler و shutdown

| متغیر | پیش‌فرض | کاربرد |
| --- | --- | --- |
| `DATA_DIR` | `data/` و در Docker برابر `/data` | database، backup، download و session |
| `SCHEDULER_MAX_WORKERS` | `5` | workerهای reminder/todo |
| `MAX_DISPATCH_RETRIES` | `5` | تعداد retry خطای دائمی |
| `SESSION_TTL_SECONDS` | `900` | عمر stateهای موقت |
| `SESSION_GC_INTERVAL` | `300` | فاصلهٔ پاک‌سازی |
| `ADMIN_ERROR_THROTTLE_SECONDS` | `60` | فاصلهٔ اعلان خطا به مدیر |
| `DROP_PENDING_UPDATES` | `false` | فقط برای حذف backlog قدیمی |
| `SHUTDOWN_GRACE_SECONDS` | `20` | زمان پایان graceful |
| `DB_RETENTION_DAYS` | `90` | retention پیام‌ها |
| `DB_SIZE_WARN_MB` | `500` | آستانهٔ هشدار حجم DB؛ صفر یعنی خاموش |
| `BACKUP_KEEP_DAILY` | `7` | تعداد snapshotهای محلی |

### search و jobهای روزانه

| متغیر | پیش‌فرض | کاربرد |
| --- | --- | --- |
| `TAVILY_API_KEY` | unset | فعال‌سازی search |
| `TAVILY_SEARCH_URL` | `https://api.tavily.com/search` | endpoint search |
| `TAVILY_MAX_RESULTS` | `5` | تعداد نتیجه |
| `TAVILY_DAILY_LIMIT` | `40` | بودجهٔ روزانه؛ صفر یعنی بدون سقف |
| `DIGEST_HOUR_IRAN` | `9` | ساعت digest ایران |
| `DIGEST_MINUTE_IRAN` | `0` | دقیقهٔ digest |
| `BACKUP_HOUR_IRAN` | `4` | ساعت backup |
| `MAX_DEBATE_ROUNDS` | `50` | سقف round مناظره |
| `DEBATE_DELAY` | `4` | فاصلهٔ turnهای مناظره |
| `DEBATE_GENERATE_TIMEOUT` | `180` | timeout تولید مناظره |
| `DEBATE_GENERATE_ATTEMPTS` | `2` | تلاش تولید مناظره |
| `TLDR_WINDOW_MINUTES` | `180` | پنجرهٔ پیش‌فرض `/tldr` |
| `TLDR_COOLDOWN_SECONDS` | `300` | cooldown گروه |
| `FACT_EXTRACT_COOLDOWN` | `45` | cooldown استخراج fact |
| `BEHAVIOR_PROFILE_HOUR` | `3` | ساعت profiling ایران |
| `BEHAVIOR_TRANSCRIPT_MAX_CHARS` | `16000` | سقف transcript profiler |

### media، Local Bot API و userbot

| متغیر | پیش‌فرض | کاربرد |
| --- | --- | --- |
| `LOCAL_BOT_API_URL` | unset | استفاده از Local Bot API و سقف upload بزرگ‌تر |
| `INSTAGRAM_COOKIES_FILE` | unset | cookie Netscape اینستاگرام |
| `YOUTUBE_COOKIES_FILE` | unset | cookie Netscape یوتیوب |
| `YOUTUBE_MAX_HEIGHT` | `1080` | سقف کیفیت YouTube |
| `TG_API_ID` | `0` | application ID تلگرام |
| `TG_API_HASH` | خالی | application hash |
| `TG_SESSION_NAME` | `yt_uploader` | نام session Pyrogram |
| `TG_SESSION_DIR` | `DATA_DIR` | مسیر session |

برای cookie از account اختصاصی استفاده کنید. session userbot مانند credential کامل حساب Telegram است.

### Instagram relay

| متغیر | پیش‌فرض | کاربرد |
| --- | --- | --- |
| `IG_RELAY_USERNAME` | خالی | username حساب relay |
| `IG_RELAY_PASSWORD` | خالی | password حساب relay |
| `IG_RELAY_TARGET_CHAT` | خالی | مقصد پیش‌فرض Telegram |
| `IG_RELAY_THREADS` | خالی | شناسهٔ threadها |
| `IG_RELAY_POLL_SECONDS` | `20`، حداقل `5` | فاصلهٔ polling |
| `IG_RELAY_MAX_ATTEMPTS` | `3`، حداقل `1` | retry هر پست |
| `IG_RELAY_LOGIN_BACKOFF` | `300`، حداقل `30` | backoff login |
| `IG_RELAY_CHALLENGE_BACKOFF` | `900`، حداقل `300` | backoff checkpoint |

## اجرای ربات

```bash
set -a
. ./.env
set +a
python bot.py
```

این دستور long polling را در foreground اجرا می‌کند. برای shutdown از `Ctrl-C` یا `SIGTERM` استفاده کنید؛ WAL در پایان checkpoint می‌شود. `bot.py` shim است و منطق اصلی در `main.py` قرار دارد.

## دستورها و workflowها

### chat و session

| دستور/سطح | رفتار |
| --- | --- |
| `/start` | معرفی L و راهنمای session |
| `/new` | ساخت session خصوصی |
| `/session` | فهرست، انتخاب، تغییر نام و حذف session |
| `/stop` | لغو پاسخ فعال |
| `/reset` | reset حافظهٔ خصوصی یا تاریخچهٔ گروه طبق permission |
| متن/عکس | private مستقیم؛ گروه با mention/reply/call به L؛ عکس با vision |

### ترجمه و تصویر

- `/tr [language] [text]` برای ترجمه؛ caption عکس با `/tr` از OCR/vision استفاده می‌کند.
- `/pic <prompt>` برای تولید تصویر.

### task و reminder

- `/todo [text]`: priority، category، deadline، recurrence و delegation با `@username`.
- `/todos`: فهرست todo فعال و منقضی با دکمه‌های done/delete.
- `/remind [text]`: زمان، mention target و recurrence.
- `/reminders`: فهرست reminderهای pending و حذف مجاز.

### memory

- `/tldr [N]`: خلاصهٔ چت گروه.
- `/myfacts`: نمایش factهای پایدار.
- `/forget <number|all>`: حذف یک یا همهٔ factها.

### media و Telegram

- ارسال لینک Instagram/YouTube downloader را فعال می‌کند.
- YouTube quality picker دارد؛ ffmpeg و Deno کیفیت و merge را بهتر می‌کنند.
- برای inline، `/setinline` و `/setinlinefeedback 100%` را در BotFather فعال کنید.
- برای Guest Mode باید این قابلیت را برای هر bot فعال کنید.

### admin

| دستور | رفتار |
| --- | --- |
| `/admin` | پنل inline خصوصی با بررسی permission در هر update |
| `/stats` | آمار مدیر |
| `/digest` | اجرای digest برای owner |
| `/relay ...` | کنترل login/logout/status/target/thread رله |
| `/stopall` | توقف مناظره برای مدیر |

پنل user/group، ban، toggle، setting، rate limit، job، broadcast، digest mode و profile را مدیریت می‌کند. mutationها audit می‌شوند و عملیات مخرب confirmation دارند.

## پایداری داده و امنیت

SQLite در `DATA_DIR/chat_history.db` با WAL و writeهای serial نگهداری می‌شود:

```text
DATA_DIR/
├── chat_history.db
├── backups/
├── ig_downloads/
├── yt_downloads/
├── botapi/
└── session files
```

حجم retention فقط روی message history اعمال می‌شود؛ summary، fact، todo، reminder، session و profile حذف نمی‌شوند. در production حتماً Volume پایدار وصل کنید.

امنیت:

- اولین مقدار `ADMIN_IDS` مالک است و `ADMIN_ID` fallback قدیمی است.
- پنل فقط private است و stranger/group را بی‌صدا رد می‌کند.
- admin قابل ban/reset نیست و mutationها audit می‌شوند.
- token، key، password، cookie، `.db` و `.session` را log یا commit نکنید.
- cookie و userbot را برای account اختصاصی نگه دارید.

## Docker و Railway

```bash
docker build -t telegram-ai-chat-bot .
docker run --env-file .env -v telegram-ai-chat-bot-data:/data telegram-ai-chat-bot
```

Dockerfile:

- Local Bot API Server را از source pinned build می‌کند.
- از `python:3.13-slim` استفاده می‌کند.
- ffmpeg و Deno checksum-verified را نصب می‌کند.
- dependencyهای دقیق `requirements.txt` را نصب می‌کند.
- `DATA_DIR=/data` و `./start.sh` را تنظیم می‌کند.

برای Railway:

1. repository را به service وصل کنید.
2. Volume پایدار را روی `/data` mount کنید.
3. variableهای لازم را از [`.env.example`](.env.example) وارد کنید.
4. branch بررسی‌شده را از طریق PR به `main` merge کنید.

Railway Dockerfile را build و `start.sh` را اجرا می‌کند. اگر `TG_API_ID` و `TG_API_HASH` تنظیم باشند، Local Bot API روی `8081` اجرا می‌شود؛ در غیر این صورت cloud Bot API با سقف upload کوچک‌تر استفاده می‌شود. این سرویس long polling است و health server جدا ندارد.

## تست و validation

تست‌ها hermetic هستند و `tests/conftest.py` credential آزمایشی و data directory موقت می‌سازد:

```bash
python -m pytest -q tests/
python tools/check_order.py
python -m pytest -q tests/test_dispatch.py
```

ابزارهای مفید:

```bash
python tools/census.py
python tools/vision_check.py
python tools/verify_restore.py --help
```

`vision_check.py` به credential واقعی نیاز دارد؛ جزو تست hermetic نیست. `check_order.py` ترتیب first-match handlerها را محافظت می‌کند.

## backup و restore

backup روزانه در `BACKUP_HOUR_IRAN` اجرا می‌شود، retention را اعمال می‌کند، snapshot gzip در `DATA_DIR/backups/` می‌سازد، snapshotهای قدیمی را طبق `BACKUP_KEEP_DAILY` حذف و یک کپی برای owner ارسال می‌کند.

```bash
python tools/verify_restore.py \
  --archive DATA_DIR/backups/chat_history_YYYY-MM-DD_HHMM.db.gz
```

برای restore:

1. ربات را متوقف کنید؛ DB در حال اجرا را جایگزین نکنید.
2. archive را verify کنید.
3. فایل را restore کنید:

   ```bash
   python tools/verify_restore.py \
     --archive DATA_DIR/backups/<archive>.db.gz \
     --restore /data/chat_history.db
   ```

4. ربات را restart و logهای شروع را بررسی کنید.

راهنمای کامل در [`RESTORE.md`](RESTORE.md) است.

## رفع اشکال

### شروع نمی‌شود

- `TELEGRAM_TOKEN` و `CHAT_API_KEY`/`AGNES_API_KEY` را بررسی کنید.
- Python 3.13 و اجرای `pip install -r requirements.txt` در همان virtualenv را بررسی کنید.
- writable بودن `DATA_DIR` را بررسی کنید.

### Inline یا Guest Mode کار نمی‌کند

- نسخهٔ `pyTelegramBotAPI==4.36.0` را در محیط واقعی بررسی کنید.
- `/setinline` و `/setinlinefeedback 100%` را در BotFather تنظیم کنید.
- Guest Mode را برای همان bot فعال کنید.

### vision یا media مشکل دارد

- vision از `VISION_*` استفاده می‌کند، نه فقط chat router.
- برای Instagram/YouTube cookie اختصاصی، Deno، ffmpeg و فضای `DATA_DIR` را بررسی کنید.

### فایل بزرگ ارسال نمی‌شود

- Local Bot API یا userbot را با `TG_API_ID`/`TG_API_HASH` و session معتبر تنظیم کنید.
- session Pyrogram باید قبلاً به‌صورت interactive ساخته شده باشد.

### پیام‌های قدیمی بعد از deploy می‌آیند

این رفتار پیش‌فرض است چون `DROP_PENDING_UPDATES=false` است. فقط برای backlog خراب، یک‌بار آن را true کنید و سپس حذف کنید.

### دادهٔ Railway ناپدید شده

mount شدن Volume روی `/data` را بررسی و آخرین archive سالم را طبق `RESTORE.md` restore کنید.

## نکات deploy

- `main` شاخهٔ production است و merge آن deploy محسوب می‌شود.
- force-push، حذف tag یا جایگزینی DB پایدار انجام ندهید.
- `DROP_PENDING_UPDATES=false` را در deployهای عادی نگه دارید.
- backup off-site و Volume پایدار داشته باشید.
- پیش از تغییر handler، schema، config یا integration، `PROJECT_MAP.md` و `docs/map/` را بخوانید.
- منوی command در startup توسط `wiring.py` sync می‌شود و تغییر دستی BotFather overwrite خواهد شد.

## مجوز

این پروژه تحت [MIT License](LICENSE) منتشر شده است. Copyright © 2026 Moai (`@moai_artwork`). متن کامل در فایل [`LICENSE`](LICENSE) قرار دارد.
