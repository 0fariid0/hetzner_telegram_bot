#!/usr/bin/env python3
import asyncio
import base64
import json
import logging
import os
import re
from datetime import datetime, time
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from hcloud import Client
from hcloud.servers import ServerCreatePublicNetwork
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
CHEAP_CHECK_HOURS = float(os.getenv("CHEAP_CHECK_HOURS", "1") or 1)
STATE_FILE = Path(os.getenv("STATE_FILE", "/opt/hetzner-telegram-bot/.traffic_alert_state.json"))
AVAILABILITY_STATE_FILE = Path(os.getenv("AVAILABILITY_STATE_FILE", "/opt/hetzner-telegram-bot/.cost_optimized_state.json"))
BOT_VERSION = "14.4"
COST_TRACK_STATE_FILE = Path(os.getenv("COST_TRACK_STATE_FILE", "/opt/hetzner-telegram-bot/.cost_tracking.json"))
AUTO_CREATE_STATE_FILE = Path(os.getenv("AUTO_CREATE_STATE_FILE", "/opt/hetzner-telegram-bot/.cost_auto_create.json"))
AUTO_CREATE_CHECK_MINUTES = int(os.getenv("AUTO_CREATE_CHECK_MINUTES", "60") or 60)
# Hetzner reports traffic as raw bytes, while the Cloud Console
# presents its traffic quota in 1024-based "TB" units (20 TB = 20 * 1024^4 bytes).
# Using 10^12 here makes a 20 TB quota appear as ~21.99 TB.
HETZNER_TB_BYTES = 1024 ** 4
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
    return float(getattr(server, "outgoing_traffic", 0) or 0) / HETZNER_TB_BYTES


def included_tb(server) -> float | None:
    raw = getattr(server, "included_traffic", None)
    return float(raw) / HETZNER_TB_BYTES if raw else None


def traffic_line(server) -> str:
    used = traffic_tb(server)
    included = included_tb(server)
    if included:
        return f"ترافیک خروجی: <b>{used:.2f} TB</b> از <b>{included:.2f} TB</b>"
    return f"ترافیک خروجی: <b>{used:.2f} TB</b>"



def _money_to_float(value) -> float:
    """Convert hcloud Money/string/number values to EUR float."""
    try:
        if value is None:
            return 0.0
        amount = getattr(value, "amount", None)
        if amount is not None:
            return float(amount)
        if isinstance(value, dict):
            return float(value.get("amount", 0) or 0)
        return float(value)
    except Exception:
        return 0.0


def _money_to_float(value) -> float:
    try:
        if value is None:
            return 0.0
        amount = getattr(value, "amount", None)
        if amount is not None:
            return float(amount)
        if isinstance(value, dict):
            return float(value.get("amount", 0) or 0)
        return float(value)
    except Exception:
        return 0.0


def server_monthly_price_eur(server, server_types=None) -> float:
    """Get monthly price from server type pricing. Some hcloud versions do not attach prices to server objects."""
    try:
        st = getattr(server, "server_type", None)
        candidates = []
        if st:
            candidates.append(st)
        if server_types:
            sid = getattr(st, "id", None)
            name = getattr(st, "name", None)
            candidates.extend([
                x for x in server_types
                if (sid and getattr(x, "id", None) == sid) or (name and getattr(x, "name", None) == name)
            ])
        loc = server_location(server)
        for item in candidates:
            prices = getattr(item, "prices", None) or []
            for price in prices:
                location = getattr(getattr(price, "location", None), "name", "")
                if location == loc:
                    value = _money_to_float(getattr(price, "price_monthly", None))
                    if value:
                        return value
            if prices:
                value = _money_to_float(getattr(prices[0], "price_monthly", None))
                if value:
                    return value
    except Exception:
        pass
    return 0.0


def cost_tracking_load() -> dict:
    try:
        if COST_TRACK_STATE_FILE.exists():
            return json.loads(COST_TRACK_STATE_FILE.read_text())
    except Exception:
        pass
    return {}


def cost_tracking_save(data: dict) -> None:
    try:
        COST_TRACK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        COST_TRACK_STATE_FILE.write_text(json.dumps(data))
    except Exception:
        pass


def server_cost_line(server) -> str:
    price = server_monthly_price_eur(server)
    if price:
        return f"💰 ماهانه: <b>€{price:.2f}</b>"
    return ""


def server_spent_so_far(server, price: float) -> float:
    data = cost_tracking_load()
    key = str(getattr(server, "id", server.name))
    if key not in data:
        data[key] = {"first_seen": datetime.now(timezone.utc).isoformat()}
        cost_tracking_save(data)
    try:
        started = datetime.fromisoformat(data[key]["first_seen"])
        days = max(0, (datetime.now(timezone.utc)-started).total_seconds()/86400)
        return price * days / 30.0
    except Exception:
        return 0.0


async def cost_report_text() -> str:
    lines = ["📊 <b>گزارش هزینه ماهانه</b>", ""]
    total = 0.0
    for project in PROJECTS:
        try:
            servers = await asyncio.to_thread(project["client"].servers.get_all)
        except Exception:
            continue
        project_total = 0.0
        project_lines = []
        try:
            server_types = await asyncio.to_thread(project["client"].server_types.get_all)
        except Exception:
            server_types = []
        for server in servers:
            price = server_monthly_price_eur(server, server_types)
            if price:
                project_total += price
                total += price
                project_lines.append(f"🖥 {escape(server.name)}   €{price:.2f} | مصرف: €{server_spent_so_far(server, price):.2f}")
        if project_lines:
            lines.append(f"📁 <b>{escape(project['name'])}</b>")
            lines.extend(project_lines)
            lines.append(f"\nجمع پروژه: <b>€{project_total:.2f}</b>\n")
    if not total:
        lines.append("اطلاعات قیمت از API هتزنر دریافت نشد.")
    else:
        lines.append(f"💰 <b>کل همه پروژه‌ها: €{total:.2f}</b>")
    return "\n".join(lines)

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


async def compact_overview_text() -> str:
    async def one_project(project: dict) -> tuple[str, int, int, int]:
        client = project["client"]
        servers, fips, primary_ips = await asyncio.gather(
            asyncio.to_thread(client.servers.get_all),
            asyncio.to_thread(client.floating_ips.get_all),
            asyncio.to_thread(client.primary_ips.get_all),
        )
        free_primary_ipv4 = sum(
            1
            for ip in primary_ips
            if getattr(ip, "type", None) == "ipv4"
            and getattr(ip, "assignee_id", None) is None
        )
        return project["name"], len(servers), len(fips), free_primary_ipv4

    results = await asyncio.gather(
        *(one_project(project) for project in PROJECTS),
        return_exceptions=True,
    )

    lines = [f"🤖 <b>Hetzner</b>  |  📁 <b>{len(PROJECTS)} پروژه</b>"]
    for project, result in zip(PROJECTS, results):
        name = escape(project["name"])
        if isinstance(result, Exception):
            lines.append(f"• <b>{name}</b> — ⚠️ خطای آمار")
            continue

        _, server_count, fip_count, free_primary_count = result
        stats = []
        if server_count:
            stats.append(f"🖥 {server_count}")
        if fip_count:
            stats.append(f"🌐 {fip_count} FIP")
        if free_primary_count:
            stats.append(f"📦 {free_primary_count} IPv4 آزاد")

        suffix = f" — {' | '.join(stats)}" if stats else ""
        lines.append(f"• <b>{name}</b>{suffix}")

    return "\n".join(lines)


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📁 انتخاب پروژه", callback_data="projects")],
            [InlineKeyboardButton("💸 Cost-Optimized", callback_data="cheap:show")],
            [InlineKeyboardButton("📊 ترافیک همه پروژه‌ها", callback_data="traffic:all")],
            [InlineKeyboardButton("💰 گزارش هزینه ماهانه", callback_data="cost:report")],
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
        f"{server_cost_line(server)}\n"
        f"💳 مصرف تا الان: <b>€{server_spent_so_far(server, server_monthly_price_eur(server)):.2f}</b>\n"
        f"{traffic_line(server)}"
    )


def server_keyboard(pidx: int, server) -> InlineKeyboardMarkup:
    sid = server.id
    has_ipv4 = bool(server_ipv4(server))
    primary_rows = [
        [
            InlineKeyboardButton(
                "🔁 تعویض Primary IPv4" if has_ipv4 else "➕ افزودن Primary IPv4",
                callback_data=f"srv:swappip:{pidx}:{sid}",
            )
        ]
    ]
    if has_ipv4:
        primary_rows.append(
            [InlineKeyboardButton("🗑 حذف Primary IPv4", callback_data=f"srv:askpip:{pidx}:{sid}")]
        )
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🟢 روشن", callback_data=f"srv:on:{pidx}:{sid}"),
                InlineKeyboardButton("🔴 خاموش", callback_data=f"srv:off:{pidx}:{sid}"),
            ],
            [InlineKeyboardButton("⚙️ تغییر سایز", callback_data=f"srv:resize:{pidx}:{sid}")],
            *primary_rows,
            [InlineKeyboardButton("📦 Primary IPv4های پروژه", callback_data=f"pips:{pidx}")],
            [InlineKeyboardButton("🌐 Floating IPهای سرور", callback_data=f"srv:fips:{pidx}:{sid}")],
            [InlineKeyboardButton("🗑 حذف سرور", callback_data=f"srv:askdelete:{pidx}:{sid}")],
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
    await safe_edit(query, await compact_overview_text(), projects_keyboard())


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



def is_cost_optimized_server_type(server_type) -> bool:
    """Match Hetzner's cost-optimized families without relying on a single category string."""
    name = str(getattr(server_type, "name", "") or "").lower()
    category = str(getattr(server_type, "category", "") or "").lower().replace("_", "-")
    if "cost" in category and ("optim" in category or "optimized" in category):
        return True
    # Current Cost-Optimized families are CX (x86) and CAX (Arm64).
    return bool(re.match(r"^(cx|cax)\d+$", name))


def server_type_location_entry(server_type, location_name: str):
    for entry in getattr(server_type, "locations", None) or []:
        loc = getattr(entry, "location", None)
        if getattr(loc, "name", None) == location_name:
            return entry
    return None


def available_server_types_in_location(server_types: list, location_name: str) -> list:
    available = []
    for st in server_types:
        entry = server_type_location_entry(st, location_name)
        if not entry or getattr(entry, "available", None) is not True:
            continue
        if getattr(entry, "deprecation", None) is not None:
            continue
        available.append(st)
    return sorted(
        available,
        key=lambda st: (
            0 if is_cost_optimized_server_type(st) else 1,
            getattr(st, "memory", 0) or 0,
            getattr(st, "cores", 0) or 0,
            str(getattr(st, "name", "")),
        ),
    )


def cost_optimized_matrix(server_types: list) -> dict[str, list]:
    matrix: dict[str, list] = {}
    for st in server_types:
        if not is_cost_optimized_server_type(st):
            continue
        for entry in getattr(st, "locations", None) or []:
            if getattr(entry, "available", None) is not True:
                continue
            if getattr(entry, "deprecation", None) is not None:
                continue
            loc = getattr(entry, "location", None)
            loc_name = getattr(loc, "name", None)
            if not loc_name:
                continue
            matrix.setdefault(loc_name, []).append(st)
    for loc_name in matrix:
        matrix[loc_name].sort(key=lambda st: (getattr(st, "memory", 0) or 0, getattr(st, "cores", 0) or 0, st.name))
    return dict(sorted(matrix.items()))


def cost_optimized_text(matrix: dict[str, list], *, title: str = "💸 موجودی Cost-Optimized") -> str:
    lines = [f"<b>{title}</b>", ""]
    if not matrix:
        lines.extend(
            [
                "در حال حاضر هیچ پلن Cost-Optimized قابل سفارشی از طریق API گزارش نشده است.",
                "",
                f"ربات هر <b>{CHEAP_CHECK_HOURS:g} ساعت</b> دوباره بررسی می‌کند.",
            ]
        )
        return "\n".join(lines)
    for loc_name, plans in matrix.items():
        lines.append(f"📍 <b>{escape(loc_name)}</b>")
        for st in plans:
            arch = "ARM" if getattr(st, "architecture", "") == "arm" else "x86"
            lines.append(
                f"• <code>{escape(st.name)}</code> — {st.cores} vCPU / {st.memory:g} GB / {st.disk} GB — {arch}"
            )
        lines.append("")
    lines.extend(
        [
            "⚠️ موجودی API یک شاخص لحظه‌ای است و Hetzner تضمین نمی‌کند مرحله نهایی Allocation همیشه موفق شود.",
            f"🔄 بررسی خودکار: هر <b>{CHEAP_CHECK_HOURS:g} ساعت</b>",
        ]
    )
    return "\n".join(lines)


async def fetch_cost_optimized_matrix() -> tuple[dict, dict[str, list]]:
    last_exc = None
    for project in PROJECTS:
        try:
            server_types = await asyncio.to_thread(project["client"].server_types.get_all)
            return project, cost_optimized_matrix(server_types)
        except Exception as exc:
            last_exc = exc
            log.exception("Cost-Optimized availability check failed for project %s", project["name"])
    raise RuntimeError(f"هیچ توکن فعالی برای بررسی موجودی پاسخ نداد: {last_exc}")


def cost_monitor_enabled() -> bool:
    state = read_availability_state()
    return bool(state.get("enabled", True))


def set_cost_monitor_enabled(enabled: bool) -> None:
    state = read_availability_state()
    state["enabled"] = bool(enabled)
    state["updated_at"] = datetime.now(ZoneInfo(BOT_TIMEZONE)).isoformat()
    write_availability_state(state)


async def show_cost_optimized(query, notice: str | None = None) -> None:
    _, matrix = await fetch_cost_optimized_matrix()
    enabled = cost_monitor_enabled()
    text = cost_optimized_text(matrix)
    status = "🟢 روشن" if enabled else "🔴 خاموش"
    text += f"\n\n🔔 مانیتور: <b>{status}</b> — هر <b>{CHEAP_CHECK_HOURS:g} ساعت</b>"
    auto = read_auto_create_state()
    text += "\n🤖 ساخت خودکار: " + ("فعال" if auto.get("request") and not auto.get("created") else "غیرفعال")
    if notice:
        text = f"{escape(notice)}\n\n{text}"

    toggle_text = "🔕 خاموش کردن مانیتور" if enabled else "🔔 روشن کردن مانیتور"
    await safe_edit(
        query,
        text,
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔄 بررسی دوباره", callback_data="cheap:show"),
                    InlineKeyboardButton(toggle_text, callback_data="cheap:toggle"),
                ],
                [InlineKeyboardButton("🤖 ساخت خودکار هنگام موجود شدن", callback_data="cheap:auto")],
                [InlineKeyboardButton("📁 انتخاب پروژه برای ساخت", callback_data="projects")],
                [InlineKeyboardButton("⬅️ منوی اصلی", callback_data="main")],
            ]
        ),
    )


def valid_server_name(name: str) -> bool:
    if not 1 <= len(name) <= 63:
        return False
    return bool(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", name))


def latest_ubuntu_image(images: list):
    """Return Ubuntu 24.04 LTS as the default image for new servers."""
    for image in images:
        name = str(getattr(image, "name", "") or "")
        if name != "ubuntu-24.04":
            continue
        if getattr(image, "status", "available") not in (None, "available"):
            continue
        return image
    return None


def server_create_summary(project: dict, pending: dict, location, server_type, image, ipv6: bool) -> str:
    cost_tag = "💸 Cost-Optimized" if is_cost_optimized_server_type(server_type) else "☁️ Cloud"
    if pending.get("auth_mode") == "ssh":
        auth_text = f"🔑 SSH Key — <code>{escape(str(pending.get('ssh_key_name') or 'Selected key'))}</code>"
    else:
        auth_text = "🔐 رمز root خودکار Hetzner"
    return (
        f"➕ <b>تأیید ساخت سرور — {escape(project['name'])}</b>\n\n"
        f"نام: <code>{escape(pending['name'])}</code>\n"
        f"Location: <code>{escape(location.name)}</code> — {escape(str(getattr(location, 'city', '') or ''))}\n"
        f"پلن: <code>{escape(server_type.name)}</code> — {server_type.cores} vCPU / {server_type.memory:g} GB RAM / {server_type.disk} GB\n"
        f"نوع: {cost_tag}\n"
        f"معماری: <code>{escape(str(server_type.architecture))}</code>\n"
        f"سیستم‌عامل: <code>{escape(image.name)}</code>\n"
        f"IPv4: ✅ فعال\n"
        f"IPv6: {'✅ فعال' if ipv6 else '❌ غیرفعال'}\n"
        f"ورود: {auth_text}\n\n"
        "با تأیید، سرور ساخته و روشن می‌شود."
    )


async def show_create_auth_method(query, context: ContextTypes.DEFAULT_TYPE, pidx: int) -> None:
    pending = context.user_data.get("server_create", {})
    if int(pending.get("project", -1)) != pidx or not pending.get("server_type_id"):
        await query.answer("فرآیند ساخت منقضی شده؛ دوباره شروع کنید.", show_alert=True)
        return
    await safe_edit(
        query,
        "➕ <b>ساخت سرور</b>\n\n"
        f"نام: <code>{escape(pending['name'])}</code>\n"
        f"IPv6: {'✅ فعال' if pending.get('ipv6') else '❌ غیرفعال'}\n\n"
        "روش ورود اولیه به سرور را انتخاب کنید:",
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔑 SSH Key", callback_data=f"newsv:authkey:{pidx}"),
                    InlineKeyboardButton("🔐 رمز root", callback_data=f"newsv:authpass:{pidx}"),
                ],
                [InlineKeyboardButton("⬅️ تغییر IPv6", callback_data=f"newsv:plan:{pidx}:{pending['server_type_id']}")],
                [InlineKeyboardButton("❌ انصراف", callback_data=f"project:{pidx}")],
            ]
        ),
    )


async def show_create_ssh_keys(query, context: ContextTypes.DEFAULT_TYPE, pidx: int) -> None:
    project = get_project(pidx)
    pending = context.user_data.get("server_create", {})
    if not project or int(pending.get("project", -1)) != pidx:
        await query.answer("فرآیند ساخت منقضی شده؛ دوباره شروع کنید.", show_alert=True)
        return
    keys = await asyncio.to_thread(project["client"].ssh_keys.get_all)
    if not keys:
        await safe_edit(
            query,
            "🔑 <b>SSH Key</b>\n\n"
            "در این پروژه هیچ SSH Key ثبت نشده است.\n"
            "می‌توانید سرور را با رمز root بسازید.",
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔐 ساخت با رمز root", callback_data=f"newsv:authpass:{pidx}")],
                    [InlineKeyboardButton("⬅️ برگشت", callback_data=f"newsv:auth:{pidx}")],
                ]
            ),
        )
        return
    rows = []
    pair = []
    for key in keys:
        name = str(getattr(key, "name", "") or f"Key {key.id}")
        pair.append(InlineKeyboardButton(f"🔑 {name[:28]}", callback_data=f"newsv:key:{pidx}:{key.id}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([InlineKeyboardButton("⬅️ روش ورود", callback_data=f"newsv:auth:{pidx}")])
    await safe_edit(
        query,
        f"🔑 <b>SSH Key — {escape(project['name'])}</b>\n\nکلید موردنظر را انتخاب کنید:",
        InlineKeyboardMarkup(rows),
    )


async def show_create_confirmation(query, context: ContextTypes.DEFAULT_TYPE, pidx: int) -> None:
    project = get_project(pidx)
    pending = context.user_data.get("server_create", {})
    if not project or int(pending.get("project", -1)) != pidx:
        await query.answer("فرآیند ساخت منقضی شده؛ دوباره شروع کنید.", show_alert=True)
        return
    client = project["client"]
    st = await asyncio.to_thread(client.server_types.get_by_id, int(pending["server_type_id"]))
    location = await asyncio.to_thread(client.locations.get_by_id, int(pending["location_id"]))
    if not st or not location:
        await query.answer("پلن یا Location پیدا نشد.", show_alert=True)
        return
    images = await asyncio.to_thread(
        client.images.get_all,
        type=["system"],
        architecture=[st.architecture],
        include_deprecated=False,
    )
    image = latest_ubuntu_image(images)
    if not image:
        await query.answer("Ubuntu 24.04 LTS سازگار با این معماری پیدا نشد.", show_alert=True)
        return
    pending["image_id"] = image.id
    context.user_data["server_create"] = pending
    await safe_edit(
        query,
        server_create_summary(project, pending, location, st, image, bool(pending.get("ipv6", True))),
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ ساخت سرور", callback_data=f"newsv:create:{pidx}")],
                [InlineKeyboardButton("⬅️ تغییر روش ورود", callback_data=f"newsv:auth:{pidx}")],
                [InlineKeyboardButton("❌ انصراف", callback_data=f"project:{pidx}")],
            ]
        ),
    )


async def show_create_locations(query, context: ContextTypes.DEFAULT_TYPE, pidx: int) -> None:
    project = get_project(pidx)
    if not project:
        await query.answer("پروژه پیدا نشد.", show_alert=True)
        return
    locations = await asyncio.to_thread(project["client"].locations.get_all)
    pending = context.user_data.get("server_create", {})
    if not pending.get("name"):
        await query.answer("فرآیند ساخت منقضی شده؛ دوباره شروع کنید.", show_alert=True)
        return
    rows = [
        [InlineKeyboardButton(f"📍 {loc.name} — {loc.city}", callback_data=f"newsv:loc:{pidx}:{loc.id}")]
        for loc in locations
    ]
    rows.append([InlineKeyboardButton("⬅️ انصراف", callback_data=f"project:{pidx}")])
    await safe_edit(
        query,
        f"➕ <b>ساخت سرور</b>\n\nنام: <code>{escape(pending['name'])}</code>\n\nLocation را انتخاب کنید:",
        InlineKeyboardMarkup(rows),
    )


async def show_create_plans(query, context: ContextTypes.DEFAULT_TYPE, pidx: int, location_id: int) -> None:
    project = get_project(pidx)
    if not project:
        await query.answer("پروژه پیدا نشد.", show_alert=True)
        return
    client = project["client"]
    location, server_types = await asyncio.gather(
        asyncio.to_thread(client.locations.get_by_id, location_id),
        asyncio.to_thread(client.server_types.get_all),
    )
    if not location:
        await query.answer("Location پیدا نشد.", show_alert=True)
        return
    plans = available_server_types_in_location(server_types, location.name)
    pending = context.user_data.get("server_create", {})
    pending.update({"project": pidx, "location_id": location_id, "location_name": location.name})
    context.user_data["server_create"] = pending
    if not plans:
        await safe_edit(
            query,
            f"📍 <b>{escape(location.name)}</b>\n\n❌ در حال حاضر پلن قابل سفارشی برای این Location گزارش نشده است.",
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔄 دوباره بررسی کن", callback_data=f"newsv:loc:{pidx}:{location_id}")],
                    [InlineKeyboardButton("⬅️ Locationها", callback_data=f"newsv:locations:{pidx}")],
                ]
            ),
        )
        return
    rows = []
    buttons = []
    for st in plans:
        marker = "💸" if is_cost_optimized_server_type(st) else "☁️"
        buttons.append(
            InlineKeyboardButton(
                f"{marker} {st.name} | {st.cores}C/{st.memory:g}G",
                callback_data=f"newsv:plan:{pidx}:{st.id}",
            )
        )
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i:i+2])
    rows.append([InlineKeyboardButton("⬅️ Locationها", callback_data=f"newsv:locations:{pidx}")])
    await safe_edit(
        query,
        f"📍 <b>{escape(location.name)}</b>\n\nپلن را انتخاب کنید. فقط پلن‌هایی که Hetzner همین حالا Available گزارش می‌کند نمایش داده شده‌اند:\n\n💸 = Cost-Optimized",
        InlineKeyboardMarkup(rows),
    )

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
            [InlineKeyboardButton("➕ ساخت سرور", callback_data=f"newsv:start:{pidx}")],
            [
                InlineKeyboardButton("📦 Primary IPv4", callback_data=f"pips:{pidx}"),
                InlineKeyboardButton("🌐 Floating IP", callback_data=f"fips:{pidx}"),
            ],
            [InlineKeyboardButton("📊 ترافیک", callback_data=f"traffic:{pidx}")],
            [InlineKeyboardButton("💰 هزینه ماهانه", callback_data="cost:report")],
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



def primary_ip_location(primary_ip) -> str:
    location = getattr(primary_ip, "location", None)
    return getattr(location, "name", None) or "نامشخص"


def primary_ipv4_for_server(primary_ips: list, server_id: int):
    return next(
        (
            ip
            for ip in primary_ips
            if getattr(ip, "type", None) == "ipv4"
            and getattr(ip, "assignee_id", None) == server_id
        ),
        None,
    )


def primary_ip_name(primary_ip) -> str:
    return getattr(primary_ip, "name", None) or f"Primary IP #{primary_ip.id}"


def primary_ip_list_text(project: dict, primary_ips: list, servers: list) -> str:
    ipv4s = [ip for ip in primary_ips if getattr(ip, "type", None) == "ipv4"]
    server_names = {s.id: s.name for s in servers}
    free_count = sum(1 for ip in ipv4s if getattr(ip, "assignee_id", None) is None)
    lines = [
        f"📦 <b>Primary IPv4 — {escape(project['name'])}</b>",
        f"تعداد: <b>{len(ipv4s)}</b>   |   آزاد: <b>{free_count}</b>",
        "",
    ]
    if not ipv4s:
        lines.append("هیچ Primary IPv4 در این پروژه وجود ندارد.")
    else:
        for idx, ip in enumerate(ipv4s, 1):
            assignee_id = getattr(ip, "assignee_id", None)
            if assignee_id is None:
                state = "🟢 آزاد"
            else:
                state = f"🖥 {escape(server_names.get(assignee_id, f'Server #{assignee_id}'))}"
            lines.append(
                f"{idx}. <code>{escape(str(ip.ip))}</code> — {state} — "
                f"<code>{escape(primary_ip_location(ip))}</code>"
            )
    lines.extend(
        [
            "",
            "IPهایی که بعد از تعویض از سرور جدا می‌شوند، اینجا به حالت «آزاد» باقی می‌مانند و می‌توانید حذفشان کنید.",
        ]
    )
    return "\n".join(lines)


def primary_ip_list_keyboard(pidx: int, primary_ips: list) -> InlineKeyboardMarkup:
    ipv4s = [ip for ip in primary_ips if getattr(ip, "type", None) == "ipv4"]
    rows = []
    for ip in ipv4s[:60]:
        marker = "🟢" if getattr(ip, "assignee_id", None) is None else "🔗"
        rows.append(
            [InlineKeyboardButton(f"{marker} {ip.ip}", callback_data=f"pip:open:{pidx}:{ip.id}")]
        )
    rows.extend(
        [
            [InlineKeyboardButton("🔄 بروزرسانی", callback_data=f"pips:{pidx}")],
            [InlineKeyboardButton("⬅️ برگشت به پروژه", callback_data=f"project:{pidx}")],
        ]
    )
    return InlineKeyboardMarkup(rows)


async def show_primary_ip_list(query, pidx: int) -> None:
    project = get_project(pidx)
    if not project:
        await query.answer("پروژه پیدا نشد.", show_alert=True)
        return
    client = project["client"]
    primary_ips, servers = await asyncio.gather(
        asyncio.to_thread(client.primary_ips.get_all),
        asyncio.to_thread(client.servers.get_all),
    )
    await safe_edit(
        query,
        primary_ip_list_text(project, primary_ips, servers),
        primary_ip_list_keyboard(pidx, primary_ips),
    )


async def show_primary_ip_detail(query, pidx: int, primary_ip_id: int) -> None:
    project = get_project(pidx)
    if not project:
        await query.answer("پروژه پیدا نشد.", show_alert=True)
        return
    client = project["client"]
    ip = await asyncio.to_thread(client.primary_ips.get_by_id, primary_ip_id)
    if not ip or getattr(ip, "type", None) != "ipv4":
        await query.answer("Primary IPv4 پیدا نشد.", show_alert=True)
        return
    assignee_id = getattr(ip, "assignee_id", None)
    server_name = "آزاد / متصل نیست"
    if assignee_id is not None:
        server = await asyncio.to_thread(client.servers.get_by_id, assignee_id)
        if server:
            server_name = server.name
    text = (
        f"📦 <b>{escape(primary_ip_name(ip))}</b>\n"
        f"📁 پروژه: <b>{escape(project['name'])}</b>\n\n"
        f"IPv4: <code>{escape(str(ip.ip))}</code>\n"
        f"Location: <code>{escape(primary_ip_location(ip))}</code>\n"
        f"وضعیت: <b>{'آزاد' if assignee_id is None else 'متصل'}</b>\n"
        f"سرور: <b>{escape(server_name)}</b>"
    )
    rows = []
    if assignee_id is None:
        rows.append(
            [InlineKeyboardButton("🗑 حذف این IPv4 آزاد", callback_data=f"pip:askdel:{pidx}:{primary_ip_id}")]
        )
    else:
        rows.append(
            [InlineKeyboardButton("🖥 مدیریت سرور", callback_data=f"srv:open:{pidx}:{assignee_id}")]
        )
    rows.extend(
        [
            [InlineKeyboardButton("🔄 بروزرسانی", callback_data=f"pip:open:{pidx}:{primary_ip_id}")],
            [InlineKeyboardButton("⬅️ Primary IPv4ها", callback_data=f"pips:{pidx}")],
        ]
    )
    await safe_edit(query, text, InlineKeyboardMarkup(rows))


async def show_primary_swap_choices(query, pidx: int, server_id: int) -> None:
    project = get_project(pidx)
    if not project:
        await query.answer("پروژه پیدا نشد.", show_alert=True)
        return
    client = project["client"]
    server, primary_ips = await asyncio.gather(
        asyncio.to_thread(client.servers.get_by_id, server_id),
        asyncio.to_thread(client.primary_ips.get_all),
    )
    if not server:
        await query.answer("سرور پیدا نشد.", show_alert=True)
        return
    current = primary_ipv4_for_server(primary_ips, server_id)
    location_name = server_location(server)
    free_ips = [
        ip
        for ip in primary_ips
        if getattr(ip, "type", None) == "ipv4"
        and getattr(ip, "assignee_id", None) is None
        and primary_ip_location(ip) == location_name
    ]
    current_line = (
        f"<code>{escape(str(current.ip))}</code>"
        if current
        else "<b>ندارد</b>"
    )
    text = (
        f"🔁 <b>{'تعویض' if current else 'افزودن'} Primary IPv4</b>\n\n"
        f"سرور: <b>{escape(server.name)}</b>\n"
        f"IP فعلی: {current_line}\n"
        f"Location: <code>{escape(location_name)}</code>\n\n"
        "روند کاملاً خودکار است:\n"
        "1️⃣ خاموش کردن امن سرور و انتظار تا Off\n"
        "2️⃣ جدا کردن IP فعلی (اگر وجود داشته باشد)\n"
        "3️⃣ اتصال IPv4 جدید\n"
        "4️⃣ روشن کردن دوباره سرور\n\n"
        "IP قبلی حذف نمی‌شود و در بخش Primary IPv4های پروژه به‌صورت آزاد باقی می‌ماند."
    )
    rows = []
    for ip in free_ips[:35]:
        name = primary_ip_name(ip)
        rows.append(
            [
                InlineKeyboardButton(
                    f"🔁 {ip.ip} | {name}"[:60],
                    callback_data=f"pip:swap:{pidx}:{server_id}:{ip.id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "✨ ساخت IPv4 جدید و تعویض خودکار",
                callback_data=f"pip:newswap:{pidx}:{server_id}",
            )
        ]
    )
    rows.extend(
        [
            [InlineKeyboardButton("📦 مشاهده IPv4های پروژه", callback_data=f"pips:{pidx}")],
            [InlineKeyboardButton("⬅️ برگشت به سرور", callback_data=f"srv:open:{pidx}:{server_id}")],
        ]
    )
    if free_ips:
        text += f"\n\nIPv4 آزاد سازگار: <b>{len(free_ips)}</b>"
    else:
        text += "\n\nIPv4 آزاد سازگار وجود ندارد؛ می‌توانید یک IPv4 جدید بسازید."
    await safe_edit(query, text, InlineKeyboardMarkup(rows))


async def wait_server_status(client, server_id: int, wanted: str, timeout: int = 300):
    deadline = asyncio.get_running_loop().time() + timeout
    last = None
    while asyncio.get_running_loop().time() < deadline:
        last = await asyncio.to_thread(client.servers.get_by_id, server_id)
        if last and getattr(last, "status", None) == wanted:
            return last
        await asyncio.sleep(3)
    return last


def swap_progress_text(server_name: str, old_ip: str | None, new_ip: str | None, steps: list[str]) -> str:
    return (
        "🔁 <b>تعویض خودکار Primary IPv4</b>\n\n"
        f"سرور: <b>{escape(server_name)}</b>\n"
        f"IP قبلی: <code>{escape(old_ip or 'ندارد')}</code>\n"
        f"IP جدید: <code>{escape(new_ip or 'در حال ساخت')}</code>\n\n"
        + "\n".join(steps)
        + "\n\nلطفاً تا پایان عملیات دکمه دیگری نزنید."
    )


async def perform_primary_ipv4_swap(query, pidx: int, server_id: int, target_ip_id: int | None) -> None:
    project = get_project(pidx)
    if not project:
        await query.answer("پروژه پیدا نشد.", show_alert=True)
        return
    client = project["client"]
    server, primary_ips = await asyncio.gather(
        asyncio.to_thread(client.servers.get_by_id, server_id),
        asyncio.to_thread(client.primary_ips.get_all),
    )
    if not server:
        await query.answer("سرور پیدا نشد.", show_alert=True)
        return

    old_ip = primary_ipv4_for_server(primary_ips, server_id)
    old_value = str(old_ip.ip) if old_ip else None
    location_name = server_location(server)
    new_ip = None
    created_new = False

    if target_ip_id is not None:
        new_ip = await asyncio.to_thread(client.primary_ips.get_by_id, target_ip_id)
        if not new_ip or getattr(new_ip, "type", None) != "ipv4":
            await query.answer("IPv4 مقصد پیدا نشد.", show_alert=True)
            return
        if getattr(new_ip, "assignee_id", None) is not None:
            await query.answer("این IPv4 دیگر آزاد نیست.", show_alert=True)
            return
        if primary_ip_location(new_ip) != location_name:
            await query.answer("Location این IPv4 با سرور یکسان نیست.", show_alert=True)
            return
        if old_ip and new_ip.id == old_ip.id:
            await query.answer("این همان IPv4 فعلی سرور است.", show_alert=True)
            return

    await query.answer("تعویض خودکار شروع شد.")
    steps = [
        "⏳ 1/4 خاموش کردن سرور...",
        "▫️ 2/4 جدا کردن IP قبلی",
        "▫️ 3/4 اتصال IP جدید",
        "▫️ 4/4 روشن کردن سرور",
    ]
    await safe_edit(query, swap_progress_text(server.name, old_value, str(new_ip.ip) if new_ip else None, steps))

    # Step 1: graceful shutdown and wait until Hetzner reports the server as off.
    try:
        fresh_server = await asyncio.to_thread(client.servers.get_by_id, server_id)
        if getattr(fresh_server, "status", None) != "off":
            if getattr(fresh_server, "status", None) != "stopping":
                await asyncio.to_thread(fresh_server.shutdown)
            fresh_server = await wait_server_status(client, server_id, "off", timeout=300)
        if not fresh_server or getattr(fresh_server, "status", None) != "off":
            steps[0] = "❌ 1/4 سرور در 5 دقیقه خاموش نشد"
            await safe_edit(
                query,
                swap_progress_text(server.name, old_value, str(new_ip.ip) if new_ip else None, steps)
                + "\n\nتعویض متوقف شد و هیچ IPای تغییر نکرد.",
                InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ برگشت به سرور", callback_data=f"srv:open:{pidx}:{server_id}")]]),
            )
            return
    except Exception as exc:
        log.exception("Primary IPv4 swap shutdown failed")
        steps[0] = "❌ 1/4 خطا در خاموش کردن سرور"
        await safe_edit(
            query,
            swap_progress_text(server.name, old_value, str(new_ip.ip) if new_ip else None, steps)
            + f"\n\n<code>{escape(str(exc))}</code>",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ برگشت به سرور", callback_data=f"srv:open:{pidx}:{server_id}")]]),
        )
        return

    steps[0] = "✅ 1/4 سرور کاملاً خاموش شد"
    await safe_edit(query, swap_progress_text(server.name, old_value, str(new_ip.ip) if new_ip else None, steps))

    # If requested, create a new unassigned IPv4 in the server's Location after shutdown.
    if new_ip is None:
        try:
            generated_name = f"swap-{server.id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            response = await asyncio.to_thread(
                client.primary_ips.create,
                "ipv4",
                generated_name,
                location=server.location,
                auto_delete=False,
            )
            new_ip = response.primary_ip
            created_new = True
            steps[2] = f"⏳ 3/4 IPv4 جدید ساخته شد: {new_ip.ip} — در حال اتصال..."
            await safe_edit(query, swap_progress_text(server.name, old_value, str(new_ip.ip), steps))
        except Exception as exc:
            log.exception("Primary IPv4 create for swap failed")
            # Nothing has been unassigned yet, so simply power the server back on.
            try:
                fresh_server = await asyncio.to_thread(client.servers.get_by_id, server_id)
                action = await asyncio.to_thread(fresh_server.power_on)
                await asyncio.to_thread(action.wait_until_finished)
            except Exception:
                log.exception("Could not power server back on after primary IP create failure")
            await safe_edit(
                query,
                swap_progress_text(server.name, old_value, None, steps)
                + f"\n\n❌ ساخت IPv4 جدید ناموفق بود. IP قبلی دست‌نخورده باقی ماند.\n<code>{escape(str(exc))}</code>",
                InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ برگشت به سرور", callback_data=f"srv:open:{pidx}:{server_id}")]]),
            )
            return

    old_unassigned = False
    try:
        # Re-fetch resources right before changing assignment.
        if old_ip:
            old_ip = await asyncio.to_thread(client.primary_ips.get_by_id, old_ip.id)
            action = await asyncio.to_thread(old_ip.unassign)
            await asyncio.to_thread(action.wait_until_finished)
            old_unassigned = True
            steps[1] = f"✅ 2/4 IP قبلی جدا شد: {old_value}"
        else:
            steps[1] = "✅ 2/4 IP قبلی وجود نداشت"
        await safe_edit(query, swap_progress_text(server.name, old_value, str(new_ip.ip), steps))

        new_ip = await asyncio.to_thread(client.primary_ips.get_by_id, new_ip.id)
        if getattr(new_ip, "assignee_id", None) is not None:
            raise RuntimeError("IPv4 جدید دیگر آزاد نیست")
        action = await asyncio.to_thread(new_ip.assign, assignee_id=server_id, assignee_type="server")
        await asyncio.to_thread(action.wait_until_finished)
        steps[2] = f"✅ 3/4 IP جدید متصل شد: {new_ip.ip}"
        await safe_edit(query, swap_progress_text(server.name, old_value, str(new_ip.ip), steps))
    except Exception as exc:
        log.exception("Primary IPv4 swap assignment failed")
        rollback_ok = not old_unassigned
        rollback_note = ""
        if old_ip and old_unassigned:
            try:
                old_ip = await asyncio.to_thread(client.primary_ips.get_by_id, old_ip.id)
                action = await asyncio.to_thread(old_ip.assign, assignee_id=server_id, assignee_type="server")
                await asyncio.to_thread(action.wait_until_finished)
                rollback_ok = True
                rollback_note = "\n✅ IP قبلی دوباره به سرور متصل شد."
            except Exception as rollback_exc:
                rollback_note = f"\n🛑 بازگردانی IP قبلی هم ناموفق بود: <code>{escape(str(rollback_exc))}</code>"
                log.exception("Primary IPv4 rollback failed")
        if created_new and new_ip:
            try:
                candidate = await asyncio.to_thread(client.primary_ips.get_by_id, new_ip.id)
                if candidate and getattr(candidate, "assignee_id", None) is None:
                    await asyncio.to_thread(candidate.delete)
            except Exception:
                log.exception("Could not clean up newly created Primary IPv4")
        try:
            fresh_server = await asyncio.to_thread(client.servers.get_by_id, server_id)
            action = await asyncio.to_thread(fresh_server.power_on)
            await asyncio.to_thread(action.wait_until_finished)
        except Exception:
            log.exception("Could not power server on after swap rollback")
        await safe_edit(
            query,
            swap_progress_text(server.name, old_value, str(new_ip.ip) if new_ip else None, steps)
            + f"\n\n❌ تعویض IP ناموفق بود.\n<code>{escape(str(exc))}</code>"
            + rollback_note
            + ("" if rollback_ok else "\n🛑 وضعیت IP را فوراً در پنل Hetzner بررسی کنید."),
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("📦 Primary IPv4ها", callback_data=f"pips:{pidx}")],
                    [InlineKeyboardButton("⬅️ برگشت به سرور", callback_data=f"srv:open:{pidx}:{server_id}")],
                ]
            ),
        )
        return

    # Step 4: power the server on. The IP replacement is already complete at this point.
    try:
        fresh_server = await asyncio.to_thread(client.servers.get_by_id, server_id)
        action = await asyncio.to_thread(fresh_server.power_on)
        await asyncio.to_thread(action.wait_until_finished)
        running = await wait_server_status(client, server_id, "running", timeout=180)
        if running and getattr(running, "status", None) == "running":
            steps[3] = "✅ 4/4 سرور دوباره روشن شد"
        else:
            steps[3] = "⚠️ 4/4 دستور روشن شدن ارسال شد؛ وضعیت هنوز Running نشده"
    except Exception as exc:
        log.exception("Power on after Primary IPv4 swap failed")
        steps[3] = "❌ 4/4 IP تعویض شد اما روشن کردن سرور خطا داد"
        await safe_edit(
            query,
            swap_progress_text(server.name, old_value, str(new_ip.ip), steps)
            + f"\n\nIP جدید متصل است؛ سرور را دستی روشن کنید.\n<code>{escape(str(exc))}</code>",
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🟢 روشن کردن سرور", callback_data=f"srv:on:{pidx}:{server_id}")],
                    [InlineKeyboardButton("📦 Primary IPv4ها", callback_data=f"pips:{pidx}")],
                    [InlineKeyboardButton("⬅️ برگشت به سرور", callback_data=f"srv:open:{pidx}:{server_id}")],
                ]
            ),
        )
        return

    final_text = swap_progress_text(server.name, old_value, str(new_ip.ip), steps)
    final_text += "\n\n✅ <b>تعویض Primary IPv4 با موفقیت انجام شد.</b>"
    if old_value:
        final_text += f"\n📦 IP قبلی <code>{escape(old_value)}</code> حذف نشده و اکنون در لیست Primary IPv4های پروژه آزاد است."
    await safe_edit(
        query,
        final_text,
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🖥 برگشت به سرور", callback_data=f"srv:open:{pidx}:{server_id}")],
                [InlineKeyboardButton("📦 مدیریت IPv4های موجود", callback_data=f"pips:{pidx}")],
            ]
        ),
    )


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


def server_type_availability(server_type, location_name: str) -> bool | None:
    """True/False when Hetzner reports availability, otherwise None."""
    info = server_type_location_entry(server_type, location_name)
    if info is None:
        return None
    value = getattr(info, "available", None)
    return value if isinstance(value, bool) else None


def available_server_types(client, server) -> list:
    """Only server types currently available for migration in the server Location."""
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

        # Once a disk has been enlarged, Hetzner does not allow shrinking it again.
        if int(getattr(st, "disk", 0) or 0) < current_disk:
            continue

        loc_info = server_type_location_entry(st, location_name)
        if loc_info is None:
            continue
        if getattr(loc_info, "deprecation", None) is not None:
            continue

        # Important: only show types Hetzner currently reports as available
        # for this exact Location. None/unknown is intentionally hidden.
        if getattr(loc_info, "available", None) is not True:
            continue

        result.append(st)

    return sorted(
        result,
        key=lambda x: (
            getattr(x, "memory", 0),
            getattr(x, "cores", 0),
            getattr(x, "disk", 0),
            getattr(x, "name", ""),
        ),
    )


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

    current = getattr(server, "server_type", None)
    location_name = server_location(server)
    fresh_current = None
    if current and getattr(current, "id", None):
        fresh_current = await asyncio.to_thread(client.server_types.get_by_id, current.id)
    current_for_status = fresh_current or current
    current_available = server_type_availability(current_for_status, location_name)
    if current_available is True:
        current_status = "🟢 موجود"
    elif current_available is False:
        current_status = "🔴 فعلاً ناموجود"
    else:
        current_status = "⚪ وضعیت نامشخص"

    types = await asyncio.to_thread(available_server_types, client, server)
    rows = []
    for st in types[:35]:
        label = f"{st.name} | {st.cores}C / {st.memory:g}GB / {st.disk}GB"
        rows.append([InlineKeyboardButton(label, callback_data=f"rzt:pick:{pidx}:{server_id}:{st.id}")])

    rows.append([InlineKeyboardButton("🔄 بروزرسانی موجودی", callback_data=f"srv:resize:{pidx}:{server_id}")])
    rows.append([InlineKeyboardButton("⬅️ برگشت به سرور", callback_data=f"srv:open:{pidx}:{server_id}")])

    status_note = ""
    if server.status != "off":
        status_note = "\n\n⚠️ برای <b>اعمال</b> تغییر سایز، سرور باید خاموش باشد."

    if types:
        list_note = "فقط پلن‌هایی نمایش داده شده‌اند که همین الان در Location این سرور موجود هستند."
    else:
        list_note = "در حال حاضر هیچ پلن سازگار و موجود دیگری برای این سرور گزارش نشده است."

    await safe_edit(
        query,
        f"⚙️ <b>تغییر سایز {escape(server.name)}</b>\n\n"
        f"📍 Location: <code>{escape(location_name)}</code>\n"
        f"پلن فعلی: <code>{escape(getattr(current, 'name', 'نامشخص'))}</code> — {current_status}\n\n"
        f"{list_note}{status_note}",
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
    context.user_data.pop("server_create", None)
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
            await safe_edit(query, await compact_overview_text(), main_keyboard())
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
                "• Primary IPv4 را می‌توانید خودکار تعویض کنید؛ ربات سرور را خاموش، IP را جابه‌جا و دوباره روشن می‌کند.\n"
                "• IP قبلی بعد از تعویض حذف نمی‌شود و در بخش Primary IPv4های پروژه قابل مشاهده و حذف است.\n"
                "• Floating IP را می‌توانید بسازید، متصل، منتقل، جدا و حذف کنید.\n"
                "• بعد از اتصال، دستور Linux برای افزودن IP نمایش داده می‌شود.\n"
                "• Rescale فقط روی سرور خاموش انجام می‌شود.\n"
                "• ساخت و حذف سرور از داخل هر پروژه انجام می‌شود؛ برای ساخت، نام را تایپ و بقیه گزینه‌ها را با دکمه انتخاب می‌کنید.\n"
                f"• مانیتور Cost-Optimized هر {CHEAP_CHECK_HOURS:g} ساعت بررسی می‌کند و از صفحه موجودی قابل روشن/خاموش کردن است.\n"
                "• هشدار 18/19/20 TB هر شب فقط یک بار در روز ارسال می‌شود.",
                InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ منوی اصلی", callback_data="main")]]),
            )
            return
        if data == "cheap:show":
            await query.answer("در حال بررسی موجودی...")
            await show_cost_optimized(query)
            return
        if data == "cheap:auto":
            await query.answer()
            await safe_edit(query, "🤖 <b>ساخت خودکار Cost-Optimized</b>\n\nاین بخش جدا از مانیتور Cost-Optimized کار می‌کند.\n\nوقتی سفارش ساخت خودکار ثبت شود، حتی اگر مانیتور خاموش باشد، بررسی موجودی ادامه دارد و بعد از Available شدن سرور ساخته و اعلان ارسال می‌شود.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ برگشت", callback_data="cheap:show")]]))
            return
        if data == "cheap:toggle":
            current = cost_monitor_enabled()
            new_state = not current
            set_cost_monitor_enabled(new_state)
            await query.answer("مانیتور روشن شد." if new_state else "مانیتور خاموش شد.")
            await show_cost_optimized(
                query,
                "✅ مانیتور Cost-Optimized روشن شد." if new_state else "⏸ مانیتور Cost-Optimized خاموش شد.",
            )
            return
        if data == "cost:report":
            await query.answer()
            await safe_edit(query, await cost_report_text(), InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ منوی اصلی", callback_data="main")]]))
            return
        if data.startswith("project:"):
            await query.answer()
            await show_project(query, int(data.split(":", 1)[1]))
            return
        if data.startswith("fips:"):
            await query.answer()
            await show_floating_list(query, int(data.split(":", 1)[1]))
            return
        if data.startswith("pips:"):
            await query.answer()
            await show_primary_ip_list(query, int(data.split(":", 1)[1]))
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
            elif action == "swappip":
                await query.answer()
                await show_primary_swap_choices(query, pidx, sid)
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
                    "برای حذف مستقیم، سرور باید خاموش باشد. IP ابتدا Unassign و سپس از پروژه حذف می‌شود. ادامه می‌دهید؟",
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
                target = primary_ipv4_for_server(primary_ips, sid)
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
            elif action == "askdelete":
                await query.answer()
                await safe_edit(
                    query,
                    f"⚠️ <b>حذف کامل سرور</b>\n\n"
                    f"سرور: <b>{escape(server.name)}</b>\n"
                    f"Location: <code>{escape(server_location(server))}</code>\n"
                    f"پلن: <code>{escape(server.server_type.name)}</code>\n\n"
                    "تمام اطلاعات روی دیسک این سرور برای همیشه حذف می‌شود. این عملیات قابل بازگشت نیست.\n\n"
                    "مطمئن هستید؟",
                    InlineKeyboardMarkup(
                        [
                            [InlineKeyboardButton("🗑 بله، سرور حذف شود", callback_data=f"srv:delete:{pidx}:{sid}")],
                            [InlineKeyboardButton("⬅️ انصراف", callback_data=f"srv:open:{pidx}:{sid}")],
                        ]
                    ),
                )
            elif action == "delete":
                server_name = server.name
                await query.answer("در حال حذف سرور...")
                await safe_edit(
                    query,
                    f"🗑 <b>در حال حذف سرور</b>\n\n<b>{escape(server_name)}</b>\nلطفاً صبر کنید...",
                )
                act = await asyncio.to_thread(server.delete)
                await asyncio.to_thread(act.wait_until_finished)
                project_obj, servers, fips = await fetch_project_data(pidx)
                await safe_edit(
                    query,
                    f"✅ سرور <b>{escape(server_name)}</b> حذف شد.\n\n" + project_dashboard_text(project_obj, servers, fips),
                    project_dashboard_keyboard(pidx, servers),
                )
            return

        if kind == "pip":
            action = parts[1]
            if action == "open":
                pidx, ipid = int(parts[2]), int(parts[3])
                await query.answer()
                await show_primary_ip_detail(query, pidx, ipid)
            elif action == "askdel":
                pidx, ipid = int(parts[2]), int(parts[3])
                project = get_project(pidx)
                if not project:
                    await query.answer("پروژه پیدا نشد.", show_alert=True)
                    return
                ip = await asyncio.to_thread(project["client"].primary_ips.get_by_id, ipid)
                if not ip or getattr(ip, "type", None) != "ipv4":
                    await query.answer("Primary IPv4 پیدا نشد.", show_alert=True)
                    return
                if getattr(ip, "assignee_id", None) is not None:
                    await query.answer("این IPv4 هنوز به یک سرور متصل است و قابل حذف نیست.", show_alert=True)
                    return
                await query.answer()
                await safe_edit(
                    query,
                    f"⚠️ <b>حذف Primary IPv4 آزاد</b>\n\n"
                    f"IP: <code>{escape(str(ip.ip))}</code>\n"
                    f"نام: <b>{escape(primary_ip_name(ip))}</b>\n\n"
                    "این IP از پروژه Hetzner حذف می‌شود و این کار قابل بازگشت نیست. ادامه می‌دهید؟",
                    InlineKeyboardMarkup(
                        [
                            [InlineKeyboardButton("🗑 بله، حذف شود", callback_data=f"pip:delete:{pidx}:{ipid}")],
                            [InlineKeyboardButton("⬅️ انصراف", callback_data=f"pip:open:{pidx}:{ipid}")],
                        ]
                    ),
                )
            elif action == "delete":
                pidx, ipid = int(parts[2]), int(parts[3])
                project = get_project(pidx)
                if not project:
                    await query.answer("پروژه پیدا نشد.", show_alert=True)
                    return
                client = project["client"]
                ip = await asyncio.to_thread(client.primary_ips.get_by_id, ipid)
                if not ip or getattr(ip, "type", None) != "ipv4":
                    await query.answer("Primary IPv4 پیدا نشد.", show_alert=True)
                    return
                if getattr(ip, "assignee_id", None) is not None:
                    await query.answer("ابتدا باید IP از سرور جدا شود.", show_alert=True)
                    return
                ip_value = str(ip.ip)
                await query.answer("در حال حذف IPv4...")
                await asyncio.to_thread(ip.delete)
                primary_ips, servers = await asyncio.gather(
                    asyncio.to_thread(client.primary_ips.get_all),
                    asyncio.to_thread(client.servers.get_all),
                )
                await safe_edit(
                    query,
                    f"✅ Primary IPv4 <code>{escape(ip_value)}</code> حذف شد.\n\n"
                    + primary_ip_list_text(project, primary_ips, servers),
                    primary_ip_list_keyboard(pidx, primary_ips),
                )
            elif action == "swap":
                pidx, sid, ipid = int(parts[2]), int(parts[3]), int(parts[4])
                await perform_primary_ipv4_swap(query, pidx, sid, ipid)
            elif action == "newswap":
                pidx, sid = int(parts[2]), int(parts[3])
                await perform_primary_ipv4_swap(query, pidx, sid, None)
            return

        if kind == "newsv":
            action = parts[1]
            pidx = int(parts[2])
            project = get_project(pidx)
            if not project:
                await query.answer("پروژه پیدا نشد.", show_alert=True)
                return
            client = project["client"]

            if action == "start":
                context.user_data["server_create"] = {"project": pidx}
                context.user_data["awaiting_text"] = "server_name"
                context.user_data["panel_chat_id"] = query.message.chat_id
                context.user_data["panel_message_id"] = query.message.message_id
                await query.answer()
                await safe_edit(
                    query,
                    f"➕ <b>ساخت سرور — {escape(project['name'])}</b>\n\n"
                    "اسم سرور را به‌صورت پیام بفرستید.\n"
                    "فقط حروف انگلیسی کوچک، عدد و خط تیره مجاز است.\n"
                    "مثال: <code>panel-01</code>\n\n"
                    "بعد از اسم، بقیه گزینه‌ها با دکمه شیشه‌ای انتخاب می‌شوند.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ انصراف", callback_data=f"project:{pidx}")]]),
                )
                return

            if action == "locations":
                await query.answer()
                await show_create_locations(query, context, pidx)
                return

            if action == "loc":
                location_id = int(parts[3])
                await query.answer("در حال گرفتن پلن‌های موجود...")
                await show_create_plans(query, context, pidx, location_id)
                return

            pending = context.user_data.get("server_create", {})
            if int(pending.get("project", -1)) != pidx or not pending.get("name"):
                await query.answer("فرآیند ساخت منقضی شده؛ دوباره شروع کنید.", show_alert=True)
                return

            if action == "plan":
                stid = int(parts[3])
                st = await asyncio.to_thread(client.server_types.get_by_id, stid)
                location = await asyncio.to_thread(client.locations.get_by_id, int(pending["location_id"]))
                if not st or not location:
                    await query.answer("پلن یا Location پیدا نشد.", show_alert=True)
                    return
                entry = server_type_location_entry(st, location.name)
                if not entry or getattr(entry, "available", None) is not True:
                    await query.answer("این پلن همین حالا در این Location موجود نیست.", show_alert=True)
                    await show_create_plans(query, context, pidx, location.id)
                    return
                pending["server_type_id"] = stid
                context.user_data["server_create"] = pending
                await query.answer()
                await safe_edit(
                    query,
                    f"➕ <b>ساخت سرور</b>\n\n"
                    f"نام: <code>{escape(pending['name'])}</code>\n"
                    f"Location: <code>{escape(location.name)}</code>\n"
                    f"پلن: <code>{escape(st.name)}</code> — {st.cores} vCPU / {st.memory:g} GB\n\n"
                    "IPv6 برای این سرور فعال باشد؟\n\n"
                    "IPv4 در هر دو حالت فعال است.",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton("✅ با IPv6", callback_data=f"newsv:ip6on:{pidx}"),
                                InlineKeyboardButton("❌ بدون IPv6", callback_data=f"newsv:ip6off:{pidx}"),
                            ],
                            [InlineKeyboardButton("⬅️ پلن‌ها", callback_data=f"newsv:loc:{pidx}:{location.id}")],
                        ]
                    ),
                )
                return

            if action in {"ip6on", "ip6off"}:
                pending["ipv6"] = action == "ip6on"
                pending.pop("auth_mode", None)
                pending.pop("ssh_key_id", None)
                pending.pop("ssh_key_name", None)
                context.user_data["server_create"] = pending
                await query.answer()
                await show_create_auth_method(query, context, pidx)
                return

            if action == "auth":
                await query.answer()
                await show_create_auth_method(query, context, pidx)
                return

            if action == "authkey":
                await query.answer("در حال دریافت SSH Keyهای پروژه...")
                await show_create_ssh_keys(query, context, pidx)
                return

            if action == "authpass":
                pending["auth_mode"] = "password"
                pending.pop("ssh_key_id", None)
                pending.pop("ssh_key_name", None)
                context.user_data["server_create"] = pending
                await query.answer()
                await show_create_confirmation(query, context, pidx)
                return

            if action == "key":
                key_id = int(parts[3])
                key = await asyncio.to_thread(client.ssh_keys.get_by_id, key_id)
                if not key:
                    await query.answer("SSH Key پیدا نشد یا حذف شده است.", show_alert=True)
                    await show_create_ssh_keys(query, context, pidx)
                    return
                pending["auth_mode"] = "ssh"
                pending["ssh_key_id"] = key.id
                pending["ssh_key_name"] = str(getattr(key, "name", "") or f"Key {key.id}")
                context.user_data["server_create"] = pending
                await query.answer()
                await show_create_confirmation(query, context, pidx)
                return

            if action == "create":
                if pending.get("auth_mode") not in {"password", "ssh"}:
                    await query.answer("ابتدا روش ورود (رمز یا SSH Key) را انتخاب کنید.", show_alert=True)
                    await show_create_auth_method(query, context, pidx)
                    return
                st = await asyncio.to_thread(client.server_types.get_by_id, int(pending["server_type_id"]))
                location = await asyncio.to_thread(client.locations.get_by_id, int(pending["location_id"]))
                image = await asyncio.to_thread(client.images.get_by_id, int(pending["image_id"]))
                entry = server_type_location_entry(st, location.name) if st else None
                if not st or not location or not image:
                    await query.answer("اطلاعات ساخت ناقص است؛ دوباره شروع کنید.", show_alert=True)
                    return
                if not entry or getattr(entry, "available", None) is not True:
                    await query.answer("موجودی این پلن تمام شده؛ یک پلن دیگر انتخاب کنید.", show_alert=True)
                    await show_create_plans(query, context, pidx, location.id)
                    return
                await query.answer("در حال ساخت سرور...")
                await safe_edit(
                    query,
                    f"⏳ <b>در حال ساخت سرور</b>\n\n"
                    f"نام: <code>{escape(pending['name'])}</code>\n"
                    f"پلن: <code>{escape(st.name)}</code>\n"
                    f"Location: <code>{escape(location.name)}</code>\n\n"
                    "لطفاً صبر کنید؛ Allocation نهایی ممکن است کمی زمان ببرد.",
                )
                try:
                    create_kwargs = {
                        "name": pending["name"],
                        "server_type": st,
                        "image": image,
                        "location": location,
                        "start_after_create": True,
                        "public_net": ServerCreatePublicNetwork(
                            enable_ipv4=True,
                            enable_ipv6=bool(pending.get("ipv6", True)),
                        ),
                    }
                    if pending.get("auth_mode") == "ssh":
                        key_id = pending.get("ssh_key_id")
                        ssh_key = await asyncio.to_thread(client.ssh_keys.get_by_id, int(key_id)) if key_id else None
                        if not ssh_key:
                            await query.answer("SSH Key انتخاب‌شده دیگر موجود نیست.", show_alert=True)
                            await show_create_ssh_keys(query, context, pidx)
                            return
                        create_kwargs["ssh_keys"] = [ssh_key]
                    response = await asyncio.to_thread(client.servers.create, **create_kwargs)
                    await asyncio.to_thread(response.action.wait_until_finished)
                    for next_action in getattr(response, "next_actions", []) or []:
                        await asyncio.to_thread(next_action.wait_until_finished)
                    server = await asyncio.to_thread(client.servers.get_by_id, response.server.id)
                    if not server:
                        raise RuntimeError("سرور بعد از ساخت در API پیدا نشد")
                except Exception as exc:
                    log.exception("Server creation/allocation failed")
                    await safe_edit(
                        query,
                        f"❌ <b>ساخت سرور ناموفق بود.</b>\n\n"
                        f"Hetzner ممکن است بین Availability اولیه و Allocation نهایی ظرفیتش تمام شده باشد.\n\n"
                        f"<code>{escape(str(exc))}</code>",
                        InlineKeyboardMarkup(
                            [
                                [InlineKeyboardButton("🔄 انتخاب دوباره پلن", callback_data=f"newsv:loc:{pidx}:{location.id}")],
                                [InlineKeyboardButton("⬅️ برگشت به پروژه", callback_data=f"project:{pidx}")],
                            ]
                        ),
                    )
                    return
                root_password = getattr(response, "root_password", None)
                context.user_data.pop("server_create", None)
                context.user_data.pop("panel_chat_id", None)
                context.user_data.pop("panel_message_id", None)
                text = "✅ <b>سرور با موفقیت ساخته شد.</b>\n\n" + server_text(server, project["name"])
                if pending.get("auth_mode") == "ssh":
                    text += f"\n\n🔑 ورود با SSH Key: <code>{escape(str(pending.get('ssh_key_name') or 'Selected key'))}</code>"
                elif root_password:
                    text += (
                        "\n\n🔐 <b>رمز root — فقط همین بار نمایش داده می‌شود:</b>\n"
                        f"<code>{escape(root_password)}</code>"
                    )
                await safe_edit(query, text, server_keyboard(pidx, server))
                return

            await query.answer("درخواست ساخت نامعتبر است.", show_alert=True)
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
                current_disk = int(getattr(server, "primary_disk_size", 0) or getattr(server.server_type, "disk", 0) or 0)
                target_disk = int(getattr(st, "disk", 0) or 0)
                rows = [
                    [InlineKeyboardButton("✅ تغییر CPU/RAM — دیسک دست‌نخورده", callback_data=f"rzt:go0:{pidx}:{sid}:{stid}")],
                ]
                if target_disk > current_disk:
                    rows.append(
                        [InlineKeyboardButton(f"💽 ارتقای دیسک به {target_disk} GB", callback_data=f"rzt:go1:{pidx}:{sid}:{stid}")]
                    )
                rows.append([InlineKeyboardButton("⬅️ برگشت", callback_data=f"srv:resize:{pidx}:{sid}")])

                disk_text = (
                    f"دیسک فعلی: <b>{current_disk} GB</b> → دیسک پلن جدید: <b>{target_disk} GB</b>\n\n"
                    "⚠️ <b>هشدار دیسک:</b> اگر ارتقای دیسک را انتخاب کنید، افزایش فضای دیسک دائمی است؛ "
                    "بعداً نمی‌توان Disk را کوچک کرد و ممکن است امکان برگشت به بعضی پلن‌های کوچک‌تر را از دست بدهید."
                    if target_disk > current_disk
                    else f"دیسک فعلی: <b>{current_disk} GB</b> — برای این تغییر نیازی به ارتقای دیسک نیست."
                )

                await safe_edit(
                    query,
                    f"⚙️ <b>تأیید تغییر سایز</b>\n\n"
                    f"سرور: <b>{escape(server.name)}</b>\n"
                    f"از <code>{escape(server.server_type.name)}</code> به <code>{escape(st.name)}</code>\n"
                    f"منابع جدید: {st.cores} vCPU / {st.memory:g} GB RAM / {target_disk} GB\n\n"
                    f"{disk_text}",
                    InlineKeyboardMarkup(rows),
                )
            elif action in {"go0", "go1"}:
                pidx, sid, stid = map(int, parts[2:5])
                project = get_project(pidx)
                client = project["client"]
                server = await asyncio.to_thread(client.servers.get_by_id, sid)
                st = await asyncio.to_thread(client.server_types.get_by_id, stid)
                if not server or not st:
                    await query.answer("سرور یا پلن پیدا نشد.", show_alert=True)
                    return
                if server_type_availability(st, server_location(server)) is not True:
                    await query.answer("این پلن دیگر در Location سرور موجود نیست. لیست را بروزرسانی کنید.", show_alert=True)
                    return
                if server.status != "off":
                    await query.answer("برای اعمال تغییر سایز، ابتدا سرور را خاموش کنید.", show_alert=True)
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
    awaiting = context.user_data.get("awaiting_text")
    if awaiting not in {"fip_name", "server_name"}:
        return
    message = update.effective_message
    name = (message.text or "").strip()

    if awaiting == "server_name":
        pending = context.user_data.get("server_create", {})
        pidx = int(pending.get("project", -1))
        project = get_project(pidx)
        chat_id = context.user_data.get("panel_chat_id")
        message_id = context.user_data.get("panel_message_id")
        normalized = name.lower()
        try:
            await message.delete()
        except Exception:
            pass
        if not project or not valid_server_name(normalized):
            if chat_id and message_id and project:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=(
                            "❌ نام سرور معتبر نیست.\n\n"
                            "فقط حروف انگلیسی کوچک، عدد و خط تیره مجاز است؛ نام نباید با خط تیره شروع یا تمام شود.\n"
                            "مثال: <code>panel-01</code>\n\n"
                            "نام را دوباره بفرستید."
                        ),
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ انصراف", callback_data=f"project:{pidx}")]]),
                    )
                except Exception:
                    pass
            return
        try:
            existing = await asyncio.to_thread(project["client"].servers.get_by_name, normalized)
            if existing:
                if chat_id and message_id:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=f"❌ سروری با نام <code>{escape(normalized)}</code> از قبل وجود دارد.\n\nنام دیگری بفرستید.",
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ انصراف", callback_data=f"project:{pidx}")]]),
                    )
                return
            pending["name"] = normalized
            context.user_data["server_create"] = pending
            context.user_data.pop("awaiting_text", None)
            locations = await asyncio.to_thread(project["client"].locations.get_all)
            rows = [
                [InlineKeyboardButton(f"📍 {loc.name} — {loc.city}", callback_data=f"newsv:loc:{pidx}:{loc.id}")]
                for loc in locations
            ]
            rows.append([InlineKeyboardButton("⬅️ انصراف", callback_data=f"project:{pidx}")])
            if chat_id and message_id:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=f"➕ <b>ساخت سرور</b>\n\nنام: <code>{escape(normalized)}</code>\n\nLocation را انتخاب کنید:",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(rows),
                )
            return
        except Exception as exc:
            log.exception("Server create name flow failed")
            if chat_id and message_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=f"❌ خطا در آماده‌سازی ساخت سرور:\n<code>{escape(str(exc))}</code>",
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ برگشت", callback_data=f"project:{pidx}")]]),
                    )
                except Exception:
                    pass
            await clear_text_flow(context)
            return

    pending = context.user_data.get("fip_create", {})
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
    await render_message(update.effective_message, await compact_overview_text(), main_keyboard())


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



async def cmd_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await deny(update)
        return
    await render_message(
        update.effective_message,
        await cost_report_text(),
        InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ منوی اصلی", callback_data="main")]]),
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




def read_auto_create_state() -> dict:
    try:
        if AUTO_CREATE_STATE_FILE.exists():
            return json.loads(AUTO_CREATE_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Could not read auto create state")
    return {}


def write_auto_create_state(state: dict) -> None:
    AUTO_CREATE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTO_CREATE_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


async def auto_create_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Independent Cost-Optimized reservation watcher. It does not depend on availability monitor."""
    state = read_auto_create_state()
    request = state.get("request")
    if not request or state.get("created"):
        return
    try:
        project = PROJECTS[int(request["project"])]
        types = await asyncio.to_thread(project["client"].server_types.get_all)
        st = next((x for x in types if x.name == request["server_type"]), None)
        if not st:
            return
        loc = await asyncio.to_thread(project["client"].locations.get_by_name, request["location"])
        if not loc:
            return
        image = await asyncio.to_thread(project["client"].images.get_by_name, request.get("image", "ubuntu-24.04"))
        if not image:
            return
        result = await asyncio.to_thread(project["client"].servers.create,
            name=request["name"], server_type=st, image=image,
            location=loc, start_after_create=True)
        state["created"] = True
        write_auto_create_state(state)
        await context.bot.send_message(
            chat_id=int(ALLOWED_USER_ID),
            text=(f"✅ ساخت خودکار انجام شد\n\n🖥 {escape(result.server.name)}\n"
                  f"💸 پلن: {escape(st.name)}\n📍 {escape(loc.name)}"),
            parse_mode=ParseMode.HTML)
    except Exception:
        log.exception("Auto create failed")


def read_availability_state() -> dict:
    try:
        if AVAILABILITY_STATE_FILE.exists():
            return json.loads(AVAILABILITY_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Could not read Cost-Optimized availability state")
    return {}


def write_availability_state(state: dict) -> None:
    try:
        AVAILABILITY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = AVAILABILITY_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(AVAILABILITY_STATE_FILE)
    except Exception:
        log.exception("Could not write Cost-Optimized availability state")


def availability_pairs(matrix: dict[str, list]) -> list[str]:
    return sorted(f"{loc}|{st.name}" for loc, plans in matrix.items() for st in plans)


async def cost_optimized_availability_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ALLOWED_USER_ID or not cost_monitor_enabled():
        return
    try:
        _, matrix = await fetch_cost_optimized_matrix()
    except Exception:
        log.exception("Cost-Optimized availability job failed")
        return
    current = availability_pairs(matrix)
    state = read_availability_state()
    previous = set(state.get("available_pairs", []))
    new_pairs = set(current) - previous
    state["available_pairs"] = current
    state["last_checked_at"] = datetime.now(ZoneInfo(BOT_TIMEZONE)).isoformat()
    write_availability_state(state)
    if not new_pairs:
        return
    text = cost_optimized_text(matrix, title="🔔 Cost-Optimized موجود شد")
    text += "\n\n<b>موجودی جدید:</b>\n" + "\n".join(
        f"• <code>{escape(pair.split('|', 1)[1])}</code> در <code>{escape(pair.split('|', 1)[0])}</code>"
        for pair in sorted(new_pairs)
    )
    await context.bot.send_message(
        chat_id=int(ALLOWED_USER_ID),
        text=clip(text),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📁 انتخاب پروژه برای ساخت", callback_data="projects")]]),
    )


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
    if CHEAP_CHECK_HOURS <= 0:
        missing.append("CHEAP_CHECK_HOURS (> 0)")
    if missing:
        raise SystemExit("Missing/invalid configuration: " + ", ".join(missing))


if __name__ == "__main__":
    validate_config()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("servers", cmd_servers))
    app.add_handler(CommandHandler("traffic", cmd_traffic))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CommandHandler("floating", cmd_floating))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.job_queue.run_daily(traffic_alert_job, time=parse_job_time(), name="nightly-traffic-alert")
    app.job_queue.run_repeating(
        cost_optimized_availability_job,
        interval=CHEAP_CHECK_HOURS * 3600,
        first=30,
        name="cost-optimized-availability",
    )
    app.job_queue.run_repeating(
        auto_create_job,
        interval=AUTO_CREATE_CHECK_MINUTES * 60,
        first=60,
        name="cost-auto-create",
    )
    print(f"Hetzner Telegram Bot v{BOT_VERSION} is running with {len(PROJECTS)} project(s)...")
    app.run_polling(drop_pending_updates=True)
