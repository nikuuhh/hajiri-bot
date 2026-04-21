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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ERP_BASE  = "https://erp.sgtu.in"

USERNAME, PASSWORD, CAPTCHA = range(3)
sessions = {}


# ─────────────────────────────────────────────────────────────────────────────
# ERP
# ─────────────────────────────────────────────────────────────────────────────

def get_login_page():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    try:
        r = s.get(f"{ERP_BASE}/Default.aspx", timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        return s, soup, r.text, None
    except Exception as e:
        return None, None, None, str(e)


def extract_aspnet_fields(soup):
    fields = {}
    for tag in soup.find_all("input", type="hidden"):
        if tag.get("name"):
            fields[tag["name"]] = tag.get("value", "")
    return fields


def extract_captcha_from_html(soup, raw_html):
    """
    The SGT ERP captcha is plain styled text in the HTML — not an image.
    From screenshot: numbers like '6 1 8 6' displayed next to a refresh icon.
    We try multiple strategies to extract it.
    """

    # Strategy 1: hdncaptcha hidden field may already contain the value
    cap_field = soup.find("input", {"name": "hdncaptcha"})
    if cap_field:
        val = cap_field.get("value", "").strip()
        if val and val.isdigit():
            log.info(f"Captcha from hdncaptcha field: {val}")
            return val

    # Strategy 2: Look for a label/span/div with short numeric text
    for tag in soup.find_all(["label", "span", "div", "td", "p", "h4", "h5", "strong", "b"]):
        text = tag.get_text(strip=True).replace(" ", "")
        if text.isdigit() and 3 <= len(text) <= 8:
            log.info(f"Captcha from <{tag.name}> id={tag.get('id')}: {text}")
            return text

    # Strategy 3: Scan raw HTML for patterns like value="1234" near captcha
    import re
    # Find captcha-related section and grab nearby numbers
    lower = raw_html.lower()
    cap_idx = lower.find("captcha")
    if cap_idx > 0:
        chunk = raw_html[max(0, cap_idx-300):cap_idx+500]
        log.info(f"HTML around 'captcha': {chunk}")
        # Look for 4-digit number in that chunk
        matches = re.findall(r'\b(\d{4,6})\b', chunk)
        if matches:
            log.info(f"Captcha candidates near 'captcha' keyword: {matches}")
            return matches[0]

    # Strategy 4: Any 4-digit number in the whole page
    matches = re.findall(r'\b(\d{4})\b', raw_html)
    # Filter out years and common numbers
    candidates = [m for m in matches if not m.startswith("20") and m not in ["1000","2000","9999"]]
    if candidates:
        log.info(f"Fallback captcha candidates: {candidates[:5]}")
        # Return most frequent one (likely the captcha repeated in hidden field)
        from collections import Counter
        most_common = Counter(candidates).most_common(1)[0][0]
        return most_common

    log.warning("Could not extract captcha from HTML")
    return None


def do_login(session, soup, username, password, captcha_text):
    fields = extract_aspnet_fields(soup)

    fields["hdnusername"] = username
    fields["hdnpassword"] = password
    if captcha_text:
        fields["hdncaptcha"] = captcha_text

    # Set visible inputs dynamically
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
        elif any(x in nl for x in ["captcha", "cap", "verify", "code"]):
            if captcha_text:
                fields[name] = captcha_text

    fields["__EVENTTARGET"]   = ""
    fields["__EVENTARGUMENT"] = ""

    btn = soup.find("input", {"type": "submit"}) or soup.find("button", {"type": "submit"})
    if btn and btn.get("name"):
        fields[btn["name"]] = btn.get("value", "Login")

    safe = {k: v for k, v in fields.items() if "STATE" not in k.upper()}
    log.info(f"POSTing: {safe}")

    try:
        r = session.post(
            f"{ERP_BASE}/Default.aspx",
            data=fields,
            timeout=15,
            allow_redirects=True
        )
        log.info(f"Login → status={r.status_code} url={r.url}")
        log.info(f"Login response: {r.text[:600]}")

        if "StudeHome" in r.url:
            return True, None
        if "Default.aspx" in r.url:
            return False, "Wrong credentials or captcha."
        return False, f"Unexpected redirect: {r.url}"

    except Exception as e:
        return False, str(e)


def fetch_attendance(session):
    try:
        r = session.post(
            f"{ERP_BASE}/StudeHome.aspx/ShowAttPer",
            headers={
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
            json={},
            timeout=10
        )
        log.info(f"ShowAttPer → {r.status_code} | {r.text[:300]}")

        try:
            data = r.json()
            if "d" in data and data["d"] and data["d"] != "0.00":
                return {"overall": data["d"]}, None
        except Exception:
            log.warning("ShowAttPer not JSON")

        r2 = session.get(f"{ERP_BASE}/StudeHome.aspx", timeout=10)
        log.info(f"StudeHome → {r2.status_code} | url={r2.url}")
        log.info(f"StudeHome body: {r2.text[:2000]}")

        soup = BeautifulSoup(r2.text, "html.parser")
        tables = soup.find_all("table")
        log.info(f"Tables: {len(tables)}")
        for i, t in enumerate(tables):
            for row in t.find_all("tr")[:4]:
                cols = [td.get_text(strip=True) for td in row.find_all("td")]
                if cols:
                    log.info(f"  T{i}: {cols}")

        rows = scrape_attendance_table(soup)
        if rows:
            return rows, None

        return None, "ERP returned empty data"

    except Exception as e:
        log.error(f"fetch_attendance: {e}")
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
    return f"📋 Raw:\n`{json.dumps(data, indent=2)}`"


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
        "Commands:\n"
        "• /login — Login with ERP credentials\n"
        "• /attendance — View your attendance\n"
        "• /bunk <present> <total> — Bunk calculator\n"
        "• /logout — Clear session\n"
        "• /demo — Demo attendance",
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
    await update.message.reply_text("⏳ Loading ERP login page...")

    session, soup, raw_html, err = get_login_page()
    if err:
        await update.message.reply_text(f"❌ Could not reach ERP: {err}\n\nUse /demo for mock data.")
        return ConversationHandler.END

    ctx.user_data["erp_session"] = session
    ctx.user_data["erp_soup"]    = soup

    # Try to auto-extract captcha from HTML
    captcha_val = extract_captcha_from_html(soup, raw_html)

    if captcha_val:
        log.info(f"Auto-extracted captcha: {captcha_val}")
        await update.message.reply_text(f"🔢 Captcha detected: `{captcha_val}`\n⏳ Logging in...", parse_mode="Markdown")
        return await _attempt_login(update, ctx, captcha_val)
    else:
        # Could not auto-extract — ask user manually
        await update.message.reply_text(
            "🔢 Please open https://erp.sgtu.in and type the *captcha number* shown on the login page:",
            parse_mode="Markdown"
        )
        return CAPTCHA

async def login_captcha(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    captcha_text = "".join(c for c in update.message.text.strip() if c.isdigit())
    log.info(f"Manual captcha entered: {captcha_text}")
    return await _attempt_login(update, ctx, captcha_text)

async def _attempt_login(update, ctx, captcha_text):
    session  = ctx.user_data.get("erp_session")
    soup     = ctx.user_data.get("erp_soup")
    username = ctx.user_data.get("username")
    password = ctx.user_data.get("password")

    await update.message.reply_text("⏳ Logging in...")
    success, err = do_login(session, soup, username, password, captcha_text)

    if not success:
        await update.message.reply_text(f"❌ {err}\n\nTry /login again.")
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
        await update.message.reply_text(
            f"⚠️ Could not fetch real data (`{err}`)\n\n{format_mock_attendance()}",
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
    await update.message.reply_text("✅ Logged out.")


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
