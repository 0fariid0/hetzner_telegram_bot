#!/usr/bin/env python3
import asyncio
import base64
import json
import logging
import os
from datetime import datetime, time
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from hcloud import Client
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
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
MAX_TEXT = 3900

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


def clip(text: str, limit: int = MAX_TEXT) -> str:
    if len(text) <= limit:
        return text
    suffix = "\n\n… فهرست طولانی است و بخشی از متن کوتاه شده است."
    out = []
    used = 0
    for line in text.splitlines():
        addition = len(line) + (1 if out else 0)
        if used + addition + len(suffix) > limit:
            break
        out.append(line)
        used += addition
    return "\n".join(out) + suffix


def main_text() -> str:
    return (
        "🤖 <b>پنل خصوصی Hetzner</b>\n\n"
        f"📁 پروژه‌ها: <b>{len(PROJECTS)}</b>\n"
        "برای مدیریت، ابتدا پروژه را انتخاب کنید.\n\n"
        "🚨 بررسی ترافیک 18 / 19 / 20 TB هر شب به‌صورت خودکار انجام می‌شود."
    )


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📁 انتخاب پروژه", callback_data="projects")],
            [InlineKeyboardButton("📊 ترافیک همه پروژه‌ها", callback_data="traffic:all")],
            [InlineKeyboardButton("ℹ️ راهنما", callback_data="help")],
        ]
    )


def projects_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"📁 {project['name']}", callback_data=f"project:{i}")]
        for i, project in enumerate(PROJECTS)
    ]
    rows.append([InlineKeyboardButton("⬅️ منوی اصلی", callback_data="main")])
    return InlineKeyboardMarkup(rows)


def server_text(server, project_name: str) -> str:
    ipv4 = server_ipv4(server) or "ندارد"
    ipv6 = server_ipv6(server) or "ندارد"
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
        f"IPv6: <code>{escape(str(ipv6))}</code>\n"
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
            [InlineKeyboardButton("🌐 Floating IPهای سرور", callback_data=f"srv:fips:{pidx}:{sid}")],
            [InlineKeyboardButton("🔄 بروزرسانی", callback_data=f"srv:refresh:{pidx}:{sid}")],
            [InlineKeyboardButton("⬅️ برگشت به پروژه", callback_data=f"project:{pidx}")],
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
                InlineKeyboardButton("🔁 انتقال", callback_data=f"fip:choose:{pidx}:{fid}"),
                InlineKeyboardButton("➖ جدا کردن", callback_data=f"fip:unassign:{pidx}:{fid}"),
            ]
        )
    else:
        rows.append([InlineKeyboardButton("➕ اتصال به سرور", callback_data=f"fip:choose:{pidx}:{fid}")])
    rows.extend(
        [
            [InlineKeyboardButton("📋 دستور تنظیم IP", callback_data=f"fip:cmd:{pidx}:{fid}")],
            [InlineKeyboardButton("🗑 حذف Floating IP", callback_data=f"fip:askdel:{pidx}:{fid}")],
            [InlineKeyboardButton("⬅️ برگشت به Floating IPها", callback_data=f"fips:{pidx}")],
        ]
    )
    return InlineKeyboardMarkup(rows)


async def safe_edit(query, text: str, reply_markup=None, parse_mode=ParseMode.HTML) -> None:
    try:
        await query.edit_message_text(
            clip(text),
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            raise


async def render_message(message, text: str, reply_markup=None) -> None:
    await message.reply_text(
        clip(text),
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )


async def deny(update: Update) -> None:
    if update.callback_query:
        await update.callback_query.answer()


async def show_projects(query) -> None:
    text = "📁 <b>پروژه‌های Hetzner</b>\n\nپروژه موردنظر را انتخاب کنید:"
    await safe_edit(query, text, projects_keyboard())


async def fetch_project_data(pidx: int):
    project = get_project(pidx)
    if not project:
        raise ValueError("پروژه پیدا نشد")
    client = project["client"]
    servers, fips = await asyncio.gather(
        asyncio.to_thread(client.servers.get_all),
        asyncio.to_thread(client.floating_ips.get_all),
    )
    return project, servers, fips


def project_dashboard_text(project: dict, servers: list, fips: list) -> str:
    lines = [
        f"📁 <b>{escape(project['name'])}</b>",
        f"🖥 سرورها: <b>{len(servers)}</b>   |   🌐 Floating IP: <b>{len(fips)}</b>",
        "",
        "<b>سرورها:</b>",
    ]
    if not servers:
        lines.append("— هیچ سروری در این پروژه نیست.")
    else:
        for idx, server in enumerate(servers, 1):
            status = getattr(server, "status", "unknown")
            st = getattr(getattr(server, "server_type", None), "name", "?")
            ip = server_ipv4(server) or "بدون IPv4"
            used = traffic_tb(server)
            lines.append(
                f"{idx}. {status_icon(status)} <b>{escape(server.name)}</b> — "
                f"<code>{escape(str(st))}</code> — <code>{escape(str(ip))}</code> — {used:.2f} TB"
            )
    lines.extend(["", "از دکمه‌های زیر سرور یا بخش موردنظر را انتخاب کنید."])
    return "\n".join(lines)


def project_dashboard_keyboard(pidx: int, servers: list) -> InlineKeyboardMarkup:
    rows = []
    # Two compact server buttons per row to reduce vertical clutter.
    buttons = [InlineKeyboardButton(f"🖥 {s.name}", callback_data=f"srv:open:{pidx}:{s.id}") for s in servers]
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i : i + 2])
    rows.extend(
        [
            [
                InlineKeyboardButton("🌐 Floating IP", callback_data=f"fips:{pidx}"),
                InlineKeyboardButton("📊 ترافیک", callback_data=f"traffic:{pidx}"),
            ],
            [InlineKeyboardButton("🔄 بروزرسانی پروژه", callback_data=f"project:{pidx}")],
            [InlineKeyboardButton("⬅️ پروژه‌ها", callback_data="projects")],
        ]
    )
    return InlineKeyboardMarkup(rows)


async def show_project(query, pidx: int) -> None:
    project, servers, fips = await fetch_project_data(pidx)
    await safe_edit(query, project_dashboard_text(project, servers, fips), project_dashboard_keyboard(pidx, servers))


def floating_list_text(project: dict, fips: list) -> str:
    lines = [
        f"🌐 <b>Floating IP — {escape(project['name'])}</b>",
        f"تعداد: <b>{len(fips)}</b>",
        "",
    ]
    if not fips:
        lines.append("هیچ Floating IP در این پروژه وجود ندارد.")
    else:
        for idx, fip in enumerate(fips, 1):
            name = getattr(fip, "name", None) or f"IP #{fip.id}"
            server = getattr(getattr(fip, "server", None), "name", None) or "آزاد"
            lines.append(
                f"{idx}. <b>{escape(str(name))}</b> — <code>{escape(str(fip.ip))}</code> — {escape(str(server))}"
            )
    lines.append("\nبرای مدیریت، IP را از دکمه‌های زیر انتخاب کنید.")
    return "\n".join(lines)


def floating_list_keyboard(pidx: int, fips: list) -> InlineKeyboardMarkup:
    rows = []
    for fip in fips:
        name = getattr(fip, "name", None) or str(fip.ip)
        rows.append([InlineKeyboardButton(f"🌐 {name}", callback_data=f"fip:open:{pidx}:{fip.id}")])
    rows.extend(
        [
            [InlineKeyboardButton("➕ ساخت Floating IP", callback_data=f"fip:new:{pidx}")],
            [InlineKeyboardButton("🔄 بروزرسانی", callback_data=f"fips:{pidx}")],
            [InlineKeyboardButton("⬅️ برگشت به پروژه", callback_data=f"project:{pidx}")],
        ]
    )
    return InlineKeyboardMarkup(rows)


async def show_floating_list(query, pidx: int) -> None:
    project = get_project(pidx)
    if not project:
        await query.answer("پروژه پیدا نشد.", show_alert=True)
        return
    fips = await asyncio.to_thread(project["client"].floating_ips.get_all)
    await safe_edit(query, floating_list_text(project, fips), floating_list_keyboard(pidx, fips))


async def show_server(query, pidx: int, sid: int, notice: str | None = None) -> None:
    project = get_project(pidx)
    if not project:
        await query.answer("پروژه پیدا نشد.", show_alert=True)
        return
    server = await asyncio.to_thread(project["client"].servers.get_by_id, sid)
    if not server:
        await query.answer("سرور پیدا نشد.", show_alert=True)
        return
    text = server_text(server, project["name"])
    if notice:
        text += f"\n\n{notice}"
    await safe_edit(query, text, server_keyboard(pidx, server))


async def show_project_traffic(query, pidx: int) -> None:
    project = get_project(pidx)
    if not project:
        await query.answer("پروژه پیدا نشد.", show_alert=True)
        return
    servers = await asyncio.to_thread(project["client"].servers.get_all)
    lines = [f"📊 <b>ترافیک ماه جاری — {escape(project['name'])}</b>", ""]
    if not servers:
        lines.append("هیچ سروری پیدا نشد.")
    for server in servers:
        used = traffic_tb(server)
        included = included_tb(server)
        limit = f" / {included:.2f} TB" if included else ""
        marker = "🛑" if used >= 20 else "🚨" if used >= 19 else "⚠️" if used >= 18 else "✅"
        lines.append(f"{marker} <b>{escape(server.name)}</b>: {used:.2f}{limit}")
    await safe_edit(
        query,
        "\n".join(lines),
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔄 بروزرسانی", callback_data=f"traffic:{pidx}")],
                [InlineKeyboardButton("⬅️ برگشت به پروژه", callback_data=f"project:{pidx}")],
            ]
        ),
    )


async def show_all_traffic(query) -> None:
    lines = ["📊 <b>ترافیک ماه جاری — همه پروژه‌ها</b>", ""]
    for pidx, project in enumerate(PROJECTS):
        try:
            servers = await asyncio.to_thread(project["client"].servers.get_all)
        except Exception as exc:
            lines.append(f"📁 <b>{escape(project['name'])}</b>: ❌ {escape(str(exc))}")
            continue
        lines.append(f"📁 <b>{escape(project['name'])}</b>")
        if not servers:
            lines.append("— بدون سرور")
        for server in servers:
            used = traffic_tb(server)
            marker = "🛑" if used >= 20 else "🚨" if used >= 19 else "⚠️" if used >= 18 else "✅"
            lines.append(f"{marker} {escape(server.name)}: <b>{used:.2f} TB</b>")
        lines.append("")
    await safe_edit(
        query,
        "\n".join(lines),
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔄 بروزرسانی", callback_data="traffic:all")],
                [InlineKeyboardButton("⬅️ منوی اصلی", callback_data="main")],
            ]
        ),
    )


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
        if getattr(st, "deprecated", False):
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
    for st in types[:35]:
        label = f"{st.name} | {st.cores}C / {st.memory}GB / {st.disk}GB"
        rows.append([InlineKeyboardButton(label, callback_data=f"rzt:pick:{pidx}:{server_id}:{st.id}")])
    rows.append([InlineKeyboardButton("⬅️ برگشت به سرور", callback_data=f"srv:open:{pidx}:{server_id}")])
    await safe_edit(
        query,
        f"⚙️ <b>تغییر سایز {escape(server.name)}</b>\n\n"
        f"پلن فعلی: <code>{escape(server.server_type.name)}</code>\n"
        "پلن جدید را انتخاب کنید. فقط گزینه‌های هم‌معماری و سازگار نمایش داده شده‌اند.",
        InlineKeyboardMarkup(rows),
    )


async def show_server_fips(query, pidx: int, server_id: int) -> None:
    project = get_project(pidx)
    if not project:
        await query.answer("پروژه پیدا نشد.", show_alert=True)
        return
    client = project["client"]
    server, fips = await asyncio.gather(
        asyncio.to_thread(client.servers.get_by_id, server_id),
        asyncio.to_thread(client.floating_ips.get_all),
    )
    if not server:
        await query.answer("سرور پیدا نشد.", show_alert=True)
        return
    assigned = [f for f in fips if getattr(getattr(f, "server", None), "id", None) == server_id]
    free = [f for f in fips if getattr(f, "server", None) is None]
    lines = [f"🌐 <b>Floating IPهای {escape(server.name)}</b>", ""]
    if assigned:
        lines.append("<b>متصل:</b>")
        for f in assigned:
            lines.append(f"• <code>{escape(str(f.ip))}</code> — {escape(str(f.name or f.id))}")
    else:
        lines.append("هیچ Floating IP متصل نیست.")
    if free:
        lines.extend(["", f"Floating IP آزاد در پروژه: <b>{len(free)}</b>"])
    rows = []
    for f in assigned:
        rows.append([InlineKeyboardButton(f"🌐 {f.name or f.ip}", callback_data=f"fip:open:{pidx}:{f.id}")])
    rows.extend(
        [
            [InlineKeyboardButton("🌐 مدیریت همه Floating IPها", callback_data=f"fips:{pidx}")],
            [InlineKeyboardButton("⬅️ برگشت به سرور", callback_data=f"srv:open:{pidx}:{server_id}")],
        ]
    )
    await safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(rows))


async def show_fip_server_choices(query, pidx: int, fip_id: int) -> None:
    project = get_project(pidx)
    if not project:
        await query.answer("پروژه پیدا نشد.", show_alert=True)
        return
    client = project["client"]
    fip, servers = await asyncio.gather(
        asyncio.to_thread(client.floating_ips.get_by_id, fip_id),
        asyncio.to_thread(client.servers.get_all),
    )
    if not fip:
        await query.answer("Floating IP پیدا نشد.", show_alert=True)
        return
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
    rows = [
        [InlineKeyboardButton(f"🖥 {s.name}", callback_data=f"fip:assign:{pidx}:{fip_id}:{s.id}")]
        for s in compatible
    ]
    rows.append([InlineKeyboardButton("⬅️ برگشت به IP", callback_data=f"fip:open:{pidx}:{fip_id}")])
    await safe_edit(
        query,
        f"🌐 Floating IP: <code>{escape(str(fip.ip))}</code>\n\nسرور مقصد را انتخاب کنید:",
        InlineKeyboardMarkup(rows),
    )


async def clear_text_flow(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("awaiting_text", None)
    context.user_data.pop("fip_create", None)
    context.user_data.pop("panel_chat_id", None)
    context.user_data.pop("panel_message_id", None)


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not authorized(update):
        await deny(update)
        return
    data = query.data or ""

    # Any button press while waiting for a name means the text-entry flow was abandoned.
    if context.user_data.get("awaiting_text"):
        await clear_text_flow(context)

    try:
        if data == "main":
            await query.answer()
            await safe_edit(query, main_text(), main_keyboard())
            return
        if data == "projects":
            await query.answer()
            await show_projects(query)
            return
        if data == "help":
            await query.answer()
            await safe_edit(
                query,
                "ℹ️ <b>راهنما</b>\n\n"
                "• ابتدا پروژه را انتخاب کنید.\n"
                "• لیست سرورها داخل همان پیام پروژه نمایش داده می‌شود.\n"
                "• تمام منوها با ویرایش همان پیام باز می‌شوند و دکمه برگشت دارند.\n"
                "• Floating IP را می‌توانید بسازید، متصل، منتقل، جدا و حذف کنید.\n"
                "• بعد از اتصال، دستور Linux برای افزودن IP نمایش داده می‌شود.\n"
                "• Rescale فقط روی سرور خاموش انجام می‌شود.\n"
                "• هشدار 18/19/20 TB هر شب فقط یک بار در روز ارسال می‌شود.",
                InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ منوی اصلی", callback_data="main")]]),
            )
            return
        if data.startswith("project:"):
            await query.answer()
            await show_project(query, int(data.split(":", 1)[1]))
            return
        if data.startswith("fips:"):
            await query.answer()
            await show_floating_list(query, int(data.split(":", 1)[1]))
            return
        if data.startswith("traffic:"):
            target = data.split(":", 1)[1]
            await query.answer()
            if target == "all":
                await show_all_traffic(query)
            else:
                await show_project_traffic(query, int(target))
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

            if action in {"open", "refresh"}:
                await query.answer("بروزرسانی شد" if action == "refresh" else None)
                await show_server(query, pidx, sid)
            elif action == "on":
                if server.status == "running":
                    await query.answer("سرور از قبل روشن است.", show_alert=True)
                else:
                    await query.answer("در حال روشن کردن سرور...")
                    action_obj = await asyncio.to_thread(server.power_on)
                    await asyncio.to_thread(action_obj.wait_until_finished)
                    await show_server(query, pidx, sid, "✅ سرور روشن شد.")
            elif action == "off":
                if server.status == "off":
                    await query.answer("سرور از قبل خاموش است.", show_alert=True)
                else:
                    action_obj = await asyncio.to_thread(server.shutdown)
                    await query.answer("دستور خاموش شدن امن ارسال شد.", show_alert=True)
                    # Shutdown may take time; don't block the UI until the guest OS stops.
                    await show_server(query, pidx, sid, "⏳ دستور خاموش شدن امن ارسال شد.")
            elif action == "resize":
                await query.answer()
                await show_resize_options(query, pidx, sid)
            elif action == "assignpip":
                if server.status != "off":
                    await query.answer("برای اتصال Primary IPv4 ابتدا سرور را خاموش کنید.", show_alert=True)
                    return
                primary_ips = await asyncio.to_thread(client.primary_ips.get_all)
                free_ip = next(
                    (
                        ip
                        for ip in primary_ips
                        if getattr(ip, "type", None) == "ipv4"
                        and getattr(ip, "assignee_id", None) is None
                        and getattr(getattr(ip, "location", None), "name", None) == server_location(server)
                    ),
                    None,
                )
                if not free_ip:
                    await query.answer("Primary IPv4 آزاد و هم‌Location پیدا نشد.", show_alert=True)
                    return
                await query.answer("در حال اتصال Primary IPv4...")
                act = await asyncio.to_thread(free_ip.assign, assignee_id=server.id, assignee_type="server")
                await asyncio.to_thread(act.wait_until_finished)
                await show_server(query, pidx, sid, f"✅ Primary IPv4 <code>{escape(str(free_ip.ip))}</code> متصل شد.")
            elif action == "askpip":
                ipv4 = server_ipv4(server)
                if not ipv4:
                    await query.answer("این سرور Primary IPv4 ندارد.", show_alert=True)
                    return
                await query.answer()
                await safe_edit(
                    query,
                    f"⚠️ <b>حذف Primary IPv4</b>\n\n"
                    f"سرور: <b>{escape(server.name)}</b>\n"
                    f"IP: <code>{escape(ipv4)}</code>\n\n"
                    "IP ابتدا Unassign و سپس از پروژه حذف می‌شود. ادامه می‌دهید؟",
                    InlineKeyboardMarkup(
                        [
                            [InlineKeyboardButton("✅ بله، حذف شود", callback_data=f"srv:delpip:{pidx}:{sid}")],
                            [InlineKeyboardButton("⬅️ انصراف", callback_data=f"srv:open:{pidx}:{sid}")],
                        ]
                    ),
                )
            elif action == "delpip":
                if server.status != "off":
                    await query.answer("برای حذف Primary IPv4 ابتدا سرور را خاموش کنید.", show_alert=True)
                    return
                primary_ips = await asyncio.to_thread(client.primary_ips.get_all)
                target = next(
                    (
                        ip
                        for ip in primary_ips
                        if getattr(ip, "type", None) == "ipv4" and getattr(ip, "assignee_id", None) == sid
                    ),
                    None,
                )
                if not target:
                    await query.answer("Primary IPv4 متصل پیدا نشد.", show_alert=True)
                    return
                ip_value = str(target.ip)
                await query.answer("در حال حذف Primary IPv4...")
                act = await asyncio.to_thread(target.unassign)
                await asyncio.to_thread(act.wait_until_finished)
                target = await asyncio.to_thread(client.primary_ips.get_by_id, target.id)
                await asyncio.to_thread(target.delete)
                await show_server(query, pidx, sid, f"✅ Primary IPv4 <code>{escape(ip_value)}</code> حذف شد.")
            elif action == "fips":
                await query.answer()
                await show_server_fips(query, pidx, sid)
            return

        if kind == "rzt":
            action = parts[1]
            if action == "pick":
                pidx, sid, stid = map(int, parts[2:5])
                project = get_project(pidx)
                client = project["client"]
                server = await asyncio.to_thread(client.servers.get_by_id, sid)
                st = await asyncio.to_thread(client.server_types.get_by_id, stid)
                if not server or not st:
                    await query.answer("سرور یا پلن پیدا نشد.", show_alert=True)
                    return
                await query.answer()
                await safe_edit(
                    query,
                    f"⚠️ <b>تأیید تغییر سایز</b>\n\n"
                    f"سرور: <b>{escape(server.name)}</b>\n"
                    f"از <code>{escape(server.server_type.name)}</code> به <code>{escape(st.name)}</code>\n"
                    f"منابع جدید: {st.cores} vCPU / {st.memory} GB RAM / {st.disk} GB\n\n"
                    "گزینه افزایش دیسک برگشت‌پذیر نیست و بعداً امکان کوچک‌کردن Disk وجود ندارد.",
                    InlineKeyboardMarkup(
                        [
                            [InlineKeyboardButton("✅ بدون افزایش دیسک", callback_data=f"rzt:go0:{pidx}:{sid}:{stid}")],
                            [InlineKeyboardButton("💽 با افزایش دیسک", callback_data=f"rzt:go1:{pidx}:{sid}:{stid}")],
                            [InlineKeyboardButton("⬅️ برگشت", callback_data=f"srv:resize:{pidx}:{sid}")],
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
                await query.answer("در حال تغییر سایز...")
                act = await asyncio.to_thread(server.change_type, st, upgrade_disk)
                await asyncio.to_thread(act.wait_until_finished)
                await show_server(query, pidx, sid, f"✅ پلن به <code>{escape(st.name)}</code> تغییر کرد.")
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
                await safe_edit(
                    query,
                    f"🌐 <b>ساخت Floating IP — {escape(project['name'])}</b>\n\nنوع IP را انتخاب کنید:",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton("IPv4", callback_data=f"fip:type4:{pidx}"),
                                InlineKeyboardButton("IPv6", callback_data=f"fip:type6:{pidx}"),
                            ],
                            [InlineKeyboardButton("⬅️ برگشت", callback_data=f"fips:{pidx}")],
                        ]
                    ),
                )
                return

            if action in {"type4", "type6"}:
                ip_type = "ipv4" if action == "type4" else "ipv6"
                locations = await asyncio.to_thread(client.locations.get_all)
                context.user_data["fip_create"] = {"project": pidx, "type": ip_type}
                rows = [
                    [InlineKeyboardButton(f"📍 {loc.name} — {loc.city}", callback_data=f"fip:loc:{pidx}:{loc.id}")]
                    for loc in locations
                ]
                rows.append([InlineKeyboardButton("⬅️ برگشت", callback_data=f"fip:new:{pidx}")])
                await query.answer()
                await safe_edit(
                    query,
                    f"📍 <b>Home Location</b>\n\nنوع: <code>{ip_type}</code>\nموقعیت را انتخاب کنید:",
                    InlineKeyboardMarkup(rows),
                )
                return

            if action == "loc":
                loc_id = int(parts[3])
                pending = context.user_data.get("fip_create", {})
                pending.update({"project": pidx, "location": loc_id})
                context.user_data["fip_create"] = pending
                context.user_data["awaiting_text"] = "fip_name"
                context.user_data["panel_chat_id"] = query.message.chat_id
                context.user_data["panel_message_id"] = query.message.message_id
                await query.answer()
                await safe_edit(
                    query,
                    "✏️ <b>نام Floating IP</b>\n\n"
                    "نام دلخواه را به‌صورت پیام بفرستید.\n"
                    "مثال: <code>panel-prod</code>\n\n"
                    "بعد از دریافت نام، پیام شما پاک می‌شود و همین پنل بروزرسانی خواهد شد.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ انصراف", callback_data=f"fips:{pidx}")]]),
                )
                return

            fid = int(parts[3]) if len(parts) > 3 else None
            fip = await asyncio.to_thread(client.floating_ips.get_by_id, fid) if fid else None
            if fid and not fip:
                await query.answer("Floating IP پیدا نشد.", show_alert=True)
                return

            if action == "open":
                await query.answer()
                await safe_edit(query, floating_text(fip, project["name"]), floating_keyboard(pidx, fip))
            elif action == "choose":
                await query.answer()
                await show_fip_server_choices(query, pidx, fid)
            elif action == "assign":
                sid = int(parts[4])
                server = await asyncio.to_thread(client.servers.get_by_id, sid)
                if not server:
                    await query.answer("سرور پیدا نشد.", show_alert=True)
                    return
                if fip.type == "ipv4" and not server_ipv4(server):
                    await query.answer("این سرور Primary IPv4 ندارد.", show_alert=True)
                    return
                if fip.type == "ipv6" and not server_ipv6(server):
                    await query.answer("این سرور Primary IPv6 ندارد.", show_alert=True)
                    return
                current_server = getattr(fip, "server", None)
                if current_server and getattr(current_server, "id", None) == sid:
                    await query.answer("این IP از قبل روی همین سرور است.", show_alert=True)
                    return
                await query.answer("در حال اتصال Floating IP...")
                if current_server:
                    old_name = getattr(current_server, "name", "سرور قبلی")
                    unassign_act = await asyncio.to_thread(fip.unassign)
                    await asyncio.to_thread(unassign_act.wait_until_finished)
                    fip = await asyncio.to_thread(client.floating_ips.get_by_id, fid)
                else:
                    old_name = None
                act = await asyncio.to_thread(fip.assign, server)
                await asyncio.to_thread(act.wait_until_finished)
                fip = await asyncio.to_thread(client.floating_ips.get_by_id, fid)
                add_cmd, _ = floating_commands(fip)
                notice = "✅ <b>اتصال در Hetzner انجام شد.</b>"
                if old_name:
                    notice = f"✅ IP از <b>{escape(str(old_name))}</b> به <b>{escape(server.name)}</b> منتقل شد."
                await safe_edit(
                    query,
                    floating_text(fip, project["name"])
                    + f"\n\n{notice}\n"
                    + "دستور اضافه‌کردن داخل Linux:\n"
                    + f"<pre>{escape(add_cmd)}</pre>\n"
                    + "⚠️ این دستور موقت است؛ برای ماندگاری بعد از reboot تنظیم Persistent لازم است.",
                    floating_keyboard(pidx, fip),
                )
            elif action == "unassign":
                old_server = getattr(getattr(fip, "server", None), "name", "سرور قبلی")
                _, del_cmd = floating_commands(fip)
                await query.answer("در حال جدا کردن Floating IP...")
                act = await asyncio.to_thread(fip.unassign)
                await asyncio.to_thread(act.wait_until_finished)
                fip = await asyncio.to_thread(client.floating_ips.get_by_id, fid)
                await safe_edit(
                    query,
                    floating_text(fip, project["name"])
                    + f"\n\n✅ از <b>{escape(str(old_server))}</b> جدا شد.\n"
                    + "در صورت نیاز روی سرور قبلی نیز حذف کنید:\n"
                    + f"<pre>{escape(del_cmd)}</pre>",
                    floating_keyboard(pidx, fip),
                )
            elif action == "cmd":
                add_cmd, del_cmd = floating_commands(fip)
                await query.answer()
                await safe_edit(
                    query,
                    floating_text(fip, project["name"])
                    + "\n\n📋 <b>اضافه کردن موقت:</b>\n"
                    + f"<pre>{escape(add_cmd)}</pre>\n"
                    + "📋 <b>حذف موقت:</b>\n"
                    + f"<pre>{escape(del_cmd)}</pre>\n"
                    + "⚠️ برای ماندگاری بعد از reboot باید تنظیم Persistent سیستم‌عامل انجام شود.",
                    floating_keyboard(pidx, fip),
                )
            elif action == "askdel":
                await query.answer()
                assigned = getattr(fip, "server", None)
                assigned_text = (
                    f"\nاین IP اکنون به <b>{escape(assigned.name)}</b> متصل است و قبل از حذف Unassign می‌شود."
                    if assigned
                    else ""
                )
                await safe_edit(
                    query,
                    f"⚠️ <b>حذف Floating IP</b>\n\n"
                    f"IP: <code>{escape(str(fip.ip))}</code>{assigned_text}\n\nادامه می‌دهید؟",
                    InlineKeyboardMarkup(
                        [
                            [InlineKeyboardButton("✅ بله، حذف شود", callback_data=f"fip:delete:{pidx}:{fid}")],
                            [InlineKeyboardButton("⬅️ انصراف", callback_data=f"fip:open:{pidx}:{fid}")],
                        ]
                    ),
                )
            elif action == "delete":
                ip_value = str(fip.ip)
                await query.answer("در حال حذف Floating IP...")
                if getattr(fip, "server", None):
                    act = await asyncio.to_thread(fip.unassign)
                    await asyncio.to_thread(act.wait_until_finished)
                    fip = await asyncio.to_thread(client.floating_ips.get_by_id, fid)
                await asyncio.to_thread(fip.delete)
                fips = await asyncio.to_thread(client.floating_ips.get_all)
                await safe_edit(
                    query,
                    f"✅ Floating IP <code>{escape(ip_value)}</code> حذف شد.\n\n" + floating_list_text(project, fips),
                    floating_list_keyboard(pidx, fips),
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
    message = update.effective_message
    name = (message.text or "").strip()
    if not name or len(name) > 64:
        chat_id = context.user_data.get("panel_chat_id")
        message_id = context.user_data.get("panel_message_id")
        pidx = int(pending.get("project", -1))
        try:
            await message.delete()
        except Exception:
            pass
        if chat_id and message_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text="❌ نام باید بین 1 تا 64 کاراکتر باشد.\n\n✏️ نام Floating IP را دوباره بفرستید.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ انصراف", callback_data=f"fips:{pidx}")]]),
                )
            except Exception:
                pass
        return
    pidx = int(pending.get("project", -1))
    project = get_project(pidx)
    if not project:
        await clear_text_flow(context)
        return
    client = project["client"]
    chat_id = context.user_data.get("panel_chat_id")
    message_id = context.user_data.get("panel_message_id")
    try:
        existing = await asyncio.to_thread(client.floating_ips.get_by_name, name)
        if existing:
            raise ValueError("این نام قبلاً در پروژه استفاده شده است.")
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
        try:
            await message.delete()
        except Exception:
            pass
        if chat_id and message_id:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="✅ Floating IP ساخته شد.\n\n" + floating_text(fip, project["name"]),
                parse_mode=ParseMode.HTML,
                reply_markup=floating_keyboard(pidx, fip),
                disable_web_page_preview=True,
            )
        else:
            await render_message(message, "✅ Floating IP ساخته شد.\n\n" + floating_text(fip, project["name"]), floating_keyboard(pidx, fip))
    except Exception as exc:
        log.exception("Floating IP create failed")
        try:
            await message.delete()
        except Exception:
            pass
        error_text = f"❌ ساخت Floating IP ناموفق بود:\n<code>{escape(str(exc))}</code>"
        if chat_id and message_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=error_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Floating IPها", callback_data=f"fips:{pidx}")]]),
                )
            except Exception:
                pass
    finally:
        await clear_text_flow(context)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await deny(update)
        return
    await render_message(update.effective_message, main_text(), main_keyboard())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await deny(update)
        return
    await render_message(
        update.effective_message,
        "ℹ️ <b>راهنما</b>\n\n"
        "از /start وارد پنل شوید. تمام صفحات بعدی در همان پیام باز می‌شوند.\n"
        "برای هر پروژه، سرورها در متن و دکمه انتخاب هر سرور زیر همان پیام هستند.",
        InlineKeyboardMarkup([[InlineKeyboardButton("📁 انتخاب پروژه", callback_data="projects")]]),
    )


async def cmd_servers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await deny(update)
        return
    await render_message(update.effective_message, "📁 پروژه را انتخاب کنید:", projects_keyboard())


async def cmd_floating(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await deny(update)
        return
    await render_message(update.effective_message, "📁 پروژه را برای Floating IP انتخاب کنید:", projects_keyboard())


async def cmd_traffic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await deny(update)
        return
    await render_message(
        update.effective_message,
        "📊 گزارش ترافیک را از پنل باز کنید:",
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📊 همه پروژه‌ها", callback_data="traffic:all")],
                [InlineKeyboardButton("📁 انتخاب پروژه", callback_data="projects")],
            ]
        ),
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await deny(update)
        return
    await update.effective_message.reply_text(
        f"شناسه عددی مجاز:\n<code>{update.effective_user.id}</code>", parse_mode=ParseMode.HTML
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await deny(update)
        return
    chat_id = context.user_data.get("panel_chat_id")
    message_id = context.user_data.get("panel_message_id")
    pending = context.user_data.get("fip_create", {})
    pidx = int(pending.get("project", -1)) if pending else -1
    await clear_text_flow(context)
    try:
        await update.effective_message.delete()
    except Exception:
        pass
    if chat_id and message_id and get_project(pidx):
        project = get_project(pidx)
        fips = await asyncio.to_thread(project["client"].floating_ips.get_all)
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=floating_list_text(project, fips),
                parse_mode=ParseMode.HTML,
                reply_markup=floating_list_keyboard(pidx, fips),
            )
        except Exception:
            pass


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
    today = datetime.now(tz).date().isoformat()
    state = read_alert_state()
    if state.get("last_alert_date") == today:
        return

    warnings = []
    for project in PROJECTS:
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
        await context.bot.send_message(chat_id=int(ALLOWED_USER_ID), text=clip(text), parse_mode=ParseMode.HTML)
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
