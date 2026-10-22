# نصب روی VPS لینوکسی با systemd

> **یادداشت صادقانه:** فایل `deploy/wallex-agent.service` **هرگز روی یک میزبان واقعی systemd اجرا نشده است.** تست‌های پروژه هم هرگز روی لینوکس اجرا نشده‌اند؛ همه روی ویندوز اجرا شده‌اند. منطق شروع دوباره (`live --resume`) در تست‌ها پوشش داده شده، اما خود unit، گزینه‌های سخت‌سازی (hardening) آن، و اجرای `selftest` با کاربر سرویس آزموده نشده‌اند. پیش از روز مسابقه، «اجرای آزمایشی بدون پول» در بخش ۷ را حتماً انجام دهید.

این راهنما برای Debian یا Ubuntu نوشته شده است. فرض این است که همهٔ دستورها را با یک کاربر دارای `sudo` اجرا می‌کنید.

## ۱. پیش‌نیازها و انتخاب سرور

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv git
python3 --version
timedatectl status
```

- نسخهٔ پایتون باید ۳.۱۰ یا بالاتر باشد.
- در خروجی `timedatectl` باید `System clock synchronized: yes` ببینید. ساعت درست برای اعتبار شاهدها و تازگی دفتر سفارش لازم است.
- **دسترسی به والکس:** در مستندات رسمی فقط آمده که «برای برداشت کوین IP شما باید کشور ایران باشد» (این ربات برداشت ندارد). دسترسی API از IP خارج از ایران مستند نشده است و ممکن است کند یا مسدود باشد. قبل از هر کار دیگری، `diagnose` را روی **همین VPS** اجرا کنید (بخش ۵).
- **IP مجاز کلید:** IP عمومی ثابت VPS را (از پنل ارائه‌دهنده) در فهرست IP مجاز کلید API والکس وارد کنید. اگر روی VPS از پروکسی استفاده می‌کنید (`WALLEX_USE_ENV_PROXY=true`)، IP خروجی پروکسی را وارد کنید.

## ۲. کاربر سرویس، کد و venv

کاربر سیستمی بدون shell:

```bash
sudo useradd --system --home /var/lib/wallex-agent --shell /usr/sbin/nologin wallex-agent
```

کد را در `/opt/wallex-agent` قرار دهید. مالک کد root می‌ماند تا سرویس نتواند کد خودش را تغییر دهد (هش سورس به همین کد گره خورده است). اگر مخزن git در دسترس است:

```bash
sudo git clone <REPO_URL> /opt/wallex-agent
```

اگر کد فقط روی ویندوز شماست، روی ویندوز (PowerShell، داخل پوشهٔ پروژه) یک بسته بسازید. `git archive` فقط فایل‌های commit‌شده را برمی‌دارد، پس `.env`، `.venv` و `state` منتقل **نمی‌شوند** و این همان چیزی است که می‌خواهیم:

```powershell
git archive --format=tar.gz -o wallex-agent.tar.gz HEAD
scp wallex-agent.tar.gz <user>@<vps-ip>:/tmp/
```

سپس روی VPS:

```bash
sudo mkdir -p /opt/wallex-agent
sudo tar -xzf /tmp/wallex-agent.tar.gz -C /opt/wallex-agent
```

ساخت venv و نصب (`[dev]` برای pytest لازم است، چون `selftest` به آن نیاز دارد):

```bash
sudo python3 -m venv /opt/wallex-agent/.venv
sudo /opt/wallex-agent/.venv/bin/pip install --upgrade pip
sudo /opt/wallex-agent/.venv/bin/pip install -e "/opt/wallex-agent[dev]"
/opt/wallex-agent/.venv/bin/wallex-agent --version
```

> فایل `.env` را در `/opt/wallex-agent` **نگذارید.** سرویس با `WorkingDirectory=/opt/wallex-agent` اجرا می‌شود و یک `.env` در آن پوشه را هم می‌خواند. کلید فقط باید در `EnvironmentFile` (بخش بعد) باشد.

## ۳. پوشهٔ وضعیت و فایل محیطی (EnvironmentFile)

پوشهٔ وضعیت، که تنها جای قابل‌نوشتن برای سرویس است:

```bash
sudo install -d -m 0750 -o wallex-agent -g wallex-agent /var/lib/wallex-agent /var/lib/wallex-agent/state
```

فایل محیطی، قابل خواندن فقط برای root و گروه `wallex-agent`:

```bash
sudo install -d -m 0750 -o root -g wallex-agent /etc/wallex-agent
sudo install -m 0640 -o root -g wallex-agent /dev/null /etc/wallex-agent/wallex-agent.env
sudoedit /etc/wallex-agent/wallex-agent.env
```

محتوای فایل را به این شکل بنویسید (`KEY=VALUE`، بدون `export`، بدون فاصله دور `=`). مقدار `DATA_DIR` **باید دقیقاً همین مسیر باشد**، چون unit مسیر فایل‌های `LIVE_ARMED` و `KILL` را ثابت نوشته است:

```ini
WALLEX_API_KEY=<paste-the-key-only-here>
TRADING_MODE=live
ENABLE_LIVE=true
INITIAL_TMN=5000000
COMPETITION_HOURS=24
PROFILE=conservative
FINAL_POLICY=report_only
DATA_DIR=/var/lib/wallex-agent/state
WALLEX_USE_ENV_PROXY=false
```

اگر `PROFILE=competition` می‌گذارید، خط `MAX_EXPOSURE_FRACTION` را **ننویسید** (README بخش ۴-۱). `FINAL_POLICY` را پیش از شروع قطعی کنید (README بخش ۴-۲). `TRADING_MODE=live` فقط باعث می‌شود `status`، `report`، `trace` و `kill` به‌طور پیش‌فرض حالت live را نشان دهند.

## ۴. نصب unit

```bash
sudo install -m 0644 /opt/wallex-agent/deploy/wallex-agent.service /etc/systemd/system/wallex-agent.service
sudo systemd-analyze verify /etc/systemd/system/wallex-agent.service
sudo systemctl daemon-reload
sudo systemctl enable wallex-agent
```

`enable` سرویس را **همین الان شروع نمی‌کند.** حتی اگر شروع هم شود، تا وقتی فایل `LIVE_ARMED` وجود نداشته باشد، systemd بی‌صدا از آن رد می‌شود (بخش ۶).

فرمان سرویس:

```text
ExecStart=/opt/wallex-agent/.venv/bin/wallex-agent live --resume --i-understand-real-money
```

## ۵. مراحل تعاملی با کاربر سرویس

همهٔ دروازه‌ها (`selftest`، `check-live`، `canary` و عبارت تأیید) فقط به‌صورت تعاملی برآورده می‌شوند. systemd فقط پس از `arm-live` می‌تواند ربات را اجرا کند.

برای راحتی، در همان ترمینال یک alias بسازید. همهٔ مراحل با کاربر `wallex-agent` اجرا می‌شوند تا فایل‌های `state` مالک درست داشته باشند:

```bash
cd /opt/wallex-agent
alias wa='sudo -u wallex-agent /opt/wallex-agent/.venv/bin/wallex-agent --env-file /etc/wallex-agent/wallex-agent.env'
```

سپس به همان ترتیب [`RUNBOOK_LIVE_FA.md`](RUNBOOK_LIVE_FA.md) (خروجی و کد خروج هر مرحله آنجا آمده):

```bash
wa diagnose
wa paper --seconds 300 --interval 10
wa selftest
wa check-live
wa canary --i-understand-real-money
wa live --stage convert --i-understand-real-money
wa arm-live --i-understand-real-money
sudo systemctl start wallex-agent
```

- `canary`، `live` و `arm-live` عبارت تأیید را در همین ترمینال می‌پرسند (`RUN CANARY 5000000` یا `START LIVE 5000000`). `sudo -u` ترمینال را حفظ می‌کند.
- `live --stage convert` **ساعت ۲۴ ساعتهٔ مسابقه را شروع می‌کند.** بعد از اینکه تبدیل را در اپ والکس بررسی کردید، بی‌معطلی `arm-live` و `systemctl start` را اجرا کنید. سرویس `live --resume` را اجرا می‌کند که پیش‌فرضش `--stage full` است.
- می‌توانید مرحلهٔ convert را حذف کنید (`arm-live` و بعد `systemctl start`). در آن صورت سرویس تبدیل را هم خودش انجام می‌دهد، اما فرصت بررسی جداگانهٔ تبدیل را از دست می‌دهید. توضیحات داخل خود unit می‌گویند «اولین شروع هرگز توسط systemd انجام نمی‌شود». این یک قرارداد اجرایی است و کد آن را اجبار نمی‌کند: `live --resume` روی دفتری که هنوز شروع نشده، مثل یک شروع تازه عمل می‌کند (با شاهدهای حداکثر ۲۴ ساعته). توصیهٔ ما این است که شروع اول را تعاملی و با `--stage convert` انجام دهید.
- اگر `selftest` با کاربر سرویس به خطای مجوز نوشتن خورد (پوشهٔ کد متعلق به root است)، این را امتحان کنید. این مورد آزموده نشده است:

  ```bash
  sudo -u wallex-agent env PYTHONDONTWRITEBYTECODE=1 /opt/wallex-agent/.venv/bin/wallex-agent --env-file /etc/wallex-agent/wallex-agent.env selftest
  ```

## ۶. معنای `LIVE_ARMED` و `KILL` در unit

| فایل | شرط unit | معنی |
|---|---|---|
| `/var/lib/wallex-agent/state/LIVE_ARMED` | `ConditionPathExists=` | فقط وقتی وجود دارد سرویس شروع می‌شود (چه دستی، چه هنگام بوت). محتوایش باید دقیقاً `START LIVE <INITIAL_TMN>` باشد و `arm-live` همین را می‌نویسد. `live --resume` عبارت را از این فایل می‌خواند و از کسی چیزی نمی‌پرسد. **برای خلع سلاح، فایل را پاک کنید.** اگر `INITIAL_TMN` را عوض کردید، دوباره `arm-live` کنید. اگر محتوا با عبارت نخواند، دروازه رد می‌کند (کد ۳) و سرویس پایین می‌ماند. |
| `/var/lib/wallex-agent/state/KILL` | `ConditionPathExists=!` | تا وقتی وجود دارد، سرویس شروع نمی‌شود. ربات هم پیش از هر سفارش آن را بررسی می‌کند. |

خلع سلاح:

```bash
sudo rm /var/lib/wallex-agent/state/LIVE_ARMED
```

## ۷. اجرای آزمایشی بدون پول (توصیهٔ جدی، قبل از روز مسابقه)

برای اینکه ببینید سندباکس systemd (شبکه، DNS، دسترسی نوشتن) با این برنامه کار می‌کند، موقتاً به‌جای live یک جلسهٔ paper اجرا کنید:

```bash
sudo mkdir -p /etc/systemd/system/wallex-agent.service.d
sudo tee /etc/systemd/system/wallex-agent.service.d/dryrun.conf >/dev/null <<'EOF'
[Unit]
ConditionPathExists=
[Service]
ExecStart=
ExecStart=/opt/wallex-agent/.venv/bin/wallex-agent paper --seconds 120 --interval 10
Restart=no
EOF
sudo systemctl daemon-reload
sudo systemctl start wallex-agent
journalctl -u wallex-agent -f
```

باید خروجی `PAPER ...` و در پایان `status: session_end (exit 0)` را ببینید. paper هرگز از کلید استفاده نمی‌کند و هیچ سفارشی نمی‌فرستد. بعد از آزمایش، این تغییر موقت را **حتماً** پاک کنید:

```bash
sudo rm -r /etc/systemd/system/wallex-agent.service.d
sudo systemctl daemon-reload
```

اگر این آزمایش با خطای شبکه یا DNS شکست خورد، ولی همان فرمان با `wa paper ...` کار کرد، مقصر یکی از گزینه‌های سخت‌سازی unit است (مثلاً `RestrictAddressFamilies` یا `SystemCallFilter`). این گزینه‌ها با این برنامه آزموده نشده‌اند.

## ۸. سیاست شروع دوباره و تازگی شواهد

| کد خروج | رفتار systemd (`Restart=on-failure`، `RestartSec=30`) |
|---|---|
| `0` (پایان مسابقه) | پایین می‌ماند. |
| `130` (توقف با `systemctl stop`، SIGINT یا SIGTERM) | به‌عنوان موفقیت حساب می‌شود (`SuccessExitStatus=130`) و پایین می‌ماند. |
| `3`، `4`، `5`، `6` | **هرگز دوباره اجرا نمی‌شود** (`RestartPreventExitStatus`). این خطاها را فقط اپراتور می‌تواند رفع کند. |
| `1`، `2`، `7`، یا کرش | پس از ۳۰ ثانیه دوباره اجرا می‌شود؛ حداکثر ۵ بار در ساعت (`StartLimitBurst=5`). بعد از آن پایین می‌ماند. |

اگر به سقف شروع دوباره رسید، بعد از رفع مشکل این را اجرا کنید:

```bash
sudo systemctl reset-failed wallex-agent
sudo systemctl start wallex-agent
```

هر شروع (چه دستی، چه شروع دوباره، چه بوت) **همهٔ دروازه‌ها را دوباره بررسی می‌کند**:

- شاهدهای `selftest.json` و `live_schema_attestation.json` باید برای **همین سورس** باشند (هش سورس). پس در طول مسابقه فایل‌های `/opt/wallex-agent` را تغییر ندهید.
- عمر این شاهدها برای شروع تازه حداکثر **۲۴ ساعت** است. وقتی مسابقه قبلاً شروع شده باشد و با `--resume` ادامه دهیم، تا **۴۸ ساعت** پذیرفته می‌شود. پس شاهدهایی که درست پیش از شروع ساخته شده‌اند، برای کل یک مسابقهٔ ۲۴ ساعته کافی‌اند.
- `live_canary.json` باید `ok: true` و حداکثر **۷۲ ساعت** عمر داشته باشد.
- نباید فایل KILL وجود داشته باشد.

اگر دروازه رد کند، سرویس با کد ۳ خارج می‌شود و پایین می‌ماند. هرگز با یک دروازهٔ ردشده معامله نمی‌کند. برای رفع، `wa selftest` و/یا `wa check-live` (و در صورت نیاز `wa canary --i-understand-real-money`) را اجرا کنید، سپس `sudo systemctl start wallex-agent`.

## ۹. چرا شروع دوباره بی‌خطر است

- **ثبت پیش از ارسال (persist-before-send):** هر سفارش، قبل از اینکه تنها POST آن ارسال شود، در SQLite ثبت و commit می‌شود (`INTENT` → `SENDING`، همراه با یک عکس از موجودی). یک `client_id` هرگز دوبار POST نمی‌شود.
- **شروع با تطبیق (reconcile):** ربات هنگام شروع ابتدا هر سفارش حل‌نشده یا با نتیجهٔ نامعلوم را از روی واقعیت‌های صرافی حل می‌کند. منابع این واقعیت‌ها: جستجوی سفارش، سفارش‌های باز، معاملات، و تغییر موجودی. سپس دفتر را از روی پرشدن‌های ثبت‌شده بازسازی و با موجودی واقعی مقایسه می‌کند. اگر اختلاف توضیح‌ناپذیر باشد، STOPPED می‌شود و معامله نمی‌کند.
- **پیوستگی:** زنجیرهٔ هش audit ادامه پیدا می‌کند و زمان شروع مسابقه در دیتابیس ذخیره شده است. پس شروع دوباره نه سفارش تکراری می‌سازد و نه پنجرهٔ ۲۴ ساعته را از نو شروع می‌کند.
- **یک نمونه در هر زمان:** قفل `instance.lock` در `state/live` جلوی اجرای همزمان دو نمونه روی یک دفتر را می‌گیرد.

## ۱۰. دیدن لاگ‌ها و وضعیت

```bash
systemctl status wallex-agent
journalctl -u wallex-agent -f
journalctl -u wallex-agent --since "1 hour ago"
wa status --mode live
wa trace --mode live --last 3
sudo -u wallex-agent sh -c 'tail -f "$(ls -td /var/lib/wallex-agent/state/live/runs/*/ | head -1)audit.jsonl"'
```

`status` و `trace` از یک کپی فقط‌خواندنی دیتابیس می‌خوانند و کنار سرویس در حال اجرا بی‌خطرند. خروجی ربات به‌دلیل `PYTHONUNBUFFERED=1` بلافاصله در journal دیده می‌شود. کلید هرگز چاپ نمی‌شود، چون همهٔ خروجی‌ها از یک پاک‌کنندهٔ راز عبور می‌کنند.

## ۱۱. توقف اضطراری

**توقف نهایی** (سفارش‌های باز لغو می‌شوند، ربات STOPPED می‌شود و دیگر شروع نمی‌شود):

```bash
wa kill --reason "emergency stop"
systemctl status wallex-agent
```

ربات در حال اجرا ظرف چند ثانیه سفارش‌هایش را لغو می‌کند و با کد ۴ خارج می‌شود. systemd آن را دوباره اجرا نمی‌کند و تا وقتی فایل KILL هست اجازهٔ شروع هم نمی‌دهد. اگر بعد از حدود یک دقیقه هنوز در حال اجرا بود:

```bash
sudo systemctl stop wallex-agent
```

**مکث قابل ادامه** (سفارش‌های باز لغو می‌شوند و وضعیت قابل ادامه است، کد ۱۳۰):

```bash
sudo systemctl stop wallex-agent
sudo rm /var/lib/wallex-agent/state/LIVE_ARMED
```

`systemctl stop` سیگنال SIGINT می‌فرستد (`KillSignal=SIGINT`). ربات گام جاری را تمام می‌کند، سفارش‌های باز را لغو می‌کند و گزارش موقت می‌نویسد؛ تا ۶۰ ثانیه (`TimeoutStopSec`) برای این کار وقت دارد. پاک کردن `LIVE_ARMED` جلوی شروع خودکار بعد از ریبوت را می‌گیرد. برای ادامه، `wa arm-live --i-understand-real-money` و سپس `sudo systemctl start wallex-agent`.

**بعد از کرش، اگر سرویس پایین است و سفارشی باز مانده:**

```bash
wa kill --cancel-open --mode live --reason "service down with an open order"
```

با یک کلاینت «فقط لغو» (که ثبت سفارش در آن ناممکن است)، فقط سفارش‌های ثبت‌شده در دفتر همین ربات را لغو می‌کند.

> هیچ‌کدام از این روش‌ها **چیزی نمی‌فروشند.** برای صاف کردن دستی موقعیت در اپ والکس و پیامدهای آن، بخش ۱۰ [`RUNBOOK_LIVE_FA.md`](RUNBOOK_LIVE_FA.md) را ببینید. `kill` برای دفتر live نهایی است.

## ۱۲. پس از مسابقه

```bash
wa report --mode live
sudo rm -f /var/lib/wallex-agent/state/LIVE_ARMED
sudo systemctl disable wallex-agent
```

فایل‌های `final_report.json`، `audit.jsonl` و `trace.txt` در `/var/lib/wallex-agent/state/live/runs/live-<id>/` هستند. اگر کلید API دیگر لازم نیست، در سایت والکس حذفش کنید.
