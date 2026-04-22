"""
Hajiri Bot — SGT University Attendance Bot
Reads attendance from attendance_data.json (mock backend).
Announcements pulled from college Telegram channel (placeholder).
"""

import os
import json
import logging
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATA_FILE = os.path.join(os.path.dirname(__file__), "attendance_data.json")

# TODO: Replace with your college Telegram channel username e.g. "@sgtu_official"
COLLEGE_CHANNEL = "@your_college_channel"

ROLLNO, PASSWORD = range(2)
sessions = {}  # { user_id: { "rollno": str, "name": str, "course": str } }


# ─────────────────────────────────────────────────────────────────────────────
# DATA
# ─────────────────────────────────────────────────────────────────────────────

def load_student_data(rollno: str):
    with open(DATA_FILE, "r") as f:
        db = json.load(f)
    students = db.get("students", {})
    return students.get(rollno) or students.get("default")


def bunk_calc(present: int, total: int, target: int = 75):
    percent = round(present / total * 100, 2) if total else 0.0
    if percent >= target:
        can_bunk = int((present * 100 / target) - total)
        msg = f"You can leave next {can_bunk} classes"
    else:
        needed = int(((target * total) - (100 * present)) / (100 - target)) + 1
        msg = f"You need to attend next {needed} classes"
    return percent, msg


def format_attendance(data: dict, rollno: str, session: dict = None) -> str:
    subjects = data.get("subjects", [])
    total_present = sum(s["present"] for s in subjects)
    total_classes = sum(s["total"] for s in subjects)
    overall_pct   = round(total_present / total_classes * 100, 2) if total_classes else 0.0

    name   = (session or {}).get("name",   data.get("name",   rollno))
    course = (session or {}).get("course", data.get("course", ""))

    lines = [
        "HAJIRI",
        "",
        f"👤 *{name}*",
        f"📚 {course}",
        f"🎫 Roll No: `{rollno}`",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "*Attendance Summary:*",
    ]
    for s in subjects:
        pct, bunk_msg = bunk_calc(s["present"], s["total"])
        lines.append(f"\n*{s['subject']}*")
        lines.append(f"*Total:* {pct:.2f}% ({s['present']}/{s['total']})")
        lines.append(bunk_msg)

    lines.append("\n━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"*Overall:* {overall_pct:.2f}% ({total_present}/{total_classes})")
    lines.append("\n_Made By Nitin Kumar_")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# ANNOUNCEMENTS — Mock data (replace with live channel fetch when ready)
# ─────────────────────────────────────────────────────────────────────────────

MOCK_ANNOUNCEMENTS = [
    {
        "id": 1,
        "date": "22 Apr 2026",
        "tag": "🚨 URGENT",
        "title": "Fee Payment Notice",
        "body": (
            "All students of *3rd, 5th & 7th Semester* must clear their pending fees "
            "immediately to avoid de-registration. Visit the accounts office or pay "
            "via the ERP portal before the deadline. Non-payment will result in "
            "restricted access to exams and attendance portal."
        ),
    },
    {
        "id": 2,
        "date": "20 Apr 2026",
        "tag": "📋 EXAM",
        "title": "Regular Exam Form Submission",
        "body": (
            "All students are required to submit the *Regular Exam Form* as per the "
            "attached notification *without delay.*\n\n"
            "📌 *Submit through:* ERP Portal _(Mastersoft)_\n"
            "Ensure all details are filled correctly. Late submissions will not be accepted."
        ),
    },
    {
        "id": 3,
        "date": "10 Apr 2026",
        "tag": "🎬 EVENT",
        "title": "Special Event — Bhoot Bangla Film Promotion",
        "body": (
            "A special event for the film promotion by the star cast of *Bhoot Bangla* "
            "is scheduled for *10 April 2026* at the university premises.\n\n"
            "📌 *Guidelines:*\n"
            "• Students must carry and display their valid *University ID card*\n"
            "• Students must be in proper *prescribed uniform*\n"
            "• No student will be permitted to enter without a valid ID card and appropriate uniform"
        ),
    },
]


def get_announcements() -> str:
    """
    Returns formatted announcements string for Telegram.
    Mock data — replace with live Telegram channel fetch when COLLEGE_CHANNEL is configured.
    """
    lines = [
        "📢 *Announcements*",
        f"_(SGT University — {datetime.now().strftime('%d %b %Y')})_",
        "",
    ]

    for ann in MOCK_ANNOUNCEMENTS:
        lines.append(f"{ann['tag']} | {ann['date']}")
        lines.append(f"*{ann['title']}*")
        lines.append(ann["body"])
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("")

    lines.append("_Source: SGT University Official_")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# MENUS
# ─────────────────────────────────────────────────────────────────────────────

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋  Attendance",    callback_data="attendance")],
        [InlineKeyboardButton("📢  Announcements", callback_data="announcements")],
        [InlineKeyboardButton("🚪  Logout",        callback_data="logout")],
    ])

def back_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠  Back to Menu", callback_data="menu")],
    ])


# ─────────────────────────────────────────────────────────────────────────────
# LOGIN FLOW
# ─────────────────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in sessions:
        name = sessions[uid]["name"]
        await update.message.reply_text(
            f"👋 Welcome back, *{name}*!\n\nWhat do you need?",
            parse_mode="Markdown",
            reply_markup=main_menu()
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
    rollno  = ctx.user_data.get("rollno", "unknown")
    student = load_student_data(rollno)
    name    = student.get("name",   rollno)  if student else rollno
    course  = student.get("course", "")      if student else ""

    sessions[update.effective_user.id] = {"rollno": rollno, "name": name, "course": course}
    ctx.user_data.clear()

    await update.message.reply_text(
        f"✅ *Welcome, {name}!*\n\n"
        f"📚 {course}\n"
        f"🎫 Roll No: `{rollno}`\n\n"
        f"What do you need?",
        parse_mode="Markdown",
        reply_markup=main_menu()
    )
    return ConversationHandler.END


async def login_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Login cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# INLINE BUTTON CALLBACKS
# ─────────────────────────────────────────────────────────────────────────────

async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    data = query.data

    if data == "menu":
        session = sessions.get(uid)
        if not session:
            await query.message.reply_text("⚠️ Not logged in. Use /login first.")
            return
        await query.message.reply_text("What do you need?", reply_markup=main_menu())

    elif data == "attendance":
        session = sessions.get(uid)
        if not session:
            await query.message.reply_text("⚠️ Not logged in. Use /login first.")
            return
        student = load_student_data(session["rollno"])
        if not student:
            await query.message.reply_text("❌ No attendance data found for your roll number.")
            return
        await query.message.reply_text(
            format_attendance(student, session["rollno"], session),
            parse_mode="Markdown",
            reply_markup=back_menu()
        )

    elif data == "announcements":
        await query.message.reply_text(
            get_announcements(),
            parse_mode="Markdown",
            reply_markup=back_menu()
        )

    elif data == "logout":
        sessions.pop(uid, None)
        ctx.user_data.clear()
        await query.message.reply_text(
            "✅ Logged out. Use /login to sign back in.",
            reply_markup=ReplyKeyboardRemove()
        )


# ─────────────────────────────────────────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

async def attendance_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid     = update.effective_user.id
    session = sessions.get(uid)
    if not session:
        await update.message.reply_text("⚠️ Not logged in. Use /login first.")
        return
    student = load_student_data(session["rollno"])
    if not student:
        await update.message.reply_text("❌ No attendance data found.")
        return
    await update.message.reply_text(
        format_attendance(student, session["rollno"], session),
        parse_mode="Markdown",
        reply_markup=back_menu()
    )


async def bunk_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text(
            "📐 *Bunk Calculator*\n\nUsage: `/bunk <present> <total> [target%]`\nExample: `/bunk 35 45`",
            parse_mode="Markdown"
        )
        return
    try:
        present = int(args[0])
        total   = int(args[1])
        target  = int(args[2]) if len(args) > 2 else 75
        pct, msg = bunk_calc(present, total, target)
        await update.message.reply_text(
            f"📐 *Bunk Calculator*\n\n`{present}/{total}` → `{pct:.2f}%`\n\n{msg}",
            parse_mode="Markdown"
        )
    except ValueError:
        await update.message.reply_text("❌ Numbers only. Example: `/bunk 35 45`", parse_mode="Markdown")


async def logout_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sessions.pop(update.effective_user.id, None)
    ctx.user_data.clear()
    await update.message.reply_text("✅ Logged out. Use /login to sign back in.")


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Commands*\n\n"
        "• /start — Welcome / main menu\n"
        "• /login — Login with roll no & password\n"
        "• /attendance — View your attendance\n"
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
    app.add_handler(CommandHandler("attendance", attendance_cmd))
    app.add_handler(CommandHandler("bunk",       bunk_cmd))
    app.add_handler(CommandHandler("logout",     logout_cmd))
    app.add_handler(login_conv)
    app.add_handler(CallbackQueryHandler(button_handler))

    log.info("Hajiri Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
