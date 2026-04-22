"""
Hajiri Bot — SGT University Attendance Bot
Reads attendance from attendance_data.json (mock backend).
"""

import os
import json
import logging
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
DATA_FILE = os.path.join(os.path.dirname(__file__), "attendance_data.json")

# ConversationHandler states
ROLLNO, PASSWORD = range(2)

# In-memory session store  { user_id: { "rollno": ..., "name": ... } }
sessions = {}


# ─────────────────────────────────────────────────────────────────────────────
# DATA HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def load_student_data(rollno: str) -> dict:
    """Return subjects list for a rollno. Falls back to 'default' record."""
    with open(DATA_FILE, "r") as f:
        db = json.load(f)
    students = db.get("students", {})
    return students.get(rollno) or students.get("default")


def bunk_calc(present: int, total: int, target: int = 75):
    percent = round(present / total * 100, 1) if total else 0.0
    if percent >= target:
        # max classes you can miss and stay >= target
        can_bunk = int((present * 100 / target) - total)
        msg = f"✅ Safe to bunk *{can_bunk}* more class(es) and stay above {target}%."
    else:
        # classes needed to reach target
        needed = int(((target * total) - (100 * present)) / (100 - target)) + 1
        msg = f"⚠️ Attend *{needed}* more class(es) to reach {target}%."
    return percent, msg


def format_attendance(data: dict, rollno: str) -> str:
    name = data.get("name", rollno)
    subjects = data.get("subjects", [])

    total_present = sum(s["present"] for s in subjects)
    total_classes = sum(s["total"] for s in subjects)
    overall_pct = round(total_present / total_classes * 100, 1) if total_classes else 0.0
    overall_emoji = "✅" if overall_pct >= 75 else "⚠️"

    lines = [
        f"📋 *Attendance — {name}*",
        f"Roll No: `{rollno}`",
        "",
        f"{overall_emoji} *Overall: {overall_pct}% ({total_present}/{total_classes})*",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    for s in subjects:
        pct, bunk_msg = bunk_calc(s["present"], s["total"])
        emoji = "✅" if pct >= 75 else "⚠️"
        lines.append(f"{emoji} *{s['subject']}*")
        lines.append(f"   `{s['present']}/{s['total']}` — `{pct}%`")
        lines.append(f"   {bunk_msg}")
        lines.append("")

    return "\n".join(lines).strip()


# ─────────────────────────────────────────────────────────────────────────────
# CONVERSATION — LOGIN
# ─────────────────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in sessions:
        rollno = sessions[uid]["rollno"]
        await update.message.reply_text(
            f"👋 Welcome back, `{rollno}`!\n\nUse /attendance to view your report or /logout to switch accounts.",
            parse_mode="Markdown"
        )
        return

    await update.message.reply_text(
        "👋 *Welcome to Hajiri Bot!*\n\n"
        "Track your SGT University attendance right here.\n\n"
        "Use /login to get started.",
        parse_mode="Markdown"
    )


async def login_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔐 *Login*\n\nEnter your *Roll Number:*",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    return ROLLNO


async def login_rollno(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["rollno"] = update.message.text.strip()
    await update.message.reply_text("🔑 Enter your *Password:*", parse_mode="Markdown")
    return PASSWORD


async def login_password(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Password is accepted but not validated
    rollno = ctx.user_data.get("rollno", "unknown")

    # Fetch data to resolve the display name
    data = load_student_data(rollno)
    name = data.get("name", rollno) if data else rollno

    sessions[update.effective_user.id] = {"rollno": rollno, "name": name}
    ctx.user_data.clear()

    await update.message.reply_text(
        f"✅ *Logged in!*\n\nHello, *{name}* (`{rollno}`)\n\nUse /attendance to fetch your report.",
        parse_mode="Markdown"
    )
    return ConversationHandler.END


async def login_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Login cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# ATTENDANCE
# ─────────────────────────────────────────────────────────────────────────────

async def attendance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    session = sessions.get(uid)

    if not session:
        await update.message.reply_text(
            "⚠️ You're not logged in. Use /login first."
        )
        return

    rollno = session["rollno"]
    data = load_student_data(rollno)

    if not data:
        await update.message.reply_text("❌ No attendance data found for your roll number.")
        return

    await update.message.reply_text(format_attendance(data, rollno), parse_mode="Markdown")


# ─────────────────────────────────────────────────────────────────────────────
# BUNK (standalone command)
# ─────────────────────────────────────────────────────────────────────────────

async def bunk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text(
            "📐 *Bunk Calculator*\n\nUsage: `/bunk <present> <total> [target%]`\nExample: `/bunk 35 45` or `/bunk 35 45 80`",
            parse_mode="Markdown"
        )
        return

    try:
        present = int(args[0])
        total   = int(args[1])
        target  = int(args[2]) if len(args) > 2 else 75
        pct, msg = bunk_calc(present, total, target)
        await update.message.reply_text(
            f"📐 *Bunk Calculator*\n\n`{present}/{total}` → `{pct}%`\n\n{msg}",
            parse_mode="Markdown"
        )
    except ValueError:
        await update.message.reply_text("❌ Numbers only. Example: `/bunk 35 45`", parse_mode="Markdown")


# ─────────────────────────────────────────────────────────────────────────────
# LOGOUT
# ─────────────────────────────────────────────────────────────────────────────

async def logout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sessions.pop(update.effective_user.id, None)
    ctx.user_data.clear()
    await update.message.reply_text("✅ Logged out. Use /login to sign back in.")


# ─────────────────────────────────────────────────────────────────────────────
# HELP
# ─────────────────────────────────────────────────────────────────────────────

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Commands*\n\n"
        "• /start — Welcome\n"
        "• /login — Login with roll no & password\n"
        "• /attendance — View your full attendance report\n"
        "• /bunk `<present> <total>` — Bunk calculator\n"
        "• /logout — Clear session\n"
        "• /help — This message",
        parse_mode="Markdown"
    )


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    login_conv = ConversationHandler(
        entry_points=[CommandHandler("login", login_start)],
        states={
            ROLLNO:   [MessageHandler(filters.TEXT & ~filters.COMMAND, login_rollno)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_password)],
        },
        fallbacks=[CommandHandler("cancel", login_cancel)],
    )

    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("help",       help_cmd))
    app.add_handler(CommandHandler("attendance", attendance))
    app.add_handler(CommandHandler("bunk",       bunk))
    app.add_handler(CommandHandler("logout",     logout))
    app.add_handler(login_conv)

    log.info("Hajiri Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
