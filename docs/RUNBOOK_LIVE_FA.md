# دستورالعمل اجرای واقعی (Live Runbook)

این سند روال دقیق و مرحله‌به‌مرحلهٔ اجرای ربات با **پول واقعی** است. برای هر مرحله آمده است که چه می‌کند، چه خروجی و کد خروجی انتظار می‌رود، و اگر نتیجه چیز دیگری بود چه باید کرد.

> **هشدار صادقانه:** مراحل `check-live`، `canary` و `live` هرگز با یک کلید واقعی اجرا نشده‌اند. هیچ سفارش واقعی‌ای تا امروز توسط این پروژه ثبت نشده است. خروجی‌های مورد انتظار این مراحل **از روی کد** نوشته شده‌اند و در تست‌ها با پاسخ‌های mock بررسی شده‌اند، نه با والکس واقعی. خروجی مراحل `diagnose`، `paper` و `selftest` واقعی هستند (۲۰۲۶-۱۰-۲۲)، و خروجی `net-check` هم واقعی است (۲۰۲۶-۱۰-۲۳، فقط بخش عمومی و بدون کلید). ([`TEST_STATUS_FA.md`](TEST_STATUS_FA.md))

همهٔ فرمان‌ها از داخل پوشهٔ پروژه و با venv فعال اجرا می‌شوند. روی ویندوز (PowerShell) و لینوکس فرمان‌ها یکسان‌اند، مگر جایی که جدا نوشته شده باشد. اگر venv فعال نیست، روی ویندوز به جای `python` بنویسید `.\.venv\Scripts\python.exe`. روی VPS هر فرمان را با همان فایل محیطی سرویس اجرا کنید: `wallex-agent --env-file /etc/wallex-agent/wallex-agent.env <فرمان>` ([`VPS_SYSTEMD_FA.md`](VPS_SYSTEMD_FA.md)).

> **مدل حفاظت:** دروازه‌های این دستورالعمل جلوی **اشتباه و اجرای خودکار یا تصادفی** را می‌گیرند، نه جلوی یک فرایند بدخواه که با همان کاربر سیستم‌عامل اجرا شود (او می‌تواند کلید را بخواند و شواهد را جعل کند). جزئیات و ریسک‌های باقی‌مانده: README بخش‌های ۱۵ و ۱۶.

## ترتیب کلی

```text
net-check -> diagnose -> paper (smoke) -> selftest -> check-live -> canary -> [arm-live] -> live --stage convert
          -> (بررسی در اپ والکس) -> live --stage full -> پایش (status / trace) -> report
```

## ۰. چک‌لیست پیش از پرواز

- [ ] پایتون ۳.۱۰ یا جدیدتر و venv ساخته شده است. نصب با `pip install -e ".[dev]"` انجام شده، چون `selftest` به pytest نیاز دارد.
- [ ] پروژه یک پوشهٔ کامل سورس است (پوشهٔ `tests/` وجود دارد) و **در طول مسابقه هیچ فایلی در پکیج، `tests/` یا `pyproject.toml` تغییر نمی‌کند.** هش سورس همهٔ فایل‌های پکیج (نه فقط `.py`، بلکه داده و افزونه‌های باینری هم)، `pyproject.toml` و `tests/**/*.py` را می‌پوشاند؛ هر تغییری دروازهٔ live را تا اجرای دوبارهٔ `selftest`، `check-live` و `arm-live` می‌بندد. پس وسط کار `git pull` نکنید.
- [ ] فایل `.env` آماده است:
  - `WALLEX_API_KEY=...` (بخش ۱۰ README را ببینید)
  - `ENABLE_LIVE=true`
  - `DATA_DIR` **صریح و ترجیحاً مطلق** (مثلاً `DATA_DIR=C:\wallex-agent\state`). بدون `DATA_DIR` صریح، live اجرا نمی‌شود (`data_dir_explicit`).
  - `INITIAL_TMN` درست است
  - `PROFILE` آگاهانه انتخاب شده (و اگر `competition` است، خط `MAX_EXPOSURE_FRACTION` حذف شده؛ README بخش ۴-۱)
  - **`FINAL_POLICY` از قبل انتخاب شده است** (README بخش ۴-۲)
  - مسیر شبکه آگاهانه انتخاب شده: مستقیم (پیش‌فرض)، یا `WALLEX_PROXY_URL=http://host:port` (توصیه‌شده وقتی پروکسی لازم است)، یا `WALLEX_USE_ENV_PROXY=true`؛ و با IP مجاز کلید سازگار است (با پروکسی، IP خروجی پروکسی). `net-check` (مرحلهٔ ۰-۱) مسیر واقعی را نشان می‌دهد.
- [ ] کلید API فقط مجوز «معامله» و «خواندن» دارد، **مجوز برداشت ندارد**، فهرست IP مجاز (اگر پر شده) شامل IP همین دستگاه است، و تا پس از پایان مسابقه منقضی نمی‌شود.
- [ ] حساب والکس **حداقل `INITIAL_TMN` تومان موجودی آزاد** دارد. ربات دفترش را با `INITIAL_TMN` تومان شروع می‌کند و هنگام شروع، دفتر را با موجودی واقعی مقایسه می‌کند. کمبود بیش از ۱۰ تومان باعث توقف ایمنی (STOPPED) می‌شود. موجودی **بیشتر** مشکلی ندارد و فقط گزارش می‌شود.
- [ ] حداقل ۶۵٬۰۰۰ تومان موجودی آزاد برای `canary` وجود دارد (۱٫۳ برابر حداقل سفارش ۵۰٬۰۰۰ تومانی `USDTTMN`). با `INITIAL_TMN=5000000` این شرط خودبه‌خود برقرار است.
- [ ] **هیچ سفارش بازی در بازارهای `BTCUSDT` و `USDTTMN` در حساب ندارید.** ربات در شروع، هر سفارش باز در این دو بازار را که خودش ثبت نکرده باشد تناقض می‌داند و STOPPED می‌شود.
- [ ] تصمیم گرفته‌اید که **در طول مسابقه دستی در این حساب معامله، واریز یا برداشت نکنید.** هر کمبود دارایی نسبت به دفتر ربات، ربات را متوقف می‌کند.
- [ ] ساعت سیستم همگام است (Windows: Settings → Time & language → Set time automatically؛ لینوکس: `timedatectl`). شواهدی که «در آینده» تاریخ خورده باشند (بیش از ۵ دقیقه) رد می‌شوند.
- [ ] روی ویندوز، Sleep در تنظیمات Power غیرفعال است، اینترنت پایدار است و پنجرهٔ PowerShell بسته نمی‌شود. اگر بسته شد، بخش «بعد از کرش» را ببینید.
- [ ] زمان شروع را با زمان رسمی مسابقه هماهنگ کرده‌اید. **ساعت ۲۴ ساعتهٔ ربات از اولین اجرای `live` شروع می‌شود، شامل `--stage convert`.**
- [ ] بخش «توقف اضطراری» همین سند را خوانده‌اید و می‌دانید `kill` چه می‌کند و چه نمی‌کند.

---

## ۰-۱. `net-check` — مسیر شبکه و خودآزمایی اتصال (فقط GET)

```bash
python -m wallex_agent net-check
python -m wallex_agent net-check --repeat 3     # اگر خطاها گاه‌به‌گاه‌اند
```

هیچ سفارشی ثبت نمی‌کند، چیزی نمی‌نویسد و هر درخواست را فقط یک بار امتحان می‌کند. اول مسیر را می‌گوید (پروکسی و منبعش، یا `DIRECT`، و اینکه کدام متغیر پروکسی تنظیم شده ولی **استفاده نمی‌شود**)، بعد میزبان اول را resolve می‌کند و سپس `GET /v1/markets` و `GET /v1/depth` هر دو نماد را از همان مسیری می‌فرستد که معامله استفاده خواهد کرد. برای هر درخواست زمان اتصال، پاسخ پروکسی به `CONNECT`، زمان و نسخهٔ TLS، وضعیت HTTP، تعداد بایت و زمان کل چاپ می‌شود؛ قوانین معاملهٔ USDTTMN و BTCUSDT نشان داده و crossed نبودن هر دو دفتر تأیید می‌شود. اگر کلید در `.env` باشد، GETهای خصوصی (موجودی، کارمزد، سفارش‌های باز، معاملات) با کلاینت **فقط‌خواندنی** هم اجرا می‌شوند؛ کلید و مبلغ‌ها چاپ نمی‌شوند. خروجی واقعی از محیط ساخت (۲۰۲۶-۱۰-۲۳، پشت پروکسی محلی، بدون کلید، کوتاه‌شده):

```text
route:  proxy http://127.0.0.1:2080 (from HTTPS_PROXY) -> CONNECT api.wallex.ir:443 -> TLS to api.wallex.ir, verified end-to-end through the tunnel
[PASS] GET /v1/markets: rules parsed for USDTTMN, BTCUSDT  [connect 4 ms | CONNECT 200 in 2 ms | TLS 1042 ms (TLSv1.3) | HTTP 200 after 5009 ms | 445258 bytes (chunked) in 1280 ms | total 7338 ms]
[PASS] rules USDTTMN: price decimals 0, qty decimals 2, minQty 0.01 USDT, minNotional 50000 TMN
[PASS] rules BTCUSDT: price decimals 2, qty decimals 8, minQty 0.00000001 BTC, minNotional 1 USDT
[FAIL] GET /v1/depth?symbol=BTCUSDT (1/2): tls_eof_no_close_notify: connection closed during the TLS handshake
       hint: the connection was cut during the TLS handshake (filtering/DPI, or the proxy cannot reach the server): try another route/proxy
       phases: connect 2 ms | CONNECT 200 in 1 ms | TLS 5322 ms | total 5324 ms
[PASS] GET /v1/depth?symbol=BTCUSDT (2/2): bid 81315.66 < ask 81391.37 (spread 9.31 bps, 50/50 levels): NOT crossed  [...]
net-check: FAIL (2 of 9 steps; first: ...) - a NETWORK step failed (exit 7)
```

| نتیجه | معنی و اقدام |
|---|---|
| کد `0` و `net-check: PASS` | مسیر کار می‌کند. ادامه دهید. |
| کد `7`، `route: DIRECT` و `note: HTTPS_PROXY ... NOT used` | ربات مستقیم وصل می‌شود، در حالی که `curl` شما احتمالاً از پروکسی رد می‌شود. `WALLEX_PROXY_URL=http://127.0.0.1:<پورت>` را در `.env` بگذارید و دوباره اجرا کنید. |
| کد `7` و `proxy_unreachable` | برنامهٔ پروکسی اجرا نیست یا آدرس/پورت غلط است. |
| کد `7` و `proxy_connect_failed` | پروکسی تونل را رد کرد: `407` یعنی ورود لازم است (`http://user:pass@host:port`)، `502/503/504` یعنی پروکسی خودش به والکس نمی‌رسد. |
| کد `7` و `tls_eof_no_close_notify` یا `connect_timeout` در مرحلهٔ TLS | پروکسی `CONNECT` را پذیرفت ولی اتصال بالادستی‌اش به والکس قطع شد یا کند بود (همان خطای `UNEXPECTED_EOF_WHILE_READING`). با `--repeat 3` میزان تکرارش را ببینید. ربات خواندن‌ها را خودکار تکرار می‌کند، ولی اگر بیشتر درخواست‌ها شکست می‌خورند، مسیر/پروکسی پایدارتری بیابید. |
| کد `7` و `tls_cert_verify_failed` | گواهی تأیید نشد: یک آنتی‌ویروس یا پروکسی TLS را رهگیری می‌کند، یا ساعت سیستم غلط است. **هرگز** بررسی گواهی را دور نزنید؛ علت را برطرف کنید. |
| کد `7` و `http_status` با `403` روی یک GET عمومی | صرافی دسترسی این IP/مسیر را بسته است (مسدودیت جغرافیایی یا WAF). مسیر دیگری امتحان کنید. |
| کد `6` و `http_status` با `401/403` روی یک GET خصوصی | شبکه سالم است ولی کلید رد شد: کلید غلط، نبودِ مجوز خواندن، یا IP مسیر (با پروکسی، IP خروجی پروکسی) در فهرست IP مجاز کلید نیست. |
| کد `6` (بقیه) | شبکه سالم است ولی یک بررسی رد شد (قوانین نامعتبر، دفتر crossed، کارمزد نامعلوم). live را شروع نکنید. |
| کد `2` | `WALLEX_PROXY_URL` یا متغیر پروکسی محیط نامعتبر است (مثلاً بدون پورت، `https://` یا `socks`). مقدار در پیام چاپ نمی‌شود. |

## ۱. `diagnose` — دسترسی و امکان‌سنجی (فقط درخواست‌های عمومی)

```bash
python -m wallex_agent diagnose
```

این فرمان بدون کلید کار می‌کند. قوانین بازار، دفتر سفارش هر دو بازار، اسپرد و عمق، و اینکه `INITIAL_TMN` تقریباً چند تتر می‌خرد (با کارمزد **فرضی**) را چاپ می‌کند. همچنین فهرست چیزهایی را که هنوز نامعلوم‌اند نشان می‌دهد. خروجی واقعی (۲۰۲۶-۱۰-۲۲، خلاصه):

```text
"book_USDTTMN": { "best_ask": "271827", "best_bid": "271770", "spread_bps": "2.1", ... },
"book_BTCUSDT": { "best_ask": "81441.7", "best_bid": "81431.81", "spread_bps": "1.21", ... },
"conversion": { "tmn": "5000000", "usdt_net_of_assumed_fee": "18.329673", "book_can_fill": true, ... },
"problems": [],
"trading_feasible_now": true,
...
5000000 TMN buys ~18.329673 USDT net of an ASSUMED 0.0035 fee (walking USDTTMN asks; avg 271827.00 TMN/USDT).
Trading feasible now: YES (public facts only; the real fee is still unknown)
```

| نتیجه | معنی و اقدام |
|---|---|
| کد خروج `0` و `YES` | ادامه دهید. |
| کد خروج `0` و `NO: ...` | دلیل چاپ شده است (مثلاً اسپرد بیش از حد یا عمق ناکافی). صبر کنید و دوباره اجرا کنید. live را شروع نکنید. |
| کد خروج `7` و `public API unavailable` | شبکه، DNS، فیلترینگ یا پروکسی. پیام شامل دستهٔ خطاست (مثلاً `[connect_timeout]`). `net-check` را اجرا کنید (مرحلهٔ ۰-۱). اگر دسترسی فقط با پروکسی ممکن است، `WALLEX_PROXY_URL=http://host:port` (README بخش ۱۱). |

## ۲. `paper` — تست دود

```bash
python -m wallex_agent paper --seconds 300 --interval 10
```

انتظار: کد خروج `0` و `status: session_end`، یک سفارش `CONVERT_TMN_USDT` پرشده، و `hash chain OK`. نمونهٔ کامل خروجی در README بخش ۷ آمده است. این مرحله ثابت می‌کند که حلقهٔ ایجنت روی دادهٔ واقعی همین دستگاه کار می‌کند. هیچ سفارشی به والکس نمی‌رود.

## ۳. `selftest` — شهادت دادن به همین سورس

```bash
python -m wallex_agent selftest
```

کل مجموعهٔ تست را روی **همین سورس** اجرا می‌کند و `state/selftest.json` را می‌نویسد که شامل هش سورس است. pytest در یک **محیط پاک‌شده** اجرا می‌شود: کلید API، `DATA_DIR`، `ENABLE_LIVE`، `TRADING_MODE`، بقیهٔ متغیرهای پیکربندی، پروکسی‌ها و `PYTEST_ADDOPTS` به تست‌ها نمی‌رسند. اجرایی که کمتر از **۱۰۰۰** تست موفق داشته باشد (مثلاً با `-k` یا پوشهٔ `tests/` ناقص) «کل مجموعه» نیست و چیزی را تأیید نمی‌کند. روی سیستم سازنده حدود ۷۰ ثانیه طول کشید. خروجی واقعی (۲۰۲۶-۱۰-۲۲، مسیر کوتاه شده):

```text
running the project's test suite (this attests THIS source tree for live trading)...
{
  "at": "2026-10-22T21:18:30Z",
  "duration_s": 70.844,
  "failures": 0,
  "passed": true,
  "reason": "all tests passed",
  "source_hash": "d8eb8a78b98ee098e9e5194856fcb8b5a302727667f6f5748fd07bc56eabda5f",
  "tests": 2333
}
written: C:\...\state\selftest.json
```

(هش سورس با هر تغییر کد عوض می‌شود؛ عدد بالا فقط نمونه است.)

- کد خروج `0` یعنی `passed: true`. این شاهد **۲۴ ساعت** برای شروع تازه و **۴۸ ساعت** برای ادامهٔ یک مسابقهٔ شروع‌شده اعتبار دارد.
- کد خروج `1` یعنی تستی شکست خورده، کمتر از ۱۰۰۰ تست اجرا شده، pytest نصب نیست (`pip install -e ".[dev]"`)، یا پوشهٔ `tests/` پیدا نشده است. علت در `reason` نوشته شده. تا این مرحله سبز نشود، live ممکن نیست.

## ۴. `check-live` — بررسی فقط‌خواندنی API خصوصی (اولین استفاده از کلید)

```bash
python -m wallex_agent check-live
```

این فرمان با یک کلاینت **فقط-GET** (که POST و DELETE در آن ناممکن است) این موارد را بررسی می‌کند:

- موجودی‌ها، کارمزد حساب، سفارش‌های باز و معاملات قبلی؛
- اینکه یک شناسهٔ تصادفی «پیدا نشد» برمی‌گرداند یا نه.

سپس `state/live_schema_attestation.json` را می‌نویسد. این فایل هیچ مبلغ یا رازی ندارد؛ فقط شکل پاسخ‌ها و نام بررسی‌ها را نگه می‌دارد. به `ENABLE_LIVE` نیاز ندارد. شکل خروجی (از روی کد):

```text
GET-only probe of the private endpoints (no order is placed; read-only client)...
{
  "attestation": {
    "at": "...Z",
    "base_url": "https://api.wallex.ir",
    "failed_checks": [],
    "ok": true,
    "reasons": [],
    "unverified": ["balance_value_includes_locked", "cancel_response", "client_id_echo", ...],
    "verified_by_canary": []
  },
  "checks": [
    {"name": "public.markets", "ok": true}, {"name": "public.depth.BTCUSDT", "ok": true},
    {"name": "public.depth.USDTTMN", "ok": true}, {"name": "private.balances", "ok": true},
    {"name": "private.fee", "ok": true}, {"name": "private.open_orders", "ok": true},
    {"name": "private.account_trades", "ok": true}, {"name": "private.order_not_found", "ok": true}
  ]
}
live gate still missing: canary_ok
```

| کد خروج | معنی و اقدام |
|---|---|
| `0` | شاهد معتبر است. در این مرحله طبیعی است که فقط `canary_ok` کم باشد. |
| `3` | حداقل یک بررسی شکست خورده (`failed_checks`). دلایل رایج: |
| | `private.balances` یا `private.fee` با خطای `AuthError`: کلید غلط، منقضی، یا IP مجاز نیست. |
| | `private.fee` با `symbols_present` ناقص: والکس برای یکی از دو بازار کارمزد برنگردانده؛ ربات بدون کارمزد معامله نمی‌کند. |
| | خطای `SchemaError`: پاسخ والکس با مستندات فرق دارد. **متوقف شوید و گزارش دهید**؛ کد را دستکاری نکنید. |
| `6` | کلید در `.env` نیست، یا قالب کلید پذیرفته نشد (`API key has an unexpected format`). |
| `2` | مقدار بدی در `.env` وجود دارد (`configuration error: ...`). |

## ۵. `canary` — یک سفارش واقعی، کوچک و عمداً پرنشدنی

```bash
python -m wallex_agent canary --i-understand-real-money
```

**این مرحله یک سفارش واقعی ثبت می‌کند:**

- یک سفارش LIMIT خرید در `USDTTMN` با قیمتی **حدود ۵۰٪ پایین‌تر از بهترین قیمت خرید (bid)** (با bid حدود ۲۷۱٬۷۷۰، قیمت حدود ۱۳۵٬۸۸۰ تومان).
- مقدار سفارش کمی بیشتر از حداقل ارزش بازار است: حدود ۰٫۳۸ تتر، یعنی حدود ۵۱ هزار تومان قفل‌شده.
- سفارش ثبت، بررسی و **بلافاصله لغو** می‌شود و لغو آن تأیید می‌شود.

شرط‌ها: `ENABLE_LIVE=true`، کلید، `selftest` و `check-live` تازه و موفق، نبودِ فایل KILL، فلگ `--i-understand-real-money`، و تایپ دقیق عبارت **`RUN CANARY 5000000`** (عدد = `INITIAL_TMN`) در یک ترمینال تعاملی.

canary چیزهایی را مشخص می‌کند که GET به‌تنهایی نمی‌تواند:

- آیا `client_id` ما عیناً برگردانده می‌شود؟
- آیا سفارش با شناسهٔ ما قابل جستجوست؟
- پاسخ لغو (DELETE) چه شکلی دارد؟
- آیا `active` بعد از لغو `false` می‌شود؟
- آیا `value` در موجودی شامل `locked` است؟

نتیجه در `state/live_canary.json` نوشته می‌شود (فقط بولی و متن، بدون مبلغ). شکل خروجی (از روی کد):

```text
CANARY: places ONE real LIMIT BUY on USDTTMN at ~50% of the best bid (non-fillable by design), just above minNotional, then cancels it. Gated like live trading.
REAL MONEY. Type exactly 'RUN CANARY 5000000' to continue: RUN CANARY 5000000
{
  "at": "...Z", "base_url": "https://api.wallex.ir", "ok": true,
  "observations": { "post_outcome": "acknowledged", "client_id_echo": true, "get_order_by_our_client_id": true,
                    "in_open_orders": true, "balance_locked_semantics": "...", "final_lifecycle": "CANCELED",
                    "final_active": false, "unexpected_fill": false, ... },
  "reasons": [],
  "verified": ["balance_value_includes_locked", "cancel_response", "client_id_echo", "get_order_by_client_id",
               "post_order_response_shape", "terminal_active_flag"]
}
written: state\live_canary.json
```

بعد از اجرا در اپ یا سایت والکس بررسی کنید: در تاریخچهٔ سفارش‌های `USDTTMN` باید **یک سفارش خرید لغوشده** با قیمت حدود نصف بازار ببینید، و در «سفارش‌های باز» هیچ چیزی نباشد.

| نتیجه | معنی و اقدام |
|---|---|
| کد `0`، `ok: true` | canary معتبر است و **۷۲ ساعت** اعتبار دارد. |
| کد `6` و `canary refused (nothing sent): ...` | هیچ چیزی ارسال نشده. دلیل نوشته شده: موجودی آزاد کمتر از ۶۵٬۰۰۰ تومان، KILL موجود است، سفارشی از ربات هنوز حل‌نشده است، یا وضعیت ربات UNFUNDED/READY نیست. |
| کد `6`، `ok: false` و پیام `!!! THE CANARY ORDER FILLED` | سفارش با وجود قیمت پایین پر شد (بازار ناگهانی یا رقابت با لغو). مقدار کمی تتر (حدود ۰٫۳۸) به قیمتی **ارزان‌تر** از بازار خریده‌اید که در دفتر live ربات ثبت شده است. ضرری ندارد. canary را دوباره اجرا کنید، چون دروازه `ok: true` می‌خواهد. |
| کد `6`، `ok: false` و `cancel could not be verified` | سفارش ممکن است هنوز باز باشد. در اپ والکس بررسی کنید و اگر باز است **دستی لغوش کنید**. سپس `status --mode live` و دوباره `canary` را اجرا کنید. برای این کار `kill` را **به کار نبرید**، چون `kill` دفتر live را STOPPED می‌کند و برای ادامه باید KILL را پاک و `unstop` را اجرا کنید (بخش ۱۰). |
| کد `3` | دروازه رد کرد و هیچ چیزی ارسال نشد. شرط‌های برآورده‌نشده فهرست شده‌اند. |
| کد `5` یا `7` و `canary refused: startup reconciliation not ok` | هنگام مقایسهٔ دفتر با حساب مشکلی دیده شد: کمبود TMN نسبت به `INITIAL_TMN` (با عدد: `TMN ledger ... exchange ... short ...`)، سفارش باز ناشناخته، یا نبودِ شبکه (مثلاً `rules_unavailable: TransportError[read_timeout]`). جدول پایانی را ببینید. |
| کد `7` و `canary refused (nothing sent): connectivity/reconcile preflight failed: ...` | پیش‌پرواز پیش از اولین سفارش رد شد و **هیچ چیزی ارسال نشد**: قوانین بازار یکی از دو نماد خوانده نشد، یا یکی از دو دفتر سفارش در دسترس، تازه یا غیر crossed نبود (دلیل با دستهٔ خطا چاپ می‌شود). `net-check` را اجرا کنید و وقتی پاس شد، canary را دوباره اجرا کنید. |
| کد `130` | Ctrl+C (یا SIGTERM) در حین canary. اگر پیش از ارسال بود، **هیچ چیزی ارسال نشده**؛ اگر پس از ثبت سفارش بود، بقیهٔ مشاهده‌ها رها و سفارش canary **بلافاصله لغو** شد (و در هر خطای دیگری پس از ثبت هم لغو می‌شود). canary را دوباره اجرا کنید. |

> canary از همان دفتر live (`state/live/state.db`) استفاده می‌کند ولی ساعت مسابقه را شروع **نمی‌کند**.

## ۶. `arm-live` — (اختیاری روی ویندوز، لازم برای systemd و `--resume`)

```bash
python -m wallex_agent arm-live --i-understand-real-money
```

همهٔ دروازه‌ها (شامل canary) را بررسی می‌کند، عبارت **`START LIVE 5000000`** را در ترمینال می‌پرسد و فایل `state/LIVE_ARMED` را به‌صورت یک **رکورد JSON ساخت‌یافته** می‌نویسد: عبارت، هش سورس، آدرس API، `DATA_DIR`، `INITIAL_TMN`، زمان arm و زمان انقضا (`COMPETITION_HOURS` + ۲۴ ساعت پس از arm). خروجی (از روی کد):

```text
REAL MONEY. Type exactly 'START LIVE 5000000' to continue: START LIVE 5000000
armed: C:\...\state\LIVE_ARMED written (bound to this source tree, https://api.wallex.ir, C:\...\state; expires ...Z).
It lets `live --resume` (systemd) CONTINUE a competition that you started interactively with `live`; it can never start a fresh live run. Delete the file to disarm.
```

نکته‌های مهم:

- `live --resume` **هرگز یک مسابقهٔ تازه را شروع نمی‌کند.** فقط مسابقه‌ای را ادامه می‌دهد که در دفتر live قبلاً شروع شده باشد (یعنی پس از اولین `live` تعاملی، شامل `--stage convert`). روی دفتری که مسابقه‌اش شروع نشده، با کد ۳ و پیام `first live start must be interactive` رد می‌شود.
- `--resume` فقط رکورد معتبر را می‌پذیرد: فایل متنی ساده (مثلاً `echo START LIVE 5000000 > LIVE_ARMED`) رد می‌شود (`not a structured arming record`). همچنین اگر کد عوض شده باشد (هش سورس)، `INITIAL_TMN`، آدرس API یا `DATA_DIR` فرق کند، یا رکورد منقضی شده باشد، رد می‌شود. در این صورت دوباره `selftest`، `check-live` و `arm-live` را اجرا کنید.
- برای خلع سلاح، فایل را پاک کنید.

## ۷. `live --stage convert` — فقط تبدیل TMN به USDT

```bash
python -m wallex_agent live --stage convert --i-understand-real-money
```

این **اولین شروع live** است و باید تعاملی باشد (عبارت در ترمینال تایپ می‌شود). همهٔ دروازه‌ها را بررسی می‌کند و عبارت `START LIVE 5000000` را می‌پرسد. سپس:

1. دفتر را با حساب مقایسه می‌کند (reconcile)، و سپس **پیش‌پرواز اتصال** را اجرا می‌کند: قوانین هر دو نماد همین حالا خوانده و معتبر، و هر دو دفتر سفارش تازه و غیر crossed. تا این دو موفق نشوند هیچ سفارشی ارسال نمی‌شود؛ اگر نشوند، با کد ۷ و دلیل (`connectivity/reconcile preflight failed, no order was sent: ...`) خارج می‌شود و ساعت مسابقه شروع **نمی‌شود**.
2. **ساعت ۲۴ ساعتهٔ مسابقه را شروع می‌کند.**
3. فقط مرحلهٔ تبدیل را اجرا می‌کند: یک یا چند سفارش LIMIT خرید `USDTTMN` با سقف لغزش ۳۰bps، با کارمزد **واقعی** حساب. **پیش از هر دور** (نه فقط دور اول) همان بررسی ریسک تصمیم ایجنت دوباره روی دادهٔ تازه اجرا می‌شود (اسپرد، عمق در محدودهٔ لغزش، تازگی دفتر سفارش، قوانین بازار، موجودی)؛ اگر بین دورها اسپرد ناگهان باز شود، دور بعدی ارسال نمی‌شود. Ctrl+C یا SIGTERM هم پیش از هر دور بررسی می‌شود.
4. وقتی وضعیت READY شد، متوقف می‌شود.

شکل خروجی (از روی کد):

```text
REAL MONEY. Type exactly 'START LIVE 5000000' to continue: START LIVE 5000000
LIVE (conversion stage only): run live-xxxxxxxxxxxx. REAL MONEY. Kill switch: `wallex-agent kill` (or create C:\...\state\KILL).
== LIVE (conversion stage only) ==
run:        live-xxxxxxxxxxxx
status:     stage_done (exit 0) - conversion stage complete: state READY (run `live --stage full` to trade)
...
report:     C:\...\state\live\runs\live-xxxxxxxxxxxx\partial_report.json
audit:      C:\...\state\live\runs\live-xxxxxxxxxxxx\audit.jsonl (hash chain OK)
REAL-MONEY run: the numbers above are the bot's own fills ledger valued at the last books, ...
```

خط آخر می‌گوید آیا ارزش با موجودی صرافی در پایان تأیید شد (`confirmed against the exchange balances`) یا نه (`NOT confirmed by the exchange at the end`). اگر تأیید نشد، عدد ارزش با `LEDGER-BASED, NOT verified` چاپ می‌شود و موجودی را باید در اپ والکس دید.

**در اپ والکس بررسی کنید:**

- سفارش(های) خرید `USDTTMN` در وضعیت «انجام‌شده» هستند؛
- موجودی USDT حدود ۱۸ است (دقیقش به نرخ و کارمزد شما بستگی دارد)؛
- چند ده هزار تومان باقی مانده است. این **طبیعی است**: ربات برای کارمزد و لغزش جا نگه می‌دارد و باقی‌مانده کمتر از حداقل ۵۰٬۰۰۰ تومانی بازار است. در paper حدود ۲۹ هزار تومان باقی ماند.
- هیچ سفارش بازی نمانده است.

خروجی `python -m wallex_agent status --mode live` هم باید `"state": "READY"` و `"open_orders": []` نشان دهد.

اگر تبدیل منتظر ماند (اسپرد غیرعادی یا عمق کم)، ربات صبر می‌کند و در هر چرخه دلیل را در audit می‌نویسد. Ctrl+C آن را تمیز متوقف می‌کند.

## ۸. `live --stage full` — اجرای اصلی

```bash
python -m wallex_agent live --stage full --i-understand-real-money
```

دوباره عبارت `START LIVE 5000000` پرسیده می‌شود (شاهدهای selftest و check-live باید هنوز زیر ۲۴ ساعت عمر داشته باشند). ربات هر `LOOP_INTERVAL_S` ثانیه (پیش‌فرض ۳۰) یک چرخهٔ کامل اجرا می‌کند:

- مشاهده؛
- تصمیم ایجنت؛
- دروازهٔ ریسک؛
- اجرا و تأیید؛
- بازتاب (reflect).

بیشتر چرخه‌ها `HOLD` خواهند بود. ربات در پایان ۲۴ ساعت (از شروع مرحلهٔ ۷) سفارش‌های باز را لغو می‌کند، `FINAL_POLICY` را اجرا می‌کند، `final_report.json` را می‌نویسد و با `deadline (exit 0)` خارج می‌شود.

## ۹. پایش (در یک ترمینال دوم، از همان پوشه)

```bash
python -m wallex_agent status --mode live
python -m wallex_agent trace --mode live --last 3
```

در خروجی `status` به این‌ها نگاه کنید:

- `state`: یکی از UNFUNDED، CONVERTING، READY، BUY_PENDING، BTC_HELD، SELL_PENDING یا STOPPED؛
- `time_left_h`؛
- `equity_tmn_net`: ارزش پس از هزینهٔ خروج؛
- `open_orders`؛
- `stop`: دلیل توقف، اگر STOPPED شده باشد؛
- `kill_file_present`.

دنبال کردن زندهٔ audit:

```powershell
Get-ChildItem state\live\runs -Directory | Sort-Object LastWriteTime | Select-Object -Last 1 | ForEach-Object { Get-Content (Join-Path $_.FullName "audit.jsonl") -Tail 5 -Wait }
```

```bash
tail -f "$(ls -td state/live/runs/*/ | head -1)audit.jsonl"
```

رکوردهای `"kind":"alert"` را جدی بگیرید:

| alert | معنی |
|---|---|
| `market_facts_unknown` | این چرخه معامله‌ای انجام نمی‌شود؛ معمولاً شبکه یا کلید. |
| `balance_shortfall_suspected` | موجودی صرافی از دفتر ربات **کمتر** است (با عدد). از همین لحظه هیچ سفارش تازه‌ای ارسال نمی‌شود. اگر دست‌کم ۱۵ ثانیه بعد هنوز کمبود باشد، ربات با `needs_manual_reconcile: ledger/exchange shortfall: ...` متوقف می‌شود؛ اگر برطرف شود، `balance_shortfall_cleared` ثبت می‌شود. |
| `ledger_exchange_mismatch` | پس از یک اقدام، دفتر ربات با موجودی حساب نخواند (همان بررسی بالا). |
| `needs_manual_reconcile` | تناقض بین ربات و صرافی؛ ربات STOPPED شده است. دلیل شامل عدد و شناسه است. |
| `late_accept` | سفارشی که ربات «رهاشده» (`ABANDONED`) می‌دانست، بعداً در صرافی پیدا شد. ربات آن را (اگر باز بود) لغو کرد و STOPPED شد؛ دلیل، مقدار اجراشدهٔ آن سفارش و دفتر در برابر صرافی را نشان می‌دهد. جدول پایانی را ببینید. |
| `unknown_outcome` / `unknown_unresolved` / `unknown_at_shutdown` | نتیجهٔ یک POST نامعلوم ماند؛ هرگز دوباره ارسال نمی‌شود و از روی شواهد صرافی (یا در شروع بعدی) حل می‌شود. |
| `cycle_exception` | خطای یک چرخه. پنج خطای پیاپی به توقف ایمنی می‌انجامد. |

رکورد `abandoned_watch_unverified` (نه alert) یعنی سفارش رهاشده‌ای که زیر نظر است در این بررسی قابل تأیید نبود (خواندن از صرافی شکست خورد)؛ تا وقتی تأیید نشود، سفارش تازه ارسال نمی‌شود. سفارش رهاشده **برای همیشه** در هر چرخه، پیش از هر سفارش تازه و در هر شروع دوباره بررسی می‌شود؛ قرنطینهٔ آن نماد و جهت فقط پس از ۳ بررسی پیاپیِ «پیدا نشد» که هرکدام دست‌کم ۴۵ ثانیه فاصله دارند (در مجموع دست‌کم ۱۳۵ ثانیه) برداشته می‌شود.

`status` و `trace` از یک کپی فقط‌خواندنی می‌خوانند و کنار ربات در حال اجرا بی‌خطرند.

## ۱۰. توقف اضطراری

| روش | چه می‌کند | بعد از آن |
|---|---|---|
| **Ctrl+C** در پنجرهٔ ربات (روی ویندوز Ctrl+Break هم کار می‌کند) | گام جاری تمام می‌شود (پیام توقف پس از آن چاپ می‌شود)، هر سفارش با نتیجهٔ نامعلوم یک بار جستجو و در صورت یافتن پذیرفته می‌شود، سفارش‌های باز **لغو** می‌شوند (فقط لغو)، `partial_report.json` نوشته می‌شود و کد خروج `130` است. وضعیت STOPPED **نمی‌شود**. | قابل ادامه است: همان فرمان `live` را دوباره اجرا کنید (یا `live --resume ...`). |
| **روی VPS: `sudo systemctl stop wallex-agent`** | همان Ctrl+C (سیگنال SIGINT). **روش اصلی توقف اضطراری روی VPS همین است**؛ پس از آن `kill` را هم بزنید تا systemd دوباره شروعش نکند. | مثل ردیف بالا. |
| **`kill`** از ترمینال دوم | فایل `<DATA_DIR>/KILL` را می‌سازد و **مسیر مطلقش را چاپ می‌کند**. ربات paper یا live در حال اجرا ظرف حدود یک ثانیه (در حالت انتظار) یا در شروع چرخهٔ بعد، سفارش‌های بازش را لغو می‌کند، **STOPPED** می‌شود و با کد `4` خارج می‌شود. تا وقتی KILL هست، هیچ اجرای paper/live، canary یا arm-live شروع نمی‌شود. (demo و backtest این فایل را نمی‌خوانند.) | دفتر live STOPPED است؛ برای ادامه پایین را ببینید. |
| **`kill --cancel-open`** | مثل بالا، و هر وقت `<DATA_DIR>/live/state.db` وجود داشته باشد (صرف‌نظر از `TRADING_MODE`) و ربات live در حال اجرا **نباشد** (مثلاً بعد از کرش)، سفارش‌های باز ثبت‌شده در دفتر **همین ربات** را با یک کلاینت «فقط لغو» لغو می‌کند. به سفارش‌های دیگر شما دست نمی‌زند. بدون کلید در محیط، با کد ۶ و پیام صریح رد می‌شود. | دفتر STOPPED می‌شود. کد `0`، یا `5` اگر سفارشی باز ماند. |

```bash
python -m wallex_agent kill --reason "manual emergency stop"
python -m wallex_agent kill --cancel-open --reason "bot crashed with an open order"
```

**`kill` را با همان تنظیمات ربات اجرا کنید.** فرمان `kill` فایل را در `DATA_DIR` **ترمینال خودتان** می‌نویسد. اگر در آن پوشه هیچ دفتر live، اجرای paper یا قفل تک‌نمونه‌ای نباشد (یعنی به احتمال زیاد پوشهٔ ربات را اشتباه گرفته‌اید)، فرمان **هیچ فایلی نمی‌نویسد**، مسیر مطلقی را که بررسی کرده با هشدار بلند چاپ می‌کند و با کد `6` خارج می‌شود. در این صورت با همان تنظیمات ربات دوباره اجرا کنید (`--env-file <فایل محیطی ربات>` یا `--data-dir <پوشهٔ ربات>`). اگر عمداً می‌خواهید فایل KILL را در پوشهٔ خالی بنویسید (مثلاً برای جلوگیری از یک شروع آینده)، `--force-write` بدهید. خروجی `kill` نشان می‌دهد آیا ربات live یا paper در همان پوشه در حال اجراست (`live_agent_running`، `paper_running`).

**چه چیزی فروخته نمی‌شود:** هیچ‌کدام از این روش‌ها **هیچ چیزی نمی‌فروشند.** فقط سفارش‌های باز لغو می‌شوند. BTC و USDT شما همان‌طور که هست می‌ماند. `FINAL_POLICY` فقط در پایان عادی مسابقه اجرا می‌شود، نه هنگام kill یا Ctrl+C.

**صاف کردن دستی موقعیت در اپ والکس** (اگر می‌خواهید):

1. ابتدا مطمئن شوید ربات متوقف شده است (`status --mode live` → `STOPPED`، یا پنجرهٔ آن بسته است).
2. در بخش «سفارش‌های باز» بررسی کنید که سفارش بازی در `BTCUSDT` و `USDTTMN` نمانده باشد.
3. در بازار اسپات **BTC/USDT** مقدار BTC را بفروشید.
4. اگر تومان می‌خواهید، در بازار **USDT/TMN** تتر را بفروشید.

بعد از هر معاملهٔ دستی، **این دفتر live را دوباره راه‌اندازی نکنید.** دفتر ربات دیگر با حساب نمی‌خواند و ربات با `needs_manual_reconcile` متوقف می‌شود، که رفتار درست و مورد انتظار است (و `unstop` هم رد می‌شود).

**ادامه دادن پس از KILL یا هر توقف ایمنی (`unstop`):** بعد از اینکه ربات KILL را اجرا کرد، دفتر live در حالت STOPPED است و فقط پاک کردن فایل KILL کافی نیست (`live` با کد `5` و `bot is STOPPED` خارج می‌شود). مراحل:

1. حساب را در اپ والکس بررسی کنید: سفارش باز در `BTCUSDT`/`USDTTMN` نمانده باشد و موجودی با انتظار شما بخواند.
2. فایل KILL را **خودتان** پاک کنید (`unstop` تا وقتی فایل هست اجرا نمی‌شود و هرگز آن را خودکار پاک نمی‌کند).
3. اجرا کنید:

   ```bash
   python -m wallex_agent unstop --mode live --i-understand-real-money
   ```

   `unstop` همان شواهد `live --resume` را می‌خواهد (فلگ، `ENABLE_LIVE`، کلید، `DATA_DIR` صریح، selftest، check-live، canary، و عبارت تأیید: از رکورد معتبر `LIVE_ARMED` اگر مسابقه شروع شده باشد، وگرنه تایپ‌شده در ترمینال)، اما با یک کلاینت **فقط‌خواندنی** کار می‌کند: هیچ سفارشی ثبت یا لغو نمی‌کند و معامله را شروع نمی‌کند. تطبیق شروع را فقط‌خواندنی دوباره اجرا می‌کند و برای هر دارایی دفتر، موجودی صرافی و کمبود را چاپ می‌کند. **فقط اگر** هیچ سفارش حل‌نشده، سفارش رهاشدهٔ مشکوک، سفارش باز ناشناخته یا کمبودی نباشد، دفتر را به حالتی که واقعیت‌ها نشان می‌دهند (مثلاً READY یا BTC_HELD) می‌برد و `operator_unstop` را در audit ثبت می‌کند. چیز دیگری را پاک نمی‌کند. وگرنه با کد `5` خارج می‌شود و چیزی تغییر نمی‌کند.
4. ربات را دوباره اجرا کنید (`live --stage full --i-understand-real-money`، یا روی VPS `systemctl start`).

اگر مسابقه تمام شده باشد، `unstop` رد می‌شود (STOPPED پایان عادی اجراست).

## ۱۱. پس از پایان

```bash
python -m wallex_agent status --mode live
python -m wallex_agent report --mode live
```

- `report` گزارش را آفلاین بازسازی می‌کند و در فایل جداگانهٔ `state/live/runs/live-<id>/final_report.rebuilt.json` می‌نویسد؛ `final_report.json` خودِ ربات **دست نمی‌خورد**. خلاصه‌ای با `value` (ارزش نهایی پس از هزینهٔ خروج)، `pnl` و جملهٔ هدف چاپ می‌کند، مثلاً `"target_statement": "Target 2x NOT reached: final/initial = 0.993x net of exit costs"` (این نمونه از اجرای paper است). ارزش در بازسازی همیشه «تأییدنشده با صرافی» برچسب می‌خورد.
- در گزارش ربات، اگر `value.verified_against_exchange` برابر `true` نیست (و برچسب `valuation_unverified` آمده)، عدد ارزش از دفتر خود ربات است و صرافی در پایان آن را تأیید نکرده است؛ موجودی واقعی را در اپ والکس ببینید.
- این‌ها را نگه دارید یا برای داوری بفرستید (هیچ‌کدام کلید ندارند): `final_report.json`، `audit.jsonl` و `trace.txt`. هر تصمیم غیر HOLD در `round_trips.decisions` با سفارش‌ها، پرشدن‌ها، کارمزد، موجودی، ارزش، افت، فرضیه و نتیجه‌اش آمده است.
- فایل `state/LIVE_ARMED` را پاک کنید.
- اگر دیگر لازم ندارید، کلید API را در سایت والکس حذف کنید.
- برای یک اجرای جدید در آینده، پوشهٔ `state/live` را جابه‌جا کنید (آرشیو)، **پاکش نکنید**.

---

## جدول «اگر X شد چه کنم»

| وضعیت | علت محتمل | اقدام |
|---|---|---|
| `status` نشان می‌دهد `"state": "STOPPED"` | توقف ایمنی، kill، پایان مسابقه، `late_accept`، یا افت بیش از `MAX_DRAWDOWN_PCT` | دلیل (با عدد) را در `stop.reason` و `last_transition` ببینید. اگر مسابقه تمام نشده: علت را در حساب بررسی و برطرف کنید، فایل KILL را (اگر هست) خودتان پاک کنید و `unstop --mode live --i-understand-real-money` را اجرا کنید (بخش ۱۰). `unstop` فقط وقتی دفتر را از STOPPED خارج می‌کند که تطبیق فقط‌خواندنی کاملاً تمیز باشد. اگر تمیز نشود (مثلاً پس از معاملهٔ دستی یا `late_accept`)، تصمیم انسانی لازم است: پوشهٔ `state/live` را آرشیو کنید (پاک نکنید) و یک اجرای تازه شروع کنید. توجه: اجرای تازه دفتر را با `INITIAL_TMN` **تومان** شروع می‌کند و تتر/بیت‌کوین موجود را «اضافه» می‌بیند و معامله‌اش نمی‌کند. موجودی TMN واقعی هم باید دست‌کم `INITIAL_TMN` باشد، وگرنه دوباره STOPPED می‌شود. ساعت ۲۴ ساعته هم از نو شروع می‌شود (و شاید لازم باشد `COMPETITION_HOURS` را کم کنید). |
| alert با `needs_manual_reconcile` | چیزی در صرافی با دفتر ربات تناقض دارد: سفارش باز ناشناخته، معاملهٔ توضیح‌ناپذیر پس از یک سفارش رهاشده، یا کمبود دارایی (با عدد) | ربات را دوباره راه نیندازید. با `trace --mode live` و رکوردهای `alert` در audit، شناسه‌های سفارش را با تاریخچهٔ والکس مقایسه کنید. سفارش‌های باز را در اپ ببینید و در صورت نیاز دستی لغو کنید (ربات سفارشی را که مطمئن نیست مال خودش است لغو نمی‌کند). پس از رفع علت، `unstop`. |
| alert با `late_accept` | صرافی سفارشی را که ربات «رهاشده» می‌دانست بعداً پذیرفت یا اجرا کرد | ربات آن سفارش را (اگر باز بود) لغو کرده و STOPPED است. دلیل، مقدار اجراشده و دفتر در برابر صرافی را نشان می‌دهد. پرشدن آن سفارش وارد دفتر نمی‌شود، پس `unstop` رد خواهد شد: در اپ والکس بررسی کنید و اگر لازم بود دفتر را آرشیو و اجرای تازه شروع کنید. |
| کد خروج `3` | دروازه رد کرد؛ هیچ چیزی ارسال نشد | فهرست شرط‌های برآورده‌نشده را بخوانید: |
| | `selftest_fresh` یا `schema_attestation_fresh` | `selftest` و `check-live` را دوباره اجرا کنید. هر دو بی‌خطرند. |
| | `selftest_test_count` | selftest کمتر از ۱۰۰۰ تست اجرا کرده (کل مجموعه نبوده). `selftest` را بدون تغییر در `tests/` دوباره اجرا کنید. |
| | `*_source_hash` | سورس (پکیج، `tests/` یا `pyproject.toml`) عوض شده است. تغییر را برگردانید یا `selftest`، `check-live` و `arm-live` را دوباره اجرا کنید. |
| | `canary_ok` | canary وجود ندارد، بیش از ۷۲ ساعت عمر دارد، یا `ok: false` است. canary را دوباره اجرا کنید. |
| | `data_dir_explicit` | `DATA_DIR` در `.env`، محیط یا `--data-dir` تنظیم نشده است. تنظیمش کنید (ترجیحاً مسیر مطلق). |
| | `no_kill_file` | فایل KILL هست. |
| | `typed_phrase` | عبارت را دقیقاً با حروف بزرگ و بدون فاصلهٔ اضافه تایپ کنید. اگر پیام `stdin is not a TTY` آمد، در یک ترمینال واقعی اجرا کنید. |
| | `first live start must be interactive` | `live --resume` روی دفتری اجرا شد که مسابقه‌اش هنوز شروع نشده. اولین شروع را تعاملی انجام دهید (`live --stage convert ...` یا `live ...` در ترمینال). |
| | `--resume refused: ... LIVE_ARMED ...` | رکورد arm معتبر نیست (متن ساده، کهنه، منقضی، یا برای کد/`INITIAL_TMN`/آدرس/پوشهٔ دیگری). `arm-live` را دوباره اجرا کنید (و اگر کد عوض شده، قبلش `selftest` و `check-live`). |
| | شاهد «dated ... in the future» | ساعت سیستم را درست کنید. |
| کد خروج `4` | kill اجرا شد | سفارش‌ها لغو شده‌اند و چیزی فروخته نشده. برای ادامه: بخش ۱۰ (`unstop`). |
| کد خروج `5` | توقف ایمنی: اختلاف دفتر و حساب، تناقض، `late_accept`، ۵ خطای پیاپی چرخه، افت سرمایه، یا ربات از قبل STOPPED بوده است | دلیل (با عدد) در خط `status:` و در `stop` در خروجی `status` آمده. ردیف STOPPED را ببینید. |
| کد خروج `6` | پیش‌شرط برقرار نیست | `another instance is running: ... instance.lock is locked` یعنی فرایند دیگری با همین `DATA_DIR` در حال اجراست (قفل سیستم‌عامل؛ وقتی آن فرایند بمیرد، سیستم‌عامل خودکار آزادش می‌کند و فایل `instance.lock` عمداً باقی می‌ماند). در Task Manager یا با `ps` بررسی کنید. اگر `kill` با کد ۶ و پیام `NO wallex-agent ... store` خارج شد، `DATA_DIR` ترمینال شما با ربات فرق دارد (بخش ۱۰). برای canary یا کلید، مرحله‌های ۴ و ۵ را ببینید. |
| کد خروج `7` | در شروع، شبکه یا صرافی برای یک خواندن ضروری در دسترس نبود، یا پیش‌پرواز اتصال رد شد (`preflight failed, no order was sent`؛ هیچ سفارشی ارسال نشد) | `net-check` را اجرا کنید (مرحلهٔ ۰-۱): دستهٔ خطا و راهنمای یک‌خطی را می‌گوید. اینترنت و پروکسی را بررسی کنید. وقتی `net-check` پاس شد، همان فرمان `live` را دوباره اجرا کنید (روی systemd، سرویس پس از ۳۰ ثانیه خودش دوباره تلاش می‌کند). |
| کد خروج `1` و `AuditChainError` | فایل `audit.jsonl` دستکاری یا خراب شده است. ربات زنجیرهٔ شکسته را ادامه نمی‌دهد. | فایل اصلی را برگردانید. ویرایش دستی audit ممنوع است. اگر برگرداندن ممکن نیست، تصمیم انسانی لازم است. |
| کلید منقضی یا حذف شد (`AuthError`، HTTP 401/403) | ربات موجودی و کارمزد را نمی‌بیند، پس **معامله نمی‌کند** و در هر چرخه `market_facts_unknown` ثبت می‌کند. لغو سفارش هم ممکن نیست. | در والکس کلید جدید با همان مجوزها و همان IP مجاز بسازید. `.env` را به‌روز کنید. ربات را با Ctrl+C متوقف کنید. `check-live` را اجرا کنید. سپس `live --stage full --i-understand-real-money` (شروع دوباره، که خودش reconcile می‌کند). |
| اینترنت وسط اجرا قطع شد | خواندن‌ها چند بار با تأخیر تکرار می‌شوند. چرخه‌ها بدون معامله ادامه می‌یابند. سفارش با نتیجهٔ نامعلوم **هرگز دوباره ارسال نمی‌شود** و بعداً از روی شواهد صرافی حل می‌شود. هشدار `unknown_outcome` در audit دستهٔ خطا (`category`) و یک راهنما (`hint`) دارد. | کاری لازم نیست. بعد از وصل شدن، `status` را ببینید. اگر ربات خارج شده بود، دوباره اجرا کنید. |
| خطای `SSLError`/`UNEXPECTED_EOF_WHILE_READING` یا `tls_eof_no_close_notify` | پروکسی `CONNECT` را پذیرفته ولی اتصال بالادستی‌اش به والکس در میانهٔ دست‌دهی TLS قطع شده است (README بخش ۱۱) | `net-check --repeat 3`. اگر گاه‌به‌گاه است، تکرار خودکار خواندن‌ها کافی است؛ اگر مکرر است، پروکسی یا مسیر دیگری انتخاب کنید. |
| کرش، قطع برق یا بسته شدن پنجره | — | از همان پوشه یکی از این دو را اجرا کنید: (الف) `python -m wallex_agent live --stage full --i-understand-real-money` (عبارت پرسیده می‌شود و شاهدها باید ≤ ۲۴ ساعت باشند)؛ (ب) `python -m wallex_agent live --resume --i-understand-real-money` (نیاز به رکورد معتبر و منقضی‌نشدهٔ `LIVE_ARMED`؛ شاهدهای selftest/check-live تا ۴۸ ساعت پذیرفته می‌شوند چون مسابقه شروع شده است). در هر دو حالت ابتدا reconcile انجام می‌شود (شامل بررسی دوبارهٔ همهٔ سفارش‌های رهاشده)، ساعت مسابقه **ادامه** پیدا می‌کند و هیچ سفارشی دوبار فرستاده نمی‌شود. |
| Ctrl+C اشتباهی زده شد | — | مثل ردیف کرش. وضعیت قابل ادامه است. |
| یک سفارش خرید یا فروش نیمه‌پر مانده | سفارش LIMIT تهاجمی کامل پر نشده است | خودکار: پس از `ORDER_TIMEOUT_S` (۲۰ ثانیه) لغو و بررسی می‌شود و بخش پرشده در دفتر ثبت می‌شود. |
| canary پر شد | بخش ۵ | canary را دوباره اجرا کنید. |
| `diagnose` می‌گوید `NO` | اسپرد یا عمق غیرعادی | صبر کنید. live را شروع نکنید. |
