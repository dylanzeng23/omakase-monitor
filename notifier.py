import logging
import re
from pathlib import Path

import yaml
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from models import AvailabilitySlot, Config, Restaurant
import db

logger = logging.getLogger(__name__)

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# JP name hints for common restaurants
JP_NAMES = {
    "Sazenka": "茶禅華", "Shimazu": "島津", "Sawada": "さわ田", "Quintessence": "カンテサンス",
    "PELLEGRINO": "ペレグリーノ", "CHIUnE": "チューン", "Makimura": "まき村",
    "SEZANNE": "セザン", "Kabuto": "かぶと", "Kohaku": "虎白", "Kurosaki": "くろ﨑",
    "Sushi Hashimoto": "鮨 はしもと", "Sushi Yoshitake": "鮨 よしたけ",
    "Sushi Kimura": "すし 喜邑", "Sushi Meino": "鮨 めい乃",
    "Sushi Namba Hibiya": "鮨 なんば 日比谷", "Sushi Ikkou": "鮨 一幸",
    "Sushi Ryujiro": "鮨 龍次郎", "Ginza Kitagawa": "銀座 きた川",
    "Ginza Oishi": "銀座 大石", "YORONIKU TOKYO": "焼肉 よろにく",
    "TACUBO": "タクボ", "PRISMA": "プリズマ", "Ji-Cube": "ジーキューブ",
    "NARISAWA": "ナリサワ", "sincere": "シンシア", "Sushi Masuda": "鮨 ます田",
    "Sushi Tsubomi": "鮨 つぼみ", "Sushi Miyuki": "鮨 美幸",
    "Sushi Saito Hanare NANZUKA": "鮨 さいとう", "Sushi Shunsuke Asagaya": "鮨 しゅん輔",
    "Sushi Yuki": "鮨 ゆうき", "Sushi Riku": "鮨 陸",
    "Sushi Sho Yotsuya": "すし匠", "Sushi Masashi": "鮨 まさし",
    "Sushi Keita": "鮨 けいた", "Ebisu YORONIKU": "蕃 よろにく",
    "Sushi Namba Yotsuya": "鮨 なんば 四谷", "Sushi Nishizaki": "鮨 西崎",
    "Kaoru HIROO": "薫 HIROO", "ASAHINA Gastronome": "アサヒナ ガストロノーム",
    "commedia": "コメディア", "ShinoiS": "シノワ", "unis": "ユニス",
}


def format_report(slots: list[AvailabilitySlot], report: list[dict]) -> list[str]:
    """Format a full check report with all restaurant statuses. Returns list of messages."""
    if not report:
        return ["No data."]

    available = [r for r in report if "Y" in r.get("status", "")]
    open_no_slots = [r for r in report if "open" in r.get("status", "") or "no slots" in r.get("status", "")]
    closed = [r for r in report if r.get("status") == "closed"]
    errors = [r for r in report if r.get("status") == "error"]

    lines = []
    lines.append(f"Checked {len(report)} restaurants\n")

    if available:
        lines.append("=== AVAILABLE ===")
        for r in available:
            jp = JP_NAMES.get(r["name"], "")
            rating = f" [{r['rating']}]" if r.get("rating") else ""
            lines.append(f"  {r['name']}{rating} {jp}")
            lines.append(f"    {r['cuisine']} | {r['status']}")
        lines.append("")

    if open_no_slots:
        lines.append(f"--- Open but no target dates ({len(open_no_slots)}) ---")
        for r in open_no_slots:
            jp = JP_NAMES.get(r["name"], "")
            rating = f"[{r['rating']}]" if r.get("rating") else ""
            lines.append(f"  {r['name']} {jp} {rating} - {r['status']}")
        lines.append("")

    if closed:
        lines.append(f"--- Closed ({len(closed)}) ---")
        for r in sorted(closed, key=lambda x: -(x.get("rating") or 0)):
            jp = JP_NAMES.get(r["name"], "")
            rating = f"[{r['rating']}]" if r.get("rating") else ""
            cuisine = r.get("cuisine", "")
            lines.append(f"  {r['name']} {jp} {rating} {cuisine}")

    if errors:
        lines.append(f"\n--- Errors ({len(errors)}) ---")
        for r in errors:
            lines.append(f"  {r['name']}")

    # Split into 4000-char messages
    full = "\n".join(lines)
    messages = []
    while full:
        if len(full) <= 4000:
            messages.append(full)
            break
        cut = full[:4000].rfind("\n")
        if cut < 100:
            cut = 4000
        messages.append(full[:cut])
        full = full[cut:]
    return messages


def format_alert(slot: AvailabilitySlot) -> str:
    """Format an availability alert for Telegram."""
    lines = [
        f"{slot.restaurant_name} ({slot.omakase_code})",
        f"  {slot.slot_date}",
    ]
    if slot.slot_time:
        lines[-1] += f" | {slot.slot_time}"
    if slot.course_name:
        lines.append(f"  {slot.course_name}")
    if slot.price_jpy:
        lines[-1] += f" | JPY {slot.price_jpy:,}"
    lines.append(f"  https://omakase.in/en/r/{slot.omakase_code}")
    return "\n".join(lines)


async def send_alerts(config: Config, new_slots: list[AvailabilitySlot]):
    """Send Telegram alerts for new availability."""
    if not new_slots or not config.bot_token or not config.chat_id:
        return

    bot = Bot(token=config.bot_token)
    header = f"New Availability! {len(new_slots)} slot{'s' if len(new_slots) > 1 else ''} found\n{'=' * 30}\n"

    messages = []
    current_msg = header
    for slot in new_slots:
        alert_text = format_alert(slot)
        if len(current_msg) + len(alert_text) + 10 > 4000:
            messages.append(current_msg)
            current_msg = ""
        current_msg += "\n" + alert_text + "\n"

    if current_msg.strip():
        messages.append(current_msg)

    for msg in messages:
        try:
            await bot.send_message(chat_id=config.chat_id, text=msg)
            logger.info(f"Sent Telegram alert ({len(msg)} chars)")
        except Exception as e:
            logger.error(f"Failed to send Telegram alert: {e}")


async def send_message(config: Config, text: str):
    """Send a message to Telegram."""
    if not config.bot_token or not config.chat_id:
        return
    bot = Bot(token=config.bot_token)
    try:
        await bot.send_message(chat_id=config.chat_id, text=text)
    except Exception as e:
        logger.error(f"Failed to send message: {e}")


# --- Telegram Bot Command Handlers ---

_config: Config | None = None
_search_callback = None
_watchlist: list = []
_watchlist_path: Path | None = None


def set_config(config: Config):
    global _config
    _config = config


def set_watchlist(watchlist: list):
    global _watchlist
    _watchlist = watchlist


def set_watchlist_path(path: Path):
    global _watchlist_path
    _watchlist_path = path


def set_search_callback(callback):
    global _search_callback
    _search_callback = callback


def _save_watchlist():
    """Write current watchlist back to YAML file."""
    if not _watchlist_path:
        return
    restaurants = []
    for r in _watchlist:
        entry = {"name": r.name, "omakase_code": r.omakase_code}
        if r.tabelog_rating:
            entry["tabelog_rating"] = r.tabelog_rating
        if r.cuisine:
            entry["cuisine"] = r.cuisine
        if r.location and r.location != "Tokyo":
            entry["location"] = r.location
        restaurants.append(entry)
    with open(_watchlist_path, "w") as f:
        yaml.dump({"restaurants": restaurants}, f, default_flow_style=False,
                  allow_unicode=True, sort_keys=False)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command."""
    last_run = db.get_last_run()
    if not last_run:
        await update.message.reply_text("No runs recorded yet.")
        return

    text = (
        f"Last run:\n"
        f"Started: {last_run['started_at']}\n"
        f"Status: {last_run['status']}\n"
        f"Restaurants checked: {last_run['restaurants_checked']}\n"
        f"Slots found: {last_run['slots_found']}\n"
        f"New slots: {last_run['new_slots']}"
    )
    if last_run["error_message"]:
        text += f"\nError: {last_run['error_message']}"
    await update.message.reply_text(text)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /list command - show monitored restaurants."""
    if not _watchlist:
        await update.message.reply_text("No restaurants in watchlist.")
        return

    lines = [f"Watching {len(_watchlist)} restaurants:"]
    for i, r in enumerate(_watchlist, 1):
        rating = f" ({r.tabelog_rating})" if r.tabelog_rating else ""
        lines.append(f"{i}. {r.name}{rating}")
    await update.message.reply_text("\n".join(lines))


async def cmd_recent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /recent command."""
    recent = db.get_recent_availability(limit=10)
    if not recent:
        await update.message.reply_text("No availability found yet.")
        return

    lines = ["Recent availability:"]
    for r in recent:
        time_str = f" {r['slot_time']}" if r['slot_time'] else ""
        lines.append(f"  {r['slot_date']}{time_str} - {r['restaurant_name']}")
    await update.message.reply_text("\n".join(lines))


async def cmd_dates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /dates command."""
    if not _config:
        await update.message.reply_text("Config not loaded.")
        return
    dates_str = ", ".join(_config.target_dates)
    await update.message.reply_text(f"Monitoring dates: {dates_str}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    await update.message.reply_text(
        "Omakase Monitor Commands:\n"
        "  /check - Run immediate check\n"
        "  /status - Last run info\n"
        "  /list - Monitored restaurants\n"
        "  /recent - Recent availability\n"
        "  /dates - Target dates\n"
        "  /add ay187967 Name - Add restaurant\n"
        "  /remove Name - Remove restaurant\n"
        "  /help - This message"
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /add command - add a restaurant.
    /add ay187967                        — by code
    /add ay187967 Sazenka                — code + name
    /add ay187967 Sazenka Sushi 4.56     — code + name + cuisine + rating
    /add https://omakase.in/en/r/ay187967 — paste URL
    """
    args = (update.message.text or "").split()[1:]
    if not args:
        await update.message.reply_text(
            "Usage:\n"
            "  /add ay187967\n"
            "  /add ay187967 Sazenka\n"
            "  /add ay187967 Sazenka Sushi 4.56\n"
            "  /add https://omakase.in/en/r/ay187967"
        )
        return

    first = args[0]
    if "omakase.in" in first:
        match = re.search(r'/r/([a-z0-9]+)', first)
        code = match.group(1) if match else None
        if not code:
            await update.message.reply_text("Could not parse omakase code from URL.")
            return
    else:
        code = first.lower()

    for r in _watchlist:
        if r.omakase_code == code:
            await update.message.reply_text(f"Already watching: {r.name} ({code})")
            return

    name = args[1] if len(args) > 1 else code
    cuisine = args[2] if len(args) > 2 else ""
    rating = 0.0
    if len(args) > 3:
        try:
            rating = float(args[3])
        except ValueError:
            pass

    new_r = Restaurant(name=name, omakase_code=code, tabelog_rating=rating, cuisine=cuisine)
    _watchlist.append(new_r)
    _save_watchlist()

    await update.message.reply_text(
        f"Added: {name} ({code})\n"
        f"https://omakase.in/en/r/{code}\n"
        f"Total: {len(_watchlist)} restaurants"
    )


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /remove command.
    /remove 3          — by index from /list
    /remove Sazenka    — by name (partial, case-insensitive)
    """
    args = (update.message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("Usage: /remove 3 or /remove Sazenka")
        return

    query = args[1].strip()
    removed = None

    try:
        idx = int(query) - 1
        if 0 <= idx < len(_watchlist):
            removed = _watchlist.pop(idx)
        else:
            await update.message.reply_text(f"Invalid index. Use 1-{len(_watchlist)}.")
            return
    except ValueError:
        query_lower = query.lower()
        for i, r in enumerate(_watchlist):
            if query_lower in r.name.lower() or query_lower == r.omakase_code:
                removed = _watchlist.pop(i)
                break

    if removed:
        _save_watchlist()
        await update.message.reply_text(
            f"Removed: {removed.name} ({removed.omakase_code})\n"
            f"Remaining: {len(_watchlist)} restaurants"
        )
    else:
        await update.message.reply_text(f"Not found: {query}")


_search_requested = False


def is_search_requested() -> bool:
    global _search_requested
    if _search_requested:
        _search_requested = False
        return True
    return False


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle free-text messages."""
    text = (update.message.text or "").strip().lower()

    if text in ("check", "search", "scan", "搜", "查"):
        global _search_requested
        _search_requested = True
        await update.message.reply_text("Checking all restaurants...")

        if _search_callback:
            slots, report_data = await _search_callback()
            for msg in format_report(slots, report_data):
                await update.message.reply_text(msg)
    elif text in ("help", "帮助"):
        await cmd_help(update, context)


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /check command."""
    global _search_requested
    _search_requested = True
    await update.message.reply_text("Checking all restaurants...")

    if _search_callback:
        slots, report_data = await _search_callback()
        for msg in format_report(slots, report_data):
            await update.message.reply_text(msg)


def build_bot_app(config: Config) -> Application:
    """Build Telegram bot application with command handlers."""
    set_config(config)
    app = Application.builder().token(config.bot_token).build()
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("recent", cmd_recent))
    app.add_handler(CommandHandler("dates", cmd_dates))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app
