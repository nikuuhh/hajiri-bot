"""
Hajiri Bot — SGT University Attendance Bot
Real ERP scraping with mock fallback.
"""

import os
import json
import logging
import requests
from bs4 import BeautifulSoup
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
ERP_BASE  = "https://erp.sgtu.in"

# ── Conversation states ───────────────────────────────────────────────────────
USERNAME, PASSWORD, CAPTCHA = range(3)

# ── In-memory session store: { user_id: requests.Session } ───────────────────
sessions = {}

# ─────────────────────────────────────────────────────────────────────────────
# ERP SCRAPING
# ─────────────────────────────────────────────────────────────────────────────

def get_login_page():
    """Fetch ERP login page and return (session, soup, error)."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    try:
        r = s.get(f"{ERP_BASE}/Default.aspx", timeout=10)
        r.raise_for_status()
        return s, BeautifulSoup(r.text, "html.parser"), None
    except Exception as e:
        return None, None, str(e)


def extract_aspnet_fields(soup):
    """Pull hidden ASP.NET form fields from login page."""
    fields = {}
    for tag in soup.find_all("input", type="hidden"):
        if tag.get("name"):
            fields[tag["name"]] = tag.get("value", "")
    return fields


def get_captcha_image(session, soup):
    """Download captcha image bytes. Returns (bytes, error)."""
    img_tag = soup.find("img", {"id": lambda x: x and "captcha" in x.lower()})
    if not img_tag:
        # Try common captcha image IDs used in SGT ERP
        for possible_id in ["imgCaptcha", "CaptchaImage", "imgcaptcha"]:
            img_tag = soup.find("img", {"id": possible_id})
            if img_tag:
                break
    if not img_tag:
        return None, "Captcha image not found in page"
    src = img_tag.get("src", "")
    if not src.startswith("http"):
        src = ERP_BASE + "/" + src.lstrip("/")
    try:
        r = session.get(src, timeout=10)
        return r.content, None
    except Exception as e:
        return None, str(e)


def do_login(session, soup, username, password, captcha_text):
    """
    Submit login form. Returns (success: bool, error: str|None).
    SGT ERP uses plain password (no RSA observed in form POST — 
    RSA may be JS-only client side validation, actual POST is plaintext).
    """
    fields = extract_aspnet_fields(soup)

    # Find username / password / captcha field names dynamically
    user_field = _find_field(soup, ["txtUserName", "txtUsername", "UserName", "username"])
    pass_field = _find_field(soup, ["txtPassword", "Password", "password", "txtPass"])
    cap_field  = _find_field(soup, ["txtCaptcha", "CaptchaInput", "captcha", "txtcaptcha"])

    if not user_field or not pass_field:
        return False, f"Could not find login fields. Found fields: {list(fields.keys())}"

    fields[user_field] = username
    fields[pass_field] = password
    if cap_field and captcha_text:
        fields[cap_field] = captcha_text

    # ASP.NET requires this
    fields["__EVENTTARGET"]   = fields.get("__EVENTTARGET", "")
    fields["__EVENTARGUMENT"] = fields.get("__EVENTARGUMENT", "")

    # Find submit button
    btn = soup.find("input", {"type": "submit"}) or soup.find("button", {"type": "submit"})
    if btn and btn.get("name"):
        fields[btn["name"]] = btn.get("value", "Login")

    try:
        r = session.post(
            f"{ERP_BASE}/Default.aspx",
            data=fields,
            timeout=15,
            allow_redirects=True
        )
        # If login succeeded, we should NOT be on Default.aspx anymore
        # or the page should contain student name / dashboard content
        if "StudeHome" in r.url or "studhome" in r.url.lower():
            return True, None
        if "logout" in r.text.lower() or "welcome" in r.text.lower():
            return True, None
        if "invalid" in r.text.lower() or "incorrect" in r.text.lower():
            return False, "Invalid username, password, or captcha."
        # Ambiguous — store session anyway and try fetching attendance
        return True, None
    except Exception as e:
        return False, str(e)


def _find_field(soup, candidates):
    """Find first matching input field name from candidates list."""
    for name in candidates:
        tag = soup.find("input", {"id": name}) or soup.find("input", {"name": name})
        if tag:
            return tag.get("name") or tag.get("id")
    return None


def fetch_attendance(session):
    """
    Call ShowAttPer endpoint with authenticated session.
    Returns (data: dict|list, error: str|None)
    """
    try:
        # First try the JSON API endpoint
        r = session.post(
            f"{ERP_BASE}/StudeHome.aspx/ShowAttPer",
            headers={
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
            json={},
            timeout=10
        )
        data = r.json()
        log.info(f"ShowAttPer response: {data}")

        # If d is a non-zero string percentage
        if "d" in data:
            d_val = data["d"]
            if d_val and d_val != "0.00":
                return {"overall": d_val}, None

        # Try scraping the attendance table from the page directly
        r2 = session.get(f"{ERP_BASE}/StudeHome.aspx", timeout=10)
        soup = BeautifulSoup(r2.text, "html.parser")
        attendance = scrape_attendance_table(soup)
        if attendance:
            return attendance, None

        return None, "ERP returned empty attendance data."

    except Exception as e:
        return None, str(e)


def scrape_attendance_table(soup):
    """Scrape attendance from HTML table on StudeHome page."""
    results = []
    # Look for tables containing attendance data
    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        for row in rows:
            cols = [td.get_text(strip=True) for td in row.find_all("td")]
            # Attendance rows typically have subject, present, total, percentage
            if len(cols) >= 3:
                # Check if any column looks like a percentage
                for i, col in enumerate(cols):
                    if "%" in col or (col.replace(".", "").isdigit() and 0 < float(col or 0) <= 100):
                        results.append(cols)
                        break
    return results if results else None


# ─────────────────────────────────────────────────────────────────────────────
# MOCK FALLBACK DATA
# ─────────────────────────────────────────────────────────────────────────────

MOCK_ATTENDANCE = [
    {"subject": "Mathematics",        "present": 38, "total": 45, "percent": 84.4},
    {"subject": "Physics",            "present": 30, "total": 40, "percent": 75.0},
    {"subject": "Chemistry",          "present": 35, "total": 42, "percent": 83.3},
    {"subject": "English",            "present": 28, "total": 38, "percent": 73.7},
    {"subject": "Computer Science",   "present": 42, "total": 46, "percent": 91.3},
]

def format_mock_attendance(name="Student"):
    lines = [f"📋 *Attendance Report* (Demo)\n👤 {name}\n"]
    for s in MOCK_ATTENDANCE:
        emoji = "✅" if s["percent"] >= 75 else "⚠️"
        lines.append(f"{emoji} *{s['subject']}*")
        lines.append(f"   {s['present']}/{s['total']} — `{s['percent']}%`")
    lines.append("\n_⚠️ This is demo data. Real ERP login coming soon._")
    return "\n".join(lines)


def format_real_attendance(data):
    """Format real ERP attendance data for Telegram."""
    if isinstance(data, dict) and "overall" in data:
        return f"📋 *Attendance Report*\n\n📊 Overall: `{data['overall']}%`"

    if isinstance(data, list):
        lines = ["📋 *Attendance Report*\n"]
        for row in data:
            if isinstance(row, dict):
                pct = row.get("percent", "?")
                emoji = "✅" if isinstance(pct, (int, float)) and pct >= 75 else "⚠️"
                lines.append(f"{emoji} *{row.get('subject','Subject')}*")
                lines.append(f"   {row.get('present','?')}/{row.get('total','?')} — `{pct}%`")
            elif isinstance(row, list) and len(row) >= 3:
                lines.append(f"• {' | '.join(str(c) for c in row)}")
        return "\n".join(lines)

    return f"📋 Attendance data:\n```{json.dumps(data, indent=2)}```"


# ─────────────────────────────────────────────────────────────────────────────
# BUNK CALCULATOR
# ─────────────────────────────────────────────────────────────────────────────

def bunk_calc(present, total, target=75):
    percent = (present / total * 100) if total else 0
    if percent >= target:
        # How many can we bunk?
        # (present) / (total + x) >= target/100  →  x <= present*100/target - total
        can_bunk = int((present * 100 / target) - total)
        return percent, f"✅ You can bunk *{can_bunk}* more class(es) and stay above {target}%."
    else:
        # How many to attend?
        # (present + x) / (total + x) >= target/100
        # x >= (target*total - 100*present) / (100 - target)
        needed = int(((target * total) - (100 * present)) / (100 - target)) + 1
        return percent, f"⚠️ Attend *{needed}* more class(es) to reach {target}%."


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Welcome to Hajiri Bot!*\n\n"
        "Check your SGT University attendance without logging into ERP.\n\n"
        "Commands:\n"
        "• /login — Login with ERP credentials\n"
        "• /attendance — View your attendance\n"
        "• /bunk — Bunk calculator\n"
        "• /logout — Clear your session\n"
        "• /demo — See demo attendance",
        parse_mode="Markdown"
    )


async def demo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        format_mock_attendance(),
        parse_mode="Markdown"
    )


# ── Login flow ────────────────────────────────────────────────────────────────

async def login_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔐 *ERP Login*\n\nEnter your *username* (enrollment number):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    return USERNAME


async def login_username(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["username"] = update.message.text.strip()
    await update.message.reply_text("🔑 Enter your *password*:", parse_mode="Markdown")
    return PASSWORD


async def login_password(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["password"] = update.message.text.strip()

    await update.message.reply_text("⏳ Fetching captcha from ERP...")

    session, soup, err = get_login_page()
    if err:
        await update.message.reply_text(
            f"❌ Could not reach ERP: `{err}`\n\nUse /demo for mock data.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    ctx.user_data["erp_session"] = session
    ctx.user_data["erp_soup"]    = soup

    # Try to get captcha
    cap_bytes, cap_err = get_captcha_image(session, soup)

    if cap_err or not cap_bytes:
        # No captcha found — try logging in directly
        await update.message.reply_text("🔄 No captcha detected, attempting login...")
        return await _attempt_login(update, ctx, captcha_text="")

    # Send captcha image to user
    await update.message.reply_photo(
        photo=cap_bytes,
        caption="🔢 Enter the *captcha* shown above:",
        parse_mode="Markdown"
    )
    return CAPTCHA


async def login_captcha(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    captcha_text = update.message.text.strip()
    return await _attempt_login(update, ctx, captcha_text)


async def _attempt_login(update, ctx, captcha_text):
    session  = ctx.user_data.get("erp_session")
    soup     = ctx.user_data.get("erp_soup")
    username = ctx.user_data.get("username")
    password = ctx.user_data.get("password")

    await update.message.reply_text("⏳ Logging in...")

    success, err = do_login(session, soup, username, password, captcha_text)

    if not success:
        await update.message.reply_text(
            f"❌ Login failed: {err}\n\nTry /login again or use /demo.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    # Store session keyed by Telegram user ID
    sessions[update.effective_user.id] = session
    await update.message.reply_text(
        "✅ *Logged in successfully!*\n\nUse /attendance to fetch your data.",
        parse_mode="Markdown"
    )
    return ConversationHandler.END


async def login_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Login cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ── Attendance ────────────────────────────────────────────────────────────────

async def attendance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    session = sessions.get(uid)

    if not session:
        await update.message.reply_text(
            "⚠️ You're not logged in.\n\nUse /login to login, or /demo for mock data."
        )
        return

    await update.message.reply_text("⏳ Fetching attendance from ERP...")
    data, err = fetch_attendance(session)

    if err or not data:
        log.warning(f"Attendance fetch failed: {err} — falling back to mock")
        await update.message.reply_text(
            f"⚠️ Could not fetch real data (`{err}`)\n\n"
            "Showing demo data instead:\n\n" + format_mock_attendance(),
            parse_mode="Markdown"
        )
        return

    await update.message.reply_text(format_real_attendance(data), parse_mode="Markdown")


# ── Bunk calculator ───────────────────────────────────────────────────────────

async def bunk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text(
            "📐 *Bunk Calculator*\n\n"
            "Usage: `/bunk <present> <total>`\n"
            "Example: `/bunk 35 45`\n\n"
            "Optional target: `/bunk 35 45 80`",
            parse_mode="Markdown"
        )
        return
    try:
        present = int(args[0])
        total   = int(args[1])
        target  = int(args[2]) if len(args) > 2 else 75
        pct, msg = bunk_calc(present, total, target)
        await update.message.reply_text(
            f"📐 *Bunk Calculator*\n\n"
            f"Present: `{present}/{total}` → `{pct:.1f}%`\n\n{msg}",
            parse_mode="Markdown"
        )
    except ValueError:
        await update.message.reply_text("❌ Use numbers only. Example: `/bunk 35 45`", parse_mode="Markdown")


# ── Logout ────────────────────────────────────────────────────────────────────

async def logout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in sessions:
        del sessions[uid]
    ctx.user_data.clear()
    await update.message.reply_text("✅ Logged out. Use /login to login again.")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    login_conv = ConversationHandler(
        entry_points=[CommandHandler("login", login_start)],
        states={
            USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_username)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_password)],
            CAPTCHA:  [MessageHandler(filters.TEXT & ~filters.COMMAND, login_captcha)],
        },
        fallbacks=[CommandHandler("cancel", login_cancel)],
    )

    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("demo",       demo))
    app.add_handler(CommandHandler("attendance", attendance))
    app.add_handler(CommandHandler("bunk",       bunk))
    app.add_handler(CommandHandler("logout",     logout))
    app.add_handler(login_conv)

    log.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
