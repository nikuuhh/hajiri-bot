"""
Hajiri Bot — SGT University Attendance Bot
Real ERP scraping with mock fallback.
"""

import os
import json
import logging
import requests
from bs4 import BeautifulSoup
from telegram import Update, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
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
    fields = {}
    for tag in soup.find_all("input", type="hidden"):
        if tag.get("name"):
            fields[tag["name"]] = tag.get("value", "")
    return fields


def get_captcha_image(session, soup):
    for possible_id in ["imgCaptcha", "CaptchaImage", "imgcaptcha", "img_captcha"]:
        img_tag = soup.find("img", {"id": possible_id})
        if img_tag:
            src = img_tag.get("src", "")
            if not src.startswith("http"):
                src = ERP_BASE + "/" + src.lstrip("/")
            try:
                r = session.get(src, timeout=10)
                return r.content, None
            except Exception as e:
                return None, str(e)
    return None, "Captcha image not found"


def do_login(session, soup, username, password, captcha_text):
    # Pull all hidden ASP.NET fields
    fields = extract_aspnet_fields(soup)

    # SGT ERP confirmed hidden credential fields from live HTML inspection
    fields["hdnusername"] = username
    fields["hdnpassword"] = password
    if captcha_text:
        fields["hdncaptcha"] = captcha_text

    # Also set any visible text/password inputs found dynamically
    for tag in soup.find_all("input"):
        itype = (tag.get("type") or "text").lower()
        if itype not in ["text", "password", "email"]:
            continue
        name = tag.get("name") or tag.get("id", "")
        if not name:
            continue
        nl = name.lower()
        if any(x in nl for x in ["user", "userid", "enroll", "login"]):
            fields[name] = username
        elif any(x in nl for x in ["pass", "pwd", "password"]):
            fields[name] = password
        elif any(x in nl for x in ["captcha", "cap", "verify"]):
            if captcha_text:
                fields[name] = captcha_text

    fields["__EVENTTARGET"]   = ""
    fields["__EVENTARGUMENT"] = ""

    # Submit button
    btn = soup.find("input", {"type": "submit"}) or soup.find("button", {"type": "submit"})
    if btn and btn.get("name"):
        fields[btn["name"]] = btn.get("value", "Login")

    log.info(f"Attempting login for: {username}")

    try:
        r = session.post(
            f"{ERP_BASE}/Default.aspx",
            data=fields,
            timeout=15,
            allow_redirects=True
        )
        log.info(f"Login → status={r.status_code} url={r.url}")
        log.info(f"Response preview: {r.text[:500]}")

        log.info(f"Login POST url={r.url}")
        log.info(f"Login POST body={r.text[:1000]}")

        # STRICT check: only trust a URL change to StudeHome
        if "StudeHome" in r.url:
            return True, None

        # Still on Default.aspx = login failed
        if "Default.aspx" in r.url:
            return False, "Wrong username or password."

        # Fallback: unknown redirect
        log.warning(f"Unknown redirect after login: {r.url}")
        return False, f"Unexpected page after login: {r.url}"

    except Exception as e:
        return False, str(e)


def fetch_attendance(session):
    try:
        # Step 1: Try JSON API
        r = session.post(
            f"{ERP_BASE}/StudeHome.aspx/ShowAttPer",
            headers={
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
            json={},
            timeout=10
        )
        log.info(f"ShowAttPer status={r.status_code}")
        log.info(f"ShowAttPer body={r.text[:500]}")

        try:
            data = r.json()
            if "d" in data and data["d"] and data["d"] != "0.00":
                return {"overall": data["d"]}, None
        except Exception:
            log.warning("ShowAttPer not JSON")

        # Step 2: Load dashboard and log structure
        r2 = session.get(f"{ERP_BASE}/StudeHome.aspx", timeout=10)
        log.info(f"StudeHome status={r2.status_code} url={r2.url}")
        log.info(f"StudeHome body={r2.text[:2000]}")

        soup = BeautifulSoup(r2.text, "html.parser")
        tables = soup.find_all("table")
        log.info(f"Tables found: {len(tables)}")
        for i, t in enumerate(tables):
            rows = t.find_all("tr")
            log.info(f"  Table {i}: {len(rows)} rows")
            for row in rows[:3]:
                cols = [td.get_text(strip=True) for td in row.find_all("td")]
                if cols:
                    log.info(f"    Row: {cols}")

        rows = scrape_attendance_table(soup)
        if rows:
            return rows, None

        return None, "ERP returned empty data"

    except Exception as e:
        log.error(f"fetch_attendance error: {e}")
        return None, str(e)


def scrape_attendance_table(soup):
    results = []
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cols = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cols) >= 3:
                for col in cols:
                    try:
                        val = float(col.replace("%", ""))
                        if 0 < val <= 100:
                            results.append(cols)
                            break
                    except ValueError:
                        continue
    return results if results else None


# ─────────────────────────────────────────────────────────────────────────────
# MOCK FALLBACK
# ─────────────────────────────────────────────────────────────────────────────

MOCK_ATTENDANCE = [
    {"subject": "Mathematics",      "present": 38, "total": 45, "percent": 84.4},
    {"subject": "Physics",          "present": 30, "total": 40, "percent": 75.0},
    {"subject": "Chemistry",        "present": 35, "total": 42, "percent": 83.3},
    {"subject": "English",          "present": 28, "total": 38, "percent": 73.7},
    {"subject": "Computer Science", "present": 42, "total": 46, "percent": 91.3},
]

def format_mock_attendance():
    lines = ["📋 *Attendance Report* (Demo Data)\n"]
    for s in MOCK_ATTENDANCE:
        emoji = "✅" if s["percent"] >= 75 else "⚠️"
        lines.append(f"{emoji} *{s['subject']}*")
        lines.append(f"   {s['present']}/{s['total']} — `{s['percent']}%`\n")
    lines.append("_⚠️ This is demo data. Use /login for real data._")
    return "\n".join(lines)


def format_real_attendance(data):
    if isinstance(data, dict) and "overall" in data:
        return f"📋 *Attendance Report*\n\n📊 Overall: `{data['overall']}%`"
    if isinstance(data, list):
        lines = ["📋 *Attendance Report*\n"]
        for row in data:
            if isinstance(row, list) and len(row) >= 3:
                lines.append(f"• {' | '.join(str(c) for c in row)}")
            elif isinstance(row, dict):
                pct = row.get("percent", "?")
                emoji = "✅" if isinstance(pct, (int, float)) and pct >= 75 else "⚠️"
                lines.append(f"{emoji} *{row.get('subject','Subject')}*")
                lines.append(f"   {row.get('present','?')}/{row.get('total','?')} — `{pct}%`\n")
        return "\n".join(lines)
    return f"📋 Raw data:\n`{json.dumps(data, indent=2)}`"


# ─────────────────────────────────────────────────────────────────────────────
# BUNK CALCULATOR
# ─────────────────────────────────────────────────────────────────────────────

def bunk_calc(present, total, target=75):
    percent = (present / total * 100) if total else 0
    if percent >= target:
        can_bunk = int((present * 100 / target) - total)
        return percent, f"✅ You can bunk *{can_bunk}* more class(es) and stay above {target}%."
    else:
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
        "• /bunk <present> <total> — Bunk calculator\n"
        "• /logout — Clear your session\n"
        "• /demo — See demo attendance",
        parse_mode="Markdown"
    )

async def demo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(format_mock_attendance(), parse_mode="Markdown")

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
    await update.message.reply_text("⏳ Connecting to ERP...")

    session, soup, err = get_login_page()
    if err:
        await update.message.reply_text(
            f"❌ Could not reach ERP: `{err}`\n\nUse /demo for mock data.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    ctx.user_data["erp_session"] = session
    ctx.user_data["erp_soup"]    = soup

    cap_bytes, cap_err = get_captcha_image(session, soup)
    if cap_err or not cap_bytes:
        await update.message.reply_text("🔄 No captcha required, logging in...")
        return await _attempt_login(update, ctx, captcha_text="")

    await update.message.reply_photo(
        photo=cap_bytes,
        caption="🔢 Enter the *captcha* shown above:",
        parse_mode="Markdown"
    )
    return CAPTCHA

async def login_captcha(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    return await _attempt_login(update, ctx, update.message.text.strip())

async def _attempt_login(update, ctx, captcha_text):
    session  = ctx.user_data.get("erp_session")
    soup     = ctx.user_data.get("erp_soup")
    username = ctx.user_data.get("username")
    password = ctx.user_data.get("password")

    await update.message.reply_text("⏳ Logging in...")
    success, err = do_login(session, soup, username, password, captcha_text)

    if not success:
        await update.message.reply_text(f"❌ Login failed: {err}\n\nTry /login again or use /demo.")
        return ConversationHandler.END

    sessions[update.effective_user.id] = session
    await update.message.reply_text(
        "✅ *Logged in!*\n\nUse /attendance to fetch your data.",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def login_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def attendance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    session = sessions.get(uid)

    if not session:
        await update.message.reply_text("⚠️ Not logged in. Use /login first, or /demo for mock data.")
        return

    await update.message.reply_text("⏳ Fetching attendance from ERP...")
    data, err = fetch_attendance(session)

    if err or not data:
        log.warning(f"Attendance fetch failed: {err} — showing mock")
        await update.message.reply_text(
            f"⚠️ Could not fetch real data (`{err}`)\n\nShowing demo data:\n\n{format_mock_attendance()}",
            parse_mode="Markdown"
        )
        return

    await update.message.reply_text(format_real_attendance(data), parse_mode="Markdown")

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
    sessions.pop(update.effective_user.id, None)
    ctx.user_data.clear()
    await update.message.reply_text("✅ Logged out. Use /login to login again.")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

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
