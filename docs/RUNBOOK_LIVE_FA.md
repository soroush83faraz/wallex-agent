# دستورالعمل اجرای واقعی (Live Runbook)

این سند روال دقیق و مرحله‌به‌مرحلهٔ اجرای ربات با **پول واقعی** است. برای هر مرحله آمده است که چه می‌کند، چه خروجی و کد خروجی انتظار می‌رود، و اگر نتیجه چیز دیگری بود چه باید کرد.

> **هشدار صادقانه:** مراحل `check-live`، `canary` و `live` هرگز با یک کلید واقعی اجرا نشده‌اند. هیچ سفارش واقعی‌ای تا امروز توسط این پروژه ثبت نشده است. خروجی‌های مورد انتظار این مراحل **از روی کد** نوشته شده‌اند و در تست‌ها با پاسخ‌های mock بررسی شده‌اند، نه با والکس واقعی. خروجی مراحل `diagnose`، `paper` و `selftest` واقعی هستند (۲۰۲۶-۱۰-۲۲). ([`TEST_STATUS_FA.md`](TEST_STATUS_FA.md))

همهٔ فرمان‌ها از داخل پوشهٔ پروژه و با venv فعال اجرا می‌شوند. روی ویندوز (PowerShell) و لینوکس فرمان‌ها یکسان‌اند، مگر جایی که جدا نوشته شده باشد. اگر venv فعال نیست، روی ویندوز به جای `python` بنویسید `.\.venv\Scripts\python.exe`.

## ترتیب کلی

```text
diagnose -> paper (smoke) -> selftest -> check-live -> canary -> [arm-live] -> live --stage convert
        -> (بررسی در اپ والکس) -> live --stage full -> پایش (status / trace) -> report
```

## ۰. چک‌لیست پیش از پرواز

- [ ] پایتون ۳.۱۰ یا جدیدتر و venv ساخته شده است. نصب با `pip install -e ".[dev]"` انجام شده، چون `selftest` به pytest نیاز دارد.
- [ ] پروژه یک پوشهٔ کامل سورس است (پوشهٔ `tests/` وجود دارد) و **در طول مسابقه هیچ فایل `.py` تغییر نمی‌کند.** هر تغییری هش سورس را عوض می‌کند و دروازهٔ live تا اجرای دوبارهٔ `selftest` و `check-live` بسته می‌ماند. پس وسط کار `git pull` نکنید.
- [ ] فایل `.env` آماده است:
  - `WALLEX_API_KEY=...` (بخش ۱۰ README را ببینید)
  - `ENABLE_LIVE=true`
  - `INITIAL_TMN` درست است
  - `PROFILE` آگاهانه انتخاب شده (و اگر `competition` است، خط `MAX_EXPOSURE_FRACTION` حذف شده؛ README بخش ۴-۱)
  - **`FINAL_POLICY` از قبل انتخاب شده است** (README بخش ۴-۲)
  - `WALLEX_USE_ENV_PROXY` با IP مجاز کلید سازگار است
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
| کد خروج `7` و `public API unavailable` | شبکه، DNS یا فیلترینگ. اگر دسترسی فقط با پروکسی ممکن است، `WALLEX_USE_ENV_PROXY=true` (README بخش ۱۱). |

## ۲. `paper` — تست دود

```bash
python -m wallex_agent paper --seconds 300 --interval 10
```

انتظار: کد خروج `0` و `status: session_end`، یک سفارش `CONVERT_TMN_USDT` پرشده، و `hash chain OK`. نمونهٔ کامل خروجی در README بخش ۷ آمده است. این مرحله ثابت می‌کند که حلقهٔ ایجنت روی دادهٔ واقعی همین دستگاه کار می‌کند. هیچ سفارشی به والکس نمی‌رود.

## ۳. `selftest` — شهادت دادن به همین سورس

```bash
python -m wallex_agent selftest
```

کل مجموعهٔ تست را روی **همین سورس** اجرا می‌کند و `state/selftest.json` را می‌نویسد که شامل هش سورس است. روی سیستم سازنده حدود ۴۵ ثانیه طول کشید. خروجی واقعی:

```text
running the project's test suite (this attests THIS source tree for live trading)...
{
  "at": "2026-10-22T19:50:46Z",
  "duration_s": 43.813,
  "failures": 0,
  "passed": true,
  "reason": "all tests passed",
  "source_hash": "9fa08483a4bdf939e50ef21ea90dca8e99c0611b2262b8dac234696867d1943f",
  "tests": 2241
}
```

- کد خروج `0` یعنی `passed: true`. این شاهد **۲۴ ساعت** برای شروع تازه و **۴۸ ساعت** برای ادامهٔ یک مسابقهٔ شروع‌شده اعتبار دارد.
- کد خروج `1` یعنی تستی شکست خورده، pytest نصب نیست (`pip install -e ".[dev]"`)، یا پوشهٔ `tests/` پیدا نشده است. علت در `reason` نوشته شده. تا این مرحله سبز نشود، live ممکن نیست.

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
| کد `6`، `ok: false` و `cancel could not be verified` | سفارش ممکن است هنوز باز باشد. در اپ والکس بررسی کنید و اگر باز است **دستی لغوش کنید**. سپس `status --mode live` و دوباره `canary` را اجرا کنید. برای این کار `kill` را **به کار نبرید**، چون `kill` دفتر live را برای همیشه STOPPED می‌کند. |
| کد `3` | دروازه رد کرد و هیچ چیزی ارسال نشد. شرط‌های برآورده‌نشده فهرست شده‌اند. |
| کد `5` یا `7` و `canary refused: startup reconciliation not ok` | هنگام مقایسهٔ دفتر با حساب مشکلی دیده شد: کمبود TMN نسبت به `INITIAL_TMN`، سفارش باز ناشناخته، یا نبودِ شبکه. جدول پایانی را ببینید. |

> canary از همان دفتر live (`state/live/state.db`) استفاده می‌کند ولی ساعت مسابقه را شروع **نمی‌کند**.

## ۶. `arm-live` — (اختیاری روی ویندوز، لازم برای systemd و `--resume`)

```bash
python -m wallex_agent arm-live --i-understand-real-money
```

همهٔ دروازه‌ها (شامل canary) را بررسی می‌کند، عبارت **`START LIVE 5000000`** را در ترمینال می‌پرسد و فایل `state/LIVE_ARMED` را با همین عبارت می‌نویسد. خروجی (از روی کد):

```text
REAL MONEY. Type exactly 'START LIVE 5000000' to continue: START LIVE 5000000
armed: state\LIVE_ARMED written. systemd (ConditionPathExists) may now start `live --resume`. Delete the file to disarm.
```

با وجود این فایل، فرمان `live --resume --i-understand-real-money` بدون پرسیدن عبارت شروع می‌شود. این برای شروع دوباره پس از کرش مفید است. برای خلع سلاح، فایل را پاک کنید. اگر `INITIAL_TMN` را عوض کردید، دوباره arm کنید.

## ۷. `live --stage convert` — فقط تبدیل TMN به USDT

```bash
python -m wallex_agent live --stage convert --i-understand-real-money
```

همهٔ دروازه‌ها را بررسی می‌کند و عبارت `START LIVE 5000000` را می‌پرسد. سپس:

1. دفتر را با حساب مقایسه می‌کند (reconcile).
2. **ساعت ۲۴ ساعتهٔ مسابقه را شروع می‌کند.**
3. فقط مرحلهٔ تبدیل را اجرا می‌کند: یک یا چند سفارش LIMIT خرید `USDTTMN` با سقف لغزش ۳۰bps، با کارمزد **واقعی** حساب.
4. وقتی وضعیت READY شد، متوقف می‌شود.

شکل خروجی (از روی کد):

```text
REAL MONEY. Type exactly 'START LIVE 5000000' to continue: START LIVE 5000000
LIVE (conversion stage only): run live-xxxxxxxxxxxx. REAL MONEY. Kill switch: `wallex-agent kill` (or create state\KILL).
== LIVE (conversion stage only) ==
run:        live-xxxxxxxxxxxx
status:     stage_done (exit 0) - conversion stage complete: state READY (run `live --stage full` to trade)
...
report:     state\live\runs\live-xxxxxxxxxxxx\partial_report.json
audit:      state\live\runs\live-xxxxxxxxxxxx\audit.jsonl (hash chain OK)
```

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
| `ledger_exchange_mismatch` | دفتر ربات با موجودی حساب نمی‌خواند. |
| `needs_manual_reconcile` | تناقض بین ربات و صرافی؛ ربات STOPPED شده است. |
| `cycle_exception` | خطای یک چرخه. پنج خطای پیاپی به توقف ایمنی می‌انجامد. |

`status` و `trace` از یک کپی فقط‌خواندنی می‌خوانند و کنار ربات در حال اجرا بی‌خطرند.

## ۱۰. توقف اضطراری

| روش | چه می‌کند | بعد از آن |
|---|---|---|
| **Ctrl+C** در پنجرهٔ ربات (روی ویندوز Ctrl+Break هم کار می‌کند) | گام جاری تمام می‌شود، سفارش‌های باز **لغو** می‌شوند (فقط لغو)، `partial_report.json` نوشته می‌شود و کد خروج `130` است. وضعیت STOPPED **نمی‌شود**. | قابل ادامه است: همان فرمان `live` را دوباره اجرا کنید (یا `live --resume ...`). |
| **`kill`** از ترمینال دوم | فایل `state/KILL` را می‌سازد. ربات در حال اجرا ظرف حدود یک ثانیه (در حالت انتظار) یا در شروع چرخهٔ بعد، سفارش‌های بازش را لغو می‌کند، **STOPPED** می‌شود و با کد `4` خارج می‌شود. تا وقتی KILL هست، هیچ حالتی سفارش تازه نمی‌فرستد. | **برای این دفتر live نهایی است.** راه برگشتی وجود ندارد (پایین را ببینید). |
| **`kill --cancel-open --mode live`** | مثل بالا، و اگر ربات در حال اجرا **نیست** (مثلاً بعد از کرش)، سفارش‌های باز ثبت‌شده در دفتر **همین ربات** را با یک کلاینت «فقط لغو» لغو می‌کند. به سفارش‌های دیگر شما دست نمی‌زند. | دفتر STOPPED می‌شود. کد `0`، یا `5` اگر سفارشی باز ماند. |

```bash
python -m wallex_agent kill --reason "manual emergency stop"
python -m wallex_agent kill --cancel-open --mode live --reason "bot crashed with an open order"
```

**چه چیزی فروخته نمی‌شود:** هیچ‌کدام از این روش‌ها **هیچ چیزی نمی‌فروشند.** فقط سفارش‌های باز لغو می‌شوند. BTC و USDT شما همان‌طور که هست می‌ماند. `FINAL_POLICY` فقط در پایان عادی مسابقه اجرا می‌شود، نه هنگام kill یا Ctrl+C.

**صاف کردن دستی موقعیت در اپ والکس** (اگر می‌خواهید):

1. ابتدا مطمئن شوید ربات متوقف شده است (`status --mode live` → `STOPPED`، یا پنجرهٔ آن بسته است).
2. در بخش «سفارش‌های باز» بررسی کنید که سفارش بازی در `BTCUSDT` و `USDTTMN` نمانده باشد.
3. در بازار اسپات **BTC/USDT** مقدار BTC را بفروشید.
4. اگر تومان می‌خواهید، در بازار **USDT/TMN** تتر را بفروشید.

بعد از هر معاملهٔ دستی، **این دفتر live را دوباره راه‌اندازی نکنید.** دفتر ربات دیگر با حساب نمی‌خواند و ربات با `needs_manual_reconcile` متوقف می‌شود، که رفتار درست و مورد انتظار است.

**چرا KILL نهایی است:** بعد از اینکه ربات KILL را اجرا کرد، دفتر live در حالت STOPPED است. اگر فایل `state/KILL` را پاک کنید، `live` با کد `5` (`bot is STOPPED`) خارج می‌شود. پیام «Delete the file to allow trading again» فقط وقتی درست است که هیچ رباتی هنوز KILL را اجرا نکرده باشد. هیچ فرمانی برای خارج کردن یک دفتر از STOPPED وجود ندارد (بخش «اگر X شد» را ببینید).

## ۱۱. پس از پایان

```bash
python -m wallex_agent status --mode live
python -m wallex_agent report --mode live
```

- `report` گزارش را آفلاین بازسازی می‌کند و `state/live/runs/live-<id>/final_report.json` را بازنویسی می‌کند. خلاصه‌ای با `value` (ارزش نهایی پس از هزینهٔ خروج)، `pnl` و جملهٔ هدف چاپ می‌کند، مثلاً `"target_statement": "Target 2x NOT reached: final/initial = 0.993x net of exit costs"` (این نمونه از اجرای paper است).
- این‌ها را نگه دارید یا برای داوری بفرستید (هیچ‌کدام کلید ندارند): `final_report.json`، `audit.jsonl` و `trace.txt`.
- فایل `state/LIVE_ARMED` را پاک کنید.
- اگر دیگر لازم ندارید، کلید API را در سایت والکس حذف کنید.
- برای یک اجرای جدید در آینده، پوشهٔ `state/live` را جابه‌جا کنید (آرشیو)، **پاکش نکنید**.

---

## جدول «اگر X شد چه کنم»

| وضعیت | علت محتمل | اقدام |
|---|---|---|
| `status` نشان می‌دهد `"state": "STOPPED"` | توقف ایمنی، kill، پایان مسابقه، یا افت بیش از `MAX_DRAWDOWN_PCT` | دلیل را در `stop.reason` و `last_transition` ببینید. STOPPED برای این دفتر live **نهایی** است و فرمانی برای «ادامه با زور» وجود ندارد. اگر مسابقه تمام نشده و می‌خواهید ادامه دهید، باید تصمیم انسانی بگیرید: پوشهٔ `state/live` را آرشیو کنید (پاک نکنید) و یک اجرای تازه شروع کنید. توجه: اجرای تازه دفتر را با `INITIAL_TMN` **تومان** شروع می‌کند و تتر/بیت‌کوین موجود را «اضافه» می‌بیند و معامله‌اش نمی‌کند. موجودی TMN واقعی هم باید دست‌کم `INITIAL_TMN` باشد، وگرنه دوباره STOPPED می‌شود. ساعت ۲۴ ساعته هم از نو شروع می‌شود (و شاید لازم باشد `COMPETITION_HOURS` را کم کنید). |
| alert با `needs_manual_reconcile` | چیزی در صرافی با دفتر ربات تناقض دارد: سفارش باز ناشناخته، سفارش «رهاشده» که بعداً پیدا شد، معاملهٔ توضیح‌ناپذیر، یا کمبود دارایی | ربات را دوباره راه نیندازید. با `trace --mode live` و رکوردهای `alert` در audit، شناسه‌های سفارش را با تاریخچهٔ والکس مقایسه کنید. سفارش‌های باز را در اپ ببینید و در صورت نیاز دستی لغو کنید. این حالت عمداً به تصمیم انسانی واگذار شده است. |
| کد خروج `3` | دروازه رد کرد؛ هیچ چیزی ارسال نشد | فهرست شرط‌های برآورده‌نشده را بخوانید: |
| | `selftest_fresh` یا `schema_attestation_fresh` | `selftest` و `check-live` را دوباره اجرا کنید. هر دو بی‌خطرند. |
| | `*_source_hash` | سورس عوض شده است. تغییر را برگردانید یا هر دو را دوباره اجرا کنید. |
| | `canary_ok` | canary وجود ندارد، بیش از ۷۲ ساعت عمر دارد، یا `ok: false` است. canary را دوباره اجرا کنید. |
| | `no_kill_file` | فایل KILL هست. |
| | `typed_phrase` | عبارت را دقیقاً با حروف بزرگ و بدون فاصلهٔ اضافه تایپ کنید. اگر پیام `stdin is not a TTY` آمد، در یک ترمینال واقعی اجرا کنید یا از `--resume` با `LIVE_ARMED` استفاده کنید. |
| | شاهد «dated ... in the future» | ساعت سیستم را درست کنید. |
| کد خروج `4` | kill اجرا شد | سفارش‌ها لغو شده‌اند و چیزی فروخته نشده. این دفتر نهایی است (بخش ۱۰). |
| کد خروج `5` | توقف ایمنی: اختلاف دفتر و حساب، تناقض، ۵ خطای پیاپی چرخه، افت سرمایه، یا ربات از قبل STOPPED بوده است | دلیل در خط `status:` و در `stop` در خروجی `status` آمده. ردیف STOPPED را ببینید. |
| کد خروج `6` | پیش‌شرط برقرار نیست | `instance.lock` یعنی نمونهٔ دیگری با همین `DATA_DIR` در حال اجراست. در Task Manager یا با `ps` بررسی کنید. قفل یک فرایند مرده خودکار آزاد می‌شود. برای canary یا کلید، مرحله‌های ۴ و ۵ را ببینید. |
| کد خروج `7` | در شروع، شبکه یا صرافی برای یک خواندن ضروری در دسترس نبود | `diagnose` را اجرا کنید. اینترنت و پروکسی را بررسی کنید. وقتی دسترسی برگشت، همان فرمان `live` را دوباره اجرا کنید. |
| کد خروج `1` و `AuditChainError` | فایل `audit.jsonl` دستکاری یا خراب شده است. ربات زنجیرهٔ شکسته را ادامه نمی‌دهد. | فایل اصلی را برگردانید. ویرایش دستی audit ممنوع است. اگر برگرداندن ممکن نیست، تصمیم انسانی لازم است. |
| کلید منقضی یا حذف شد (`AuthError`، HTTP 401/403) | ربات موجودی و کارمزد را نمی‌بیند، پس **معامله نمی‌کند** و در هر چرخه `market_facts_unknown` ثبت می‌کند. لغو سفارش هم ممکن نیست. | در والکس کلید جدید با همان مجوزها و همان IP مجاز بسازید. `.env` را به‌روز کنید. ربات را با Ctrl+C متوقف کنید. `check-live` را اجرا کنید. سپس `live --stage full --i-understand-real-money` (شروع دوباره، که خودش reconcile می‌کند). |
| اینترنت وسط اجرا قطع شد | خواندن‌ها چند بار با تأخیر تکرار می‌شوند. چرخه‌ها بدون معامله ادامه می‌یابند. سفارش با نتیجهٔ نامعلوم **هرگز دوباره ارسال نمی‌شود** و بعداً از روی شواهد صرافی حل می‌شود. | کاری لازم نیست. بعد از وصل شدن، `status` را ببینید. اگر ربات خارج شده بود، دوباره اجرا کنید. |
| کرش، قطع برق یا بسته شدن پنجره | — | از همان پوشه یکی از این دو را اجرا کنید: (الف) `python -m wallex_agent live --stage full --i-understand-real-money` (عبارت پرسیده می‌شود و شاهدها باید ≤ ۲۴ ساعت باشند)؛ (ب) `python -m wallex_agent live --resume --i-understand-real-money` (نیاز به `LIVE_ARMED`؛ شاهدهای selftest/check-live تا ۴۸ ساعت پذیرفته می‌شوند چون مسابقه شروع شده است). در هر دو حالت ابتدا reconcile انجام می‌شود، ساعت مسابقه **ادامه** پیدا می‌کند و هیچ سفارشی دوبار فرستاده نمی‌شود. |
| Ctrl+C اشتباهی زده شد | — | مثل ردیف کرش. وضعیت قابل ادامه است. |
| یک سفارش خرید یا فروش نیمه‌پر مانده | سفارش LIMIT تهاجمی کامل پر نشده است | خودکار: پس از `ORDER_TIMEOUT_S` (۲۰ ثانیه) لغو و بررسی می‌شود و بخش پرشده در دفتر ثبت می‌شود. |
| canary پر شد | بخش ۵ | canary را دوباره اجرا کنید. |
| `diagnose` می‌گوید `NO` | اسپرد یا عمق غیرعادی | صبر کنید. live را شروع نکنید. |
