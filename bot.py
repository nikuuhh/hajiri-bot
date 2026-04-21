import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)

# ── MOCK DATA ──────────────────────────────────────────────────
MOCK_USERS = {
    "2310992856": {
        "password": "Sgt@2024",
        "name": "Rahul Sharma",
        "roll": "SGT/CSE/2023/0856",
        "branch": "B.Tech CSE (AI & ML)",
        "semester": "IV",
        "subjects": [
            {"code": "CSE-401", "name": "Data Structures & Algorithms", "present": 38, "total": 44},
            {"code": "CSE-403", "name": "Operating Systems",             "present": 29, "total": 42},
            {"code": "CSE-405", "name": "Database Management Systems",   "present": 40, "total": 44},
            {"code": "CSE-407", "name": "Computer Networks",             "present": 31, "total": 44},
            {"code": "CSE-409", "name": "Theory of Computation",         "present": 22, "total": 40},
            {"code": "CSE-411", "name": "Artificial Intelligence",       "present": 36, "total": 38},
            {"code": "HUM-401", "name": "Technical Communication",       "present": 18, "total": 24},
            {"code": "CSE-413", "name": "Software Engineering Lab",      "present": 16, "total": 20},
        ]
    },
    "2310992999": {
        "password": "Sgt@1234",
        "name": "Priya Verma",
        "roll": "SGT/CSE/2023/0999",
        "branch": "B.Tech CSE",
        "semester": "IV",
        "subjects": [
            {"code": "CSE-401", "name": "Data Structures & Algorithms", "present": 42, "total": 44},
            {"code": "CSE-403", "name": "Operating Systems",             "present": 38, "total": 42},
            {"code": "CSE-405", "name": "Database Management Systems",   "present": 40, "total": 44},
            {"code": "CSE-407", "name": "Computer Networks",             "present": 36, "total": 44},
            {"code": "CSE-409", "name": "Theory of Computation",         "present": 35, "total": 40},
            {"code": "CSE-411", "name": "Artificial Intelligence",       "present": 37, "total": 38},
            {"code": "HUM-401", "name": "Technical Communication",       "present": 22, "total": 24},
            {"code": "CSE-413", "name": "Software Engineering Lab",      "present": 19, "total": 20},
        ]
    }
}

# ── HELPERS ────────────────────────────────────────────────────
def pct(present, total):
    return round(present / total * 100)

def status_emoji(p):
    if p >= 85: return "🟢"
    if p >= 75: return "🟡"
    return "🔴"

def bunk_info(present, total):
    if present / total >= 0.75:
        can = int((present - 0.75 * total) / 0.75)
        return f"can bunk *{can}* more ✅" if can > 0 else "at limit ⚠️"
    else:
        need = -int(-(0.75 * total - present) / 0.25)
        return f"attend *{need}* consecutive ❗"

def format_attendance(user):
    subjs = user["subjects"]
    tp = sum(s["present"] for s in subjs)
    tt = sum(s["total"]   for s in subjs)
    op = pct(tp, tt)

    lines = [
        "📋 *Hajiri — SGT University*",
        f"👤 {user['name']}  |  `{user['roll']}`",
        f"📚 {user['branch']} · Sem {user['semester']}",
        "",
        f"{status_emoji(op)} *Overall: {op}%*  ({tp}/{tt} classes)",
        "─────────────────────────",
    ]
    for s in subjs:
        p = pct(s["present"], s["total"])
        lines.append(f"{status_emoji(p)} *{s['name']}*")
        lines.append(f"   `{p}%` · {s['present']}/{s['total']} · {bunk_info(s['present'], s['total'])}")
    lines += [
        "─────────────────────────",
        "🟢 ≥85%   🟡 75–84%   🔴 <75%",
        "",
        "Commands: /bunk · /refresh · /logout",
    ]
    return "\n".join(lines)

def format_bunk(user):
    lines = [
        "🎯 *Bunk Calculator* (75% threshold)",
        "─────────────────────────",
    ]
    for s in user["subjects"]:
        p = pct(s["present"], s["total"])
        if s["present"] / s["total"] >= 0.75:
            can = int((s["present"] - 0.75 * s["total"]) / 0.75)
            detail = f"bunk *{can}* more ✅" if can > 0 else "at limit ⚠️"
        else:
            need = -int(-(0.75 * s["total"] - s["present"]) / 0.25)
            detail = f"need *{need}* consecutive ❗"
        lines.append(f"{status_emoji(p)} {s['name'][:30]}")
        lines.append(f"   `{p}%` → {detail}")
    lines += ["─────────────────────────", "/attendance to go back"]
    return "\n".join(lines)

# ── SESSION HELPERS ────────────────────────────────────────────
def get_session(context): return context.user_data
def logged_in_user(context):
    u = context.user_data.get("username")
    return MOCK_USERS.get(u) if u else None

# ── HANDLERS ──────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["stage"] = "await_username"
    await update.message.reply_text(
        "👋 *Welcome to Hajiri\\!*\n"
        "Attendance tracker for SGT University\\.\n\n"
        "Please enter your *SGT ERP Username*\n"
        "_\\(your enrollment number, e\\.g\\. 2310992856\\)_",
        parse_mode="MarkdownV2"
    )

async def cmd_attendance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = logged_in_user(context)
    if not user:
        await update.message.reply_text("Please /start and log in first.")
        return
    await update.message.reply_text(format_attendance(user), parse_mode="Markdown")

async def cmd_bunk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = logged_in_user(context)
    if not user:
        await update.message.reply_text("Please /start and log in first.")
        return
    await update.message.reply_text(format_bunk(user), parse_mode="Markdown")

async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = logged_in_user(context)
    if not user:
        await update.message.reply_text("Please /start and log in first.")
        return
    await update.message.reply_text("🔄 Fetching latest data from SGT ERP\\.\\.\\.", parse_mode="MarkdownV2")
    await update.message.reply_text(format_attendance(user), parse_mode="Markdown")

async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["stage"] = "await_username"
    await update.message.reply_text(
        "👋 *Logged out successfully\\.*\n\nSay /start to log in again\\.",
        parse_mode="MarkdownV2"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text  = update.message.text.strip()
    stage = context.user_data.get("stage", "await_username")

    # shortcut commands as plain text
    tl = text.lower()
    if stage == "logged_in":
        if tl in ["attendance", "att", "show", "refresh", "check"]:
            await cmd_attendance(update, context); return
        if tl in ["bunk", "bunk calculator", "calc"]:
            await cmd_bunk(update, context); return
        if tl in ["logout", "log out", "signout"]:
            await cmd_logout(update, context); return
        await update.message.reply_text(
            "Commands:\n/attendance — view attendance\n/bunk — bunk calculator\n/refresh — re-fetch\n/logout — sign out"
        )
        return

    # ── AWAIT USERNAME ────────────────────────────────────────
    if stage == "await_username":
        context.user_data["username_input"] = text
        context.user_data["stage"] = "await_password"
        await update.message.reply_text(
            f"Got it\\! Username: `{text}`\n\n"
            "Now enter your *SGT ERP Password*\n"
            "_Same password as erp\\.sgtu\\.in_\n\n"
            "🔒 Used only to fetch your attendance\\.",
            parse_mode="MarkdownV2"
        )
        return

    # ── AWAIT PASSWORD ────────────────────────────────────────
    if stage == "await_password":
        username = context.user_data.get("username_input", "")
        password = text

        if username in MOCK_USERS and MOCK_USERS[username]["password"] == password:
            context.user_data["username"] = username
            context.user_data["stage"]    = "logged_in"
            user = MOCK_USERS[username]
            await update.message.reply_text(
                f"⏳ Logging into SGT ERP and fetching your attendance\\.\\.\\.",
                parse_mode="MarkdownV2"
            )
            await update.message.reply_text(format_attendance(user), parse_mode="Markdown")
        else:
            context.user_data["stage"] = "await_username"
            await update.message.reply_text(
                "❌ *Invalid credentials\\.* Please try again\\.\n\n"
                "Enter your *SGT ERP Username*:",
                parse_mode="MarkdownV2"
            )
        return

# ── MAIN ──────────────────────────────────────────────────────
def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise ValueError("BOT_TOKEN environment variable not set!")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("attendance", cmd_attendance))
    app.add_handler(CommandHandler("bunk",       cmd_bunk))
    app.add_handler(CommandHandler("refresh",    cmd_refresh))
    app.add_handler(CommandHandler("logout",     cmd_logout))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🚀 Hajiri bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
