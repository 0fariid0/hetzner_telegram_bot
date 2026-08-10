#!/usr/bin/env python3
import logging
import os
from html import escape

from dotenv import load_dotenv
from hcloud import Client
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
HETZNER_TOKEN = os.getenv("HETZNER_API_TOKEN", "").strip()
ALLOWED_CHAT_ID = os.getenv("ALLOWED_CHAT_ID", "").strip()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("hetzner-bot")

client = Client(token=HETZNER_TOKEN) if HETZNER_TOKEN else None

STATUS_FA = {
    "running": "روشن",
    "off": "خاموش",
    "starting": "در حال روشن شدن",
    "stopping": "در حال خاموش شدن",
    "rebuilding": "در حال بازسازی",
    "migrating": "در حال انتقال",
    "unknown": "نامشخص",
}


def authorized(update: Update) -> bool:
    chat = update.effective_chat
    if not chat or not ALLOWED_CHAT_ID or ALLOWED_CHAT_ID == "0":
        return False
    return str(chat.id) == ALLOWED_CHAT_ID


def status_icon(status: str) -> str:
    if status == "running":
        return "🟢"
    if status == "off":
        return "🔴"
    return "🟡"


def server_ipv4(server) -> str | None:
    ipv4 = getattr(getattr(server, "public_net", None), "ipv4", None)
    return getattr(ipv4, "ip", None) if ipv4 else None


def server_location(server) -> str:
    try:
        return server.datacenter.location.name
    except Exception:
        return "نامشخص"


def server_text(server) -> str:
    ipv4 = server_ipv4(server) or "ندارد"
    status = getattr(server, "status", "unknown")
    status_fa = STATUS_FA.get(status, status)
    server_type = getattr(getattr(server, "server_type", None), "name", "نامشخص")

    return (
        f"🖥 <b>{escape(server.name)}</b>\n\n"
        f"وضعیت: {status_icon(status)} {escape(status_fa)}\n"
        f"IPv4: <code>{escape(str(ipv4))}</code>\n"
        f"نوع سرور: <code>{escape(str(server_type))}</code>\n"
        f"موقعیت: <code>{escape(server_location(server))}</code>"
    )


def server_keyboard(server) -> InlineKeyboardMarkup:
    ipv4 = server_ipv4(server)
    ip_button = (
        InlineKeyboardButton("🗑 حذف IPv4", callback_data=f"askdelip_{server.id}")
        if ipv4
        else InlineKeyboardButton("➕ اتصال IPv4", callback_data=f"assignip_{server.id}")
    )

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🟢 روشن", callback_data=f"on_{server.id}"),
                InlineKeyboardButton("🔴 خاموش", callback_data=f"off_{server.id}"),
            ],
            [ip_button],
            [InlineKeyboardButton("🔄 بروزرسانی", callback_data=f"refresh_{server.id}")],
            [InlineKeyboardButton("⬅️ لیست سرورها", callback_data="main_servers")],
        ]
    )


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🖥 مدیریت سرورها", callback_data="main_servers")],
            [InlineKeyboardButton("ℹ️ راهنما", callback_data="main_help")],
        ]
    )


async def deny(update: Update) -> None:
    text = (
        "⛔️ دسترسی شما به بخش مدیریت مجاز نیست.\n"
        "برای دیدن شناسه این گفتگو دستور /id را ارسال کنید."
    )
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(text)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.effective_message:
        return
    await update.effective_message.reply_text(
        f"شناسه این گفتگو:\n<code>{update.effective_chat.id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await deny(update)
        return
    await update.effective_message.reply_text(
        "سلام 👋\n"
        "به ربات مدیریت Hetzner خوش آمدید.\n"
        "از منوی زیر می‌توانید سرورها و IPv4های حساب خود را مدیریت کنید.",
        reply_markup=main_keyboard(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await deny(update)
        return
    await update.effective_message.reply_text(
        "ℹ️ <b>راهنما</b>\n\n"
        "/start — منوی اصلی\n"
        "/servers — نمایش و مدیریت سرورها\n"
        "/id — نمایش شناسه گفتگو\n\n"
        "برای اتصال یا حذف Primary IPv4، سرور باید خاموش باشد.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )


async def send_servers(message) -> None:
    if not client:
        await message.reply_text("❌ ارتباط با Hetzner تنظیم نشده است. توکن API را بررسی کنید.")
        return

    try:
        servers = client.servers.get_all()
        primary_ips = client.primary_ips.get_all()
        free_ipv4 = sum(
            1
            for ip in primary_ips
            if getattr(ip, "type", None) == "ipv4" and getattr(ip, "assignee_id", None) is None
        )
    except Exception as exc:
        log.exception("Failed to load Hetzner resources")
        await message.reply_text(f"❌ دریافت اطلاعات از Hetzner ناموفق بود:\n<code>{escape(str(exc))}</code>", parse_mode=ParseMode.HTML)
        return

    if not servers:
        await message.reply_text("هیچ سروری در پروژه Hetzner پیدا نشد.", reply_markup=main_keyboard())
        return

    await message.reply_text(
        f"📊 تعداد سرورها: <b>{len(servers)}</b>\n"
        f"🌐 IPv4 آزاد: <b>{free_ipv4}</b>",
        parse_mode=ParseMode.HTML,
    )

    for server in servers:
        await message.reply_text(
            server_text(server),
            parse_mode=ParseMode.HTML,
            reply_markup=server_keyboard(server),
        )


async def cmd_servers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await deny(update)
        return
    await send_servers(update.effective_message)


async def refresh_server_message(query, server_id: int) -> None:
    server = client.servers.get_by_id(server_id)
    if not server:
        await query.edit_message_text("❌ سرور پیدا نشد.")
        return
    await query.edit_message_text(
        server_text(server),
        parse_mode=ParseMode.HTML,
        reply_markup=server_keyboard(server),
    )


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    if not authorized(update):
        await deny(update)
        return

    if not client:
        await query.answer("ارتباط با Hetzner تنظیم نشده است.", show_alert=True)
        return

    data = query.data or ""

    if data == "main_servers":
        await query.answer()
        await query.message.reply_text("در حال دریافت لیست سرورها...")
        await send_servers(query.message)
        return

    if data == "main_help":
        await query.answer()
        await query.edit_message_text(
            "ℹ️ <b>راهنما</b>\n\n"
            "• مشاهده مشخصات و وضعیت سرورها\n"
            "• روشن و خاموش کردن سرور\n"
            "• اتصال IPv4 آزاد به سرور\n"
            "• جدا کردن و حذف IPv4 با تأیید نهایی\n\n"
            "⚠️ اتصال/جداکردن Primary IPv4 فقط زمانی انجام می‌شود که سرور خاموش باشد.",
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(),
        )
        return

    try:
        action, raw_server_id = data.split("_", 1)
        server_id = int(raw_server_id)
    except (ValueError, TypeError):
        await query.answer("درخواست نامعتبر است.", show_alert=True)
        return

    try:
        server = client.servers.get_by_id(server_id)
        if not server:
            await query.answer("سرور پیدا نشد.", show_alert=True)
            return

        if action == "refresh":
            await query.answer("بروزرسانی شد")
            await refresh_server_message(query, server_id)
            return

        if action == "on":
            if server.status == "running":
                await query.answer("سرور از قبل روشن است.", show_alert=True)
                return
            server.power_on()
            await query.answer("دستور روشن شدن ارسال شد.", show_alert=True)
            await refresh_server_message(query, server_id)
            return

        if action == "off":
            if server.status == "off":
                await query.answer("سرور از قبل خاموش است.", show_alert=True)
                return
            server.shutdown()
            await query.answer("دستور خاموش شدن امن ارسال شد.", show_alert=True)
            await refresh_server_message(query, server_id)
            return

        if action == "assignip":
            if server.status != "off":
                await query.answer("برای اتصال Primary IPv4 ابتدا سرور را خاموش کنید.", show_alert=True)
                return

            free_ip = next(
                (
                    ip
                    for ip in client.primary_ips.get_all()
                    if getattr(ip, "type", None) == "ipv4" and getattr(ip, "assignee_id", None) is None
                ),
                None,
            )
            if not free_ip:
                await query.answer("هیچ IPv4 آزادی در پروژه وجود ندارد.", show_alert=True)
                return

            result = free_ip.assign(assignee_id=server.id, assignee_type="server")
            result.wait_until_finished()
            await query.answer(f"IPv4 {free_ip.ip} متصل شد.", show_alert=True)
            await refresh_server_message(query, server_id)
            return

        if action == "askdelip":
            ipv4 = server_ipv4(server)
            if not ipv4:
                await query.answer("این سرور IPv4 ندارد.", show_alert=True)
                return

            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("✅ بله، حذف شود", callback_data=f"delip_{server.id}")],
                    [InlineKeyboardButton("❌ انصراف", callback_data=f"refresh_{server.id}")],
                ]
            )
            await query.answer()
            await query.edit_message_text(
                f"⚠️ <b>حذف IPv4</b>\n\n"
                f"آدرس <code>{escape(ipv4)}</code> از سرور جدا و سپس از پروژه Hetzner حذف می‌شود.\n"
                "این کار ممکن است باعث از دسترس خارج شدن سرویس‌های وابسته به این IP شود.\n\n"
                "آیا مطمئن هستید؟",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            return

        if action == "delip":
            if server.status != "off":
                await query.answer("برای حذف Primary IPv4 ابتدا سرور را خاموش کنید.", show_alert=True)
                return

            target = next(
                (
                    ip
                    for ip in client.primary_ips.get_all()
                    if getattr(ip, "type", None) == "ipv4" and getattr(ip, "assignee_id", None) == server.id
                ),
                None,
            )
            if not target:
                await query.answer("IPv4 متصل به این سرور پیدا نشد.", show_alert=True)
                await refresh_server_message(query, server_id)
                return

            if target.assignee_id is not None:
                unassign_action = target.unassign()
                unassign_action.wait_until_finished()
            target.delete()

            await query.answer(f"IPv4 {target.ip} حذف شد.", show_alert=True)
            await refresh_server_message(query, server_id)
            return

        await query.answer("عملیات ناشناخته است.", show_alert=True)

    except Exception as exc:
        log.exception("Callback failed: %s", data)
        text = str(exc)
        if len(text) > 180:
            text = text[:177] + "..."
        await query.answer(f"خطا: {text}", show_alert=True)


def validate_config() -> None:
    missing = []
    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not HETZNER_TOKEN:
        missing.append("HETZNER_API_TOKEN")
    if missing:
        raise SystemExit("Missing environment variables: " + ", ".join(missing))


if __name__ == "__main__":
    validate_config()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("servers", cmd_servers))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CallbackQueryHandler(callbacks))

    print("Hetzner Telegram Bot is running...")
    app.run_polling(drop_pending_updates=True)
