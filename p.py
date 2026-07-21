import logging
import asyncio
import random
import sqlite3
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, TypeHandler, filters, ContextTypes
from telegram.constants import ParseMode
from telegram.error import TelegramError, Forbidden, BadRequest
from collections import defaultdict
import json
import os
from typing import Union
# Import necessary classes for media handling
from telegram import InputMediaAnimation, InputMediaPhoto

# 🎨 ======================== PREMIUM UI/UX LAYER ======================== 🎨
# (Cricoverse-style presentation helpers — merged in from ui_helpers.py,
#  kept as a self-contained block so it's easy to find/edit as one unit)

# ---------- ICONS / LABELS ----------

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

# ---------- NAME CACHE (so mention() can show real names) ----------

_NAME_CACHE = {}


def register_name(user_id: int, name: str) -> None:
    """Remembers a player's display name so mention() can use it later
    even when only a user_id is available at the call site."""
    if user_id and name:
        _NAME_CACHE[int(user_id)] = name


def mention(user_id: int, name: str | None = None) -> str:
    """HTML mention link. Uses the given name, else the cached name for
    this user_id, else falls back to 'Captain'."""
    label = name or _NAME_CACHE.get(int(user_id)) or "Captain"
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


# ---------- CARD / LIST BUILDERS ----------


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
    """Builds a clickable inline-button grid.
    cell_state_fn(r, c) -> either a state key in CELL_ICONS, or a (state, count) tuple
    when more than one ship occupies the same cell (count is shown as a badge)."""
    rows = []
    for r in range(size):
        row = []
        for c in range(size):
            result = cell_state_fn(r, c)
            if isinstance(result, tuple):
                state, count = result
            else:
                state, count = result, 1
            icon = CELL_ICONS.get(state, CELL_ICONS["unknown"])
            label = f"{icon}{count}" if count and count > 1 else icon
            row.append(InlineKeyboardButton(label, callback_data=f"{callback_prefix}:{r}:{c}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def build_locator_lines(nearby: list) -> list:
    """
    nearby: list of dicts with keys 'user_id', 'distance', 'd_row', 'd_col'
    (as produced by the /position handler).
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

# ======================================================================== #

# ✨ --- Logging Setup --- ✨
# Configure logging for debugging and monitoring
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'), # Log to 'bot.log' file
        logging.StreamHandler() # Also print logs to the console
    ]
)
# Get a logger instance for this bot
logger = logging.getLogger(__name__)

# 🚀 ======================== CONFIGURATION ======================== 🚀

# --- Bot Token ---
BOT_TOKEN = '8474213410:AAE7UDzzrJUwdmQL1qssQclsVrAyMVSHv7A' # <<< 🔑 Replace with your Bot Token from BotFather

# --- User IDs ---
OWNER_ID = 8644197194 # 👑 The Bot Owner (Full Access)
ADMIN_IDS = [7460266461, 7379484662, 8049934625] # 🛡️ Additional Bot Admins (e.g., for /stats)

# --- Group IDs ---
SUPPORTIVE_GROUP_ID = -1002707382739 # 📣 Optional: For bot notifications
SUPPORTIVE_GROUP1_ID = -1003162937388 # 🔗 Group link for the /start button

# --- Anti-Spam Settings ---
SPAM_COOLDOWN = {} # Tracks user command times
SPAM_LIMIT = 3     # Max commands in timeframe for non-registered users
SPAM_TIMEFRAME = 10 # Seconds

# --- Economy: Coins ---
DAILY_COIN_AMOUNT = 50 # 🪙 Base daily reward
WIN_COIN_BONUS = 150   # 🏆 Bonus for winning a game
LAST_DAILY_CLAIM = {}  # Tracks user daily claims

# 🌟 ======================== LEVEL & XP SYSTEM ======================== 🌟

XP_PER_WIN = 100
XP_PER_KILL = 25
XP_PER_GAME = 10 # Participation XP

# Level definitions: XP threshold, Name, Emoji
LEVELS = {
    1: {'xp': 0, 'name': 'Recruit', 'emoji': '🔰'},
    2: {'xp': 500, 'name': 'Soldier', 'emoji': '⭐'},
    3: {'xp': 1200, 'name': 'Commander', 'emoji': '⭐⭐'},
    4: {'xp': 2500, 'name': 'Captain', 'emoji': '⭐⭐⭐'},
    5: {'xp': 4500, 'name': 'Admiral', 'emoji': '🌟'},
    6: {'xp': 7000, 'name': 'Fleet Admiral', 'emoji': '🌟🌟'},
    7: {'xp': 10000, 'name': 'Grand Admiral', 'emoji': '👑'},
    8: {'xp': 15000, 'name': 'Legendary Hero', 'emoji': '💎'},
}

def get_player_level(total_xp: int) -> int:
    """Calculates the player's level based on total XP."""
    current_level = 1
    for level in sorted(LEVELS.keys(), reverse=True):
        if total_xp >= LEVELS[level]['xp']:
            current_level = level
            break
    return current_level

def get_xp_for_next_level(current_level: int) -> int:
    """Gets the XP threshold for the level *after* the current one."""
    next_level_num = current_level + 1
    if next_level_num in LEVELS:
        return LEVELS[next_level_num]['xp']
    # Define behavior after max level (e.g., large number or fixed increment)
    return LEVELS[current_level]['xp'] + 10000 

def get_level_info(level: int) -> dict:
    """Gets the name and emoji for a specific level number."""
    return LEVELS.get(level, LEVELS[1]) # Default to Level 1 if not found

def calculate_xp_progress(current_level: int, total_xp: int) -> float:
    """Calculates the percentage progress towards the next level."""
    current_level_xp_req = LEVELS[current_level]['xp']
    next_level_xp_req = get_xp_for_next_level(current_level)
    
    max_level = max(LEVELS.keys())
    if current_level >= max_level:
        return 100.0 # Already at max level
        
    xp_needed_for_level = next_level_xp_req - current_level_xp_req
    if xp_needed_for_level <= 0:
        return 100.0 # Avoid division by zero
        
    xp_gained_in_level = total_xp - current_level_xp_req
    progress = (xp_gained_in_level / xp_needed_for_level) * 100.0
    
    return min(100.0, max(0.0, progress)) # Clamp between 0 and 100

# 🤖 ======================== BOT USERNAME ======================== 🤖
BOT_USERNAME = "shipoverse_bot" # Set your bot's username here (without @)

# 🌊 ======================== SEA SYSTEM (ORIGIN SEAS) ======================== 🌊
# Four permanent origin Seas every Captain chooses on first DM registration.
# Replace the invite_link values with each Sea's real Telegram group invite link.
SEAS = {
    'storm':   {'name': 'Storm Sea',   'emoji': '⛈️', 'color': '🔵', 'invite_link': 'https://t.me/+StormSeaInviteLinkHere'},
    'emerald': {'name': 'Emerald Sea', 'emoji': '🌿', 'color': '🟢', 'invite_link': 'https://t.me/+EmeraldSeaInviteLinkHere'},
    'crimson': {'name': 'Crimson Sea', 'emoji': '🔥', 'color': '🔴', 'invite_link': 'https://t.me/+CrimsonSeaInviteLinkHere'},
    'abyss':   {'name': 'Abyss Sea',   'emoji': '🌑', 'color': '⚫', 'invite_link': 'https://t.me/+AbyssSeaInviteLinkHere'},
}

# ✨ ======================== SHOP & TITLES ======================== ✨
# Titles players can acquire and display
PLAYER_TITLES = {
    'novice_captain': {'name': '⭐ Novice Captain', 'cost': 0, 'emoji': '⭐'},
    'space_pirate': {'name': '🏴‍☠️ Space Pirate', 'cost': 500, 'emoji': '🏴‍☠️'},
    'star_admiral': {'name': '🔱 Star Admiral', 'cost': 1500, 'emoji': '🔱'},
    'void_wanderer': {'name': '🌀 Void Wanderer', 'cost': 3000, 'emoji': '🌀'},
    'galaxy_conqueror': {'name': '👑 Galaxy Conqueror', 'cost': 5000, 'emoji': '👑'},
    'immortal_god': {'name': '✨ Immortal God', 'cost': 10000, 'emoji': '✨'}
}

# 💾 ======================== DATABASE SETUP ======================== 💾
DB_FILE = 'ship_battle.db' # Central definition for the database file

def init_database():
    """Sets up the SQLite database and creates necessary tables."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # Player Statistics Table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS players (
                user_id INTEGER PRIMARY KEY, username TEXT, total_games INTEGER DEFAULT 0, 
                wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0, kills INTEGER DEFAULT 0, 
                deaths INTEGER DEFAULT 0, damage_dealt INTEGER DEFAULT 0, damage_taken INTEGER DEFAULT 0, 
                heals_done INTEGER DEFAULT 0, loots_collected INTEGER DEFAULT 0, 
                win_streak INTEGER DEFAULT 0, best_streak INTEGER DEFAULT 0, total_score INTEGER DEFAULT 0, 
                betrayals INTEGER DEFAULT 0, alliances_formed INTEGER DEFAULT 0, last_played TEXT, 
                coins INTEGER DEFAULT 0, title TEXT DEFAULT 'novice_captain'
            )
        ''')

        # 🌊 --- Sea System Migration (adds DM-registration + Sea/origin columns to existing players table) ---
        cursor.execute("PRAGMA table_info(players)")
        existing_cols = [col[1] for col in cursor.fetchall()]
        if 'dm_registered' not in existing_cols:
            cursor.execute("ALTER TABLE players ADD COLUMN dm_registered INTEGER DEFAULT 0")
        if 'sea' not in existing_cols:
            cursor.execute("ALTER TABLE players ADD COLUMN sea TEXT DEFAULT NULL")
        conn.commit()

        # 🌊 --- Sea System: Island / Treasury / Captains / Contributions tables --- 🌊
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sea_treasury (
                sea TEXT PRIMARY KEY, total_coins INTEGER DEFAULT 0,
                project_name TEXT DEFAULT NULL, project_target INTEGER DEFAULT 0,
                project_current INTEGER DEFAULT 0, project_status TEXT DEFAULT 'none',
                project_created_by INTEGER DEFAULT NULL
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sea_captains (
                sea TEXT PRIMARY KEY, user_id INTEGER, assigned_at TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sea_contribution_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, sea TEXT,
                amount INTEGER, ts TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sea_project_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, sea TEXT, name TEXT,
                target INTEGER, completed_at TEXT
            )
        ''')
        # Seed a treasury row for each of the 4 seas
        for sea_key in SEAS.keys():
            cursor.execute('INSERT OR IGNORE INTO sea_treasury (sea, total_coins) VALUES (?, 0)', (sea_key,))
        conn.commit()

        # Game History Log Table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS game_history (
                game_id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, winner_id INTEGER, 
                winner_name TEXT, total_players INTEGER, total_rounds INTEGER, map_name TEXT, 
                start_time TEXT, end_time TEXT
            )
        ''')
        
        # Group-Specific Settings Table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS group_settings (
                chat_id INTEGER PRIMARY KEY, join_time INTEGER DEFAULT 120, 
                operation_time INTEGER DEFAULT 120, min_players INTEGER DEFAULT 2, 
                max_players INTEGER DEFAULT 20, allow_spectators INTEGER DEFAULT 1
            )
        ''')

        # Player Achievements Table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS player_achievements (
                user_id INTEGER, achievement TEXT, unlocked_at TEXT, 
                PRIMARY KEY (user_id, achievement)
            )
        ''')
        
        # Global Bans Table (replaces old group-specific bans)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS global_bans (
                user_id INTEGER PRIMARY KEY, reason TEXT, banned_by INTEGER, banned_at TEXT
            )
        ''')
        
        conn.commit()
        logger.info("✅ Database tables checked/created successfully.")
    except sqlite3.Error as e:
        logger.error(f"❌ Database initialization failed: {e}", exc_info=True)
    finally:
        if conn:
            conn.close()

def fix_corrupted_coins_in_db():
    """Scans player coins and resets invalid/corrupt values to 0."""
    fixed_count = 0
    conn = None # Initialize conn to None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT user_id, coins FROM players')
        rows = cursor.fetchall()
        
        updates_needed = [] # List to hold (0, user_id) tuples for fixing
        for user_id, coins_value in rows:
            needs_fix = False
            try:
                if coins_value is None or not isinstance(coins_value, (int, float, str)):
                    needs_fix = True
                else:
                    int_value = int(float(coins_value)) # Handle potential float strings like "50.0"
                    if int_value < 0 or int_value > 9999999: # Check range
                        needs_fix = True
            except (ValueError, TypeError):
                needs_fix = True # Conversion failed

            if needs_fix:
                updates_needed.append((0, user_id))
                # logger.info(f"🪙 Fixing coin value for user {user_id}: '{coins_value}' -> 0") # Optional: more verbose logging

        if updates_needed:
            cursor.executemany('UPDATE players SET coins = ? WHERE user_id = ?', updates_needed)
            conn.commit()
            fixed_count = len(updates_needed)
            
    except sqlite3.Error as e:
        logger.error(f"❌ Database error during coin fix: {e}", exc_info=True)
    finally:
        if conn:
            conn.close()
            
    logger.info(f"🪙 Coin integrity check complete. Found {fixed_count} values to potentially fix.")
    return fixed_count

# Initialize DB on script start
init_database()
fix_corrupted_coins_in_db()

# 🎬 ======================== GIF COLLECTIONS ======================== 🎬
# Used for dynamic messages like joining, starting, winning etc.
GIFS = {
    # 🚧 TEMP PLACEHOLDERS — replace these with your own GIF URLs later.
    # These are stable, publicly hotlinkable Wikimedia URLs so send_animation actually works in the meantime.
    'joining': [
        'https://upload.wikimedia.org/wikipedia/commons/2/2c/Rotating_earth_%28large%29.gif'
    ],
    'start': [
        'https://upload.wikimedia.org/wikipedia/commons/2/2c/Rotating_earth_%28large%29.gif'
    ],
    'operation': [
        'https://upload.wikimedia.org/wikipedia/commons/2/2c/Rotating_earth_%28large%29.gif'
    ],
    'day_summary': [
        'https://upload.wikimedia.org/wikipedia/commons/2/2c/Rotating_earth_%28large%29.gif'
    ],
    'victory': [
        'https://upload.wikimedia.org/wikipedia/commons/2/2c/Rotating_earth_%28large%29.gif'
    ],
    'eliminated': [
        'https://upload.wikimedia.org/wikipedia/commons/2/2c/Rotating_earth_%28large%29.gif'
    ],
    'extend': [
        'https://upload.wikimedia.org/wikipedia/commons/2/2c/Rotating_earth_%28large%29.gif'
    ],
    'event': 'https://upload.wikimedia.org/wikipedia/commons/2/2c/Rotating_earth_%28large%29.gif',
    'meteor': 'https://upload.wikimedia.org/wikipedia/commons/2/2c/Rotating_earth_%28large%29.gif',
    'boost': 'https://upload.wikimedia.org/wikipedia/commons/2/2c/Rotating_earth_%28large%29.gif'
}

# 🖼️ ======================== IMAGE COLLECTIONS ======================== 🖼️
# Static images for command responses (Fancy UI)
# 🚧 TEMP PLACEHOLDERS — replace these with your own image URLs later.
# Using a stable, publicly hotlinkable Wikimedia space image so send_photo actually works in the meantime.
IMAGES = {
    'start':        'https://upload.wikimedia.org/wikipedia/commons/e/ea/Hubble_ultra_deep_field.jpg',
    'help':         'https://upload.wikimedia.org/wikipedia/commons/e/ea/Hubble_ultra_deep_field.jpg',
    'rules':        'https://upload.wikimedia.org/wikipedia/commons/e/ea/Hubble_ultra_deep_field.jpg',
    'stats_admin':  'https://upload.wikimedia.org/wikipedia/commons/e/ea/Hubble_ultra_deep_field.jpg',
    'mystats':      'https://upload.wikimedia.org/wikipedia/commons/e/ea/Hubble_ultra_deep_field.jpg',
    'leaderboard':  'https://upload.wikimedia.org/wikipedia/commons/e/ea/Hubble_ultra_deep_field.jpg',
    'shop':         'https://upload.wikimedia.org/wikipedia/commons/e/ea/Hubble_ultra_deep_field.jpg',
    'daily':        'https://upload.wikimedia.org/wikipedia/commons/e/ea/Hubble_ultra_deep_field.jpg',
    'achievements': 'https://upload.wikimedia.org/wikipedia/commons/e/ea/Hubble_ultra_deep_field.jpg',
    'compare':      'https://upload.wikimedia.org/wikipedia/commons/e/ea/Hubble_ultra_deep_field.jpg',
    'tips':         'https://upload.wikimedia.org/wikipedia/commons/e/ea/Hubble_ultra_deep_field.jpg',
    'history':      'https://upload.wikimedia.org/wikipedia/commons/e/ea/Hubble_ultra_deep_field.jpg',
    'default':      'https://upload.wikimedia.org/wikipedia/commons/e/ea/Hubble_ultra_deep_field.jpg'
}

# ⚙️ ======================== GAME CONSTANTS ======================== ⚙️
# Core gameplay values
HP_START = 100                 # Starting health
ATTACK_DAMAGE = (20, 25)       # Damage range for Attack
HEAL_AMOUNT = (8, 16)          # HP restored range for Heal
DEFEND_REDUCTION = 0.5         # 50% damage reduction for Defend
CRIT_CHANCE = 0.20             # 20% chance of critical hit
CRIT_MULTIPLIER = 1.5          # 1.5x damage on critical hit
AFK_TURNS_LIMIT = 3            # Max missed turns before elimination
ATTACK_RANGE = 2               # Max distance (squares) for attack
ALLIANCE_DURATION = 2          # Turns an alliance lasts
BETRAYAL_DAMAGE_BONUS = 1.5    # 1.5x damage after betraying

# Inventory and Safe Zone
LOOT_ITEM_CAP = 5              # Max non-energy items held
SAFE_ZONE_DAMAGE = 15          # Damage per turn outside safe zone
SAFE_ZONE_SCHEDULE = {         # Day the zone shrinks
    7: {'name': 'Phase 1 Shrink', 'size_reduction_factor': 1},
    11: {'name': 'Phase 2 Shrink', 'size_reduction_factor': 2},
    14: {'name': 'Phase 3 Shrink', 'size_reduction_factor': 3},
    17: {'name': 'Phase 4 Shrink', 'size_reduction_factor': 4},
    30: {'name': 'Final Collapse', 'size_reduction_factor': 'final'} 
}

# 🗺️ ======================== MAP SYSTEMS ======================== 🗺️
# Available battlegrounds
MAPS = {
    'classic': {'name': '🗺️ Classic Arena', 'size': 5, 'emoji': '⬜', 'description': 'Standard 5x5 field'},
    'volcano': {'name': '🌋 Volcanic Wasteland', 'size': 6, 'emoji': '🟥', 'description': '6x6 hazardous terrain'},
    'ice': {'name': '❄️ Frozen Tundra', 'size': 5, 'emoji': '🟦', 'description': '5x5 slippery battlefield'},
    'urban': {'name': '🏙️ Urban Warfare', 'size': 7, 'emoji': '⬛', 'description': '7x7 close-quarters city'},
    'space': {'name': '🌌 Deep Space', 'size': 8, 'emoji': '🟪', 'description': '8x8 vast emptiness'}
}

# 🎒 ======================== LOOT ITEMS ======================== 🎒
# Items players can find
LOOT_ITEMS = {
    # --- Weapons (Single Use on Attack) ---
    'laser_gun': {'type': 'weapon', 'bonus': 20, 'rarity': 'rare', 'emoji': '🔫', 'desc': '+20 Damage'},
    'plasma_cannon': {'type': 'weapon', 'bonus': 35, 'rarity': 'epic', 'emoji': '💥', 'desc': '+35 Damage'},
    'nova_blaster': {'type': 'weapon', 'bonus': 50, 'rarity': 'legendary', 'emoji': '🌟', 'desc': '+50 Damage'},
    'pulse_rifle': {'type': 'weapon', 'bonus': 28, 'rarity': 'epic', 'emoji': '⚡', 'desc': '+28 Damage & Shield Bypass (WIP)'}, # TODO: Implement shield bypass

    # --- Shields (Single Use on Defense) ---
    'shield_gen': {'type': 'shield', 'bonus': 0.3, 'rarity': 'rare', 'emoji': '🛡️', 'desc': 'Block 30% Damage'},
    'fortress_shield': {'type': 'shield', 'bonus': 0.5, 'rarity': 'epic', 'emoji': '🏰', 'desc': 'Block 50% Damage'},
    'quantum_shield': {'type': 'shield', 'bonus': 0.7, 'rarity': 'legendary', 'emoji': '✨', 'desc': 'Block 70% Damage'},
    'reflective_shield': {'type': 'shield', 'bonus': 0.4, 'rarity': 'rare', 'emoji': '🪞', 'desc': 'Block 40% & Reflect 20% (WIP)'}, # TODO: Implement reflection

    # --- Energy (Instant Use on Pickup) ---
    'energy_core': {'type': 'energy', 'bonus': 15, 'rarity': 'common', 'emoji': '⚡', 'desc': 'Restore 15 HP'},
    'quantum_core': {'type': 'energy', 'bonus': 30, 'rarity': 'epic', 'emoji': '✨', 'desc': 'Restore 30 HP'},
    'life_essence': {'type': 'energy', 'bonus': 50, 'rarity': 'legendary', 'emoji': '💚', 'desc': 'Restore 50 HP'},
    'medkit': {'type': 'energy', 'bonus': 25, 'rarity': 'rare', 'emoji': '🩺', 'desc': 'Restore 25 HP & Cure AFK (WIP)'}, # TODO: Implement AFK cure

    # --- Utilities (Varying Effects) ---
    'stealth_device': {'type': 'utility', 'bonus': 0, 'rarity': 'legendary', 'emoji': '👻', 'desc': 'Become Untargetable (WIP)'}, # TODO: Implement stealth
    'emp_grenade': {'type': 'utility', 'bonus': 0, 'rarity': 'rare', 'emoji': '💣', 'desc': 'Halve Next Incoming Attack'}, # Implemented
    'teleport_beacon': {'type': 'utility', 'bonus': 0, 'rarity': 'epic', 'emoji': '🌀', 'desc': 'Random Teleport (WIP)'}, # TODO: Implement teleport use
    'radar_jammer': {'type': 'utility', 'bonus': 0, 'rarity': 'rare', 'emoji': '📡', 'desc': 'Hide Position (WIP)'}, # TODO: Implement position hiding
    'speed_boost': {'type': 'utility', 'bonus': 0, 'rarity': 'rare', 'emoji': '💨', 'desc': 'Chance for Double Attack'}, # Implemented
}

# Probability weights for finding loot of different rarities
RARITY_WEIGHTS = {'common': 50, 'rare': 30, 'epic': 15, 'legendary': 5}

# 🌌 ======================== COSMIC EVENTS ======================== 🌌
# Random occurrences that affect the battlefield
COSMIC_EVENTS = {
    'meteor_storm': {'name': '☄️ Meteor Storm', 'desc': 'Debris rains down, damaging all ships!', 'effect': 'damage_all', 'value': (15, 30), 'emoji': '☄️'},
    'solar_boost': {'name': '🌟 Solar Boost', 'desc': 'A wave of solar energy repairs all ships!', 'effect': 'heal_all', 'value': (20, 35), 'emoji': '🌟'},
    'wormhole': {'name': '🌀 Wormhole', 'desc': 'Unstable portals teleport random ships!', 'effect': 'teleport', 'value': None, 'emoji': '🌀'},
    'energy_surge': {'name': '⚡ Energy Surge', 'desc': 'Weapons systems overloaded! Bonus damage next turn!', 'effect': 'damage_boost', 'value': 1.5, 'emoji': '⚡'},
    'pirate_ambush': {'name': '🏴‍☠️ Pirate Ambush', 'desc': 'Space pirates attack random targets!', 'effect': 'random_damage', 'value': (20, 40), 'emoji': '🏴‍☠️'},
    'asteroid_field': {'name': '🪨 Asteroid Field', 'desc': 'Navigating dense asteroids causes minor damage!', 'effect': 'damage_all', 'value': (10, 20), 'emoji': '🪨'},
    'nebula_shield': {'name': '🌌 Nebula Shield', 'desc': 'Cosmic gas provides temporary shielding!', 'effect': 'shield_all', 'value': 0.3, 'emoji': '🌌'},
    # 'double_damage_round': {'name': '⚡ Double Damage', 'desc': 'All attacks deal 2x damage!', 'trigger': 'round_start', 'effect': 'damage_multiplier', 'value': 2.0, 'emoji': '⚡'}, # Example for future trigger system
    # 'healing_surge': {'name': '💚 Healing Surge', 'desc': 'All heals are 50% more effective!', 'trigger': 'round_start', 'effect': 'heal_multiplier', 'value': 1.5, 'emoji': '💚'}, # Example for future trigger system
    # 'treasure_chest': {'name': '💰 Treasure Find', 'desc': 'Random captains find bonus coins!', 'trigger': 'round_end', 'effect': 'coin_reward', 'value': 100, 'emoji': '💰'}, # Example for future trigger system
}

# 🏅 ======================== ACHIEVEMENTS ======================== 🏅
# Milestones players can unlock
ACHIEVEMENTS = {
    'first_blood': {'name': 'First Blood', 'desc': 'Achieve your first elimination', 'emoji': '🩸'},
    'killer': {'name': 'Skilled Hunter', 'desc': 'Eliminate 5 ships in one game', 'emoji': '💀'},
    'survivor': {'name': 'Survivor', 'desc': 'Win your first game', 'emoji': '🏆'},
    'champion': {'name': 'Champion', 'desc': 'Achieve 10 victories', 'emoji': '👑'},
    'collector': {'name': 'Collector', 'desc': 'Loot 50 items across all games', 'emoji': '📦'},
    'healer': {'name': 'Field Medic', 'desc': 'Restore 1000 HP total', 'emoji': '🩺'},
    'damage_dealer': {'name': 'Destroyer', 'desc': 'Inflict 5000 damage total', 'emoji': '💥'},
    'streak_3': {'name': 'Winning Streak', 'desc': 'Win 3 games consecutively', 'emoji': '🔥'},
    'team_player': {'name': 'Team Player', 'desc': 'Win a team-based game', 'emoji': '🤝'},
    'explorer': {'name': 'Explorer', 'desc': 'Move 50 times total', 'emoji': '🧭'},
    'betrayer': {'name': 'Backstabber', 'desc': 'Betray an alliance', 'emoji': '😈'},
    'diplomat': {'name': 'Diplomat', 'desc': 'Form 10 alliances total', 'emoji': '🕊️'}
}


# 🛠️ ======================== UTILITY FUNCTIONS ======================== 🛠️

def get_random_gif(category: str) -> str:
    """Selects a random GIF URL for a given category."""
    if category in GIFS:
        source = GIFS[category]
        if isinstance(source, list) and source:
            return random.choice(source)
        elif isinstance(source, str): # Handle single string entries
             return source
    # Fallback if category invalid or list empty
    logger.warning(f"GIF category '{category}' not found or empty, using fallback.")
    return GIFS['joining'][0] 

def get_random_image(category: str) -> str:
    """Selects an image URL for a given category, with fallback."""
    return IMAGES.get(category, IMAGES['default']) # Use .get for safe dictionary access

def get_progress_bar(current: float, maximum: float, length: int = 10) -> str:
    """Generates a text progress bar string (e.g., ████░░░░░░ 40%)."""
    current = max(0, current)
    maximum = max(1, maximum) # Prevent division by zero
    filled_length = int(length * current / maximum)
    bar = '█' * filled_length + '░' * (length - filled_length)
    percentage = (current / maximum) * 100
    return f"{bar} {percentage:.0f}%"

def format_time(seconds: float) -> str:
    """Formats seconds into a MM:SS string."""
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"

def get_rarity_color(rarity: str) -> str:
    """Returns an emoji representing item rarity color."""
    colors = {'common': '⚪', 'rare': '🔵', 'epic': '🟣', 'legendary': '🟠'}
    return colors.get(rarity.lower(), '⚪')

def get_hp_indicator(hp: float, max_hp: float) -> str:
    """Returns an emoji indicating HP status (🟢🟡🔴💀)."""
    if max_hp <= 0: return "💀"
    ratio = hp / max_hp
    if hp <= 0: return "💀"
    if ratio > 0.75: return "🟢"
    if ratio > 0.25: return "🟡"
    return "🔴"

def get_user_rank(user_id: int) -> int:
    """Calculates a user's global rank based on score. Returns 0 if error/unranked."""
    rank = 0
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        # Fetch ordered list of user IDs
        cursor.execute('SELECT user_id FROM players ORDER BY total_score DESC, wins DESC, kills DESC')
        results = cursor.fetchall()
        
        # Find the index (rank) + 1
        for i, (uid,) in enumerate(results, 1):
            if uid == user_id:
                rank = i
                break
    except sqlite3.Error as e:
        logger.error(f"DB error getting rank for {user_id}: {e}")
    finally:
        if conn:
            conn.close()
    return rank

# 🛠️ ======================== UTILITY FUNCTIONS (Continued) ======================== 🛠️

def escape_markdown_value(text: any) -> str:
    """Safely escapes text for Telegram HTML parse mode (name kept for compatibility with existing call sites)."""
    if not isinstance(text, str):
        text = str(text) # Ensure input is a string
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
def format_user_stats(stats_tuple: Union[tuple, None]) -> str:
    """Formats the player stats tuple into a Cricoverse-style bordered profile card."""
    if not stats_tuple or len(stats_tuple) < 19:
        return build_card("PLAYER PROFILE", ["No stats recorded yet. Time to battle!"], emoji="📊")

    # Safely unpack the tuple
    user_id = stats_tuple[0]
    username = stats_tuple[1] if len(stats_tuple) > 1 else "Unknown"
    games = stats_tuple[2] if len(stats_tuple) > 2 else 0
    wins = stats_tuple[3] if len(stats_tuple) > 3 else 0
    losses = stats_tuple[4] if len(stats_tuple) > 4 else 0
    kills = stats_tuple[5] if len(stats_tuple) > 5 else 0
    deaths = stats_tuple[6] if len(stats_tuple) > 6 else 0
    dmg_dealt = stats_tuple[7] if len(stats_tuple) > 7 else 0
    dmg_taken = stats_tuple[8] if len(stats_tuple) > 8 else 0
    heals = stats_tuple[9] if len(stats_tuple) > 9 else 0
    loots = stats_tuple[10] if len(stats_tuple) > 10 else 0
    win_streak = stats_tuple[11] if len(stats_tuple) > 11 else 0
    best_streak = stats_tuple[12] if len(stats_tuple) > 12 else 0
    score = stats_tuple[13] if len(stats_tuple) > 13 else 0
    betrayals = stats_tuple[14] if len(stats_tuple) > 14 else 0
    alliances = stats_tuple[15] if len(stats_tuple) > 15 else 0
    coins = stats_tuple[17] if len(stats_tuple) > 17 else 0
    title_key = stats_tuple[18] if len(stats_tuple) > 18 else 'novice_captain'

    # Validate title and coins
    if title_key not in PLAYER_TITLES: title_key = 'novice_captain'
    title_data = PLAYER_TITLES[title_key]
    try: coins_display = int(coins)
    except: coins_display = 0

    safe_username = escape_markdown_value(username or f"Captain_{user_id}")
    win_rate = int((wins / games) * 100) if games > 0 else 0
    kd_ratio = round(kills / max(1, deaths), 2)
    rank = get_user_rank(user_id)
    rank_display = f"#{rank}" if rank > 0 else "Unranked"

    sea_key = get_player_sea(user_id)
    if sea_key:
        sea_info = SEAS[sea_key]
        captain_id = get_sea_captain(sea_key)
        captain_tag = " 👑" if captain_id == user_id else ""
        sea_line = f"{sea_info['emoji']} Sea : {sea_info['name']}{captain_tag}"
    else:
        sea_line = "🌊 Sea : Not chosen — /start in DM"

    lines = [
        f"👤 Captain : <b>{safe_username}</b>",
        f"{title_data['emoji']} Title : {title_data['name']}   🏆 Rank : {rank_display}",
        sea_line,
        f"🪙 Coins : {coins_display}",
        "",
        "📈 BATTLE RECORD",
    ] + branch_lines([
        f"Games : {games}   Win Rate : {win_rate}%",
        f"Wins : {wins}   Losses : {losses}   Score : {score}",
    ]) + [
        "",
        "⚔️ COMBAT PROWESS",
    ] + branch_lines([
        f"Kills : {kills}   Deaths : {deaths}   K/D : {kd_ratio}",
        f"Damage Dealt : {dmg_dealt}   Damage Taken : {dmg_taken}",
    ]) + [
        "",
        "🛠 FIELD ACTIONS",
    ] + branch_lines([
        f"Healed : {heals} HP   Looted : {loots}",
        f"Win Streak : {win_streak} 🔥 (Best {best_streak} 🏅)",
        f"Alliances : {alliances} 🤝   Betrayals : {betrayals} 😈",
    ])

    return build_card("PLAYER PROFILE", lines, emoji="📊")

# --- Safe Sending Wrappers ---
# These handle errors gracefully when sending messages/media

async def safe_send(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, reply_markup=None, parse_mode=None, **kwargs):
    """Safely sends a text message."""
    try:
        msg = await context.bot.send_message(
            chat_id=chat_id, text=text, reply_markup=reply_markup,
            parse_mode=parse_mode, **kwargs
        )
        return msg
    except Forbidden:
        logger.warning(f"🚫 Blocked/Kicked: Cannot send text to {chat_id}.")
    except BadRequest as e:
        if 'message is not modified' not in str(e).lower(): # Ignore this common error
            logger.error(f"❌ Bad Request (Text): Chat {chat_id}, Error: {e}")
    except TelegramError as e:
        logger.error(f"❌ Telegram Error (Text): Chat {chat_id}, Error: {e}")
    except Exception as e:
        logger.error(f"❌ Unexpected Error (Text): Chat {chat_id}, Error: {e}", exc_info=True)
    return None

async def safe_send_animation(context: ContextTypes.DEFAULT_TYPE, chat_id: int, animation: str, caption: str, reply_markup=None, parse_mode=None, **kwargs):
    """Safely sends an animation (GIF), falls back to text."""
    try:
        msg = await context.bot.send_animation(
            chat_id=chat_id, animation=animation, caption=caption,
            reply_markup=reply_markup, parse_mode=parse_mode, **kwargs
        )
        return msg
    except Forbidden:
        logger.warning(f"🚫 Blocked/Kicked: Cannot send animation to {chat_id}. Falling back to text.")
        return await safe_send(context, chat_id, caption, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs)
    except (BadRequest, TelegramError) as e:
        logger.error(f"❌ Error sending animation to {chat_id}: {e}. Falling back to text.")
        return await safe_send(context, chat_id, caption, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs)
    except Exception as e:
        logger.error(f"❌ Unexpected Error (Animation): Chat {chat_id}, Error: {e}. Falling back to text.", exc_info=True)
        return await safe_send(context, chat_id, caption, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs)

async def safe_send_photo(context: ContextTypes.DEFAULT_TYPE, chat_id: int, photo_url: str, caption: str, reply_markup=None, parse_mode=None, **kwargs):
    """Safely sends a photo, falls back to text (or default photo)."""
    try:
        msg = await context.bot.send_photo(
            chat_id=chat_id, photo=photo_url, caption=caption,
            reply_markup=reply_markup, parse_mode=parse_mode, **kwargs
        )
        return msg
    except Forbidden:
        logger.warning(f"🚫 Blocked/Kicked: Cannot send photo to {chat_id}. Falling back to text.")
        return await safe_send(context, chat_id, caption, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs)
    except (BadRequest, TelegramError) as e:
        logger.error(f"❌ Error sending photo to {chat_id}: {e}. Falling back...")
        # Check for specific URL errors
        error_str = str(e).lower()
        is_url_error = ('wrong file identifier' in error_str or
                        'failed to get http url content' in error_str or
                        'wrong type of the web page content' in error_str)

        if is_url_error:
            logger.error(f"📸 Invalid photo URL: {photo_url}")
            default_photo = get_random_image('default')
            if photo_url != default_photo: # Avoid recursion if default is also bad
                logger.info(f"Retrying photo send to {chat_id} with default image.")
                return await safe_send_photo(context, chat_id, default_photo, caption, reply_markup, parse_mode, **kwargs)
        # Fallback to text if not a URL error or retry failed
        return await safe_send(context, chat_id, caption, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs)
    except Exception as e:
        logger.error(f"❌ Unexpected Error (Photo): Chat {chat_id}, Error: {e}. Falling back to text.", exc_info=True)
        return await safe_send(context, chat_id, caption, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs)

# --- 👮 Ban / Admin / Spam Checks ---

def is_globally_banned(user_id: int) -> bool:
    """Checks the database to see if a user is globally banned."""
    banned = False
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT 1 FROM global_bans WHERE user_id = ? LIMIT 1', (user_id,))
        banned = cursor.fetchone() is not None
    except sqlite3.Error as e:
        logger.error(f"❌ DB Error checking global ban for {user_id}: {e}")
        return False # Fail-safe: Assume not banned on DB error
    finally:
        if conn: conn.close()
    return banned

def check_spam(user_id: int) -> bool:
    """Checks if a user is globally banned OR spamming (if unregistered)."""
    # 1. Global Ban Check (Priority)
    if is_globally_banned(user_id):
        logger.warning(f"🚫 Globally banned user {user_id} attempted action.")
        return True # Block action

    # 2. Registered Player Check
    conn = None
    is_registered = False
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT 1 FROM players WHERE user_id = ? LIMIT 1', (user_id,))
        is_registered = cursor.fetchone() is not None
    except sqlite3.Error as e:
        logger.error(f"❌ DB Error checking registration for {user_id}: {e}")
        # Proceed to time check as fail-safe
    finally:
        if conn: conn.close()

    if is_registered:
        return False # Registered users bypass time check

    # 3. Time-Based Spam Check (Unregistered Users)
    current_time = datetime.now()
    if user_id not in SPAM_COOLDOWN:
        SPAM_COOLDOWN[user_id] = {'count': 1, 'first_time': current_time}
        return False
    else:
        user_data = SPAM_COOLDOWN[user_id]
        time_diff = (current_time - user_data['first_time']).total_seconds()

        if time_diff > SPAM_TIMEFRAME: # Reset if timeframe passed
            user_data['count'] = 1
            user_data['first_time'] = current_time
            return False
        else: # Increment and check limit if within timeframe
            user_data['count'] += 1
            if user_data['count'] > SPAM_LIMIT:
                logger.warning(f"⏳ Spam detected from unregistered user {user_id}.")
                return True # Spam detected
            return False # Within limit

async def is_owner(user_id: int) -> bool:
    """Checks if user ID matches the OWNER_ID."""
    return user_id == OWNER_ID

async def is_admin(user_id: int) -> bool:
    """Checks if user ID is owner or in ADMIN_IDS list."""
    return user_id == OWNER_ID or user_id in ADMIN_IDS

async def is_admin_or_owner(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    """Checks if user is bot owner OR a group admin/creator."""
    if await is_owner(user_id):
        return True
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status in ['creator', 'administrator']:
            return True
    except (BadRequest, TelegramError) as e:
        logger.warning(f"⚠️ Could not check group admin status for {user_id} in {chat_id}: {e}")
    except Exception as e:
        logger.error(f"❌ Unexpected error checking group admin status: {e}", exc_info=True)
    return False

# --- 📌 Other Utilities ---

async def pin_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    """Attempts to silently pin a message."""
    try:
        await context.bot.pin_chat_message(
            chat_id=chat_id, message_id=message_id, disable_notification=True
        )
        logger.info(f"📌 Pinned message {message_id} in chat {chat_id}.")
    except (BadRequest, TelegramError) as e:
        logger.warning(f"⚠️ Failed to pin message {message_id} in {chat_id}: {e}") # Often due to permissions
    except Exception as e:
        logger.error(f"❌ Unexpected error pinning message: {e}", exc_info=True)

def trigger_cosmic_event() -> tuple[Union[str, None], Union[dict, None]]:
    """Randomly determines if a cosmic event should trigger."""
    if random.random() < 0.30: # 30% chance per check
        event_key = random.choice(list(COSMIC_EVENTS.keys()))
        logger.info(f"🌌 Cosmic Event Triggered: {event_key}")
        return event_key, COSMIC_EVENTS[event_key]
    return None, None # No event

# 🎮 ======================== GLOBAL GAME STATE ======================== 🎮
# Stores active Game objects, keyed by chat_id
games: dict[int, 'Game'] = {}


def find_active_game_for_player(user_id: int) -> Union[tuple[int, 'Game'], tuple[None, None]]:
    """Scans all active games and returns (chat_id, game) for the one this player is in.
    Needed because /map, /myhp, /ranking are DM-only, but game state is keyed by group chat_id."""
    for chat_id, game in games.items():
        if game.is_active and user_id in game.players:
            return chat_id, game
    return None, None

# 🎲 ======================== GAME CLASS ======================== 🎲
# Represents and manages a single game instance

class Game:
    """Holds all state and logic for one Ship Battle game session."""
    def __init__(self, chat_id: int, creator_id: int, creator_name: str):
        self.chat_id: int = chat_id
        self.creator_id: int = creator_id
        self.creator_name: str = creator_name
        self.mode: Union[str, None] = None          # 'solo' or 'team'
        self.players: dict[int, dict] = {}   # {user_id: player_data}
        self.spectators: set[int] = set()    # {user_id}
        self.day: int = 0                     # Game round counter
        self.joining_message_id: Union[int, None] = None # Message with Join/Team buttons
        self.last_map_message_id: Union[int, None] = None # Latest interactive map message
        self.is_joining: bool = False         # True during player join phase
        self.is_active: bool = False          # True during active battle rounds
        self.join_end_time: Union[datetime, None] = None
        self.operation_end_time: Union[datetime, None] = None
        self.settings: dict = self.load_settings() # Load settings from DB for this group
        self.start_time: datetime = datetime.now()

        # In-game stats (consider if needed per-game or just globally)
        self.total_damage_this_game: int = 0
        self.total_heals_this_game: int = 0
        self.operations_log: list[str] = [] # Log actions for summary

        # Event tracking
        self.active_event: Union[str, None] = None
        self.event_effect: Union[dict, None] = None

        # Map state
        self.map_type: str = 'classic' # Default map
        self.map_size: int = MAPS['classic']['size']
        self.map_grid: list[list[list[int]]] = [
            [[] for _ in range(self.map_size)] for _ in range(self.map_size)
        ] # grid[row][col] = [user_id1, user_id2,...]

        # Safe Zone state
        self.safe_zone_center: tuple[int, int] = (self.map_size // 2, self.map_size // 2)
        self.safe_zone_radius: float = float('inf') # Start covering the whole map
        self.safe_zone_current_phase: int = 0

        # Team state
        self.teams: dict[str, set[int]] = {'alpha': set(), 'beta': set()}

        # Voting state
        self.map_votes: dict[int, str] = {} # {user_id: map_key}
        self.map_voting: bool = False
        self.map_vote_end_time: Union[datetime, None] = None

        # Alliance state
        self.alliances: dict[int, dict] = {} # {user_id: {'ally': ally_id, 'turns_left': int}}

        self._operation_countdown_running: bool = False # Internal flag

        logger.info(f"🚀 New Game object initialized for chat {self.chat_id} by {self.creator_id}")

    def load_settings(self) -> dict:
        """Loads group-specific game settings from the database."""
        defaults = {'join_time': 120, 'operation_time': 120, 'min_players': 2, 'max_players': 20, 'allow_spectators': 1}
        conn = None
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute('SELECT join_time, operation_time, min_players, max_players, allow_spectators FROM group_settings WHERE chat_id = ?', (self.chat_id,))
            row = cursor.fetchone()
            if row:
                return {'join_time': row[0], 'operation_time': row[1], 'min_players': row[2], 'max_players': row[3], 'allow_spectators': row[4]}
        except sqlite3.Error as e:
            logger.error(f"❌ DB Error loading settings for chat {self.chat_id}: {e}")
        finally:
            if conn: conn.close()
        return defaults # Return defaults if not found or on error

    def set_map(self, map_key: str):
        """Sets the game map and resets grid/safe zone accordingly."""
        if map_key not in MAPS:
            logger.warning(f"⚠️ Invalid map key '{map_key}', defaulting to 'classic'.")
            map_key = 'classic'

        self.map_type = map_key
        self.map_size = MAPS[map_key]['size']
        self.map_grid = [[[] for _ in range(self.map_size)] for _ in range(self.map_size)] # Reset grid

        # Reset safe zone for the new map
        self.safe_zone_center = (self.map_size // 2, self.map_size // 2)
        self.safe_zone_radius = float('inf') # Start covering everything
        self.safe_zone_current_phase = 0
        logger.info(f"🗺️ Map set to '{map_key}' ({self.map_size}x{self.map_size}) for game in chat {self.chat_id}")

    def add_player(self, user_id: int, username: str, first_name: str, team: Union[str, None] = None) -> tuple[bool, str]:
        """Adds a player to the game if possible."""
        if len(self.players) >= self.settings['max_players']:
            return False, f"🚫 Fleet is full! Max capacity: {self.settings['max_players']} captains."
        if user_id in self.players:
            return False, "⚠️ You're already registered for this battle!"

        # Assign random starting position
        x, y = random.randint(0, self.map_size - 1), random.randint(0, self.map_size - 1)
        self.map_grid[x][y].append(user_id)

        # Store player data
        self.players[user_id] = {
            'user_id': user_id, 'username': username or f"Anon_{user_id}", 'first_name': first_name or "Captain",
            'hp': HP_START, 'max_hp': HP_START, 'inventory': [], 'operation': None, 'target': None,
            'position': (x, y), 'team': team, 'afk_turns': 0,
            'stats': {'kills': 0, 'damage_dealt': 0, 'damage_taken': 0, 'heals_done': 0, 'loots': 0, 'moves': 0},
            'alive': True, 'last_action_time': None
        }

        if team and team in self.teams:
            self.teams[team].add(user_id)

        logger.info(f"✅ Player {user_id} ({first_name}) joined game {self.chat_id}. Pos: ({x},{y}), Team: {team}")
        return True, "Welcome aboard, Captain!"

    def get_alive_players(self) -> list[int]:
        """Returns IDs of players currently alive."""
        return [uid for uid, data in self.players.items() if data.get('alive', False)]

    def get_alive_team_players(self, team_name: str) -> list[int]:
        """Returns IDs of alive players on a specific team."""
        if team_name not in self.teams: return []
        return [uid for uid in self.teams[team_name] if self.players.get(uid, {}).get('alive', False)]

    def get_players_in_range(self, user_id: int, attack_range: int = ATTACK_RANGE) -> list[int]:
        """Finds potential targets within attack range (alive, not self, not ally, not teammate)."""
        targets = []
        if user_id not in self.players or not self.players[user_id].get('alive'): return []

        player_data = self.players[user_id]
        px, py = player_data['position']
        player_team = player_data.get('team')
        ally_id = self.alliances.get(user_id, {}).get('ally')

        for target_id, target_data in self.players.items():
            if (target_id != user_id and
                    target_data.get('alive', False) and
                    target_id != ally_id and
                    (self.mode != 'team' or target_data.get('team') != player_team)):

                tx, ty = target_data['position']
                distance = abs(px - tx) + abs(py - ty) # Manhattan distance
                if distance <= attack_range:
                    targets.append(target_id)
        return targets

    def move_player(self, user_id: int, direction: str) -> bool:
        """Moves the player one square, updating grid position. Returns True if successful."""
        if user_id not in self.players or not self.players[user_id].get('alive'):
            logger.warning(f"⚠️ Attempted move for non-existent/dead player {user_id}")
            return False

        player = self.players[user_id]
        x, y = player['position']
        new_x, new_y = x, y

        # --- Calculate New Position ---
        if direction == 'up' and x > 0: new_x -= 1
        elif direction == 'down' and x < self.map_size - 1: new_x += 1
        elif direction == 'left' and y > 0: new_y -= 1
        elif direction == 'right' and y < self.map_size - 1: new_y += 1
        else: # Invalid direction or boundary hit immediately
            logger.debug(f"Player {user_id} move failed: Direction '{direction}' invalid or at boundary ({x},{y}).")
            return False

        # --- Update Grid ---
        try:
            # Remove from old cell
            if user_id in self.map_grid[x][y]:
                self.map_grid[x][y].remove(user_id)
            else:
                logger.warning(f"⚠️ Player {user_id} not found in expected grid cell ({x},{y}) on move removal.")
            # Add to new cell
            self.map_grid[new_x][new_y].append(user_id)
        except IndexError:
            logger.error(f"❌ Grid IndexError during move for player {user_id} from ({x},{y}) to ({new_x},{new_y}). Map size: {self.map_size}. Grid state might be inconsistent.", exc_info=True)
            # Attempt to revert logical position if grid update fails? Or just log? For now, log.
            # player['position'] = (x, y) # Revert? Potentially complex if grid already modified.
            return False # Treat grid error as move failure for safety

        # --- Update Player State ---
        player['position'] = (new_x, new_y)
        player['stats']['moves'] = player['stats'].get('moves', 0) + 1
        # logger.info(f"🧭 Player {user_id} moved {direction} to ({new_x},{new_y}).")
        return True

    def is_in_safe_zone(self, x: int, y: int) -> bool:
        """Checks if coordinates (x, y) are within the safe zone radius."""
        # Manhattan distance check
        distance = abs(x - self.safe_zone_center[0]) + abs(y - self.safe_zone_center[1])
        return distance <= self.safe_zone_radius

    def get_map_display(self) -> str:
        """Generates a string representation of the map WITHOUT grid lines, using square zone markers."""
        map_data = MAPS.get(self.map_type, MAPS['classic'])
        n = self.map_size # Grid size (e.g., 5 for 5x5)
        alive_count = len(self.get_alive_players())

        # --- Header ---
        safe_zone_side = min(n, int(self.safe_zone_radius * 2) + 1) if self.safe_zone_radius != float('inf') else n
        zone_status = f"{safe_zone_side}x{safe_zone_side} Square" if self.safe_zone_radius != float('inf') else "Full Map"

        header = (
            f"🗺️ <b>Battle Map:</b> {map_data['name']} ({n}x{n})\n"
            f"☀️ <b>Day:</b> {self.day} | 🚢 <b>Ships Alive:</b> {alive_count}/{len(self.players)}\n"
            f"🌀 <b>Safe Zone:</b> {zone_status}\n\n"
        )

        # --- Map Grid Construction (No Lines) ---
        map_lines = []
        # Column numbers header (optional, keep for reference)
        col_header = "   " + " ".join(map(str, range(n))) # Simple spaced numbers: " 0 1 2..."
        map_lines.append(col_header)

        for i in range(n): # Rows (x-coordinate)
            # Row number, right-aligned with width 2 for consistent alignment
            row_str = f"{i:>2} "
            for j in range(n): # Columns (y-coordinate)
                cell_ids = self.map_grid[i][j]
                is_safe = self.is_in_safe_zone(i, j)
                alive_here = [uid for uid in cell_ids if self.players.get(uid, {}).get('alive')]

                symbol = "?" # Default unknown symbol
                if alive_here:
                    symbol = "🚢" if len(alive_here) == 1 else f"🚢{len(alive_here)}"
                elif cell_ids:
                    symbol = "💀" # Wreck emoji
                else:
                    symbol = "🟩" if is_safe else "🟥" # Zone square emojis

                # Add the symbol followed by a space for separation
                row_str += symbol + " "

            map_lines.append(row_str.strip()) # Add the completed row string (strip trailing space)

        # --- Legend ---
        legend = (
            f"\n\n<b>Legend:</b>\n"
            f"  🚢 Ship | 💀 Wreck | 🟩 Safe Zone | 🟥 Danger Zone"
        )

        # Combine header, grid, and legend
        # NO code block ``` used here
        return header + "\n".join(map_lines) + legend

    def get_map_header_card(self) -> str:
        """HTML header card summarizing the battle map (used above the interactive grid)."""
        map_data = MAPS.get(self.map_type, MAPS['classic'])
        n = self.map_size
        alive_count = len(self.get_alive_players())
        safe_zone_side = min(n, int(self.safe_zone_radius * 2) + 1) if self.safe_zone_radius != float('inf') else n
        zone_status = f"{safe_zone_side}x{safe_zone_side} Square" if self.safe_zone_radius != float('inf') else "Full Map"
        return build_card(
            "BATTLE MAP",
            [
                f"🗺 {map_data['name']} ({n}x{n})",
                f"☀️ Day : {self.day}   🚢 Ships Alive : {alive_count}/{len(self.players)}",
                f"🌀 Safe Zone : {zone_status}",
                "",
                "🟢 You  🔴 Enemy  🟡 Loot  🟦 Safe  ⬛ Destroyed  ⬜ Unknown",
            ],
            emoji="🗺",
        )

    def get_map_keyboard(self, viewer_id: Union[int, None] = None) -> InlineKeyboardMarkup:
        """Builds the clickable inline-button map grid (interactive, no plain-text map).
        Shows every ship in a cell (with a count badge if more than one share a cell).
        Callback data embeds this game's chat_id so taps resolve correctly even when the
        map is viewed in a player's DM (not just the group)."""
        def cell_state(r, c):
            cell_ids = self.map_grid[r][c]
            alive_here = [uid for uid in cell_ids if self.players.get(uid, {}).get('alive')]
            count = len(alive_here)
            if not alive_here:
                if cell_ids:
                    return "destroyed"
                return "safe" if self.is_in_safe_zone(r, c) else "unknown"
            if viewer_id is not None and viewer_id in alive_here:
                return ("self", count)
            return ("enemy", count)
        return build_map_grid(self.map_size, cell_state, callback_prefix=f"shipmap:{self.chat_id}")

    def get_player_rank(self, user_id: int) -> int:
        """Gets player's rank among the currently alive players."""
        rank = 0
        alive_ids = self.get_alive_players()
        if user_id not in alive_ids: return 0 # Not alive or not in game

        player_stats_list = [
            (uid, self.players[uid]['hp'], self.players[uid]['stats'].get('kills', 0))
            for uid in alive_ids
        ]
        # Sort by HP desc, then Kills desc
        sorted_players = sorted(player_stats_list, key=lambda x: (x[1], x[2]), reverse=True)

        for i, (uid, _, _) in enumerate(sorted_players, 1):
            if uid == user_id:
                rank = i
                break
        return rank

    def form_alliance(self, user_id1: int, user_id2: int):
        """Creates a temporary alliance entry for two players."""
        self.alliances[user_id1] = {'ally': user_id2, 'turns_left': ALLIANCE_DURATION}
        self.alliances[user_id2] = {'ally': user_id1, 'turns_left': ALLIANCE_DURATION}
        logger.info(f"🤝 Alliance formed: {user_id1} <-> {user_id2} ({ALLIANCE_DURATION} turns)")

    def break_alliance(self, user_id: int) -> Union[int, None]:
        """Removes alliance entries involving the user. Returns the former ally ID."""
        former_ally_id = None
        alliance_info = self.alliances.pop(user_id, None) # Remove betrayer's entry
        if alliance_info:
            former_ally_id = alliance_info['ally']
            self.alliances.pop(former_ally_id, None) # Remove betrayed's entry
            logger.info(f"💔 Alliance broken by {user_id} (was allied with {former_ally_id})")
        else:
             logger.warning(f"⚠️ {user_id} tried to break non-existent alliance.")
        return former_ally_id

    def update_alliances(self):
        """Decrements turn counters for alliances and removes expired ones."""
        expired_pairs = set()
        for user_id in list(self.alliances.keys()): # Iterate on copy
            if user_id in self.alliances:
                self.alliances[user_id]['turns_left'] -= 1
                if self.alliances[user_id]['turns_left'] <= 0:
                    ally_id = self.alliances[user_id]['ally']
                    pair = tuple(sorted((user_id, ally_id)))
                    expired_pairs.add(pair) # Mark pair for removal

        # Remove expired pairs
        removed_count = 0
        for u1, u2 in expired_pairs:
            if self.alliances.pop(u1, None): removed_count += 1
            if self.alliances.pop(u2, None): removed_count += 1
            if removed_count > 0:
                logger.info(f"⏳ Alliance expired between {u1} and {u2}")

    def update_safe_zone(self) -> Union[str, None]:
        """Checks schedule and shrinks safe zone if needed. Returns log message."""
        log_msg = None
        if self.day in SAFE_ZONE_SCHEDULE:
            schedule = SAFE_ZONE_SCHEDULE[self.day]
            phase_name = schedule['name']
            factor = schedule['size_reduction_factor']
            current_radius = self.safe_zone_radius
            new_radius = current_radius

            if factor == 'final':
                new_radius = 0
            elif isinstance(factor, (int, float)):
                # Calculate reduction (example: shrink by a fraction of map size based on factor)
                # This needs careful balancing based on map sizes!
                max_possible_radius = self.map_size # Max distance from center
                total_phases = len([d for d in SAFE_ZONE_SCHEDULE if SAFE_ZONE_SCHEDULE[d]['size_reduction_factor'] != 'final'])
                shrink_amount_per_phase = max_possible_radius / total_phases if total_phases > 0 else max_possible_radius
                # Calculate target radius for this phase factor
                target_radius = round(max_possible_radius - (factor * shrink_amount_per_phase))
                new_radius = max(0, target_radius) # Ensure radius is not negative
            else:
                 logger.error(f"❌ Invalid size_reduction_factor '{factor}' in SAFE_ZONE_SCHEDULE for day {self.day}")

            # Only update if the radius is actually shrinking
            if new_radius < current_radius:
                self.safe_zone_radius = new_radius
                self.safe_zone_current_phase += 1
                log_msg = f"🌀 <b>{phase_name}!</b> Safe zone shrinks! New radius: {self.safe_zone_radius} blocks."
                logger.info(f" Safe zone updated for chat {self.chat_id}: {log_msg}")

        return log_msg

    # 💾 ======================== DATABASE HELPER FUNCTIONS ======================== 💾
# (These were missing from previous parts)

def get_player_coins(user_id: int) -> int:
    """Safely retrieves the current coin balance for a user."""
    coins = 0
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT coins FROM players WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        if result and result[0] is not None:
            try:
                coins = int(result[0]) # Ensure it's an integer
                if coins < 0: coins = 0 # Prevent negative coins
            except (ValueError, TypeError):
                logger.warning(f"⚠️ Corrupt coin value found for user {user_id}: {result[0]}. Resetting to 0.")
                # Optionally, fix it in the DB here
                # cursor.execute('UPDATE players SET coins = 0 WHERE user_id = ?', (user_id,))
                # conn.commit()
                coins = 0
    except sqlite3.Error as e:
        logger.error(f"❌ DB Error getting coins for user {user_id}: {e}")
    finally:
        if conn: conn.close()
    return coins

def add_player_coins(user_id: int, amount: int, reason: str = "transaction") -> int:
    """Adds (or subtracts if amount is negative) coins for a player. Returns new balance."""
    conn = None
    new_balance = 0
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        # Use atomic update (read current, calculate new, write new)
        cursor.execute('SELECT coins FROM players WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        current_coins = 0
        if result and result[0] is not None:
            try:
                current_coins = int(result[0])
            except (ValueError, TypeError):
                current_coins = 0 # Treat corrupt data as 0

        new_balance = max(0, current_coins + amount) # Ensure balance doesn't go below 0

        cursor.execute('UPDATE players SET coins = ? WHERE user_id = ?', (new_balance, user_id))
        conn.commit()
        logger.info(f"🪙 Coins Update: User {user_id} | Reason: {reason} | Amount: {amount:+} | New Balance: {new_balance}")
    except sqlite3.Error as e:
        logger.error(f"❌ DB Error adding coins for user {user_id}: {e}")
        # Return current known coins (or 0) on error, as update failed
        new_balance = get_player_coins(user_id)
    finally:
        if conn: conn.close()
    return new_balance

def set_player_coins(user_id: int, amount: int) -> int:
    """Sets a player's coin balance to a specific amount."""
    conn = None
    final_amount = max(0, int(amount)) # Ensure non-negative integer
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('UPDATE players SET coins = ? WHERE user_id = ?', (final_amount, user_id))
        conn.commit()
        logger.info(f"🪙 Coins Set: User {user_id} | New Balance: {final_amount}")
    except sqlite3.Error as e:
        logger.error(f"❌ DB Error setting coins for user {user_id}: {e}")
        return get_player_coins(user_id) # Return current balance on error
    finally:
        if conn: conn.close()
    return final_amount

def get_player_stats(user_id: int) -> Union[tuple, None]:
    """Retrieves all player stats as a tuple from the database, performing validation."""
    conn = None
    stats_tuple = None
    try:
        conn = sqlite3.connect(DB_FILE)
        # Use row factory for easier access? For now, tuple is fine.
        conn.row_factory = sqlite3.Row # Access columns by name
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM players WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()

        if row:
            # Convert row object to a standard tuple for consistency with previous code
            # Ensure order matches the expected format used in format_user_stats etc.
            stats_tuple = (
                row['user_id'], row['username'], row['total_games'], row['wins'], row['losses'],
                row['kills'], row['deaths'], row['damage_dealt'], row['damage_taken'], row['heals_done'],
                row['loots_collected'], row['win_streak'], row['best_streak'], row['total_score'],
                row['betrayals'], row['alliances_formed'], row['last_played'],
                # Perform coin/title validation here before returning
                max(0, int(row['coins'] or 0)), # Validate coins
                row['title'] if row['title'] in PLAYER_TITLES else 'novice_captain' # Validate title
            )
            # Simple check for expected number of columns (adjust if table changes)
            if len(stats_tuple) < 19:
                 logger.warning(f"⚠️ Incomplete stats tuple fetched for user {user_id}. Length: {len(stats_tuple)}")
                 # Attempt to return what was fetched, format_user_stats should handle missing indices somewhat
            # Fix potentially invalid values directly in DB? (Could slow down reads)
            # fix_needed = False
            # corrected_coins = max(0, int(row['coins'] or 0))
            # corrected_title = row['title'] if row['title'] in PLAYER_TITLES else 'novice_captain'
            # if corrected_coins != row['coins'] or corrected_title != row['title']:
            #     fix_needed = True
            # if fix_needed:
            #     # Run UPDATE query here if desired
            #     pass
        # else: user not found

    except sqlite3.Error as e:
        logger.error(f"❌ DB Error getting stats for user {user_id}: {e}")
    except (ValueError, TypeError) as e:
         logger.error(f"❌ Data type error processing stats for user {user_id}: {e}") # e.g., if coins is not number
    finally:
        if conn: conn.close()
    return stats_tuple


def update_player_stats(user_id: int, username: Union[str, None], stats_update: dict):
    """Updates player stats in the database. Creates player if not exists."""
    conn = None
    safe_username = username or f"User_{user_id}" # Ensure a username exists

    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        # --- Create player row if it doesn't exist ---
        # Use INSERT OR IGNORE to avoid errors if player already exists
        cursor.execute('''
            INSERT OR IGNORE INTO players (user_id, username, title, last_played) VALUES (?, ?, ?, ?)
        ''', (user_id, safe_username, 'novice_captain', datetime.now().isoformat()))

        # --- Prepare UPDATE query ---
        set_clauses = []
        values = []

        # Handle incremental updates (add to existing value)
        for key, value in stats_update.items():
            # Ensure keys are valid column names to prevent injection (though values are parameterized)
            valid_increment_keys = [
                'total_games', 'wins', 'losses', 'kills', 'deaths', 'damage_dealt',
                'damage_taken', 'heals_done', 'loots_collected', 'total_score',
                'betrayals', 'alliances_formed', 'coins'
            ]
            if key in valid_increment_keys:
                set_clauses.append(f"{key} = {key} + ?")
                values.append(value)

        # Handle direct set updates (overwrite existing value)
        valid_set_keys = ['title', 'win_streak', 'best_streak'] # Add others if needed
        for key in valid_set_keys:
             if key in stats_update:
                  # Special validation for title
                  if key == 'title' and stats_update[key] not in PLAYER_TITLES:
                       logger.warning(f"⚠️ Invalid title '{stats_update[key]}' provided for user {user_id}. Using default.")
                       set_clauses.append(f"{key} = ?")
                       values.append('novice_captain')
                  else:
                       set_clauses.append(f"{key} = ?")
                       values.append(stats_update[key])


        # Always update username and last_played timestamp
        set_clauses.append("username = ?")
        values.append(safe_username)
        set_clauses.append("last_played = ?")
        values.append(datetime.now().isoformat())

        # Finalize query
        if set_clauses: # Only run UPDATE if there's something to update
            query = f"UPDATE players SET {', '.join(set_clauses)} WHERE user_id = ?"
            values.append(user_id)
            cursor.execute(query, values)
            conn.commit()
            # logger.debug(f"Updated stats for user {user_id}: {stats_update}") # Optional debug log
        # else: logger.debug(f"No valid stats updates provided for user {user_id}.")


    except sqlite3.Error as e:
        logger.error(f"❌ DB Error updating stats for user {user_id}: {e}")
    except Exception as e:
         logger.error(f"❌ Unexpected error updating stats for {user_id}: {e}", exc_info=True)
    finally:
        if conn: conn.close()

def unlock_achievement(user_id: int, achievement_key: str) -> bool:
    """Adds an achievement for a user if they haven't unlocked it yet. Returns True if newly unlocked."""
    if achievement_key not in ACHIEVEMENTS:
        logger.warning(f"⚠️ Attempted to unlock invalid achievement key: {achievement_key}")
        return False

    conn = None
    newly_unlocked = False
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO player_achievements (user_id, achievement, unlocked_at)
            VALUES (?, ?, ?)
        ''', (user_id, achievement_key, datetime.now().isoformat()))
        conn.commit()
        # rowcount > 0 means a new row was inserted (achievement was newly unlocked)
        if cursor.rowcount > 0:
            newly_unlocked = True
            logger.info(f"🏅 Achievement Unlocked: User {user_id} -> {achievement_key}")
    except sqlite3.Error as e:
        logger.error(f"❌ DB Error unlocking achievement '{achievement_key}' for user {user_id}: {e}")
    finally:
        if conn: conn.close()
    return newly_unlocked

def get_player_achievements(user_id: int) -> list[str]:
    """Retrieves a list of achievement keys unlocked by a user."""
    achievements_list = []
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT achievement FROM player_achievements WHERE user_id = ? ORDER BY unlocked_at', (user_id,))
        results = cursor.fetchall()
        achievements_list = [row[0] for row in results]
    except sqlite3.Error as e:
        logger.error(f"❌ DB Error getting achievements for user {user_id}: {e}")
    finally:
        if conn: conn.close()
    return achievements_list

def save_game_history(game: Game, winner_id: int, winner_name: str):
    """Saves the details of a completed game to the history table."""
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO game_history
            (chat_id, winner_id, winner_name, total_players, total_rounds, map_name, start_time, end_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            game.chat_id, winner_id, winner_name, len(game.players), game.day,
            game.map_type, game.start_time.isoformat(), datetime.now().isoformat()
        ))
        conn.commit()
        logger.info(f"📜 Game history saved for chat {game.chat_id}. Winner: {winner_name}")
    except sqlite3.Error as e:
        logger.error(f"❌ DB Error saving game history for chat {game.chat_id}: {e}")
    finally:
        if conn: conn.close()

def get_leaderboard(limit: int = 10) -> list[tuple]:
    """Retrieves the top players globally based on score, wins, kills."""
    results = []
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        # Fetch necessary columns for display, ordered by ranking criteria
        cursor.execute('''
            SELECT username, wins, total_games, kills, damage_dealt, total_score, title
            FROM players
            ORDER BY total_score DESC, wins DESC, kills DESC
            LIMIT ?
        ''', (limit,))
        results = cursor.fetchall()
    except sqlite3.Error as e:
        logger.error(f"❌ DB Error fetching leaderboard: {e}")
    finally:
        if conn: conn.close()
    return results

def get_player_stats_by_username(username: str) -> Union[tuple, None]:
    """Retrieves player stats tuple by searching for username (case-insensitive)."""
    conn = None
    stats_tuple = None
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row # Use row factory for easier access by name
        cursor = conn.cursor()
        # Case-insensitive search
        cursor.execute('SELECT * FROM players WHERE username = ? COLLATE NOCASE', (username,))
        row = cursor.fetchone()

        # Fallback: Simple LIKE search if exact match fails (can be slow on large DB)
        # if not row:
        #     cursor.execute('SELECT * FROM players WHERE username LIKE ? COLLATE NOCASE LIMIT 1', (f'%{username}%',))
        #     row = cursor.fetchone()

        if row:
             # Convert row back to tuple in the correct order, with validation
             stats_tuple = (
                row['user_id'], row['username'], row['total_games'], row['wins'], row['losses'],
                row['kills'], row['deaths'], row['damage_dealt'], row['damage_taken'], row['heals_done'],
                row['loots_collected'], row['win_streak'], row['best_streak'], row['total_score'],
                row['betrayals'], row['alliances_formed'], row['last_played'],
                max(0, int(row['coins'] or 0)), # Validate coins
                row['title'] if row['title'] in PLAYER_TITLES else 'novice_captain' # Validate title
            )
             if len(stats_tuple) < 19: # Simple length check
                  logger.warning(f"⚠️ Incomplete stats tuple fetched for username '{username}'. Length: {len(stats_tuple)}")

    except sqlite3.Error as e:
        logger.error(f"❌ DB Error getting stats for username '{username}': {e}")
    except (ValueError, TypeError) as e:
         logger.error(f"❌ Data type error processing stats for username '{username}': {e}")
    finally:
        if conn: conn.close()
    return stats_tuple


# 🌊 ======================== SEA SYSTEM: DB HELPERS ======================== 🌊

def is_player_registered(user_id: int) -> bool:
    """A player is 'registered' once they've started the bot in DM AND picked an origin Sea."""
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT dm_registered, sea FROM players WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        if not row:
            return False
        dm_registered, sea = row
        return bool(dm_registered) and sea in SEAS
    except sqlite3.Error as e:
        logger.error(f"❌ DB Error checking registration for user {user_id}: {e}")
        return False
    finally:
        if conn: conn.close()


def get_player_sea(user_id: int) -> Union[str, None]:
    """Returns the sea key ('storm'/'emerald'/'crimson'/'abyss') for a player, or None."""
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT sea FROM players WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        if row and row[0] in SEAS:
            return row[0]
        return None
    except sqlite3.Error as e:
        logger.error(f"❌ DB Error getting sea for user {user_id}: {e}")
        return None
    finally:
        if conn: conn.close()


def mark_dm_registered(user_id: int, username: Union[str, None]):
    """Ensures the player row exists and flags dm_registered = 1."""
    conn = None
    safe_username = username or f"User_{user_id}"
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO players (user_id, username, title, last_played) VALUES (?, ?, ?, ?)
        ''', (user_id, safe_username, 'novice_captain', datetime.now().isoformat()))
        cursor.execute('UPDATE players SET dm_registered = 1, username = ? WHERE user_id = ?', (safe_username, user_id))
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"❌ DB Error marking dm_registered for user {user_id}: {e}")
    finally:
        if conn: conn.close()


def set_player_sea(user_id: int, sea_key: str) -> bool:
    """Sets a player's permanent origin Sea. Returns False if they already have one."""
    if sea_key not in SEAS:
        return False
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT sea FROM players WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        if row and row[0] in SEAS:
            return False # Already assigned — Sea choice is permanent
        cursor.execute('UPDATE players SET sea = ? WHERE user_id = ?', (sea_key, user_id))
        conn.commit()
        return True
    except sqlite3.Error as e:
        logger.error(f"❌ DB Error setting sea for user {user_id}: {e}")
        return False
    finally:
        if conn: conn.close()


def build_sea_selection_keyboard() -> InlineKeyboardMarkup:
    """Builds the 4-button Sea choice keyboard."""
    buttons = [
        InlineKeyboardButton(f"{info['emoji']} {info['name']}", callback_data=f"sea_pick_{key}")
        for key, info in SEAS.items()
    ]
    # 2 per row
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


async def send_dm_registration_alert(context: ContextTypes.DEFAULT_TYPE, chat_id: int, first_name: str, user_id: int = None):
    """Sent inside a GROUP when an unregistered player tries to join a game there."""
    bot_link_username = context.bot.username or BOT_USERNAME
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📡 Start in DM", url=f"https://t.me/{bot_link_username}?start=register")
    ]])
    tag = mention(user_id, first_name) if user_id else escape_markdown_value(first_name)
    await safe_send(
        context, chat_id,
        f"⚠️ {tag}, please go to my DM and press <b>/start</b> first "
        f"to choose your origin Sea before you can join a battle here!",
        reply_markup=keyboard, parse_mode=ParseMode.HTML
    )


async def require_registration(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, first_name: str, chat_id: int) -> bool:
    """Gate used before letting a player join/play in a group. Returns True if allowed to proceed."""
    if is_player_registered(user_id):
        return True
    # If this call came from a button press, answer with an alert too
    if update.callback_query:
        await update.callback_query.answer("⚠️ Please start the bot in DM and choose your Sea first!", show_alert=True)
    await send_dm_registration_alert(context, chat_id, first_name, user_id)
    return False


# 🏝️ ======================== SEA SYSTEM: ISLAND / TREASURY / CAPTAIN HELPERS ======================== 🏝️

def get_sea_member_count(sea_key: str) -> int:
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM players WHERE sea = ?', (sea_key,))
        row = cursor.fetchone()
        return row[0] if row else 0
    except sqlite3.Error as e:
        logger.error(f"❌ DB Error counting members for sea {sea_key}: {e}")
        return 0
    finally:
        if conn: conn.close()


def get_sea_captain(sea_key: str) -> Union[int, None]:
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT user_id FROM sea_captains WHERE sea = ?', (sea_key,))
        row = cursor.fetchone()
        return row[0] if row else None
    except sqlite3.Error as e:
        logger.error(f"❌ DB Error getting captain for sea {sea_key}: {e}")
        return None
    finally:
        if conn: conn.close()


def is_sea_captain(user_id: int, sea_key: Union[str, None] = None) -> bool:
    """Checks if user_id is captain of their own sea (or the given sea)."""
    target_sea = sea_key or get_player_sea(user_id)
    if not target_sea:
        return False
    return get_sea_captain(target_sea) == user_id


def set_sea_captain(sea_key: str, user_id: int) -> bool:
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO sea_captains (sea, user_id, assigned_at) VALUES (?, ?, ?)
            ON CONFLICT(sea) DO UPDATE SET user_id = excluded.user_id, assigned_at = excluded.assigned_at
        ''', (sea_key, user_id, datetime.now().isoformat()))
        conn.commit()
        return True
    except sqlite3.Error as e:
        logger.error(f"❌ DB Error setting captain for sea {sea_key}: {e}")
        return False
    finally:
        if conn: conn.close()


def get_sea_treasury_row(sea_key: str) -> Union[tuple, None]:
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT total_coins, project_name, project_target, project_current, project_status, project_created_by
            FROM sea_treasury WHERE sea = ?
        ''', (sea_key,))
        return cursor.fetchone()
    except sqlite3.Error as e:
        logger.error(f"❌ DB Error reading treasury for sea {sea_key}: {e}")
        return None
    finally:
        if conn: conn.close()


def get_all_sea_treasuries() -> dict:
    """Returns {sea_key: total_coins} for every sea."""
    conn = None
    result = {}
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT sea, total_coins FROM sea_treasury')
        for sea_key, coins in cursor.fetchall():
            result[sea_key] = coins
    except sqlite3.Error as e:
        logger.error(f"❌ DB Error reading all treasuries: {e}")
    finally:
        if conn: conn.close()
    return result


async def contribute_coins_to_sea(context: ContextTypes.DEFAULT_TYPE, user_id: int, sea_key: str, amount: int) -> tuple:
    """Deducts coins from player, adds to sea treasury + active project.
    Returns (success: bool, message: str, project_completed: Union[dict, None])."""
    stats = get_player_stats(user_id)
    current_coins = stats[17] if stats and len(stats) > 17 else 0
    if amount <= 0:
        return False, "⚠️ Enter a positive coin amount.", None
    if amount > current_coins:
        return False, f"⚠️ You only have 🪙 {current_coins} coins.", None

    conn = None
    project_completed = None
    try:
        # Deduct from player
        update_player_stats(user_id=user_id, username=None, stats_update={'coins': -amount})

        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('UPDATE sea_treasury SET total_coins = total_coins + ? WHERE sea = ?', (amount, sea_key))

        cursor.execute('SELECT project_name, project_target, project_current, project_status FROM sea_treasury WHERE sea = ?', (sea_key,))
        row = cursor.fetchone()
        if row and row[3] == 'active':
            project_name, project_target, project_current, _ = row
            new_current = project_current + amount
            if new_current >= project_target:
                # Project complete!
                cursor.execute('''
                    UPDATE sea_treasury SET project_current = ?, project_status = 'complete' WHERE sea = ?
                ''', (new_current, sea_key))
                cursor.execute('''
                    INSERT INTO sea_project_history (sea, name, target, completed_at) VALUES (?, ?, ?, ?)
                ''', (sea_key, project_name, project_target, datetime.now().isoformat()))
                project_completed = {'name': project_name, 'target': project_target}
            else:
                cursor.execute('UPDATE sea_treasury SET project_current = ? WHERE sea = ?', (new_current, sea_key))

        cursor.execute('''
            INSERT INTO sea_contribution_log (user_id, sea, amount, ts) VALUES (?, ?, ?, ?)
        ''', (user_id, sea_key, amount, datetime.now().isoformat()))
        conn.commit()
        return True, f"✅ Contributed 🪙 {amount} to the {SEAS[sea_key]['name']}!", project_completed
    except sqlite3.Error as e:
        logger.error(f"❌ DB Error contributing coins for user {user_id} to sea {sea_key}: {e}")
        return False, "⚠️ Something went wrong recording your contribution.", None
    finally:
        if conn: conn.close()


def get_builder_leaderboard(limit: int = 10) -> list:
    """Returns [(user_id, username, sea, total_contributed), ...] sorted by total contributed."""
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT l.user_id, p.username, l.sea, SUM(l.amount) as total
            FROM sea_contribution_log l
            LEFT JOIN players p ON p.user_id = l.user_id
            GROUP BY l.user_id
            ORDER BY total DESC
            LIMIT ?
        ''', (limit,))
        return cursor.fetchall()
    except sqlite3.Error as e:
        logger.error(f"❌ DB Error fetching builder leaderboard: {e}")
        return []
    finally:
        if conn: conn.close()


def start_sea_project(sea_key: str, name: str, target: int, created_by: int) -> bool:
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE sea_treasury
            SET project_name = ?, project_target = ?, project_current = 0,
                project_status = 'active', project_created_by = ?
            WHERE sea = ?
        ''', (name, target, created_by, sea_key))
        conn.commit()
        return True
    except sqlite3.Error as e:
        logger.error(f"❌ DB Error starting project for sea {sea_key}: {e}")
        return False
    finally:
        if conn: conn.close()


def calculate_score(wins: int, kills: int, damage_dealt: int) -> int:
    """Calculates a player's score based on performance metrics."""
    # Simple scoring formula: points for wins, kills, and damage
    score = (wins * 100) + (kills * 10) + (damage_dealt // 10)
    return max(0, score) # Ensure score is not negative


# ✨ ======================== START MAP VOTING FUNCTION ======================== ✨
# (This was missing from previous parts)

async def start_map_voting(query: Update.callback_query, context: ContextTypes.DEFAULT_TYPE, game: Game, mode: str):
    """Initiates the map voting phase after mode selection."""
    game.mode = mode
    game.map_voting = True
    game.map_vote_end_time = datetime.now() + timedelta(seconds=30) # 30 sec voting time

    # --- Automatically add the creator to the game now ---
    # Fetch creator's details again in case username changed
    creator_user = query.from_user
    success, msg = game.add_player(
        user_id=game.creator_id,
        username=creator_user.username,
        first_name=creator_user.first_name or f"Captain_{game.creator_id}",
        team='alpha' if mode == 'team' else None # Assign to alpha by default if team mode
    )
    if not success:
         logger.error(f"❌ Critical Error: Failed to add creator {game.creator_id} to their own game in chat {game.chat_id}: {msg}")
         await safe_send(context, game.chat_id, "❌ Critical Error: Failed to initialize game creator. Cancelling.")
         if game.chat_id in games: del games[game.chat_id]
         try: await query.edit_message_caption(caption="❌ Error creating game.")
         except: pass
         return

    # --- Display Map Voting Message (Fancy UI) ---
    fancy_separator = "🗺️ • ⋅ ⋅ ────────── ⋅ ⋅ • 🗺️"
    caption = f"""
    🗳️ <b>Map Selection Commencing!</b> 🗳️

    The battle mode is set to <b>{mode.capitalize()}</b>!
    Now, vote for your preferred arena, Captains!

    Voting Closes in: <b>30 Seconds</b> ⏳

    {fancy_separator}
    <b>Available Arenas:</b>
    """
    keyboard = []
    map_options = list(MAPS.items())
    # Arrange buttons nicely (e.g., 2 per row)
    for i in range(0, len(map_options), 2):
        row = []
        map_key1, map_data1 = map_options[i]
        row.append(InlineKeyboardButton(f"{map_data1['name']}", callback_data=f"map_vote_{map_key1}"))
        if i + 1 < len(map_options):
            map_key2, map_data2 = map_options[i+1]
            row.append(InlineKeyboardButton(f"{map_data2['name']}", callback_data=f"map_vote_{map_key2}"))
        keyboard.append(row)

    caption += "\n" # Add space before buttons implicit list
    reply_markup = InlineKeyboardMarkup(keyboard)

    # --- Edit the Mode Selection Message to Show Map Voting ---
    try:
        await query.edit_message_caption(
            caption=caption,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
        # game.joining_message_id remains the same (the message being edited)
    except BadRequest as e:
        if 'message is not modified' not in str(e).lower():
            logger.warning(f"⚠️ Failed to edit message for map voting: {e}")
            # If edit fails, maybe send a new message? Less ideal.
            await safe_send(context, game.chat_id, "Error updating message. Please use buttons above if possible.", reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"❌ Unexpected error starting map voting display: {e}", exc_info=True)
        # Consider cancelling game on unexpected error
        # await safe_send(context, game.chat_id, "❌ Error starting map voting.")
        # if game.chat_id in games: del games[game.chat_id]
        return

    # --- Announce Start of Voting ---
    await safe_send(
        context, game.chat_id,
        f"📣 Voting for the <b>{escape_markdown_value(MAPS[game.map_type]['name'])}</b> arena begins now! You have 30 seconds!",
        parse_mode=ParseMode.HTML
    )

    # --- Start Countdown Task ---
    asyncio.create_task(map_voting_countdown(context, game))

# Define GAME_CONSTANTS dictionary needed for help text
GAME_CONSTANTS = {
    'MIN_PLAYERS_DEFAULT': 2, # Example, loaded from settings later
    'ATTACK_RANGE': ATTACK_RANGE,
    'ATTACK_DAMAGE': ATTACK_DAMAGE,
    'HEAL_AMOUNT': HEAL_AMOUNT,
    'DEFEND_REDUCTION': DEFEND_REDUCTION,
    'LOOT_ITEM_CAP': LOOT_ITEM_CAP,
    'AFK_TURNS_LIMIT': AFK_TURNS_LIMIT,
}

# ======================== COMMAND HANDLERS ========================

# --- ✨ Start Command ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Greets the user with a fancy welcome message. In DM, this also drives the
    mandatory registration + Origin Sea selection flow before a player can play in groups."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    if is_globally_banned(user.id): return # Silently ignore banned users
    if check_spam(user.id):
        await safe_send(context, chat_id, "⏳ Please wait a moment before using commands again.")
        return

    # Ensure player record exists
    update_player_stats(user_id=user.id, username=user.username, stats_update={})

    # 🌊 --- DM Registration + Sea Selection (private chat only) --- 🌊
    if update.effective_chat.type == 'private':
        mark_dm_registered(user.id, user.username)
        current_sea = get_player_sea(user.id)

        if not current_sea:
            # First time in DM (or hasn't picked a Sea yet) — ask them to choose one of the 4 Seas
            pick_text = build_card(
                "🌊 CHOOSE YOUR ORIGIN SEA",
                [
                    f"⚓ Welcome, Captain {mention(user.id)}!",
                    "",
                    "Before you can sail into battle, pick your permanent origin Sea.",
                    "This choice is <b>final</b> — choose wisely!",
                    "",
                ] + branch_lines([f"{info['emoji']} {info['name']}" for info in SEAS.values()]),
                emoji="🧭",
            )
            await safe_send(
                context, chat_id, pick_text,
                reply_markup=build_sea_selection_keyboard(), parse_mode=ParseMode.HTML
            )
            return
        # else: already has a Sea — fall through to normal welcome below

    # Premium Cricoverse-style welcome card
    welcome_text = build_card(
        "🚢 SHIPOVERSE",
        [
            f"⚓ Greetings, Captain {mention(user.id)}!",
            "",
            "🎮 GAME MODES",
        ] + branch_lines([
            "⚔️ Solo Battle   ➔ Free-for-all",
            "👥 Team Mode     ➔ 2-team battles",
        ]) + [
            "",
            "🚀 /creategame  📖 /help  🪪 /license",
        ],
        emoji="🚀",
    )

    keyboard = pack_buttons([
        InlineKeyboardButton("💬 Join Community", url=f"https://t.me/c/{str(SUPPORTIVE_GROUP1_ID)[4:]}/1"),
        InlineKeyboardButton("🧑‍🚀 Meet the Dev", url=f"tg://user?id={OWNER_ID}"),
    ], per_row=2)

    await safe_send_photo(
        context=context, chat_id=chat_id, photo_url=get_random_image('start'),
        caption=welcome_text, reply_markup=keyboard, parse_mode=ParseMode.HTML
    )

# 🌊 ======================== SEA SELECTION CALLBACK ======================== 🌊
async def handle_sea_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles a Captain tapping one of the 4 origin Sea buttons in DM."""
    query = update.callback_query
    user = query.from_user

    if is_globally_banned(user.id):
        await query.answer("🚫 Access Denied.", show_alert=True)
        return

    sea_key = query.data.replace('sea_pick_', '', 1)
    if sea_key not in SEAS:
        await query.answer("⚠️ Invalid Sea selection.", show_alert=True)
        return

    success = set_player_sea(user.id, sea_key)
    if not success:
        # Already assigned earlier (e.g. double tap) — just show their existing Sea
        existing = get_player_sea(user.id)
        if existing:
            await query.answer(f"You already sailed the {SEAS[existing]['name']}!", show_alert=True)
        else:
            await query.answer("⚠️ Something went wrong. Try /start again.", show_alert=True)
        return

    await query.answer(f"🌊 Welcome to the {SEAS[sea_key]['name']}!")

    sea_info = SEAS[sea_key]
    welcome_text = build_card(
        "🧭 ORIGIN SEA ASSIGNED",
        [
            f"⚓ Captain {mention(user.id)}, you now sail under the",
            f"{sea_info['emoji']} <b>{sea_info['name']}</b> {sea_info['color']}",
            "",
            "This is your permanent origin — it will appear on",
            "your profile and decides your side in Sea tournaments.",
            "",
            "👇 Join your Sea's group to coordinate with your crew:",
        ],
        emoji=sea_info['emoji'],
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"{sea_info['emoji']} Join {sea_info['name']} Group", url=sea_info['invite_link'])
    ]])

    try:
        await query.edit_message_text(welcome_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except (BadRequest, TelegramError):
        # Original message may have been a photo caption etc. — fall back to a fresh message
        await safe_send(context, user.id, welcome_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def mysea_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the Captain their assigned origin Sea, its captain, member count,
    active project progress, and a Contribute button (or prompts to pick a Sea)."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    if is_globally_banned(user.id): return
    if check_spam(user.id):
        await safe_send(context, chat_id, "⏳ Please wait a moment before using commands again.")
        return

    sea_key = get_player_sea(user.id)
    if not sea_key:
        bot_link_username = context.bot.username or BOT_USERNAME
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📡 Choose Your Sea in DM", url=f"https://t.me/{bot_link_username}?start=register")
        ]])
        await safe_send(context, chat_id, "🌊 You haven't chosen an origin Sea yet! Head to my DM and press /start.", reply_markup=keyboard)
        return

    sea_info = SEAS[sea_key]
    member_count = get_sea_member_count(sea_key)
    captain_id = get_sea_captain(sea_key)
    captain_display = mention(captain_id) if captain_id else "Unassigned — awaiting /assign"

    treasury_row = get_sea_treasury_row(sea_key)
    total_coins = treasury_row[0] if treasury_row else 0
    project_lines = ["No active project right now."]
    if treasury_row and treasury_row[4] == 'active':
        p_name, p_target, p_current = treasury_row[1], treasury_row[2], treasury_row[3]
        pct = int(min(100, (p_current / max(1, p_target)) * 100))
        project_lines = [f"🏗️ {p_name}", f"Progress : 🪙 {p_current}/{p_target} ({pct}%)"]

    lines = [
        f"⚓ Captain {mention(user.id)} sails under the",
        f"{sea_info['emoji']} <b>{sea_info['name']}</b> {sea_info['color']}",
        "",
        f"👥 Members : {member_count}",
        f"👑 Sea Captain : {captain_display}",
        f"🏦 Treasury : 🪙 {total_coins}",
        "",
        "🏝️ ACTIVE PROJECT",
    ] + branch_lines(project_lines)

    text = build_card("🧭 MY SEA", lines, emoji=sea_info['emoji'])

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{sea_info['emoji']} Open {sea_info['name']} Group", url=sea_info['invite_link'])],
        [InlineKeyboardButton("🪙 Contribute Coins", callback_data="sea_contribute_start")],
    ])
    await safe_send(context, chat_id, text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def handle_contribute_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Contribute button pressed — asks the player to type an amount next."""
    query = update.callback_query
    user_id = query.from_user.id

    sea_key = get_player_sea(user_id)
    if not sea_key:
        await query.answer("⚠️ You need to pick a Sea first via /start in DM.", show_alert=True)
        return

    await query.answer()
    context.user_data['awaiting_contribution_sea'] = sea_key
    await safe_send(
        context, query.message.chat_id,
        f"🪙 Type the amount of coins you want to contribute to the {SEAS[sea_key]['name']} (reply with a number):"
    )


async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generic text handler — currently only used to capture Sea contribution amounts."""
    if 'awaiting_contribution_sea' not in context.user_data:
        return # Not something we're waiting on — ignore

    sea_key = context.user_data.pop('awaiting_contribution_sea')
    user = update.effective_user
    chat_id = update.effective_chat.id
    raw = (update.message.text or "").strip()

    try:
        amount = int(raw)
    except ValueError:
        await safe_send(context, chat_id, "⚠️ That's not a valid number. Contribution cancelled — try /mysea again.")
        return

    success, msg, project_completed = await contribute_coins_to_sea(context, user.id, sea_key, amount)
    await safe_send(context, chat_id, msg)

    if project_completed:
        await announce_project_completion(context, sea_key, project_completed['name'])


async def announce_project_completion(context: ContextTypes.DEFAULT_TYPE, sea_key: str, project_name: str):
    """Broadcasts a project-complete announcement to the supportive group."""
    sea_info = SEAS[sea_key]
    text = (
        f"🎉 <b>PROJECT COMPLETE!</b> 🎉\n\n"
        f"{sea_info['emoji']} The <b>{sea_info['name']}</b> has finished the project "
        f"\"<b>{project_name}</b>\"! 🏝️🔨"
    )
    try:
        await safe_send(context, SUPPORTIVE_GROUP_ID, text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"❌ Failed to announce project completion: {e}")


async def builders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the top coin contributors ('builders') across all Seas."""
    chat_id = update.effective_chat.id
    top = get_builder_leaderboard(10)

    if not top:
        await safe_send(context, chat_id, "🏗️ No contributions have been made yet. Be the first builder!")
        return

    lines = []
    for i, (user_id, username, sea_key, total) in enumerate(top, start=1):
        sea_emoji = SEAS.get(sea_key, {}).get('emoji', '🌊')
        display_name = escape_markdown_value(username or f"Captain_{user_id}")
        lines.append(f"{i}. {display_name} {sea_emoji} — 🪙 {total}")

    text = build_card("🏗️ TOP BUILDERS", branch_lines(lines), emoji="🏗️")
    await safe_send(context, chat_id, text, parse_mode=ParseMode.HTML)


async def seas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows a leaderboard of all 4 Seas: members, treasury coins, and captain."""
    chat_id = update.effective_chat.id
    treasuries = get_all_sea_treasuries()

    lines = []
    for sea_key, info in SEAS.items():
        members = get_sea_member_count(sea_key)
        coins = treasuries.get(sea_key, 0)
        captain_id = get_sea_captain(sea_key)
        captain_display = mention(captain_id) if captain_id else "Unassigned"
        lines.append(f"{info['emoji']} <b>{info['name']}</b>")
        lines.extend(branch_lines([
            f"Members : {members}   Treasury : 🪙 {coins}",
            f"Captain : {captain_display}",
        ]))
        lines.append("")

    text = build_card("🌊 SEAS LEADERBOARD", lines, emoji="🌊")
    await safe_send(context, chat_id, text, parse_mode=ParseMode.HTML)


async def projects_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows/starts the calling captain's Sea project. Only the Sea Captain can start one."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    sea_key = get_player_sea(user.id)
    if not sea_key:
        await safe_send(context, chat_id, "🌊 You need to pick a Sea first via /start in DM.")
        return

    treasury_row = get_sea_treasury_row(sea_key)
    is_captain = is_sea_captain(user.id, sea_key)

    args = context.args # /projects <name> | <target_coins> -- captain only, to start a new project
    if args and is_captain:
        raw = " ".join(args)
        if '|' not in raw:
            await safe_send(context, chat_id, "⚠️ Usage: <code>/projects Lighthouse | 500</code>", parse_mode=ParseMode.HTML)
            return
        name_part, target_part = raw.split('|', 1)
        name_part = name_part.strip()
        try:
            target = int(target_part.strip())
        except ValueError:
            await safe_send(context, chat_id, "⚠️ Target must be a whole number of coins.")
            return
        if target <= 0 or not name_part:
            await safe_send(context, chat_id, "⚠️ Give a project name and a positive coin target.")
            return

        start_sea_project(sea_key, name_part, target, user.id)
        await safe_send(
            context, chat_id,
            f"🏗️ New project started for {SEAS[sea_key]['name']}: <b>{escape_markdown_value(name_part)}</b> "
            f"(Target 🪙 {target})", parse_mode=ParseMode.HTML
        )
        return
    elif args and not is_captain:
        await safe_send(context, chat_id, "🚫 Only your Sea's Captain can start a project.")
        return

    # No args — just show current project status
    sea_info = SEAS[sea_key]
    if treasury_row and treasury_row[4] == 'active':
        p_name, p_target, p_current = treasury_row[1], treasury_row[2], treasury_row[3]
        pct = int(min(100, (p_current / max(1, p_target)) * 100))
        lines = [f"🏗️ {p_name}", f"Progress : 🪙 {p_current}/{p_target} ({pct}%)"]
    else:
        lines = ["No active project.", "Contribute via /mysea once your Captain starts one!"]
        if is_captain:
            lines.append("")
            lines.append("Start one: <code>/projects Lighthouse | 500</code>")

    text = build_card(f"🏝️ {sea_info['name']} PROJECT", lines, emoji=sea_info['emoji'])
    await safe_send(context, chat_id, text, parse_mode=ParseMode.HTML)


# ✍️ ======================== OWNER: ASSIGN SEA CAPTAIN ======================== ✍️
async def assign_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only: /assign <user_id or @username> -- then bot asks which Sea to make them captain of."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    if user.id != OWNER_ID:
        await safe_send(context, chat_id, "🚫 Only the bot owner can assign Sea Captains.")
        return

    if not context.args:
        await safe_send(context, chat_id, "⚠️ Usage: <code>/assign @username</code> or <code>/assign user_id</code>", parse_mode=ParseMode.HTML)
        return

    target_raw = context.args[0].lstrip('@')
    target_id = None
    if target_raw.isdigit():
        target_id = int(target_raw)
    else:
        stats = get_player_stats_by_username(target_raw)
        if stats:
            target_id = stats[0]

    if not target_id:
        await safe_send(context, chat_id, "⚠️ Couldn't find that player. They must have started the bot at least once.")
        return

    context.user_data['assign_target_id'] = target_id
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{info['emoji']} {info['name']}", callback_data=f"assign_cap_{key}")]
        for key, info in SEAS.items()
    ])
    await safe_send(context, chat_id, f"👑 Which Sea should user <code>{target_id}</code> captain?", reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def handle_assign_captain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner picks which Sea the /assign target becomes captain of."""
    query = update.callback_query
    if query.from_user.id != OWNER_ID:
        await query.answer("🚫 Owner only.", show_alert=True)
        return

    target_id = context.user_data.pop('assign_target_id', None)
    if not target_id:
        await query.answer("⚠️ No pending assignment. Run /assign again.", show_alert=True)
        return

    sea_key = query.data.replace('assign_cap_', '', 1)
    if sea_key not in SEAS:
        await query.answer("⚠️ Invalid Sea.", show_alert=True)
        return

    set_sea_captain(sea_key, target_id)
    await query.answer(f"👑 Assigned as {SEAS[sea_key]['name']} Captain!")
    await query.edit_message_text(f"👑 User <code>{target_id}</code> is now Captain of the {SEAS[sea_key]['emoji']} {SEAS[sea_key]['name']}!", parse_mode=ParseMode.HTML)
    try:
        await safe_send(context, target_id, f"👑 Congratulations! You've been made Captain of the {SEAS[sea_key]['emoji']} {SEAS[sea_key]['name']}!")
    except Exception:
        pass


async def recruit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Captain-only: /recruit <user_id or @username> -- invites an unassigned player into the captain's Sea."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    sea_key = get_player_sea(user.id)
    if not sea_key or not is_sea_captain(user.id, sea_key):
        await safe_send(context, chat_id, "🚫 Only a Sea Captain can recruit new members.")
        return

    if not context.args:
        await safe_send(context, chat_id, "⚠️ Usage: <code>/recruit @username</code> or <code>/recruit user_id</code>", parse_mode=ParseMode.HTML)
        return

    target_raw = context.args[0].lstrip('@')
    target_id = None
    if target_raw.isdigit():
        target_id = int(target_raw)
    else:
        stats = get_player_stats_by_username(target_raw)
        if stats:
            target_id = stats[0]

    if not target_id:
        await safe_send(context, chat_id, "⚠️ Couldn't find that player. They must have started the bot at least once.")
        return

    existing_sea = get_player_sea(target_id)
    if existing_sea:
        await safe_send(context, chat_id, f"⚠️ That Captain already sails the {SEAS[existing_sea]['name']}.")
        return

    if not set_player_sea(target_id, sea_key):
        await safe_send(context, chat_id, "⚠️ Couldn't recruit — they may already have a Sea.")
        return

    sea_info = SEAS[sea_key]
    await safe_send(context, chat_id, f"✅ Recruited into the {sea_info['emoji']} {sea_info['name']}!")
    try:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"{sea_info['emoji']} Join {sea_info['name']} Group", url=sea_info['invite_link'])
        ]])
        await safe_send(context, target_id, f"🌊 You've been recruited into the {sea_info['emoji']} {sea_info['name']} by your Captain!", reply_markup=keyboard)
    except Exception:
        pass


# --- 📚 Help Command ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays the main help menu with category buttons."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    if is_globally_banned(user.id): return
    if check_spam(user.id):
        await safe_send(context, chat_id, "⏳ Please wait a moment before using commands again.")
        return

    help_text = "📚 <b>Bot Command Manual</b>\n\nChoose a category to explore the available commands:"

    keyboard = [
        [InlineKeyboardButton("🎮 Game Actions", callback_data="help_game")],
        [InlineKeyboardButton("📊 In-Game Info", callback_data="help_info")],
        [InlineKeyboardButton("🏆 Player Profile & Global", callback_data="help_global")],
        [InlineKeyboardButton("🛡️ Admin & Settings", callback_data="help_settings")],
        [InlineKeyboardButton("🚀 How to Play Guide", callback_data="help_howtoplay")],
        [InlineKeyboardButton("💎 About Loot Items", callback_data="help_lootinfo")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await safe_send(context, chat_id, help_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

# 📚 ======================== HELP COMMAND CALLBACKS ======================== 📚

async def help_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles button presses from the main /help menu (Fancy UI)."""
    query = update.callback_query
    user_id = query.from_user.id

    if is_globally_banned(user_id):
        await query.answer("🚫 Access Denied: You are banned.", show_alert=True)
        return

    await query.answer() # Acknowledge tap

    category = query.data # e.g., "help_game"
    text = "" # Initialize empty reply text
    header_line = "═" * 25 # Fancy separator line

    # --- Generate Text based on Category ---
    if category == "help_game":
        text = f"""
🎮 <b>Game Actions</b> (Group Only)
{header_line}
Commands for starting and playing the game in a group chat.

- /creategame : Begin setting up a new battle.
- /join : Enter a game during the join phase (Solo). Use buttons for Team mode.
- /leave : Exit a game during the join phase.
- /cancel : Same function as /leave.
- /spectate : Observe an ongoing game (if allowed).
- /ally <code>@username</code> or reply: Form a temporary alliance (Solo Mode).
- /betray : Break your alliance for a tactical advantage (Solo Mode).
- /selectmap <code><map_name></code>: Vote for map via text (alternative).
"""
    elif category == "help_info":
        text = f"""
📊 <b>In-Game Information</b>
{header_line}
Check game status and your ship's condition during battle.

- /map : View the current battlefield map (ships, safe zone).
- /position : Get your ship's coordinates (Row, Column).
- /myhp : Check your current health points (HP).
- /inventory : List items held in your cargo ({LOOT_ITEM_CAP} item limit).
- /ranking : See the current rank of surviving players.
- /dailystats : Detailed overview of the current game day (alias: /stats in group).
"""
    elif category == "help_global":
        text = f"""
🏆 <b>Player Profile & Global Features</b>
{header_line}
Commands related to your overall progress and bot-wide interactions.

- /license : View your full profile — stats, Sea, rank & coins.
- /leaderboard : See the Top 10 captains globally.
- /achievements : Check your unlocked medals and honors.
- /compare <code>@username</code> or reply: Compare your stats with another captain.
- /tips : Receive a random strategic hint.
- /daily : Claim your daily login coin reward. 🪙
- /shop : Browse and purchase prestigious titles. ✨
- /history : Review the outcomes of recent battles in this chat.
- /challenges : See current daily objectives for bonus coins.
- /cosmetics : View available ship customizations (visual only).
"""
    elif category == "help_settings":
        admin_list_str = ", ".join([f"<code>{admin_id}</code>" for admin_id in ADMIN_IDS])
        text = f"""
🛡️ <b>Admin & Settings Commands</b>
{header_line}
Configure game rules for groups (Group Admins) or manage the bot (Owner).

<b>Group Admin Commands:</b>
- /settings : Open the settings panel (tap buttons to change join time, action time, min players, spectators).
- /tutorial : Step-by-step guide on how to play.
- 🚀 Force Start : Button on the muster board (admin-only, asks for confirmation) to skip the join timer.
- /extend : Add 30 seconds to the current join timer.
- /endgame : Immediately terminate the current game in this group.

<b>Bot Owner Only (ID: <code>{OWNER_ID}</code>):</b>
- /broadcast (reply): Send a message to all bot users.
- /ban <code>@username</code> or reply: Globally ban a user from the bot.
- /unban <code>@username</code> or reply: Lift a global ban.
- /export : Get a JSON backup of the player database (via DM).
- /restore (reply to file): Restore player data from a JSON backup.
- /stats : View bot usage statistics (Owner & Bot Admins: {admin_list_str}).
"""
    elif category == "help_howtoplay":
        text = f"""
🚀 <b>How to Play Guide</b>
{header_line}
Your quick start manual for Ship Battle Royale!

1.  <b>Initiate:</b> A Group Admin uses <code>/creategame</code>.
2.  <b>Setup:</b> Creator chooses Mode (Solo/Team), players vote on the Map.
3.  <b>Boarding:</b> Use <code>/join</code> (Solo) or Team buttons to enter before time runs out! ({GAME_CONSTANTS['MIN_PLAYERS_DEFAULT']} players needed).
4.  <b>Engage:</b> The battle starts (Day 1).
5.  <b>Orders (via DM):</b> Each Day, check your private message from the bot and choose an action:
    - <b>Attack:</b> Damage ships in range ({ATTACK_RANGE} squares).
    - <b>Heal:</b> Repair {HEAL_AMOUNT[0]}-{HEAL_AMOUNT[1]} HP.
    - <b>Defend:</b> Reduce incoming damage by {int(DEFEND_REDUCTION*100)}%.
    - <b>Move:</b> Navigate one square (Up, Down, Left, Right).
    - <b>Loot:</b> Scavenge for items (Max {LOOT_ITEM_CAP} hold). Energy items used instantly!
6.  <b>Resolution:</b> Actions process simultaneously after the timer.
7.  <b>Report:</b> Check the group chat for the Day Summary.
8.  <b>Zone Shrink:</b> The Safe Zone (<code>🟢</code>) shrinks on Days {', '.join(map(str, SAFE_ZONE_SCHEDULE.keys()))}! Avoid the Danger Zone (<code>🔴</code>) or take {SAFE_ZONE_DAMAGE} damage.
9.  <b>AFK Penalty:</b> Miss {AFK_TURNS_LIMIT} turns = Elimination! Stay active!
10. <b>Victory:</b> Be the last ship or team afloat!

May the stars guide your aim! ✨
"""
    elif category == "help_lootinfo":
        text = f"""
💎 <b>About Loot Items</b>
{header_line}
Gain the upper hand by finding powerful items! Use the <b>Loot</b> action.

<b>Inventory Limit:</b>
- You can hold <b>{LOOT_ITEM_CAP}</b> items max (Weapons, Shields, Utilities).
- Energy items (⚡💚🩺) are used instantly and don't count towards the limit.
- If full, use an item to make space before looting non-energy items.

<b>Item Categories:</b>
- <b>Weapons</b> (🔫💥🌟⚡): Used automatically on your next Attack for bonus damage. One use per item.
- <b>Shields</b> (🛡️🏰✨🪞): Used automatically when you are attacked, reducing damage. One use per item.
- <b>Energy</b> (⚡✨💚🩺): Instantly restore HP upon looting.
- <b>Utilities</b> (👻💣🌀📡💨): Provide various tactical effects (some automatic, some WIP features). Check descriptions!

Loot wisely, manage your cargo, and dominate! 🎒
"""
    else:
        text = "❓ Unknown help category."

    # --- Add Back Button ---
    keyboard = [[InlineKeyboardButton("◀️ Back to Categories", callback_data="help_main")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # --- Edit the message ---
    try:
        await query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    except BadRequest as e:
        if 'message is not modified' not in str(e).lower():
            logger.warning(f"⚠️ Failed to edit help message: {e}")
            # Fall back to sending a fresh message instead of leaving the user stuck
            await safe_send(context, query.message.chat_id, text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"❌ Unexpected error editing help: {e}", exc_info=True)
        await safe_send(context, query.message.chat_id, text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def help_main_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the 'Back' button, returning to the main help category view."""
    query = update.callback_query
    user_id = query.from_user.id

    if is_globally_banned(user_id):
        await query.answer("🚫 Access Denied: You are banned.", show_alert=True)
        return

    await query.answer()

    # --- Restore Main Help Text & Buttons ---
    help_text = "📚 <b>Bot Command Manual</b>\n\nChoose a category to explore the available commands:"
    keyboard = [
        [InlineKeyboardButton("🎮 Game Actions", callback_data="help_game")],
        [InlineKeyboardButton("📊 In-Game Info", callback_data="help_info")],
        [InlineKeyboardButton("🏆 Player Profile & Global", callback_data="help_global")],
        [InlineKeyboardButton("🛡️ Admin & Settings", callback_data="help_settings")],
        [InlineKeyboardButton("🚀 How to Play Guide", callback_data="help_howtoplay")],
        [InlineKeyboardButton("💎 About Loot Items", callback_data="help_lootinfo")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # --- Edit the message ---
    try:
        await query.edit_message_text(
            text=help_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    except BadRequest as e:
        if 'message is not modified' not in str(e).lower():
            logger.warning(f"⚠️ Failed to edit back to main help menu: {e}")
            await safe_send(context, query.message.chat_id, help_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"❌ Unexpected error editing back to main help: {e}", exc_info=True)
        await safe_send(context, query.message.chat_id, help_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

# --- 📜 Rules Command ---
async def tutorial_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step-by-step guide for brand new Captains on how to actually play."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    if is_globally_banned(user.id): return
    if check_spam(user.id):
        await safe_send(context, chat_id, "⏳ Please wait a moment before commands.")
        return

    sep = "🧭 • ⋅ ⋅ ────────── ⋅ ⋅ • 🧭"

    tutorial_text = f"""
🧭 <b>Captain's Tutorial — Getting Started</b> 🧭

{sep}

<b>Step 1 — Register in DM</b>
Message me directly and send /start. Pick your origin Sea (Storm, Emerald, Crimson, or Abyss). This is one-time and permanent.

<b>Step 2 — Join a battle</b>
In any group with the bot, use /creategame (or wait for one) then tap <b>Join Battle</b> or send /join before the timer runs out.

<b>Step 3 — Issue daily orders (in DM!)</b>
Once the game starts, I'll DM you a command console each day. Tap a button to choose:
  ⚔️ Attack · 🛡️ Defend · 🔧 Repair · 🎒 Loot · 🧭 Move
You get one action per day — pick wisely before the timer runs out.

<b>Step 4 — Track the battle</b>
  /map — see the interactive battle grid (DM only)
  /position — check your sector & nearby ships
  /myhp — check your health (DM only)
  /inventory — see items you've looted

<b>Step 5 — Survive & win</b>
The safe zone shrinks over time — staying outside it deals damage. Be the last ship standing (or last team alive in Team Mode) to win coins, XP, and glory!

{sep}

Need the full rules? Use /rules. Need the full command list? Use /help.
"""
    await safe_send(context, chat_id, tutorial_text, parse_mode=ParseMode.HTML)


async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays the core game rules with a fancy UI."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    if is_globally_banned(user.id): return
    if check_spam(user.id):
        await safe_send(context, chat_id, "⏳ Please wait a moment before commands.")
        return

    # Fancy Separator
    rules_separator = "📜 • ⋅ ⋅ ────────── ⋅ ⋅ • 📜"

    # Fetch a few loot examples dynamically
    loot_examples = []
    i = 0
    for key, data in LOOT_ITEMS.items():
        if data['type'] != 'energy': # Show items that take inventory slots
            loot_examples.append(f"  {get_rarity_color(data['rarity'])} {data['emoji']} {key.replace('_',' ').title()}: {data['desc']}")
            i += 1
            if i >= 3: break # Limit examples shown

    rules_text = f"""
📜 <b>Ship Battle Royale - Rules of Engagement</b> 📜

<b>Objective:</b> Annihilate all opposition and be the last vessel operational!

{rules_separator}

<b>Core Actions</b> (Select one each Day via DM):
  💥 <b>Attack:</b> Engage targets within {ATTACK_RANGE} squares ({ATTACK_DAMAGE[0]}-{ATTACK_DAMAGE[1]} Base DMG).
  🛡️ <b>Defend:</b> Brace for impact! Reduce incoming damage by {int(DEFEND_REDUCTION*100)}%.
  🔧 <b>Heal:</b> Conduct emergency repairs, restoring {HEAL_AMOUNT[0]}-{HEAL_AMOUNT[1]} HP.
  🎒 <b>Loot:</b> Scavenge the battlefield for items (Max {LOOT_ITEM_CAP} held).
  🧭 <b>Move:</b> Reposition your ship one square (Up, Down, Left, Right).

{rules_separator}

🌀 <b>The Constricting Void (Safe Zone):</b>
  The battlefield shrinks! Watch the map (<code>/map</code>).
  Safe Zone: <code>🟢</code> | Danger Zone: <code>🔴</code>
  Being in the Danger Zone after it shrinks inflicts {SAFE_ZONE_DAMAGE} damage each turn!
  Shrinks occur on Days: {', '.join(map(str, SAFE_ZONE_SCHEDULE.keys()))}.

{rules_separator}

🤝 <b>Alliances & Betrayal</b> (Solo Mode Only):
  <code>/ally @user</code>: Form a {ALLIANCE_DURATION}-turn truce. Cannot attack allies.
  <code>/betray</code>: Sever ties! Your next attack gains a +{int((BETRAYAL_DAMAGE_BONUS-1)*100)}% damage bonus! 😈

{rules_separator}

⚠️ <b>Important Notes:</b>
  - <b>AFK:</b> Missing {AFK_TURNS_LIMIT} consecutive turns results in elimination!
  - <b>HP:</b> Reaching 0 HP means your ship is destroyed.
  - <b>Items:</b> Use <code>/inventory</code> to check your loot. Max {LOOT_ITEM_CAP}!
    Examples:
{chr(10).join(loot_examples)}

{rules_separator}

🪙 <b>Rewards & Progression:</b>
  Earn Coins for participation and victories ({WIN_COIN_BONUS} Coins for winning!).
  Claim free Coins daily with <code>/daily</code>.
  Purchase fancy Titles in the <code>/shop</code> to show off!

{rules_separator}

Now go forth and claim your stellar victory! ✨
"""

    await safe_send_photo(
        context=context, chat_id=chat_id, photo_url=get_random_image('rules'),
        caption=rules_text, parse_mode=ParseMode.HTML
    )


# ✨ ======================== GAME CREATION COMMAND ======================== ✨

async def creategame_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiates the game creation process in a group chat (Fancy UI)."""
    user = update.effective_user
    chat = update.effective_chat

    # --- Pre-Checks ---
    if chat.type == 'private':
        await safe_send(context, chat.id, "⚔️ Battles can only be initiated in group chats!")
        return
    if is_globally_banned(user.id):
        # Maybe send a message here? Or just ignore silently.
        logger.warning(f"🚫 Banned user {user.id} tried /creategame in {chat.id}")
        return
    if check_spam(user.id):
        await safe_send(context, chat.id, "⏳ Please wait a moment before starting a new game.")
        return

    chat_id = chat.id

    # --- Check for Existing Game ---
    if chat_id in games:
        if games[chat_id].is_active:
            await safe_send(context, chat_id, "⏳ A battle is already in progress! Use <code>/spectate</code> or wait for it to finish.")
            return
        elif games[chat_id].is_joining or games[chat_id].map_voting:
             await safe_send(context, chat_id, "⏳ A game setup is already underway!")
             return
        else:
             # Clean up potential stale game object
             logger.warning(f"Removing stale game object for chat {chat_id} before creating new one.")
             del games[chat_id]

    # --- Create New Game Object ---
    creator_name = user.first_name or f"Captain_{user.id}"
    game = Game(chat_id, user.id, creator_name)
    games[chat_id] = game # Add to global state

    # --- Mode Selection Message ---
    fancy_separator = "✨ • ⋅ ⋅ ────────── ⋅ ⋅ • ✨"
    caption = f"""
    🚀 <b>New Battle Initiative!</b> 🚀

    Captain {escape_markdown_value(creator_name)} is assembling a fleet!

    Choose the rules of engagement:

    {fancy_separator}

    ⚔️ <b>Solo Mode:</b> Every captain for themselves! Last ship standing reigns supreme.

    🤝 <b>Team Mode:</b> Form squadrons! Alpha (🔵) vs Beta (🔴) clash for dominance.

    {fancy_separator}

    Select the mode below to proceed to map voting! 👇
    """
    keyboard = [
        [InlineKeyboardButton("⚔️ Solo Combat", callback_data=f"mode_solo_{chat_id}")],
        [InlineKeyboardButton("🤝 Team Skirmish", callback_data=f"mode_team_{chat_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # --- Send Initial Message ---
    sent_msg = await safe_send_animation(
        context, chat_id, get_random_gif('joining'),
        caption=caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML
    )

    if sent_msg:
        # Store message ID for later editing (into map voting, then joining phase)
        game.joining_message_id = sent_msg.message_id
    else:
        # If sending failed, clean up the game object
        logger.error(f"❌ Failed to send initial creategame message for chat {chat_id}. Aborting.")
        if chat_id in games: del games[chat_id]
        await safe_send(context, chat_id, "❌ Error starting game creation. Please try again.")
        return

    # Optional: Log game creation to support group
    try:
        support_group_message = f"🎮 New Game Creation Started!\nGroup: {escape_markdown_value(chat.title)} ({chat_id})\nCreator: {escape_markdown_value(creator_name)} ({user.id})"
        await context.bot.send_message(SUPPORTIVE_GROUP_ID, support_group_message, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"⚠️ Failed to log game creation to support group {SUPPORTIVE_GROUP_ID}: {e}")


# ✨ ======================== IN-GAME STATS COMMAND ======================== ✨

async def stats_detailed_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows detailed statistics about the currently active game (Fancy UI). Alias: /stats in group"""
    chat = update.effective_chat
    user = update.effective_user # For ban check if needed, though usually not critical for info cmd

    # --- Pre-Checks ---
    if chat.type == 'private':
        await safe_send(context, chat.id, "📊 Game stats can only be viewed within the group chat during a battle.")
        return
    # Optional: Ban check if desired for info commands
    # if is_globally_banned(user.id): return

    chat_id = chat.id
    if chat_id not in games or not games[chat_id].is_active:
        await safe_send(context, chat_id, "📊 No active battle found in this chat to show stats for.")
        return

    game = games[chat_id]
    fancy_separator = "📊 • ⋅ ⋅ ────────── ⋅ ⋅ • 📊"

    # --- Gather Stats ---
    alive_count = len(game.get_alive_players())
    total_players = len(game.players)
    map_info = MAPS.get(game.map_type, {'name': 'Unknown Map', 'emoji': '❓'})
    time_left_str = "N/A"
    if game.operation_end_time:
        remaining_sec = max(0, (game.operation_end_time - datetime.now()).total_seconds())
        time_left_str = format_time(remaining_sec)

    # Top Killers
    killers = []
    for uid, p_data in game.players.items():
        if p_data.get('alive'):
            killers.append({'id': uid, 'name': p_data['first_name'], 'kills': p_data['stats'].get('kills', 0)})
    sorted_killers = sorted(killers, key=lambda k: k['kills'], reverse=True)[:3] # Top 3

    killers_display = []
    medals = ["🥇", "🥈", "🥉"]
    if sorted_killers:
        for i, k_info in enumerate(sorted_killers):
            if k_info['kills'] > 0: # Only show if they have kills
                 medal = medals[i] if i < len(medals) else "🔹"
                 killers_display.append(f"  {medal} {escape_markdown_value(k_info['name'])}: {k_info['kills']} Eliminations")
    if not killers_display:
        killers_display.append("  No eliminations recorded yet.")

    # Next Safe Zone Shrink
    next_shrink_day_str = "None Scheduled"
    for day in sorted(SAFE_ZONE_SCHEDULE.keys()):
        if day > game.day:
            next_shrink_day_str = f"Day {day}"
            break
    if game.safe_zone_radius == 0:
        next_shrink_day_str = "Fully Collapsed"


    # --- Assemble Text ---
    stats_text = f"""
    📊 <b>Battle Report - Day {game.day}</b> 📊

    <b>Arena:</b> {escape_markdown_value(map_info['name'])}
    <b>Status:</b> {alive_count} / {total_players} Ships Operational
    <b>Time Until Next Phase:</b> {time_left_str}

    {fancy_separator}

    🌀 <b>Safe Zone:</b>
      Current Radius: {'Full Map' if game.safe_zone_radius > game.map_size*2 else f'{game.safe_zone_radius} blocks'}
      Next Shrink: {next_shrink_day_str}

    {fancy_separator}

    🏆 <b>Top Captains (Eliminations):</b>
{chr(10).join(killers_display)}

    {fancy_separator}

    Use <code>/map</code>, <code>/ranking</code>, <code>/myhp</code> for more details.
    """

    await safe_send(context, chat_id, stats_text, parse_mode=ParseMode.HTML)

# ✨ ======================== ALLIANCE & BETRAYAL COMMANDS ======================== ✨

async def ally_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forms a temporary alliance with another player (Solo Mode)."""
    user = update.effective_user
    chat = update.effective_chat

    # --- Pre-Checks ---
    if chat.type == 'private':
        await safe_send(context, chat.id, "🤝 Alliances can only be formed within group battles!")
        return
    if is_globally_banned(user.id): return

    chat_id = chat.id
    if chat_id not in games or not games[chat_id].is_active:
        await safe_send(context, chat_id, "🤝 No active battle found to form an alliance in.")
        return

    game = games[chat_id]
    if game.mode != 'solo':
        await safe_send(context, chat_id, "🤝 Alliances are only available in Solo mode battles.")
        return

    player_data = game.players.get(user.id)
    if not player_data or not player_data.get('alive'):
        await safe_send(context, chat_id, "🤷 You need to be an active participant to form an alliance.")
        return

    if user.id in game.alliances:
        ally_id = game.alliances[user.id]['ally']
        ally_name = escape_markdown_value(game.players.get(ally_id, {}).get('first_name', 'Unknown'))
        await safe_send(context, chat_id, f"🤝 You are already allied with {ally_name}.")
        return

    # --- Determine Target ---
    target_id = None
    target_name = None
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        # Prevent targeting self via reply
        if target_user.id == user.id:
             await safe_send(context, chat_id, "😅 You cannot form an alliance with yourself, Captain!")
             return
        # Check if replied user is in the game and alive
        target_data = game.players.get(target_user.id)
        if target_data and target_data.get('alive'):
            target_id = target_user.id
            target_name = target_user.first_name or f"Captain_{target_id}"
        else:
             await safe_send(context, chat_id, "❓ The captain you replied to is not currently active in this battle.")
             return
    elif context.args:
        username_arg = context.args[0].replace('@', '')
        found = False
        for p_id, p_data in game.players.items():
             # Check username match and ensure not self and alive
             if (p_data.get('username') and p_data['username'].lower() == username_arg.lower() and
                     p_id != user.id and p_data.get('alive')):
                 target_id = p_id
                 target_name = p_data['first_name'] or f"Captain_{target_id}"
                 found = True
                 break
        if not found:
             await safe_send(context, chat_id, f"❓ Captain '@{escape_markdown_value(username_arg)}' not found among active participants.")
             return
    else:
        await safe_send(context, chat_id, "🤝 <b>How to Ally:</b> Reply to a player's message with <code>/ally</code> or use <code>/ally @username</code>.")
        return

    # --- Final Checks on Target ---
    if target_id in game.alliances:
        await safe_send(context, chat_id, f"⏳ {escape_markdown_value(target_name)} is already allied with someone else.")
        return

    # --- Form Alliance ---
    game.form_alliance(user.id, target_id)
    safe_user_name = escape_markdown_value(user.first_name or f"Captain_{user.id}")
    safe_target_name = escape_markdown_value(target_name)

    # Update stats (global)
    update_player_stats(user.id, user.username, {'alliances_formed': 1})
    update_player_stats(target_id, game.players[target_id]['username'], {'alliances_formed': 1}) # Update target's stats too

    await safe_send(
        context, chat_id,
        f"🤝 <b>Alliance Forged!</b> 🤝\nCaptain {safe_user_name} and Captain {safe_target_name} have formed a truce for the next {ALLIANCE_DURATION} days!\n(Attacks between allies are disabled)",
        parse_mode=ParseMode.HTML
    )

    # Achievement check (Diplomat) - Check updated global stats
    if (get_player_stats(user.id) or [0]*16)[15] >= 10: # Index 15 is alliances_formed
         if unlock_achievement(user.id, 'diplomat'):
              await safe_send(context, user.id, "🕊️ Achievement Unlocked: <b>Diplomat</b> - Formed 10 alliances!")


async def betray_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Breaks the user's current alliance (Solo Mode)."""
    user = update.effective_user
    chat = update.effective_chat

    # --- Pre-Checks ---
    if chat.type == 'private':
        await safe_send(context, chat.id, "💔 Betrayal only happens in the heat of group battles!")
        return
    if is_globally_banned(user.id): return

    chat_id = chat.id
    if chat_id not in games or not games[chat_id].is_active:
        await safe_send(context, chat_id, "💔 No active battle found to commit betrayal in.")
        return

    game = games[chat_id]
    if game.mode != 'solo':
        await safe_send(context, chat_id, "💔 Betrayal is a concept for Solo mode battles.")
        return

    if user.id not in game.alliances:
        await safe_send(context, chat_id, "🤷 You have no active alliance to betray.")
        return

    # --- Break Alliance ---
    former_ally_id = game.break_alliance(user.id) # This removes entries for both
    if former_ally_id and former_ally_id in game.players:
        former_ally_name = escape_markdown_value(game.players[former_ally_id]['first_name'])
        betrayer_name = escape_markdown_value(user.first_name)

        # Update betrayer's global stats
        update_player_stats(user.id, user.username, {'betrayals': 1})

        await safe_send(
            context, chat_id,
            f"😈 <b>Betrayal!</b> 😈\nCaptain {betrayer_name} has broken their truce with Captain {former_ally_name}!\nTheir next attack gains a <b>+{int((BETRAYAL_DAMAGE_BONUS-1)*100)}% damage bonus</b>!",
            parse_mode=ParseMode.HTML
        )

        # Notify the betrayed player via DM
        await safe_send(context, former_ally_id, f"⚠️ <b>Alliance Broken!</b> ⚠️\nYour ally {betrayer_name} has betrayed you in the group chat battle! Watch your six!")

        # Achievement Check (Betrayer) - Check updated global stats
        if (get_player_stats(user.id) or [0]*15)[14] == 1: # Index 14 is betrayals
             if unlock_achievement(user.id, 'betrayer'):
                  await safe_send(context, user.id, "😈 Achievement Unlocked: <b>Backstabber</b> - Committed your first betrayal!")

    else:
        # Should ideally not happen if break_alliance worked, but as a fallback
        logger.warning(f"⚠️ Betrayal command used by {user.id} in chat {chat_id}, but former ally ID {former_ally_id} could not be resolved.")
        await safe_send(context, chat_id, "⚠️ Alliance broken, but encountered an issue identifying the former ally.")

# ✨ ======================== MAP SELECTION COMMAND (Fallback) ======================== ✨

async def selectmap_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows players to vote for a map using text during the map voting phase."""
    user = update.effective_user
    chat = update.effective_chat

    # --- Pre-Checks ---
    if chat.type == 'private':
        await safe_send(context, chat.id, "🗺️ Map voting only happens in group chats.")
        return
    if is_globally_banned(user.id): return

    chat_id = chat.id
    if chat_id not in games:
        await safe_send(context, chat_id, "🗳️ No map voting is currently active.")
        return

    game = games[chat_id]

    # Check if map voting phase is active
    if not game.map_voting:
        await safe_send(context, chat_id, "⏳ Map voting has already ended for this game.")
        return

    # Check if user is part of the game (allowed to vote)
    if user.id not in game.players:
        await safe_send(context, chat_id, "✋ You need to be part of the game setup to vote. The creator is added automatically.")
        return

    # --- Process Vote ---
    if not context.args:
        map_list = ", ".join([f"<code>{k}</code>" for k in MAPS.keys()])
        await safe_send(context, chat_id, f"🗳️ <b>Usage:</b> <code>/selectmap <map_name></code>\nAvailable maps: {map_list}", parse_mode=ParseMode.HTML)
        return

    chosen_map_key = context.args[0].lower() # Get the first argument as the map key

    if chosen_map_key not in MAPS:
        map_list = ", ".join([f"<code>{k}</code>" for k in MAPS.keys()])
        await safe_send(context, chat_id, f"❓ <b>Invalid Map:</b> '{escape_markdown_value(chosen_map_key)}'.\nChoose from: {map_list}", parse_mode=ParseMode.HTML)
        return

    # --- Record Vote ---
    game.map_votes[user.id] = chosen_map_key
    map_name = MAPS[chosen_map_key]['name']
    await safe_send(context, chat_id, f"🗳️ Captain {escape_markdown_value(user.first_name)} voted for <b>{escape_markdown_value(map_name)}</b>!")

    # --- Update Vote Counts (Optional feedback) ---
    vote_counts = defaultdict(int)
    for vote in game.map_votes.values(): vote_counts[vote] += 1
    votes_display = "\n".join([f"  {MAPS[mk]['name']}: {count} vote{'s' if count > 1 else ''}" for mk, count in sorted(vote_counts.items())])
    await safe_send(context, chat_id, f"Current Votes:\n{votes_display}", parse_mode=ParseMode.HTML)

    # Note: This doesn't update the *buttons* on the original message, only sends text feedback.
    # The button handler (<code>handle_map_vote</code>) is the primary way intended for voting.

# ✨ ======================== CHALLENGES COMMAND ======================== ✨

async def challenges_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays the daily challenges available for bonus coins (Fancy UI)."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    if is_globally_banned(user.id): return # Silently ignore
    # No spam check needed usually for info commands

    # --- Define Daily Challenges ---
    # (You can make this dynamic later, e.g., load from DB or rotate daily)
    challenges = {
        'first_kill': {'name': 'First Blood', 'desc': 'Score your first elimination in any game mode.', 'reward': 50, 'emoji': '🩸'},
        'triple_kill': {'name': 'Triple Threat', 'desc': 'Achieve 3 eliminations in a single battle.', 'reward': 150, 'emoji': '🔥'},
        'survivor': {'name': 'Sole Survivor', 'desc': 'Win a Solo Mode battle royale.', 'reward': 200, 'emoji': '👑'},
        'item_hoarder': {'name': 'Resourceful Captain', 'desc': 'Collect 5 or more loot items in one game.', 'reward': 100, 'emoji': '🎒'}, # Changed desc slightly
        'medic_duty': {'name': 'Field Repairs', 'desc': 'Heal a total of 150 HP in a single game.', 'reward': 75, 'emoji': '🔧'}, # Changed desc slightly
    }

    # --- Assemble Fancy Text ---
    fancy_separator = "🎯 • ⋅ ⋅ ────────── ⋅ ⋅ • 🎯"
    text = f"""
    🎯 <b>Daily Directives</b> 🎯

    Complete these objectives today for bonus Coin rewards!

    {fancy_separator}
    """

    # Add each challenge to the text
    for key, challenge_data in challenges.items():
        # TODO: Add logic here later to check if the user has *already* completed this challenge today
        completion_status = "⏳" # Placeholder for 'In Progress'
        text += f"\n{completion_status} {challenge_data['emoji']} <b>{challenge_data['name']}</b>\n"
        text += f"    Objective: {challenge_data['desc']}\n"
        text += f"    Reward: {challenge_data['reward']} 🪙\n"

    text += f"""
    {fancy_separator}
    New directives arrive daily. Good luck, Captain! ✨
    """

    # --- Send Message ---
    # Use a relevant image if you have one, otherwise default
    await safe_send_photo(
        context=context, chat_id=chat_id,
        photo_url=get_random_image('default'), # Consider adding an 'IMAGES['challenges']' entry
        caption=text,
        parse_mode=ParseMode.HTML
    )


# ✨ ======================== COSMETICS COMMAND ======================== ✨

async def cosmetics_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays available cosmetic items (currently visual only) (Fancy UI)."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    if is_globally_banned(user.id): return # Silently ignore
    # No spam check needed usually

    # Get user's coins to show affordability
    stats = get_player_stats(user.id)
    if not stats: # Register if needed
        update_player_stats(user.id, user.username, {})
        stats = get_player_stats(user.id)

    user_coins = get_player_coins(user.id) # Get current coins safely

    # --- Define Available Cosmetics ---
    # (Functionality for equipping/using these is not implemented in this version)
    cosmetics = {
        'ship_skin_red': {'name': '🔴 Red Fury Skin', 'desc': 'Fiery red paint job.', 'cost': 500, 'rarity': 'rare'},
        'ship_skin_blue': {'name': '🔵 Frost Viper Skin', 'desc': 'Icy blue camouflage.', 'cost': 500, 'rarity': 'rare'},
        'ship_skin_gold': {'name': '🟡 Golden Aegis Skin', 'desc': 'Gleaming gold plating.', 'cost': 2000, 'rarity': 'legendary'},
        'trail_fire': {'name': '🔥 Blazing Trail', 'desc': 'Leave a fiery engine trail.', 'cost': 750, 'rarity': 'epic'},
        'trail_ice': {'name': '❄️ Cryo Trail', 'desc': 'Leave a frosty engine trail.', 'cost': 750, 'rarity': 'epic'},
    }

    # --- Assemble Fancy Text ---
    fancy_separator = "🎨 • ⋅ ⋅ ────────── ⋅ ⋅ • 🎨"
    text = f"""
    🎨 <b>Ship Customization Bay</b> 🎨

    View available cosmetic upgrades for your vessel!
    (Note: Equipping functionality is under development.)

    🪙 <b>Your Balance:</b> {user_coins} Coins

    {fancy_separator}
    <b>Available Cosmetics:</b>
    """

    # Add each cosmetic item
    if not cosmetics:
        text += "\n  No cosmetic items currently available."
    else:
        for key, cosmetic_data in cosmetics.items():
            cost = cosmetic_data['cost']
            rarity_color = get_rarity_color(cosmetic_data['rarity'])
            # Check affordability
            status = ""
            if user_coins >= cost:
                status = f" ({cost} 🪙 - ✅ Affordable)"
            else:
                status = f" ({cost} 🪙 - 🔒 Needs {cost - user_coins} more)"

            text += f"\n{rarity_color} <b>{cosmetic_data['name']}</b>{status}\n"
            text += f"    Description: {cosmetic_data['desc']}\n"

    text += f"\n{fancy_separator}\nMore customizations coming soon! ✨"

    # --- Send Message ---
    # Use a relevant image if you have one, otherwise default
    await safe_send_photo(
        context=context, chat_id=chat_id,
        photo_url=get_random_image('shop'), # Use shop image or create a 'cosmetics' one
        caption=text,
        parse_mode=ParseMode.HTML
    )

# ✨ ======================== EXTEND JOINING TIME COMMAND ======================== ✨

async def extend_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Extends the current joining phase timer by 30 seconds (Group Admin)."""
    user = update.effective_user
    chat = update.effective_chat

    # --- Pre-Checks ---
    if chat.type == 'private':
        await safe_send(context, chat.id, "⏱️ This command only works in group chats during the joining phase.")
        return
    if is_globally_banned(user.id): return # Silently ignore banned

    chat_id = chat.id
    if chat_id not in games:
        await safe_send(context, chat_id, "⏱️ No game is currently in the joining phase to extend.")
        return

    game = games[chat_id]

    if not game.is_joining:
        await safe_send(context, chat_id, "⏱️ Can only extend time during the joining phase.")
        return

    if not await is_admin_or_owner(context, chat_id, user.id):
        await safe_send(context, chat_id, "🚫 Only Group Admins can extend the joining time.")
        return

    # --- Extend Time ---
    if game.join_end_time:
        game.join_end_time += timedelta(seconds=30)
        new_remaining_sec = max(0, (game.join_end_time - datetime.now()).total_seconds())
        logger.info(f"⏳ Joining time extended by 30s in chat {chat_id} by admin {user.id}. New end time: {game.join_end_time}")

        await safe_send_animation(
            context, chat_id, get_random_gif('extend'),
            caption=f"⏳ <b>Time Extended!</b> ⏳\nCaptain {escape_markdown_value(user.first_name)} added 30 seconds to the joining phase!\nNew Time Left: <b>{format_time(new_remaining_sec)}</b>",
            parse_mode=ParseMode.HTML
        )

        # --- Trigger Joining Message Update ---
        # Update the pinned message immediately to show the new time
        mock_message = type('obj', (object,), {'message_id': game.joining_message_id, 'chat_id': chat_id})
        try:
            if game.mode == 'team':
                await display_team_joining_phase(mock_message, context, game, edit=True)
            else:
                await display_joining_phase(mock_message, context, game, edit=True)
        except Exception as e:
            logger.warning(f"⚠️ Failed to auto-update joining message after /extend: {e}")
    else:
        # Should not happen if game.is_joining is True, but handle defensively
        await safe_send(context, chat_id, "⚠️ Cannot extend time; joining end time not set.")

# ✨ ======================== BOT STATS COMMAND (Admin/Owner) ======================== ✨

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows global bot statistics (Owner & Bot Admins only)."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # --- Permission Check ---
    if not await is_admin(user_id): # Checks if owner or in ADMIN_IDS
        await safe_send(context, chat_id, "🚫 Access Denied: This command is restricted to Bot Admins.")
        logger.warning(f"Unauthorized /stats access attempt by user {user_id}")
        return

    # --- Fetch Stats from Database ---
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        # Total registered players
        cursor.execute('SELECT COUNT(user_id) FROM players')
        total_players = cursor.fetchone()[0]

        # Total games played (from history)
        cursor.execute('SELECT COUNT(game_id) FROM game_history')
        total_games_played = cursor.fetchone()[0]

        # Active groups (groups with settings or history) - simple estimate
        cursor.execute('SELECT COUNT(DISTINCT chat_id) FROM game_history')
        groups_with_history = cursor.fetchone()[0]
        # cursor.execute('SELECT COUNT(chat_id) FROM group_settings') # Alternative count

        # Games played in last 7 days
        seven_days_ago_iso = (datetime.now() - timedelta(days=7)).isoformat()
        cursor.execute('SELECT COUNT(game_id) FROM game_history WHERE end_time >= ?', (seven_days_ago_iso,))
        games_last_7_days = cursor.fetchone()[0]

    except sqlite3.Error as e:
        logger.error(f"❌ DB Error fetching global stats: {e}")
        await safe_send(context, chat_id, "❌ Error retrieving statistics from the database.")
        return
    finally:
        if conn: conn.close()

    # --- Get In-Memory Stats ---
    active_games_now = len(games) # Count of games currently running

    # --- Assemble Fancy Text ---
    fancy_separator = "📈 • ⋅ ⋅ ────────── ⋅ ⋅ • 📈"
    stats_text = f"""
    📈 <b>Bot Performance Metrics</b> 📈

    Live snapshot of Ship Battle Royale operations:

    {fancy_separator}

    <b>User Base:</b>
      👤 Total Registered Captains: {total_players}

    <b>Game Activity:</b>
      🎮 Total Battles Completed: {total_games_played}
      ⚔️ Battles (Last 7 Days): {games_last_7_days}
      🌍 Active Sectors (Groups with History): {groups_with_history}

    <b>Current Status:</b>
      ⚡ Live Battles Running: {active_games_now}

    {fancy_separator}
    System operational. All parameters nominal. ✨
    """

    # --- Send Stats ---
    await safe_send_photo(
        context=context, chat_id=chat_id,
        photo_url=get_random_image('stats_admin'), # Use the admin stats image
        caption=stats_text,
        parse_mode=ParseMode.HTML
    )

# ✨ ======================== BROADCAST COMMAND (Owner Only) ======================== ✨

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forwards the replied message to all registered users (Owner Only)."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id # Can be run in group or PM

    # --- Permission Check ---
    if not await is_owner(user_id):
        await safe_send(context, chat_id, "🚫 Access Denied: This command is restricted to the Bot Owner.")
        logger.warning(f"Unauthorized /broadcast attempt by user {user_id}")
        return

    # --- Check for Replied Message ---
    replied_message = update.message.reply_to_message
    if not replied_message:
        await safe_send(context, chat_id, "⚠️ <b>How to Broadcast:</b> Reply to the message you want to send with the command <code>/broadcast</code>.")
        return

    # --- Fetch Users ---
    conn = None
    user_ids_to_broadcast = []
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        # Get all distinct user IDs from the players table
        cursor.execute('SELECT DISTINCT user_id FROM players')
        user_ids_to_broadcast = [row[0] for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logger.error(f"❌ DB Error fetching users for broadcast: {e}")
        await safe_send(context, chat_id, "❌ Error retrieving user list from database.")
        return
    finally:
        if conn: conn.close()

    if not user_ids_to_broadcast:
        await safe_send(context, chat_id, "ℹ️ No registered users found to broadcast to.")
        return

    # --- Start Broadcast ---
    total_users = len(user_ids_to_broadcast)
    await safe_send(context, chat_id, f"🚀 Initiating broadcast of replied message to {total_users} users... This may take some time.")
    logger.info(f"📣 Starting broadcast initiated by owner {user_id} to {total_users} users.")

    success_count = 0
    fail_count = 0
    start_time = datetime.now()

    for target_user_id in user_ids_to_broadcast:
        try:
            # Forward the original message
            await context.bot.forward_message(
                chat_id=target_user_id,
                from_chat_id=replied_message.chat_id,
                message_id=replied_message.message_id
            )
            success_count += 1
        except Forbidden:
            # User blocked the bot or left the chat
            fail_count += 1
            logger.warning(f"🚫 Broadcast failed to {target_user_id}: Bot blocked or user inactive.")
        except (BadRequest, TelegramError) as e:
            # Other Telegram errors
            fail_count += 1
            logger.error(f"❌ Broadcast failed to {target_user_id}: {e}")
        except Exception as e:
            # Unexpected errors
            fail_count += 1
            logger.error(f"❌ Unexpected error broadcasting to {target_user_id}: {e}", exc_info=True)

        # Small delay to avoid hitting Telegram rate limits
        await asyncio.sleep(0.1) # Adjust sleep time if needed (e.g., 0.05 for faster, 0.2 for slower)

    # --- Broadcast Completion ---
    end_time = datetime.now()
    duration = end_time - start_time
    fancy_separator = "✅ • ⋅ ⋅ ────────── ⋅ ⋅ • ✅"

    completion_text = f"""
    ✅ <b>Broadcast Complete!</b> ✅

    The message has been sent out.

    {fancy_separator}

    <b>Results:</b>
      📬 Successfully Sent: {success_count} / {total_users}
      🚫 Failed / Blocked: {fail_count}
      ⏱️ Duration: {str(duration).split('.')[0]} (H:MM:SS)

    {fancy_separator}
    """
    await safe_send(context, chat_id, completion_text, parse_mode=ParseMode.HTML)
    logger.info(f"📣 Broadcast finished. Success: {success_count}, Failed: {fail_count}. Duration: {duration}")


# 🗳️ ======================== MAP VOTING & JOINING PHASE LOGIC ======================== 🗳️

async def handle_map_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles a player's map vote button press."""
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    if is_globally_banned(user_id):
        await query.answer("🚫 Access Denied.", show_alert=True)
        return

    if chat_id not in games:
        await query.answer("⚠️ Game not found or expired.", show_alert=True)
        return

    game = games[chat_id]

    if not game.map_voting:
        await query.answer("⌛ Voting has ended.", show_alert=True)
        return

    # Only players currently in the game can vote (creator added automatically)
    if user_id not in game.players:
        await query.answer("✋ Please join the game first to vote!", show_alert=True)
        return

    map_key = query.data.split('_')[-1] # Extract map key (e.g., 'classic')
    if map_key not in MAPS:
        await query.answer("❓ Invalid map choice.", show_alert=True)
        return

    game.map_votes[user_id] = map_key
    map_name = MAPS[map_key]['name']
    await query.answer(f"✅ Voted for {map_name}!")

    # --- Update vote counts in chat (optional user feedback) ---
    vote_counts = defaultdict(int)
    for vote in game.map_votes.values():
        vote_counts[vote] += 1

    votes_display = "\n".join([
        f"  {MAPS[mk]['name']}: {count} vote{'s' if count > 1 else ''}"
        for mk, count in sorted(vote_counts.items())
    ])
    await safe_send(
        context, game.chat_id,
        f"🗳️ {escape_markdown_value(query.from_user.first_name)} voted for <b>{escape_markdown_value(map_name)}</b>!\n\nCurrent Votes:\n{votes_display}",
        parse_mode=ParseMode.HTML
    )

async def map_voting_countdown(context: ContextTypes.DEFAULT_TYPE, game: Game):
    """Waits for map voting duration, determines map, and starts joining phase."""
    try:
        await asyncio.sleep(30) # Wait for 30 seconds

        # Check if the game still exists and is in the map voting stage
        if game.chat_id not in games or not game.map_voting:
            logger.info(f"Map voting countdown cancelled or game ended for chat {game.chat_id}.")
            return

        game.map_voting = False # End the voting phase

        # --- Determine Winning Map ---
        selected_map_key = 'classic' # Default
        if game.map_votes:
            vote_counts = defaultdict(int)
            for vote in game.map_votes.values():
                vote_counts[vote] += 1
            # Find the map key with the maximum votes
            selected_map_key = max(vote_counts, key=vote_counts.get)
            selected_map_votes = vote_counts[selected_map_key]
            map_name = MAPS[selected_map_key]['name']
            await safe_send(context, game.chat_id,
                f"✅ <b>Map Selected:</b> {escape_markdown_value(map_name)} ({selected_map_votes} Votes)\nThe battle commences!",
                parse_mode=ParseMode.HTML
            )
        else:
            map_name = MAPS[selected_map_key]['name']
            await safe_send(context, game.chat_id,
                f"⏳ No votes received. Defaulting to <b>{escape_markdown_value(map_name)}</b>!",
                parse_mode=ParseMode.HTML
            )

        game.set_map(selected_map_key) # Set the chosen map in the game object

        # --- Transition to Joining Phase ---
        if game.mode == 'solo':
            await start_solo_mode_after_voting(context, game)
        elif game.mode == 'team':
            await start_team_mode_after_voting(context, game)
        else:
             logger.error(f"❌ Invalid game mode '{game.mode}' after map voting for chat {game.chat_id}. Cancelling game.")
             await safe_send(context, game.chat_id, "❌ Error: Invalid game mode selected. Game cancelled.")
             if game.chat_id in games: del games[game.chat_id]

    except Exception as e:
        logger.error(f"❌ Error during map voting countdown for chat {game.chat_id}: {e}", exc_info=True)
        # Attempt to clean up the game state on error
        await safe_send(context, game.chat_id, "❌ An error occurred during map selection. The game has been cancelled.")
        if game.chat_id in games: del games[game.chat_id]

async def start_solo_mode_after_voting(context: ContextTypes.DEFAULT_TYPE, game: Game):
    """Initiates the solo game joining phase after map selection."""
    game.is_joining = True
    game.join_end_time = datetime.now() + timedelta(seconds=game.settings['join_time'])

    # Announce the start of the joining phase first
    announce_msg = await safe_send(
        context, game.chat_id,
        f"⚔️ <b>Solo Battle Joining Phase Started!</b> ⚔️\nMap: {escape_markdown_value(MAPS[game.map_type]['name'])}\n\nCaptains, use the <code>/join</code> command to enter the fray! The muster board will appear shortly.",
        parse_mode=ParseMode.HTML
    )

    # Start the countdown timer for the joining phase immediately (doesn't wait on the muster card)
    asyncio.create_task(joining_countdown(context, game))

    # Wait 30s, then post the pinned muster board and clean up the announcement
    await asyncio.sleep(30)
    if game.chat_id not in games or not game.is_joining:
        return  # Game may have already been cancelled/started

    mock_message = type('obj', (object,), {
        'message_id': game.joining_message_id,
        'chat_id': game.chat_id
    })
    await display_joining_phase(mock_message, context, game, edit=True)
    await pin_message(context, game.chat_id, game.joining_message_id)

    if announce_msg:
        try:
            await context.bot.delete_message(game.chat_id, announce_msg.message_id)
        except Exception:
            pass

async def start_team_mode_after_voting(context: ContextTypes.DEFAULT_TYPE, game: Game):
    """Initiates the team game joining phase after map selection."""
    game.is_joining = True
    game.join_end_time = datetime.now() + timedelta(seconds=game.settings['join_time'])

    announce_msg = await safe_send(
        context, game.chat_id,
        f"🤝 <b>Team Battle Joining Phase Started!</b> 🤝\nMap: {escape_markdown_value(MAPS[game.map_type]['name'])}\nMode: Alpha 🔵 vs Beta 🔴\n\nThe muster board will appear shortly — choose your allegiance there!",
        parse_mode=ParseMode.HTML
    )

    asyncio.create_task(joining_countdown(context, game))

    await asyncio.sleep(30)
    if game.chat_id not in games or not game.is_joining:
        return

    mock_message = type('obj', (object,), {
        'message_id': game.joining_message_id,
        'chat_id': game.chat_id
    })
    await display_team_joining_phase(mock_message, context, game, edit=True)
    await pin_message(context, game.chat_id, game.joining_message_id)

    if announce_msg:
        try:
            await context.bot.delete_message(game.chat_id, announce_msg.message_id)
        except Exception:
            pass

async def display_team_joining_phase(message, context: ContextTypes.DEFAULT_TYPE, game: Game, edit: bool = False):
    """Displays or updates the team joining message (Fancy UI)."""
    remaining_seconds = max(0, (game.join_end_time - datetime.now()).total_seconds())
    time_str = format_time(remaining_seconds)
    min_players = game.settings['min_players']
    max_players = game.settings['max_players']
    current_players = len(game.players)

    # --- Prepare Player Lists ---
    alpha_players = []
    beta_players = []
    sorted_player_ids = sorted(game.players.keys(), key=lambda uid: game.players[uid].get('first_name', ''))

    for i, user_id in enumerate(sorted_player_ids):
        data = game.players[user_id]
        name = escape_markdown_value(data.get('first_name', f'Captain_{user_id}'))
        stats = get_player_stats(user_id) # Fetch stats for title
        title_key = stats[18] if stats and len(stats) > 18 and stats[18] in PLAYER_TITLES else 'novice_captain'
        title_emoji = PLAYER_TITLES[title_key]['emoji']
        display_name = f"{title_emoji} {name}"

        if data.get('team') == 'alpha':
            alpha_players.append(f"  {len(alpha_players) + 1}. 🔵 {display_name}")
        elif data.get('team') == 'beta':
            beta_players.append(f"  {len(beta_players) + 1}. 🔴 {display_name}")

    alpha_list_str = "\n".join(alpha_players) if alpha_players else " Awaiting Captain..." # Empty team placeholder
    beta_list_str = "\n".join(beta_players) if beta_players else "  Awaiting Captain..." # Empty team placeholder

    # --- Assemble Caption (Fancy UI) ---
    caption = f"""
✨ <b>Team Battle Formation</b> ✨

🗺️ <b>Arena:</b> {escape_markdown_value(MAPS[game.map_type]['name'])}
⏳ <b>Time Remaining:</b> {time_str}
👥 <b>Crew:</b> {current_players}/{max_players} (Need {min_players} to launch!)

~~~~~ 🔵 <b>Team Alpha</b> ({len(alpha_players)}) ~~~~~
{alpha_list_str}

~~~~~ 🔴 <b>Team Beta</b> ({len(beta_players)}) ~~~~~
{beta_list_str}

Choose your side, Captain! Victory awaits the coordinated!
"""
    if remaining_seconds <= 30 and remaining_seconds > 0:
        caption += f"\n\n🚨 <b>Final Call! {int(remaining_seconds)} seconds left!</b> 🚨"

    # --- Buttons ---
    keyboard = [
        [
            InlineKeyboardButton("🔵 Join Alpha Force", callback_data=f"team_join_alpha_{game.chat_id}"),
            InlineKeyboardButton("🔴 Join Beta Squadron", callback_data=f"team_join_beta_{game.chat_id}")
        ],
        [
            InlineKeyboardButton("❌ Abandon Ship", callback_data=f"leave_game_{game.chat_id}"),
            InlineKeyboardButton("🔭 Spectate", callback_data=f"spectate_{game.chat_id}")
        ],
        [
            InlineKeyboardButton("🚀 Force Start (Admin)", callback_data=f"forcestart_ask_{game.chat_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # --- Send / Edit Message ---
    try:
        if edit and game.joining_message_id:
            await context.bot.edit_message_caption(
                chat_id=game.chat_id, message_id=game.joining_message_id,
                caption=caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML
            )
        else: # Send as new message if not editing
            gif_url = get_random_gif('joining')
            new_msg = await safe_send_animation(
                context, game.chat_id, gif_url, caption=caption,
                reply_markup=reply_markup, parse_mode=ParseMode.HTML
            )
            if new_msg: game.joining_message_id = new_msg.message_id
    except BadRequest as e:
        if 'message is not modified' not in str(e).lower():
            logger.warning(f"⚠️ Failed to update team joining message: {e}")
    except Exception as e:
        logger.error(f"❌ Unexpected error displaying team joining phase: {e}", exc_info=True)


async def handle_team_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles button presses for joining a specific team."""
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    if is_globally_banned(user_id):
        await query.answer("🚫 Access Denied.", show_alert=True)
        return

    if chat_id not in games:
        await query.answer("⚠️ Game not found or expired.", show_alert=True)
        return

    game = games[chat_id]

    if not game.is_joining:
        await query.answer("⌛ Joining phase is over!", show_alert=True)
        return

    team_choice = query.data.split('_')[2] # 'alpha' or 'beta'
    user_info = query.from_user
    first_name = user_info.first_name or "Captain"
    username = user_info.username

    # --- Logic for joining or switching team ---
    if user_id in game.players:
        current_team = game.players[user_id].get('team')
        if current_team == team_choice:
            await query.answer(f"✅ Already on Team {team_choice.capitalize()}!", show_alert=False)
            return
        else:
            # Switch team
            if current_team and current_team in game.teams:
                game.teams[current_team].discard(user_id) # Use discard for safety
            game.teams[team_choice].add(user_id)
            game.players[user_id]['team'] = team_choice
            team_emoji = '🔵' if team_choice == 'alpha' else '🔴'
            await safe_send(context, chat_id, f"🔄 {mention(user_id, first_name)} switched allegiance to Team {team_choice.capitalize()}! {team_emoji}", parse_mode=ParseMode.HTML)
            await query.answer(f"✅ Switched to Team {team_choice.capitalize()}!")
    else:
        # Add new player
        if not await require_registration(update, context, user_id, first_name, chat_id):
            return

        success, msg = game.add_player(user_id, username, first_name, team=team_choice)
        if success:
            stats = get_player_stats(user_id)
            title_key = stats[18] if stats and len(stats) > 18 and stats[18] in PLAYER_TITLES else 'novice_captain'
            title_emoji = PLAYER_TITLES[title_key]['emoji']
            team_emoji = '🔵' if team_choice == 'alpha' else '🔴'
            await safe_send(context, chat_id,
                            f"✨ {title_emoji} {mention(user_id, first_name)} joins Team {team_choice.capitalize()}! {team_emoji}", parse_mode=ParseMode.HTML)
            await query.answer(f"✅ Welcome to Team {team_choice.capitalize()}!")
        else:
            await query.answer(f"❌ {msg}", show_alert=True)

    # --- Update the joining message ---
    await display_team_joining_phase(query.message, context, game, edit=True)


async def display_joining_phase(message, context: ContextTypes.DEFAULT_TYPE, game: Game, edit: bool = False):
    """Displays or updates the solo joining message (Fancy UI)."""
    remaining_seconds = max(0, (game.join_end_time - datetime.now()).total_seconds())
    time_str = format_time(remaining_seconds)
    min_players = game.settings['min_players']
    max_players = game.settings['max_players']
    current_players = len(game.players)

    # --- Prepare Player List ---
    player_list = []
    sorted_player_ids = sorted(game.players.keys(), key=lambda uid: game.players[uid].get('first_name', ''))

    for i, user_id in enumerate(sorted_player_ids):
        stats = get_player_stats(user_id)
        title_key = stats[18] if stats and len(stats) > 18 and stats[18] in PLAYER_TITLES else 'novice_captain'
        title_emoji = PLAYER_TITLES[title_key]['emoji']
        player_list.append(f"{i + 1}. {title_emoji} {mention(user_id)}")

    player_list_str = "\n".join(player_list) if player_list else "Waiting for captains..."

    # --- Assemble Caption (premium card) ---
    card_lines = [
        f"🗺 Arena : {escape_markdown_value(MAPS[game.map_type]['name'])}",
        f"⏳ Time Remaining : {time_str}",
        f"👥 Crew : {current_players}/{max_players} (Need {min_players} to launch!)",
        "",
        "<b>Registered Captains</b>",
    ]
    card_lines.extend(player_list_str.split("\n"))
    card_lines.append("")
    card_lines.append("Tap Join Battle or use /join to enter this free-for-all!")
    if remaining_seconds <= 30 and remaining_seconds > 0:
        card_lines.append("")
        card_lines.append(f"🚨 <b>Final Call! {int(remaining_seconds)} seconds left!</b> 🚨")

    caption = build_card("⚓ Solo Battle Muster", card_lines, emoji="🚢")

    # --- Buttons ---
    keyboard = pack_buttons([
        InlineKeyboardButton("✅ Join Battle", callback_data=f"join_game_{game.chat_id}"),
        InlineKeyboardButton("❌ Withdraw", callback_data=f"leave_game_{game.chat_id}"),
        InlineKeyboardButton("🚀 Force Start (Admin)", callback_data=f"forcestart_ask_{game.chat_id}"),
    ], per_row=2)
    reply_markup = keyboard

    # --- Send / Edit Message ---
    try:
        gif_url = get_random_gif('joining')
        if edit and game.joining_message_id:
            # Edit existing animation message
             await context.bot.edit_message_media(
                 chat_id=game.chat_id, message_id=game.joining_message_id,
                 media=InputMediaAnimation(media=gif_url, caption=caption, parse_mode=ParseMode.HTML),
                 reply_markup=reply_markup
             )
        else:
             # If not editing, try deleting old map vote message and send new join message
            if game.joining_message_id:
                 try: await context.bot.delete_message(game.chat_id, game.joining_message_id)
                 except: pass # Ignore if deletion fails

            new_msg = await safe_send_animation(
                context, game.chat_id, gif_url, caption=caption,
                reply_markup=reply_markup, parse_mode=ParseMode.HTML
            )
            if new_msg: game.joining_message_id = new_msg.message_id

    except BadRequest as e:
        if 'message is not modified' not in str(e).lower():
            logger.warning(f"⚠️ Failed to update solo joining message: {e}")
            # If edit failed catastrophically, try sending new
            if edit:
                 new_msg = await safe_send_animation(
                     context, game.chat_id, get_random_gif('joining'), caption=caption,
                     reply_markup=reply_markup, parse_mode=ParseMode.HTML
                 )
                 if new_msg: game.joining_message_id = new_msg.message_id
    except Exception as e:
        logger.error(f"❌ Unexpected error displaying solo joining phase: {e}", exc_info=True)


async def joining_countdown(context: ContextTypes.DEFAULT_TYPE, game: Game):
    """Periodically updates the joining message timer and starts the game."""
    try:
        while game.is_joining and game.chat_id in games:
            remaining_sec = (game.join_end_time - datetime.now()).total_seconds()

            if remaining_sec <= 0:
                break # Timer expired

            # Update message periodically (e.g., every 15s or last 10s)
            update_interval = 15
            if remaining_sec <= 10: update_interval = 1 # Update every second in the last 10

            if int(remaining_sec) % update_interval == 0 or remaining_sec <= 10:
                mock_message = type('obj', (object,), {
                    'message_id': game.joining_message_id, 'chat_id': game.chat_id
                })
                try:
                    if game.mode == 'team':
                        await display_team_joining_phase(mock_message, context, game, edit=True)
                    else:
                        await display_joining_phase(mock_message, context, game, edit=True)
                except Exception as e:
                    logger.warning(f"⚠️ Failed to update joining countdown message: {e}")

            await asyncio.sleep(1) # Check every second

        # --- Timer finished or break ---
        # Ensure game still exists and joining phase was active
        if game.chat_id in games and game.is_joining:
            game.is_joining = False # Mark joining as ended
            logger.info(f"Joining phase ended for game in chat {game.chat_id}. Starting game check.")
            await start_game_phase(context, game) # Proceed to start game logic

    except Exception as e:
        logger.error(f"❌ Error in joining countdown for chat {game.chat_id}: {e}", exc_info=True)
        # Attempt cleanup if an error occurred
        if game.chat_id in games:
            await safe_send(context, game.chat_id, "❌ An error occurred during the joining phase. Game cancelled.")
            del games[game.chat_id]


# --- 🚀 Force Start (Admin Only, via button) ---
async def handle_forcestart_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the 'Force Start' button on the muster board. Admin-only, asks for confirmation."""
    query = update.callback_query
    user_id = query.from_user.id
    try:
        chat_id = int(query.data.rsplit('_', 1)[1])
    except (ValueError, IndexError):
        await query.answer("⚠️ Malformed request.", show_alert=True)
        return

    if not await is_admin_or_owner(context, chat_id, user_id):
        await query.answer("🚫 Only group admins can force-start a game.", show_alert=True)
        return

    game = games.get(chat_id)
    if not game or not game.is_joining:
        await query.answer("ℹ️ No game is currently in the joining phase.", show_alert=True)
        return

    min_players = game.settings.get('min_players', 2)
    current_players = len(game.players)
    if current_players < min_players:
        await query.answer(
            f"⚠️ Need at least {min_players} captains to force-start, only {current_players} joined so far.",
            show_alert=True
        )
        return

    await query.answer()
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, force start", callback_data=f"confirm_forcestart_{user_id}_{chat_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_forcestart_{user_id}_{chat_id}"),
    ]])
    await safe_send(
        context, chat_id,
        f"🚀 {mention(user_id)}, are you sure you want to <b>force start</b> the battle now with {current_players} captains, "
        f"skipping the rest of the join timer?",
        reply_markup=keyboard, parse_mode=ParseMode.HTML
    )


async def handle_forcestart_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the Yes/No buttons from the /forcestart confirmation prompt."""
    query = update.callback_query
    data = query.data
    try:
        action, _, requester_id_str, chat_id_str = data.split('_', 3)
        requester_id = int(requester_id_str)
        chat_id = int(chat_id_str)
    except ValueError:
        await query.answer("⚠️ Malformed request.", show_alert=True)
        return

    if query.from_user.id != requester_id:
        await query.answer("✋ Only the admin who ran /forcestart can confirm this.", show_alert=True)
        return

    if data.startswith("cancel_forcestart_"):
        await query.answer("Cancelled.")
        await query.edit_message_text("✅ Force start cancelled — the join timer continues.")
        return

    game = games.get(chat_id)
    if not game or not game.is_joining:
        await query.answer("ℹ️ Joining phase already ended.", show_alert=True)
        await query.edit_message_text("ℹ️ Joining phase has already ended.")
        return

    await query.answer("🚀 Starting now!")
    await query.edit_message_text(f"🚀 {mention(requester_id)} force-started the battle!", parse_mode=ParseMode.HTML)

    game.is_joining = False  # Stops the joining_countdown loop on its next check
    await start_game_phase(context, game)



# ✨ ======================== START GAME PHASE ======================== ✨
async def start_game_phase(context: ContextTypes.DEFAULT_TYPE, game: Game):
    """Checks player count and starts the actual battle phase (Fancy UI)."""
    chat_id = game.chat_id
    min_players = game.settings.get('min_players', 2)
    current_players = len(game.players)

    # --- Check Minimum Player Count ---
    if current_players < min_players:
        cancel_caption = build_card(
            "LAUNCH ABORTED",
            [
                "Insufficient crew for battle commencement!",
                f"Required : {min_players}   Joined : {current_players}",
                "",
                "The fleet disperses. Try again with /creategame!",
            ],
            emoji="⏳",
        )
        await safe_send_animation(context, chat_id, get_random_gif('eliminated'), # Use a fitting GIF
                                  caption=cancel_caption, parse_mode=ParseMode.HTML)
        logger.warning(f"Game cancelled in chat {chat_id} due to insufficient players ({current_players}/{min_players}).")
        if chat_id in games: del games[chat_id] # Clean up game object
        return

    # --- Check Team Balance (if applicable) ---
    if game.mode == 'team':
        alpha_count = len(game.get_alive_team_players('alpha'))
        beta_count = len(game.get_alive_team_players('beta'))
        if alpha_count == 0 or beta_count == 0:
            balance_caption = f"""
            ⚖️ <b>Launch Aborted!</b> ⚖️

            Team battle requires captains on both sides!
            Alpha: {alpha_count} | Beta: {beta_count}

            The battle cannot begin unbalanced. Reform the fleets with <code>/creategame</code>!
            """
            await safe_send_animation(context, chat_id, get_random_gif('eliminated'),
                                      caption=balance_caption, parse_mode=ParseMode.HTML)
            logger.warning(f"Game cancelled in chat {chat_id} due to unbalanced teams (A:{alpha_count}, B:{beta_count}).")
            if chat_id in games: del games[chat_id]
            return

    # --- Start the Game ---
    game.is_joining = False # End joining phase
    game.is_active = True  # Start active battle phase
    game.day = 1           # Set Day to 1
    logger.info(f"🚀 Starting Day 1 for game in chat {chat_id}. Mode: {game.mode}. Players: {current_players}")

    mode_display = "Solo Combat" if game.mode == 'solo' else f"Team Skirmish (Alpha 🔵 vs Beta 🔴)"
    fancy_separator = "⚔️ • ⋅ ⋅ ────────── ⋅ ⋅ • ⚔️"

    start_caption = f"""
    ✨ <b>BATTLE COMMENCES! - DAY {game.day}</b> ✨

    <b>Mode:</b> {mode_display}
    <b>Arena:</b> {escape_markdown_value(MAPS[game.map_type]['name'])}
    <b>Captains Ready:</b> {current_players}

    {fancy_separator}

    <b>Initial Parameters:</b>
      Starting HP: {HP_START}
      Attack Range: {ATTACK_RANGE} squares
      Action Time: {format_time(game.settings['operation_time'])}
      AFK Limit: {AFK_TURNS_LIMIT} missed turns

    {fancy_separator}

    Captains, check your Direct Messages (DMs) for orders!
    May the most cunning strategist prevail!  LUCK! 🍀
    """

    # Send start announcement with GIF
    await safe_send_animation(context, chat_id, get_random_gif('start'),
                              caption=start_caption, parse_mode=ParseMode.HTML)

    # Send the interactive button map as its own message (tap any cell for info)
    await safe_send(context, chat_id, game.get_map_header_card(), parse_mode=ParseMode.HTML)
    map_msg = await safe_send(context, chat_id, "🗺 Tap a cell to inspect it:", reply_markup=game.get_map_keyboard())
    if map_msg:
        game.last_map_message_id = map_msg.message_id

    # --- Send Initial Action Prompts via DM ---
    alive_ids_start = game.get_alive_players()
    for user_id in alive_ids_start:
        await send_operation_choice_button(context, game, user_id)

    # --- Start Operation Countdown ---
    game.operation_end_time = datetime.now() + timedelta(seconds=game.settings['operation_time'])
    asyncio.create_task(operation_countdown(context, game))

async def handle_join_leave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles join/leave button presses during the joining phase."""
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    if is_globally_banned(user_id):
        await query.answer("🚫 Access Denied.", show_alert=True)
        return

    if chat_id not in games:
        await query.answer("⚠️ Game not found or expired.", show_alert=True)
        return

    game = games[chat_id]

    if not game.is_joining:
        await query.answer("⌛ Joining phase is over!", show_alert=True)
        return

    action = query.data.split('_')[0] # 'join' or 'leave'
    user_info = query.from_user
    first_name = user_info.first_name or "Captain"
    username = user_info.username

    if action == 'join':
        if game.mode == 'team':
            await query.answer("✋ Please use the 'Join Alpha' or 'Join Beta' buttons for Team Mode!", show_alert=True)
            return

        if not await require_registration(update, context, user_id, first_name, chat_id):
            return

        success, msg = game.add_player(user_id, username, first_name)
        if success:
            stats = get_player_stats(user_id)
            title_key = stats[18] if stats and len(stats) > 18 and stats[18] in PLAYER_TITLES else 'novice_captain'
            title_emoji = PLAYER_TITLES[title_key]['emoji']
            await safe_send(context, chat_id, f"✅ {title_emoji} {mention(user_id, first_name)} has joined the battle!", parse_mode=ParseMode.HTML)
            await query.answer("🚀 Welcome aboard!")
        else:
            await query.answer(f"❌ {msg}", show_alert=True)

    elif action == 'leave':
        if user_id in game.players:
            player_data = game.players[user_id]
            px, py = player_data['position']
            team = player_data.get('team')

            # Remove from grid
            try:
                if user_id in game.map_grid[px][py]: game.map_grid[px][py].remove(user_id)
            except IndexError: pass # Ignore grid errors on leave
            # Remove from team set
            if team and team in game.teams: game.teams[team].discard(user_id)
            # Remove from players dict
            del game.players[user_id]

            await safe_send(context, chat_id, f"💨 {mention(user_id, first_name)} has withdrawn from the muster.", parse_mode=ParseMode.HTML)
            await query.answer("✅ You have left the game.")
        else:
            await query.answer("❓ You were not in the game.", show_alert=False)

    # --- Update the main joining message ---
    if game.is_joining: # Check again in case game started concurrently
        if game.mode == 'team':
            await display_team_joining_phase(query.message, context, game, edit=True)
        else:
            await display_joining_phase(query.message, context, game, edit=True)

# ✨ ======================== IN-GAME ACTION MESSAGES (DM) ======================== ✨

async def send_operation_choice_button(context: ContextTypes.DEFAULT_TYPE, game: Game, user_id: int):
    """Sends the player's command console (with real action buttons) straight to their DM.
    If delivery fails (most commonly because the player never pressed /start in DM with
    the bot), this now alerts the group instead of failing silently."""
    if user_id not in game.players: return # Safety check
    sent = await send_operation_dm(context, game, user_id)
    if sent is None:
        first_name = game.players[user_id].get('first_name', 'Captain')
        await send_dm_registration_alert(context, game.chat_id, first_name, user_id)


async def send_operation_dm(context: ContextTypes.DEFAULT_TYPE, game: Game, user_id: int):
    """Sends the main action selection panel to the player's DM (Fancy UI)."""
    if user_id not in game.players: return
    player = game.players[user_id]
    if not player.get('alive'): return # Don't send to eliminated players

    # --- Gather Player Data ---
    hp = player.get('hp', 0)
    max_hp = player.get('max_hp', HP_START)
    hp_bar = get_progress_bar(hp, max_hp)
    hp_indicator = get_hp_indicator(hp, max_hp)
    px, py = player.get('position', ('?', '?'))
    inventory = player.get('inventory', [])
    inventory_count = len(inventory)
    afk_strikes = player.get('afk_turns', 0)
    kills = player.get('stats', {}).get('kills', 0)
    op_time = game.settings.get('operation_time', 120)

    # --- Format Inventory ---
    inventory_lines = []
    if inventory:
        item_counts = defaultdict(int)
        for item_key in inventory: item_counts[item_key] += 1
        for item_key, count in item_counts.items():
            item = LOOT_ITEMS.get(item_key)
            if item:
                rarity_color = get_rarity_color(item['rarity'])
                inventory_lines.append(f"  {rarity_color} {item['emoji']} {item_key.replace('_', ' ').title()} (x{count})")
    inventory_display = "\n".join(inventory_lines) if inventory_lines else "  < Empty >"

    # --- Format Team / Alliance ---
    team_display = ""
    if game.mode == 'team':
        team = player.get('team')
        team_emoji = '🔵' if team == 'alpha' else '🔴' if team == 'beta' else '⚪'
        team_display = f"👥 Team : {team_emoji} {team.capitalize() if team else 'None'}\n"

    alliance_display = ""
    alliance_info = game.alliances.get(user_id)
    if alliance_info:
        ally_id = alliance_info['ally']
        turns = alliance_info['turns_left']
        alliance_display = f"🤝 Alliance : {mention(ally_id)} ({turns} turns left)\n"

    # --- Get Title ---
    stats = get_player_stats(user_id)
    title_key = stats[18] if stats and len(stats) > 18 and stats[18] in PLAYER_TITLES else 'novice_captain'
    title_data = PLAYER_TITLES[title_key]

    # --- Assemble Premium Card ---
    card_lines = [
        f"{title_data['emoji']} Captain : {mention(user_id)} ({title_data['name']})",
        f"🗺 Arena : {escape_markdown_value(MAPS[game.map_type]['name'])}",
        "",
        f"{hp_indicator} Hull : {hp}/{max_hp} HP",
        f"   {hp_bar}",
        f"📍 Coordinates : ({px}, {py})",
        team_display.strip() if team_display else None,
        alliance_display.strip() if alliance_display else None,
        f"⚠️ AFK Strikes : {afk_strikes}/{AFK_TURNS_LIMIT}",
        f"⏱ Time Left : {format_time(op_time)}",
        f"💥 Eliminations : {kills}",
        "",
        f"🎒 Cargo ({inventory_count}/{LOOT_ITEM_CAP}):",
    ]
    card_lines = [l for l in card_lines if l is not None]
    card_lines.insert(0, f"☀️ Day {game.day}")
    card_lines.extend([f"  {l}" for l in inventory_display.split("\n")])
    card_lines.append("")
    card_lines.append("Select your action directive 👇")

    caption = build_card("SHIP CONSOLE", card_lines, emoji="🚢")

    # --- Action Buttons (Cricoverse icon style, max 2-3 per row) ---
    keyboard = pack_buttons([
        InlineKeyboardButton(ACTION_LABELS["attack"], callback_data=f"operation_attack_{user_id}_{game.chat_id}"),
        InlineKeyboardButton(ACTION_LABELS["defend"], callback_data=f"operation_defend_{user_id}_{game.chat_id}"),
        InlineKeyboardButton(ACTION_LABELS["heal"], callback_data=f"operation_heal_{user_id}_{game.chat_id}"),
        InlineKeyboardButton("💎 Loot", callback_data=f"operation_loot_{user_id}_{game.chat_id}"),
        InlineKeyboardButton(ACTION_LABELS["move"], callback_data=f"operation_move_{user_id}_{game.chat_id}"),
    ], per_row=2)

    # --- Send as plain text (reliable — no dependency on external GIF hosting) ---
    return await safe_send(
        context, user_id, caption,
        reply_markup=keyboard, parse_mode=ParseMode.HTML
    )

async def handle_operation_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles button presses for choosing an action (Attack, Heal, etc.)."""
    query = update.callback_query
    user_id = query.from_user.id

    if is_globally_banned(user_id):
        await query.answer("🚫 Access Denied.", show_alert=True)
        return

    # --- Extract data and find game ---
    try:
        _, operation, op_user_id_str, op_chat_id_str = query.data.split('_')
        op_user_id = int(op_user_id_str)
        op_chat_id = int(op_chat_id_str)
    except ValueError:
        logger.error(f"❌ Invalid callback data format: {query.data}")
        await query.answer("⚠️ Error processing action. Please try again.", show_alert=True)
        return

    # Ensure the button presser is the intended user
    if user_id != op_user_id:
        await query.answer("✋ This is not your command console!", show_alert=True)
        return

    game = games.get(op_chat_id)
    if not game:
        await query.answer("⚠️ Game not found or has ended.", show_alert=True)
        try: await query.edit_message_text("❌ This game session has concluded.") # Clean up DM
        except: pass
        return

    if not game.is_active:
        await query.answer("⏳ The game is not currently active.", show_alert=True)
        return

    player = game.players.get(user_id)
    if not player or not player.get('alive'):
        await query.answer("💀 You have been eliminated from this battle.", show_alert=True)
        return

    if player.get('operation'):
        await query.answer("✅ Action already selected for this turn!", show_alert=False)
        return

    # --- Handle Specific Operations ---
    if operation == 'attack':
        await show_target_selection(query, context, game, user_id, op_chat_id)
    elif operation == 'move':
        await show_move_selection(query, context, game, user_id, op_chat_id)
    elif operation == 'back': # Go back to the main operation menu
        await query.message.delete() # Delete the sub-menu (Target/Move)
        await send_operation_dm(context, game, user_id) # Resend main menu
    elif operation == 'loot':
        if len(player.get('inventory', [])) >= LOOT_ITEM_CAP:
            await query.answer(f"🎒 Cargo hold full! Max {LOOT_ITEM_CAP} items.", show_alert=True)
            return # Prevent looting if full
        else:
            await set_operation(query, context, game, user_id, operation, None, op_chat_id)
    elif operation in ['defend', 'heal']: # Actions without sub-menus
        await set_operation(query, context, game, user_id, operation, None, op_chat_id)
    else:
        logger.warning(f"⚠️ Unknown operation selected: {operation}")
        await query.answer("❓ Unknown action selected.", show_alert=True)

async def show_target_selection(query: Update.callback_query, context: ContextTypes.DEFAULT_TYPE, game: Game, user_id: int, chat_id: int):
    """Displays buttons for selecting an attack target (Fancy UI)."""
    targets_in_range = game.get_players_in_range(user_id)

    if not targets_in_range:
        # New Text: Clear message indicating no targets
        no_target_text = f"""
        📡 <b>Targeting Scan: Negative</b> 📡

        No enemy ship is in range ({ATTACK_RANGE} squares) to attack.

        Try moving closer or choose another action.
        """
        # Only provide the back button
        keyboard = [[InlineKeyboardButton("◀ Return to Console", callback_data=f"operation_back_{user_id}_{chat_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            # Edit the message (plain text console)
            await query.edit_message_text(no_target_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except BadRequest as e:
            # Ignore "message not modified" error, log others
            if 'message is not modified' not in str(e).lower():
                logger.warning(f"⚠️ Failed to edit 'no target' message: {e}")
        except Exception as e:
             logger.error(f"❌ Unexpected error editing 'no target' message: {e}", exc_info=True)
        return # Important: Stop further execution of the function

    # --- Build Target Buttons ---
    keyboard = []
    player_pos = game.players[user_id]['position']
    # Sort targets by distance, then HP (optional, but can be helpful)
    targets_in_range.sort(key=lambda tid: (
        abs(player_pos[0] - game.players[tid]['position'][0]) + abs(player_pos[1] - game.players[tid]['position'][1]),
        game.players[tid]['hp']
    ))

    for target_id in targets_in_range:
        target = game.players[target_id]
        name = escape_markdown_value(target.get('first_name', f'ID_{target_id}'))
        hp = target.get('hp', 0)
        max_hp = target.get('max_hp', HP_START)
        hp_indicator = get_hp_indicator(hp, max_hp)
        tx, ty = target.get('position', ('?', '?'))
        team_emoji = ""
        if game.mode == 'team': team_emoji = '🔵 ' if target.get('team') == 'alpha' else '🔴 '

        keyboard.append([
            InlineKeyboardButton(
                f"{team_emoji}{hp_indicator} {name} ({hp} HP) @ ({tx},{ty})",
                callback_data=f"target_{target_id}_{user_id}_{chat_id}" # targetID_attackerID_chatID
            )
        ])

    keyboard.append([InlineKeyboardButton("◀ Return to Console", callback_data=f"operation_back_{user_id}_{chat_id}")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    # --- Update Message ---
    target_prompt = f"""
    🎯 <b>Select Target for Attack</b> 🎯

    Choose an enemy vessel within range ({ATTACK_RANGE} squares) to engage.

    <b>Legend:</b> 🟢 High HP | 🟡 Med HP | 🔴 Low HP
    """
    try:
        await query.edit_message_text(target_prompt, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except BadRequest as e:
         if 'message is not modified' not in str(e).lower():
              logger.warning(f"⚠️ Failed to edit target selection message: {e}")
    except Exception as e:
        logger.error(f"❌ Unexpected error editing target selection: {e}", exc_info=True)

async def show_move_selection(query: Update.callback_query, context: ContextTypes.DEFAULT_TYPE, game: Game, user_id: int, chat_id: int):
    """Displays buttons for selecting movement direction (Fancy UI)."""
    player = game.players[user_id]
    px, py = player['position']
    map_size = game.map_size

    # --- Build Mini-Map ---
    mini_map_lines = []
    radius = 1 # Show 1 square around the player (3x3 view)
    for i in range(max(0, px - radius), min(map_size, px + radius + 1)):
        row_str = ""
        for j in range(max(0, py - radius), min(map_size, py + radius + 1)):
            cell_ids = game.map_grid[i][j]
            alive_here = [uid for uid in cell_ids if game.players.get(uid, {}).get('alive')]
            is_player_cell = (i == px and j == py)

            symbol = "⬛" # Empty space default (dark square for space vibe)
            if is_player_cell: symbol = "🚀" # Your ship
            elif alive_here:
                # Check if enemies or allies are present
                is_enemy = any(
                    (game.mode != 'team' and uid != user_id) or
                    (game.mode == 'team' and game.players.get(uid, {}).get('team') != player.get('team'))
                    for uid in alive_here
                )
                symbol = "👾" if is_enemy else "✨" # Enemy or Ally/Self symbol
            elif cell_ids: symbol = "💥" # Wreck symbol (less prominent than skull?)

            row_str += symbol
        mini_map_lines.append(row_str)
    mini_map_display = "\n".join(mini_map_lines)

    # --- Build Buttons ---
    keyboard = []
    # Simplified buttons with clear direction
    if px > 0: keyboard.append([InlineKeyboardButton("⬆️ Move North (Up)", callback_data=f"move_up_{user_id}_{chat_id}")])
    if px < map_size - 1: keyboard.append([InlineKeyboardButton("⬇️ Move South (Down)", callback_data=f"move_down_{user_id}_{chat_id}")])
    if py > 0: keyboard.append([InlineKeyboardButton("⬅️ Move West (Left)", callback_data=f"move_left_{user_id}_{chat_id}")])
    if py < map_size - 1: keyboard.append([InlineKeyboardButton("➡️ Move East (Right)", callback_data=f"move_right_{user_id}_{chat_id}")])

    keyboard.append([InlineKeyboardButton("◀ Return to Console", callback_data=f"operation_back_{user_id}_{chat_id}")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    # --- Update Message ---
    move_prompt = f"""
    🧭 <b>Navigation Control</b> 🧭

    <b>Current Sector View:</b>
    ```
{mini_map_display}
    ```
    (🚀 You | 👾 Enemy | ✨ Ally | 💥 Wreck | ⬛ Void)

    Select your vector, Captain. Current Position: ({px},{py})
    """
    try:
        # Using Markdown for the code block around the minimap
        await query.edit_message_text(move_prompt, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except BadRequest: pass # Ignore if not modified

async def set_operation(query: Update.callback_query, context: ContextTypes.DEFAULT_TYPE, game: Game, user_id: int, operation: str, target_id: Union[int, None], chat_id: int):
    """Confirms the chosen action and updates the DM (Fancy UI)."""
    player = game.players[user_id]
    player['operation'] = operation
    player['target'] = target_id
    player['last_action_time'] = datetime.now()
    player['afk_turns'] = 0 # Reset AFK on action confirmation

    # --- Friendly Names & Descriptions ---
    op_details = {
        'attack': {'name': '💥 Attack', 'desc': 'Engaging target!'},
        'defend': {'name': '🛡️ Defend', 'desc': 'Shields raised!'},
        'heal': {'name': '🔧 Heal', 'desc': 'Initiating repairs!'},
        'loot': {'name': '💎 Loot', 'desc': 'Scavenging sector!'},
        'move': {'name': '🧭 Move', 'desc': 'Changing position!'}
    }
    op_info = op_details.get(operation, {'name': operation.capitalize(), 'desc': 'Executing maneuver!'})

    # --- Status Update ---
    alive_players = game.get_alive_players()
    ready_count = sum(1 for uid in alive_players if game.players.get(uid, {}).get('operation') is not None)
    total_alive = len(alive_players)
    time_left = format_time((game.operation_end_time - datetime.now()).total_seconds()) if game.operation_end_time else 'N/A'

    # --- Assemble Fancy Confirmation ---
    confirmation_text = f"""
    ✅ <b>Orders Confirmed: {op_info['name']}</b> ✅

    {op_info['desc']}
    """
    if operation == 'attack' and target_id:
        target_name = escape_markdown_value(game.players.get(target_id, {}).get('first_name', f'ID_{target_id}'))
        confirmation_text += f"\n    Target Locked: {target_name}"
    elif operation == 'move':
         px, py = player['position']
         confirmation_text += f"\n    Destination: ({px},{py})" # Show where they moved TO

    confirmation_text += f"""

    ---
    <b>Fleet Status:</b> {ready_count}/{total_alive} Captains Ready
    <b>Time Remaining:</b> {time_left}
    ---

    Awaiting next cycle... ✨
    """

    # --- Add Button to go back to Group ---
    # Attempt to get group link (works best for public groups)
    group_link = f"https://t.me/c/{str(chat_id)[4:]}" if str(chat_id).startswith('-100') else None # Basic private group link guess
    try:
        chat_info = await context.bot.get_chat(chat_id)
        if chat_info.username:
             group_link = f"https://t.me/{chat_info.username}"
        elif chat_info.invite_link:
             group_link = chat_info.invite_link
    except Exception as e:
         logger.warning(f"⚠️ Could not get better group link for {chat_id}: {e}")

    keyboard = []
    if group_link:
        keyboard.append([InlineKeyboardButton(" GO to > Battle Arena ", url=group_link)])
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

    # --- Edit DM Message ---
    try:
        # Edit the text of the message that had the action buttons
        await query.edit_message_text(
            confirmation_text,
            reply_markup=reply_markup, # Show link back to group if available
            parse_mode=ParseMode.HTML
        )
    except BadRequest as e:
        if 'message is not modified' not in str(e).lower():
            logger.warning(f"⚠️ Failed to edit confirmation DM for {user_id}: {e}")
            # If edit fails, maybe just send a simple text confirmation?
            # await safe_send(context, user_id, f"✅ Orders Confirmed: {op_info['name']}")
    except Exception as e:
        logger.error(f"❌ Unexpected error editing confirmation DM: {e}", exc_info=True)

    await query.answer(f"✅ {op_info['name']} Confirmed!") # Quick feedback on button press

# --- ⏳ Operation Countdown ---
async def operation_countdown(context: ContextTypes.DEFAULT_TYPE, game: Game):
    """Manages the timer for action selection, sends reminders, and processes actions (Fancy UI)."""
    try:
        if game._operation_countdown_running: return # Prevent multiple instances
        game._operation_countdown_running = True
        logger.info(f"⏳ Starting operation countdown for Day {game.day}, Chat {game.chat_id}")

        last_update_time = datetime.now()
        last_reminder_times = {} # Track reminders sent per user per time point

        while game.is_active and game.operation_end_time and game.chat_id in games:
            remaining_sec = (game.operation_end_time - datetime.now()).total_seconds()

            if remaining_sec <= 0: break # Timer ended

            alive_ids = game.get_alive_players()
            if not alive_ids: break # No one left

            ready_count = sum(1 for uid in alive_ids if game.players[uid].get('operation') is not None)
            total_alive = len(alive_ids)

            # Check if everyone is ready
            if ready_count == total_alive:
                await safe_send(context, game.chat_id, f"🚀 <b>All Captains Ready!</b> Processing Day {game.day} actions...")
                break # Process actions early

            now = datetime.now()
            # Send periodic updates to the group chat (e.g., every 30s)
            if (now - last_update_time).total_seconds() >= 30:
                pending_players = [
                    mention(uid)
                    for uid in alive_ids if game.players[uid].get('operation') is None
                ]
                if pending_players:
                    pending_str = ", ".join(pending_players[:3]) # Show first 3 names
                    if len(pending_players) > 3: pending_str += f" + {len(pending_players) - 3} more"

                    update_text = build_card(
                        f"DAY {game.day} — AWAITING ORDERS",
                        [
                            f"⏳ Time Left : {format_time(remaining_sec)}",
                            f"✅ Ready : {ready_count}/{total_alive}",
                            f"⌛ Awaiting : {pending_str}",
                        ],
                        emoji="⏳",
                    )
                    await safe_send(context, game.chat_id, update_text, parse_mode=ParseMode.HTML)
                last_update_time = now

            # Send DM reminders at specific times (60, 30, 10s)
            remind_times = [60, 30, 10]
            current_remind_time = None
            for t in remind_times:
                if t - 1 < remaining_sec <= t: # Check if within the second window
                     current_remind_time = t
                     break

            if current_remind_time:
                for uid in alive_ids:
                    if game.players[uid].get('operation') is None:
                        # Send reminder only once per time point per user
                        if last_reminder_times.get(uid) != current_remind_time:
                            await safe_send(context, uid,
                                urgency_banner(uid, current_remind_time, f"Submit your Day {game.day} orders or risk an AFK strike!"),
                                parse_mode=ParseMode.HTML)
                            last_reminder_times[uid] = current_remind_time

            await asyncio.sleep(1) # Check roughly every second

        # --- Countdown finished or everyone ready ---
        if game.is_active and game.chat_id in games: # Check game hasn't been ended
             logger.info(f"✅ Operation countdown finished for Day {game.day}, Chat {game.chat_id}. Processing...")
             await process_day_operations(context, game)

    except Exception as e:
        logger.error(f"❌ Error during operation countdown for Chat {game.chat_id}: {e}", exc_info=True)
    finally:
        if game.chat_id in games: # Ensure game object still exists
            game._operation_countdown_running = False # Release the flag


# --- 🛑 End Game Command (Admin) ---
async def endgame_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forcefully ends the current game in the group (Admin/Owner only). Asks for confirmation first."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if update.effective_chat.type == 'private':
        await safe_send(context, chat_id, "❌ This command can only be used in a group.")
        return

    if chat_id not in games:
        await safe_send(context, chat_id, "ℹ️ No game is currently active in this chat.")
        return

    if not await is_admin_or_owner(context, chat_id, user_id):
        await safe_send(context, chat_id, "🚫 You do not have permission to end the game.")
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, end it", callback_data=f"confirm_endgame_{user_id}_{chat_id}"),
        InlineKeyboardButton("❌ No, cancel", callback_data=f"cancel_endgame_{user_id}_{chat_id}"),
    ]])
    await safe_send(
        context, chat_id,
        f"⚠️ {mention(user_id)}, are you sure you want to <b>forcefully end</b> the current battle? "
        f"No stats will be recorded for this session.",
        reply_markup=keyboard, parse_mode=ParseMode.HTML
    )


async def handle_endgame_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the Yes/No buttons from the /endgame confirmation prompt."""
    query = update.callback_query
    data = query.data
    try:
        action, _, requester_id_str, chat_id_str = data.split('_', 3)
        requester_id = int(requester_id_str)
        chat_id = int(chat_id_str)
    except ValueError:
        await query.answer("⚠️ Malformed request.", show_alert=True)
        return

    if query.from_user.id != requester_id:
        await query.answer("✋ Only the admin who ran /endgame can confirm this.", show_alert=True)
        return

    if data.startswith("cancel_endgame_"):
        await query.answer("Cancelled.")
        await query.edit_message_text("✅ Endgame cancelled — the battle continues.")
        return

    # Confirmed
    if chat_id not in games:
        await query.answer("ℹ️ Game already ended.", show_alert=True)
        await query.edit_message_text("ℹ️ No game is currently active in this chat.")
        return

    game = games[chat_id]
    game.is_active = False
    game.is_joining = False
    game.operation_end_time = None

    logger.warning(f"🛑 Game in chat {chat_id} force-ended by admin {requester_id}.")

    await query.answer("🛑 Game ended.")
    await query.edit_message_text(
        f"🛑 <b>Game Terminated by Admin!</b> 🛑\n\n{mention(requester_id)} has ended the current battle.\nNo stats will be recorded for this session.",
        parse_mode=ParseMode.HTML
    )

    del games[chat_id]

# ✨ ======================== CORE GAME LOGIC ======================== ✨

async def apply_cosmic_event(context: ContextTypes.DEFAULT_TYPE, game: Game, event_key: str, event_data: dict) -> list[str]:
    """Applies the effects of a triggered cosmic event and returns log messages."""
    effect_type = event_data.get('effect')
    value = event_data.get('value')
    event_log = [] # List to store messages describing event effects

    logger.info(f"Applying cosmic event '{event_key}' in chat {game.chat_id}")

    if effect_type == 'damage_all':
        damage = random.randint(*value) if isinstance(value, tuple) else value
        event_log.append(f"💥 All ships caught in the storm take {damage} damage!")
        for user_id, player in game.players.items():
            if player.get('alive'):
                player['hp'] -= damage
                player['stats']['damage_taken'] = player['stats'].get('damage_taken', 0) + damage
                # event_log.append(f"   - {escape_markdown_value(player['first_name'])}: {damage} DMG") # More detailed log if needed

    elif effect_type == 'heal_all':
        heal = random.randint(*value) if isinstance(value, tuple) else value
        event_log.append(f"☀️ A wave of energy repairs ships by {heal} HP!")
        for user_id, player in game.players.items():
            if player.get('alive'):
                old_hp = player['hp']
                player['hp'] = min(player.get('max_hp', HP_START), player['hp'] + heal)
                healed_amount = player['hp'] - old_hp
                player['stats']['heals_done'] = player['stats'].get('heals_done', 0) + healed_amount
                # event_log.append(f"   - {escape_markdown_value(player['first_name'])}: +{healed_amount} HP")

    elif effect_type == 'teleport':
        alive_ids = game.get_alive_players()
        num_to_teleport = min(3, len(alive_ids)) # Teleport up to 3 players
        if num_to_teleport > 0:
            teleported_ids = random.sample(alive_ids, num_to_teleport)
            event_log.append("🌀 Wormholes shift positions!")
            for user_id in teleported_ids:
                player = game.players[user_id]
                old_x, old_y = player['position']
                # Remove from old grid position safely
                try:
                    if user_id in game.map_grid[old_x][old_y]: game.map_grid[old_x][old_y].remove(user_id)
                except IndexError: pass
                # Find new random position
                new_x, new_y = random.randint(0, game.map_size - 1), random.randint(0, game.map_size - 1)
                player['position'] = (new_x, new_y)
                # Add to new grid position safely
                try: game.map_grid[new_x][new_y].append(user_id)
                except IndexError: pass
                event_log.append(f"   - {mention(user_id)} warped to ({new_x},{new_y})!")

    elif effect_type == 'damage_boost':
        game.event_effect = {'type': 'damage_boost', 'value': value}
        boost_percent = int((value - 1) * 100)
        event_log.append(f"⚡ Energy surge! Attacks deal +{boost_percent}% damage next turn!")

    elif effect_type == 'shield_all':
        game.event_effect = {'type': 'shield', 'value': value}
        shield_percent = int(value * 100)
        event_log.append(f"🛡️ Nebula provides a {shield_percent}% shield to all next turn!")

    elif effect_type == 'random_damage':
        alive_ids = game.get_alive_players()
        num_targets = min(2, len(alive_ids)) # Attack up to 2 random players
        if num_targets > 0:
            target_ids = random.sample(alive_ids, num_targets)
            damage = random.randint(*value) if isinstance(value, tuple) else value
            event_log.append(f"🏴‍☠️ Pirates attack! {damage} damage dealt!")
            for user_id in target_ids:
                player = game.players[user_id]
                player['hp'] -= damage
                player['stats']['damage_taken'] = player['stats'].get('damage_taken', 0) + damage
                event_log.append(f"   - {mention(user_id)} hit for {damage} DMG!")

    # Add more event effect logic here if needed

    return event_log


async def process_day_operations(context: ContextTypes.DEFAULT_TYPE, game: Game):
    """Processes all player actions for the current day, applies effects, and generates a summary."""
    day = game.day
    chat_id = game.chat_id
    logger.info(f"Processing Day {day} operations for chat {chat_id}")

    await safe_send(context, chat_id, f"⏳ Processing actions for Day {day}... Stand by, Captains!")
    await asyncio.sleep(2) # Brief pause for effect

    # --- Preparation ---
    game.update_alliances() # Decrement alliance timers
    summary_log: list[str] = [f"✨ <b>Day {day} - Action Report</b> ✨\n"] # Start summary log
    fancy_separator = "〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️〰️"

    # --- Safe Zone Update & Damage ---
    zone_shrink_msg = game.update_safe_zone()
    safe_zone_damage_log = []
    if zone_shrink_msg:
        summary_log.append(zone_shrink_msg) # Add shrink message directly
        summary_log.append(fancy_separator)

    # Apply damage if players are outside the *new* zone radius
    for user_id, player in game.players.items():
        if player.get('alive'):
            px, py = player['position']
            if not game.is_in_safe_zone(px, py):
                player['hp'] -= SAFE_ZONE_DAMAGE
                player['stats']['damage_taken'] = player['stats'].get('damage_taken', 0) + SAFE_ZONE_DAMAGE
                safe_zone_damage_log.append(f"  🔴 {mention(user_id)} took {SAFE_ZONE_DAMAGE} DMG from the Danger Zone!")

    if safe_zone_damage_log:
        summary_log.append("🌀 <b>Void Pressure Alert!</b>")
        summary_log.extend(safe_zone_damage_log)
        summary_log.append(fancy_separator)

    # --- Cosmic Event ---
    event_key, event_data = trigger_cosmic_event()
    if event_key and event_data:
        # Send a separate alert for the event itself
        await safe_send_animation(
            context, chat_id, get_random_gif('event'),
            caption=build_card("COSMIC EVENT", [f"{event_data['emoji']} <b>{event_data['name']}</b>", event_data['desc'], "", "Effects are unfolding..."], emoji="🌌"),
            parse_mode=ParseMode.HTML
        )
        await asyncio.sleep(2)
        event_log_msgs = await apply_cosmic_event(context, game, event_key, event_data)
        if event_log_msgs:
            summary_log.append(f"🌌 <b>Cosmic Event: {event_data['name']}</b>")
            summary_log.extend([f"  {msg}" for msg in event_log_msgs]) # Indent event details
            summary_log.append(fancy_separator)

    # --- AFK Processing ---
    afk_log = []
    eliminated_by_afk = []
    for user_id, player in game.players.items():
        if player.get('alive') and player.get('operation') is None: # No action submitted
            player['afk_turns'] = player.get('afk_turns', 0) + 1
            if player['afk_turns'] >= AFK_TURNS_LIMIT:
                player['alive'] = False # Eliminate
                player['hp'] = 0
                eliminated_by_afk.append(f"  👻 {mention(user_id)} lost contact! (Eliminated for AFK)")
                # Send DM to eliminated player
                await safe_send_animation(context, user_id, get_random_gif('eliminated'),
                                          caption=f"🛰️ <b>Connection Lost - Day {day}</b> 🛰️\nYour ship failed to report for {AFK_TURNS_LIMIT} cycles and was lost to the void. Stay responsive next time!",
                                          parse_mode=ParseMode.HTML)
            else:
                player['operation'] = 'defend' # Auto-defend
                afk_log.append(f"  ⏳ {mention(user_id)} unresponsive, auto-shields raised! (AFK {player['afk_turns']}/{AFK_TURNS_LIMIT})")

    if afk_log:
        summary_log.append("📡 <b>Comms Link Status</b>")
        summary_log.extend(afk_log)
        summary_log.append(fancy_separator)
    if eliminated_by_afk:
        # Add AFK eliminations to the main elimination log later
        pass

    # --- Sort Operations ---
    attacks = defaultdict(list) # {target_id: [attacker_id1, attacker_id2,...]}
    defenders = set()
    healers = set()
    looters = set()
    movers = [] # Store user_ids who moved
    betrayals = {} # {attacker_id: target_id}

    for user_id, player in game.players.items():
        if not player.get('alive'): continue
        op = player.get('operation')
        target = player.get('target')

        if op == 'attack' and target:
            # Check for betrayal
            if user_id in game.alliances and game.alliances[user_id]['ally'] == target:
                betrayals[user_id] = target
                game.break_alliance(user_id) # Break alliance immediately
            # Check if target is still valid and in range (re-check in case target moved/died)
            if target in game.players and game.players[target].get('alive') and target in game.get_players_in_range(user_id):
                 attacks[target].append(user_id)
            # else: logger.debug(f"Attack by {user_id} on {target} invalid/out of range.") # Optional debug
        elif op == 'defend': defenders.add(user_id)
        elif op == 'heal': healers.add(user_id)
        elif op == 'loot': looters.add(user_id)
        elif op == 'move': movers.append(user_id)
        # else: No operation or invalid one

    # --- Process Actions ---
    combat_log = []
    heal_log = []
    loot_log = []
    move_log = []
    elimination_log = eliminated_by_afk # Start with AFK eliminations

    base_attack = random.randint(*ATTACK_DAMAGE)
    base_heal = random.randint(*HEAL_AMOUNT)

    # Apply event multipliers if active
    current_attack_mult = game.event_effect.get('value', 1.0) if game.event_effect and game.event_effect.get('type') == 'damage_boost' else 1.0
    current_heal_mult = game.event_effect.get('value', 1.0) if game.event_effect and game.event_effect.get('type') == 'heal_multiplier' else 1.0
    global_shield_reduction = game.event_effect.get('value', 0.0) if game.event_effect and game.event_effect.get('type') == 'shield' else 0.0

    # 1. Attacks
    for target_id, attacker_ids in attacks.items():
        if target_id not in game.players or not game.players[target_id].get('alive'): continue # Target already dead/gone

        target_player = game.players[target_id]
        target_name = mention(target_id)
        total_incoming_damage = 0
        attack_details = [] # Store strings describing each attack

        for attacker_id in attacker_ids:
            if attacker_id not in game.players or not game.players[attacker_id].get('alive'): continue # Attacker died?

            attacker_player = game.players[attacker_id]
            attacker_name = mention(attacker_id)
            damage = int(base_attack * current_attack_mult)
            is_crit = False
            is_betrayal = (attacker_id in betrayals and betrayals[attacker_id] == target_id)
            bonus_notes = []

            # Check for Speed Boost double tap
            if 'speed_boost' in attacker_player.get('inventory', []):
                attacker_player['inventory'].remove('speed_boost') # Consume item
                if random.random() < 0.6: # 60% chance
                    bonus_damage = int(random.randint(*ATTACK_DAMAGE) * current_attack_mult)
                    total_incoming_damage += bonus_damage
                    attacker_player['stats']['damage_dealt'] += bonus_damage
                    combat_log.append(f"  💨 {attacker_name} landed a rapid follow-up shot on {target_name} for {bonus_damage} bonus DMG!")

            # Check for Weapon item
            weapon_bonus = 0
            weapon_used = None
            for item_key in attacker_player.get('inventory', []):
                item = LOOT_ITEMS.get(item_key)
                if item and item['type'] == 'weapon':
                    weapon_bonus = item['bonus']
                    weapon_used = item['emoji']
                    attacker_player['inventory'].remove(item_key) # Consume weapon
                    bonus_notes.append(weapon_used)
                    break # Use only one weapon per attack
            damage += weapon_bonus

            # Check for Crit
            if random.random() < CRIT_CHANCE:
                damage = int(damage * CRIT_MULTIPLIER)
                is_crit = True
                bonus_notes.append("💥Crit!")

            # Check for Betrayal Bonus
            if is_betrayal:
                damage = int(damage * BETRAYAL_DAMAGE_BONUS)
                bonus_notes.append("😈Betrayal!")

            total_incoming_damage += damage
            attacker_player['stats']['damage_dealt'] += damage
            attack_details.append(f"{attacker_name}{('(' + ''.join(bonus_notes) + ')') if bonus_notes else ''}")

        # Calculate target's defense
        defense_reduction = global_shield_reduction
        shield_notes = []
        if target_id in defenders:
            defense_reduction += DEFEND_REDUCTION
            shield_notes.append("🛡️Defend")

        # Check for Shield item
        shield_bonus = 0.0
        shield_used = None
        for item_key in target_player.get('inventory', []):
            item = LOOT_ITEMS.get(item_key)
            if item and item['type'] == 'shield':
                shield_bonus = item['bonus']
                shield_used = item['emoji']
                target_player['inventory'].remove(item_key) # Consume shield
                shield_notes.append(shield_used)
                break # Use only one shield
        defense_reduction += shield_bonus

        # Apply EMP Grenade effect (halves damage *after* other bonuses but *before* defense)
        emp_active = False
        if 'emp_grenade' in target_player.get('inventory', []):
             target_player['inventory'].remove('emp_grenade') # Consume EMP
             total_incoming_damage = int(total_incoming_damage * 0.5)
             emp_active = True
             shield_notes.append("💣EMP")

        # Apply total defense, capped at ~80% reduction
        defense_reduction = min(0.8, defense_reduction)
        final_damage = int(total_incoming_damage * (1.0 - defense_reduction))

        # Apply damage to target
        target_player['hp'] -= final_damage
        target_player['stats']['damage_taken'] += final_damage
        hp_indicator = get_hp_indicator(target_player['hp'], target_player['max_hp'])
        def_text = f" ({''.join(shield_notes)} Blocked {int(defense_reduction*100)}%)" if shield_notes else ""

        attackers_str = ", ".join(attack_details)
        combat_log.append(f"  💥 {attackers_str} -> {hp_indicator} {target_name}: {final_damage} DMG{def_text}")

    # 2. Heals
    for user_id in healers:
        if user_id not in game.players or not game.players[user_id].get('alive'): continue
        player = game.players[user_id]
        heal_amount = int(base_heal * current_heal_mult)
        old_hp = player['hp']
        player['hp'] = min(player.get('max_hp', HP_START), player['hp'] + heal_amount)
        actual_heal = player['hp'] - old_hp
        player['stats']['heals_done'] = player['stats'].get('heals_done', 0) + actual_heal
        if actual_heal > 0:
            hp_indicator = get_hp_indicator(player['hp'], player['max_hp'])
            heal_log.append(f"  🔧 {hp_indicator} {mention(user_id)} repaired +{actual_heal} HP.")

    # 3. Loots
    for user_id in looters:
        if user_id not in game.players or not game.players[user_id].get('alive'): continue
        player = game.players[user_id]
        player['stats']['loots'] = player['stats'].get('loots', 0) + 1

        # 🎲 50% chance to find anything at all when scavenging a sector
        if random.random() >= 0.5:
            loot_log.append(f"  ❓ {mention(user_id)} found nothing of value.")
            continue

        # Determine item rarity based on weights
        rarity_choices = [r for r, w in RARITY_WEIGHTS.items() for _ in range(w)]
        chosen_rarity = random.choice(rarity_choices)
        # Get items of that rarity
        possible_items = [k for k, v in LOOT_ITEMS.items() if v['rarity'] == chosen_rarity]

        if not possible_items:
            loot_log.append(f"  ❓ {mention(user_id)} found nothing of value.")
            continue # No items defined for this rarity?

        item_key = random.choice(possible_items)
        item_data = LOOT_ITEMS[item_key]
        item_name = item_key.replace('_', ' ').title()
        rarity_color = get_rarity_color(item_data['rarity'])

        if item_data['type'] == 'energy':
            # Instant use energy items
            heal_bonus = item_data['bonus']
            old_hp = player['hp']
            player['hp'] = min(player.get('max_hp', HP_START), player['hp'] + heal_bonus)
            actual_heal = player['hp'] - old_hp
            player['stats']['heals_done'] = player['stats'].get('heals_done', 0) + actual_heal
            loot_log.append(f"  💎 {rarity_color} {mention(user_id)} found {item_data['emoji']} {item_name}! (+{actual_heal} HP)")
        elif len(player.get('inventory', [])) < LOOT_ITEM_CAP:
            # Add item to inventory if space allows
            player['inventory'].append(item_key)
            inv_count = len(player['inventory'])
            loot_log.append(f"  💎 {rarity_color} {mention(user_id)} acquired {item_data['emoji']} {item_name}! (Cargo: {inv_count}/{LOOT_ITEM_CAP})")
        else:
            # Inventory full
            loot_log.append(f"  ⚠️ {rarity_color} {mention(user_id)} found {item_data['emoji']} {item_name}, but cargo hold is full! ({LOOT_ITEM_CAP}/{LOOT_ITEM_CAP})")

    # 4. Moves (Just log final positions)
    for user_id in movers:
        if user_id in game.players and game.players[user_id].get('alive'):
            player = game.players[user_id]
            px, py = player['position']
            move_log.append(f"  🧭 {mention(user_id)} moved to ({px},{py}).")

    # --- Assemble Logs for Summary ---
    if combat_log:
        summary_log.append("⚔️ <b>Combat Report</b>")
        summary_log.extend(combat_log)
        summary_log.append(fancy_separator)
    if heal_log:
        summary_log.append("🔧 <b>Repair Log</b>")
        summary_log.extend(heal_log)
        summary_log.append(fancy_separator)
    if loot_log:
        summary_log.append("💎 <b>Loot Findings</b>")
        summary_log.extend(loot_log)
        summary_log.append(fancy_separator)
    if move_log:
        summary_log.append("🧭 <b>Navigation Log</b>")
        summary_log.extend(move_log)
        summary_log.append(fancy_separator)

    # --- Process Eliminations (from combat, zone damage etc.) ---
    for user_id, player in list(game.players.items()): # Iterate on copy
        if player.get('alive') and player.get('hp', 0) <= 0:
            player['alive'] = False
            elimination_log.append(f"  💀 {mention(user_id)}'s ship was destroyed!")

            # Send DM to eliminated player
            await safe_send_animation(context, user_id, get_random_gif('eliminated'),
                                      caption=f"💥 <b>Ship Destroyed - Day {day}</b> 💥\nYour vessel has succumbed to damage. Better luck next battle!\n\nFinal Stats for this game:\nKills: {player['stats']['kills']} | Damage Dealt: {player['stats']['damage_dealt']}",
                                      parse_mode=ParseMode.HTML)

            # Achievement checks for attackers can go here (First Blood, Betrayer Kill etc.)
            # Example: Find who dealt the killing blow if needed by tracking damage sources more closely

    if elimination_log: # Includes AFK eliminations
        summary_log.append("☠️ <b>Eliminations</b>")
        summary_log.extend(elimination_log)
        summary_log.append(fancy_separator)

    # --- Final Survivor List ---
    alive_ids = game.get_alive_players()
    summary_log.append(f"📊 <b>Survivors ({len(alive_ids)})</b>")
    if alive_ids:
        player_stats_list = [
            (uid, game.players[uid]['hp'], game.players[uid]['stats'].get('kills', 0), game.players[uid]['position'])
            for uid in alive_ids
        ]
        sorted_players = sorted(player_stats_list, key=lambda x: (x[1], x[2]), reverse=True) # Sort by HP, then Kills

        for i, (uid, hp, kills, pos) in enumerate(sorted_players, 1):
            player = game.players[uid]
            name = mention(uid)
            hp_indicator = get_hp_indicator(hp, player['max_hp'])
            summary_log.append(f"  {i}. {hp_indicator} {name} - {int(hp)} HP | {kills} Kills @ ({pos[0]},{pos[1]})")
    else:
        summary_log.append("  < No survivors >")

    # --- Send Summary ---
    summary_text = "\n".join(summary_log)
    await safe_send_animation(context, chat_id, get_random_gif('day_summary'),
                              caption=summary_text, parse_mode=ParseMode.HTML)

    # --- Reset for Next Day / Check End Game ---
    game.event_effect = None # Clear temporary event effects
    for player in game.players.values(): # Reset operation choices
        player['operation'] = None
        player['target'] = None

    # --- Check Game End Conditions ---
    alive_ids = game.get_alive_players() # Re-check after all processing
    if game.mode == 'solo':
        if len(alive_ids) <= 1:
            await end_game(context, game, alive_ids) # Pass list of alive IDs
        else:
            await continue_next_day(context, game)
    elif game.mode == 'team':
        alpha_alive = game.get_alive_team_players('alpha')
        beta_alive = game.get_alive_team_players('beta')
        if not alpha_alive or not beta_alive: # One team wiped out
            await end_team_game(context, game, alpha_alive, beta_alive)
        else:
            await continue_next_day(context, game)
    else: # Should not happen
         logger.error(f"❌ Invalid game mode '{game.mode}' during end-of-day check for chat {chat_id}.")
         await safe_send(context, chat_id, "❌ Internal Error: Invalid game mode detected. Game cancelled.")
         if chat_id in games: del games[chat_id]


async def continue_next_day(context: ContextTypes.DEFAULT_TYPE, game: Game):
    """Prepares and announces the start of the next game day."""
    game.day += 1
    logger.info(f"Continuing to Day {game.day} for game in chat {game.chat_id}")
    await asyncio.sleep(3) # Short pause before next day starts

    # Fancy announcement for the new day
    next_day_text = f"""
    ☀️ <b>Day {game.day} Dawns!</b> ☀️

    The battle continues! Check your DMs to issue new orders, Captains.
    """

    await safe_send(context, game.chat_id, next_day_text, parse_mode=ParseMode.HTML)

    # Send/refresh the interactive button map
    await safe_send(context, game.chat_id, game.get_map_header_card(), parse_mode=ParseMode.HTML)
    map_msg = await safe_send(context, game.chat_id, "🗺 Tap a cell to inspect it:", reply_markup=game.get_map_keyboard())
    if map_msg:
        game.last_map_message_id = map_msg.message_id

    # Send action prompts to all living players
    alive_ids = game.get_alive_players()
    for user_id in alive_ids:
        await send_operation_choice_button(context, game, user_id)

    # Start the countdown for the new day's actions
    game.operation_end_time = datetime.now() + timedelta(seconds=game.settings['operation_time'])
    asyncio.create_task(operation_countdown(context, game))

# ✨ ======================== END GAME LOGIC ======================== ✨

async def end_game(context: ContextTypes.DEFAULT_TYPE, game: Game, alive_players: list[int]):
    """Handles the end of a Solo game, declares winner, updates stats (Fancy UI)."""
    game.is_active = False
    game.is_joining = False
    game.operation_end_time = None
    chat_id = game.chat_id
    fancy_separator = "🎉 • ⋅ ⋅ ────────── ⋅ ⋅ • 🎉"

    if alive_players: # We have a winner!
        winner_id = alive_players[0]
        winner_data = game.players.get(winner_id)
        if not winner_data:
            logger.error(f"❌ Winner data not found for ID {winner_id} at end_game in chat {chat_id}.")
            await safe_send(context, chat_id, "⚠️ Error determining the winner. Game ended.")
            if chat_id in games: del games[chat_id]
            return

        winner_name = escape_markdown_value(winner_data['first_name'])
        winner_stats_ingame = winner_data['stats']

        # --- Calculate Rewards & Update Global Stats ---
        score_gain = calculate_score(1, winner_stats_ingame.get('kills', 0), winner_stats_ingame.get('damage_dealt', 0))
        coins_earned = WIN_COIN_BONUS

        global_stats = get_player_stats(winner_id)
        current_streak = (global_stats[11] if global_stats else 0) + 1
        best_streak = max(current_streak, (global_stats[12] if global_stats else 0))

        new_total_balance = add_player_coins(winner_id, coins_earned, f"SOLO WIN - Day {game.day}")

        stats_update = {
            'total_games': 1, 'wins': 1, 'losses': 0, 'deaths': 0, # Increment win, not loss/death
            'kills': winner_stats_ingame.get('kills', 0),
            'damage_dealt': winner_stats_ingame.get('damage_dealt', 0),
            'damage_taken': winner_stats_ingame.get('damage_taken', 0),
            'heals_done': winner_stats_ingame.get('heals_done', 0),
            'loots_collected': winner_stats_ingame.get('loots', 0),
            'total_score': score_gain,
            'win_streak': current_streak, # Set the new streak
            'best_streak': best_streak
        }
        update_player_stats(winner_id, winner_data['username'], stats_update)
        save_game_history(game, winner_id, winner_data['first_name']) # Log the game result

        # --- Achievement Checks ---
        if unlock_achievement(winner_id, 'survivor'):
            await safe_send(context, winner_id, "🏆 Achievement Unlocked: <b>Survivor</b> - Claimed your first victory!")
        if current_streak >= 3 and unlock_achievement(winner_id, 'streak_3'):
            await safe_send(context, winner_id, "🔥 Achievement Unlocked: <b>Winning Streak</b> - Achieved a 3-win streak!")
        # Add more achievement checks here (e.g., champion)

        # --- Premium Victory Card ---
        victory_caption = build_card(
            "👑 VICTORY ROYALE!",
            [
                f"Champion : {mention(winner_id)} — {game.day} days survived!",
                "",
                "⚔️ FINAL BATTLE STATS",
            ] + branch_lines([
                f"❤️ Hull : {winner_data.get('hp', 0)}/{winner_data.get('max_hp', HP_START)} HP",
                f"💥 Eliminations : {winner_stats_ingame.get('kills', 0)}",
                f"⚔️ Damage Inflicted : {winner_stats_ingame.get('damage_dealt', 0)}",
                f"🔥 Win Streak : {current_streak}",
            ]) + [
                "",
                "🏆 SPOILS OF WAR",
            ] + branch_lines([
                f"⭐ Score Gained : +{score_gain}",
                f"🪙 Coins Awarded : +{coins_earned}",
                f"💰 New Balance : {new_total_balance} Coins",
            ]) + [
                "",
                "GG WP! Start anew with /creategame",
            ],
            emoji="👑",
        )
        await safe_send_animation(context, chat_id, get_random_gif('victory'),
                                  caption=victory_caption, parse_mode=ParseMode.HTML)

    else: # No survivors - Draw
        draw_caption = build_card(
            "💥 MUTUAL ANNIHILATION",
            [
                f"All vessels destroyed on Day {game.day}!",
                "The battle ends in a draw — no victor claims the spoils.",
                "",
                "Start anew with /creategame",
            ],
            emoji="💥",
        )
        await safe_send_animation(context, chat_id, get_random_gif('eliminated'),
                                  caption=draw_caption, parse_mode=ParseMode.HTML)
        # Log draw in history? Maybe with winner_id=0 or None
        save_game_history(game, 0, "Draw")

    # --- Update Stats for Losers/Draw Participants ---
    participation_coins = 20 # Coins for playing
    for user_id, player_data in game.players.items():
        # Update everyone *except* the winner (if there was one)
        if not alive_players or user_id != alive_players[0]:
            player_stats_ingame = player_data.get('stats', {})
            loser_score_gain = calculate_score(0, player_stats_ingame.get('kills', 0), player_stats_ingame.get('damage_dealt', 0))
            
            # Add participation coins if they didn't win
            add_player_coins(user_id, participation_coins, f"Participation - Day {game.day}")

            stats_update = {
                'total_games': 1, 'wins': 0, 'losses': 1, 'deaths': 1, # Increment loss/death
                'kills': player_stats_ingame.get('kills', 0),
                'damage_dealt': player_stats_ingame.get('damage_dealt', 0),
                'damage_taken': player_stats_ingame.get('damage_taken', 0),
                'heals_done': player_stats_ingame.get('heals_done', 0),
                'loots_collected': player_stats_ingame.get('loots', 0),
                'total_score': loser_score_gain,
                'win_streak': 0 # Reset streak on loss/draw
                # 'best_streak' is not updated here
            }
            update_player_stats(user_id, player_data.get('username'), stats_update)

    # --- Clean up Game State ---
    if chat_id in games:
        del games[chat_id]
    logger.info(f"✅ Solo game ended in chat {chat_id}.")


async def end_team_game(context: ContextTypes.DEFAULT_TYPE, game: Game, alpha_alive: list[int], beta_alive: list[int]):
    """Handles the end of a Team game, declares winner, updates stats (Fancy UI)."""
    game.is_active = False
    game.is_joining = False
    game.operation_end_time = None
    chat_id = game.chat_id
    fancy_separator = "🏆 • ⋅ ⋅ ────────── ⋅ ⋅ • 🏆"

    winning_team_name = None
    winning_emoji = ""
    winners_ids = []
    losers_team_name = None

    if alpha_alive and not beta_alive:
        winning_team_name = "Alpha"
        winning_emoji = "🔵"
        winners_ids = alpha_alive
        losers_team_name = 'beta'
    elif beta_alive and not alpha_alive:
        winning_team_name = "Beta"
        winning_emoji = "🔴"
        winners_ids = beta_alive
        losers_team_name = 'alpha'
    else: # Draw - Both teams wiped out simultaneously? Or error?
        await safe_send_animation(context, chat_id, get_random_gif('eliminated'),
                                  caption=build_card("MUTUAL DESTRUCTION", [f"Both Alpha and Beta forces were eliminated on Day {game.day}!", "The battle is a draw."], emoji="💥"),
                                  parse_mode=ParseMode.HTML)
        save_game_history(game, 0, "Team Draw")
        # Update stats for all as loss/draw (similar to solo)
        participation_coins = 20
        for user_id, player_data in game.players.items():
            player_stats_ingame = player_data.get('stats', {})
            score_gain = calculate_score(0, player_stats_ingame.get('kills', 0), player_stats_ingame.get('damage_dealt', 0))
            add_player_coins(user_id, participation_coins, f"Team Draw Participation - Day {game.day}")
            update_player_stats(user_id, player_data.get('username'), {
                'total_games': 1, 'wins': 0, 'losses': 1, 'deaths': 1,
                'kills': player_stats_ingame.get('kills', 0), 'damage_dealt': player_stats_ingame.get('damage_dealt', 0),
                'damage_taken': player_stats_ingame.get('damage_taken', 0), 'heals_done': player_stats_ingame.get('heals_done', 0),
                'loots_collected': player_stats_ingame.get('loots', 0), 'total_score': score_gain, 'win_streak': 0
            })
        if chat_id in games: del games[chat_id]
        logger.info(f"🤝 Team game ended in a draw in chat {chat_id}.")
        return

    # --- Process Winners ---
    for winner_id in winners_ids:
        player_data = game.players.get(winner_id)
        if not player_data: continue
        player_stats_ingame = player_data.get('stats', {})

        score_gain = calculate_score(1, player_stats_ingame.get('kills', 0), player_stats_ingame.get('damage_dealt', 0))
        coins_earned = WIN_COIN_BONUS # Full bonus for team win

        add_player_coins(winner_id, coins_earned, f"TEAM WIN - Day {game.day}")
        
        # Update winner stats (increment win streak, etc.)
        global_stats = get_player_stats(winner_id)
        current_streak = (global_stats[11] if global_stats else 0) + 1
        best_streak = max(current_streak, (global_stats[12] if global_stats else 0))

        update_player_stats(winner_id, player_data.get('username'), {
            'total_games': 1, 'wins': 1, 'losses': 0, 'deaths': 0,
            'kills': player_stats_ingame.get('kills', 0), 'damage_dealt': player_stats_ingame.get('damage_dealt', 0),
            'damage_taken': player_stats_ingame.get('damage_taken', 0), 'heals_done': player_stats_ingame.get('heals_done', 0),
            'loots_collected': player_stats_ingame.get('loots', 0), 'total_score': score_gain,
            'win_streak': current_streak, 'best_streak': best_streak
        })

        if unlock_achievement(winner_id, 'team_player'):
            await safe_send(context, winner_id, "🤝 Achievement Unlocked: <b>Team Player</b> - Secured victory with your squadron!")

    save_game_history(game, winners_ids[0], f"Team {winning_team_name}") # Log win

    # --- Premium Team Victory Card ---
    victory_caption = build_card(
        f"TEAM {winning_team_name.upper()} VICTORY",
        [
            f"{winning_emoji} Team {winning_team_name} triumphed after {game.day} days of battle!",
            "",
            "🏆 VICTORIOUS CAPTAINS",
        ] + branch_lines([f"{winning_emoji} {mention(wid)}" for wid in winners_ids]) + [
            "",
            "Well played! Start a new campaign with /creategame",
        ],
        emoji="🎉",
    )
    await safe_send_animation(context, chat_id, get_random_gif('victory'),
                              caption=victory_caption, parse_mode=ParseMode.HTML)

    # --- Update Stats for Losers ---
    participation_coins = 20
    if losers_team_name and losers_team_name in game.teams:
        for loser_id in game.teams[losers_team_name]:
            player_data = game.players.get(loser_id)
            if not player_data: continue
            player_stats_ingame = player_data.get('stats', {})
            score_gain = calculate_score(0, player_stats_ingame.get('kills', 0), player_stats_ingame.get('damage_dealt', 0))
            
            add_player_coins(loser_id, participation_coins, f"Team Loss Participation - Day {game.day}")
            
            update_player_stats(loser_id, player_data.get('username'), {
                'total_games': 1, 'wins': 0, 'losses': 1, 'deaths': 1,
                'kills': player_stats_ingame.get('kills', 0), 'damage_dealt': player_stats_ingame.get('damage_dealt', 0),
                'damage_taken': player_stats_ingame.get('damage_taken', 0), 'heals_done': player_stats_ingame.get('heals_done', 0),
                'loots_collected': player_stats_ingame.get('loots', 0), 'total_score': score_gain, 'win_streak': 0
            })

    # --- Clean up Game State ---
    if chat_id in games:
        del games[chat_id]
    logger.info(f"✅ Team game ended in chat {chat_id}. Winner: Team {winning_team_name}.")


# --- 💰 Daily Command ---
async def daily_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows players to claim their daily coin reward (Fancy UI)."""
    user = update.effective_user
    user_id = user.id

    if is_globally_banned(user_id): return # Silently ignore
    # No spam check usually needed for daily, but keep if preferred

    stats = get_player_stats(user_id)
    if not stats: # Register if first time
        update_player_stats(user_id, user.username, {})
        stats = get_player_stats(user_id)

    now = datetime.now()
    fancy_separator = "🪙 • ⋅ ⋅ ────────── ⋅ ⋅ • 🪙"

    # --- Check Cooldown ---
    last_claim_time = LAST_DAILY_CLAIM.get(user_id)
    if last_claim_time:
        time_since_last = now - last_claim_time
        if time_since_last.total_seconds() < 24 * 3600: # 24 hours cooldown
            time_remaining = timedelta(hours=24) - time_since_last
            hours, remainder = divmod(int(time_remaining.total_seconds()), 3600)
            minutes, seconds = divmod(remainder, 60)
            wait_time = f"{hours}h {minutes}m {seconds}s"

            wait_caption = f"""
            ⏳ <b>Daily Reward Not Ready</b> ⏳

            Your next coin reward shipment arrives in:
            <b>{wait_time}</b>

            Patience, Captain! Check back later.
            {fancy_separator}
            Tip: Use <code>/shop</code> to spend your current coins!
            """
            await safe_send_photo(context, user_id, get_random_image('daily'), caption=wait_caption)
            return

    # --- Calculate Reward ---
    coins_to_add = DAILY_COIN_AMOUNT
    streak_bonus = 0
    win_streak = stats[11] if stats and len(stats) > 11 else 0
    if win_streak > 0:
        streak_bonus = min(win_streak * 10, 100) # Bonus up to 100 coins
        coins_to_add += streak_bonus

    # --- Grant Reward ---
    current_coins = get_player_coins(user_id)
    new_balance = add_player_coins(user_id, coins_to_add, "Daily Claim")
    LAST_DAILY_CLAIM[user_id] = now # Update last claim time

    # --- Send Confirmation ---
    bonus_text = f"\n  🔥 Win Streak Bonus: +{streak_bonus}!" if streak_bonus > 0 else ""
    success_caption = f"""
    🎉 <b>Daily Reward Claimed!</b> 🎉

    A supply drop has arrived! You received:
      🪙 Base Reward: +{DAILY_COIN_AMOUNT}{bonus_text}
      💰 <b>Total Claimed:</b> {coins_to_add} Coins!

    <b>New Balance:</b> {new_balance} 🪙

    {fancy_separator}
    Come back tomorrow for more! Remember to check the <code>/shop</code>! ✨
    """
    await safe_send_photo(context, user_id, get_random_image('daily'), caption=success_caption)


# --- 🛒 Shop Command ---
async def shop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays the title shop (Fancy UI)."""
    user = update.effective_user
    if is_globally_banned(user.id): return
    # No spam check needed usually

    # Ensure player exists
    stats = get_player_stats(user.id)
    if not stats: update_player_stats(user.id, user.username, {})

    # Use helper to display shop (allows refresh after purchase)
    await shop_command_fixed(update.message, context)


async def shop_command_fixed(message, context: ContextTypes.DEFAULT_TYPE):
    """Helper to display the shop message (Fancy UI)."""
    user_id = message.chat_id # Assumes DM or group context where chat_id is user_id
    coins = get_player_coins(user_id)
    stats = get_player_stats(user_id)
    if not stats:
        await safe_send(context, user_id, "❌ Cannot display shop. Play a game first to create your profile.")
        return

    current_title_key = stats[18] if len(stats) > 18 and stats[18] in PLAYER_TITLES else 'novice_captain'
    current_title_data = PLAYER_TITLES[current_title_key]
    fancy_separator = "✨ • ⋅ ⋅ ────────── ⋅ ⋅ • ✨"

    # --- Build Shop Text ---
    title_lines = [f"🪙 Balance : {coins} Coins", f"🎖 Current Title : {current_title_data['name']}", "", "<b>Available Titles</b>"]
    keyboard = [] # Buttons for buying

    for key, data in PLAYER_TITLES.items():
        cost = data['cost']
        line = f"{data['emoji']} <b>{data['name']}</b>"

        if key == current_title_key:
            line += " - ✅ Equipped"
        elif cost == 0:
            line += " - Free"
            keyboard.append([InlineKeyboardButton(f"✨ Equip {data['name']}", callback_data=f"shop_buy_{key}")])
        elif coins >= cost:
            line += f" - {cost} 🪙"
            keyboard.append([InlineKeyboardButton(f"🛒 Buy ({cost} 🪙)", callback_data=f"shop_buy_{key}")])
        else:
            line += f" - {cost} 🪙 (🔒 Insufficient Funds)"
        title_lines.append(line)

    text = build_card("TITLE SHOP", title_lines, emoji="🛍")
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

    # --- Send Shop Message ---
    await safe_send_photo(
        context, user_id, get_random_image('shop'),
        caption=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML
    )


async def handle_shop_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles button presses for buying titles."""
    query = update.callback_query
    user_id = query.from_user.id

    if is_globally_banned(user_id):
        await query.answer("🚫 Access Denied.", show_alert=True)
        return

    data = query.data # e.g., "shop_buy_space_pirate"
    parts = data.split('_')

    if len(parts) < 3 or parts[1] != 'buy':
        # Handles "shop_none" or invalid data
        await query.answer() # Acknowledge silently
        return

    title_key = parts[2]
    title_data = PLAYER_TITLES.get(title_key)

    if not title_data:
        await query.answer("❓ Invalid title selected.", show_alert=True)
        return

    stats = get_player_stats(user_id)
    if not stats:
        await query.answer("❌ Error fetching your data. Please try again.", show_alert=True)
        return
        
    current_title_key = stats[18] if len(stats) > 18 and stats[18] in PLAYER_TITLES else 'novice_captain'
    
    if current_title_key == title_key:
        await query.answer("✅ You already have this title equipped!", show_alert=False)
        return

    cost = title_data['cost']
    user_coins = get_player_coins(user_id)

    if user_coins < cost:
        await query.answer(f" 부족한 코인! Need {cost} 🪙, you have {user_coins} 🪙.", show_alert=True)
        return

    # --- Process Purchase ---
    new_balance = add_player_coins(user_id, -cost, f"Purchased Title: {title_key}")
    update_player_stats(user_id, query.from_user.username, {'title': title_key}) # Update title in DB

    await query.answer(f"✅ Acquired & Equipped: {title_data['name']}!", show_alert=True)

    # Send confirmation message
    await safe_send(
        context, user_id,
        f"🎉 <b>Title Acquired!</b> 🎉\nYou now bear the title: {title_data['name']}\n\n💰 Remaining Balance: {new_balance} 🪙",
        parse_mode=ParseMode.HTML
    )

    # Refresh the shop message in the DM
    try:
        await shop_command_fixed(query.message, context)
    except Exception as e:
        logger.warning(f"⚠️ Could not refresh shop message after purchase: {e}")

# ✨ ======================== GLOBAL PLAYER COMMANDS (Fancy UI) ======================== ✨

async def mystats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays the player's global statistics with a fancy UI."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    if is_globally_banned(user.id): return
    if check_spam(user.id):
        await safe_send(context, chat_id, "⏳ Please wait a moment before commands.")
        return

    stats_tuple = get_player_stats(user.id)
    if not stats_tuple: # Register if first time
        update_player_stats(user_id=user.id, username=user.username, stats_update={})
        stats_tuple = get_player_stats(user.id) # Try fetching again

    formatted_stats = format_user_stats(stats_tuple) # Use the fancy formatter

    await safe_send_photo(
        context=context, chat_id=chat_id, photo_url=get_random_image('mystats'),
        caption=formatted_stats, parse_mode=ParseMode.HTML
    )

async def achievements_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the player's unlocked achievements with a fancy UI."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    if is_globally_banned(user.id): return
    if check_spam(user.id):
        await safe_send(context, chat_id, "⏳ Please wait a moment before commands.")
        return

    unlocked_keys = get_player_achievements(user.id)
    total_achievements = len(ACHIEVEMENTS)

    lines = [f"Progress : {len(unlocked_keys)} / {total_achievements} Unlocked", ""]

    if not unlocked_keys:
        lines.append("No achievements earned yet. Your legend awaits!")
    else:
        lines.append("✅ UNLOCKED")
        lines.extend(branch_lines([f"{ach['emoji']} <b>{ach['name']}</b>: {ach['desc']}" for k in sorted(unlocked_keys) if (ach := ACHIEVEMENTS.get(k))]))

        locked = [(k, a) for k, a in ACHIEVEMENTS.items() if k not in unlocked_keys]
        if locked:
            lines.append("")
            lines.append("🔒 LOCKED")
            lines.extend(branch_lines([f"{a['emoji']} <b>{a['name']}</b>: {a['desc']}" for k, a in locked]))
        else:
            lines.append("")
            lines.append("🎉 You've unlocked all available achievements! 🎉")

    text = build_card("ACHIEVEMENTS", lines, emoji="🏅")

    await safe_send_photo(
        context=context, chat_id=chat_id, photo_url=get_random_image('achievements'),
        caption=text, parse_mode=ParseMode.HTML
    )

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays the global top 10 players with a fancy UI."""
    user = update.effective_user # Kept for potential future use (e.g., highlighting user's rank)
    chat_id = update.effective_chat.id

    if is_globally_banned(user.id): return
    if check_spam(user.id):
        await safe_send(context, chat_id, "⏳ Please wait a moment before commands.")
        return

    top_players = get_leaderboard(10) # Fetch top 10

    if not top_players:
        await safe_send_photo(
            context=context, chat_id=chat_id, photo_url=get_random_image('leaderboard'),
            caption=build_card("HALL OF FAME", ["The leaderboard is currently empty.", "Be the first legend!"], emoji="🏆"),
            parse_mode=ParseMode.HTML
        )
        return

    medals = ["🥇", "🥈", "🥉"] + ["✨"] * (len(top_players) - 3) # Medals for top 3, stars after
    lines = []
    for i, (username, wins, games, kills, damage, score, title_key) in enumerate(top_players):
        medal = medals[i] if i < len(medals) else "🔹" # Fallback marker
        if title_key not in PLAYER_TITLES: title_key = 'novice_captain'
        title_data = PLAYER_TITLES[title_key]
        safe_username = escape_markdown_value(username or f"Captain_{i+1}")
        win_rate = int((wins / games) * 100) if games > 0 else 0
        lines.append(f"{medal} <b>{safe_username}</b> {title_data['emoji']}")
        lines.append(f"   Score: {score} | Wins: {wins} ({win_rate}%) | Kills: {kills}")

    text = build_card(f"TOP {len(top_players)} CAPTAINS", lines, emoji="🏆")

    await safe_send_photo(
        context=context, chat_id=chat_id, photo_url=get_random_image('leaderboard'),
        caption=text, parse_mode=ParseMode.HTML
    )

async def compare_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Compares the user's stats with another player (Fancy UI)."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    if is_globally_banned(user.id): return
    if check_spam(user.id):
        await safe_send(context, chat_id, "⏳ Please wait a moment before commands.")
        return

    stats1_tuple = get_player_stats(user.id)
    if not stats1_tuple:
        await safe_send(context, chat_id, "❌ Cannot compare: Your stats aren't available yet. Play a game!")
        return

    # --- Determine Target Player ---
    stats2_tuple = None
    target_display_name = "Opponent" # Default
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        stats2_tuple = get_player_stats(target_user.id)
        target_display_name = escape_markdown_value(target_user.first_name or f"ID_{target_user.id}")
        if not stats2_tuple:
            await safe_send(context, chat_id, f"❌ Cannot compare: {target_display_name} hasn't played yet.")
            return
    elif context.args:
        target_username = context.args[0].replace('@', '')
        stats2_tuple = get_player_stats_by_username(target_username)
        if not stats2_tuple:
            await safe_send(context, chat_id, f"❌ Player '@{escape_markdown_value(target_username)}' not found in records.")
            return
        target_display_name = escape_markdown_value(stats2_tuple[1] or f"@{target_username}") # Use username from DB
    else:
        await safe_send(context, chat_id, "❓ <b>How to use:</b> Reply to a user's message with <code>/compare</code> or type <code>/compare @username</code>.")
        return

    # --- Unpack Stats Safely ---
    # Player 1 (You)
# --- Unpack Stats Safely using Indices ---
    # Indices based on SELECT order in get_player_stats: 0:id, 1:username, 2:games, 3:wins, 4:losses, 5:kills, 6:deaths, 7:dmg_dealt, 8:dmg_taken, 9:heals, 10:loots, 11:streak, 12:best_streak, 13:score, 14:betrayals, 15:alliances, 16:last_played, 17:coins, 18:title
    try:
        # Player 1 (You)
        u1_name = escape_markdown_value(stats1_tuple[1] or "You")
        g1, w1, l1, k1, d1, dmg1, h1, s1, c1 = stats1_tuple[2], stats1_tuple[3], stats1_tuple[4], stats1_tuple[5], stats1_tuple[6], stats1_tuple[7], stats1_tuple[9], stats1_tuple[13], stats1_tuple[17]
        # Player 2 (Target)
        u2_name = target_display_name
        g2, w2, l2, k2, d2, dmg2, h2, s2, c2 = stats2_tuple[2], stats2_tuple[3], stats2_tuple[4], stats2_tuple[5], stats2_tuple[6], stats2_tuple[7], stats2_tuple[9], stats2_tuple[13], stats2_tuple[17]
    except IndexError:
         logger.error(f"❌ IndexError during stats unpacking for /compare. User1: {user.id}, Target: {stats2_tuple[0] if stats2_tuple else 'Unknown'}")
         await safe_send(context, chat_id, "❌ Error retrieving all stats needed for comparison.")
         return

    # --- Comparison Logic ---
    def compare_icon(v1, v2):
        if v1 > v2: return "🔼" # Up arrow for higher
        if v1 < v2: return "🔽" # Down arrow for lower
        return "➖" # Equal sign for tie

    # --- Assemble Fancy Comparison Text ---
    fancy_separator = "⚔️ • ⋅ ⋅ ────────── ⋅ ⋅ • ⚔️"
    text = f"""
    📊 <b>Stats Showdown</b> 📊

    <b>{u1_name}</b> vs  <b>{u2_name}</b>
    (🔼 Higher | 🔽 Lower | ➖ Equal)

    {fancy_separator}

    <b>Battle Record:</b>
      Games: {g1} {compare_icon(g1, g2)} {g2}
      Wins: {w1} {compare_icon(w1, w2)} {w2}
      Score: {s1} {compare_icon(s1, s2)} {s2}
      Coins: {c1} {compare_icon(c1, c2)} {c2}

    {fancy_separator}

    <b>Combat Prowess:</b>
      Kills: {k1} {compare_icon(k1, k2)} {k2}
      Deaths: {d1} {compare_icon(d2, d1)} {d2}  _(Lower is better)_
      Damage Dealt: {dmg1} {compare_icon(dmg1, dmg2)} {dmg2}
      Heals Done: {h1} {compare_icon(h1, h2)} {h2}

    {fancy_separator}
    """

    await safe_send_photo(
        context=context, chat_id=chat_id, photo_url=get_random_image('compare'),
        caption=text, parse_mode=ParseMode.HTML
    )

async def tips_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Provides a random gameplay tip with a fancy UI."""
    user = update.effective_user # Check ban status
    chat_id = update.effective_chat.id

    if is_globally_banned(user.id): return
    if check_spam(user.id):
        await safe_send(context, chat_id, "⏳ Please wait a moment before commands.")
        return

    # Expanded and more engaging tips
    tips = [
        f"🧭 <b>Positioning is Key:</b> Use <code>/move</code> to control engagement distance. Keep targets within {ATTACK_RANGE} squares, but stay out of theirs if possible!",
        f"🛡️ <b>Strategic Defense:</b> Activate <code>/defend</code> when you anticipate heavy fire, especially if low on HP or facing multiple foes. It blocks {int(DEFEND_REDUCTION*100)}%!",
        f"💥 <b>Focus Fire (Teams):</b> Coordinate attacks with your allies (🔵 or 🔴) to quickly eliminate high-priority targets.",
        f"💰 <b>Daily Logins:</b> Don't forget your <code>/daily</code> coin reward! Use coins in the <code>/shop</code> for cool titles.",
        f"🎒 <b>Inventory Management:</b> Max {LOOT_ITEM_CAP} items! Use powerful Weapons 🔫 and Shields 🛡️ strategically to make room for new loot.",
        f"🌀 <b>Zone Awareness:</b> Keep an eye on the Safe Zone (<code>/map</code>)! Taking {SAFE_ZONE_DAMAGE} damage each turn outside the zone adds up quickly.",
        f"🤝 <b>Risky Alliances (Solo):</b> <code>/ally</code> can provide temporary safety, but a <code>/betray</code> at the right moment offers a massive {int((BETRAYAL_DAMAGE_BONUS-1)*100)}% damage boost!",
        f"🔧 <b>Timely Repairs:</b> Use <code>/heal</code> ({HEAL_AMOUNT[0]}-{HEAL_AMOUNT[1]} HP) before your HP gets critically low. Staying above 0 is the goal!",
        f"👀 <b>Observe Opponents:</b> Check the <code>/ranking</code> or Day Summary to see who is wounded (🔴/🟡 HP) - they might be easier targets!",
        f"⚡ <b>Know Your Items:</b> Some loot provides instant benefits (Energy 💚), while others power up your next action (Weapons 💥) or defense (Shields 🏰)."
    ]
    selected_tip = random.choice(tips)
    fancy_separator = "💡 • ⋅ ⋅ ────────── ⋅ ⋅ • 💡"

    tip_text = f"""
    💡 <b>Captain's Log: Tactical Tip</b> 💡

    {selected_tip}

    {fancy_separator}
    Apply this wisdom in your next battle! ✨
    """

    await safe_send_photo(
        context=context, chat_id=chat_id, photo_url=get_random_image('tips'),
        caption=tip_text, parse_mode=ParseMode.HTML
    )

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows recent game history for the current chat (Fancy UI)."""
    user = update.effective_user # Check ban status
    chat_id = update.effective_chat.id

    if is_globally_banned(user.id): return
    if check_spam(user.id):
        await safe_send(context, chat_id, "⏳ Please wait a moment before commands.")
        return

    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        # Fetch last 5 games from *this* chat
        cursor.execute('''
            SELECT winner_name, total_players, total_rounds, map_name, end_time
            FROM game_history WHERE chat_id = ?
            ORDER BY game_id DESC LIMIT 5
        ''', (chat_id,))
        results = cursor.fetchall()
    except sqlite3.Error as e:
        logger.error(f"❌ DB Error fetching history for chat {chat_id}: {e}")
        await safe_send(context, chat_id, "❌ Error retrieving game history.")
        return
    finally:
        if conn: conn.close()

    fancy_separator = "📜 • ⋅ ⋅ ────────── ⋅ ⋅ • 📜"

    if not results:
        history_text = f"📜 <b>Battle Archives</b> 📜\n{fancy_separator}\nNo recorded battles found for this sector (group chat)."
        await safe_send_photo(
            context=context, chat_id=chat_id, photo_url=get_random_image('history'),
            caption=history_text, parse_mode=ParseMode.HTML
        )
        return

    text = f"📜 <b>Battle Archives - Recent Engagements</b> 📜\n{fancy_separator}\n"

    for winner, players, rounds, map_key, end_time_str in results:
        try:
            # Parse timestamp for prettier date
            end_dt = datetime.fromisoformat(end_time_str)
            date_str = end_dt.strftime("%b %d, %Y %H:%M") # e.g., Oct 30, 2025 14:30
        except ValueError:
            date_str = "Unknown Date"

        map_name = MAPS.get(map_key, {}).get('name', map_key) # Get map name safely
        winner_display = escape_markdown_value(winner) if winner != "Draw" and winner != "Team Draw" else winner

        text += (
            f"\n📅 <b>{date_str}</b>\n"
            f"  🏆 Winner: <b>{winner_display}</b>\n"
            f"  🗺️ Arena: {escape_markdown_value(map_name)}\n"
            f"  👥 Participants: {players} | ⏳ Duration: {rounds} Days\n"
        )

    text += f"\n{fancy_separator}"

    await safe_send_photo(
        context=context, chat_id=chat_id, photo_url=get_random_image('history'),
        caption=text, parse_mode=ParseMode.HTML
    )


# 🛡️ ======================== ADMIN & SETTINGS COMMANDS (Fancy UI) ======================== 🛡️

# 🛡️ ======================== ADMIN & SETTINGS COMMANDS (Fancy UI) ======================== 🛡️

def get_group_settings(chat_id: int) -> dict:
    """Loads group-specific game settings from the database (standalone helper, no Game instance needed)."""
    defaults = {'join_time': 120, 'operation_time': 120, 'min_players': 2, 'max_players': 20, 'allow_spectators': 1}
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT join_time, operation_time, min_players, max_players, allow_spectators FROM group_settings WHERE chat_id = ?', (chat_id,))
        row = cursor.fetchone()
        if row:
            return {'join_time': row[0], 'operation_time': row[1], 'min_players': row[2], 'max_players': row[3], 'allow_spectators': row[4]}
    except sqlite3.Error as e:
        logger.error(f"❌ DB Error loading settings for chat {chat_id}: {e}")
    finally:
        if conn: conn.close()
    return defaults


def update_group_setting(chat_id: int, field: str, value) -> bool:
    """Updates a single group_settings column. `field` must be one of the whitelisted columns below."""
    allowed_fields = {'join_time', 'operation_time', 'min_players', 'allow_spectators'}
    if field not in allowed_fields:
        logger.error(f"❌ Rejected update_group_setting for disallowed field '{field}'.")
        return False
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('INSERT OR IGNORE INTO group_settings (chat_id) VALUES (?)', (chat_id,))
        cursor.execute(f'UPDATE group_settings SET {field} = ? WHERE chat_id = ?', (value, chat_id))
        conn.commit()
        return True
    except sqlite3.Error as e:
        logger.error(f"❌ DB Error updating '{field}' for chat {chat_id}: {e}")
        return False
    finally:
        if conn: conn.close()


def build_settings_text(settings: dict) -> str:
    fancy_separator = "⚙️ • ⋅ ⋅ ────────── ⋅ ⋅ • ⚙️"
    return f"""
    ⚙️ <b>Group Game Settings</b> ⚙️

    Current configuration for battles in this chat:

    {fancy_separator}

    ⏱️ <b>Timers:</b>
      Join Phase: {settings['join_time']} seconds
      Action Phase: {settings['operation_time']} seconds

    👥 <b>Player Limits:</b>
      Minimum to Start: {settings['min_players']}
      Maximum Capacity: {settings['max_players']}

    🔭 <b>Spectators:</b> {'✅ Allowed' if settings['allow_spectators'] else '❌ Disabled'}

    {fancy_separator}

    Tap a button below to change a setting.
    """


def build_settings_main_keyboard() -> InlineKeyboardMarkup:
    return pack_buttons([
        InlineKeyboardButton("⏱️ Join Time", callback_data="settings_menu_jointime"),
        InlineKeyboardButton("⏱️ Action Time", callback_data="settings_menu_optime"),
        InlineKeyboardButton("👥 Min Players", callback_data="settings_menu_minplayers"),
        InlineKeyboardButton("🔭 Spectators", callback_data="settings_menu_spectate"),
    ], per_row=2)


async def _render_settings_main(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: Union[int, None] = None):
    settings = get_group_settings(chat_id)
    text = build_settings_text(settings)
    keyboard = build_settings_main_keyboard()
    if message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text,
                reply_markup=keyboard, parse_mode=ParseMode.HTML
            )
            return
        except (BadRequest, TelegramError):
            pass
    await safe_send(context, chat_id, text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays current game settings for the group with tappable buttons (Group Admin)."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if update.effective_chat.type == 'private':
        await safe_send(context, chat_id, "⚙️ This command is for group chats only.")
        return
    if not await is_admin_or_owner(context, chat_id, user_id):
        await safe_send(context, chat_id, "🚫 Access Denied: You need Group Admin rights.")
        return

    await _render_settings_main(context, chat_id)


async def handle_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles every button tap inside the /settings panel (menus + value selection)."""
    query = update.callback_query
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    data = query.data

    if not await is_admin_or_owner(context, chat_id, user_id):
        await query.answer("🚫 Group Admin rights required.", show_alert=True)
        return

    # --- Sub-menus (pick a category) ---
    if data == "settings_menu_jointime":
        keyboard = pack_buttons([
            InlineKeyboardButton(f"{s}s", callback_data=f"settings_set_jointime_{s}")
            for s in (30, 60, 90, 120, 180, 300)
        ] + [InlineKeyboardButton("◀️ Back", callback_data="settings_back")], per_row=3)
        await query.edit_message_text("⏱️ <b>Choose Join Phase duration:</b>", reply_markup=keyboard, parse_mode=ParseMode.HTML)
        await query.answer()
        return

    if data == "settings_menu_optime":
        keyboard = pack_buttons([
            InlineKeyboardButton(f"{s}s", callback_data=f"settings_set_optime_{s}")
            for s in (30, 60, 90, 120, 180, 300)
        ] + [InlineKeyboardButton("◀️ Back", callback_data="settings_back")], per_row=3)
        await query.edit_message_text("⏱️ <b>Choose Action Phase duration:</b>", reply_markup=keyboard, parse_mode=ParseMode.HTML)
        await query.answer()
        return

    if data == "settings_menu_minplayers":
        keyboard = pack_buttons([
            InlineKeyboardButton(str(n), callback_data=f"settings_set_minplayers_{n}")
            for n in (2, 3, 4, 5, 6, 8, 10)
        ] + [InlineKeyboardButton("◀️ Back", callback_data="settings_back")], per_row=4)
        await query.edit_message_text("👥 <b>Choose minimum players to start:</b>", reply_markup=keyboard, parse_mode=ParseMode.HTML)
        await query.answer()
        return

    if data == "settings_menu_spectate":
        keyboard = pack_buttons([
            InlineKeyboardButton("✅ Allow", callback_data="settings_set_spectate_1"),
            InlineKeyboardButton("❌ Disable", callback_data="settings_set_spectate_0"),
            InlineKeyboardButton("◀️ Back", callback_data="settings_back"),
        ], per_row=2)
        await query.edit_message_text("🔭 <b>Allow spectators in this group?</b>", reply_markup=keyboard, parse_mode=ParseMode.HTML)
        await query.answer()
        return

    if data == "settings_back":
        await _render_settings_main(context, chat_id, message_id=query.message.message_id)
        await query.answer()
        return

    # --- Applying a chosen value ---
    if data.startswith("settings_set_jointime_"):
        value = int(data.rsplit("_", 1)[1])
        update_group_setting(chat_id, "join_time", value)
        await query.answer(f"✅ Join time set to {value}s")
    elif data.startswith("settings_set_optime_"):
        value = int(data.rsplit("_", 1)[1])
        update_group_setting(chat_id, "operation_time", value)
        await query.answer(f"✅ Action time set to {value}s")
    elif data.startswith("settings_set_minplayers_"):
        value = int(data.rsplit("_", 1)[1])
        update_group_setting(chat_id, "min_players", value)
        await query.answer(f"✅ Min players set to {value}")
    elif data.startswith("settings_set_spectate_"):
        value = int(data.rsplit("_", 1)[1])
        update_group_setting(chat_id, "allow_spectators", value)
        await query.answer("✅ Spectator setting updated")
    else:
        await query.answer()
        return

    # Go back to the main settings screen showing the updated value
    await _render_settings_main(context, chat_id, message_id=query.message.message_id)

# ✨ ======================== DATABASE EXPORT/RESTORE COMMANDS (Owner Only) ======================== ✨

# ✨ ======================== DATABASE EXPORT/RESTORE COMMANDS (Owner Only) ======================== ✨

async def export_database(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exports the entire players table as a JSON file (Owner Only)."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id # For confirmation

    # --- Permission Check ---
    if not await is_owner(user_id):
        await safe_send(context, chat_id, "🚫 Access Denied: Only the Bot Owner can export the database.")
        logger.warning(f"Unauthorized /export attempt by {user_id}")
        return

    await safe_send(context, chat_id, "💾 Generating database export... Please wait.")
    logger.info(f"💾 Database export initiated by owner {user_id}.")

    conn = None
    players_data = []
    column_names = []
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row # Fetch rows as dictionary-like objects
        cursor = conn.cursor()

        # Get column names dynamically
        cursor.execute("PRAGMA table_info(players)")
        # Correctly access column names from PRAGMA result
        column_names = [info['name'] for info in cursor.fetchall()] # Access by name 'name'

        # Fetch all player data
        cursor.execute("SELECT * FROM players ORDER BY user_id")
        players_data = cursor.fetchall() # List of Row objects

    except sqlite3.Error as e:
        logger.error(f"❌ DB Error during export data fetch: {e}")
        await safe_send(context, chat_id, "❌ Error retrieving data from the database.")
        if conn: conn.close()
        return
    finally:
        # Ensure connection is closed even if PRAGMA fails
        if conn: conn.close() # Moved close to finally

    if not players_data:
        await safe_send(context, chat_id, "ℹ️ The players table is currently empty. Nothing to export.")
        return

    # --- Convert to JSON serializable format ---
    export_list = []
    for row in players_data:
        player_dict = {}
        for col_name in column_names:
            player_dict[col_name] = row[col_name]
        # Perform validation/correction before adding to export list
        player_dict['coins'] = max(0, int(float(player_dict.get('coins', 0) or 0))) # Ensure valid coins
        if player_dict.get('title') not in PLAYER_TITLES: # Ensure valid title
             player_dict['title'] = 'novice_captain'
        export_list.append(player_dict)

    export_data_final = {
        "export_time_utc": datetime.now(datetime.UTC).isoformat(), # Use timezone-aware UTC time
        "total_players": len(export_list),
        "players": export_list # The list of player dictionaries
    }

    # --- Save to File ---
    filename = f"players_db_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    temp_file_path = filename
    try:
        with open(temp_file_path, 'w', encoding='utf-8') as f:
            json.dump(export_data_final, f, indent=2, ensure_ascii=False)
    except IOError as e:
        logger.error(f"❌ Failed to write export file {filename}: {e}", exc_info=True)
        await safe_send(context, chat_id, "❌ Error: Could not write export file to disk.")
        return
    except Exception as e:
        logger.error(f"❌ Unexpected error writing export JSON {filename}: {e}", exc_info=True)
        await safe_send(context, chat_id, "❌ Error: Failed during JSON export.")
        if os.path.exists(temp_file_path): os.remove(temp_file_path)
        return

    # --- Send File to Owner ---
    try:
        with open(temp_file_path, 'rb') as f_read:
            caption = (
                f"📄 <b>Player Database Export</b> 📄\n\n"
                f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Players Exported: {len(export_list)}\n"
                f"File: <code>{filename}</code>"
            )
            await context.bot.send_document(
                chat_id=OWNER_ID, # Send to owner's DM
                document=f_read,
                filename=filename,
                caption=caption,
                parse_mode=ParseMode.HTML
            )
        logger.info(f"✅ Export file {filename} sent successfully to owner {OWNER_ID}.")
        await safe_send(context, chat_id, f"✅ Database export successful! File sent to your DM.")

    except Exception as e:
        logger.error(f"❌ Failed to send export file {filename} to owner {OWNER_ID}: {e}", exc_info=True)
        await safe_send(context, chat_id, f"❌ Export file created (<code>{filename}</code>), but failed to send it via DM. Check server logs/files.")
    finally:
        # Clean up the file from the server
        if os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except OSError as e:
                logger.error(f"⚠️ Failed to delete temporary export file {temp_file_path}: {e}")


# --- CORRECTED restore_database function ---
async def restore_database(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Restores the players table from a JSON backup file via reply (Owner Only)."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # --- Permission Check ---
    if not await is_owner(user_id):
        await safe_send(context, chat_id, "🚫 Access Denied: Only the Bot Owner can restore the database.")
        logger.warning(f"Unauthorized /restore attempt by {user_id}")
        return

    # --- Check for Replied File ---
    replied_message = update.message.reply_to_message
    if not replied_message or not replied_message.document:
        await safe_send(context, chat_id, "⚠️ <b>Usage:</b> Reply to the JSON backup file with <code>/restore</code>.")
        return

    document = replied_message.document
    if not document.file_name.lower().endswith('.json'):
        await safe_send(context, chat_id, "⚠️ Please reply to a valid <code>.json</code> backup file.")
        return

    # Optional: File size check
    if document.file_size > 20 * 1024 * 1024: # Limit to 20MB?
        await safe_send(context, chat_id, "❌ Error: Backup file is too large (Max 20MB).")
        return

    await safe_send(context, chat_id, "🔄 Starting database restore... This may overwrite existing player data.")
    logger.info(f"🔄 Database restore initiated by owner {user_id} from file {document.file_name}.")

    # --- Download and Process File ---
    temp_file_path = f"temp_restore_{document.file_id}.json"
    conn = None # Initialize conn outside try
    restored_count = 0
    error_count = 0
    total_in_file = 0
    start_time = datetime.now()

    try:
        # --- Download and Read JSON ---
        file = await context.bot.get_file(document.file_id)
        await file.download_to_drive(temp_file_path)

        with open(temp_file_path, 'r', encoding='utf-8') as f:
            backup_data = json.load(f)

        if 'players' not in backup_data or not isinstance(backup_data['players'], list):
            raise ValueError("Invalid JSON format: Missing or invalid 'players' list.")

        players_to_restore = backup_data['players']
        total_in_file = len(players_to_restore)
        logger.info(f"Read {total_in_file} player entries from backup file.")

        # --- Connect and Restore ---
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        # Get expected columns AND their types (important for default values)
        cursor.execute("PRAGMA table_info(players)")
        # Store as dict: {name: type_string}
        table_info = {info[1]: info[2].upper() for info in cursor.fetchall()}
        db_columns = list(table_info.keys()) # Get ordered list of column names

        if not db_columns:
             raise sqlite3.Error("Could not retrieve column info from players table.")

        placeholders = ', '.join(['?'] * len(db_columns))
        sql = f"INSERT OR REPLACE INTO players ({', '.join(db_columns)}) VALUES ({placeholders})"

        for player_data in players_to_restore:
            if not isinstance(player_data, dict) or 'user_id' not in player_data:
                logger.warning(f"⚠️ Skipping invalid player entry in restore file: {player_data}")
                error_count += 1
                continue

            # Prepare values tuple in the correct DB column order
            values = []
            for col_name in db_columns:
                value = player_data.get(col_name) # Get value from JSON dict

                # --- Apply Defaults & Validation ---
                if value is None: # Handle missing values
                    col_type = table_info.get(col_name, "TEXT")
                    if "INTEGER" in col_type or "INT" in col_type: value = 0
                    elif "REAL" in col_type or "FLOAT" in col_type: value = 0.0
                    # For TEXT, None is acceptable (becomes NULL)
                elif col_name == 'coins': # Validate coins
                    try: value = max(0, int(float(value)))
                    except: value = 0
                elif col_name == 'title' and value not in PLAYER_TITLES: # Validate title
                    value = 'novice_captain'
                elif col_name == 'username' and not value: # Ensure username
                    value = f"User_{player_data['user_id']}"
                # Add more specific type checks if needed

                values.append(value)

            try:
                # Ensure the number of values matches placeholders
                if len(values) == len(db_columns):
                    cursor.execute(sql, tuple(values))
                    restored_count += 1
                else:
                     logger.error(f"❌ Mismatch in value count ({len(values)}) vs column count ({len(db_columns)}) for user {player_data.get('user_id')}. Skipping.")
                     error_count += 1
            except Exception as insert_e:
                logger.error(f"❌ Error restoring player {player_data.get('user_id')}: {insert_e}")
                error_count += 1
                # Optionally: Rollback transaction on error? Or just skip entry? Skipping for now.

        conn.commit() # Commit all successful inserts/replaces

        # --- Report Results ---
        duration = datetime.now() - start_time
        await safe_send(context, chat_id, f"✅ <b>Database Restore Complete!</b> ✅\n\nRestored: {restored_count} / {total_in_file} players\nErrors/Skipped: {error_count}\nDuration: {str(duration).split('.')[0]}")
        logger.info(f"✅ Restore finished. Restored: {restored_count}, Errors: {error_count}. Duration: {duration}")

    # --- Exception Handling ---
    except FileNotFoundError:
        logger.error(f"❌ Restore failed: Could not download file.")
        await safe_send(context, chat_id, "❌ Error: Failed to download the backup file.")
    except json.JSONDecodeError:
        logger.error(f"❌ Restore failed: Invalid JSON in backup file.")
        await safe_send(context, chat_id, "❌ Error: The provided file is not valid JSON.")
    except ValueError as e: # Catch our custom validation error
         logger.error(f"❌ Restore failed: {e}")
         await safe_send(context, chat_id, f"❌ Error: {e}")
    except sqlite3.Error as e:
        logger.error(f"❌ DB Error during restore: {e}", exc_info=True) # Log full traceback for DB errors
        await safe_send(context, chat_id, "❌ Database error occurred during restore. Check logs.")
    except Exception as e:
        logger.error(f"❌ Unexpected error during restore: {e}", exc_info=True)
        await safe_send(context, chat_id, "❌ An unexpected error occurred during restore. Check logs.")
    # --- Cleanup ---
    finally:
        if conn: conn.close() # Ensure connection is closed
        # Clean up downloaded file
        if os.path.exists(temp_file_path):
            try: os.remove(temp_file_path)
            except OSError as e: logger.error(f"⚠️ Failed to delete temp restore file {temp_file_path}: {e}")

# ✨ ======================== IN-GAME COMMANDS (Fancy UI) ======================== ✨

async def join_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /join command during the joining phase (Solo mode)."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    if update.effective_chat.type == 'private':
        await safe_send(context, chat_id, "🔗 This command only works inside group chats!")
        return
    if is_globally_banned(user.id): return # Silently ignore banned

    if chat_id not in games:
        await safe_send(context, chat_id, "⏱️ No game is currently in the joining phase here.")
        return

    game = games[chat_id]

    if not game.is_joining:
        await safe_send(context, chat_id, "⏳ The joining phase for this battle has ended. Try <code>/spectate</code>?")
        return

    if game.mode == 'team':
        await safe_send(context, chat_id, "🤝 This is a Team Battle! Please use the 'Join Alpha' or 'Join Beta' buttons on the pinned message.")
        return

    if not await require_registration(update, context, user.id, user.first_name or "Captain", chat_id):
        return

    # --- Add Player (Solo) ---
    success, msg = game.add_player(user.id, user.username, user.first_name)
    if success:
        stats = get_player_stats(user.id)
        title_key = stats[18] if stats and len(stats) > 18 and stats[18] in PLAYER_TITLES else 'novice_captain'
        title_emoji = PLAYER_TITLES[title_key]['emoji']
        await safe_send(context, chat_id, f"✅ Welcome aboard! {title_emoji} {mention(user.id, user.first_name)} has joined the fleet!", parse_mode=ParseMode.HTML)
        # Update the joining message
        await display_joining_phase(update.message, context, game, edit=True) # Pass update.message for context
    else:
        await safe_send(context, chat_id, f"⚠️ {msg}") # Show error like 'already joined' or 'game full'


async def leave_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /leave command during the joining phase."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    if update.effective_chat.type == 'private':
        await safe_send(context, chat_id, "🔗 This command only works inside group chats!")
        return
    if is_globally_banned(user.id): return

    if chat_id not in games:
        await safe_send(context, chat_id, "⏱️ No game is currently in the joining phase to leave.")
        return

    game = games[chat_id]

    if not game.is_joining:
        await safe_send(context, chat_id, "⏳ Cannot leave now, the battle has likely begun!")
        return

    if user.id not in game.players:
        await safe_send(context, chat_id, "❓ You weren't registered for this battle.")
        return

    # --- Remove Player ---
    player_data = game.players.pop(user.id) # Remove from player dict
    px, py = player_data['position']
    team = player_data.get('team')

    try: # Safely remove from grid and team
        if user.id in game.map_grid[px][py]: game.map_grid[px][py].remove(user.id)
        if team and team in game.teams: game.teams[team].discard(user.id)
    except Exception as e:
        logger.warning(f"⚠️ Minor error removing player {user.id} from grid/team during leave: {e}")

    await safe_send(context, chat_id, f"💨 {mention(user.id, user.first_name)} has withdrawn from the upcoming battle.", parse_mode=ParseMode.HTML)

    # Update the joining message
    await display_joining_phase(update.message, context, game, edit=True) if game.mode == 'solo' else await display_team_joining_phase(update.message, context, game, edit=True)


async def spectate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows a user to spectate an ongoing game."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    if update.effective_chat.type == 'private':
        await safe_send(context, chat_id, "🔗 This command only works inside group chats!")
        return
    if is_globally_banned(user.id): return

    if chat_id not in games:
        await safe_send(context, chat_id, "🔭 No battle is currently taking place here to spectate.")
        return

    game = games[chat_id]

    if not game.settings.get('allow_spectators', 1): # Default to allowed if setting missing
        await safe_send(context, chat_id, "🔭 Spectating is currently disabled for games in this group.")
        return

    if user.id in game.players:
        await safe_send(context, chat_id, "😅 Captains in the battle cannot spectate!")
        return

    if user.id in game.spectators:
        await safe_send(context, chat_id, "✅ You are already spectating this match.")
        return

    game.spectators.add(user.id)
    await safe_send(context, chat_id, f"👀 {escape_markdown_value(user.first_name)} takes a seat in the observation deck. Enjoy the show!")


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias for the /leave command."""
    await leave_command(update, context)


async def map_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays the current game map. DM only — usable from any active game the player has joined."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if update.effective_chat.type != 'private':
        await safe_send(context, chat_id, "🗺️ This command only works in my DM. Please message me privately and use /map there.")
        return

    game_chat_id, game = find_active_game_for_player(user_id)
    if not game:
        await safe_send(context, chat_id, "🗺️ You're not currently part of an active battle.")
        return

    header_card = game.get_map_header_card()
    keyboard = game.get_map_keyboard(viewer_id=user_id)
    await safe_send(context, chat_id, header_card, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def position_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the user their current coordinates (in-game)."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    if update.effective_chat.type == 'private':
        await safe_send(context, chat_id, "📍 Position check only works inside an active game group.")
        return
    if is_globally_banned(user.id): return

    if chat_id not in games or not games[chat_id].is_active:
        await safe_send(context, chat_id, "📍 No active game to check position in.")
        return

    game = games[chat_id]
    player_data = game.players.get(user.id)

    if not player_data:
        await safe_send(context, chat_id, "❓ You don't seem to be participating in this battle.")
        return
    if not player_data.get('alive'):
        await safe_send(context, chat_id, "💀 Your ship has been destroyed. Coordinates unavailable.")
        return

    px, py = player_data['position']
    hp = player_data.get('hp', 0)
    max_hp = player_data.get('max_hp', HP_START)
    inv_count = len(player_data.get('inventory', []))

    nearby = []
    for other_id, other in game.players.items():
        if other_id == user.id or not other.get('alive'):
            continue
        ox, oy = other['position']
        d_row, d_col = ox - px, oy - py
        distance = abs(d_row) + abs(d_col)
        if distance <= 3:
            nearby.append({'user_id': other_id, 'distance': distance, 'd_row': d_row, 'd_col': d_col})
    nearby.sort(key=lambda p: p['distance'])

    lines = [f"📍 Sector : {col_letter(py)}{px + 1}", status_bar(hp, 0, "-", f"{inv_count}/{LOOT_ITEM_CAP}", f"{col_letter(py)}{px + 1}")]
    if nearby:
        lines.append("")
        lines.append("🧭 <b>Nearby Contacts</b>")
        lines.extend(build_locator_lines(nearby[:5]))

    await safe_send(context, chat_id, build_card("POSITION REPORT", [f"⚓ {mention(user.id)}"] + lines, emoji="📍"), parse_mode=ParseMode.HTML)


async def handle_map_cell_tap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Answers a tap on the interactive map grid with a quick info popup (view-only, no gameplay effect).
    Works whether the map was shown in the group or in a player's DM."""
    query = update.callback_query
    if not query.data:
        await query.answer()
        return
    try:
        _, chat_id_str, r_str, c_str = query.data.split(":")
        chat_id, r, c = int(chat_id_str), int(r_str), int(c_str)
    except (ValueError, IndexError):
        await query.answer()
        return

    game = games.get(chat_id)
    if not game:
        await query.answer("⚠️ That battle has ended.", show_alert=False)
        return

    cell_ids = game.map_grid[r][c] if 0 <= r < game.map_size and 0 <= c < game.map_size else []
    alive_here = [uid for uid in cell_ids if game.players.get(uid, {}).get('alive')]
    label = f"{col_letter(c)}{r + 1}"
    if alive_here:
        await query.answer(f"{label}: {len(alive_here)} ship(s) here", show_alert=False)
    elif cell_ids:
        await query.answer(f"{label}: wreckage", show_alert=False)
    else:
        zone = "Safe Zone" if game.is_in_safe_zone(r, c) else "Danger Zone"
        await query.answer(f"{label}: {zone}", show_alert=False)


async def myhp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the user their current HP. DM only."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    if update.effective_chat.type != 'private':
        await safe_send(context, chat_id, "❤️ This command only works in my DM. Please message me privately and use /myhp there.")
        return
    if is_globally_banned(user.id): return

    game_chat_id, game = find_active_game_for_player(user.id)
    if not game:
        await safe_send(context, chat_id, "❤️ You're not currently part of an active battle.")
        return

    player_data = game.players.get(user.id)
    if not player_data.get('alive'):
        await safe_send(context, chat_id, "💀 Your ship has been destroyed (0 HP).")
        return

    hp = player_data.get('hp', 0)
    max_hp = player_data.get('max_hp', HP_START)
    hp_indicator = get_hp_indicator(hp, max_hp)
    hp_bar = get_progress_bar(hp, max_hp)

    await safe_send(context, chat_id, f"{hp_indicator} <b>Your Hull Integrity:</b> {int(hp)} / {int(max_hp)} HP\n{hp_bar}", parse_mode=ParseMode.HTML)


async def inventory_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the user's current inventory (in-game)."""
    user = update.effective_user
    chat_id = update.effective_chat.id

    if update.effective_chat.type == 'private':
        await safe_send(context, chat_id, "🎒 Inventory check only works inside an active game group.")
        return
    if is_globally_banned(user.id): return

    if chat_id not in games or not games[chat_id].is_active:
        await safe_send(context, chat_id, "🎒 No active game to check inventory in.")
        return

    game = games[chat_id]
    player_data = game.players.get(user.id)

    if not player_data:
        await safe_send(context, chat_id, "❓ You don't seem to be participating in this battle.")
        return
    if not player_data.get('alive'):
        await safe_send(context, chat_id, "💀 Your ship was destroyed, inventory lost.")
        return

    inventory = player_data.get('inventory', [])
    inv_count = len(inventory)
    fancy_separator = "🎒 • ⋅ ⋅ ────────── ⋅ ⋅ • 🎒"

    text = f"🎒 <b>Cargo Hold Status ({inv_count}/{LOOT_ITEM_CAP})</b>\n{fancy_separator}\n"

    if not inventory:
        text += "  < Empty >\n  Use the 'Loot' action to find items!"
    else:
        item_counts = defaultdict(int)
        for item_key in inventory: item_counts[item_key] += 1

        for item_key, count in sorted(item_counts.items()): # Sort for consistency
            item_data = LOOT_ITEMS.get(item_key)
            if item_data:
                rarity_color = get_rarity_color(item_data['rarity'])
                item_name = item_key.replace('_', ' ').title()
                text += f"  {rarity_color} {item_data['emoji']} <b>{item_name}</b> (x{count})\n     Description: {item_data['desc']}\n"
            else:
                 text += f"  ❓ Unknown Item: {item_key} (x{count})\n" # Fallback

    text += f"\n{fancy_separator}"
    await safe_send(context, chat_id, text, parse_mode=ParseMode.HTML)


async def ranking_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the current ranking of alive players. DM only."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if update.effective_chat.type != 'private':
        await safe_send(context, chat_id, "🏆 This command only works in my DM. Please message me privately and use /ranking there.")
        return

    game_chat_id, game = find_active_game_for_player(user_id)
    if not game:
        await safe_send(context, chat_id, "🏆 You're not currently part of an active battle.")
        return

    alive_ids = game.get_alive_players()

    if not alive_ids:
        await safe_send(context, chat_id, "🏆 No captains remain on the battlefield.")
        return

    # Fetch data needed for ranking
    player_ranks = []
    for uid in alive_ids:
        player = game.players[uid]
        player_ranks.append({
            'id': uid,
            'name': escape_markdown_value(player.get('first_name', f'ID_{uid}')),
            'hp': player.get('hp', 0),
            'max_hp': player.get('max_hp', HP_START),
            'kills': player.get('stats', {}).get('kills', 0),
            'pos': player.get('position', ('?','?'))
        })

    # Sort: Higher HP first, then higher Kills for ties
    sorted_ranks = sorted(player_ranks, key=lambda p: (p['hp'], p['kills']), reverse=True)

    fancy_separator = "🏆 • ⋅ ⋅ ────────── ⋅ ⋅ • 🏆"
    text = f"🏆 <b>Current Battle Ranking</b> (Day {game.day})\n{fancy_separator}\n"
    medals = ["🥇", "🥈", "🥉"] + ["🔹"] * (len(sorted_ranks) - 3)

    for i, player in enumerate(sorted_ranks):
        medal = medals[i] if i < len(medals) else "🔹"
        hp_indicator = get_hp_indicator(player['hp'], player['max_hp'])
        text += (
            f"{medal} {hp_indicator} <b>{player['name']}</b>\n"
            f"   HP: {int(player['hp'])} | Kills: {player['kills']} | Pos: ({player['pos'][0]},{player['pos'][1]})\n"
        )

    text += f"\n{fancy_separator}"
    await safe_send(context, chat_id, text, parse_mode=ParseMode.HTML)


# ✨ ======================== BAN/UNBAN COMMANDS (Owner Only) ======================== ✨

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Globally bans a user from interacting with the bot (Owner Only)."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id # For sending confirmation

    # --- Permission Check ---
    if not await is_owner(user_id):
        await safe_send(context, chat_id, "🚫 Access Denied: Only the Bot Owner can issue global bans.")
        logger.warning(f"Unauthorized /ban attempt by {user_id}")
        return

    # --- Determine Target ---
    target_id = None
    target_name = None
    reason = "Banned by owner." # Default reason

    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        target_id = target_user.id
        target_name = target_user.first_name or f"ID_{target_id}"
        # Optional: Extract reason from the command message if args exist
        if context.args: reason = " ".join(context.args)

    elif context.args:
        username_arg = context.args[0].replace('@', '')
        # Optional: Extract reason if more args exist
        if len(context.args) > 1: reason = " ".join(context.args[1:])

        # Find user ID from username in DB
        stats = get_player_stats_by_username(username_arg)
        if stats:
            target_id = stats[0]
            target_name = stats[1] or f"@{username_arg}" # Use username from DB
        else:
            await safe_send(context, chat_id, f"❓ User '@{escape_markdown_value(username_arg)}' not found in player records.")
            return
    else:
        await safe_send(context, chat_id, "⚠️ <b>Usage:</b> Reply to a user with <code>/ban [reason]</code> or use <code>/ban @username [reason]</code>.")
        return

    # --- Sanity Checks ---
    if target_id == OWNER_ID:
        await safe_send(context, chat_id, "😅 Cannot ban the bot owner.")
        return
    if target_id == context.bot.id:
         await safe_send(context, chat_id, "😅 Cannot ban the bot itself.")
         return

    # --- Apply Ban ---
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        # Insert or ignore if already banned
        cursor.execute('''
            INSERT OR IGNORE INTO global_bans (user_id, reason, banned_by, banned_at)
            VALUES (?, ?, ?, ?)
        ''', (target_id, reason, user_id, datetime.now().isoformat()))
        conn.commit()

        if cursor.rowcount > 0: # Check if a row was actually inserted (i.e., not already banned)
            logger.info(f"🚫 User {target_id} globally banned by owner {user_id}. Reason: {reason}")
            await safe_send(context, chat_id, f"🚫 <b>User Globally Banned!</b> 🚫\nCaptain {escape_markdown_value(target_name)} (<code>{target_id}</code>) is now restricted from all bot interactions.\nReason: {escape_markdown_value(reason)}")
            # Optionally try to notify the banned user (might fail if blocked)
            await safe_send(context, target_id, f"🚫 You have been globally banned from using this bot.\nReason: {escape_markdown_value(reason)}")
        else:
            await safe_send(context, chat_id, f"ℹ️ User {escape_markdown_value(target_name)} (<code>{target_id}</code>) is already globally banned.")

    except sqlite3.Error as e:
        logger.error(f"❌ DB Error applying global ban to {target_id}: {e}")
        await safe_send(context, chat_id, "❌ Database error occurred while applying the ban.")
    finally:
        if conn: conn.close()


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Removes a global ban from a user (Owner Only)."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # --- Permission Check ---
    if not await is_owner(user_id):
        await safe_send(context, chat_id, "🚫 Access Denied: Only the Bot Owner can lift global bans.")
        logger.warning(f"Unauthorized /unban attempt by {user_id}")
        return

    # --- Determine Target ---
    target_id = None
    target_name = None

    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        target_id = target_user.id
        target_name = target_user.first_name or f"ID_{target_id}"

    elif context.args:
        username_arg = context.args[0].replace('@', '')
        # Find user ID from username in DB
        # Check global bans table first? Or players table? Players is better for name.
        stats = get_player_stats_by_username(username_arg)
        if stats:
            target_id = stats[0]
            target_name = stats[1] or f"@{username_arg}"
        else:
             # Maybe they were banned before playing? Check bans table directly.
             conn_check = None
             try:
                 conn_check = sqlite3.connect(DB_FILE)
                 cursor_check = conn_check.cursor()
                 cursor_check.execute("SELECT user_id FROM global_bans WHERE user_id = (SELECT user_id FROM players WHERE LOWER(username) = LOWER(?) LIMIT 1)", (username_arg,))
                 result = cursor_check.fetchone()
                 if result: target_id = result[0]
                 target_name = f"@{username_arg}" # Best guess for name
             except sqlite3.Error as e:
                 logger.error(f"DB error checking ban by username {username_arg}: {e}")
             finally:
                 if conn_check: conn_check.close()

             if not target_id:
                await safe_send(context, chat_id, f"❓ User '@{escape_markdown_value(username_arg)}' not found in player records or ban list.")
                return
    else:
        await safe_send(context, chat_id, "⚠️ <b>Usage:</b> Reply to a user with <code>/unban</code> or use <code>/unban @username</code>.")
        return

    # --- Remove Ban ---
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM global_bans WHERE user_id = ?', (target_id,))
        conn.commit()

        if cursor.rowcount > 0: # Check if a row was actually deleted
            logger.info(f"✅ User {target_id} globally unbanned by owner {user_id}.")
            await safe_send(context, chat_id, f"✅ <b>User Globally Unbanned!</b> ✅\nCaptain {escape_markdown_value(target_name)} (<code>{target_id}</code>) can now interact with the bot again.")
             # Optionally notify the unbanned user
            await safe_send(context, target_id, "✅ Your global restriction from using this bot has been lifted.")
        else:
            await safe_send(context, chat_id, f"ℹ️ User {escape_markdown_value(target_name)} (<code>{target_id}</code>) was not found in the global ban list.")

    except sqlite3.Error as e:
        logger.error(f"❌ DB Error removing global ban for {target_id}: {e}")
        await safe_send(context, chat_id, "❌ Database error occurred while removing the ban.")
    finally:
        if conn: conn.close()

# ✨ ======================== LIVE GAME BACKUP COMMAND (Owner Only) ======================== ✨

async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Backs up the current state of active in-memory games to a JSON file (Owner only)."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id # For confirmation message

    # --- Permission Check ---
    if not await is_owner(user_id):
        await safe_send(context, chat_id, "🚫 Access Denied: Only the Bot Owner can create live backups.")
        logger.warning(f"Unauthorized /backup attempt by {user_id}")
        return

    # --- Check if any games are active ---
    if not games:
        await safe_send(context, chat_id, "ℹ️ No active games found in memory to back up.")
        return

    # --- Create Backup Data ---
    backup_data = {
        "backup_time_utc": datetime.utcnow().isoformat(),
        "total_active_games": len(games),
        "games_data": {}
    }
    games_backed_up = 0
    start_time = datetime.now()

    logger.info(f"💾 Starting live game backup for {len(games)} games initiated by owner {user_id}.")
    await safe_send(context, chat_id, f"💾 Starting backup of {len(games)} active game states...")

    for game_chat_id, game_obj in games.items():
        try:
            # --- Serialize Game State (Carefully select serializable data) ---
            # Avoid trying to serialize complex objects like context or bot directly
            serialized_game = {
                "chat_id": game_obj.chat_id,
                "creator_id": game_obj.creator_id,
                "creator_name": game_obj.creator_name,
                "mode": game_obj.mode,
                "day": game_obj.day,
                "is_joining": game_obj.is_joining,
                "is_active": game_obj.is_active,
                "joining_message_id": game_obj.joining_message_id,
                "join_end_time_iso": game_obj.join_end_time.isoformat() if game_obj.join_end_time else None,
                "operation_end_time_iso": game_obj.operation_end_time.isoformat() if game_obj.operation_end_time else None,
                "start_time_iso": game_obj.start_time.isoformat(),
                "map_type": game_obj.map_type,
                "map_size": game_obj.map_size,
                # Safe Zone State
                "safe_zone_center": game_obj.safe_zone_center,
                "safe_zone_radius": game_obj.safe_zone_radius,
                "safe_zone_current_phase": game_obj.safe_zone_current_phase,
                # Player Data (Serialize relevant parts)
                "players": {},
                "spectators": list(game_obj.spectators),
                "teams": {team: list(player_set) for team, player_set in game_obj.teams.items()},
                "map_votes": game_obj.map_votes, # {user_id: map_key} - generally safe
                "alliances": game_obj.alliances, # {user_id: {'ally': ally_id, 'turns_left': int}} - generally safe
                # Note: map_grid is complex and might be hard to restore perfectly, maybe omit?
                # "map_grid": game_obj.map_grid, # Omit if restoration is too complex
            }

            # Serialize individual player data carefully
            for p_id, p_data in game_obj.players.items():
                serialized_game["players"][p_id] = {
                    'user_id': p_data.get('user_id'),
                    'username': p_data.get('username'),
                    'first_name': p_data.get('first_name'),
                    'hp': p_data.get('hp'),
                    'max_hp': p_data.get('max_hp'),
                    'inventory': p_data.get('inventory'),
                    'operation': p_data.get('operation'),
                    'target': p_data.get('target'),
                    'position': p_data.get('position'),
                    'team': p_data.get('team'),
                    'afk_turns': p_data.get('afk_turns'),
                    'stats': p_data.get('stats'), # In-game stats dict
                    'alive': p_data.get('alive'),
                    'last_action_time_iso': p_data.get('last_action_time').isoformat() if p_data.get('last_action_time') else None,
                }

            backup_data["games_data"][str(game_chat_id)] = serialized_game
            games_backed_up += 1

        except Exception as e:
            logger.error(f"❌ Error serializing game state for chat {game_chat_id}: {e}", exc_info=True)
            # Optionally add an error marker to the backup data for this game
            backup_data["games_data"][str(game_chat_id)] = {"error": f"Failed to serialize: {e}"}

    # --- Save Backup to File ---
    filename = f"live_games_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    temp_file_path = filename # Save directly in current dir, or specify a path
    try:
        with open(temp_file_path, 'w', encoding='utf-8') as f:
            json.dump(backup_data, f, indent=2, ensure_ascii=False) # Use indent for readability
    except IOError as e:
        logger.error(f"❌ Failed to write backup file {filename}: {e}", exc_info=True)
        await safe_send(context, chat_id, f"❌ Error: Could not write backup file to disk.")
        return
    except Exception as e:
        logger.error(f"❌ Unexpected error writing backup JSON {filename}: {e}", exc_info=True)
        await safe_send(context, chat_id, f"❌ Error: Failed during JSON serialization.")
        if os.path.exists(temp_file_path): os.remove(temp_file_path) # Clean up partial file
        return

    # --- Send Backup File to Owner ---
    try:
        with open(temp_file_path, 'rb') as f_read:
            caption = (
                f"💾 <b>Live Game State Backup</b> 💾\n\n"
                f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Games Backed Up: {games_backed_up} / {len(games)}\n"
                f"File: <code>{filename}</code>"
            )
            await context.bot.send_document(
                chat_id=OWNER_ID, # Send directly to owner's DM
                document=f_read,
                filename=filename, # Ensure filename is sent correctly
                caption=caption,
                parse_mode=ParseMode.HTML
            )
        logger.info(f"✅ Backup file {filename} sent successfully to owner {OWNER_ID}.")
        await safe_send(context, chat_id, f"✅ Backup successful! {games_backed_up} game states saved. File sent to your DM.")

    except Exception as e:
        logger.error(f"❌ Failed to send backup file {filename} to owner {OWNER_ID}: {e}", exc_info=True)
        await safe_send(context, chat_id, f"❌ Backup file created (<code>{filename}</code>), but failed to send it via DM. Check server logs/files.")
        # Don't delete the file if sending failed, so it can be retrieved manually

    finally:
        # Clean up the local file *only if* sending was potentially successful or not needed locally
        # Decide based on your preference whether to keep the file on disk regardless
         if os.path.exists(temp_file_path):
             try:
                 # Uncomment the line below if you want to delete the file after attempting to send
                 # os.remove(temp_file_path)
                 pass # Keep the file for now
             except OSError as e:
                 logger.error(f"⚠️ Failed to delete temporary backup file {temp_file_path}: {e}")

# ✨ ======================== BUTTON HANDLERS ======================== ✨

# (Ensure button_handler is defined before this if not already)
# async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE): ...

async def handle_mode_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the Solo vs Team mode selection button press after /creategame."""
    query = update.callback_query
    user_id = query.from_user.id

    if is_globally_banned(user_id):
        await query.answer("🚫 Access Denied.", show_alert=True)
        return

    await query.answer() # Acknowledge button press

    try:
        # Extract chat_id and mode from callback_data (e.g., "mode_solo_-100123...")
        parts = query.data.split('_')
        mode = parts[1] # 'solo' or 'team'
        chat_id = int(parts[2]) # The chat where the game was created
    except (IndexError, ValueError):
        logger.error(f"❌ Invalid mode selection callback data: {query.data}")
        await query.edit_message_caption(caption="❌ Error processing mode selection. Please try /creategame again.")
        return

    # --- Find the relevant game ---
    game = games.get(chat_id)
    if not game:
        logger.warning(f"⚠️ Game not found for chat {chat_id} during mode selection.")
        await query.edit_message_caption(caption="❌ This game session seems to have expired. Please start a new one with /creategame.")
        return

    # --- Check if the user pressing the button is the creator ---
    if user_id != game.creator_id:
        await query.answer("✋ Only the Captain who initiated the game can select the mode!", show_alert=True)
        return

    # --- Check if mode already selected (prevent double-clicks) ---
    if game.mode is not None or game.map_voting: # Mode is set before map voting starts
        await query.answer("⏳ Mode already selected, proceeding...", show_alert=False)
        return

    # --- Proceed to Map Voting ---
    logger.info(f"✨ Mode '{mode}' selected for game in chat {chat_id} by creator {user_id}.")
    if mode == 'solo':
        await start_map_voting(query, context, game, 'solo')
    elif mode == 'team':
        await start_map_voting(query, context, game, 'team')
    else:
        logger.error(f"❌ Unknown mode '{mode}' selected in callback for chat {chat_id}.")
        await query.edit_message_caption(caption="❌ Error: An unknown game mode was selected. Please try /creategame again.")
        # Clean up the invalid game state
        if chat_id in games: del games[chat_id]

# (Add other button handlers like handle_map_vote, handle_shop_selection etc. here or ensure they are present)

async def handle_target_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the button press when a player selects a target to attack."""
    query = update.callback_query
    user_id = query.from_user.id

    if is_globally_banned(user_id):
        await query.answer("🚫 Access Denied.", show_alert=True)
        return

    # Don't acknowledge with query.answer() immediately, do it in set_operation

    # --- Extract data ---
    try:
        # data format: target_targetID_attackerID_chatID
        _, target_id_str, attacker_id_str, chat_id_str = query.data.split('_')
        target_id = int(target_id_str)
        attacker_id = int(attacker_id_str)
        chat_id = int(chat_id_str)
    except (ValueError, IndexError):
        logger.error(f"❌ Invalid target selection callback data: {query.data}")
        await query.answer("⚠️ Error processing target. Please try again.", show_alert=True)
        # Try to send back to main operation menu?
        # await send_operation_dm(context, game, user_id) # Need game object here
        return

    # --- Validate ---
    if user_id != attacker_id:
        await query.answer("✋ This is not your targeting computer!", show_alert=True)
        return

    game = games.get(chat_id)
    if not game or not game.is_active:
        await query.answer("⚠️ Game not found or has ended.", show_alert=True)
        try: await query.edit_message_caption(caption="❌ This game session has concluded.")
        except: pass
        return

    player = game.players.get(user_id)
    target_player = game.players.get(target_id)

    if not player or not player.get('alive'):
        await query.answer("💀 You have been eliminated.", show_alert=True)
        return
    if not target_player or not target_player.get('alive'):
        await query.answer("❓ Target is no longer active.", show_alert=True)
        # Reshow target selection? Or main menu? Main menu is safer.
        await query.message.delete()
        await send_operation_dm(context, game, user_id)
        return

    # Re-check range in case target moved
    if target_id not in game.get_players_in_range(user_id):
        await query.answer("📡 Target moved out of range!", show_alert=True)
        await query.message.delete() # Delete target selection
        await send_operation_dm(context, game, user_id) # Back to main menu
        return

    # --- Set Operation ---
    # Pass query to set_operation so it can acknowledge the button press *after* setting
    await set_operation(query, context, game, user_id, 'attack', target_id, chat_id)

async def handle_move_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the button press when a player selects a direction to move."""
    query = update.callback_query
    user_id = query.from_user.id

    if is_globally_banned(user_id):
        await query.answer("🚫 Access Denied.", show_alert=True)
        return

    # Don't acknowledge immediately, let set_operation do it after confirming

    # --- Extract data ---
    try:
        # data format: move_direction_attackerID_chatID
        _, direction, mover_id_str, chat_id_str = query.data.split('_')
        mover_id = int(mover_id_str)
        chat_id = int(chat_id_str)
    except (ValueError, IndexError):
        logger.error(f"❌ Invalid move selection callback data: {query.data}")
        await query.answer("⚠️ Error processing move. Please try again.", show_alert=True)
        return

    # --- Validate ---
    if user_id != mover_id:
        await query.answer("✋ This is not your navigation control!", show_alert=True)
        return

    game = games.get(chat_id)
    if not game or not game.is_active:
        await query.answer("⚠️ Game not found or has ended.", show_alert=True)
        try: await query.edit_message_caption(caption="❌ This game session has concluded.")
        except: pass
        return

    player = game.players.get(user_id)
    if not player or not player.get('alive'):
        await query.answer("💀 You have been eliminated.", show_alert=True)
        return

    # --- Attempt Move ---
    try:
        move_successful = game.move_player(user_id, direction)

        if move_successful:
            # Confirm operation via set_operation (which will also ack the query)
            await set_operation(query, context, game, user_id, 'move', None, chat_id)
        else:
            # Move failed (e.g., hit boundary) - re-show move options and notify user
            await query.answer("🚫 Cannot move in that direction (boundary). Choose another!", show_alert=True)
            # Re-display move options (don't delete message here, just edit)
            await show_move_selection(query, context, game, user_id, chat_id)

    except Exception as e:
        logger.error(f"❌ Unexpected error during player move execution for {user_id}: {e}", exc_info=True)
        await query.answer(f"⚠️ Movement error occurred. Please try again.", show_alert=True)
        # Optionally delete message and resend main menu on unexpected error
        # await query.message.delete()
        # await send_operation_dm(context, game, user_id)


# 🪪 ======================== NAME TRACKING (for mentions) ======================== 🪪
async def track_user_name(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Runs before every other handler; caches each user's display name so
    mention() can show real names instead of the generic 'Captain'."""
    try:
        user = update.effective_user
        if user:
            display_name = user.first_name or user.username or f"Captain_{user.id}"
            register_name(user.id, display_name)
    except Exception:
        pass


# 🚨 ======================== GLOBAL ERROR HANDLER ======================== 🚨
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Logs errors and notifies user/dev if possible."""
    logger.error(f"❌ Exception while handling an update: {context.error}", exc_info=context.error)

    # Attempt to notify the user in chat where the error occurred
    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ Oops! Something went wrong on my end. The error has been logged."
            )
    except Exception as e:
        logger.error(f"❌ Failed to send error notification message to user: {e}")

    # Optionally: Send detailed error info to the OWNER_ID via DM
    # (Be careful with sensitive info if you implement this)
    # try:
    #     tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    #     tb_string = "".join(tb_list)
    #     error_message = f"An error occurred: {context.error}\n\nTraceback:\n``<code>\n{tb_string[:3500]}\n</code>``" # Limit traceback length
    #     await context.bot.send_message(chat_id=OWNER_ID, text=error_message, parse_mode=ParseMode.HTML)
    # except Exception as dev_e:
    #     logger.error(f"❌ Failed to send detailed error report to developer: {dev_e}")


# 🚀 ======================== MAIN EXECUTION ======================== 🚀
def main() -> None:
    """Initializes and runs the Ship Battle Bot."""
    logger.info(" M A I N F R A M E   I N I T I A L I Z I N G . . . ")
    logger.info("----------------------------------------------------")

    try:
        # --- Build Application ---
        application = Application.builder().token(BOT_TOKEN).build()

        # --- Register Handlers ---
        logger.info("Registering command handlers...")
        # Name tracker — runs first, for every update type (messages, callbacks, etc.)
        application.add_handler(TypeHandler(Update, track_user_name), group=-1)
        # Core
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("rules", rules_command))
        application.add_handler(CommandHandler("tutorial", tutorial_command))
        application.add_handler(CommandHandler("mysea", mysea_command))
        application.add_handler(CommandHandler("builders", builders_command))
        application.add_handler(CommandHandler("seas", seas_command))
        application.add_handler(CommandHandler("projects", projects_command))
        application.add_handler(CommandHandler("assign", assign_command))
        application.add_handler(CommandHandler("recruit", recruit_command))
        # Game Management
        application.add_handler(CommandHandler("creategame", creategame_command))
        application.add_handler(CommandHandler("join", join_command))
        application.add_handler(CommandHandler("leave", leave_command))
        application.add_handler(CommandHandler("cancel", cancel_command))
        application.add_handler(CommandHandler("spectate", spectate_command))
        # In-Game Info
        application.add_handler(CommandHandler("map", map_command))
        application.add_handler(CommandHandler("position", position_command))
        application.add_handler(CommandHandler("myhp", myhp_command))
        application.add_handler(CommandHandler("inventory", inventory_command))
        application.add_handler(CommandHandler("ranking", ranking_command))
        application.add_handler(CommandHandler("dailystats", stats_detailed_command))
        application.add_handler(CommandHandler("stats", stats_detailed_command, filters=filters.ChatType.GROUPS)) # Alias in groups
        # In-Game Actions
        application.add_handler(CommandHandler("ally", ally_command))
        application.add_handler(CommandHandler("betray", betray_command))
        application.add_handler(CommandHandler("selectmap", selectmap_command))
        # Global Player
        application.add_handler(CommandHandler("license", mystats_command)) # 🪪 Full profile: stats + Sea + rank + coins
        application.add_handler(CommandHandler("leaderboard", leaderboard_command))
        application.add_handler(CommandHandler("achievements", achievements_command))
        application.add_handler(CommandHandler("compare", compare_command))
        application.add_handler(CommandHandler("tips", tips_command))
        application.add_handler(CommandHandler("history", history_command))
        # Economy & Extras
        application.add_handler(CommandHandler("daily", daily_command))
        application.add_handler(CommandHandler("shop", shop_command))
        application.add_handler(CommandHandler("challenges", challenges_command))
        application.add_handler(CommandHandler("cosmetics", cosmetics_command))
        # Group Admin
        application.add_handler(CommandHandler("settings", settings_command))
        application.add_handler(CommandHandler("extend", extend_command))
        application.add_handler(CommandHandler("endgame", endgame_command))
        # Bot Admin / Owner
        application.add_handler(CommandHandler("stats", stats_command, filters=filters.ChatType.PRIVATE)) # Global stats in PM
        application.add_handler(CommandHandler("broadcast", broadcast_command))
        application.add_handler(CommandHandler("ban", ban_command))
        application.add_handler(CommandHandler("unban", unban_command))
        application.add_handler(CommandHandler("backup", backup_command)) # In-memory game backup
        application.add_handler(CommandHandler("export", export_database)) # DB backup
        application.add_handler(CommandHandler("restore", restore_database)) # DB restore

        # Callback Query Handler (for all buttons)
        # Specific handler for the "Back" button
        application.add_handler(CallbackQueryHandler(help_main_handler, pattern=r'^help_main$'))
        # Handler for all *other* help categories
        application.add_handler(CallbackQueryHandler(help_callback_handler, pattern=r'^help_(game|info|global|settings|howtoplay|lootinfo)$'))
        
        application.add_handler(CallbackQueryHandler(handle_sea_selection, pattern=r'^sea_pick_'))
        application.add_handler(CallbackQueryHandler(handle_contribute_start, pattern=r'^sea_contribute_start$'))
        application.add_handler(CallbackQueryHandler(handle_assign_captain, pattern=r'^assign_cap_'))
        application.add_handler(CallbackQueryHandler(handle_map_vote, pattern=r'^map_vote_'))
        application.add_handler(CallbackQueryHandler(handle_mode_selection, pattern=r'^mode_'))
        application.add_handler(CallbackQueryHandler(handle_join_leave, pattern=r'^(join|leave)_game_'))
        application.add_handler(CallbackQueryHandler(handle_team_join, pattern=r'^team_join_'))
        application.add_handler(CallbackQueryHandler(handle_operation_selection, pattern=r'^operation_'))
        application.add_handler(CallbackQueryHandler(handle_target_selection, pattern=r'^target_'))
        application.add_handler(CallbackQueryHandler(handle_move_selection, pattern=r'^move_'))
        application.add_handler(CallbackQueryHandler(handle_shop_selection, pattern=r'^shop_'))
        application.add_handler(CallbackQueryHandler(handle_settings_callback, pattern=r'^settings_'))
        application.add_handler(CallbackQueryHandler(handle_endgame_confirmation, pattern=r'^(confirm|cancel)_endgame_'))
        application.add_handler(CallbackQueryHandler(handle_forcestart_confirmation, pattern=r'^(confirm|cancel)_forcestart_'))
        application.add_handler(CallbackQueryHandler(handle_forcestart_ask, pattern=r'^forcestart_ask_'))
        application.add_handler(CallbackQueryHandler(handle_map_cell_tap, pattern=r'^shipmap:'))
        # Add pattern for spectate button if needed, e.g. pattern=r'^spectate_'
        # application.add_handler(CallbackQueryHandler(handle_spectate_button, pattern=r'^spectate_'))


        # Generic text handler (currently used for Sea contribution amount input)
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))

        # Global Error Handler (must be last handler added)
        application.add_error_handler(error_handler)

        logger.info("✅ All handlers registered.")
        logger.info("----------------------------------------------------")
        logger.info("🚀 SYSTEM ONLINE. Awaiting Captains...")
        logger.info("----------------------------------------------------")

        # --- Run Bot ---
        application.run_polling(allowed_updates=Update.ALL_TYPES)

    except Exception as e:
        logger.critical(f"❌❌❌ BOT FAILED TO START: {e} ❌❌❌", exc_info=True)

# --- Entry Point ---
if __name__ == '__main__':
    main()