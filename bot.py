import os
import time
from datetime import datetime, timedelta
from gevent import monkey
monkey.patch_all()

import random
import requests
import re
import gevent
from flask import Flask, render_template, jsonify, request
from pymongo import MongoClient
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from bson.objectid import ObjectId

app = Flask(__name__, template_folder='templates')
CORS(app)

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent", ping_timeout=20, ping_interval=5)

ADMIN_ID = os.getenv("ADMIN_ID") 
BOT_TOKEN = os.getenv("BOT_TOKEN") 
MONGO_URL = os.getenv("MONGO_URL")
WEB_APP_URL = os.getenv("WEB_APP_URL", "https://bingo1-pjyb.onrender.com") 

# የክፍያ አካውንቶች መረጃ (Deposit Accounts)
TELEBIRR_ACCOUNT = "0960736061"
TELEBIRR_NAME = "ZEHARA"
CBEBIRR_ACCOUNT = "0960736061"
CBEBIRR_NAME = "ZEHARA"
SUPPORT_USERNAME = "@BeshBingoSupport"

# MongoDB ግንኙነት ማዋቀር (Resilient MongoDB Client Initialization)
MONGO_URL = os.getenv("MONGO_URL") or os.getenv("MONGO_URI") or "mongodb+srv://yabebal98:Yabebal98@cluster0.p7ovn.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"

class SafeCollectionFallback:
    """MongoDB በማይገኝበት ጊዜ አፕሊኬሽኑ እንዳይዘጋ የሚከላከል ጊዜያዊ ማስተናገጃ"""
    def __init__(self, name):
        self.name = name
    def find_one(self, *args, **kwargs):
        return None
    def find(self, *args, **kwargs):
        return []
    def insert_one(self, doc, *args, **kwargs):
        return type("Result", (), {"inserted_id": "dummy_id"})()
    def update_one(self, *args, **kwargs):
        return type("Result", (), {"modified_count": 0, "matched_count": 0})()
    def update_many(self, *args, **kwargs):
        return type("Result", (), {"modified_count": 0})()
    def count_documents(self, *args, **kwargs):
        return 0
    def aggregate(self, *args, **kwargs):
        return []
    def create_index(self, *args, **kwargs):
        pass

class SafeDBFallback:
    def __getitem__(self, name):
        return SafeCollectionFallback(name)

try:
    client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000)
    db = client['bingo_db']
    wallets = db['wallets']
    blocked_phones = db['blocked_phones'] 
    transactions = db['transactions']
    admin_state = db['admin_state']
    user_state = db['user_state']

    wallets.create_index("phone", unique=True)
    wallets.create_index("telegram_id", unique=True, sparse=True)
    blocked_phones.create_index("phone", unique=True)
    transactions.create_index("timestamp")
    transactions.create_index("status")
    user_state.create_index("chat_id", unique=True)
    print("✅ MongoDB connected successfully!")
except Exception as e:
    print(f"⚠️ [MongoDB Warning] የዳታቤዝ ግንኙነት አልተሳካም: {e}")
    db = SafeDBFallback()
    wallets = db['wallets']
    blocked_phones = db['blocked_phones']
    transactions = db['transactions']
    admin_state = db['admin_state']
    user_state = db['user_state']

# የስልክ ቁጥር ትክክለኛነት ማረጋገጫ (Valid Ethiopian Phone check)
def is_valid_ethiopian_phone(phone_str):
    if not phone_str:
        return False
    clean = re.sub(r'[^0-9]', '', str(phone_str))
    if (clean.startswith("2519") or clean.startswith("2517")) and len(clean) == 12:
        return True
    if (clean.startswith("09") or clean.startswith("07")) and len(clean) == 10:
        return True
    if (clean.startswith("9") or clean.startswith("7")) and len(clean) == 9:
        return True
    return False

# የስልክ ቁጥር ፎርማት ማስተካከያ (Formatting to 2519xxxxxxxx)
def format_phone_number(phone_str):
    if not phone_str:
        return ""
    clean = re.sub(r'[^0-9]', '', str(phone_str))
    if (clean.startswith("2519") or clean.startswith("2517")) and len(clean) == 12:
        return clean
    elif clean.startswith("09") and len(clean) == 10:
        return "251" + clean[1:]
    elif clean.startswith("07") and len(clean) == 10:
        return "251" + clean[1:]
    elif (clean.startswith("9") or clean.startswith("7")) and len(clean) == 9:
        return "251" + clean
    return clean

# የተጠቃሚውን ዋሌት እና ትክክለኛ ስልክ ቁጥር ማግኛ
def get_user_wallet_and_phone(chat_id_val):
    if not chat_id_val:
        return None, None
    chat_str = str(chat_id_val)
    
    user = wallets.find_one({
        "$or": [
            {"chat_id": chat_str},
            {"telegram_id": chat_str}
        ]
    })
    if user:
        ph = user.get("phone", "")
        if is_valid_ethiopian_phone(ph):
            return user, format_phone_number(ph)
            
    try:
        chat_int = int(chat_id_val)
        user_int = wallets.find_one({
            "$or": [
                {"chat_id": chat_int},
                {"telegram_id": chat_int}
            ]
        })
        if user_int:
            ph = user_int.get("phone", "")
            if is_valid_ethiopian_phone(ph):
                return user_int, format_phone_number(ph)
    except:
        pass
        
    return user, None

# የ Rate Limit ማረጋገጫ (በ5 ደቂቃ ውስጥ ቢበዛ 2 ጥያቄ ብቻ)
def check_rate_limit(phone_number):
    five_minutes_ago = datetime.utcnow() - timedelta(minutes=5)
    recent_requests_count = transactions.count_documents({
        "phone": phone_number,
        "type": {"$in": ["deposit", "withdrawal"]},
        "timestamp": {"$gte": five_minutes_ago}
    })
    return recent_requests_count < 2

# የ SMS ፎርማት ማረጋገጫ
def validate_sms_text(sms_text):
    clean = sms_text.strip()
    if clean.isdigit():
        return False, "❌ ያስገቡት መረጃ ቁጥር ብቻ ነው። እባክዎ ከቴሌብር/ሲቢኢ የደረሰዎትን ሙሉ SMS ኮፒ አድርገው ይላኩ!"
    digits = re.findall(r'\d', clean)
    if not digits:
        return False, "❌ ያስገቡት መረጃ ፊደል ብቻ ነው። እባክዎ ከቴሌብር/ሲቢኢ የደረሰዎትን ሙሉ SMS ኮፒ አድርገው ይላኩ!"
    if len(digits) <= 9:
        return False, "❌ ያስገቡት መረጃ ትክክል አይደለም (ስልክ ቁጥር ብቻ መሆን የለበትም)። እባክዎ ከቴሌብር/ሲቢኢ የደረሰዎትን ሙሉ SMS ኮፒ አድርገው ይላኩ!"
    if len(clean) < 15:
        return False, "❌ ያስገቡት የSMS መረጃ በጣም አጭር ነው። እባክዎ ከቴሌብር/ሲቢኢ የደረሰዎትን ሙሉ SMS ኮፒ አድርገው ይላኩ!"
    return True, ""

# ከ SMS ውስጥ የገንዘብ መጠን ማውጫ
def extract_amount_from_sms(sms_text):
    match = re.search(r'(\d+(?:\.\d+)?)\s*(?:ETB|ብር|birr)', sms_text, re.IGNORECASE)
    if match:
        try:
            return float(match.group(1))
        except:
            pass
    match2 = re.search(r'(?:credited|deposited|amount|transferred)\D+(\d+(?:\.\d+)?)', sms_text, re.IGNORECASE)
    if match2:
        try:
            return float(match2.group(1))
        except:
            pass
    return 0.0

def get_pending_button_text():
    count = transactions.count_documents({"status": "pending"})
    if count == 0:
        return "⏳ የሚጠብቁ ጥያቄዎች (0)"
    else:
        return f"🚨 የሚጠብቁ ጥያቄዎች ({count})"

def get_admin_dashboard_keyboard():
    pending_btn_txt = get_pending_button_text()
    return {
        "inline_keyboard": [
            [
                {"text": pending_btn_txt, "callback_data": "admin_pending_req"},
                {"text": "📅 የእለት/የሳምንት ገቢ (20%)", "callback_data": "admin_financial_stats"}
            ],
            [
                {"text": "🔗 የኤጀንት ሊንክ (Gen Link)", "callback_data": "admin_gen_agent_link"},
                {"text": "👥 የኤጀንት ስታቲስቲክስ", "callback_data": "admin_agent_stats_guide"}
            ],
            [
                {"text": "📋 የሁሉም ተጠቃሚዎች ባላንስ", "callback_data": "admin_show_all_bal"},
                {"text": "📜 የትራንዛክሽን ታሪክ (ፈልግ)", "callback_data": "admin_history_req"}
            ],
            [
                {"text": "➕ ባላንስ ጨምር (/add)", "callback_data": "guide_add"},
                {"text": "➖ ባላንስ ቀንስ (/sub)", "callback_data": "guide_sub"}
            ],
            [
                {"text": "📢 መልእክት አስላልፍ (/broadcast)", "callback_data": "guide_broadcast"},
                {"text": "🚫 ብሎክ / ማጥፊያ", "callback_data": "guide_block_remove"}
            ],
            [
                {"text": "🔄 ሰሌዳውን አድስ (Refresh)", "callback_data": "admin_refresh_dashboard"}
            ]
        ]
    }

def get_player_play_keyboard(phone, referred_by=None):
    u_10 = f"{WEB_APP_URL}/10?phone={phone}"
    u_20 = f"{WEB_APP_URL}/20?phone={phone}"
    u_50 = f"{WEB_APP_URL}/50?phone={phone}"
    u_100 = f"{WEB_APP_URL}/100?phone={phone}"
    u_100 = f"{WEB_APP_URL}/100?phone={phone}"
    u_200 = f"{WEB_APP_URL}/200?phone={phone}"
    u_500 = f"{WEB_APP_URL}/500?phone={phone}"
    u_super = f"{WEB_APP_URL}/superbesh"
    if referred_by:
        u_super += f"?ref={referred_by}"
    else:
        u_super += f"?phone={phone}"

    return {
        "inline_keyboard": [
            [
                {"text": "🎮 PLAY | በ10 ብር", "web_app": {"url": u_10}},
                {"text": "🎮 PLAY | በ20 ብር", "web_app": {"url": u_20}}
            ],
            [
                {"text": "🎮 PLAY | በ50 ብር", "web_app": {"url": u_50}},
                {"text": "🔥 PLAY | በ100 ብር", "web_app": {"url": u_100}}
            ],
            [
                {"text": "💎 PLAY | በ200 ብር", "web_app": {"url": u_200}},
                {"text": "👑 PLAY | በ500 ብር", "web_app": {"url": u_500}}
            ],
            [
                {"text": "🏆 BeshBingo (ሳምንታዊ) | 50 ብር", "web_app": {"url": u_super}},
                {"text": "⚽ BeshBingo Bonus", "callback_data": "Besh_bingo_bonus"}
            ]
        ]
    }

game_states = {
    "10": {
        "status": "lobby", "timer": 30, "ball_timer": 3, "pot": 0, "players": {}, 
        "sold_tickets": {}, "current_ball": "--", "drawn_balls": [], "winner": None,
        "winners_data": [], "winning_card": None, "winning_ticket_num": None, 
        "winning_indices": None, "winning_line_name": None, "all_cards": {}
    },
    "20": {
        "status": "lobby", "timer": 30, "ball_timer": 3, "pot": 0, "players": {}, 
        "sold_tickets": {}, "current_ball": "--", "drawn_balls": [], "winner": None,
        "winners_data": [], "winning_card": None, "winning_ticket_num": None, 
        "winning_indices": None, "winning_line_name": None, "all_cards": {}
    },
    "50": {
        "status": "lobby", "timer": 30, "ball_timer": 3, "pot": 0, "players": {}, 
        "sold_tickets": {}, "current_ball": "--", "drawn_balls": [], "winner": None,
        "winners_data": [], "winning_card": None, "winning_ticket_num": None, 
        "winning_indices": None, "winning_line_name": None, "all_cards": {}
    },
    "100": {
        "status": "lobby", "timer": 30, "ball_timer": 3, "pot": 0, "players": {}, 
        "sold_tickets": {}, "current_ball": "--", "drawn_balls": [], "winner": None,
        "winners_data": [], "winning_card": None, "winning_ticket_num": None, 
        "winning_indices": None, "winning_line_name": None, "all_cards": {}
    },
    "200": {
        "status": "lobby", "timer": 30, "ball_timer": 3, "pot": 0, "players": {}, 
        "sold_tickets": {}, "current_ball": "--", "drawn_balls": [], "winner": None,
        "winners_data": [], "winning_card": None, "winning_ticket_num": None, 
        "winning_indices": None, "winning_line_name": None, "all_cards": {}
    },
    "500": {
        "status": "lobby", "timer": 30, "ball_timer": 3, "pot": 0, "players": {}, 
        "sold_tickets": {}, "current_ball": "--", "drawn_balls": [], "winner": None,
        "winners_data": [], "winning_card": None, "winning_ticket_num": None, 
        "winning_indices": None, "winning_line_name": None, "all_cards": {}
    },
    "super": { 
        "status": "lobby", "timer": 30, "ball_timer": 3, "pot": 0, "players": {}, 
        "sold_tickets": {}, "current_ball": "--", "drawn_balls": [], "winner": None,
        "winners_data": [], "winning_card": None, "winning_ticket_num": None, 
        "winning_indices": None, "winning_line_name": None, "all_cards": {}
    }
}

ROOM_PRICES = {
    "10": 10,
    "20": 20,
    "50": 50,
    "100": 100,
    "200": 200,
    "500": 500,
    "super": 50
}

loop_started = False
reset_task_references = {"10": None, "20": None, "50": None, "100": None, "200": None, "500": None, "super": None}
pending_claims = {"10": [], "20": [], "50": [], "100": [], "200": [], "500": [], "super": []}
claim_lock_active = {"10": False, "20": False, "50": False, "100": False, "200": False, "500": False, "super": False}

def sanitize_input(text):
    if not text:
        return ""
    return re.sub(r'[^\w\s\-\\.\@:]', '', str(text)).strip()

def send_telegram(text, reply_markup=None):
    def _send():
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": ADMIN_ID, "text": text, "parse_mode": "Markdown"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            requests.post(url, json=payload, timeout=3)
        except Exception as e:
            print(f"Telegram Error: {e}")
    gevent.spawn(_send)

def send_telegram_user(chat_id, text, reply_markup=None):
    def _send():
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            requests.post(url, json=payload, timeout=3)
        except Exception as e:
            print(f"Telegram User Error: {e}")
    gevent.spawn(_send)

def edit_telegram_message(chat_id, message_id, new_text, reply_markup=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": new_text,
        "parse_mode": "Markdown"
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    else:
        payload["reply_markup"] = {"inline_keyboard": []}
    try:
        requests.post(url, json=payload, timeout=3)
    except Exception as e:
        print(f"Edit Message Error: {e}")

def set_webhook():
    webhook_url = f"{WEB_APP_URL}/webhook"
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={webhook_url}"
    try:
        requests.get(url, timeout=3)
    except Exception as e:
        print(f"Webhook set failed: {e}")

def set_bot_commands():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setMyCommands"
    
    default_commands = [
        {"command": "play", "description": "🎮 ጨዋታ ይምረጡ"},
        {"command": "deposit", "description": "📥 ሂሳብ ለመሙላት (Deposit)"},
        {"command": "withdraw", "description": "📤 ብር ወጪ ለማድረግ (Withdraw)"},
        {"command": "transfer", "description": "💸 ብር ለማስተላለፍ (Transfer)"},
        {"command": "balance", "description": "💰 ቀሪ (Balance)"},
        {"command": "history", "description": "📥 የትራንዛክሽን ታሪክ"},
        {"command": "instruction", "description": "ℹ️ የጨዋታ ህጎች"}
    ]
    try:
        requests.post(url, json={"commands": default_commands}, timeout=3)
    except Exception as e:
        print(f"Error setting default commands: {e}")

    if ADMIN_ID:
        admin_commands = [
            {"command": "play", "description": "🎮 play"},
            {"command": "deposit", "description": "📥 Deposit"},
            {"command": "withdraw", "description": "📤 Withdraw"},
            {"command": "transfer", "description": "💸 Transfer"},
            {"command": "admin", "description": "🛠 Admin"},
            {"command": "pending", "description": "⏳ Approvals"},
            {"command": "agent", "description": "👥 Agent h"},
            {"command": "daily", "description": "📅 D/W ገቢና ወጪ"},
            {"command": "balance", "description": "💰 Balance"},
            {"command": "history", "description": "📥 History"},
            {"command": "instruction", "description": "ℹ️ Rule"},
        ]
        payload = {
            "commands": admin_commands,
            "scope": {
                "type": "chat",
                "chat_id": int(ADMIN_ID)
            }
        }
        try:
            requests.post(url, json=payload, timeout=3)
        except Exception as e:
            print(f"Error setting admin commands: {e}")

def broadcast_game_state(room_type):
    state = game_states[room_type]
    state_payload = {
        "status": state["status"],
        "timer": state["timer"],
        "ball_timer": state["ball_timer"],
        "pot": state["pot"],
        "sold_tickets": state["sold_tickets"],
        "current_ball": state["current_ball"],
        "drawn_balls": state["drawn_balls"],
        "winner": state["winner"],
        "winners_data": state.get("winners_data", []),
        "winning_card": state["winning_card"],
        "winning_ticket_num": state["winning_ticket_num"],
        "winning_indices": state.get("winning_indices"),
        "winning_line_name": state.get("winning_line_name"), 
        "all_cards": state.get("all_cards", {}), 
        "active_players": len(state["players"])
    }
    socketio.emit(f'game_update_{room_type}' if room_type != "super" else 'game_update', state_payload)

def notify_user_balance_update(phone_num, new_balance):
    socketio.emit('balance_update', {"phone": phone_num, "balance": new_balance})

def get_financial_stats():
    now = datetime.utcnow()
    today_start = datetime(now.year, now.month, now.day)
    week_start = now - timedelta(days=7)

    today_game_comm = list(transactions.aggregate([
        {"$match": {"type": "game_commission", "timestamp": {"$gte": today_start}}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
    ]))
    week_game_comm = list(transactions.aggregate([
        {"$match": {"type": "game_commission", "timestamp": {"$gte": week_start}}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
    ]))

    today_wit = list(transactions.aggregate([
        {"$match": {"type": "withdrawal", "status": "approved", "timestamp": {"$gte": today_start}}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
    ]))
    week_wit = list(transactions.aggregate([
        {"$match": {"type": "withdrawal", "status": "approved", "timestamp": {"$gte": week_start}}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
    ]))

    t_comm = today_game_comm[0]["total"] if today_game_comm else 0.0
    w_comm = week_game_comm[0]["total"] if week_game_comm else 0.0
    t_out = today_wit[0]["total"] if today_wit else 0.0
    w_out = week_wit[0]["total"] if week_wit else 0.0

    today_profit = t_comm - t_out
    week_profit = w_comm - w_out

    return (
        f"📊 *የፋይናንስ ስታቲስቲክስ ሪፖርት*\n\n"
        f"📅 *የዛሬው ውሎ (Daily):*\n"
        f" 🎮 ከጨዋታ የተገኘ (20%): `{t_comm:,.2f} ETB`\n"
        f" 📤 የወጣ ወጪ (Approved Withdraw): `{t_out:,.2f} ETB`\n"
        f" 💰 *የእለቱ የተጣራ ትርፍ:* `{today_profit:,.2f} ETB`\n\n"
        f"🗓 *የባለፉት 7 ቀናት (Weekly):*\n"
        f" 🎮 ከጨዋታ የተገኘ (20%): `{w_comm:,.2f} ETB`\n"
        f" 📤 የወጣ ወጪ (Approved Withdraw): `{w_out:,.2f} ETB`\n"
        f" 💰 *የሳምንቱ የተጣራ ትርፍ:* `{week_profit:,.2f} ETB`"
    )

def check_winning_line(card, drawn_numbers, player_marked_numbers=None):
    drawn_set = set()
    for b in drawn_numbers:
        if len(b) > 1:
            try:
                drawn_set.add(int(b[1:]))
            except ValueError:
                pass
    drawn_set.add(0) 
    marked_set = set(player_marked_numbers) if player_marked_numbers is not None else None

    def is_hit(idx):
        val = card[idx]
        if idx == 12 or val == 0 or val == "Besh" or val == "★":
            return True
        try:
            val_int = int(val)
            if marked_set is not None:
                return (val_int in drawn_set) and (val_int in marked_set)
            return val_int in drawn_set
        except:
            return False

    all_win_indices = set()
    line_types = []
    
    for i in range(5):
        row_indices = [i*5 + j for j in range(5)]
        if all(is_hit(idx) for idx in row_indices):
            all_win_indices.update(row_indices)
            line_types.append(f"ረድፍ {i+1}")
            
    for j in range(5):
        col_indices = [j + i*5 for i in range(5)]
        if all(is_hit(idx) for idx in col_indices):
            all_win_indices.update(col_indices)
            line_types.append(f"አምድ {j+1}")
            
    diag1_indices = [0, 6, 12, 18, 24]
    if all(is_hit(idx) for idx in diag1_indices):
        all_win_indices.update(diag1_indices)
        line_types.append("ዲያጎናል ↘")
        
    diag2_indices = [4, 8, 12, 16, 20]
    if all(is_hit(idx) for idx in diag2_indices):
        all_win_indices.update(diag2_indices)
        line_types.append("ዲያጎናል ↙")
        
    corner_indices = [0, 4, 20, 24]
    if all(is_hit(idx) for idx in corner_indices):
        all_win_indices.update(corner_indices)
        line_types.append("ኮርነር (4 ማዕዘኖች)")

    if all_win_indices:
        return list(all_win_indices), " + ".join(line_types)
    return None, None

def refund_all_sold_tickets(room_type):
    price = ROOM_PRICES.get(room_type, 10)
    state = game_states[room_type]
    for t_num, phone_num in list(state["sold_tickets"].items()):
        if phone_num != "RESERVED_LOCK":
            updated_user = wallets.find_one_and_update({"phone": phone_num}, {"$inc": {"balance": price}}, return_document=True)
            if updated_user:
                notify_user_balance_update(phone_num, updated_user.get("balance", 0))

def reset_game(room_type):
    global reset_task_references, claim_lock_active, pending_claims
    reset_task_references[room_type] = None
    claim_lock_active[room_type] = False
    pending_claims[room_type] = []
    
    default_ball_timer = 3
    game_states[room_type].update({
        "status": "lobby", "winner": None, "winners_data": [], "winning_card": None, "winning_ticket_num": None, 
        "winning_indices": None, "winning_line_name": None, "pot": 0, "players": {}, 
        "sold_tickets": {}, "drawn_balls": [], "current_ball": "--", "timer": 30, "ball_timer": default_ball_timer, "all_cards": {}
    })
    broadcast_game_state(room_type) 

# መደበኛ የጨዋታ ሎፕ (Rooms: 10, 20, 50, 100, 200)
# ምንም "READY" ሳይባል 3 ሰከንድ እንዳበቃ የመጀመሪያው ቁጥር ይወጣል
def run_game_loop(room_type):
    balls = [f"{'BINGO'[i//15]}{i+1}" for i in range(75)]
    global reset_task_references
    last_broadcasted_timer = -1
    
    while True:
        state = game_states[room_type]
        current_status = state["status"]
        if current_status == "lobby":
            for i in range(30, -1, -1):
                if state["status"] != "lobby": 
                    break
                state["timer"] = i
                if last_broadcasted_timer != i:
                    last_broadcasted_timer = i
                    broadcast_game_state(room_type) 
                socketio.sleep(1) 
            
            if state["status"] == "lobby" and len(state["players"]) >= 2:
                state["status"] = "playing"
                state["drawn_balls"] = []
                state["ball_timer"] = 3
                shuffled = balls.copy()
                random.shuffle(shuffled)
                broadcast_game_state(room_type)
            else:
                state["timer"] = 30
                broadcast_game_state(room_type)
                continue

            if shuffled:
                # 3 ሰከንድ ቆጠራ (3, 2, 1) - ያለ "READY" ፅሁፍ
                for j in range(3, 0, -1):
                    if state["status"] != "playing":
                        break
                    state["ball_timer"] = j
                    broadcast_game_state(room_type) 
                    socketio.sleep(1)

                state["ball_timer"] = 0

                # 3 ሰከንድ ካለቀ የመጀመሪያው ቁጥር ወዲያው ይወጣል
                for b in shuffled:
                    if state["status"] != "playing": 
                        break
                    if len(state["players"]) < 2:
                        state["status"] = "result"
                        state["winner"] = "No Winner (Insufficient Players)"
                        refund_all_sold_tickets(room_type)
                        
                        def player_shortage_reset():
                            for t in range(5, -1, -1):
                                if state["status"] != "result":
                                    return
                                state["timer"] = t
                                broadcast_game_state(room_type)
                                socketio.sleep(1)
                            reset_game(room_type)
                        reset_task_references[room_type] = socketio.start_background_task(player_shortage_reset)
                        break

                    state["current_ball"] = b
                    state["drawn_balls"].append(b)
                    broadcast_game_state(room_type) 
                    socketio.sleep(3.5) 
            
            if state["status"] == "playing":
                state["status"] = "result"
                state["winner"] = "No Winner (House)"
                refund_all_sold_tickets(room_type)
                def house_countdown_and_reset():
                    for t in range(5, -1, -1):
                        if state["status"] != "result":
                            return
                        state["timer"] = t
                        broadcast_game_state(room_type)
                        socketio.sleep(1)
                    reset_game(room_type)
                reset_task_references[room_type] = socketio.start_background_task(house_countdown_and_reset)
            broadcast_game_state(room_type)
        socketio.sleep(1)

# ሱፐር ጨዋታ ሎፕ (Super Game Loop)
def super_game_loop():
    balls = [f"{'BINGO'[i//15]}{i+1}" for i in range(75)]
    global reset_task_references
    while True:
        state = game_states["super"]
        current_status = state["status"]

        if current_status == "lobby":
            while True:
                current_time = time.gmtime()
                if current_time.tm_wday in [5, 6] and current_time.tm_hour == 14 and current_time.tm_min == 0 and current_time.tm_sec == 0:
                    break
                if state["status"] != "lobby":
                    break
                socketio.sleep(0.5)

            for i in range(30, -1, -1):
                if state["status"] != "lobby": 
                    break
                state["timer"] = i
                broadcast_game_state("super") 
                socketio.sleep(1) 
            
            if state["status"] == "lobby" and len(state["players"]) >= 2:
                state["status"] = "playing"
                state["drawn_balls"] = []
                state["ball_timer"] = 3
                shuffled = balls.copy()
                random.shuffle(shuffled)
                broadcast_game_state("super")
            else:
                state["timer"] = 30
                broadcast_game_state("super")
                continue

            if shuffled:
                for j in range(3, 0, -1):
                    if state["status"] != "playing":
                        break
                    state["ball_timer"] = j
                    broadcast_game_state("super") 
                    socketio.sleep(1)

                state["ball_timer"] = 0

                for b in shuffled:
                    if state["status"] != "playing": 
                        break
                    if len(state["players"]) < 2:
                        state["status"] = "result"
                        state["winner"] = "No Winner (Insufficient Players)"
                        state["winning_card"] = None
                        state["winning_ticket_num"] = None
                        state["winning_indices"] = None
                        state["winning_line_name"] = None
                        send_telegram("ℹ️ ተጫዋቾች ከሁለት ስለወረዱ የሳምንቱ መጨረሻ ጨዋታው ተቋርቋል።")
                        break

                    state["current_ball"] = b
                    state["drawn_balls"].append(b)
                    broadcast_game_state("super") 
                    socketio.sleep(4) 
            
            if state["status"] == "playing":
                state["status"] = "result"
                state["winner"] = "No Winner (House)"
                state["winning_card"] = None
                state["winning_ticket_num"] = None
                state["winning_indices"] = None
                state["winning_line_name"] = None
                send_telegram("ℹ️ የሳምንቱ መጨረሻ ጨዋታ ያለ አሸናፊ ተጠናቋል።")
                
                def super_house_countdown_and_reset():
                    for t in range(10, -1, -1):
                        if state["status"] != "result":
                            return
                        state["timer"] = t
                        broadcast_game_state("super")
                        socketio.sleep(1)
                    reset_game("super")

                reset_task_references["super"] = socketio.start_background_task(super_house_countdown_and_reset)
            broadcast_game_state("super")

        socketio.sleep(1)

# ==========================================
# የዌብ-አፕ ሩቶች
# ==========================================
@app.route('/')
@app.route('/10')
@app.route('/ten')
def index_10(): 
    phone = sanitize_input(request.args.get('phone', ''))
    user_bal = None
    if phone:
        formatted_ph = format_phone_number(phone)
        u = wallets.find_one({"phone": formatted_ph})
        if u:
            user_bal = float(u.get("balance", 0.0))
    return render_template('index.html', initial_balance=user_bal)

@app.route('/20')
@app.route('/twenty')
def index_20():
    phone = sanitize_input(request.args.get('phone', ''))
    user_bal = None
    if phone:
        u = wallets.find_one({"phone": format_phone_number(phone)})
        if u:
            user_bal = float(u.get("balance", 0.0))
    return render_template('room20.html', initial_balance=user_bal)

@app.route('/50')
@app.route('/fifty')
def index_50():
    phone = sanitize_input(request.args.get('phone', ''))
    user_bal = None
    if phone:
        u = wallets.find_one({"phone": format_phone_number(phone)})
        if u:
            user_bal = float(u.get("balance", 0.0))
    return render_template('room50.html', initial_balance=user_bal)

@app.route('/100')
@app.route('/hundred')
def index_100():
    phone = sanitize_input(request.args.get('phone', ''))
    user_bal = None
    if phone:
        u = wallets.find_one({"phone": format_phone_number(phone)})
        if u:
            user_bal = float(u.get("balance", 0.0))
    return render_template('room100.html', initial_balance=user_bal)

@app.route('/200')
@app.route('/twohundred')
def index_200():
    phone = sanitize_input(request.args.get('phone', ''))
    user_bal = None
    if phone:
        u = wallets.find_one({"phone": format_phone_number(phone)})
        if u:
            user_bal = float(u.get("balance", 0.0))
    return render_template('room200.html', initial_balance=user_bal)

@app.route('/500')
@app.route('/fivehundred')
def index_500():
    phone = sanitize_input(request.args.get('phone', ''))
    user_bal = None
    if phone:
        u = wallets.find_one({"phone": format_phone_number(phone)})
        if u:
            user_bal = float(u.get("balance", 0.0))
    return render_template('room500.html', initial_balance=user_bal)

@app.route('/super')
@app.route('/superbesh')
def index_superbesh():
    return render_template('index3.html')

@app.route('/get_status')
def get_status():
    room = request.args.get('room', '10')
    state = game_states.get(room, game_states["10"])
    
    phone = sanitize_input(request.args.get('phone'))
    formatted_phone = format_phone_number(phone) if phone else None
    user = wallets.find_one({"phone": formatted_phone}) if formatted_phone else None
    if not user and room == "super":
        return jsonify({
            "success": False,
            "user_exists": False,
            "msg": "መለያዎ ከሲስተሙ ተሰርዟል! እባክዎ እንደገና ይመዝገቡ።"
        })
    elif not user:
        return jsonify({
            "success": False,
            "user_exists": False,
            "msg": "መለያዎ ከሲስተሙ ተሰርዟል! እባክዎ እንደገና ይመዝገቡ።"
        })
        
    db_phone = user['phone'] if user else (formatted_phone or phone)
    p_data = state["players"].get(db_phone, {"cards": {}})
    cards_list = list(p_data["cards"].values())
    clean_players = {k: {"username": v.get("username", ""), "cards": list(v.get("cards", {}).values())} for k, v in state["players"].items()}
    
    is_waiting = False
    if state["status"] in ["playing", "result"] and db_phone not in state["players"]:
        is_waiting = True

    return jsonify({
        "success": True,
        "user_exists": True,
        "status": state["status"],
        "timer": state["timer"],
        "ball_timer": state["ball_timer"],
        "pot": state["pot"],
        "sold_tickets": state["sold_tickets"],
        "current_ball": state["current_ball"],
        "drawn_balls": state["drawn_balls"],
        "winner": state["winner"],
        "winners_data": state.get("winners_data", []),
        "winning_card": state["winning_card"],
        "winning_ticket_num": state["winning_ticket_num"],
        "winning_indices": state.get("winning_indices"),
        "winning_line_name": state.get("winning_line_name"),
        "all_cards": state.get("all_cards", {}),
        "players": clean_players, 
        "balance": user.get('balance', 0) if user else 0, 
        "bonus": user.get('bonus', 0) if user else 0,
        "my_cards": cards_list, 
        "active_players": len(state["players"]),
        "is_waiting": is_waiting
    })

@app.route('/buy_specific_ticket', methods=['POST'])
def buy_ticket():
    d = request.json or {}
    ph, t_num, uname = sanitize_input(d.get('phone')), str(d.get('ticket_num')), sanitize_input(d.get('username'))
    room = str(d.get('room', '10'))
    formatted_ph = format_phone_number(ph)
    
    for r_key, r_state in game_states.items():
        if r_key != room and formatted_ph in r_state["players"]:
            return jsonify({"success": False, "msg": "በአንድ ጊዜ ከ 1 ሩም በላይ መጫወት አይቻልም!"})

    state = game_states.get(room, game_states["10"])
    price = ROOM_PRICES.get(room, 10)

    if not formatted_ph or not t_num:
        return jsonify({"success": False, "msg": "የተሳሳተ መረጃ!"})
    user = wallets.find_one({"phone": formatted_ph})
    if not user:
        return jsonify({"success": False, "msg": "ተጠቃሚው አልተገኘም!"})
    db_phone = user["phone"]

    if state["status"] != "lobby":
        return jsonify({"success": False, "msg": "ጨዋታ ተጀምሯል!"})

    if t_num in state["sold_tickets"]:
        return jsonify({"success": False, "msg": "ይህ ካርተላ ቀድሞ ተይዟል!"})
        
    if db_phone in state["players"] and len(state["players"][db_phone]["cards"]) >= 2:
        return jsonify({"success": False, "msg": "ከ 2 ካርተላ በላይ መግዛት አይቻልም!"})
    
    state["sold_tickets"][t_num] = "RESERVED_LOCK"

    res = wallets.find_one_and_update(
        {"phone": db_phone, "balance": {"$gte": price}}, 
        {"$inc": {"balance": -price}},
        return_document=True
    )
    if res:
        columns = [random.sample(range(r[0], r[1]+1), 5) for r in [(1,15), (16,30), (31,45), (46,60), (61,75)]]
        flat = [columns[c][r] for r in range(5) for c in range(5)]
        flat[12] = 0  
        
        state["sold_tickets"][t_num] = db_phone
        state["pot"] += price
        state.setdefault("all_cards", {})[t_num] = flat
        
        p_uname = uname if uname else res.get("username", f"User_{db_phone[-4:]}")
        if db_phone not in state["players"]:
            state["players"][db_phone] = {"cards": {t_num: flat}, "username": p_uname}
        else:
            state["players"][db_phone]["cards"][t_num] = flat
                
        gevent.spawn(notify_user_balance_update, db_phone, res.get("balance", 0))
        gevent.spawn(broadcast_game_state, room)
        return jsonify({"success": True, "balance": res.get("balance", 0)})
    
    if state["sold_tickets"].get(t_num) == "RESERVED_LOCK":
        del state["sold_tickets"][t_num]
        
    return jsonify({"success": False, "msg": "በቂ ባላንስ የለም!"})

@app.route('/cancel_ticket', methods=['POST'])
def cancel_ticket():
    d = request.json or {}
    ph, t_num = sanitize_input(d.get('phone')), str(d.get('ticket_num'))
    room = str(d.get('room', '10'))
    formatted_ph = format_phone_number(ph)
    
    if room == "super":
        return jsonify({"success": False, "msg": "ካርተላ ከተገዛ በኋላ መመለስ (ሰረዝ ማድረግ) አይቻልም!"})

    state = game_states.get(room, game_states["10"])
    price = ROOM_PRICES.get(room, 10)

    user = wallets.find_one({"phone": formatted_ph})
    if not user or state["status"] != "lobby":
        return jsonify({"success": False})
    db_phone = user["phone"]

    if state["sold_tickets"].get(t_num) == db_phone:
        res = wallets.find_one_and_update({"phone": db_phone}, {"$inc": {"balance": price}}, return_document=True)
        state["pot"] -= price
        del state["sold_tickets"][t_num]
        state.get("all_cards", {}).pop(t_num, None)
        if db_phone in state["players"]:
            state["players"][db_phone]["cards"].pop(t_num, None)
            if not state["players"][db_phone]["cards"]: 
                state["players"].pop(db_phone, None)
        if res:
            gevent.spawn(notify_user_balance_update, db_phone, res.get("balance", 0))
        gevent.spawn(broadcast_game_state, room) 
        return jsonify({"success": True})
    return jsonify({"success": False})

@app.route('/claim_bingo', methods=['POST'])
def claim_bingo():
    global claim_lock_active, pending_claims
    d = request.json or {}
    ph = sanitize_input(d.get('phone'))
    formatted_ph = format_phone_number(ph)
    room = str(d.get('room', '10'))
    state = game_states.get(room, game_states["10"])
    
    user_info = wallets.find_one({"phone": formatted_ph})
    if not user_info:
        return jsonify({"success": False, "msg": "ተጠቃሚው አልተገኘም!"})
    db_phone = user_info["phone"]

    if state["status"] not in ["playing", "result"]:
        return jsonify({"success": False, "msg": "ጨዋታው በሂደት ላይ አይደለም!"})
        
    p_data = state["players"].get(db_phone)
    if not p_data:
        return jsonify({"success": False, "msg": "ተጫዋቹ አልተገኘም!"})
        
    current_drawn_balls = state["drawn_balls"]
    if not current_drawn_balls:
        return jsonify({"success": False, "msg": "ኳስ አልወጣም!"})
        
    valid_win_found = False
    winning_ticket_num = None
    winning_card_data = None
    winning_line_type = None
    winning_indices_list = None
    
    if room == "super":
        marked_0 = d.get('marked_0', [])
        marked_1 = d.get('marked_1', [])
        cards_to_check = p_data["cards"]
        for t_num, card in cards_to_check.items():
            card_keys_list = list(cards_to_check.keys())
            card_index = card_keys_list.index(t_num)
            current_marked = marked_0 if card_index == 0 else marked_1
            win_indices, line_type = check_winning_line(card, current_drawn_balls, player_marked_numbers=current_marked)
            if win_indices is not None:
                valid_win_found = True
                winning_ticket_num = str(t_num)
                winning_card_data = card
                winning_line_type = line_type
                winning_indices_list = win_indices
                break
        if not valid_win_found:
            return jsonify({"success": False, "msg": "ቢንጎ ሙሉ በሙሉ አልሞላም (ሙሉ ዝግ ብቻ ነው የሚሰራው)!"})
    else:
        for t_num, card in p_data["cards"].items():
            win_indices, line_type = check_winning_line(card, current_drawn_balls, player_marked_numbers=None)
            if win_indices is not None:
                valid_win_found = True
                winning_ticket_num = str(t_num)
                winning_card_data = card
                winning_line_type = line_type
                winning_indices_list = win_indices
                break 
                
        if not valid_win_found:
            return jsonify({"success": False, "msg": "ቢንጎ አልሞላም!"})
        
    claim_info = {
        "phone": db_phone,
        "username": p_data["username"],
        "ticket_num": winning_ticket_num,
        "card": winning_card_data,
        "indices": winning_indices_list,
        "line_name": winning_line_type,
        "winning_ball": current_drawn_balls[-1]
    }

    if state["status"] == "playing":
        if not claim_lock_active[room]:
            claim_lock_active[room] = True
            state["status"] = "result"
            state["timer"] = 10
            pending_claims[room] = [claim_info]

            def process_claims_by_ball():
                global claim_lock_active, pending_claims
                socketio.sleep(0.2)

                total_prize = state["pot"] * 0.8  
                num_winners = len(pending_claims[room])

                # አሸናፊውን ወይም አሸናፊዎቹን በፎቶው መሰረት ማዘጋጀት:
                # 1 አሸናፊ ከሆነ: "{user} has won!"
                # 2 አሸናፊ ከሆነ: "{user} and 1 other have won!"
                # ከ 2 በላይ ከሆነ: "{user} and {num_winners - 1} others have won!"
                first_winner = pending_claims[room][0]
                first_name = first_winner['username']

                if num_winners == 1:
                    winner_display = f"{first_name} has won!"
                elif num_winners == 2:
                    winner_display = f"{first_name} and 1 other have won!"
                else:
                    others_count = num_winners - 1
                    winner_display = f"{first_name} and {others_count} others have won!"

                state["winner"] = winner_display
                state["winners_data"] = pending_claims[room]
                state["winning_card"] = first_winner["card"]  
                state["winning_ticket_num"] = first_winner["ticket_num"] 
                state["winning_indices"] = first_winner["indices"]
                state["winning_line_name"] = first_winner["line_name"] 

                def background_win_task():
                    house_commission = state["pot"] * 0.2
                    if house_commission > 0 and room != "super":
                        transactions.insert_one({
                            "type": "game_commission",
                            "amount": house_commission,
                            "room": room,
                            "timestamp": datetime.utcnow()
                        })

                    w_user_doc = wallets.find_one({"phone": pending_claims[room][0]["phone"]})
                    agent_info = f"\n📲 የያዘው ኤጀንት: `{w_user_doc.get('referred_by', 'የለም')}`" if w_user_doc and "referred_by" in w_user_doc and room == "super" else ""

                    if num_winners == 1:
                        w = pending_claims[room][0]
                        win_res = wallets.find_one_and_update(
                            {"phone": w["phone"]}, 
                            {"$inc": {"balance": total_prize}}, 
                            return_document=True
                        )
                        if win_res:
                            gevent.spawn(notify_user_balance_update, w["phone"], win_res.get("balance", 0))
                        
                        success_msg = f"🏆 *WINNER (Room {room})!* \n👤 Name: {w['username']} | 📞 Phone: `{w['phone']}` | 🎫 Ticket: {w['ticket_num']} \n🎯 Winning Ball: {w['winning_ball']} \n💰 Prize Won: {total_prize:.2f} ETB{agent_info}"
                        send_telegram(success_msg)
                    else:
                        share_prize = total_prize / num_winners
                        winner_texts = []
                        for w in pending_claims[room]:
                            w_res = wallets.find_one_and_update(
                                {"phone": w["phone"]}, 
                                {"$inc": {"balance": share_prize}}, 
                                return_document=True
                            )
                            if w_res:
                                gevent.spawn(notify_user_balance_update, w["phone"], w_res.get("balance", 0))
                            winner_texts.append(f"👤 {w['username']} (`{w['phone']}`) - 🎫 {w['ticket_num']}")
                        
                        success_msg = f"🏆 *WINNERS (Shared Prize on Ball {pending_claims[room][0]['winning_ball']})!* \n💰 Total Pot Share: {share_prize:.2f} ETB each ({num_winners} winners){agent_info}\n" + "\n".join(winner_texts)
                        send_telegram(success_msg)
                        
                    broadcast_game_state(room)

                gevent.spawn(background_win_task)

                def countdown_and_reset():
                    global claim_lock_active, pending_claims
                    for t in range(10, -1, -1):
                        if state["status"] != "result":
                            return
                        state["timer"] = t
                        broadcast_game_state(room)
                        socketio.sleep(1)
                    reset_game(room)

                socketio.start_background_task(countdown_and_reset)

            socketio.start_background_task(process_claims_by_ball)
        else:
            already_exists = any(c["phone"] == db_phone for c in pending_claims[room])
            if not already_exists:
                pending_claims[room].append(claim_info)

    elif state["status"] == "result" and claim_lock_active[room]:
        already_exists = any(c["phone"] == db_phone for c in pending_claims[room])
        if not already_exists:
            pending_claims[room].append(claim_info)

    return jsonify({"success": True})

# የትራንስፈር ዋና ማከናወኛ (Transfer Logic)
def process_user_transfer(chat_id, sender_phone, sender_wallet, target_phone_input, amount_input):
    try:
        amt_val = float(amount_input)
    except (ValueError, TypeError):
        send_telegram_user(chat_id, "❌ የብር መጠን ስህተት ነው። እባክዎ ትክክለኛ ቁጥር ያስገቡ!\nምሳሌ፦ `0925180847 50`")
        return False

    # 1. ትራንስፈር የሚፈቀደው ከ10 ብር በላይ ነው
    if amt_val < 10.0:
        send_telegram_user(chat_id, "❌ **ዝቅተኛው የማስተላለፊያ መጠን 10 ብር ነው!**\nእባክዎ 10 ብር ወይም ከዚያ በላይ ያስተላልፉ።")
        return False

    sender_current_bal = sender_wallet.get("balance", 0.0)
    bonus_amount = sender_wallet.get("bonus", 20.0)

    # 2. የመጀመሪያው ቦነስ 20 ብር ትራንስፈር ማድረግ አይቻልም
    # ተጠቃሚው ማስተላለፍ የሚችለው ከቦነሱ ውጪ ያለውን ባላንስ ብቻ ነው
    transferable_balance = max(0.0, sender_current_bal - bonus_amount)

    if amt_val > transferable_balance:
        send_telegram_user(
            chat_id,
            "❌ **የመጀመሪያውን 20 ብር ቦነስ ትራንስፈር ማድረግ አይቻልም!**\n\n"
            f"💰 አጠቃላይ ባላንስዎ፦ `{sender_current_bal:,.2f} ETB`\n"
            f"🎁 የማይተላለፍ ቦነስ፦ `{bonus_amount:,.2f} ETB`\n"
            f"💵 ማስተላለፍ የሚችሉት፦ `*{transferable_balance:,.2f} ETB*`\n\n"
            "ቦነሱ ለጨዋታ መጫወቻ ብቻ የተዘጋጀ ነው። ተጫውተው ያሸነፉትን ወይም ያስገቡትን ገንዘብ ብቻ ነው ማስተላለፍ የሚችሉት።"
        )
        return False

    formatted_rec_phone = format_phone_number(target_phone_input)
    if not is_valid_ethiopian_phone(formatted_rec_phone):
        send_telegram_user(chat_id, "❌ ያስገቡት ተቀባይ ስልክ ቁጥር ትክክለኛ የኢትዮጵያ ስልክ አይደለም! እባክዎ ያረጋግጡ።\nምሳሌ፦ `0925180847 50`")
        return False

    if sender_phone == formatted_rec_phone:
        send_telegram_user(chat_id, "❌ ራስዎ ለራስዎ ብር ማስተላለፍ አይችሉም!")
        return False

    rec_wallet = wallets.find_one({
        "$or": [
            {"phone": formatted_rec_phone},
            {"username": target_phone_input.lstrip("@")}
        ]
    })
    if not rec_wallet:
        send_telegram_user(chat_id, "❌ ተቀባዩ ተጠቃሚ በሲስተሙ ውስጥ አልተገኘም! እባክዎ መጀመሪያ ተቀባዩ ቦቱን /start ማለቱን እና ስልኩን ማረጋገጡን ያረጋግጡ።")
        return False

    rec_phone = rec_wallet.get("phone")
    rec_chat_id = rec_wallet.get("chat_id")

    debit_res = wallets.find_one_and_update(
        {"phone": sender_phone, "balance": {"$gte": amt_val}},
        {"$inc": {"balance": -amt_val}},
        return_document=True
    )
    if not debit_res:
        send_telegram_user(chat_id, "❌ ዝውውሩ አልተሳካም። እባክዎ ቀሪ ሂሳብዎን ፈትሸው እንደገና ይሞክሩ።")
        return False

    credit_res = wallets.find_one_and_update(
        {"phone": rec_phone},
        {"$inc": {"balance": amt_val}},
        return_document=True
    )

    now_time = datetime.utcnow()
    now_str = (now_time + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")

    transactions.insert_one({
        "phone": sender_phone,
        "receiver_phone": rec_phone,
        "type": "transfer",
        "amount": amt_val,
        "status": "completed",
        "timestamp": now_time
    })

    notify_user_balance_update(sender_phone, debit_res.get("balance", 0))
    notify_user_balance_update(rec_phone, credit_res.get("balance", 0))

    sender_msg = (
        "✅ **ትራንስፈር በተሳካ ሁኔታ ተከናውኗል! (Transfer Success)**\n\n"
        f"📤 የተላከለት፦ `{rec_phone}` ({rec_wallet.get('first_name', 'User')})\n"
        f"💵 የተላለፈው ብር፦ `*{amt_val:,.2f} ETB*`\n"
        f"💰 አዲሱ ቀሪ ሂሳብዎ፦ `*{debit_res.get('balance', 0):,.2f} ETB*`\n"
        f"📅 ቀን፦ `{now_str}`"
    )
    send_telegram_user(chat_id, sender_msg)

    if rec_chat_id:
        rec_msg = (
            "🎁 **ገንዘብ ገቢ ተደርጎልዎታል! (Transfer Received)**\n\n"
            f"📥 የላከው ሰው፦ `{sender_phone}` ({sender_wallet.get('first_name', 'User')})\n"
            f"💵 የገባው ብር፦ `*{amt_val:,.2f} ETB*`\n"
            f"💰 አጠቃላይ ቀሪ ሂሳብዎ፦ `*{credit_res.get('balance', 0):,.2f} ETB*`\n"
            f"📅 ቀን፦ `{now_str}`"
        )
        send_telegram_user(rec_chat_id, rec_msg)
    return True

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json or {}
    
    if "message" in data:
        msg = data["message"]
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "").strip()
        first_name = msg.get("from", {}).get("first_name", "User")
        
        if chat_id != str(ADMIN_ID):
            wallets.update_one(
                {"chat_id": chat_id}, 
                {"$set": {"chat_id": chat_id}}, 
                upsert=False
            )
        
        # 1. ተጠቃሚው ስልክ ቁጥሩን በ Contact Sharing ሲያጋራ
        if "contact" in msg:
            contact = msg["contact"]
            raw_phone = contact.get("phone_number", "")
            formatted_phone = format_phone_number(raw_phone)
            telegram_id = str(contact.get("user_id", chat_id))
            first_name = contact.get("first_name", msg.get("from", {}).get("first_name", "User"))
            username = msg.get("from", {}).get("username", "")
            
            wallets.delete_many({
                "$and": [
                    {"$or": [{"chat_id": str(chat_id)}, {"telegram_id": str(telegram_id)}]},
                    {"phone": {"$ne": formatted_phone}}
                ]
            })
            
            wallets.update_one(
                {"phone": formatted_phone},
                {
                    "$set": {
                        "phone": formatted_phone,
                        "telegram_id": str(telegram_id),
                        "chat_id": str(chat_id),
                        "first_name": first_name,
                        "username": username if username else first_name,
                        "status": "ACTIVE",
                        "updated_at": datetime.utcnow()
                    },
                    "$setOnInsert": {
                        "balance": 20.00,
                        "bonus": 20.00,
                        "created_at": datetime.utcnow()
                    }
                },
                upsert=True
            )
            
            reg_success_msg = (
                "✅ **ምዝገባዎ በተሳካ ሁኔታ ተጠናቋል!**\n\n"
                f"📞 የተመዘገበ ስልክ ቁጥር፦ `{formatted_phone}`\n"
                "🎁 የ 20.00 ETB መነሻ ቦነስ ቀሪ ሂሳብ ተሰጥቶዎታል።\n\n"
                "አሁን ዲፖዚት ማድረግ (/deposit)፣ ወጪ ማድረግ (/withdraw) ወይም ከታች ካሉት ክፍሎች መርጠው መጫወት ይችላሉ፦"
            )
            send_telegram_user(chat_id, reg_success_msg, reply_markup={"remove_keyboard": True})

            play_keyboard = get_player_play_keyboard(formatted_phone)
            send_telegram_user(chat_id, "🕹 *PLAY IN:*\nጨዋታ ለመጀመር ከታች ካሉት ክፍሎች አንዱን ይምረጡ፦", reply_markup=play_keyboard)
            return "OK", 200

        # የአድሚን ስቴት አያያዝ
        if chat_id == str(ADMIN_ID):
            state = admin_state.find_one({"chat_id": chat_id})
            
            if state and state.get("action") == "awaiting_agent_phone":
                agent_phone = sanitize_input(text)
                admin_state.delete_one({"chat_id": chat_id})
                
                bot_info = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe").json()
                bot_username = bot_info.get("result", {}).get("username", "superbeshbingobot")
                agent_link = f"https://t.me/{bot_username}?start={agent_phone}"
                
                send_telegram(
                    f"🔗 *ለኤጀንት የተዘጋጀ ሊንክ፦*\n\n"
                    f"📞 የኤጀንት ስልክ: `{agent_phone}`\n"
                    f"👇 የሪፈራል ሊንክ:\n`{agent_link}`"
                )
                return "OK", 200

            elif state and state.get("action") == "awaiting_agent_stats_phone":
                agent_ph = format_phone_number(sanitize_input(text))
                admin_state.delete_one({"chat_id": chat_id})
                
                referred_users = list(wallets.find({"referred_by": agent_ph}))
                total_referred = len(referred_users)
                user_phones = [u["phone"] for u in referred_users]
                
                deposits = list(transactions.find({"phone": {"$in": user_phones}, "type": "deposit", "status": "approved"}))
                total_dep_count = len(deposits)
                total_dep_amt = sum([d.get("amount", 0) for d in deposits])
                
                withdrawals = list(transactions.find({"phone": {"$in": user_phones}, "type": "withdrawal", "status": "approved"}))
                total_wit_amt = sum([w.get("amount", 0) for w in withdrawals])

                rep_msg = (
                    f"👥 *የኤጀንት ስታቲስቲክስ ሪፖርት*\n"
                    f"📞 የኤጀንት ስልክ: `{agent_ph}`\n\n"
                    f"👤 የተመዘገቡ ሰዎች ብዛት: *{total_referred} ተጠቃሚዎች*\n"
                    f"📥 ዲፖዚት ያደረጉ: *{total_dep_count} ግብይቶች* (ጠቅላላ: `{total_dep_amt:,.2f} ETB`)\n"
                    f"📤 ዊዝድሮው የተደረገ: `{total_wit_amt:,.2f} ETB`"
                )
                send_telegram(rep_msg)
                return "OK", 200

            elif state and state.get("action") == "awaiting_add_balance":
                admin_state.delete_one({"chat_id": chat_id})
                parts = text.split()
                if len(parts) >= 2:
                    target_ph = format_phone_number(parts[0])
                    try:
                        amt = float(parts[1])
                        u = wallets.find_one_and_update({"phone": target_ph}, {"$inc": {"balance": amt}}, return_document=True, upsert=True)
                        if u:
                            send_telegram(f"✅ ለ `{target_ph}` ተጠቃሚ {amt} ETB ተጨምሯል። አዲስ ባላንስ፦ {u.get('balance')} ETB")
                            notify_user_balance_update(target_ph, u.get('balance', 0))
                    except ValueError:
                        send_telegram("❌ ትክክለኛ የገንዘብ መጠን ያስገቡ!")
                else:
                    send_telegram("❌ ትክክለኛ ፎርማት አላስገቡም። ምሳሌ: `0911223344 50`")
                return "OK", 200

            elif state and state.get("action") == "awaiting_sub_balance":
                admin_state.delete_one({"chat_id": chat_id})
                parts = text.split()
                if len(parts) >= 2:
                    target_ph = format_phone_number(parts[0])
                    try:
                        amt = float(parts[1])
                        u = wallets.find_one_and_update({"phone": target_ph}, {"$inc": {"balance": -amt}}, return_document=True)
                        if u:
                            send_telegram(f"✅ ከ `{target_ph}` ተጠቃሚ ላይ {amt} ETB ተቀንሷል። አዲስ ባላንስ፦ {u.get('balance')} ETB")
                            notify_user_balance_update(target_ph, u.get('balance', 0))
                    except ValueError:
                        send_telegram("❌ ትክክለኛ መጠን ያስገቡ!")
                else:
                    send_telegram("❌ ትክክለኛ ፎርማት አላስገቡም። ምሳሌ: `0911223344 50`")
                return "OK", 200

            elif state and state.get("action") == "awaiting_broadcast":
                admin_state.delete_one({"chat_id": chat_id})
                all_users = list(wallets.find({"chat_id": {"$exists": True, "$ne": ""}}))
                sent_count = 0
                
                if "photo" in msg:
                    photo_id = msg["photo"][-1]["file_id"]
                    caption = (
                        "🎉በሽ ቢንጎ :24 ሰአት live\n"
                        "🎉 ሱፐር በሽ ቢንጎ  ፡ 💰 10ሺ ደራሽ 🎉\n\n"
                        "🗓 ዘወትር ቅዳሜ እና እሁድ 🕙 12 ሰዐት\n"
                        "🎫 ካርቴላ ሳያልቅ ⏳ ቀድመው ይያዙ 🏃‍♂️\n\n"
                        "❓ ማንኛውም ጥያቄ ካለ\n"
                        "📞      0925960226\n"
                        "👉      [BeshBingoSupport](http://t.me/BeshBingoSupportbot)"
                    )
                    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
                    for u in all_users:
                        u_chat_id = u.get("chat_id")
                        if u_chat_id:
                            try:
                                requests.post(url, json={"chat_id": u_chat_id, "photo": photo_id, "caption": caption, "parse_mode": "Markdown"}, timeout=2)
                                sent_count += 1
                            except:
                                pass
                else:
                    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
                    for u in all_users:
                        u_chat_id = u.get("chat_id")
                        if u_chat_id:
                            try:
                                requests.post(url, json={"chat_id": u_chat_id, "text": text, "parse_mode": "Markdown"}, timeout=2)
                                sent_count += 1
                            except:
                                pass
                send_telegram(f"📢 ብሮድካስቱ ለ *{sent_count}* ተጠቃሚዎች በተሳካ ሁኔታ ተልኳል!")
                return "OK", 200

            elif state and state.get("action") == "awaiting_history_dates":
                parts = text.split()
                if len(parts) >= 3:
                    start_date_str = parts[0]
                    end_date_str = parts[1]
                    tx_type = parts[2].lower() 
                    admin_state.delete_one({"chat_id": chat_id})
                    
                    try:
                        start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
                        end_dt = datetime.strptime(end_date_str, "%Y-%m-%d") + timedelta(days=1)
                        
                        query = {
                            "timestamp": {"$gte": start_dt, "$lt": end_dt}
                        }
                        if tx_type in ["deposit", "withdrawal"]:
                            query["type"] = tx_type

                        user_txs = list(transactions.find(query).sort("timestamp", -1))
                        
                        if not user_txs:
                            send_telegram(f"📭 ከተጠቀሰው ቀን ({start_date_str} እስከ {end_date_str}) እና ዓይነት ({tx_type}) ጋር የሚዛመድ ታሪክ አልተገኘም።")
                        else:
                            report = f"📜 *የታሪክ ሪፖርት ({start_date_str} እስከ {end_date_str}) - [{tx_type.upper()}]*\n\n"
                            for tx in user_txs:
                                t_type = tx.get("type", "N/A").upper()
                                amt = tx.get("amount", 0)
                                st = tx.get("status", "N/A").upper()
                                r_name = tx.get("room", "N/A")
                                phone = tx.get("phone", "N/A")
                                ts = (tx.get("timestamp") + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") if tx.get("timestamp") else "N/A"
                                
                                report += f"⏱ `{ts}` | 📞 `{phone}`\n🏷 ሩም: *Room {r_name}* | *{t_type}*\n💵 `{amt}` ETB | Status: `{st}`\n------------------------\n"
                            send_telegram(report)
                    except Exception as e:
                        send_telegram("❌ የቀን አጻጻፍ ስህተት! እባክዎ በዚህ መልኩ ይጻፉ:\n`2026-10-01 2026-10-07 deposit`")
                else:
                    send_telegram("❌ ትክክለኛ መረጃ አላስገቡም። እባክዎ እንደገና ይሞክሩ:\n`2026-10-01 2026-10-07 deposit`")
                return "OK", 200

        # የተጠቃሚ ስቴት አያያዝ
        u_state = user_state.find_one({"chat_id": chat_id})
        
        # ትራንስፈር ስቴት: ተጠቃሚው ስልክ እና የብር መጠን ብቻ ሲያስገባ (ለምሳሌ 0925180847 50)
        if u_state and u_state.get("action") == "awaiting_transfer_input":
            if text.startswith("/"):
                user_state.delete_one({"chat_id": chat_id})
            else:
                user_wallet, user_phone = get_user_wallet_and_phone(chat_id)
                if not user_wallet or not user_phone:
                    send_telegram_user(chat_id, "❌ አካውንትዎ አልተገኘም። እባክዎ መጀመሪያ /start ብለው ስልክ ቁጥርዎን ያጋሩ።")
                    user_state.delete_one({"chat_id": chat_id})
                    return "OK", 200

                parts = text.split()
                if len(parts) >= 2:
                    rec_target = sanitize_input(parts[0])
                    amt_target = parts[1]
                    user_state.delete_one({"chat_id": chat_id})
                    process_user_transfer(chat_id, user_phone, user_wallet, rec_target, amt_target)
                else:
                    send_telegram_user(
                        chat_id, 
                        "❌ እባክዎ ስልክ ቁጥር እና የብር መጠን ብቻ በዚህ መልኩ ያስገቡ፦\n\n`0925180847 50`"
                    )
                return "OK", 200

        # 1. ዴፖዚት የ SMS ማስገቢያ ፍሰት
        if u_state and u_state.get("action") == "awaiting_deposit_sms":
            if text.startswith("/"):
                user_state.delete_one({"chat_id": chat_id})
            else:
                user_wallet, user_phone = get_user_wallet_and_phone(chat_id)
                if not user_phone:
                    contact_kb = {
                        "keyboard": [
                            [{"text": "📱 ስልክ ቁጥርዎን ያጋሩ (Share Phone Number)", "request_contact": True}]
                        ],
                        "resize_keyboard": True,
                        "one_time_keyboard": True
                    }
                    send_telegram_user(
                        chat_id, 
                        "📱 **እባክዎ መጀመሪያ ስልክ ቁጥርዎን ያጋሩ (Share Phone Number)!**\n\nዲፖዚት ለማድረግ ትክክለኛ ስልክ ቁጥርዎ በሲስተሙ ውስጥ መመዝገብ አለበት።\nከታች ያለውን **«📱 ስልክ ቁጥርዎን ያጋሩ»** የሚለውን ቁልፍ ይጫኑ።",
                        reply_markup=contact_kb
                    )
                    user_state.delete_one({"chat_id": chat_id})
                    return "OK", 200

                if not check_rate_limit(user_phone):
                    send_telegram_user(
                        chat_id, 
                        "⚠️ **ይቅርታ!**\nአንድ ተጠቃሚ በ 5 ደቂቃ ውስጥ 2 ጊዜ ብቻ ነው የዲፖዚት ወይም የወጪ (Withdraw) ጥያቄ መላክ የሚችለው። እባክዎ ጥቂት ደቂቃዎች ቆይተው እንደገና ይሞክሩ።"
                    )
                    user_state.delete_one({"chat_id": chat_id})
                    return "OK", 200

                is_valid, err_msg = validate_sms_text(text)
                if not is_valid:
                    send_telegram_user(chat_id, err_msg)
                    return "OK", 200

                method = u_state.get("method", "telebirr")
                extracted_amt = extract_amount_from_sms(text)
                user_state.delete_one({"chat_id": chat_id})

                tx_insert = transactions.insert_one({
                    "phone": user_phone,
                    "chat_id": chat_id,
                    "first_name": first_name,
                    "type": "deposit",
                    "method": method,
                    "amount": extracted_amt,
                    "sms_body": text,
                    "status": "pending",
                    "timestamp": datetime.utcnow()
                })
                tx_id = str(tx_insert.inserted_id)
                now_str = (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")

                ack_msg = (
                    "⏳ **የዲፖዚት ጥያቄዎ በተሳካ ሁኔታ ደርሷል!**\n\n"
                    f"📞 የተመዘገበ ስልክ፦ `{user_phone}`\n"
                    f"💳 መንገድ፦ *{method.upper()}*\n"
                    f"📅 ቀን፦ `{now_str}`\n"
                    "📌 ሁኔታ፦ *PENDING (በማረጋገጥ ላይ)*\n\n"
                    "አስተዳዳሪው የከፈሉትን መረጃ አረጋግጦ ሲያጸድቀው (Approve) የተጨመረው ብር እና አጠቃላይ ባላንስዎ በቅጽበት ይላክሎታል።"
                )
                send_telegram_user(chat_id, ack_msg)

                admin_notif = (
                    f"📥 *አዲስ የዲፖዚት ጥያቄ (Pending Deposit)*\n\n"
                    f"👤 *First-name:* {first_name}\n"
                    f"📞 *Phone (የተመዘገበ ስልክ):* `{user_phone}`\n"
                    f"💵 *Amount:* `{extracted_amt}` ETB\n"
                    f"💳 *Method:* {method.upper()}\n"
                    f"📅 *Date:* `{now_str}`\n\n"
                    f"📄 *የተላከው SMS:*\n`{text}`"
                )
                admin_kb = {
                    "inline_keyboard": [
                        [
                            {"text": "✅ Approve", "callback_data": f"app_dep_{tx_id}_{user_phone}_{extracted_amt}"},
                            {"text": "❌ Reject", "callback_data": f"rej_dep_{tx_id}_{user_phone}_{extracted_amt}"}
                        ],
                        [
                            {"text": "🚫 Ban User", "callback_data": f"ban_user_{user_phone}"}
                        ]
                    ]
                }
                send_telegram(admin_notif, reply_markup=admin_kb)
                return "OK", 200

        # 2. ዊዝድሮዋል መረጃ ማስገቢያ ፍሰት
        # ቦነስ withdrawn ማድረግ አይቻልም
        if u_state and u_state.get("action") == "awaiting_withdraw_details":
            if text.startswith("/"):
                user_state.delete_one({"chat_id": chat_id})
            else:
                user_wallet, user_phone = get_user_wallet_and_phone(chat_id)
                
                parts = text.strip().split(maxsplit=2)
                if len(parts) < 3:
                    send_telegram_user(
                        chat_id, 
                        "❌ **የተሳሳተ አሞላል!**\nእባክዎ መረጃውን በዚህ መልኩ ይላኩ፦\n`<መጠን> <የአካውንት_ቁጥር> <ሙሉ_ስም>`\n\nምሳሌ፦\n`50 0960736061 Abebe Kebede`"
                    )
                    return "OK", 200

                w_account = parts[1].strip()
                w_name = parts[2].strip()
                method = u_state.get("method", "telebirr")

                if not user_phone:
                    if is_valid_ethiopian_phone(w_account):
                        user_phone = format_phone_number(w_account)
                        wallets.update_one(
                            {"chat_id": str(chat_id)},
                            {"$set": {"phone": user_phone, "first_name": w_name}},
                            upsert=True
                        )
                        user_wallet = wallets.find_one({"phone": user_phone})
                    else:
                        contact_kb = {
                            "keyboard": [
                                [{"text": "📱 ስልክ ቁጥርዎን ያጋሩ (Share Phone Number)", "request_contact": True}]
                            ],
                            "resize_keyboard": True,
                            "one_time_keyboard": True
                        }
                        send_telegram_user(
                            chat_id, 
                            "📱 **እባክዎ መጀመሪያ ስልክ ቁጥርዎን ያጋሩ (Share Phone Number)!**\n\nወጪ ለማድረግ ትክክለኛ ስልክ ቁጥርዎ በሲስተሙ ውስጥ መመዝገብ አለበት።", 
                            reply_markup=contact_kb
                        )
                        user_state.delete_one({"chat_id": chat_id})
                        return "OK", 200

                if not check_rate_limit(user_phone):
                    send_telegram_user(
                        chat_id, 
                        "⚠️ **ይቅርታ!**\nአንድ ተጠቃሚ በ 5 ደቂቃ ውስጥ 2 ጊዜ ብቻ ነው የዲፖዚት ወይም የወጪ (Withdraw) ጥያቄ መላክ የሚችለው። እባክዎ ጥቂት ደቂቃዎች ቆይተው እንደገና ይሞክሩ።"
                    )
                    user_state.delete_one({"chat_id": chat_id})
                    return "OK", 200

                try:
                    w_amount = float(parts[0])
                    if w_amount <= 0:
                        raise ValueError()
                except ValueError:
                    send_telegram_user(chat_id, "❌ እባክዎ ትክክለኛ የብር መጠን ያስገቡ!")
                    return "OK", 200

                current_bal = user_wallet.get("balance", 0.0) if user_wallet else 0.0
                bonus_amt = user_wallet.get("bonus", 20.0) if user_wallet else 0.0

                # ቦነስ withdrawn ማድረግ አይቻልም
                withdrawable_bal = max(0.0, current_bal - bonus_amt)

                if w_amount > withdrawable_bal:
                    send_telegram_user(
                        chat_id, 
                        "❌ **የመጀመሪያውን የ 20 ብር ቦነስ ወጪ (Withdraw) ማድረግ አይቻልም!**\n\n"
                        f"💰 የእርስዎ አጠቃላይ ባላንስ፦ `{current_bal:,.2f} ETB`\n"
                        f"🎁 የማይወጣ ቦነስ፦ `{bonus_amt:,.2f} ETB`\n"
                        f"💵 ወጪ ሊደረግ የሚችል፦ `*{withdrawable_bal:,.2f} ETB*`\n\n"
                        "ቦነሱ ለጨዋታ መጫወቻ ብቻ ነው። በጨዋታ አሸንፈው ያገኙትን ተጨማሪ ገንዘብ ብቻ ነው ማውጣት የሚችሉት።"
                    )
                    user_state.delete_one({"chat_id": chat_id})
                    return "OK", 200

                deducted_user = wallets.find_one_and_update(
                    {"phone": user_phone, "balance": {"$gte": w_amount}},
                    {"$inc": {"balance": -w_amount}},
                    return_document=True
                )
                if not deducted_user:
                    send_telegram_user(chat_id, "❌ ክፍያው አልተሳካም። እባክዎ ቀሪ ሂሳብዎን ፈትሸው እንደገና ይሞክሩ።")
                    user_state.delete_one({"chat_id": chat_id})
                    return "OK", 200

                notify_user_balance_update(user_phone, deducted_user.get("balance", 0))
                user_state.delete_one({"chat_id": chat_id})

                tx_insert = transactions.insert_one({
                    "phone": user_phone,
                    "chat_id": chat_id,
                    "first_name": first_name,
                    "type": "withdrawal",
                    "method": method,
                    "amount": w_amount,
                    "account_number": w_account,
                    "account_name": w_name,
                    "status": "pending",
                    "timestamp": datetime.utcnow()
                })
                tx_id = str(tx_insert.inserted_id)
                now_str = (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")

                user_msg = (
                    "⏳ **የዊዝድሮዋል ጥያቄዎ ተመዝግቧል (PENDING)!**\n\n"
                    f"📞 የተመዘገበ ስልክ፦ `{user_phone}`\n"
                    f"💵 የወጪ መጠን፦ `{w_amount:,.2f} ETB`\n"
                    f"💳 መንገድ፦ *{method.upper()}*\n"
                    f"🏦 አካውንት፦ `{w_account}` ({w_name})\n"
                    f"💰 ቀሪ ሂሳብዎ፦ `*{deducted_user.get('balance', 0):,.2f} ETB*`\n"
                    f"📅 ቀን፦ `{now_str}`\n\n"
                    "አስተዳዳሪው ገንዘቡን ወደ አካውንትዎ ልኮ ሲያጸድቀው ማሳወቂያ ይደርስዎታል።"
                )
                send_telegram_user(chat_id, user_msg)

                admin_wit_msg = (
                    f"📤 *አዲስ የዊዝድሮዋል ጥያቄ (Pending Withdraw)*\n\n"
                    f"👤 *First-name:* {first_name}\n"
                    f"📞 *Phone (የተመዘገበ ስልክ):* `{user_phone}`\n"
                    f"🏦 *የተላከበት አካውንት/ስልክ:* `{w_account}`\n"
                    f"👤 *Account Name:* {w_name}\n"
                    f"💵 *Amount:* `{w_amount}` ETB\n"
                    f"💳 *Method:* {method.upper()}\n"
                    f"📅 *Date:* `{now_str}`"
                )
                admin_wit_kb = {
                    "inline_keyboard": [
                        [
                            {"text": "✅ Approve", "callback_data": f"app_wit_{tx_id}_{user_phone}_{w_amount}"},
                            {"text": "❌ Reject (Refund)", "callback_data": f"rej_wit_{tx_id}_{user_phone}_{w_amount}"}
                        ],
                        [
                            {"text": "🚫 Ban User", "callback_data": f"ban_user_{user_phone}"}
                        ]
                    ]
                }
                send_telegram(admin_wit_msg, reply_markup=admin_wit_kb)
                return "OK", 200

        # /deposit ትዕዛዝ
        if text.lower() == "/deposit":
            user_wallet, user_phone = get_user_wallet_and_phone(chat_id)
            if not user_phone:
                contact_kb = {
                    "keyboard": [
                        [{"text": "📱 ስልክ ቁጥርዎን ያጋሩ (Share Phone Number)", "request_contact": True}]
                    ],
                    "resize_keyboard": True,
                    "one_time_keyboard": True
                }
                send_telegram_user(
                    chat_id,
                    "📱 **እባክዎ መጀመሪያ ስልክ ቁጥርዎን ያጋሩ (Share Phone Number)!**\n\n"
                    "ዲፖዚት (Deposit) ለማድረግ ትክክለኛ ስልክ ቁጥርዎ በሲስተሙ ውስጥ መመዝገብ አለበት።\n\n"
                    "ከታች ያለውን **«📱 ስልክ ቁጥርዎን ያጋሩ»** የሚለውን ቁልፍ በመጫን ያጋሩ።",
                    reply_markup=contact_kb
                )
                return "OK", 200

            dep_keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "📱 TELE-BIRR (ቴሌብር)", "callback_data": "user_select_tele_dep"},
                        {"text": "🏦 CBE-BIRR (ሲቢኢ ብር)", "callback_data": "user_select_cbe_dep"}
                    ],
                    [
                        {"text": "🔙 ወደ ዋናው ማውጫ (Back)", "callback_data": "user_back_to_menu"}
                    ]
                ]
            }
            msg_text = (
                f"📥 **የገንዘብ ማስገቢያ መንገድ ይምረጡ (Select Deposit Method)**\n"
                f"📞 የእርስዎ ስልክ፦ `{user_phone}`\n\n"
                "እባክዎ ሂሳብዎን ለመሙላት የሚጠቀሙበትን መንገድ ይምረጡ፦"
            )
            send_telegram_user(chat_id, msg_text, reply_markup=dep_keyboard)
            return "OK", 200

        # /withdraw ትዕዛዝ
        if text.lower() == "/withdraw":
            user_wallet, user_phone = get_user_wallet_and_phone(chat_id)
            if not user_phone:
                contact_kb = {
                    "keyboard": [
                        [{"text": "📱 ስልክ ቁጥርዎን ያጋሩ (Share Phone Number)", "request_contact": True}]
                    ],
                    "resize_keyboard": True,
                    "one_time_keyboard": True
                }
                send_telegram_user(
                    chat_id,
                    "📱 **እባክዎ መጀመሪያ ስልክ ቁጥርዎን ያጋሩ (Share Phone Number)!**\n\n"
                    "ገንዘብ ወጪ ለማድረግ (Withdraw) ትክክለኛ ስልክ ቁጥርዎ በሲስተሙ ውስጥ መመዝገብ አለበት።\n\n"
                    "ከታች ያለውን **«📱 ስልክ ቁጥርዎን ያጋሩ»** የሚለውን ቁልፍ በመጫን ያጋሩ።",
                    reply_markup=contact_kb
                )
                return "OK", 200

            wit_keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "📱 TELE-BIRR (ቴሌብር)", "callback_data": "user_select_tele_wit"},
                        {"text": "🏦 CBE-BIRR (ሲቢኢ ብር)", "callback_data": "user_select_cbe_wit"}
                    ],
                    [
                        {"text": "🔙 ወደ ዋናው ማውጫ (Back)", "callback_data": "user_back_to_menu"}
                    ]
                ]
            }
            msg_text = (
                f"📤 **የገንዘብ ማውጫ መንገድ ይምረጡ (Select Withdrawal Method)**\n"
                f"📞 የእርስዎ ስልክ፦ `{user_phone}`\n\n"
                "ያሸነፉትን ገንዘብ ወጪ ለማድረግ የሚፈልጉበትን መንገድ ይምረጡ፦"
            )
            send_telegram_user(chat_id, msg_text, reply_markup=wit_keyboard)
            return "OK", 200

        # 3. ትራንስፈር (Transfer / P2P)
        # /transfer ሲነኩ እነሱ ስልክ እና የብር መጠን ብቻ ነው ማስገባት ያለባቸው ለምሳሌ 0925180847 50
        # ትራንስፈር የሚፈቀደው ከ10 ብር በላይ ነው
        # የመጀመሪያው ቦነስ 20 ብር ትራንስፈር ማድረግ አይቻልም
        if text.lower().startswith("/transfer"):
            user_wallet, user_phone = get_user_wallet_and_phone(chat_id)
            if not user_wallet or not user_phone:
                send_telegram_user(chat_id, "❌ አካውንትዎ አልተገኘም። እባክዎ መጀመሪያ /start ብለው ስልክ ቁጥርዎን ያጋሩ።")
                return "OK", 200

            parts = text.split()
            if len(parts) >= 3:
                # Direct format: /transfer 0925180847 50
                rec_target = sanitize_input(parts[1])
                amt_target = parts[2]
                process_user_transfer(chat_id, user_phone, user_wallet, rec_target, amt_target)
            else:
                # Set user state to await direct phone and amount
                user_state.update_one(
                    {"chat_id": chat_id},
                    {"$set": {"action": "awaiting_transfer_input"}},
                    upsert=True
                )
                msg_txt = (
                    "💸 **ብር ለማስተላለፍ (Transfer)**\n\n"
                    "እባክዎ የሚልኩለትን **ስልክ ቁጥር** እና **የብር መጠን** ብቻ ያስገቡ፦\n\n"
                    "`0925180847 50`\n\n"
                    "⚠️ *ማሳሰቢያ፦*\n"
                    "• ትራንስፈር የሚፈቀደው ከ 10 ብር በላይ ብቻ ነው።\n"
                    "• የመጀመሪያውን የ 20 ብር ቦነስ ማስተላለፍ አይቻልም።"
                )
                send_telegram_user(chat_id, msg_txt)
            return "OK", 200

        if text.lower() == "/balance":
            u_wallet = wallets.find_one({"chat_id": chat_id})
            if not u_wallet:
                send_telegram_user(chat_id, "❌ ምንም የተመዘገበ አካውንት አልተገኘም። እባክዎ መጀመሪያ /start በመጫን ስልክ ቁጥርዎን ያጋሩ!")
            else:
                u_phone = u_wallet.get("phone", "N/A")
                u_balance = u_wallet.get("balance", 0)
                u_bonus = u_wallet.get("bonus", 20.0)
                withdrawable = max(0.0, u_balance - u_bonus)
                bal_msg = (
                    f"💰 *የሂሳብ መግለጫ (Balance)*\n\n"
                    f"📞 ስልክ ቁጥር: `{u_phone}`\n"
                    f"💵 አጠቃላይ ቀሪ ባላንስዎ: *{u_balance:,.2f} ETB*\n"
                    f"🎁 ቦነስ: *{u_bonus:,.2f} ETB*\n"
                    f"📤 ወጪ/ትራንስፈር ሊደረግ የሚችል: *{withdrawable:,.2f} ETB*"
                )
                send_telegram_user(chat_id, bal_msg)
            return "OK", 200

        if text.lower() in ["/instruction", "/instraction"]:
            rules_msg = (
                "ℹ️ **የጨዋታ ህጎች እና መመሪያዎች (Game Rules)**\n\n"
                "1. እያንዳንዱ የቢንጎ ካርቴላ 24 ቁጥሮችን እና መሃል ላይ አንድ ነፃ ቦታ (Free Space) ይይዛል።\n"
                "2. ኳሶች በዘፈቀደ ሲወጡ በካርቴላዎ ላይ ያሉትን ቁጥሮች ይምረጡ።\n"
                "3. አግድም (Row)፣ ቁልቁል (Column)፣ ዲያጎናል ወይም 4ቱን ማዕዘናት የሞላ ተጫዋች BINGO! በማለት ያሸንፋል።\n"
                "4. ማንኛውም የክፍያ ወይም የጨዋታ ጥያቄ ካለዎት @BeshBingoSupport ያነጋግሩ።"
            )
            send_telegram_user(chat_id, rules_msg)
            return "OK", 200

        if text.lower() == "/history":
            if chat_id == str(ADMIN_ID):
                admin_state.update_one({"chat_id": ADMIN_ID}, {"$set": {"action": "awaiting_history_dates"}}, upsert=True)
                send_telegram(
                    "📅 እባክዎ ፍለጋ የሚፈልጉትን **የመጀመሪያ ቀን**፣ **የመጨረሻ ቀን** እና **የታሪክ ዓይነት** በዚህ ፎርማት ይጻፉ:\n\n`YYYY-MM-DD YYYY-MM-DD deposit`"
                )
                return "OK", 200

            u_wallet = wallets.find_one({"chat_id": chat_id})
            if not u_wallet:
                send_telegram_user(chat_id, "❌ ምንም የተመዘገበ መለያ አልተገኘም።")
                return "OK", 200

            u_phone = u_wallet.get("phone")
            since_24h = datetime.utcnow() - timedelta(hours=24)
            user_txs = list(transactions.find({
                "$or": [{"phone": u_phone}, {"receiver_phone": u_phone}],
                "timestamp": {"$gte": since_24h}
            }).sort("timestamp", -1))

            if not user_txs:
                send_telegram_user(chat_id, "📭 ባለፉት 24 ሰአታት ውስጥ ምንም አይነት የትራንዛክሽን ታሪክ የለዎትም።")
            else:
                report = f"📜 *የ ባለፉት 24 ሰአታት የትራንዛክሽን ታሪክዎ:*\n\n"
                for tx in user_txs:
                    t_type = tx.get("type", "N/A").upper()
                    amt = tx.get("amount", 0)
                    st = tx.get("status", "N/A").upper()
                    r_name = tx.get("room", "N/A")
                    ts = (tx.get("timestamp") + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") if tx.get("timestamp") else "N/A"
                    report += f"⏱ `{ts}` | 🏷 *{t_type}*\n💵 `{amt}` ETB | Status: `{st}`\n------------------------\n"
                send_telegram_user(chat_id, report)
            return "OK", 200

        if text.lower().startswith("/broadcast") or text.lower().startswith("/broadcast1"):
            if chat_id == str(ADMIN_ID):
                admin_state.update_one({"chat_id": ADMIN_ID}, {"$set": {"action": "awaiting_broadcast"}}, upsert=True)
                send_telegram("📢 እባክዎ ለሁሉም ተጠቃሚዎች ማስተላለፍ የሚፈልጉትን **ፎቶ ብቻ** (ካፕሽን ሳይጨምሩ) ወይም ጽሑፍ አሁን ይላኩ፦")
            return "OK", 200

        # የአድሚን ትዕዛዞች
        if chat_id == str(ADMIN_ID):
            parts = text.split()
            cmd = parts[0] if parts else ""

            if cmd == "/admin":
                admin_keyboard = get_admin_dashboard_keyboard()
                send_telegram("🛠 *የአድሚን ማኔጅመንት ሰሌዳ (Admin Workspace)*\nከታች ካሉት አማራጮች የሚፈልጉትን ይምረጡ፦", reply_markup=admin_keyboard)
                return "OK", 200

            elif cmd == "/daily":
                send_telegram(get_financial_stats())
                return "OK", 200

            elif cmd == "/agent":
                if len(parts) > 1:
                    agent_ph = format_phone_number(parts[1])
                    referred_users = list(wallets.find({"referred_by": agent_ph}))
                    total_referred = len(referred_users)
                    user_phones = [u["phone"] for u in referred_users]
                    
                    deposits = list(transactions.find({"phone": {"$in": user_phones}, "type": "deposit", "status": "approved"}))
                    total_dep_count = len(deposits)
                    total_dep_amt = sum([d.get("amount", 0) for d in deposits])
                    
                    withdrawals = list(transactions.find({"phone": {"$in": user_phones}, "type": "withdrawal", "status": "approved"}))
                    total_wit_amt = sum([w.get("amount", 0) for w in withdrawals])

                    rep_msg = (
                        f"👥 *የኤጀንት ስታቲስቲክስ ሪፖርት*\n"
                        f"📞 የኤጀንት ስልክ: `{agent_ph}`\n\n"
                        f"👤 የተመዘገቡ ሰዎች ብዛት: *{total_referred} ተጠቃሚዎች*\n"
                        f"📥 ዲፖዚት ያደረጉ: *{total_dep_count} ግብይቶች* (ጠቅላላ: `{total_dep_amt:,.2f} ETB`)\n"
                        f"📤 ዊዝድሮው የተደረገ: `{total_wit_amt:,.2f} ETB`"
                    )
                    send_telegram(rep_msg)
                else:
                    send_telegram("❌ እባክዎ የኤጀንቱን ስልክ ቁጥር ያስገቡ:\n`/agent <የኤጀንት_ስልክ>`")
                return "OK", 200

            elif cmd == "/pending":
                pendings = list(transactions.find({"status": "pending"}).limit(10))
                if not pendings:
                    send_telegram("✅ በአሁኑ ወቅት ምንም የሚጠብቅ የዲፖዚትም ሆነ የዊዝድሮዋል ጥያቄ የለም!")
                else:
                    for p in pendings:
                        p_id = str(p.get("_id"))
                        p_type = p.get("type", "deposit").upper()
                        p_ph = p.get("phone")
                        p_amt = p.get("amount", 0)
                        p_method = p.get("method", "telebirr").upper()
                        ts = (p.get("timestamp", datetime.utcnow()) + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
                        
                        btn_code = f"app_dep_{p_id}_{p_ph}_{p_amt}" if p_type == "DEPOSIT" else f"app_wit_{p_id}_{p_ph}_{p_amt}"
                        rej_code = f"rej_dep_{p_id}_{p_ph}_{p_amt}" if p_type == "DEPOSIT" else f"rej_wit_{p_id}_{p_ph}_{p_amt}"
                        kb = {
                            "inline_keyboard": [
                                [{"text": "✅ Approve", "callback_data": btn_code}, {"text": "❌ Reject", "callback_data": rej_code}],
                                [{"text": "🚫 Ban User", "callback_data": f"ban_user_{p_ph}"}]
                            ]
                        }
                        extra_info = f"\n📄 *SMS/Account:* `{p.get('sms_body') or p.get('account_number')}`" if (p.get('sms_body') or p.get('account_number')) else ""
                        send_telegram(
                            f"⏳ *የሚጠብቅ ጥያቄ ({p_type}):*\n👤 ስም: {p.get('first_name', 'User')}\n📞 ስልክ: `{p_ph}`\n💵 መጠን: `{p_amt}` ETB\n💳 መንገድ: *{p_method}*\n⏱ ቀን: `{ts}`{extra_info}",
                            reply_markup=kb
                        )
                return "OK", 200

            elif cmd == "/block" and len(parts) > 1:
                target_phone = format_phone_number(parts[1])
                blocked_phones.update_one({"phone": target_phone}, {"$set": {"phone": target_phone}}, upsert=True)
                send_telegram(f"✅ ስልክ ቁጥር ({target_phone}) ብሎክ ተደርጓል!")
                return "OK", 200

            elif cmd == "/unblock" and len(parts) > 1:
                target_phone = format_phone_number(parts[1])
                blocked_phones.delete_one({"phone": target_phone})
                send_telegram(f"✅ ስልክ ቁጥር ({target_phone}) ከብሎክ ተነስተዋል!")
                return "OK", 200

            elif cmd == "/add" and len(parts) > 2:
                target_ph = format_phone_number(parts[1])
                try:
                    amt = float(parts[2])
                    u = wallets.find_one_and_update({"phone": target_ph}, {"$inc": {"balance": amt}}, return_document=True, upsert=True)
                    if u:
                        send_telegram(f"✅ ለ `{target_ph}` ተጠቃሚ {amt} ETB ተጨምሯል። አዲስ ባላንስ፦ {u.get('balance')} ETB")
                        notify_user_balance_update(target_ph, u.get('balance', 0))
                except ValueError:
                    send_telegram("❌ ትክክለኛ የገንዘብ መጠን ያስገቡ!")
                return "OK", 200

            elif cmd == "/sub" and len(parts) > 2:
                target_ph = format_phone_number(parts[1])
                try:
                    amt = float(parts[2])
                    u = wallets.find_one_and_update({"phone": target_ph}, {"$inc": {"balance": -amt}}, return_document=True)
                    if u:
                        send_telegram(f"✅ ከ `{target_ph}` ተጠቃሚ ላይ {amt} ETB ተቀንሷል። አዲስ ባላንስ፦ {u.get('balance')} ETB")
                        notify_user_balance_update(target_ph, u.get('balance', 0))
                except ValueError:
                    send_telegram("❌ ትክክለኛ መጠን ያስገቡ!")
                return "OK", 200

            elif cmd == "/remove" and len(parts) > 1:
                target_val = sanitize_input(parts[1])
                res = wallets.delete_one({"$or": [{"phone": format_phone_number(target_val)}, {"chat_id": target_val}]})
                if res.deleted_count > 0:
                    send_telegram(f"🗑️ ተጠቃሚ (`{target_val}`) ከዳታቤዝ ሙሉ በሙሉ ተሰርዟል።")
                else:
                    send_telegram("❌ ተጠቃሚው አልተገኘም!")
                return "OK", 200

        if text.lower().startswith("/play") or text.lower().startswith("/start") or text.lower() == "start bot":
            user_exist, formatted_phone = get_user_wallet_and_phone(chat_id)
            if user_exist and formatted_phone:
                referred_by = user_exist.get("referred_by")
                play_keyboard = get_player_play_keyboard(formatted_phone, referred_by=referred_by)
                send_telegram_user(
                    chat_id,
                    f"🕹 *PLAY IN (ተጠቃሚ፦ `{formatted_phone}`):*\nጨዋታ ለመጀመር ከታች ካሉት ክፍሎች አንዱን ይምረጡ፦",
                    reply_markup=play_keyboard
                )
            else:
                contact_keyboard = {
                    "keyboard": [
                        [{"text": "📱 እባክዎን ስልክ ቁጥርዎን ያጋሩ (Share Phone Number)", "request_contact": True}]
                    ],
                    "resize_keyboard": True,
                    "one_time_keyboard": True
                }
                send_telegram_user(
                    chat_id,
                    "👋 እንኳን ወደ **Besh Bingo** በደህና መጡ!\n\n🎁 **ለአዲስ ተመዝጋቢዎች የ 20 ብር ነፃ መነሻ ቦነስ ተዘጋጅቷል!**\n\nእባክዎ ለመመዝገብ እና ቦነሱን ለመቀበል ከታች ያለውን የ **«📱 ስልክ ቁጥርዎን ያጋሩ»** ቁልፍ በመንካት ስልክ ቁጥርዎን ያጋሩ።",
                    reply_markup=contact_keyboard
                )
            return "OK", 200

    elif "callback_query" in data:
        cq = data["callback_query"]
        cq_id = cq["id"]
        chat_id = str(cq["message"]["chat"]["id"])
        message_id = cq["message"]["message_id"]
        msg_original_text = cq["message"].get("text", "")
        data_str = cq.get("data", "")
        
        answer_url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"

        # ቦነስ ጥያቄ
        if data_str == "Besh_bingo_bonus":
            requests.post(answer_url, json={"callback_query_id": cq_id, "text": "የቦነስ ፕሮግራም በቅርቡ ይጀመራል!", "show_alert": True})
            return "OK", 200

        # የተጠቃሚ ፈጣን አዝራሮች
        if data_str == "user_back_to_menu":
            u_doc, u_ph = get_user_wallet_and_phone(chat_id)
            requests.post(answer_url, json={"callback_query_id": cq_id})
            if u_ph:
                referred_by = u_doc.get("referred_by") if u_doc else None
                play_kb = get_player_play_keyboard(u_ph, referred_by=referred_by)
                edit_telegram_message(chat_id, message_id, f"🕹 *ዋና ማውጫ (ተጠቃሚ፦ `{u_ph}`)*\nከታች ካሉት አማራጮች አንዱን ይምረጡ፦", reply_markup=play_kb)
            return "OK", 200

        if data_str == "user_quick_deposit":
            requests.post(answer_url, json={"callback_query_id": cq_id})
            dep_keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "📱 TELE-BIRR (ቴሌብር)", "callback_data": "user_select_tele_dep"},
                        {"text": "🏦 CBE-BIRR (ሲቢኢ ብር)", "callback_data": "user_select_cbe_dep"}
                    ],
                    [
                        {"text": "🔙 ወደ ዋናው ማውጫ (Back)", "callback_data": "user_back_to_menu"}
                    ]
                ]
            }
            edit_telegram_message(chat_id, message_id, "📥 **የገንዘብ ማስገቢያ መንገድ ይምረጡ (Deposit Method):**", reply_markup=dep_keyboard)
            return "OK", 200

        if data_str == "user_quick_withdraw":
            requests.post(answer_url, json={"callback_query_id": cq_id})
            wit_keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "📱 TELE-BIRR (ቴሌብር)", "callback_data": "user_select_tele_wit"},
                        {"text": "🏦 CBE-BIRR (ሲቢኢ ብር)", "callback_data": "user_select_cbe_wit"}
                    ],
                    [
                        {"text": "🔙 ወደ ዋናው ማውጫ (Back)", "callback_data": "user_back_to_menu"}
                    ]
                ]
            }
            edit_telegram_message(chat_id, message_id, "📤 **የገንዘብ ማውጫ መንገድ ይምረጡ (Withdraw Method):**", reply_markup=wit_keyboard)
            return "OK", 200

        if data_str == "user_quick_balance":
            u_wallet = wallets.find_one({"chat_id": chat_id})
            bal = u_wallet.get("balance", 0) if u_wallet else 0
            requests.post(answer_url, json={"callback_query_id": cq_id, "text": f"💰 ቀሪ ባላንስዎ: {bal:,.2f} ETB", "show_alert": True})
            return "OK", 200

        if data_str == "user_quick_rules":
            rules_msg = (
                "ℹ️ **የጨዋታ ህጎች እና መመሪያዎች**\n\n"
                "1. እያንዳንዱ ካርቴላ 24 ቁጥሮች እና 1 ነጻ ቦታ አለው።\n"
                "2. ኳሶች ሲወጡ በካርቴላዎ ላይ ምልክት ያድርጉ።\n"
                "3. አግድም፣ ቁልቁል፣ ዲያጎናል ወይም 4 ማዕዘን የሞላ BINGO ብሎ ያሸንፋል!\n"
                "4. ማንኛውም ችግር ካለ @BeshBingoSupport ያነጋግሩ።"
            )
            requests.post(answer_url, json={"callback_query_id": cq_id})
            send_telegram_user(chat_id, rules_msg)
            return "OK", 200

        # ተጠቃሚው የዲፖዚት መንገድ ሲመርጥ
        if data_str in ["user_select_tele_dep", "user_select_cbe_dep"]:
            u_doc, u_ph = get_user_wallet_and_phone(chat_id)
            if not u_ph:
                requests.post(answer_url, json={"callback_query_id": cq_id})
                contact_kb = {
                    "keyboard": [
                        [{"text": "📱 ስልክ ቁጥርዎን ያጋሩ (Share Phone Number)", "request_contact": True}]
                    ],
                    "resize_keyboard": True,
                    "one_time_keyboard": True
                }
                send_telegram_user(
                    chat_id,
                    "📱 **እባክዎ መጀመሪያ ስልክ ቁጥርዎን ያጋሩ (Share Phone Number)!**\n\nዲፖዚት ለማድረግ ትክክለኛ ስልክ ቁጥርዎ በሲስተሙ ውስጥ መመዝገብ አለበት።",
                    reply_markup=contact_kb
                )
                return "OK", 200

            sel_method = "telebirr" if "tele" in data_str else "cbebirr"
            user_state.update_one(
                {"chat_id": chat_id}, 
                {"$set": {"action": "awaiting_deposit_sms", "method": sel_method}}, 
                upsert=True
            )
            
            if sel_method == "telebirr":
                tele_instructions = (
                    f"📱 **የ TELE-Birr አካውንት፦** `{TELEBIRR_ACCOUNT}` ({TELEBIRR_NAME})\n\n"
                    "📋 **መመሪያ፦**\n\n"
                    f"1. ከላይ ባለው የ TELE-Birr አካውንት ({TELEBIRR_ACCOUNT}) (ለግብይት ለመክፈል) ወይም (Send to-individual) በሚለው አማራጭ ገንዘቡን ያስገቡ።\n\n"
                    "2. ብሩን ስትልኩ የከፈላችሁበትን መረጃ የያዘ አጭር የጹሁፍ መልክት (SMS) ከ TELE-Birr ይደርሳችኋል።\n\n"
                    "3. የደረሳችሁን አጭር የጹሁፍ መልክት (SMS) ሙሉውን ኮፒ (Copy) በማድረግ ከታች ባለው የቴሌግራም የጹሁፍ ማስገቢያው ላይ ፔስት (Paste) በማድረግ ይላኩት።\n\n"
                    f"☎️ የሚያጋጥማችሁ የክፍያ ችግር ካለ {SUPPORT_USERNAME} በዚ ሰፖርት ማውራት ይችላሉ።"
                )
                send_telegram_user(chat_id, tele_instructions)
            else:
                cbe_instructions = (
                    f"🏦 **የ CBE-Birr አካውንት፦** `{CBEBIRR_ACCOUNT}` ({CBEBIRR_NAME})\n\n"
                    "📋 **መመሪያ፦**\n\n"
                    f"1. ከላይ ባለው የ CBE-Birr አካውንት ({CBEBIRR_ACCOUNT}) ገንዘቡን ያስገቡ።\n\n"
                    "2. ብሩን ስትልኩ የከፈላችሁበትን መረጃ የያዘ አጭር የጹሁፍ መልክት (SMS) ከ CBE-Birr ይደርሳችኋል።\n\n"
                    "3. የደረሳችሁን አጭር የጹሁፍ መልክት (SMS) ሙሉውን ኮፒ (Copy) በማድረግ ከታች ባለው የቴሌግራም የጹሁፍ ማስገቢያው ላይ ፔስት (Paste) በማድረግ ይላኩት።\n\n"
                    f"☎️ የሚያጋጥማችሁ የክፍያ ችግር ካለ {SUPPORT_USERNAME} በዚ ሰፖርት ማውራት ይችላሉ።"
                )
                send_telegram_user(chat_id, cbe_instructions)
                
            requests.post(answer_url, json={"callback_query_id": cq_id})
            return "OK", 200

        # ተጠቃሚው የዊዝድሮዋል መንገድ ሲመርጥ
        if data_str in ["user_select_tele_wit", "user_select_cbe_wit"]:
            u_doc, u_ph = get_user_wallet_and_phone(chat_id)
            if not u_ph:
                requests.post(answer_url, json={"callback_query_id": cq_id})
                contact_kb = {
                    "keyboard": [
                        [{"text": "📱 ስልክ ቁጥርዎን ያጋሩ (Share Phone Number)", "request_contact": True}]
                    ],
                    "resize_keyboard": True,
                    "one_time_keyboard": True
                }
                send_telegram_user(
                    chat_id,
                    "📱 **እባክዎ መጀመሪያ ስልክ ቁጥርዎን ያጋሩ (Share Phone Number)!**\n\nወጪ ለማድረግ (Withdraw) ትክክለኛ ስልክ ቁጥርዎ በሲስተሙ ውስጥ መመዝገብ አለበት።",
                    reply_markup=contact_kb
                )
                return "OK", 200

            sel_method = "telebirr" if "tele" in data_str else "cbebirr"
            user_state.update_one(
                {"chat_id": chat_id}, 
                {"$set": {"action": "awaiting_withdraw_details", "method": sel_method}}, 
                upsert=True
            )
            wit_guide = (
                f"📤 **ወጪ ለማድረግ (Withdraw - {sel_method.upper()})**\n"
                f"📞 የእርስዎ የተመዘገበ ስልክ፦ `{u_ph}`\n\n"
                "እባክዎ የሚወጣውን የብር መጠን፣ የአካውንት ቁጥርዎን እና ሙሉ ስምዎን በዚህ ፎርማት ይላኩ፦\n\n"
                "`<የብር መጠን> <የአካውንት ቁጥር> <ሙሉ ስም>`\n\n"
                "ምሳሌ፦\n"
                "`100 0960736061 Abebe Kebede`\n\n"
                "⚠️ *ማሳሰቢያ፦ የመነሻ ቦነስ 20 ብር ወጪ ማድረግ አይቻልም።*"
            )
            send_telegram_user(chat_id, wit_guide)
            requests.post(answer_url, json={"callback_query_id": cq_id})
            return "OK", 200

        # የአድሚን Action Handlers
        if chat_id == str(ADMIN_ID):
            now_action_str = (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")

            if data_str == "admin_refresh_dashboard":
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "🔄 ሰሌዳው ታድሷል!"})
                admin_keyboard = get_admin_dashboard_keyboard()
                edit_telegram_message(chat_id, message_id, "🛠 *የአድሚን ማኔጅመንት ሰሌዳ (Admin Workspace)*\nከታች ካሉት አማራጮች የሚፈልጉትን ይምረጡ፦", reply_markup=admin_keyboard)
                return "OK", 200

            if data_str.startswith("ban_user_"):
                b_phone = format_phone_number(data_str.replace("ban_user_", ""))
                blocked_phones.update_one({"phone": b_phone}, {"$set": {"phone": b_phone}}, upsert=True)
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": f"ስልክ ቁጥር {b_phone} ታግዷል!", "show_alert": True})
                
                new_admin_msg = msg_original_text + f"\n\n🚫 **STATUS: BANNED**\n⏱ ቀን: `{now_action_str}`"
                edit_telegram_message(chat_id, message_id, new_admin_msg, reply_markup={"inline_keyboard": []})
                return "OK", 200

            elif data_str == "admin_gen_agent_link":
                admin_state.update_one({"chat_id": ADMIN_ID}, {"$set": {"action": "awaiting_agent_phone"}}, upsert=True)
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "ስልክ ቁጥር ያስገቡ"})
                send_telegram("📱 እባክዎ ሊንክ ማመንጨት የሚፈልጉትን **የኤጀንት ስልክ ቁጥር** ያስገቡ፦\n(ምሳሌ፦ `0911223344`)")
                return "OK", 200

            elif data_str == "admin_agent_stats_guide":
                admin_state.update_one({"chat_id": ADMIN_ID}, {"$set": {"action": "awaiting_agent_stats_phone"}}, upsert=True)
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "የኤጀንት ስልክ ይጠይቃል"})
                send_telegram("👥 *የኤጀንት ስታቲስቲክስ ማየት*\n\nእባክዎ መረጃውን ማየት የሚፈልጉትን **የኤጀንት ስልክ ቁጥር** ያስገቡ፦\n(ምሳሌ፦ `0911223344`)")
                return "OK", 200

            elif data_str == "admin_financial_stats":
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "ስታቲስቲክስ ተዘጋጅቷል"})
                send_telegram(get_financial_stats())
                return "OK", 200

            elif data_str == "admin_pending_req":
                pendings = list(transactions.find({"status": "pending"}).limit(10))
                if not pendings:
                    requests.post(answer_url, json={"callback_query_id": cq_id, "text": "ምንም የሚጠብቅ ጥያቄ የለም!", "show_alert": True})
                else:
                    requests.post(answer_url, json={"callback_query_id": cq_id, "text": f"{len(pendings)} ጥያቄዎች ተገኝተዋል"})
                    for p in pendings:
                        p_id = str(p.get("_id"))
                        p_type = p.get("type", "deposit").upper()
                        p_ph = p.get("phone")
                        p_amt = p.get("amount", 0)
                        p_method = p.get("method", "telebirr").upper()
                        ts = (p.get("timestamp", datetime.utcnow()) + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
                        
                        btn_code = f"app_dep_{p_id}_{p_ph}_{p_amt}" if p_type == "DEPOSIT" else f"app_wit_{p_id}_{p_ph}_{p_amt}"
                        rej_code = f"rej_dep_{p_id}_{p_ph}_{p_amt}" if p_type == "DEPOSIT" else f"rej_wit_{p_id}_{p_ph}_{p_amt}"
                        kb = {
                            "inline_keyboard": [
                                [{"text": "✅ Approve", "callback_data": btn_code}, {"text": "❌ Reject", "callback_data": rej_code}],
                                [{"text": "🚫 Ban User", "callback_data": f"ban_user_{p_ph}"}]
                            ]
                        }
                        extra_info = f"\n📄 *SMS/Account:* `{p.get('sms_body') or p.get('account_number')}`" if (p.get('sms_body') or p.get('account_number')) else ""
                        send_telegram(
                            f"⏳ *የሚጠብቅ ጥያቄ ({p_type}):*\n👤 ስም: {p.get('first_name', 'User')}\n📞 ስልክ: `{p_ph}`\n💵 መጠን: `{p_amt}` ETB\n💳 መንገድ: *{p_method}*\n⏱ ቀን: `{ts}`{extra_info}",
                            reply_markup=kb
                        )
                return "OK", 200

            elif data_str == "admin_show_all_bal":
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "የተጠቃሚዎች ዝርዝር እየተዘጋጀ ነው..."})
                all_wallets = list(wallets.find().sort("balance", -1).limit(25))
                total_sys_bal = sum([w.get("balance", 0) for w in wallets.find()])
                total_users = wallets.count_documents({})

                if not all_wallets:
                    send_telegram("📭 እስካሁን የተመዘገበ ተጠቃሚ የለም።")
                else:
                    report = f"📋 *የተጠቃሚዎች ባላንስ ዝርዝር (Top 25)*\n👥 ጠቅላላ ተጠቃሚዎች: *{total_users}*\n💰 ጠቅላላ በሲስተሙ ያለ ባላንስ: `*{total_sys_bal:,.2f} ETB*`\n\n"
                    for idx, w in enumerate(all_wallets, 1):
                        ph = w.get("phone", "N/A")
                        uname = w.get("username") or w.get("first_name") or "User"
                        bal = w.get("balance", 0)
                        report += f"{idx}. 👤 {uname}\n   📞 `{ph}` ➔ 💵 `*{bal:,.2f} ETB*`\n"
                    send_telegram(report)
                return "OK", 200

            elif data_str == "admin_history_req":
                admin_state.update_one({"chat_id": ADMIN_ID}, {"$set": {"action": "awaiting_history_dates"}}, upsert=True)
                requests.post(answer_url, json={"callback_query_id": cq_id})
                hist_kb = {
                    "inline_keyboard": [
                        [
                            {"text": "📅 የዛሬ ዲፖዚቶች", "callback_data": "quick_hist_today_dep"},
                            {"text": "📅 የዛሬ ዊዝድሮዎች", "callback_data": "quick_hist_today_wit"}
                        ],
                        [
                            {"text": "🗓 የሳምንቱ ታሪክ", "callback_data": "quick_hist_week_all"}
                        ]
                    ]
                }
                send_telegram(
                    "📜 *የትራንዛክሽን ታሪክ ፍለጋ*\n\n"
                    "ቀን እና አይነት በመጻፍ ለመፈለግ:\n`YYYY-MM-DD YYYY-MM-DD deposit`\n\n"
                    "ወይም ከታች ካሉት ፈጣን አዝራሮች ይምረጡ፦",
                    reply_markup=hist_kb
                )
                return "OK", 200

            elif data_str in ["quick_hist_today_dep", "quick_hist_today_wit", "quick_hist_week_all"]:
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "ታሪክ እየተጫነ ነው..."})
                now = datetime.utcnow()
                if "today" in data_str:
                    start_dt = datetime(now.year, now.month, now.day)
                    tx_type = "deposit" if "dep" in data_str else "withdrawal"
                    txs = list(transactions.find({"type": tx_type, "timestamp": {"$gte": start_dt}}).sort("timestamp", -1).limit(15))
                    title = f"የዛሬ {tx_type.upper()}"
                else:
                    start_dt = now - timedelta(days=7)
                    txs = list(transactions.find({"timestamp": {"$gte": start_dt}}).sort("timestamp", -1).limit(15))
                    title = "የባለፉት 7 ቀናት ሁሉም"

                if not txs:
                    send_telegram(f"📭 ምንም አይነት የ {title} ታሪክ አልተገኘም።")
                else:
                    rep = f"📜 *የ {title} ታሪክ (የቅርብ 15):*\n\n"
                    for t in txs:
                        amt = t.get("amount", 0)
                        st = t.get("status", "N/A").upper()
                        ph = t.get("phone", "N/A")
                        ttype = t.get("type", "N/A").upper()
                        ts = (t.get("timestamp", datetime.utcnow()) + timedelta(hours=3)).strftime("%m-%d %H:%M")
                        rep += f"⏱ `{ts}` | 📞 `{ph}`\n🏷 {ttype} | 💵 `{amt}` ETB | Status: *{st}*\n--------------------\n"
                    send_telegram(rep)
                return "OK", 200

            elif data_str == "guide_add":
                admin_state.update_one({"chat_id": ADMIN_ID}, {"$set": {"action": "awaiting_add_balance"}}, upsert=True)
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "የሚጨመረውን ስልክ እና መጠን ያስገቡ"})
                send_telegram(
                    "➕ *ባላንስ ለመጨመር (Add Balance)*\n\n"
                    "እባክዎ የተጠቃሚውን ስልክ እና የብር መጠን በዚህ ፎርማት ይላኩ፦\n"
                    "`<ስልክ_ቁጥር> <መጠን>`\n\n"
                    "ምሳሌ፦\n`0911223344 100`"
                )
                return "OK", 200

            elif data_str == "guide_sub":
                admin_state.update_one({"chat_id": ADMIN_ID}, {"$set": {"action": "awaiting_sub_balance"}}, upsert=True)
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "የሚቀነሰውን ስልክ እና መጠን ያስገቡ"})
                send_telegram(
                    "➖ *ባላንስ ለመቀነስ (Subtract Balance)*\n\n"
                    "እባክዎ የተጠቃሚውን ስልክ እና የብር መጠን በዚህ ፎርማት ይላኩ፦\n"
                    "`<ስልክ_ቁጥር> <መጠን>`\n\n"
                    "ምሳሌ፦\n`0911223344 50`"
                )
                return "OK", 200

            elif data_str == "guide_broadcast":
                admin_state.update_one({"chat_id": ADMIN_ID}, {"$set": {"action": "awaiting_broadcast"}}, upsert=True)
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "መልእክቱን ይላኩ"})
                send_telegram("📢 *መልእክት ለሁሉም ተጠቃሚዎች ለማስተላለፍ*\n\nእባክዎ ማስተላለፍ የሚፈልጉትን ጽሁፍ ወይም ፎቶ አሁን ይላኩ፦")
                return "OK", 200

            elif data_str == "guide_block_remove":
                requests.post(answer_url, json={"callback_query_id": cq_id})
                send_telegram(
                    "🚫 *ተጠቃሚን የማገድ እና የመሰረዝ መመሪያ*\n\n"
                    "1️⃣ ተጠቃሚ ለማገድ (Block):\n`/block 0911223344`\n\n"
                    "2️⃣ ከእገዳ ለማንሳት (Unblock):\n`/unblock 0911223344`\n\n"
                    "3️⃣ አካውንት ሙሉ ለሙሉ ለመሰረዝ (Delete):\n`/remove 0911223344`"
                )
                return "OK", 200

            elif data_str.startswith("app_dep_"):
                parts = data_str.split("_")
                tx_id_str = parts[2]
                phone_num = format_phone_number(parts[3])
                amt = float(parts[4])
                
                tx_updated = transactions.find_one_and_update(
                    {"_id": ObjectId(tx_id_str), "status": "pending"},
                    {"$set": {"status": "approved", "processed_at": datetime.utcnow()}}
                )
                if tx_updated:
                    updated_wallet = wallets.find_one_and_update(
                        {"phone": phone_num}, 
                        {"$inc": {"balance": amt}}, 
                        return_document=True, 
                        upsert=True
                    )
                    new_bal = updated_wallet.get("balance", 0) if updated_wallet else 0
                    notify_user_balance_update(phone_num, new_bal)
                    
                    user_chat = tx_updated.get("chat_id")
                    if user_chat:
                        dep_success_msg = (
                            "✅ **የዲፖዚት ጥያቄዎ ፀድቋል! (Deposit Approved)**\n\n"
                            f"💵 የተጨመረው ብር፦ `+{amt:,.2f} ETB`\n"
                            f"💳 መንገድ፦ *{tx_updated.get('method', 'telebirr').upper()}*\n"
                            f"📅 ቀን፦ `{now_action_str}`\n"
                            f"💰 አጠቃላይ አዲሱ ቀሪ ሂሳብዎ፦ `*{new_bal:,.2f} ETB*`"
                        )
                        send_telegram_user(user_chat, dep_success_msg)

                    requests.post(answer_url, json={"callback_query_id": cq_id, "text": f"✅ Approved! {amt} ETB ተጨምሯል።"})
                    
                    new_admin_msg = msg_original_text + f"\n\n══════════════════\n✅ **STATUS: APPROVED / SUCCESS**\n💵 የተጨመረ: `{amt}` ETB\n💰 አዲስ ባላንስ: `{new_bal:,.2f} ETB`\n⏱ ቀን: `{now_action_str}`"
                    edit_telegram_message(chat_id, message_id, new_admin_msg, reply_markup={"inline_keyboard": []})
                else:
                    requests.post(answer_url, json={"callback_query_id": cq_id, "text": "⚠️ ይህ ጥያቄ አስቀድሞ ተስተናግዷል!", "show_alert": True})
                    edit_telegram_message(chat_id, message_id, msg_original_text + "\n\n⚠️ *አስቀድሞ ተጠናቋል!*", reply_markup={"inline_keyboard": []})
                return "OK", 200

            elif data_str.startswith("rej_dep_"):
                parts = data_str.split("_")
                tx_id_str = parts[2]
                phone_num = format_phone_number(parts[3])
                
                tx_updated = transactions.find_one_and_update(
                    {"_id": ObjectId(tx_id_str), "status": "pending"},
                    {"$set": {"status": "rejected", "processed_at": datetime.utcnow()}}
                )
                if tx_updated:
                    user_chat = tx_updated.get("chat_id")
                    if user_chat:
                        rej_msg = (
                            "❌ **የዲፖዚት ጥያቄዎ ውድቅ ተደርጓል (Rejected)**\n\n"
                            "ያስገቡት የክፍያ መረጃ ትክክል አይደለም። እባክዎ ትክክለኛውን የክፍያ SMS በመጠቀም እንደገና ይሞክሩ "
                            f"ወይም {SUPPORT_USERNAME} ያነጋግሩ።"
                        )
                        send_telegram_user(user_chat, rej_msg)

                    requests.post(answer_url, json={"callback_query_id": cq_id, "text": "❌ ጥያቄው ውድቅ ተደርጓል!"})
                    
                    new_admin_msg = msg_original_text + f"\n\n══════════════════\n❌ **STATUS: REJECTED**\n⏱ ቀን: `{now_action_str}`"
                    edit_telegram_message(chat_id, message_id, new_admin_msg, reply_markup={"inline_keyboard": []})
                else:
                    requests.post(answer_url, json={"callback_query_id": cq_id, "text": "⚠️ ይህ ጥያቄ አስቀድሞ ተስተናግዷል!", "show_alert": True})
                    edit_telegram_message(chat_id, message_id, msg_original_text + "\n\n⚠️ *አስቀድሞ ተጠናቋል!*", reply_markup={"inline_keyboard": []})
                return "OK", 200

            elif data_str.startswith("app_wit_"):
                parts = data_str.split("_")
                tx_id_str = parts[2]
                phone_num = format_phone_number(parts[3])
                amt = float(parts[4])

                tx_updated = transactions.find_one_and_update(
                    {"_id": ObjectId(tx_id_str), "status": "pending"},
                    {"$set": {"status": "approved", "processed_at": datetime.utcnow()}}
                )
                if tx_updated:
                    user_wallet = wallets.find_one({"phone": phone_num})
                    curr_bal = user_wallet.get("balance", 0.0) if user_wallet else 0.0
                    
                    user_chat = tx_updated.get("chat_id")
                    if user_chat:
                        wit_success_msg = (
                            "✅ **የዊዝድሮዋል ጥያቄዎ ተሳክቷል! (Withdraw Approved)**\n\n"
                            f"💵 የተላከው ገንዘብ፦ `-{amt:,.2f} ETB`\n"
                            f"💳 መንገድ፦ *{tx_updated.get('method', 'telebirr').upper()}*\n"
                            f"🏦 አካውንት፦ `{tx_updated.get('account_number')}` ({tx_updated.get('account_name')})\n"
                            f"📅 ቀን፦ `{now_action_str}`\n"
                            f"💰 ቀሪ ሂሳብዎ፦ `*{curr_bal:,.2f} ETB*`"
                        )
                        send_telegram_user(user_chat, wit_success_msg)

                    requests.post(answer_url, json={"callback_query_id": cq_id, "text": "✅ Withdraw Approved!"})
                    
                    new_admin_msg = msg_original_text + f"\n\n══════════════════\n✅ **STATUS: APPROVED / SUCCESS**\n💵 የተላለፈ: `{amt}` ETB\n⏱ ቀን: `{now_action_str}`"
                    edit_telegram_message(chat_id, message_id, new_admin_msg, reply_markup={"inline_keyboard": []})
                else:
                    requests.post(answer_url, json={"callback_query_id": cq_id, "text": "⚠️ ይህ ጥያቄ አስቀድሞ ተስተናግዷል!", "show_alert": True})
                    edit_telegram_message(chat_id, message_id, msg_original_text + "\n\n⚠️ *አስቀድሞ ተጠናቋል!*", reply_markup={"inline_keyboard": []})
                return "OK", 200

            elif data_str.startswith("rej_wit_"):
                parts = data_str.split("_")
                tx_id_str = parts[2]
                phone_num = format_phone_number(parts[3])
                amt = float(parts[4])

                tx_updated = transactions.find_one_and_update(
                    {"_id": ObjectId(tx_id_str), "status": "pending"},
                    {"$set": {"status": "rejected", "processed_at": datetime.utcnow()}}
                )
                if tx_updated:
                    refunded_wallet = wallets.find_one_and_update(
                        {"phone": phone_num}, 
                        {"$inc": {"balance": amt}}, 
                        return_document=True
                    )
                    new_bal = refunded_wallet.get("balance", 0.0) if refunded_wallet else 0.0
                    notify_user_balance_update(phone_num, new_bal)

                    user_chat = tx_updated.get("chat_id")
                    if user_chat:
                        rej_msg = (
                            "❌ **የዊዝድሮዋል ጥያቄዎ ውድቅ ተደርጓል (Withdraw Rejected)**\n\n"
                            f"ያመለከቱት `{amt:,.2f} ETB` ተመልሶ ወደ ቀሪ ሂሳብዎ ገብቷል!\n"
                            f"💰 አጠቃላይ ቀሪ ሂሳብዎ፦ `*{new_bal:,.2f} ETB*`\n\n"
                            f"እባክዎ መረጃዎን አስተካክለው እንደገና ይሞክሩ ወይም {SUPPORT_USERNAME} ያነጋግሩ።"
                        )
                        send_telegram_user(user_chat, rej_msg)

                    requests.post(answer_url, json={"callback_query_id": cq_id, "text": f"❌ Rejected! {amt} ETB ወደ ተጠቃሚው ተመልሷል።"})
                    
                    new_admin_msg = msg_original_text + f"\n\n══════════════════\n❌ **STATUS: REJECTED (REFUNDED)**\n💵 የተመለሰ: `{amt}` ETB\n⏱ ቀን: `{now_action_str}`"
                    edit_telegram_message(chat_id, message_id, new_admin_msg, reply_markup={"inline_keyboard": []})
                else:
                    requests.post(answer_url, json={"callback_query_id": cq_id, "text": "⚠️ ይህ ጥያቄ አስቀድሞ ተስተናግዷል!", "show_alert": True})
                    edit_telegram_message(chat_id, message_id, msg_original_text + "\n\n⚠️ *አስቀድሞ ተጠናቋል!*", reply_markup={"inline_keyboard": []})
                return "OK", 200

    return "OK", 200

@socketio.on('connect')
def handle_connect():
    global loop_started
    if not loop_started:
        loop_started = True
        set_webhook()
        set_bot_commands()
        socketio.start_background_task(lambda: run_game_loop("10"))
        socketio.start_background_task(lambda: run_game_loop("20"))
        socketio.start_background_task(lambda: run_game_loop("50"))
        socketio.start_background_task(lambda: run_game_loop("100"))
        socketio.start_background_task(lambda: run_game_loop("200"))
        socketio.start_background_task(lambda: run_game_loop("500"))
        socketio.start_background_task(super_game_loop)
        
    broadcast_game_state("10")
    broadcast_game_state("20")
    broadcast_game_state("50")
    broadcast_game_state("100")
    broadcast_game_state("200")
    broadcast_game_state("500")
    broadcast_game_state("super")

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
