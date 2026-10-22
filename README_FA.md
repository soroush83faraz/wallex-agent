# ایجنت معاملاتی والکس (wallex-agent) — راهنمای فارسی

نسخهٔ `0.4.0` · پایتون ۳.۱۰ یا جدیدتر · فقط کتابخانهٔ استاندارد (pytest فقط برای تست)

این راهنما برای کسی نوشته شده که می‌خواهد ربات را روی ویندوز (و بعداً روی یک VPS لینوکسی) برای مسابقهٔ ۲۴ ساعتهٔ «Agentic AI Trading» والکس اجرا کند. هدف این فایل این است که در حدود ۱۰ دقیقه به یک دموی آفلاین و یک تست دود (smoke test) ۵ دقیقه‌ای برسید. جزئیات در این فایل‌ها آمده‌اند:

| فایل | موضوع |
|---|---|
| [`docs/RUNBOOK_LIVE_FA.md`](docs/RUNBOOK_LIVE_FA.md) | روال مرحله‌به‌مرحلهٔ اجرای واقعی (live)، توقف اضطراری، جدول «اگر X شد چه کنم» |
| [`docs/VPS_SYSTEMD_FA.md`](docs/VPS_SYSTEMD_FA.md) | نصب روی VPS لینوکسی با systemd |
| [`docs/ARCHITECTURE_FA.md`](docs/ARCHITECTURE_FA.md) | معماری، حلقهٔ ایجنت، ماشین حالت، ایمنی سفارش‌ها |
| [`docs/API_UNCERTAINTIES_FA.md`](docs/API_UNCERTAINTIES_FA.md) | رفتارهای تأییدنشدهٔ API والکس و اینکه کد با هرکدام چه می‌کند |
| [`docs/TEST_STATUS_FA.md`](docs/TEST_STATUS_FA.md) | گزارش صادقانهٔ تست‌ها: چه چیزی تست شده و چه چیزی هرگز اجرا نشده |
| [`docs/BASELINE_ANALYSIS_FA.md`](docs/BASELINE_ANALYSIS_FA.md) | ضعف‌های نسخهٔ اولیهٔ `agent.py` و کاری که این پروژه برای هرکدام کرد |

---

## ۱. این پروژه چیست

- یک ربات معاملهٔ **اسپات** (بدون اهرم) برای والکس است. سرمایهٔ اولیه را به **تومان (TMN)** می‌گیرد، ابتدا در بازار `USDTTMN` آن را به **تتر (USDT)** تبدیل می‌کند و بعد فقط در بازار `BTCUSDT` معامله می‌کند.
- تصمیم‌گیری یک حلقهٔ عامل‌محور (agentic) است: `observe → plan → tool → action → verify → reflect`. ایجنت ابزار انتخاب می‌کند، فرضیه می‌سازد، هزینه‌ها را حساب می‌کند و از نتیجهٔ کارش (و حتی از تصمیم‌های «کاری نکن») یاد می‌گیرد. جزئیات در [`docs/ARCHITECTURE_FA.md`](docs/ARCHITECTURE_FA.md).
- سه حالت اجرا دارد: `backtest` (آفلاین)، `paper` (دفتر سفارش واقعی، سفارش شبیه‌سازی‌شده) و `live` (پول واقعی، پشت چند دروازهٔ ایمنی).
- هیچ کدی برای برداشت یا انتقال وجه ندارد. مسیرهای برداشت در کلاینت صریحاً ممنوع شده‌اند و این موضوع تست شده است.

## ۲. این پروژه چه **نیست** (حتماً بخوانید)

- **هیچ تضمین سودی وجود ندارد.** حالت پیش‌فرض ایجنت «کاری نکن» (HOLD) است و فقط وقتی معامله می‌کند که سود مورد انتظارش **پس از کسر همهٔ هزینه‌ها** از یک حد مشخص بیشتر باشد.
- **دو برابر شدن سرمایه در ۲۴ ساعت بسیار بعید است.** برای این کار باید BTC در یک روز حدود ۱۰۰٪ بالا برود و ربات دقیقاً در جای درست باشد. ربات هیچ‌وقت برای رسیدن به هدف ۲x معاملهٔ پرریسک را به خودش تحمیل نمی‌کند.
- **هزینه‌ها واقعی‌اند و دوطرفه.** هر معامله کارمزد و اسپرد دارد، هم در خرید و هم در فروش:
  - یک دور رفت‌وبرگشت `TMN → USDT → TMN` بسته به کارمزد حساب شما (مثلاً ۰٫۲٪ تا ۰٫۳۵٪ در هر طرف) و اسپرد، تقریباً **۰٫۴٪ تا ۰٫۷٪** هزینه دارد.
  - در اجرای paper با کارمزد فرضی ۰٫۳۵٪، ارزش حساب بلافاصله پس از تبدیل حدود **‎-0.70%‎** نشان داده شد (یعنی ارزش فروش فوری، بعد از کسر کارمزد خروج). این ضرر استراتژی نیست؛ کف هزینه‌ای است که هر معامله‌ای باید از آن عبور کند.
  - یک دور خرید و فروش BTC هم دوبار کارمزد و یک اسپرد هزینه دارد.
- **سرمایه کوچک است.** ۵٬۰۰۰٬۰۰۰ تومان با نرخ حدود ۲۷۲٬۰۰۰ تومان برای هر تتر (۲۰۲۶-۱۰-۲۲) تقریباً **۱۸ تتر** است.
- **بازار BTCUSDT والکس در مقایسه با صرافی‌های جهانی کم‌عمق است.** در بررسی‌های ما عمق قابل مشاهده برای حدود ۱۸ تتر کافی بود، اما عمق و اسپرد می‌توانند سریع تغییر کنند.
- **حالت live هرگز با کلید واقعی یا سفارش واقعی اجرا نشده است.** همهٔ مسیرهای live فقط با شبیه‌ساز و پاسخ‌های mock تست شده‌اند. گزارش کامل در [`docs/TEST_STATUS_FA.md`](docs/TEST_STATUS_FA.md) است.

## ۳. نصب

پیش‌نیاز: پایتون ۳.۱۰ یا جدیدتر. برای اینکه دستور `selftest` (که برای live لازم است) کار کند، پروژه باید به‌صورت یک پوشهٔ کامل سورس (شامل `tests/`) باشد، نه یک بستهٔ نصب‌شده.

در پوشهٔ پروژه، **ویندوز (PowerShell)**:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
Copy-Item .env.example .env
```

اگر لانچر `py` نصب نیست، به‌جای خط اول بنویسید `python -m venv .venv`. اگر PowerShell اجرای `Activate.ps1` را مسدود کرد، این دستور فقط برای همان پنجره اجازه می‌دهد:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
```

راه دیگر این است که اصلاً venv را فعال نکنید و همه‌جا به جای `python` بنویسید `.\.venv\Scripts\python.exe`.

**لینوکس (bash)**:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
cp .env.example .env
```

بررسی نصب:

```bash
python -m wallex_agent --version
```

خروجی باید `wallex-agent 0.4.0` باشد. دستور `wallex-agent` (بعد از فعال کردن venv) دقیقاً معادل `python -m wallex_agent` است.

> همهٔ دستورها را **از داخل پوشهٔ پروژه** اجرا کنید. فایل `.env` و پوشهٔ `state` به‌صورت پیش‌فرض نسبت به پوشهٔ جاری خوانده/نوشته می‌شوند. (گزینه‌های سراسری `--env-file` و `--data-dir` باید **قبل** از نام فرمان بیایند، مثلاً `python -m wallex_agent --data-dir D:\wa-state status`.)

## ۴. پیکربندی (`.env`)

ترتیب اولویت: متغیر محیطی واقعی سیستم > فایل `.env` > پروفایل > مقدار پیش‌فرض. هر مقداری که در `.env` نوشته شود، **مقدار پروفایل را باطل می‌کند** (پایین‌تر را ببینید).

| متغیر | پیش‌فرض | معنی |
|---|---|---|
| `WALLEX_API_KEY` | خالی | کلید API. فقط برای `check-live`، `canary`، `arm-live`، `live` و `kill --cancel-open` لازم است. در هیچ خروجی، لاگ یا گزارشی چاپ نمی‌شود. |
| `TRADING_MODE` | `paper` | فقط حالت پیش‌فرضِ `--mode` برای `status`، `report`، `trace` و `kill` است. فرمان `live` همیشه live است و فرمان `paper` همیشه paper؛ این متغیر به‌تنهایی معاملهٔ واقعی را روشن نمی‌کند. |
| `PROFILE` | `conservative` | `conservative` یا `competition` (جدول بعدی). |
| `INITIAL_TMN` | `5000000` | سرمایهٔ اولیه به تومان. در live باید **کمتر یا مساوی موجودی واقعی TMN** حساب باشد. عبارت تأیید (`START LIVE 5000000`) هم از همین عدد ساخته می‌شود. |
| `COMPETITION_HOURS` | `24` | طول مسابقه از لحظهٔ اولین شروع ربات (بین ۰ تا ۱۶۸). |
| `MAX_EXPOSURE_FRACTION` | `0.25` | حداکثر سهمی از ارزش دلاری حساب (USDT + BTC) که می‌تواند BTC باشد. |
| `ENABLE_LIVE` | `false` | فقط وقتی `true` باشد دروازهٔ live اجازه می‌دهد (به‌علاوهٔ بقیهٔ شرط‌ها). |
| `FINAL_POLICY` | `report_only` | با دارایی‌ها در پایان مسابقه چه شود (بخش ۴-۲). |
| `DATA_DIR` | `state` | پوشهٔ وضعیت، شواهد، گزارش‌ها و لاگ‌ها. |
| `API_BASE` | `https://api.wallex.ir` | تغییرش ندهید. کلید فقط به `api.wallex.ir` روی HTTPS فرستاده می‌شود. |
| `WALLEX_USE_ENV_PROXY` | `false` | اگر `true` باشد، پروکسی تنظیم‌شده در `HTTP(S)_PROXY` استفاده می‌شود (بخش ۱۱). |
| `PAPER_FEE_RATE` | `0.0035` | کارمزد **فرضی** هر طرف، فقط برای paper و backtest. حالت live همیشه کارمزد واقعی حساب را از API می‌خواند. |
| `STOP_LOSS_PCT` | `0.015` | حد ضرر سخت روی موقعیت BTC (بر اساس قیمت فروش واقعی، پس از کارمزد خروج). |
| `MAX_DRAWDOWN_PCT` | `0.08` | افت از اوج ارزش حساب که بعد از آن فقط خروج مجاز است و سپس ربات STOPPED می‌شود. |
| `MAX_ROUND_TRIPS` | `4` | حداکثر تعداد دور خرید و فروش BTC. |
| `MIN_NET_EDGE_BPS` | `30` | حداقل سود مورد انتظار **پس از کسر کارمزد، اسپرد و لغزش**، به واحد bps (۱bps = ۰٫۰۱٪). |
| `MAX_SPREAD_BPS` | `40` | اگر اسپرد بیشتر باشد، ورود ممنوع است. |
| `MAX_SLIPPAGE_BPS` | `25` | حداکثر فاصلهٔ قیمت سفارش از بهترین قیمت در BTCUSDT. |
| `MIN_CONFIDENCE` | `0.55` | حداقل اطمینان ایجنت برای ورود. |
| `LOOP_INTERVAL_S` | `30` | فاصلهٔ بین چرخه‌ها در live (ثانیه). در paper با `--interval` تعیین می‌شود. |
| `ORDER_TIMEOUT_S` | `20` | سفارشی که تا این زمان کامل پر نشود، لغو و بررسی می‌شود. |
| `SEED` | `42` | بذر تصادفی برای تکرارپذیری. |

### ۴-۱. پروفایل `conservative` (پیش‌فرض) در برابر `competition`

پروفایل `competition` یک انتخاب **آگاهانه برای ریسک بیشتر** است و هیچ بازدهی را تضمین نمی‌کند. این پروفایل دقیقاً همین هفت مقدار را تغییر می‌دهد:

| پارامتر | conservative | competition |
|---|---|---|
| `max_exposure_fraction` | 0.25 | 0.90 |
| `stop_loss_pct` | 0.015 | 0.03 |
| `max_drawdown_pct` | 0.08 | 0.35 |
| `max_round_trips` | 4 | 6 |
| `min_net_edge_bps` | 30 | 20 |
| `min_confidence` | 0.55 | 0.50 |
| `cooldown_after_loss_s` (فاصلهٔ اجباری بعد از ضرر؛ متغیر `.env` ندارد) | 900 | 600 |

> **دام مهم:** فایل `.env.example` خط `MAX_EXPOSURE_FRACTION=0.25` را دارد. اگر `PROFILE=competition` بگذارید و این خط را نگه دارید، مقدار `.env` برنده می‌شود و سقف موقعیت **۰٫۲۵ می‌ماند، نه ۰٫۹۰**. اگر واقعاً پروفایل competition را می‌خواهید، آن خط را حذف یا با `#` غیرفعال کنید (یا صریحاً `0.90` بنویسید). همین قاعده برای هر پارامتر دیگری که در `.env` بنویسید برقرار است.

حتی با پروفایل competition، ایجنت فقط وقتی BTC می‌خرد که حرکت پیش‌بینی‌شده از مجموع هزینه‌ها (کارمزد خرید + کارمزد فروش + اسپرد + لغزش) **و** حد `min_net_edge_bps` بیشتر باشد. با کارمزد ۰٫۳۵٪ در هر طرف، فقط کارمزد یک دور کامل ۷۰bps است؛ برای همین تعداد معاملات کم خواهد بود و این رفتار عمدی است.

### ۴-۲. سیاست پایان مسابقه (`FINAL_POLICY`) — **قبل از شروع تصمیم بگیرید**

| مقدار | در لحظهٔ پایان (۲۴ ساعت پس از اولین شروع) |
|---|---|
| `report_only` (پیش‌فرض) | سفارش‌های باز لغو می‌شوند و **هیچ چیزی فروخته نمی‌شود**. گزارش نهایی دارایی‌ها را به قیمت فروش فوری (bid) و پس از کسر کارمزد خروج ارزش‌گذاری می‌کند. |
| `flatten_to_usdt` | اگر BTC دارید، به USDT فروخته می‌شود. USDT نگه داشته می‌شود. |
| `flatten_to_tmn` | BTC به USDT و سپس USDT به TMN فروخته می‌شود (یک کارمزد و اسپرد اضافه روی `USDTTMN`). |

نکته‌ها:

- فروش پایانی فقط از مسیر عادی دروازهٔ ریسک و موتور اجرا انجام می‌شود و حداکثر ۱۰ چرخه پس از پایان تلاش می‌کند. اگر اطلاعات بازار نامعلوم باشد یا باقی‌مانده کمتر از حداقل سفارش بازار باشد، همان مقدار باقی می‌ماند.
- اگر اجرا با `kill`، توقف ایمنی یا Ctrl+C تمام شود، سیاست پایانی **اجرا نمی‌شود** (هیچ چیزی فروخته نمی‌شود).
- ربات این سیاست را فقط هنگام شروع می‌خواند. تغییرش وسط اجرا نیاز به توقف و شروع دوباره دارد. پس **قبل از شروع** قوانین ارزیابی مسابقه را بخوانید و تصمیم بگیرید:
  - اگر مسابقه ارزش دارایی‌ها را به قیمت بازار حساب می‌کند، `report_only` یک کارمزد و اسپرد اضافه را حذف می‌کند.
  - اگر مسابقه دارایی نهایی را به TMN یا USDT می‌خواهد، سیاست متناظر را انتخاب کنید.

## ۵. سه حالت اجرا

| حالت | دادهٔ بازار | سفارش‌ها | کلید | فرمان |
|---|---|---|---|---|
| `backtest` | مصنوعی (seed‌دار) یا فایل کندل محلی | شبیه‌سازی‌شده | لازم نیست | `demo`، `backtest` |
| `paper` | دفتر سفارش **واقعی و عمومی** والکس | شبیه‌سازی‌شده؛ هیچ سفارشی به والکس نمی‌رود | لازم نیست | `paper` |
| `live` | واقعی | **واقعی، با پول واقعی** | لازم است | `live` (فقط پس از همهٔ دروازه‌ها) |

در paper، تنها کلاینت شبکه «فقط عمومی» است: کلید ندارد و هر POST/DELETE یا درخواست خصوصی را **قبل از ارسال** رد می‌کند. هر حالت پوشهٔ جداگانه‌ای زیر `state/` دارد و دفترهای حساب با هم مخلوط نمی‌شوند.

## ۶. دموی آفلاین (بدون شبکه، بدون کلید)

```bash
python -m wallex_agent demo
```

این فرمان یک بک‌تست ۲۴ ساعتهٔ **مصنوعی** را اجرا می‌کند و روی سیستم سازنده حدود ۱۵ ثانیه طول کشید. خروجی واقعی (۲۰۲۶-۱۰-۲۲):

```text
== DEMO: offline synthetic 24 h backtest (SYNTHETIC, no network, no key) ==
   SYNTHETIC DATA (seeded random model, not market data); fills SIMULATED; fee ASSUMED
run:        backtest-20261022T194148Z-cf3f95
status:     deadline (exit 0) - competition deadline reached (final_policy=report_only); holdings: 0.00000000396 BTC, 27374.72 TMN, 18.24975892585525 USDT
period:     2023-11-14T22:13:20Z -> 2023-11-15T22:13:20Z (24.0 h)
decisions:  BUY_BTC 2, CONVERT_TMN_USDT 1, HOLD 1435, SELL_BTC 2
orders:     5 {'FILLED': 5}; fills 5; round trips 2 (wins 2, losses 0)
value:      5,000,000 TMN -> 5,002,273 TMN net of exit costs (P&L 2272.76 TMN, 0.0455 %)
target:     Target 2x NOT reached: final/initial = 1.0005x net of exit costs
fees paid:  0.00000037604 BTC, 0.09594171194475 USDT
holdings:   0.00000000396 BTC, 27374.72 TMN, 18.24975892585525 USDT
report:     state\backtest\runs\backtest-20261022T194148Z-cf3f95\final_report.json
trace:      state\backtest\runs\backtest-20261022T194148Z-cf3f95\trace.txt
audit:      state\backtest\runs\backtest-20261022T194148Z-cf3f95\audit.jsonl (hash chain OK)
Past (simulated) results do not predict future results; 2x is not guaranteed.
```

این عددها روی دادهٔ **مصنوعی** به دست آمده‌اند و هیچ چیزی دربارهٔ عملکرد واقعی نمی‌گویند. تاریخ‌های ۲۰۲۳ ساعت شبیه‌سازی‌شدهٔ دمو هستند. حدود ۲۷ هزار تومان باقی‌مانده هم طبیعی است: ربات برای کارمزد و لغزش جا نگه می‌دارد و باقی‌مانده کمتر از حداقل سفارش `USDTTMN` (۵۰٬۰۰۰ تومان) است.

برای دیدن وضعیت و ردِ تصمیم‌ها (دمو در حالت backtest است، پس `--mode backtest` لازم است):

```bash
python -m wallex_agent status --mode backtest
python -m wallex_agent trace --mode backtest --last 2
```

## ۷. تست دود ۵ دقیقه‌ای (paper، داده‌های واقعی عمومی)

اول بررسی کنید که دسترسی به API عمومی والکس دارید:

```bash
python -m wallex_agent diagnose
```

اگر خروجی با `Trading feasible now: YES` تمام شد (کد خروج ۰)، ادامه دهید. اگر کد خروج ۷ بود و پیام `public API unavailable` آمد، بخش ۱۱ (پروکسی) را ببینید.

سپس تست ۵ دقیقه‌ای را اجرا کنید:

```bash
python -m wallex_agent paper --seconds 300 --interval 10
```

نمونهٔ خروجی واقعی یک اجرای ۶۰ ثانیه‌ای (`--seconds 60`، ۲۰۲۶-۱۰-۲۲، از طریق پروکسی):

```text
PAPER session paper-20261022T194440Z-d32317: 60.0s, a cycle every 10.0s, live PUBLIC books, simulated orders (nothing is ever sent to Wallex). Ctrl+C stops cleanly.
== PAPER (live public books, simulated orders) ==
   SIMULATED orders/fills (paper); fee ASSUMED at 0.0035
run:        paper-20261022T194440Z-d32317
status:     session_end (exit 0) - session of 60s finished
period:     2026-10-22T19:44:44Z -> 2026-10-22T19:45:51Z (0.0186 h)
decisions:  CONVERT_TMN_USDT 1, HOLD 4
orders:     1 {'FILLED': 1}; fills 1; round trips 0 (wins 0, losses 0)
value:      5,000,000 TMN -> 4,964,754 TMN net of exit costs (P&L -35245.81 TMN, -0.7049 %)
target:     Target 2x NOT reached: final/initial = 0.993x net of exit costs
fees paid:  0.064015 USDT
holdings:   28832.87 TMN, 18.225985 USDT
report:     state\paper\runs\paper-20261022T194440Z-d32317\final_report.json
trace:      state\paper\runs\paper-20261022T194440Z-d32317\trace.txt
audit:      state\paper\runs\paper-20261022T194440Z-d32317\audit.jsonl (hash chain OK)
Past (simulated) results do not predict future results; 2x is not guaranteed.
```

اجرا موفق است اگر:

- کد خروج `0` و وضعیت `session_end` باشد؛
- یک سفارش `CONVERT_TMN_USDT` پر شده باشد؛
- بقیهٔ تصمیم‌ها معمولاً `HOLD` باشند؛
- در خط audit عبارت `hash chain OK` آمده باشد.

عدد ‎-0.70%‎ همان هزینهٔ تبدیل و خروج است که در بخش ۲ توضیح داده شد. با Ctrl+C هر زمان می‌توانید اجرا را تمیز متوقف کنید (کد خروج ۱۳۰).

## ۸. اجرای کامل ۲۴ ساعته

**Paper ۲۴ ساعته** (هیچ سفارش واقعی‌ای فرستاده نمی‌شود):

```bash
python -m wallex_agent paper --seconds 86700 --interval 30
```

عدد `86700` کمی بیشتر از ۲۴ ساعت (۸۶٬۴۰۰ ثانیه) است تا پایان مسابقه (و `FINAL_POLICY`) قبل از پایان جلسه رخ دهد و وضعیت نهایی `deadline` باشد. روی ویندوز، «Sleep» کامپیوتر را برای این مدت غیرفعال کنید و پنجرهٔ PowerShell را نبندید (بستن پنجره یعنی توقف ناگهانی).

**Live ۲۴ ساعته (پول واقعی):** فرمان اصلی چنین است، اما **فقط** پس از طی کردن کامل [`docs/RUNBOOK_LIVE_FA.md`](docs/RUNBOOK_LIVE_FA.md) (diagnose → paper → selftest → check-live → canary → `--stage convert`):

```bash
python -m wallex_agent live --stage full --i-understand-real-money
```

ربات در live تا ۲۴ ساعت پس از **اولین** شروع live (شامل `--stage convert`) کار می‌کند و خودش متوقف می‌شود. روی VPS، [`docs/VPS_SYSTEMD_FA.md`](docs/VPS_SYSTEMD_FA.md) را ببینید.

## ۹. خواندن خروجی‌ها

ساختار پوشهٔ وضعیت:

```text
state/
  selftest.json, live_schema_attestation.json, live_canary.json   (live gate evidence)
  KILL, LIVE_ARMED                                                (only if created)
  backtest/runs/<run_id>/   state.db, audit.jsonl, trace.txt, final_report.json
  paper/runs/<run_id>/      state.db, audit.jsonl, trace.txt, final_report.json
  live/state.db             (one ledger, kept across restarts)
  live/runs/live-<id>/      audit.jsonl, trace.txt, final_report.json | partial_report.json
```

- **`final_report.json`** وقتی ساخته می‌شود که اجرا تمام شده باشد (`deadline`، `session_end`، پایان داده، `killed`، `stopped`). اگر اجرا قطع شده باشد (Ctrl+C) یا فقط مرحلهٔ تبدیل اجرا شده باشد، نام فایل `partial_report.json` است. کلیدهای مهم:

  | کلید | معنی |
  |---|---|
  | `labels` | برچسب‌های صادقانه، مثل `synthetic_data`، `assumed_fee`، `paper_no_real_orders` |
  | `honest_caveats` | محدودیت‌های همین اجرا، شامل موارد API تأییدنشده |
  | `value.initial_tmn` / `value.final_tmn_net_of_exit_costs` | ارزش اولیه و ارزش نهایی **پس از کسر هزینهٔ خروج** (فروش فوری به bid) |
  | `value.final_tmn_at_mid` | ارزش به قیمت میانه (خوش‌بینانه‌تر؛ فقط برای مقایسه) |
  | `pnl.pct` / `pnl.target_statement` | سود یا زیان درصدی و جملهٔ صریح دربارهٔ رسیدن یا نرسیدن به هدف ۲x |
  | `fees_paid` / `fee_source_counts` | کارمزد پرداختی به تفکیک دارایی؛ `exchange` یعنی از معاملات صرافی خوانده شده و `estimated` یعنی تخمینی |
  | `orders.list` / `fills` / `round_trips` | همهٔ سفارش‌ها با شناسه‌ها، پرشدن‌ها، و دورهای کامل BTC |
  | `equity_curve.max_drawdown_pct` | بیشترین افت ارزش |
  | `end.holdings` / `end.final_policy_applied` | دارایی‌ها در پایان و اینکه سیاست پایانی چه کرد |
  | `decisions_by_action` / `tool_usage` / `memory` | آمار تصمیم‌ها، ابزارهای استفاده‌شده، و حافظهٔ ایجنت (bandit و کالیبراسیون) |

- **`audit.jsonl`**: هر خط یک رکورد JSON با `seq`، `ts`، `run_id`، `strategy_version`، `kind`، `payload`، `prev_hash` و `hash` است. هش هر خط به خط قبلی زنجیر شده؛ هر ویرایش دستی زنجیره را می‌شکند و خلاصهٔ پایان اجرا این را گزارش می‌کند (`hash chain OK` یا `BROKEN at line N`). برخی از `kind`ها: `observe`، `decision`، `tool_call`، `risk_verdict`، `verify`، `outcome`، `reflection`، `fill`، `state_transition`، `alert`، `reconcile`. این فایل را **ویرایش نکنید**.
- **trace**: روایت خوانای چرخه‌ها به ترتیب `OBSERVE → PLAN → TOOL → ACTION → VERIFY → REFLECT`:

  ```bash
  python -m wallex_agent trace --last 3
  python -m wallex_agent trace --decision d000005-cb3e5ebd
  ```

  (شناسهٔ تصمیم را از خروجی `trace` یا از `final_report.json` بردارید. برای حالت‌های دیگر `--mode backtest` یا `--mode live` اضافه کنید.)

- **`status`**: وضعیت لحظه‌ای بدون هیچ رازی. شامل حالت ربات، زمان باقی‌مانده، ارزش، سفارش‌های باز و آنچه برای live کم است (`arm_status.missing`). اجرای آن کنار یک ربات در حال کار بی‌خطر است، چون از یک کپی فقط‌خواندنی می‌خواند.
- **`report`**: گزارش نهایی را آفلاین از روی دیتابیس بازسازی و در `final_report.json` همان اجرا **بازنویسی** می‌کند.
- عدد ارزش در `status` (آخرین چرخه) و در گزارش نهایی (ارزش‌گذاری دوباره در پایان) می‌توانند کمی متفاوت باشند. در دموی بالا این دو عدد 4,999,523 و 5,002,273 تومان بودند.

## ۱۰. کلید API والکس (فقط برای live)

- کلید را در سایت والکس، بخش «مدیریت API» بسازید. طبق مستندات رسمی، هر کلید حداکثر **۹۰ روز** اعتبار دارد، قابل تمدید نیست و هر حساب حداکثر **۳ کلید** می‌تواند داشته باشد.
- **فقط مجوز معامله (trade) و خواندن (read)** بدهید. **مجوز برداشت (withdrawal) را هرگز ندهید.** این ربات به برداشت نیازی ندارد.
- اگر ممکن است **فهرست IP مجاز** را پر کنید. طبق مستندات، خالی گذاشتن این فهرست یعنی همهٔ IPها مجازند. IP باید همان آدرسی باشد که والکس از ربات می‌بیند (IP ثابت VPS، یا IP اینترنت شما؛ اگر از VPN یا پروکسی استفاده کنید، IP آن‌ها).
- تاریخ انقضا را طوری بگذارید که تا پایان مسابقه معتبر بماند.
- کلید را **فقط** در فایل `.env` روی همان دستگاه بنویسید. هرگز آن را در چت، ایمیل، اسکرین‌شات، issue یا commit نگذارید (`.env` در `.gitignore` است). اگر کلید لو رفت، فوراً آن را در سایت والکس حذف کنید و کلید جدید بسازید.
- قالب کلید مستند نشده است. کلاینت فقط ۱۶ تا ۵۱۲ کاراکتر از `[A-Za-z0-9._~+/=|:-]` را می‌پذیرد. اگر کلید واقعی شما خارج از این قالب باشد، ربات پیش از هر ارسالی با خطای پیکربندی متوقف می‌شود. این مورد را `check-live` روشن می‌کند.

## ۱۱. پروکسی، IP و نکات مربوط به ایران

- کلاینت به‌طور پیش‌فرض **پروکسی سیستم و متغیرهای `HTTP(S)_PROXY` را نادیده می‌گیرد** (`WALLEX_USE_ENV_PROXY=false`). دلیلش این است که پروکسی IP مبدأ را عوض می‌کند (و کلید محدود به IP را از کار می‌اندازد) و کلید را از زیرساختی عبور می‌دهد که شما انتخابش نکرده‌اید.
- اگر دستگاه شما فقط از طریق پروکسی به `api.wallex.ir` دسترسی دارد (مثل محیط ساخت همین پروژه، که دسترسی مستقیم timeout می‌داد)، `WALLEX_USE_ENV_PROXY=true` را در `.env` بگذارید. در این صورت **IP پروکسی** را در فهرست IP مجاز کلید وارد کنید. TLS تا خود والکس رمزنگاری‌شده و بررسی‌شده باقی می‌ماند.
- یک‌باره برای یک فرمان:

  ```powershell
  $env:WALLEX_USE_ENV_PROXY = "true"; python -m wallex_agent diagnose
  ```

  ```bash
  WALLEX_USE_ENV_PROXY=true python -m wallex_agent diagnose
  ```

- مستندات رسمی والکس می‌گوید «به دستور پلیس فتا، برای برداشت کوین IP شما باید کشور ایران باشد». این ربات برداشت ندارد، ولی این نشان می‌دهد والکس به محل IP حساس است. دسترسی از IP خارج از ایران (مثلاً یک VPS خارجی) مستند نشده و ممکن است کند، مسدود یا ناپایدار باشد. **روی همان دستگاهی که ربات را اجرا می‌کند `diagnose` را اجرا کنید** و نتیجه را ببینید.

## ۱۲. کدهای خروج

| کد | معنی |
|---|---|
| `0` | موفق: پایان مسابقه، پایان داده، پایان جلسهٔ paper، یا پایان مرحلهٔ تبدیل |
| `1` | خطای غیرمنتظره (یا شکست `selftest`) |
| `2` | خطای استفاده یا پیکربندی (آرگومان یا مقدار بد در `.env`) |
| `3` | دروازهٔ live رد کرد (شرط، شاهد، فلگ، عبارت تأیید یا KILL). **هیچ چیزی ارسال نشد.** |
| `4` | کلید توقف (kill) فعال شد: سفارش‌های باز لغو شدند (چیزی فروخته نشد)، ربات STOPPED است |
| `5` | توقف ایمنی: ربات STOPPED است (اختلاف دفتر و صرافی، تناقض، خطاهای پیاپی، افت سرمایه) |
| `6` | رد شد: یک پیش‌شرط برقرار نیست (مثلاً کلید نیست، موجودی canary کافی نیست، نمونهٔ دیگری در حال اجراست) |
| `7` | صرافی یا شبکه برای یک خواندن ضروری در دسترس نیست |
| `130` | با Ctrl+C یا SIGTERM قطع شد: سفارش‌های باز لغو شدند و وضعیت قابل ادامه است |

## ۱۳. فرمان‌ها در یک نگاه

| فرمان | گزینه‌ها | کار |
|---|---|---|
| `demo` | `--seed`، `--hours` | بک‌تست ۲۴ ساعتهٔ مصنوعی، آفلاین |
| `backtest` | `--data FILE` یا `--synthetic`، `--seed`، `--hours`، `--step` | بک‌تست روی فایل کندل یا بازار مصنوعی |
| `fetch-history` | `--symbol`، `--resolution`، `--hours`، `--out` | دانلود کندل‌های عمومی به CSV یا JSON |
| `diagnose` | `--tmn` | بررسی عمومی: قوانین بازار، دفتر سفارش، اسپرد، محاسبهٔ تبدیل TMN به USDT |
| `paper` | `--seconds`، `--interval` | معاملهٔ کاغذی روی دفتر سفارش واقعی |
| `selftest` | — | اجرای کل تست‌ها و نوشتن `selftest.json` (برای live لازم است) |
| `check-live` | — | بررسی فقط-GET API خصوصی با کلید و نوشتن `live_schema_attestation.json` |
| `canary` | `--i-understand-real-money` | یک سفارش واقعی، کوچک و پرنشدنی: ثبت، مشاهده و لغو؛ نوشتن `live_canary.json` |
| `arm-live` | `--i-understand-real-money` | نوشتن فایل `LIVE_ARMED` برای systemd و `live --resume` |
| `live` | `--stage convert\|full`، `--resume`، `--i-understand-real-money` | معاملهٔ واقعی |
| `status` | `--mode`، `--run` | وضعیت |
| `kill` | `--cancel-open`، `--reason`، `--mode` | کلید توقف |
| `report` | `--mode`، `--run` | بازسازی `final_report.json` |
| `trace` | `--mode`، `--run`، `--last N` یا `--decision ID` | ردِ خوانای تصمیم‌ها |

راهنمای دقیق هر فرمان: `python -m wallex_agent <فرمان> --help`.

## ۱۴. اجرای تست‌ها

```bash
python -m pytest
```

نتیجه در ۲۰۲۶-۱۰-۲۲ روی ویندوز با پایتون 3.12.1: `2241 passed in 42.53s`. این تست‌ها با شبیه‌ساز و پاسخ‌های mock اجرا می‌شوند و هیچ سفارش واقعی‌ای نمی‌فرستند. جزئیات در [`docs/TEST_STATUS_FA.md`](docs/TEST_STATUS_FA.md).
