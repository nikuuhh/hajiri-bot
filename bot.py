"""
Hajiri Bot — SGT University Attendance Bot
Architecture:
  - Playwright renders SGT ERP login page (JS canvas captcha)
  - Tesseract OCR reads the captcha digits server-side
  - Credentials saved encrypted to disk for auto re-login
  - Cookies saved to disk — reused until session expires
  - On session expiry: auto re-login silently, no user interaction needed
"""

import os
import sys
import json
import logging
import asyncio
import subprocess
import requests
from io import BytesIO
from PIL import Image, ImageFilter, ImageOps
import pytesseract
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet
from telegram import Update, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# INSTALL PLAYWRIGHT BROWSER AT STARTUP
# This ensures the Chromium binary is always present regardless of build cache
# ─────────────────────────────────────────────────────────────────────────────

def ensure_playwright_browser():
    log.info("Ensuring Playwright Chromium is installed...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            log.info("Playwright Chromium ready.")
        else:
            log.warning(f"playwright install stderr: {result.stderr[:300]}")
    except Exception as e:
        log.error(f"Could not install Playwright browser: {e}")

ensure_playwright_browser()

# ─────────────────────────────────────────────────────────────────────────────

BOT_TOKEN   = os.environ["BOT_TOKEN"]
ENCRYPT_KEY = os.environ.get("ENCRYPT_KEY", "")
ERP_BASE    = "https://erp.sgtu.in"
DATA_FILE   = "userdata.json"

USERNAME_STATE, PASSWORD_STATE = range(2)

# ─────────────────────────────────────────────────────────────────────────────
# ENCRYPTION
# ─────────────────────────────────────────────────────────────────────────────

def get_fernet() -> Fernet:
    key = ENCRYPT_KEY.strip()
    if not key:
        key = Fernet.generate_key().decode()
        log.warning(f"ENCRYPT_KEY not set. Generated: {key}")
        log.warning("Set this as ENCRYPT_KEY in Railway environment variables.")
    if isinstance(key, str):
        key = key.encode()
    return Fernet(key)

def encrypt(text: str) -> str:
    return get_fernet().encrypt(text.encode()).decode()

def decrypt(token: str) -> str:
    try:
        return get_fernet().decrypt(token.encode()).decode()
    except Exception:
        return ""

# ─────────────────────────────────────────────────────────────────────────────
# USER DATA  (credentials + cookies, persisted to disk)
# ─────────────────────────────────────────────────────────────────────────────

def load_data() -> dict:
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_data(data: dict):
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log.error(f"save_data error: {e}")

def save_user(uid: int, username: str, password: str, cookies: dict):
    data = load_data()
    data[str(uid)] = {
        "username": encrypt(username),
        "password": encrypt(password),
        "cookies":  cookies
    }
    save_data(data)
    log.info(f"Saved user {uid}")

def get_user(uid: int) -> dict | None:
    data = load_data()
    entry = data.get(str(uid))
    if not entry:
        return None
    return {
        "username": decrypt(entry["username"]),
        "password": decrypt(entry["password"]),
        "cookies":  entry.get("cookies", {})
    }

def update_cookies(uid: int, cookies: dict):
    data = load_data()
    if str(uid) in data:
        data[str(uid)]["cookies"] = cookies
        save_data(data)

def delete_user(uid: int):
    data = load_data()
    data.pop(str(uid), None)
    save_data(data)

def restore_session(cookies: dict) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    for name, value in cookies.items():
        s.cookies.set(name, value)
    return s

def is_session_alive(session: requests.Session) -> bool:
    try:
        r = session.get(f"{ERP_BASE}/StudeHome.aspx", timeout=8, allow_redirects=True)
        alive = "StudeHome" in r.url
        log.info(f"Session alive={alive} url={r.url}")
        return alive
    except Exception as e:
        log.warning(f"Session check error: {e}")
        return False

# ─────────────────────────────────────────────────────────────────────────────
# PLAYWRIGHT  — renders login page, returns captcha PNG + hidden fields
# ─────────────────────────────────────────────────────────────────────────────

async def render_login_page() -> tuple[bytes | None, dict, str | None]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None, {}, "Playwright not installed"

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )
            page = await browser.new_page()
            await page.goto(f"{ERP_BASE}/Default.aspx", wait_until="networkidle", timeout=30000)

            try:
                await page.wait_for_selector("#captchaCanvas", timeout=10000)
                await asyncio.sleep(1)
            except Exception:
                log.warning("captchaCanvas not found — using fallback crop")

            hidden_fields = await page.evaluate("""() => {
                const f = {};
                document.querySelectorAll('input[type=hidden]').forEach(el => {
                    if (el.name) f[el.name] = el.value;
                });
                return f;
            }""")

            try:
                el = await page.query_selector("#captchaCanvas")
                png_bytes = await el.screenshot() if el else None
                if not png_bytes:
                    raise Exception("no element")
            except Exception:
                png_bytes = await page.screenshot(
                    clip={"x": 0, "y": 150, "width": 600, "height": 250}
                )

            await browser.close()
            return png_bytes, hidden_fields, None

    except Exception as e:
        log.error(f"Playwright error: {e}")
        return None, {}, str(e)

# ─────────────────────────────────────────────────────────────────────────────
# TESSERACT OCR
# ─────────────────────────────────────────────────────────────────────────────

def ocr_captcha(png_bytes: bytes) -> str | None:
    try:
        img = Image.open(BytesIO(png_bytes)).convert("L")

        # Upscale 3x for accuracy
        w, h = img.size
        img = img.resize((w * 3, h * 3), Image.LANCZOS)

        # Threshold to black/white
        img = img.point(lambda x: 0 if x < 140 else 255)
        img = img.filter(ImageFilter.SHARPEN)

        config = "--psm 7 -c tessedit_char_whitelist=0123456789"
        result = pytesseract.image_to_string(img, config=config).strip()
        digits = "".join(c for c in result if c.isdigit())
        log.info(f"OCR raw='{result}' digits='{digits}'")

        if 3 <= len(digits) <= 7:
            return digits

        # Retry with inverted image
        img_inv = ImageOps.invert(img)
        result2 = pytesseract.image_to_string(img_inv, config=config).strip()
        digits2 = "".join(c for c in result2 if c.isdigit())
        log.info(f"OCR retry digits='{digits2}'")

        if 3 <= len(digits2) <= 7:
            return digits2

        return digits if digits else None

    except Exception as e:
        log.error(f"OCR error: {e}")
        return None

# ─────────────────────────────────────────────────────────────────────────────
# ERP LOGIN
# ─────────────────────────────────────────────────────────────────────────────

def do_login(username: str, password: str, captcha_text: str, hidden_fields: dict) -> tuple[requests.Session | None, str | None]:
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})

    fields = dict(hidden_fields)

    if "__VIEWSTATE" not in fields:
        try:
            r0 = s.get(f"{ERP_BASE}/Default.aspx", timeout=10)
            soup0 = BeautifulSoup(r0.text, "html.parser")
            for tag in soup0.find_all("input", type="hidden"):
                if tag.get("name"):
                    fields[tag["name"]] = tag.get("value", "")
        except Exception as e:
            return None, f"Could not load ERP page: {e}"

    fields["hdnusername"] = username
    fields["hdnpassword"] = password
    fields["hdncaptcha"]  = captcha_text

    for key in list(fields.keys()):
        kl = key.lower()
        if "hdn" in kl:
            continue
        if any(x in kl for x in ["user", "userid", "enroll"]):
            fields[key] = username
        elif any(x in kl for x in ["pass", "pwd"]):
            fields[key] = password
        elif "captcha" in kl:
            fields[key] = captcha_text

    fields["__EVENTTARGET"]   = ""
    fields["__EVENTARGUMENT"] = ""

    try:
        r = s.post(f"{ERP_BASE}/Default.aspx", data=fields, timeout=15, allow_redirects=True)
        log.info(f"Login → {r.status_code} {r.url}")

        if "StudeHome" in r.url:
            return s, None

        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup.find_all(["span", "div", "label", "p"]):
            t = tag.get_text(strip=True)
            if any(w in t.lower() for w in ["invalid", "wrong", "incorrect", "captcha", "error"]):
                return None, t[:120]

        return None, "Login failed — wrong credentials or captcha."
    except Exception as e:
        return None, str(e)

async def full_login(username: str, password: str) -> tuple[requests.Session | None, str | None]:
    """Render page → OCR captcha → POST login. Retries up to 3 times."""
    for attempt in range(1, 4):
        log.info(f"Login attempt {attempt}/3")
        png_bytes, hidden_fields, err = await render_login_page()

        if err or not png_bytes:
            return None, f"Could not render ERP page: {err}"

        captcha_text = ocr_captcha(png_bytes)
        if not captcha_text:
            log.warning(f"OCR failed attempt {attempt}, retrying...")
            continue

        log.info(f"OCR captcha: '{captcha_text}'")
        session, err = do_login(username, password, captcha_text, hidden_fields)

        if session:
            return session, None

        log.warning(f"Login failed ({err}) attempt {attempt}")
        if err and any(w in err.lower() for w in ["invalid", "wrong", "incorrect"]):
            return None, err

        await asyncio.sleep(1)

    return None, "Login failed after 3 attempts. OCR may be misreading captcha — try /login again."

# ─────────────────────────────────────────────────────────────────────────────
# ATTENDANCE SCRAPING
# ─────────────────────────────────────────────────────────────────────────────

def fetch_attendance(session: requests.Session) -> tuple[dict | list | None, str | None]:
    try:
        r = session.post(
            f"{ERP_BASE}/StudeHome.aspx/ShowAttPer",
            headers={"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"},
            json={},
            timeout=10
        )
        log.info(f"ShowAttPer → {r.status_code} | {r.text[:200]}")
        try:
            data = r.json()
            if "d" in data and data["d"] and data["d"] != "0.00":
                return {"overall": data["d"]}, None
        except Exception:
            pass

        r2 = session.get(f"{ERP_BASE}/StudeHome.aspx", timeout=10)
        log.info(f"StudeHome → {r2.status_code} {r2.url}")

        if "Default.aspx" in r2.url:
            return None, "SESSION_EXPIRED"

        soup = BeautifulSoup(r2.text, "html.parser")
        rows = scrape_attendance_table(soup)
        if rows:
            return rows, None

        return None, "Attendance table not found on page."

    except Exception as e:
        log.error(f"fetch_attendance: {e}")
        return None, str(e)

def scrape_attendance_table(soup: BeautifulSoup) -> list | None:
    results = []
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cols = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cols) >= 3:
                for col in cols:
                    try:
                        val = float(col.replace("%", "").strip())
                        if 0 < val <= 100:
                            results.append(cols)
                            break
                    except ValueError:
                        continue
    return results if results else None

# ─────────────────────────────────────────────────────────────────────────────
# FORMATTERS
# ─────────────────────────────────────────────────────────────────────────────

MOCK_ATTENDANCE = [
    {"subject": "Mathematics",      "present": 38, "total": 45, "percent": 84.4},
    {"subject": "Physics",          "present": 30, "total": 40, "percent": 75.0},
    {"subject": "Chemistry",        "present": 35, "total": 42, "percent": 83.3},
    {"subject": "English",          "present": 28, "total": 38, "percent": 73.7},
    {"subject": "Computer Science", "present": 42, "total": 46, "percent": 91.3},
]

def format_mock() -> str:
    lines = ["📋 *Attendance Report* (Demo)\n"]
    for s in MOCK_ATTENDANCE:
        e = "✅" if s["percent"] >= 75 else "⚠️"
        lines.append(f"{e} *{s['subject']}*\n   {s['present']}/{s['total']} — `{s['percent']}%`\n")
    lines.append("_⚠️ Demo data. Use /login for real attendance._")
    return "\n".join(lines)

def format_attendance(data) -> str:
    if isinstance(data, dict) and "overall" in data:
        return f"📋 *Attendance Report*\n\n📊 Overall: `{data['overall']}%`"
    if isinstance(data, list):
        lines = ["📋 *Attendance Report*\n"]
        for row in data:
            if isinstance(row, list) and len(row) >= 3:
                lines.append(f"• {' | '.join(str(c) for c in row)}")
            elif isinstance(row, dict):
                pct = row.get("percent", "?")
                e   = "✅" if isinstance(pct, (int, float)) and pct >= 75 else "⚠️"
                lines.append(f"{e} *{row.get('subject','?')}*\n   {row.get('present','?')}/{row.get('total','?')} — `{pct}%`\n")
        return "\n".join(lines)
    return f"📋 Raw:\n`{data}`"

# ─────────────────────────────────────────────────────────────────────────────
# BUNK CALCULATOR
# ─────────────────────────────────────────────────────────────────────────────

def bunk_calc(present: int, total: int, target: int = 75) -> tuple:
    pct = (present / total * 100) if total else 0
    if pct >= target:
        can_bunk = int((present * 100 / target) - total)
        return pct, f"✅ You can bunk *{can_bunk}* more class(es) and stay above {target}%."
    else:
        needed = int(((target * total) - (100 * present)) / (100 - target)) + 1
        return pct, f"⚠️ Attend *{needed}* more class(es) to reach {target}%."

# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Welcome to Hajiri Bot!*\n\n"
        "Commands:\n"
        "• /login — Connect your ERP account\n"
        "• /attendance — View your attendance\n"
        "• /bunk <present> <total> — Bunk calculator\n"
        "• /logout — Remove your account\n"
        "• /demo — Demo attendance data",
        parse_mode="Markdown"
    )

async def demo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(format_mock(), parse_mode="Markdown")

async def login_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    user = get_user(uid)

    if user:
        session = restore_session(user["cookies"])
        if is_session_alive(session):
            await update.message.reply_text("✅ You're already logged in! Use /attendance.")
            return ConversationHandler.END

    await update.message.reply_text(
        "🔐 *ERP Login*\n\nEnter your *username* (enrollment number):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    return USERNAME_STATE

async def login_username(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["username"] = update.message.text.strip()
    await update.message.reply_text("🔑 Enter your *password*:", parse_mode="Markdown")
    return PASSWORD_STATE

async def login_password(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["password"] = update.message.text.strip()
    username = ctx.user_data["username"]
    password = ctx.user_data["password"]

    await update.message.reply_text(
        "⏳ Logging in automatically... (~20 seconds while we solve the captcha)"
    )

    session, err = await full_login(username, password)

    if err:
        await update.message.reply_text(f"❌ {err}\n\nTry /login again.", parse_mode="Markdown")
        ctx.user_data.clear()
        return ConversationHandler.END

    save_user(update.effective_user.id, username, password, dict(session.cookies))
    ctx.user_data.clear()

    await update.message.reply_text(
        "✅ *Logged in successfully!*\n\n"
        "Your session is saved. The bot will auto re-login if it ever expires — "
        "you won't need to login again.\n\n"
        "Use /attendance to fetch your data. 🎓",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def login_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("❌ Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def attendance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    user = get_user(uid)

    if not user:
        await update.message.reply_text("⚠️ Not logged in. Use /login first.")
        return

    await update.message.reply_text("⏳ Fetching your attendance...")
    session = restore_session(user["cookies"])
    data, err = fetch_attendance(session)

    if err == "SESSION_EXPIRED":
        await update.message.reply_text("🔄 Session expired — re-logging in automatically...")
        session, login_err = await full_login(user["username"], user["password"])
        if login_err:
            await update.message.reply_text(
                f"❌ Auto re-login failed: {login_err}\n\nTry /login again.",
                parse_mode="Markdown"
            )
            return
        update_cookies(uid, dict(session.cookies))
        data, err = fetch_attendance(session)

    if err or not data:
        await update.message.reply_text(
            f"⚠️ Could not fetch data (`{err}`)\n\n{format_mock()}",
            parse_mode="Markdown"
        )
        return

    update_cookies(uid, dict(session.cookies))
    await update.message.reply_text(format_attendance(data), parse_mode="Markdown")

async def bunk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text(
            "📐 *Bunk Calculator*\n\nUsage: `/bunk <present> <total>`\nExample: `/bunk 35 45`",
            parse_mode="Markdown"
        )
        return
    try:
        present = int(args[0])
        total   = int(args[1])
        target  = int(args[2]) if len(args) > 2 else 75
        pct, msg = bunk_calc(present, total, target)
        await update.message.reply_text(
            f"📐 *Bunk Calculator*\n\nPresent: `{present}/{total}` → `{pct:.1f}%`\n\n{msg}",
            parse_mode="Markdown"
        )
    except ValueError:
        await update.message.reply_text("❌ Numbers only. Example: `/bunk 35 45`", parse_mode="Markdown")

async def logout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    delete_user(update.effective_user.id)
    ctx.user_data.clear()
    await update.message.reply_text("✅ Logged out and credentials removed.")

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    login_conv = ConversationHandler(
        entry_points=[CommandHandler("login", login_start)],
        states={
            USERNAME_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_username)],
            PASSWORD_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_password)],
        },
        fallbacks=[CommandHandler("cancel", login_cancel)],
    )

    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("demo",       demo))
    app.add_handler(CommandHandler("attendance", attendance))
    app.add_handler(CommandHandler("bunk",       bunk))
    app.add_handler(CommandHandler("logout",     logout))
    app.add_handler(login_conv)

    log.info("Hajiri Bot started.")
    app.run_polling()

if __name__ == "__main__":
    main()
