"""
Hajiri Bot — SGT University Attendance Bot
Uses Playwright (headless Chromium) for automated ERP login + captcha.
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

USERNAME, PASSWORD = range(2)

# cookies stored per Telegram user ID
user_cookies = {}

# ─────────────────────────────────────────────────────────────────────────────
# ERP LOGIN — Playwright headless browser
# ─────────────────────────────────────────────────────────────────────────────

async def erp_login(username, password):
    """
    Launches headless Chromium, opens ERP login page,
    reads the JS-rendered captcha directly from the DOM,
    fills all fields and submits.
    Returns (cookie_dict, error) tuple.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None, "Playwright not installed. Check requirements.txt."

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ]
            )
            context = await browser.new_context()
            page    = await context.new_page()

            log.info("Opening ERP login page...")
            await page.goto(f"{ERP_BASE}/Default.aspx", wait_until="networkidle", timeout=30000)

            # Give JS time to render captcha
            await page.wait_for_timeout(2000)

            # ── Step 1: Extract captcha from DOM ────────────────────────────
            captcha_text = None

            # Try known element selectors first
            for sel in [
                "#lblCaptcha", "#CaptchaLabel", "#lbl_captcha",
                "span[id*='aptcha']", "label[id*='aptcha']",
                "div[id*='aptcha']", ".captcha",
            ]:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        t = (await el.inner_text()).strip().replace(" ", "")
                        if t.isdigit() and 3 <= len(t) <= 8:
                            captcha_text = t
                            log.info(f"Captcha via selector '{sel}': {captcha_text}")
                            break
                except Exception:
                    continue

            # Full DOM scan — find any element whose text is purely 4-6 digits
            if not captcha_text:
                candidates = await page.evaluate("""() => {
                    const found = [];
                    document.querySelectorAll('*').forEach(el => {
                        const raw   = (el.innerText || el.textContent || '').trim();
                        const clean = raw.replace(/\\s+/g, '');
                        if (/^\\d{4,6}$/.test(clean)) {
                            found.push({ tag: el.tagName, id: el.id, cls: el.className, text: clean });
                        }
                    });
                    return found;
                }""")
                log.info(f"DOM numeric candidates: {candidates}")

                SKIP = {"2025", "2026", "2024", "8080", "4000", "3000", "1433", "1234", "0000"}
                for item in candidates:
                    if item["text"] not in SKIP and not item["text"].startswith("20"):
                        captcha_text = item["text"]
                        log.info(f"Captcha from DOM scan: {captcha_text} (id={item['id']})")
                        break

            # Read hidden field value via JS as last resort
            if not captcha_text:
                val = await page.evaluate(
                    "() => { const e = document.querySelector('input[name=\"hdncaptcha\"]'); return e ? e.value : ''; }"
                )
                val = val.strip()
                if val.isdigit() and 3 <= len(val) <= 8:
                    captcha_text = val
                    log.info(f"Captcha from hdncaptcha JS: {captcha_text}")

            if not captcha_text:
                await page.screenshot(path="/tmp/erp_debug.png")
                log.error("Could not find captcha. Screenshot saved.")
                await browser.close()
                return None, "Could not read captcha from ERP page."

            log.info(f"Captcha to use: {captcha_text}")

            # ── Step 2: Fill credentials + captcha via JS ────────────────────
            await page.evaluate(f"""() => {{
                const setVal = (name, val) => {{
                    const e = document.querySelector('input[name="' + name + '"]');
                    if (e) e.value = val;
                }};
                setVal('hdnusername', '{username}');
                setVal('hdnpassword', '{password}');
                setVal('hdncaptcha',  '{captcha_text}');

                document.querySelectorAll('input[type="text"], input[type="password"]').forEach(el => {{
                    const n = (el.name || el.id || '').toLowerCase();
                    if (n.includes('user') || n.includes('enroll')) el.value = '{username}';
                    if (n.includes('pass') || n.includes('pwd'))    el.value = '{password}';
                    if (n.includes('captcha') || n.includes('cap')) el.value = '{captcha_text}';
                }});
            }}""")

            # ── Step 3: Click submit ─────────────────────────────────────────
            btn = (
                await page.query_selector("input[type='submit']") or
                await page.query_selector("button[type='submit']") or
                await page.query_selector("button")
            )
            if btn:
                await btn.click()
            else:
                await page.keyboard.press("Enter")

            await page.wait_for_load_state("networkidle", timeout=15000)

            final_url = page.url
            log.info(f"Post-login URL: {final_url}")

            # ── Step 4: Check success ────────────────────────────────────────
            if "StudeHome" in final_url:
                cookies     = await context.cookies()
                cookie_dict = {c["name"]: c["value"] for c in cookies}
                log.info(f"Login OK. Cookies: {list(cookie_dict.keys())}")
                await browser.close()
                return cookie_dict, None
            else:
                body = await page.inner_text("body")
                log.info(f"Login failed body: {body[:400]}")
                await browser.close()
                return None, "Login failed — wrong credentials or captcha mismatch."

    except Exception as e:
        log.error(f"erp_login error: {e}")
        return None, str(e)


# ─────────────────────────────────────────────────────────────────────────────
# ATTENDANCE FETCH — uses saved cookies
# ─────────────────────────────────────────────────────────────────────────────

def fetch_attendance(cookie_dict):
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    for name, value in cookie_dict.items():
        s.cookies.set(name, value, domain="erp.sgtu.in")

    try:
        # Try JSON API first
        r = s.post(
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
            pass

        # Fall back to scraping the dashboard HTML
        r2 = s.get(f"{ERP_BASE}/StudeHome.aspx", timeout=10)
        log.info(f"StudeHome → {r2.status_code} | url={r2.url}")
        log.info(f"StudeHome body: {r2.text[:2000]}")

        soup   = BeautifulSoup(r2.text, "html.parser")
        tables = soup.find_all("table")
        log.info(f"Tables found: {len(tables)}")

        for i, t in enumerate(tables[:5]):
            for row in t.find_all("tr")[:4]:
                cols = [td.get_text(strip=True) for td in row.find_all("td")]
                if cols:
                    log.info(f"  T{i}: {cols}")

        rows = scrape_attendance_table(soup)
        if rows:
            return rows, None

        return None, "ERP returned empty attendance data."

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
                pct   = row.get("percent", "?")
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
    username = ctx.user_data["username"]
    password = ctx.user_data["password"]

    await update.message.reply_text("⏳ Logging in via ERP (this may take 15–20 seconds)...")

    cookie_dict, err = await erp_login(username, password)

    if err:
        await update.message.reply_text(
            f"❌ Login failed: {err}\n\nTry /login again or use /demo.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    user_cookies[update.effective_user.id] = cookie_dict
    await update.message.reply_text(
        "✅ *Logged in successfully!*\n\nUse /attendance to fetch your real data.",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def login_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def attendance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid     = update.effective_user.id
    cookies = user_cookies.get(uid)

    if not cookies:
        await update.message.reply_text("⚠️ Not logged in. Use /login first, or /demo for mock data.")
        return

    await update.message.reply_text("⏳ Fetching attendance from ERP...")

    data, err = fetch_attendance(cookies)

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
    user_cookies.pop(update.effective_user.id, None)
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
