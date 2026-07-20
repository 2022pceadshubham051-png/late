"""
ui_helpers.py
🎨 Premium UI/UX layer (Cricoverse-style presentation, no gameplay logic)

Reconstructed from usage patterns found in p.py — this module only handles
text/keyboard presentation. All game logic stays in the main bot file.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# ============================== ICONS / LABELS ==============================

ACTION_LABELS = {
    "attack": "⚔️ Attack",
    "defend": "🛡️ Defend",
    "heal": "🔧 Repair",
    "move": "🧭 Move",
    "ally": "🤝 Ally",
    "betray": "🗡️ Betray",
    "inventory": "🎒 Inventory",
    "spectate": "👁️ Spectate",
}

CELL_ICONS = {
    "self": "🟢",
    "enemy": "🔴",
    "loot": "🟡",
    "safe": "🟦",
    "destroyed": "⬛",
    "unknown": "⬜",
}

TEAM_STATUS_ICON = {
    "alpha": "🔵",
    "beta": "🔴",
    "alive": "🟢",
    "dead": "💀",
    "afk": "⏳",
}

# ============================== TEXT PRIMITIVES ==============================


def mention(user_id: int, name: str | None = None) -> str:
    """HTML mention link. Falls back to 'Captain' if no display name given."""
    label = name if name else "Captain"
    return f'<a href="tg://user?id={user_id}">{label}</a>'


def col_letter(index: int) -> str:
    """0 -> A, 1 -> B, ... 25 -> Z, 26 -> AA, etc."""
    index = int(index)
    letters = ""
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def direction_arrow(d_row: int, d_col: int) -> str:
    """Returns an arrow icon pointing roughly toward (d_row, d_col)."""
    if d_row == 0 and d_col == 0:
        return "⚪"
    if abs(d_row) >= 2 * abs(d_col):
        return "⬇️" if d_row > 0 else "⬆️"
    if abs(d_col) >= 2 * abs(d_row):
        return "➡️" if d_col > 0 else "⬅️"
    if d_row > 0 and d_col > 0:
        return "↘️"
    if d_row > 0 and d_col < 0:
        return "↙️"
    if d_row < 0 and d_col > 0:
        return "↗️"
    return "↖️"


# ============================== CARD / LIST BUILDERS ==============================


def build_card(title: str, lines: list, emoji: str = "🎴") -> str:
    """Builds an HTML-formatted 'card' block used throughout the bot."""
    header = f"{emoji} <b>{title}</b>\n" + "─" * 20 + "\n"
    body = "\n".join(str(line) for line in lines)
    return header + body


def branch_lines(items: list) -> list:
    """Renders a list of strings as a tree-branch structure (├─ / └─)."""
    items = list(items)
    rendered = []
    for i, item in enumerate(items):
        prefix = "└─ " if i == len(items) - 1 else "├─ "
        rendered.append(f"{prefix}{item}")
    return rendered


def pack_buttons(buttons: list, per_row: int = 2) -> InlineKeyboardMarkup:
    """Chunks a flat list of InlineKeyboardButtons into rows of `per_row`."""
    rows = [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]
    return InlineKeyboardMarkup(rows)


def build_map_grid(size: int, cell_state_fn, callback_prefix: str = "shipmap") -> InlineKeyboardMarkup:
    """Builds a clickable inline-button grid. cell_state_fn(r, c) -> state key in CELL_ICONS."""
    rows = []
    for r in range(size):
        row = []
        for c in range(size):
            state = cell_state_fn(r, c)
            icon = CELL_ICONS.get(state, CELL_ICONS["unknown"])
            row.append(InlineKeyboardButton(icon, callback_data=f"{callback_prefix}:{r}:{c}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def build_locator_lines(nearby: list) -> list:
    """
    nearby: list of dicts with keys 'user_id', 'distance', 'd_row', 'd_col'
    (as produced by the /position handler in p.py).
    """
    lines = []
    for entry in nearby:
        arrow = direction_arrow(entry.get("d_row", 0), entry.get("d_col", 0))
        lines.append(f"  {arrow} {mention(entry['user_id'])} — {entry['distance']} sectors away")
    return lines


def status_bar(hp, max_hp, shield, cargo, sector) -> str:
    """One-line status summary, e.g. HP / shield / cargo / sector."""
    hp_part = f"❤️ HP: {hp}" if not max_hp else f"❤️ HP: {hp}/{max_hp}"
    shield_part = f"🛡️ {shield}" if shield not in (None, "-", "") else "🛡️ —"
    return f"{hp_part}   {shield_part}   🎒 {cargo}   📍 {sector}"


def battle_log_line(icon: str, text: str) -> str:
    """Formats a single battle-log entry line."""
    return f"  {icon} {text}"


def safe_zone_warning(text: str) -> str:
    return f"🟥 <b>DANGER ZONE WARNING</b>\n{text}"


def cosmic_event_banner(name: str, desc: str, emoji: str = "🌌") -> str:
    return build_card("COSMIC EVENT", [f"{emoji} <b>{name}</b>", desc], emoji=emoji)


def movement_confirmation(user_id: int, x: int, y: int) -> str:
    return f"🧭 {mention(user_id)} confirmed course to {col_letter(y)}{x + 1}."


def urgency_banner(user_id: int, seconds_left, message: str) -> str:
    """Used in reminders, e.g. 'Submit your orders or risk an AFK strike!'"""
    try:
        secs = int(seconds_left)
        time_part = f"{secs}s left"
    except (TypeError, ValueError):
        time_part = str(seconds_left)
    return f"⏰ {mention(user_id)} — {message} ({time_part})"
