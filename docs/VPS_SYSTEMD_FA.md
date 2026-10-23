# نصب روی VPS لینوکسی با systemd

> **یادداشت صادقانه:** فایل `deploy/wallex-agent.service` **هرگز روی یک میزبان واقعی systemd اجرا نشده است.** تست‌های پروژه هم هرگز روی لینوکس اجرا نشده‌اند؛ همه روی ویندوز اجرا شده‌اند. منطق شروع دوباره (`live --resume`) در تست‌ها پوشش داده شده، اما خود unit، گزینه‌های سخت‌سازی (hardening) آن، و اجرای `selftest` با کاربر سرویس آزموده نشده‌اند. پیش از روز مسابقه، «اجرای آزمایشی بدون پول» در بخش ۷ را حتماً انجام دهید.

این راهنما برای Debian یا Ubuntu نوشته شده است. فرض این است که همهٔ دستورها را با یک کاربر دارای `sudo` اجرا می‌کنید.

> **مدل حفاظت:** دروازه‌های live (فلگ، شواهد، عبارت تأیید، `LIVE_ARMED`، KILL، قفل تک‌نمونه) جلوی **اشتباه و اجرای خودکار یا تصادفی** را می‌گیرند. در برابر فرایند بدخواهی که با کاربر `wallex-agent` یا root اجرا شود محافظت نمی‌کنند: چنین فرایندی می‌تواند کلید را از فایل محیطی یا حافظه بخواند و هر شاهدی را جعل کند. برای این سطح، به مجوزهای سیستم‌عامل تکیه کنید: کاربر جداگانه، کد متعلق به root، فایل محیطی `0640 root:wallex-agent`، کلید بدون مجوز برداشت با IP مجاز، و «مهر کردن» شواهد (بخش ۶-۱). ریسک‌های باقی‌مانده: README بخش ۱۶.

## ۱. پیش‌نیازها و انتخاب سرور

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv git
python3 --version
timedatectl status
```

- نسخهٔ پایتون باید ۳.۱۰ یا بالاتر باشد.
- در خروجی `timedatectl` باید `System clock synchronized: yes` ببینید. ساعت درست برای اعتبار شاهدها و تازگی دفتر سفارش لازم است.
- **دسترسی به والکس:** در مستندات رسمی فقط آمده که «برای برداشت کوین IP شما باید کشور ایران باشد» (این ربات برداشت ندارد). دسترسی API از IP خارج از ایران مستند نشده است و ممکن است کند یا مسدود باشد. قبل از هر کار دیگری، `net-check` و `diagnose` را روی **همین VPS** اجرا کنید (بخش ۵). `net-check` هر مرحلهٔ شبکه (resolve، پروکسی، TLS، HTTP، بایت‌ها و زمان‌ها) را جدا نشان می‌دهد و فقط GET می‌فرستد.
- **IP مجاز کلید:** IP عمومی ثابت VPS را (از پنل ارائه‌دهنده) در فهرست IP مجاز کلید API والکس وارد کنید. اگر روی VPS از پروکسی استفاده می‌کنید (`WALLEX_PROXY_URL` یا `WALLEX_USE_ENV_PROXY=true`)، IP خروجی پروکسی را وارد کنید.

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

> فایل `.env` را در `/opt/wallex-agent` **نگذارید.** فرمان سرویس اکنون `--env-file /etc/wallex-agent/wallex-agent.env` را صریحاً می‌دهد، پس یک `.env` جاافتاده در `WorkingDirectory` دیگر خوانده نمی‌شود؛ اما هر نسخهٔ اضافهٔ کلید روی دیسک یعنی جای بیشتری برای نشت. کلید فقط باید در `EnvironmentFile` (بخش بعد) باشد. اگر برای نصب از یک `.env` کمکی استفاده کردید، پس از ساختن فایل محیطی آن را پاک کنید (`shred -u .env`) یا دست‌کم `chmod 600 .env`.

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
# فقط اگر VPS از راه پروکسی به والکس می‌رسد (README بخش ۱۱):
# WALLEX_PROXY_URL=http://127.0.0.1:2080
```

مسیر شبکه: پیش‌فرض اتصال **مستقیم** است و ربات هیچ پروکسی‌ای را خودبه‌خود به کار نمی‌برد. اگر لازم است، `WALLEX_PROXY_URL=http://host:port` را در همین فایل بنویسید (فقط `http://`، پورت الزامی؛ اگر پروکسی ورود می‌خواهد `http://user:pass@host:port`، که هرگز چاپ نمی‌شود). چون systemd همین فایل را با `EnvironmentFile` در محیط فرایند هم می‌گذارد، متغیرهای `HTTPS_PROXY`/`HTTP_PROXY`/`NO_PROXY` نوشته‌شده در آن فقط با `WALLEX_USE_ENV_PROXY=true` اثر دارند؛ `WALLEX_PROXY_URL` صریح‌تر است و بر آن‌ها اولویت دارد. مسیر را با `wa net-check` (بخش ۵) تأیید کنید.

اگر `PROFILE=competition` می‌گذارید، خط `MAX_EXPOSURE_FRACTION` را **ننویسید** (README بخش ۴-۱). `FINAL_POLICY` را پیش از شروع قطعی کنید (README بخش ۴-۲). `TRADING_MODE=live` فقط باعث می‌شود `status`، `report`، `trace` و `unstop` به‌طور پیش‌فرض حالت live را نشان دهند (`kill --cancel-open` هر وقت دفتر live باشد روی آن عمل می‌کند). `DATA_DIR` صریح و مطلق برای live **الزامی** است.

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
ExecStart=/opt/wallex-agent/.venv/bin/wallex-agent --env-file /etc/wallex-agent/wallex-agent.env live --resume --i-understand-real-money
```

`live --resume` فقط مسابقه‌ای را **ادامه** می‌دهد که اپراتور قبلاً به‌صورت تعاملی شروع کرده است (بخش ۵). روی دفتری که مسابقه‌اش شروع نشده، با کد ۳ رد می‌شود و سرویس پایین می‌ماند.

## ۵. مراحل تعاملی با کاربر سرویس

همهٔ دروازه‌ها (`selftest`، `check-live`، `canary` و عبارت تأیید) فقط به‌صورت تعاملی برآورده می‌شوند. systemd فقط پس از `arm-live` می‌تواند ربات را اجرا کند.

برای راحتی، در همان ترمینال یک alias بسازید. همهٔ مراحل با کاربر `wallex-agent` اجرا می‌شوند تا فایل‌های `state` مالک درست داشته باشند:

```bash
cd /opt/wallex-agent
alias wa='sudo -u wallex-agent /opt/wallex-agent/.venv/bin/wallex-agent --env-file /etc/wallex-agent/wallex-agent.env'
```

سپس به همان ترتیب [`RUNBOOK_LIVE_FA.md`](RUNBOOK_LIVE_FA.md) (خروجی و کد خروج هر مرحله آنجا آمده):

```bash
wa net-check
wa diagnose
wa paper --seconds 300 --interval 10
wa selftest
wa check-live
wa canary --i-understand-real-money
wa live --stage convert --i-understand-real-money
wa arm-live --i-understand-real-money
sudo systemctl start wallex-agent
```

- همهٔ فرمان‌های تعاملی را **با همان فایل محیطی سرویس** اجرا کنید (alias بالا `--env-file` را می‌دهد). بدون آن، `DATA_DIR` و کلید ترمینال شما با سرویس فرق می‌کند.
- `canary`، `live` و `arm-live` عبارت تأیید را در همین ترمینال می‌پرسند (`RUN CANARY 5000000` یا `START LIVE 5000000`). `sudo -u` ترمینال را حفظ می‌کند.
- **اولین شروع live همیشه تعاملی است و کد آن را اجبار می‌کند:** `live --resume` (یعنی سرویس) روی دفتری که مسابقه‌اش شروع نشده رد می‌شود. `wa live --stage convert ...` **ساعت ۲۴ ساعتهٔ مسابقه را شروع می‌کند.** بعد از اینکه تبدیل را در اپ والکس بررسی کردید، بی‌معطلی `arm-live` و `systemctl start` را اجرا کنید. سرویس `live --resume` را اجرا می‌کند که پیش‌فرضش `--stage full` است.
- اگر نمی‌خواهید تبدیل را جداگانه بررسی کنید، به‌جای `--stage convert` می‌توانید `wa live --i-understand-real-money` را تعاملی اجرا کنید، آن را با Ctrl+C متوقف کنید (کد ۱۳۰، قابل ادامه)، سپس `arm-live` و `systemctl start`.
- `arm-live` یک رکورد JSON می‌نویسد که به عبارت، هش سورس، آدرس API، `DATA_DIR` و یک زمان انقضا (`COMPETITION_HOURS` + ۲۴ ساعت) گره خورده است. نوشتن دستی `START LIVE ...` در این فایل دیگر کار نمی‌کند.
- اگر `selftest` با کاربر سرویس به خطای مجوز نوشتن خورد (پوشهٔ کد متعلق به root است)، این را امتحان کنید. این مورد آزموده نشده است:

  ```bash
  sudo -u wallex-agent env PYTHONDONTWRITEBYTECODE=1 /opt/wallex-agent/.venv/bin/wallex-agent --env-file /etc/wallex-agent/wallex-agent.env selftest
  ```

## ۶. معنای `LIVE_ARMED` و `KILL` در unit

| فایل | شرط unit | معنی |
|---|---|---|
| `/var/lib/wallex-agent/state/LIVE_ARMED` | `ConditionPathExists=` | فقط وقتی وجود دارد سرویس شروع می‌شود (چه دستی، چه هنگام بوت). فقط `arm-live` (پس از گذر همهٔ دروازه‌ها و عبارت تایپ‌شده) آن را به‌صورت یک رکورد JSON ساخت‌یافته می‌نویسد. `live --resume` آن را فقط برای **ادامهٔ** یک مسابقهٔ شروع‌شده می‌پذیرد و همهٔ فیلدها را بررسی می‌کند: عبارت، هش سورس، آدرس API، `DATA_DIR`، `INITIAL_TMN` و انقضا. **برای خلع سلاح، فایل را پاک کنید.** اگر چیزی از این‌ها عوض شد یا رکورد منقضی شد، دروازه رد می‌کند (کد ۳) و سرویس پایین می‌ماند؛ دوباره `arm-live` کنید. |
| `/var/lib/wallex-agent/state/KILL` | `ConditionPathExists=!` | تا وقتی وجود دارد، سرویس شروع نمی‌شود. ربات هم پیش از هر سفارش آن را بررسی می‌کند. |

خلع سلاح:

```bash
sudo rm /var/lib/wallex-agent/state/LIVE_ARMED
```

### ۶-۱. مهر کردن شواهد (توصیه‌شده روی لینوکس)

به‌طور پیش‌فرض کاربر سرویس می‌تواند فایل‌های شاهد و `LIVE_ARMED` را بازنویسی کند (همه در `DATA_DIR` او هستند). برای اینکه یک فرایند خودکار یا تصادفی که با کاربر `wallex-agent` اجرا می‌شود نتواند آن‌ها را عوض یا جعل کند، پس از `arm-live` شواهد را به root بدهید و پوشه را sticky کنید. مالک خود پوشه هم باید root باشد، وگرنه بیت sticky از فایل‌های root در برابر کاربر سرویس محافظت نمی‌کند:

```bash
cd /var/lib/wallex-agent/state
sudo chown root:wallex-agent . selftest.json live_schema_attestation.json live_canary.json LIVE_ARMED
sudo chmod 0444 selftest.json live_schema_attestation.json live_canary.json LIVE_ARMED
sudo chmod 1770 .
```

- سرویس فقط این فایل‌ها را **می‌خواند** و فقط در `state/live/` می‌نویسد (دفتر، قفل، audit و گزارش‌ها)، پس کارش ادامه پیدا می‌کند. `kill` با کاربر سرویس همچنان می‌تواند فایل KILL را بسازد و بعداً پاکش کند.
- پیش از اجرای دوبارهٔ `selftest`، `check-live`، `canary` یا `arm-live`، فایل مهرشدهٔ مربوط را با root پاک کنید (`sudo rm ...`)، وگرنه نوشتنش با خطای مجوز شکست می‌خورد.
- systemd مالکیت `StateDirectory` (یعنی `/var/lib/wallex-agent`) را بررسی می‌کند. تا وقتی مالک آن `wallex-agent` است، به زیرپوشه‌ها دست نمی‌زند؛ اگر مالکیت آن را عوض کنید، ممکن است systemd همه‌چیز را به‌صورت بازگشتی دوباره به `wallex-agent` بدهد و این مهر برداشته شود. پس از هر تغییر، `ls -l` را بررسی کنید.
- این کار در برابر root یا فرایندی که کلید را دارد محافظت نمی‌کند (مدل حفاظت، بالای همین سند). این توصیه روی یک سرور واقعی آزموده نشده است.

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
| `1`، `2`، `7`، یا کرش | پس از ۳۰ ثانیه دوباره اجرا می‌شود؛ حداکثر ۵ بار در ساعت (`StartLimitBurst=5`). بعد از آن پایین می‌ماند. کد `7` شامل شکست «پیش‌پرواز اتصال» در شروع هم هست (قوانین هر دو نماد و دو دفتر تازه و غیر crossed پیش از اولین سفارش؛ هیچ سفارشی ارسال نشده): اگر پروکسی یا شبکه ناپایدار است، `journalctl` دستهٔ خطا را نشان می‌دهد و `wa net-check --repeat 3` علتش را. |

اگر به سقف شروع دوباره رسید، بعد از رفع مشکل این را اجرا کنید:

```bash
sudo systemctl reset-failed wallex-agent
sudo systemctl start wallex-agent
```

هر شروع (چه دستی، چه شروع دوباره، چه بوت) **همهٔ دروازه‌ها را دوباره بررسی می‌کند**:

- شاهدهای `selftest.json` و `live_schema_attestation.json` باید برای **همین سورس** باشند (هش سورس). پس در طول مسابقه فایل‌های `/opt/wallex-agent` را تغییر ندهید.
- عمر این شاهدها برای شروع تازه حداکثر **۲۴ ساعت** است. وقتی مسابقه قبلاً شروع شده باشد و با `--resume` ادامه دهیم، تا **۴۸ ساعت** پذیرفته می‌شود. پس شاهدهایی که درست پیش از شروع ساخته شده‌اند، برای کل یک مسابقهٔ ۲۴ ساعته کافی‌اند.
- `live_canary.json` باید `ok: true` و حداکثر **۷۲ ساعت** عمر داشته باشد.
- رکورد `LIVE_ARMED` باید معتبر و منقضی‌نشده باشد و مسابقه در دفتر live قبلاً شروع شده باشد.
- `DATA_DIR` باید صریح باشد.
- نباید فایل KILL وجود داشته باشد.

اگر دروازه رد کند، سرویس با کد ۳ خارج می‌شود و پایین می‌ماند. هرگز با یک دروازهٔ ردشده معامله نمی‌کند. برای رفع، `wa selftest` و/یا `wa check-live` (و در صورت نیاز `wa canary --i-understand-real-money` و `wa arm-live --i-understand-real-money`) را اجرا کنید (اگر شواهد را مهر کرده‌اید، اول فایل مربوط را با root پاک کنید)، سپس `sudo systemctl start wallex-agent`.

## ۹. چرا شروع دوباره بی‌خطر است

- **ثبت پیش از ارسال (persist-before-send):** هر سفارش، قبل از اینکه تنها POST آن ارسال شود، در SQLite ثبت و commit می‌شود (`INTENT` → `SENDING`، همراه با یک عکس از موجودی). یک `client_id` هرگز دوبار POST نمی‌شود.
- **شروع با تطبیق (reconcile):** ربات هنگام شروع ابتدا هر سفارش حل‌نشده یا با نتیجهٔ نامعلوم را از روی واقعیت‌های صرافی حل می‌کند. منابع این واقعیت‌ها: جستجوی سفارش، سفارش‌های باز، معاملات، و تغییر موجودی. سپس دفتر را از روی پرشدن‌های ثبت‌شده بازسازی و با موجودی واقعی مقایسه می‌کند. اگر اختلاف توضیح‌ناپذیر باشد، STOPPED می‌شود و معامله نمی‌کند.
- **سفارش‌های رهاشده زیر نظر می‌مانند:** هر سفارشی که با نتیجهٔ نامعلوم «رهاشده» (`ABANDONED`) علامت خورده، در هر شروع و هر چرخه دوباره جستجو می‌شود. اگر صرافی آن را دیرهنگام پذیرفته باشد، ربات آن را (اگر باز است) لغو و با `late_accept` متوقف می‌شود.
- **پیوستگی:** زنجیرهٔ هش audit ادامه پیدا می‌کند و زمان شروع مسابقه در دیتابیس ذخیره شده است. پس شروع دوباره نه سفارش تکراری می‌سازد و نه پنجرهٔ ۲۴ ساعته را از نو شروع می‌کند.
- **یک نمونه در هر زمان:** `state/live/instance.lock` با یک قفل سیستم‌عامل (`flock`) برای تمام عمر فرایند نگه داشته می‌شود و جلوی اجرای همزمان دو نمونه روی یک دفتر را می‌گیرد. وقتی فرایند بمیرد، کرنل قفل را آزاد می‌کند؛ خود فایل عمداً باقی می‌ماند و پاک کردنش لازم نیست. `DATA_DIR` باید روی دیسک محلی باشد، نه NFS/SMB.

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

**روش اصلی روی VPS:** سرویس را متوقف کنید و بعد KILL بنویسید تا دوباره شروع نشود:

```bash
sudo systemctl stop wallex-agent
wa kill --reason "emergency stop"
systemctl status wallex-agent
```

`systemctl stop` سیگنال SIGINT می‌فرستد (`KillSignal=SIGINT`). ربات گام جاری را تمام می‌کند، هر سفارش با نتیجهٔ نامعلوم را یک بار جستجو می‌کند (و اگر یافت پذیرفته می‌شود)، سفارش‌های باز را لغو می‌کند و گزارش موقت می‌نویسد؛ تا ۶۰ ثانیه (`TimeoutStopSec`) برای این کار وقت دارد. `wa kill` (با همان `--env-file` سرویس، که alias بالا می‌دهد) مسیر مطلق فایل KILL را چاپ می‌کند؛ تا وقتی KILL هست، systemd سرویس را شروع نمی‌کند. اگر `kill` با کد ۶ گفت در `DATA_DIR` هیچ دفتری پیدا نکرد، فایل محیطی درست را نداده‌اید.

اگر سرویس هنوز در حال اجراست، `wa kill` به‌تنهایی هم کافی است: ربات ظرف چند ثانیه سفارش‌هایش را لغو می‌کند، STOPPED می‌شود و با کد ۴ خارج می‌شود (و systemd دوباره اجرایش نمی‌کند).

**مکث قابل ادامه** (سفارش‌های باز لغو می‌شوند و وضعیت قابل ادامه است، کد ۱۳۰):

```bash
sudo systemctl stop wallex-agent
sudo rm /var/lib/wallex-agent/state/LIVE_ARMED
```

پاک کردن `LIVE_ARMED` جلوی شروع خودکار بعد از ریبوت را می‌گیرد. برای ادامه، `wa arm-live --i-understand-real-money` و سپس `sudo systemctl start wallex-agent`.

**بعد از کرش، اگر سرویس پایین است و سفارشی باز مانده:**

```bash
wa kill --cancel-open --reason "service down with an open order"
```

هر وقت دفتر live وجود داشته باشد (صرف‌نظر از `TRADING_MODE` و `--mode`) و سرویسی آن را در دست نداشته باشد، با یک کلاینت «فقط لغو» (که ثبت سفارش در آن ناممکن است) فقط سفارش‌های ثبت‌شده در دفتر همین ربات را لغو می‌کند.

**ادامه پس از KILL یا توقف ایمنی:** حساب را در اپ والکس بررسی کنید، فایل KILL را خودتان پاک کنید (`sudo -u wallex-agent rm /var/lib/wallex-agent/state/KILL`)، سپس `wa unstop --mode live --i-understand-real-money`. `unstop` با یک کلاینت فقط‌خواندنی تطبیق را تکرار می‌کند و فقط اگر تمیز بود دفتر را از STOPPED خارج می‌کند (بخش ۱۰ [`RUNBOOK_LIVE_FA.md`](RUNBOOK_LIVE_FA.md)). بعد `sudo systemctl start wallex-agent`.

> هیچ‌کدام از این روش‌ها **چیزی نمی‌فروشند.** برای صاف کردن دستی موقعیت در اپ والکس و پیامدهای آن، بخش ۱۰ [`RUNBOOK_LIVE_FA.md`](RUNBOOK_LIVE_FA.md) را ببینید.

## ۱۲. پس از مسابقه

```bash
wa report --mode live
sudo rm -f /var/lib/wallex-agent/state/LIVE_ARMED
sudo systemctl disable wallex-agent
```

فایل‌های `final_report.json`، `audit.jsonl` و `trace.txt` در `/var/lib/wallex-agent/state/live/runs/live-<id>/` هستند. `wa report` نسخهٔ بازسازی‌شده را جداگانه در `final_report.rebuilt.json` می‌نویسد و `final_report.json` ربات را تغییر نمی‌دهد. اگر کلید API دیگر لازم نیست، در سایت والکس حذفش کنید.
