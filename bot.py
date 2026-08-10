#!/usr/bin/env python3
import asyncio
import base64
import json
import logging
import os
from datetime import time
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from hcloud import Client
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID", os.getenv("ALLOWED_CHAT_ID", "")).strip()
LEGACY_HETZNER_TOKEN = os.getenv("HETZNER_API_TOKEN", "").strip()
LEGACY_PROJECT_NAME = os.getenv("HETZNER_PROJECT_NAME", "Main").strip() or "Main"
PROJECTS_B64 = os.getenv("HETZNER_PROJECTS_B64", "").strip()
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Asia/Tehran").strip() or "Asia/Tehran"
TRAFFIC_CHECK_TIME = os.getenv("TRAFFIC_CHECK_TIME", "23:30").strip() or "23:30"
TRAFFIC_ALERT_TB = (18.0, 19.0, 20.0)
STATE_FILE = Path(os.getenv("STATE_FILE", "/opt/hetzner-telegram-bot/.traffic_alert_state.json"))
TB = 1_000_000_000_000

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("hetzner-bot")

STATUS_FA = {
    "running": "روشن",
    "off": "خاموش",
    "starting": "در حال روشن شدن",
    "stopping": "در حال خاموش شدن",
    "initializing": "در حال آماده‌سازی",
    "rebuilding": "در حال بازسازی",
    "migrating": "در حال انتقال",
    "deleting": "در حال حذف",
    "unknown": "نامشخص",
}


def load_projects() -> list[dict]:
    projects = []
    if PROJECTS_B64:
        try:
            raw = base64.b64decode(PROJECTS_B64.encode()).decode("utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                data = [{"name": name, "token": token} for name, token in data.items()]
            for item in data:
                name = str(item.get("name", "")).strip()
                token = str(item.get("token", "")).strip()
                if name and token:
                    projects.append({"name": name, "client": Client(token=token)})
        except Exception as exc:
            raise SystemExit(f"Invalid HETZNER_PROJECTS_B64: {exc}") from exc

    if not projects and LEGACY_HETZNER_TOKEN:
        projects.append({"name": LEGACY_PROJECT_NAME, "client": Client(token=LEGACY_HETZNER_TOKEN)})
    return projects


PROJECTS = load_projects()


def authorized(update: Update) -> bool:
    user = update.effective_user
    return bool(user and ALLOWED_USER_ID and str(user.id) == ALLOWED_USER_ID)


def get_project(index: int) -> dict | None:
    return PROJECTS[index] if 0 <= index < len(PROJECTS) else None


def project_keyboard(target: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"📁 {p['name']}", callback_data=f"prj:{target}:{i}")]
        for i, p in enumerate(PROJECTS)
    ]
    rows.append([InlineKeyboardButton("⬅️ منوی اصلی", callback_data="main")])
    return InlineKeyboardMarkup(rows)


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🖥 مدیریت سرورها", callback_data="main:servers")],
            [InlineKeyboardButton("🌐 مدیریت Floating IP", callback_data="main:floating")],
            [InlineKeyboardButton("📊 ترافیک ماه جاری", callback_data="main:traffic")],
            [InlineKeyboardButton("ℹ️ راهنما", callback_data="main:help")],
        ]
    )


def status_icon(status: str) -> str:
    if status == "running":
        return "🟢"
    if status == "off":
        return "🔴"
    return "🟡"


def server_ipv4(server) -> str | None:
    ipv4 = getattr(getattr(server, "public_net", None), "ipv4", None)
    return getattr(ipv4, "ip", None) if ipv4 else None


def server_ipv6(server) -> str | None:
    ipv6 = getattr(getattr(server, "public_net", None), "ipv6", None)
    return getattr(ipv6, "ip", None) if ipv6 else None


def server_location(server) -> str:
    location = getattr(server, "location", None)
    return getattr(location, "name", None) or "نامشخص"


def traffic_tb(server) -> float:
    return float(getattr(server, "outgoing_traffic", 0) or 0) / TB


def included_tb(server) -> float | None:
    raw = getattr(server, "included_traffic", None)
    return float(raw) / TB if raw else None


def traffic_line(server) -> str:
    used = traffic_tb(server)
    included = included_tb(server)
    if included:
        return f"ترافیک خروجی: <b>{used:.2f} TB</b> از <b>{included:.2f} TB</b>"
    return f"ترافیک خروجی: <b>{used:.2f} TB</b>"


def server_text(server, project_name: str) -> str:
    ipv4 = server_ipv4(server) or "ندارد"
    status = getattr(server, "status", "unknown")
    status_fa = STATUS_FA.get(status, status)
    st = getattr(server, "server_type", None)
    server_type = getattr(st, "name", "نامشخص")
    cores = getattr(st, "cores", "?")
    memory = getattr(st, "memory", "?")
    disk = getattr(st, "disk", "?")
    return (
        f"🖥 <b>{escape(server.name)}</b>\n"
        f"📁 پروژه: <b>{escape(project_name)}</b>\n\n"
        f"وضعیت: {status_icon(status)} {escape(status_fa)}\n"
        f"IPv4: <code>{escape(str(ipv4))}</code>\n"
        f"پلن: <code>{escape(str(server_type))}</code> — {cores} vCPU / {memory} GB RAM / {disk} GB\n"
        f"موقعیت: <code>{escape(server_location(server))}</code>\n"
        f"{traffic_line(server)}"
    )


def server_keyboard(pidx: int, server) -> InlineKeyboardMarkup:
    sid = server.id
    primary_button = (
        InlineKeyboardButton("🗑 حذف Primary IPv4", callback_data=f"srv:askpip:{pidx}:{sid}")
        if server_ipv4(server)
        else InlineKeyboardButton("➕ اتصال Primary IPv4 آزاد", callback_data=f"srv:assignpip:{pidx}:{sid}")
    )
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🟢 روشن", callback_data=f"srv:on:{pidx}:{sid}"),
                InlineKeyboardButton("🔴 خاموش", callback_data=f"srv:off:{pidx}:{sid}"),
            ],
            [InlineKeyboardButton("⚙️ تغییر سایز", callback_data=f"srv:resize:{pidx}:{sid}")],
            [primary_button],
            [InlineKeyboardButton("🌐 Floating IPهای این سرور", callback_data=f"srv:fips:{pidx}:{sid}")],
            [InlineKeyboardButton("🔄 بروزرسانی", callback_data=f"srv:refresh:{pidx}:{sid}")],
            [InlineKeyboardButton("⬅️ لیست سرورها", callback_data=f"prj:servers:{pidx}")],
        ]
    )


def floating_text(fip, project_name: str) -> str:
    server = getattr(fip, "server", None)
    server_name = getattr(server, "name", None) or "آزاد / متصل نیست"
    home = getattr(getattr(fip, "home_location", None), "name", "نامشخص")
    name = getattr(fip, "name", None) or getattr(fip, "description", None) or f"Floating IP #{fip.id}"
    return (
        f"🌐 <b>{escape(str(name))}</b>\n"
        f"📁 پروژه: <b>{escape(project_name)}</b>\n\n"
        f"IP: <code>{escape(str(fip.ip))}</code>\n"
        f"نوع: <code>{escape(str(fip.type))}</code>\n"
        f"Home Location: <code>{escape(str(home))}</code>\n"
        f"سرور: <b>{escape(str(server_name))}</b>"
    )


def floating_commands(fip) -> tuple[str, str]:
    if getattr(fip, "type", "ipv4") == "ipv6":
        prefix = str(fip.ip)
        if "/" not in prefix:
            prefix += "/64"
        return (
            f"sudo ip -6 addr add {prefix} dev eth0",
            f"sudo ip -6 addr del {prefix} dev eth0",
        )
    ip = str(fip.ip).split("/")[0]
    return (
        f"sudo ip addr add {ip}/32 dev eth0",
        f"sudo ip addr del {ip}/32 dev eth0",
    )


def floating_keyboard(pidx: int, fip) -> InlineKeyboardMarkup:
    fid = fip.id
    rows = []
    if getattr(fip, "server", None):
        rows.append(
            [
                InlineKeyboardButton("🔁 انتقال به سرور دیگر", callback_data=f"fip:choose:{pidx}:{fid}"),
                InlineKeyboardButton("➖ جدا کردن", callback_data=f"fip:unassign:{pidx}:{fid}"),
            ]
        )
    else:
        rows.append([InlineKeyboardButton("➕ اتصال به سرور", callback_data=f"fip:choose:{pidx}:{fid}")])
    rows.extend(
        [
            [InlineKeyboardButton("📋 دستور تنظیم روی سرور", callback_data=f"fip:cmd:{pidx}:{fid}")],
            [InlineKeyboardButton("🗑 حذف Floating IP", callback_data=f"fip:askdel:{pidx}:{fid}")],
            [InlineKeyboardButton("⬅️ Floating IPها", callback_data=f"prj:floating:{pidx}")],
        ]
    )
    return InlineKeyboardMarkup(rows)


async def deny(update: Update) -> None:
    # Private bot: unauthorized users are silently ignored.
    # Callback queries cannot normally be forged without a previous bot message,
    # but answering them avoids a stuck Telegram loading indicator.
    if update.callback_query:
        await update.callback_query.answer()


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await deny(update)
        return
    if not update.effective_user or not update.effective_message:
        return
    await update.effective_message.reply_text(
        f"شناسه عددی مجاز:\n<code>{update.effective_user.id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await deny(update)
        return
    await update.effective_message.reply_text(
        "سلام 👋\n"
        "پنل خصوصی مدیریت Hetzner آماده است.\n"
        "از اینجا می‌توانید سرورها، Floating IP، تغییر سایز و ترافیک را مدیریت کنید.",
        reply_markup=main_keyboard(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await deny(update)
        return
    await update.effective_message.reply_text(
        "ℹ️ <b>راهنما</b>\n\n"
        "• دسترسی فقط برای User ID تعیین‌شده فعال است.\n"
        "• هر شب ترافیک خروجی ماه جاری بررسی می‌شود.\n"
        "• در 18، 19 و 20 ترابایت هشدار روزانه ارسال می‌شود.\n"
        "• Floating IP را می‌توانید بسازید، متصل، جدا، منتقل و حذف کنید.\n"
        "• بعد از اتصال Floating IP دستور تنظیم داخل سیستم‌عامل هم نمایش داده می‌شود.\n"
        "• تغییر سایز فقط برای سرور خاموش و پلن هم‌معماری انجام می‌شود.\n\n"
        "/start — منوی اصلی\n"
        "/servers — سرورها\n"
        "/traffic — گزارش ترافیک\n"
        "/floating — Floating IPها\n"
        "/id — نمایش User ID",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )


async def choose_project(message, target: str, title: str) -> None:
    if len(PROJECTS) == 1:
        if target == "servers":
            await send_servers(message, 0)
        elif target == "floating":
            await send_floating_list(message, 0)
        elif target == "traffic":
            await send_traffic_report(message, 0)
        return
    await message.reply_text(title, reply_markup=project_keyboard(target))


async def send_servers(message, pidx: int) -> None:
    project = get_project(pidx)
    if not project:
        await message.reply_text("❌ پروژه پیدا نشد.")
        return
    try:
        servers = await asyncio.to_thread(project["client"].servers.get_all)
    except Exception as exc:
        log.exception("Failed to load servers")
        await message.reply_text(f"❌ خطای Hetzner:\n<code>{escape(str(exc))}</code>", parse_mode=ParseMode.HTML)
        return

    await message.reply_text(
        f"📁 پروژه: <b>{escape(project['name'])}</b>\n"
        f"🖥 تعداد سرورها: <b>{len(servers)}</b>",
        parse_mode=ParseMode.HTML,
    )
    if not servers:
        return
    for server in servers:
        await message.reply_text(
            server_text(server, project["name"]),
            parse_mode=ParseMode.HTML,
            reply_markup=server_keyboard(pidx, server),
        )


async def send_traffic_report(message, pidx: int) -> None:
    project = get_project(pidx)
    if not project:
        await message.reply_text("❌ پروژه پیدا نشد.")
        return
    try:
        servers = await asyncio.to_thread(project["client"].servers.get_all)
    except Exception as exc:
        await message.reply_text(f"❌ خطا: <code>{escape(str(exc))}</code>", parse_mode=ParseMode.HTML)
        return

    lines = [f"📊 <b>ترافیک ماه جاری — {escape(project['name'])}</b>"]
    for server in servers:
        used = traffic_tb(server)
        included = included_tb(server)
        limit = f" / {included:.2f} TB" if included else ""
        marker = "🚨" if used >= 18 else "✅"
        lines.append(f"{marker} <b>{escape(server.name)}</b>: {used:.2f}{limit}")
    if len(lines) == 1:
        lines.append("هیچ سروری پیدا نشد.")
    await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def send_floating_list(message, pidx: int) -> None:
    project = get_project(pidx)
    if not project:
        await message.reply_text("❌ پروژه پیدا نشد.")
        return
    try:
        fips = await asyncio.to_thread(project["client"].floating_ips.get_all)
    except Exception as exc:
        await message.reply_text(f"❌ خطا: <code>{escape(str(exc))}</code>", parse_mode=ParseMode.HTML)
        return

    await message.reply_text(
        f"🌐 <b>Floating IP — {escape(project['name'])}</b>\n"
        f"تعداد: <b>{len(fips)}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("➕ ساخت Floating IP جدید", callback_data=f"fip:new:{pidx}")],
                [InlineKeyboardButton("⬅️ منوی اصلی", callback_data="main")],
            ]
        ),
    )
    for fip in fips:
        await message.reply_text(
            floating_text(fip, project["name"]),
            parse_mode=ParseMode.HTML,
            reply_markup=floating_keyboard(pidx, fip),
        )


async def cmd_servers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await deny(update)
        return
    await choose_project(update.effective_message, "servers", "📁 پروژه را برای نمایش سرورها انتخاب کنید:")


async def cmd_traffic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await deny(update)
        return
    await choose_project(update.effective_message, "traffic", "📁 پروژه را برای گزارش ترافیک انتخاب کنید:")


async def cmd_floating(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await deny(update)
        return
    await choose_project(update.effective_message, "floating", "📁 پروژه را برای Floating IP انتخاب کنید:")


def available_server_types(client, server) -> list:
    current = getattr(server, "server_type", None)
    current_arch = getattr(current, "architecture", None)
    current_disk = int(getattr(server, "primary_disk_size", 0) or getattr(current, "disk", 0) or 0)
    location_name = server_location(server)
    result = []
    for st in client.server_types.get_all():
        if st.name == getattr(current, "name", None):
            continue
        if current_arch and getattr(st, "architecture", None) != current_arch:
            continue
        if int(getattr(st, "disk", 0) or 0) < current_disk:
            continue
        locations = getattr(st, "locations", None) or []
        if locations:
            matching = []
            for loc_info in locations:
                loc = getattr(loc_info, "location", None)
                if getattr(loc, "name", None) == location_name:
                    matching.append(loc_info)
            if not matching:
                continue
            if any(getattr(x, "deprecation", None) for x in matching):
                continue
        result.append(st)
    return sorted(result, key=lambda x: (getattr(x, "memory", 0), getattr(x, "cores", 0), getattr(x, "name", "")))


async def show_resize_options(query, pidx: int, server_id: int) -> None:
    project = get_project(pidx)
    if not project:
        await query.answer("پروژه پیدا نشد.", show_alert=True)
        return
    client = project["client"]
    server = await asyncio.to_thread(client.servers.get_by_id, server_id)
    if not server:
        await query.answer("سرور پیدا نشد.", show_alert=True)
        return
    if server.status != "off":
        await query.answer("برای تغییر سایز، ابتدا سرور را خاموش کنید.", show_alert=True)
        return

    types = await asyncio.to_thread(available_server_types, client, server)
    if not types:
        await query.answer("پلن سازگار دیگری برای این سرور پیدا نشد.", show_alert=True)
        return

    rows = []
    for st in types[:30]:
        label = f"{st.name} | {st.cores}C / {st.memory}GB / {st.disk}GB"
        rows.append([InlineKeyboardButton(label, callback_data=f"rzt:pick:{pidx}:{server_id}:{st.id}")])
    rows.append([InlineKeyboardButton("⬅️ برگشت", callback_data=f"srv:refresh:{pidx}:{server_id}")])
    await query.edit_message_text(
        f"⚙️ <b>تغییر سایز {escape(server.name)}</b>\n\n"
        f"پلن فعلی: <code>{escape(server.server_type.name)}</code>\n"
        "پلن جدید را انتخاب کنید. فقط پلن‌های هم‌معماری و سازگار نمایش داده شده‌اند.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def show_fip_server_choices(query, pidx: int, fip_id: int) -> None:
    project = get_project(pidx)
    if not project:
        await query.answer("پروژه پیدا نشد.", show_alert=True)
        return
    client = project["client"]
    fip = await asyncio.to_thread(client.floating_ips.get_by_id, fip_id)
    servers = await asyncio.to_thread(client.servers.get_all)
    compatible = []
    for server in servers:
        if fip.type == "ipv4" and not server_ipv4(server):
            continue
        if fip.type == "ipv6" and not server_ipv6(server):
            continue
        compatible.append(server)
    if not compatible:
        await query.answer("هیچ سرور سازگاری با این نوع IP پیدا نشد.", show_alert=True)
        return
    rows = [[InlineKeyboardButton(f"🖥 {s.name}", callback_data=f"fip:assign:{pidx}:{fip_id}:{s.id}")] for s in compatible]
    rows.append([InlineKeyboardButton("⬅️ برگشت", callback_data=f"fip:open:{pidx}:{fip_id}")])
    await query.edit_message_text(
        f"🌐 Floating IP: <code>{escape(str(fip.ip))}</code>\n\nسرور مقصد را انتخاب کنید:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not authorized(update):
        await deny(update)
        return
    data = query.data or ""

    try:
        if data == "main":
            await query.answer()
            await query.edit_message_text("پنل مدیریت Hetzner:", reply_markup=main_keyboard())
            return

        if data.startswith("main:"):
            target = data.split(":", 1)[1]
            await query.answer()
            if target == "help":
                await query.edit_message_text(
                    "ℹ️ <b>راهنما</b>\n\n"
                    "سرورها، Floating IP، ترافیک و Rescale از همین منو مدیریت می‌شوند.\n"
                    "هشدار ترافیک هر شب به‌صورت خودکار اجرا می‌شود.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=main_keyboard(),
                )
                return
            if len(PROJECTS) > 1:
                await query.edit_message_text("📁 پروژه را انتخاب کنید:", reply_markup=project_keyboard(target))
                return
            pidx = 0
            if target == "servers":
                await send_servers(query.message, pidx)
            elif target == "floating":
                await send_floating_list(query.message, pidx)
            elif target == "traffic":
                await send_traffic_report(query.message, pidx)
            return

        if data.startswith("prj:"):
            _, target, raw_pidx = data.split(":", 2)
            pidx = int(raw_pidx)
            await query.answer()
            if target == "servers":
                await send_servers(query.message, pidx)
            elif target == "floating":
                await send_floating_list(query.message, pidx)
            elif target == "traffic":
                await send_traffic_report(query.message, pidx)
            return

        parts = data.split(":")
        kind = parts[0]

        if kind == "srv":
            action, pidx, sid = parts[1], int(parts[2]), int(parts[3])
            project = get_project(pidx)
            if not project:
                await query.answer("پروژه پیدا نشد.", show_alert=True)
                return
            client = project["client"]
            server = await asyncio.to_thread(client.servers.get_by_id, sid)
            if not server:
                await query.answer("سرور پیدا نشد.", show_alert=True)
                return

            if action == "refresh":
                await query.answer("بروزرسانی شد")
                await query.edit_message_text(
                    server_text(server, project["name"]),
                    parse_mode=ParseMode.HTML,
                    reply_markup=server_keyboard(pidx, server),
                )
            elif action == "on":
                if server.status == "running":
                    await query.answer("سرور از قبل روشن است.", show_alert=True)
                else:
                    await asyncio.to_thread(server.power_on)
                    await query.answer("دستور روشن شدن ارسال شد.", show_alert=True)
            elif action == "off":
                if server.status == "off":
                    await query.answer("سرور از قبل خاموش است.", show_alert=True)
                else:
                    await asyncio.to_thread(server.shutdown)
                    await query.answer("دستور خاموش شدن امن ارسال شد.", show_alert=True)
            elif action == "resize":
                await query.answer()
                await show_resize_options(query, pidx, sid)
            elif action == "assignpip":
                if server.status != "off":
                    await query.answer("برای اتصال Primary IPv4 ابتدا سرور را خاموش کنید.", show_alert=True)
                    return
                primary_ips = await asyncio.to_thread(client.primary_ips.get_all)
                free_ip = next((ip for ip in primary_ips if getattr(ip, "type", None) == "ipv4" and getattr(ip, "assignee_id", None) is None), None)
                if not free_ip:
                    await query.answer("هیچ Primary IPv4 آزادی در پروژه وجود ندارد.", show_alert=True)
                    return
                act = await asyncio.to_thread(free_ip.assign, assignee_id=server.id, assignee_type="server")
                await asyncio.to_thread(act.wait_until_finished)
                server = await asyncio.to_thread(client.servers.get_by_id, sid)
                await query.answer(f"IPv4 {free_ip.ip} متصل شد.", show_alert=True)
                await query.edit_message_text(server_text(server, project["name"]), parse_mode=ParseMode.HTML, reply_markup=server_keyboard(pidx, server))
            elif action == "askpip":
                ipv4 = server_ipv4(server)
                if not ipv4:
                    await query.answer("این سرور Primary IPv4 ندارد.", show_alert=True)
                    return
                await query.answer()
                await query.edit_message_text(
                    f"⚠️ Primary IPv4 <code>{escape(ipv4)}</code> از سرور جدا و از پروژه حذف شود؟",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ بله، حذف شود", callback_data=f"srv:delpip:{pidx}:{sid}")],
                        [InlineKeyboardButton("❌ انصراف", callback_data=f"srv:refresh:{pidx}:{sid}")],
                    ]),
                )
            elif action == "delpip":
                if server.status != "off":
                    await query.answer("برای حذف Primary IPv4 ابتدا سرور را خاموش کنید.", show_alert=True)
                    return
                primary_ips = await asyncio.to_thread(client.primary_ips.get_all)
                target = next((ip for ip in primary_ips if getattr(ip, "type", None) == "ipv4" and getattr(ip, "assignee_id", None) == sid), None)
                if not target:
                    await query.answer("Primary IPv4 متصل پیدا نشد.", show_alert=True)
                    return
                act = await asyncio.to_thread(target.unassign)
                await asyncio.to_thread(act.wait_until_finished)
                await asyncio.to_thread(target.delete)
                server = await asyncio.to_thread(client.servers.get_by_id, sid)
                await query.answer(f"IPv4 {target.ip} حذف شد.", show_alert=True)
                await query.edit_message_text(server_text(server, project["name"]), parse_mode=ParseMode.HTML, reply_markup=server_keyboard(pidx, server))
            elif action == "fips":
                fips = await asyncio.to_thread(client.floating_ips.get_all)
                assigned = [f for f in fips if getattr(getattr(f, "server", None), "id", None) == sid]
                await query.answer()
                text = f"🌐 Floating IPهای متصل به <b>{escape(server.name)}</b>\n\n"
                text += "\n".join(f"• <code>{escape(str(f.ip))}</code> — {escape(str(f.name or f.id))}" for f in assigned) if assigned else "هیچ Floating IP متصل نیست."
                await query.edit_message_text(
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [InlineKeyboardButton("🌐 مدیریت همه Floating IPها", callback_data=f"prj:floating:{pidx}")],
                            [InlineKeyboardButton("⬅️ برگشت", callback_data=f"srv:refresh:{pidx}:{sid}")],
                        ]
                    ),
                )
            return

        if kind == "rzt":
            action = parts[1]
            if action == "pick":
                pidx, sid, stid = map(int, parts[2:5])
                project = get_project(pidx)
                client = project["client"]
                server = await asyncio.to_thread(client.servers.get_by_id, sid)
                st = await asyncio.to_thread(client.server_types.get_by_id, stid)
                await query.answer()
                await query.edit_message_text(
                    f"⚠️ <b>تأیید تغییر سایز</b>\n\n"
                    f"سرور: <b>{escape(server.name)}</b>\n"
                    f"از <code>{escape(server.server_type.name)}</code> به <code>{escape(st.name)}</code>\n"
                    f"منابع جدید: {st.cores} vCPU / {st.memory} GB RAM / {st.disk} GB\n\n"
                    "اگر «افزایش دیسک» را بزنید، دیسک بزرگ‌تر می‌شود و بعداً امکان کوچک‌کردن آن وجود ندارد.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [InlineKeyboardButton("✅ تغییر بدون افزایش دیسک", callback_data=f"rzt:go0:{pidx}:{sid}:{stid}")],
                            [InlineKeyboardButton("💽 تغییر + افزایش دیسک", callback_data=f"rzt:go1:{pidx}:{sid}:{stid}")],
                            [InlineKeyboardButton("❌ انصراف", callback_data=f"srv:refresh:{pidx}:{sid}")],
                        ]
                    ),
                )
            elif action in {"go0", "go1"}:
                pidx, sid, stid = map(int, parts[2:5])
                project = get_project(pidx)
                client = project["client"]
                server = await asyncio.to_thread(client.servers.get_by_id, sid)
                st = await asyncio.to_thread(client.server_types.get_by_id, stid)
                if server.status != "off":
                    await query.answer("سرور باید خاموش باشد.", show_alert=True)
                    return
                upgrade_disk = action == "go1"
                act = await asyncio.to_thread(server.change_type, st, upgrade_disk)
                await asyncio.to_thread(act.wait_until_finished)
                server = await asyncio.to_thread(client.servers.get_by_id, sid)
                await query.answer("تغییر سایز انجام شد.", show_alert=True)
                await query.edit_message_text(
                    server_text(server, project["name"]),
                    parse_mode=ParseMode.HTML,
                    reply_markup=server_keyboard(pidx, server),
                )
            return

        if kind == "fip":
            action = parts[1]
            pidx = int(parts[2])
            project = get_project(pidx)
            if not project:
                await query.answer("پروژه پیدا نشد.", show_alert=True)
                return
            client = project["client"]

            if action == "new":
                await query.answer()
                await query.edit_message_text(
                    "🌐 نوع Floating IP جدید را انتخاب کنید:",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [InlineKeyboardButton("IPv4", callback_data=f"fip:type4:{pidx}")],
                            [InlineKeyboardButton("IPv6", callback_data=f"fip:type6:{pidx}")],
                            [InlineKeyboardButton("⬅️ برگشت", callback_data=f"prj:floating:{pidx}")],
                        ]
                    ),
                )
                return

            if action in {"type4", "type6"}:
                ip_type = "ipv4" if action == "type4" else "ipv6"
                locations = await asyncio.to_thread(client.locations.get_all)
                context.user_data["fip_create"] = {"project": pidx, "type": ip_type}
                rows = [[InlineKeyboardButton(f"📍 {loc.name} — {loc.city}", callback_data=f"fip:loc:{pidx}:{loc.id}")] for loc in locations]
                rows.append([InlineKeyboardButton("⬅️ برگشت", callback_data=f"fip:new:{pidx}")])
                await query.answer()
                await query.edit_message_text("📍 Home Location را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(rows))
                return

            if action == "loc":
                loc_id = int(parts[3])
                pending = context.user_data.get("fip_create", {})
                pending.update({"project": pidx, "location": loc_id})
                context.user_data["fip_create"] = pending
                context.user_data["awaiting_text"] = "fip_name"
                await query.answer()
                await query.edit_message_text(
                    "✏️ یک نام برای Floating IP بفرستید.\n"
                    "مثال: <code>panel-prod</code> یا <code>project-a-ip</code>\n\n"
                    "برای انصراف /cancel را بفرستید.",
                    parse_mode=ParseMode.HTML,
                )
                return

            fid = int(parts[3]) if len(parts) > 3 else None
            fip = await asyncio.to_thread(client.floating_ips.get_by_id, fid) if fid else None
            if fid and not fip:
                await query.answer("Floating IP پیدا نشد.", show_alert=True)
                return

            if action == "open":
                await query.answer()
                await query.edit_message_text(
                    floating_text(fip, project["name"]),
                    parse_mode=ParseMode.HTML,
                    reply_markup=floating_keyboard(pidx, fip),
                )
            elif action == "choose":
                await query.answer()
                await show_fip_server_choices(query, pidx, fid)
            elif action == "assign":
                sid = int(parts[4])
                server = await asyncio.to_thread(client.servers.get_by_id, sid)
                act = await asyncio.to_thread(fip.assign, server)
                await asyncio.to_thread(act.wait_until_finished)
                fip = await asyncio.to_thread(client.floating_ips.get_by_id, fid)
                add_cmd, _ = floating_commands(fip)
                await query.answer("Floating IP متصل شد.", show_alert=True)
                await query.edit_message_text(
                    floating_text(fip, project["name"])
                    + "\n\n✅ <b>اتصال در Hetzner انجام شد.</b>\n"
                    + "برای فعال شدن IP داخل سیستم‌عامل سرور، این دستور را اجرا کنید:\n"
                    + f"<pre>{escape(add_cmd)}</pre>\n"
                    + "این دستور موقت است و بعد از ریبوت باید تنظیم Persistent سیستم‌عامل را انجام دهید.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=floating_keyboard(pidx, fip),
                )
            elif action == "unassign":
                act = await asyncio.to_thread(fip.unassign)
                await asyncio.to_thread(act.wait_until_finished)
                _, del_cmd = floating_commands(fip)
                fip = await asyncio.to_thread(client.floating_ips.get_by_id, fid)
                await query.answer("Floating IP جدا شد.", show_alert=True)
                await query.edit_message_text(
                    floating_text(fip, project["name"])
                    + "\n\nدر صورت نیاز، IP را داخل سیستم‌عامل سرور قبلی هم حذف کنید:\n"
                    + f"<pre>{escape(del_cmd)}</pre>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=floating_keyboard(pidx, fip),
                )
            elif action == "cmd":
                add_cmd, del_cmd = floating_commands(fip)
                await query.answer()
                await query.edit_message_text(
                    floating_text(fip, project["name"])
                    + "\n\n📋 <b>دستور اضافه کردن موقت:</b>\n"
                    + f"<pre>{escape(add_cmd)}</pre>\n"
                    + "📋 <b>دستور حذف موقت:</b>\n"
                    + f"<pre>{escape(del_cmd)}</pre>\n"
                    + "⚠️ برای ماندگاری بعد از reboot باید تنظیم Persistent متناسب با سیستم‌عامل انجام شود.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=floating_keyboard(pidx, fip),
                )
            elif action == "askdel":
                await query.answer()
                await query.edit_message_text(
                    f"⚠️ Floating IP <code>{escape(str(fip.ip))}</code> حذف شود؟\n"
                    "اگر متصل باشد ابتدا از سرور جدا می‌شود.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [InlineKeyboardButton("✅ بله، حذف شود", callback_data=f"fip:delete:{pidx}:{fid}")],
                            [InlineKeyboardButton("❌ انصراف", callback_data=f"fip:open:{pidx}:{fid}")],
                        ]
                    ),
                )
            elif action == "delete":
                if getattr(fip, "server", None):
                    act = await asyncio.to_thread(fip.unassign)
                    await asyncio.to_thread(act.wait_until_finished)
                    fip = await asyncio.to_thread(client.floating_ips.get_by_id, fid)
                await asyncio.to_thread(fip.delete)
                await query.answer("Floating IP حذف شد.", show_alert=True)
                await query.edit_message_text(
                    "✅ Floating IP از پروژه حذف شد.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ لیست Floating IPها", callback_data=f"prj:floating:{pidx}")]]),
                )
            return

        await query.answer("درخواست نامعتبر است.", show_alert=True)
    except Exception as exc:
        log.exception("Callback failed: %s", data)
        text = str(exc)
        if len(text) > 180:
            text = text[:177] + "..."
        try:
            await query.answer(f"خطا: {text}", show_alert=True)
        except Exception:
            pass


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await deny(update)
        return
    if context.user_data.get("awaiting_text") != "fip_name":
        return
    pending = context.user_data.get("fip_create", {})
    name = (update.effective_message.text or "").strip()
    if not name or len(name) > 64:
        await update.effective_message.reply_text("نام باید بین 1 تا 64 کاراکتر باشد.")
        return
    pidx = int(pending.get("project", -1))
    project = get_project(pidx)
    if not project:
        await update.effective_message.reply_text("❌ پروژه پیدا نشد.")
        return
    client = project["client"]
    try:
        location = await asyncio.to_thread(client.locations.get_by_id, int(pending["location"]))
        response = await asyncio.to_thread(
            client.floating_ips.create,
            type=pending["type"],
            home_location=location,
            name=name,
            description=f"Managed by Telegram bot - {name}",
        )
        fip = response.floating_ip
        if getattr(response, "action", None):
            await asyncio.to_thread(response.action.wait_until_finished)
            fip = await asyncio.to_thread(client.floating_ips.get_by_id, fip.id)
    except Exception as exc:
        log.exception("Floating IP create failed")
        await update.effective_message.reply_text(f"❌ ساخت Floating IP ناموفق بود:\n<code>{escape(str(exc))}</code>", parse_mode=ParseMode.HTML)
        return
    finally:
        context.user_data.pop("awaiting_text", None)
        context.user_data.pop("fip_create", None)

    await update.effective_message.reply_text(
        "✅ Floating IP ساخته شد.\n\n" + floating_text(fip, project["name"]),
        parse_mode=ParseMode.HTML,
        reply_markup=floating_keyboard(pidx, fip),
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await deny(update)
        return
    context.user_data.pop("awaiting_text", None)
    context.user_data.pop("fip_create", None)
    await update.effective_message.reply_text("عملیات لغو شد.", reply_markup=main_keyboard())


def read_alert_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Could not read traffic alert state")
    return {}


def write_alert_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception:
        log.exception("Could not write traffic alert state")


def reached_threshold(used_tb: float) -> int | None:
    reached = None
    for threshold in TRAFFIC_ALERT_TB:
        if used_tb >= threshold:
            reached = int(threshold)
    return reached


def threshold_icon(threshold: int) -> str:
    if threshold >= 20:
        return "🛑"
    if threshold >= 19:
        return "🚨"
    return "⚠️"


async def traffic_alert_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ALLOWED_USER_ID:
        return
    tz = ZoneInfo(BOT_TIMEZONE)
    today = __import__("datetime").datetime.now(tz).date().isoformat()
    state = read_alert_state()
    if state.get("last_alert_date") == today:
        return

    warnings = []
    for pidx, project in enumerate(PROJECTS):
        try:
            servers = await asyncio.to_thread(project["client"].servers.get_all)
        except Exception:
            log.exception("Traffic check failed for project %s", project["name"])
            continue
        for server in servers:
            used = traffic_tb(server)
            threshold = reached_threshold(used)
            if threshold is None:
                continue
            included = included_tb(server)
            remaining = max(20.0 - used, 0.0)
            warnings.append(
                "\n".join(
                    [
                        f"{threshold_icon(threshold)} <b>{escape(server.name)}</b>",
                        f"📁 پروژه: {escape(project['name'])}",
                        f"مصرف خروجی: <b>{used:.2f} TB</b>",
                        f"آستانه ردشده: <b>{threshold} TB</b>",
                        (f"سقف گزارش‌شده Hetzner: <b>{included:.2f} TB</b>" if included else "سقف مدنظر: <b>20 TB</b>"),
                        f"تا 20 TB: <b>{remaining:.2f} TB</b>",
                    ]
                )
            )

    if warnings:
        text = (
            "⚠️ <b>هشدار ترافیک Hetzner</b>\n"
            "بررسی شبانه انجام شد. سرورهای زیر به محدوده هشدار رسیده‌اند:\n\n"
            + "\n\n──────────\n\n".join(warnings)
        )
        await context.bot.send_message(chat_id=int(ALLOWED_USER_ID), text=text, parse_mode=ParseMode.HTML)
        state["last_alert_date"] = today
        write_alert_state(state)


def parse_job_time() -> time:
    try:
        hour, minute = [int(x) for x in TRAFFIC_CHECK_TIME.split(":", 1)]
        return time(hour=hour, minute=minute, tzinfo=ZoneInfo(BOT_TIMEZONE))
    except Exception as exc:
        raise SystemExit(f"Invalid TRAFFIC_CHECK_TIME/BOT_TIMEZONE: {exc}") from exc


def validate_config() -> None:
    missing = []
    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not ALLOWED_USER_ID or not ALLOWED_USER_ID.isdigit():
        missing.append("ALLOWED_USER_ID (numeric)")
    if not PROJECTS:
        missing.append("HETZNER_PROJECTS_B64 or HETZNER_API_TOKEN")
    if missing:
        raise SystemExit("Missing/invalid configuration: " + ", ".join(missing))


if __name__ == "__main__":
    validate_config()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("servers", cmd_servers))
    app.add_handler(CommandHandler("traffic", cmd_traffic))
    app.add_handler(CommandHandler("floating", cmd_floating))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.job_queue.run_daily(traffic_alert_job, time=parse_job_time(), name="nightly-traffic-alert")
    print(f"Hetzner Telegram Bot is running with {len(PROJECTS)} project(s)...")
    app.run_polling(drop_pending_updates=True)
