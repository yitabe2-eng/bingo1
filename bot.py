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

def get_user_wallet_and_phone(chat_id_val):
    if not chat_id_val:
        return None, None
    chat_str = str(chat_id_val)
    user = wallets.find_one({"$or": [{"chat_id": chat_str}, {"telegram_id": chat_str}]})
    if user:
        ph = user.get("phone", "")
        if is_valid_ethiopian_phone(ph):
            return user, format_phone_number(ph)
    try:
        chat_int = int(chat_id_val)
        user_int = wallets.find_one({"$or": [{"chat_id": chat_int}, {"telegram_id": chat_int}]})
        if user_int:
            ph = user_int.get("phone", "")
            if is_valid_ethiopian_phone(ph):
                return user_int, format_phone_number(ph)
    except:
        pass
    return user, None

def check_rate_limit(phone_number):
    five_minutes_ago = datetime.utcnow() - timedelta(minutes=5)
    recent_requests_count = transactions.count_documents({
        "phone": phone_number,
        "type": {"$in": ["deposit", "withdrawal"]},
        "timestamp": {"$gte": five_minutes_ago}
    })
    return recent_requests_count < 2

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
            [{"text": pending_btn_txt, "callback_data": "admin_pending_req"}, {"text": "📅 የእለት/የሳምንት ገቢ (20%)", "callback_data": "admin_financial_stats"}],
            [{"text": "🔗 የኤጀንት ሊንክ (Gen Link)", "callback_data": "admin_gen_agent_link"}, {"text": "👥 የኤጀንት ስታቲስቲክስ", "callback_data": "admin_agent_stats_guide"}],
            [{"text": "📋 የሁሉም ተጠቃሚዎች ባላንስ", "callback_data": "admin_show_all_bal"}, {"text": "📜 የትራንዛክሽን ታሪክ (ፈልግ)", "callback_data": "admin_history_req"}],
            [{"text": "➕ ባላንስ ጨምር (/add)", "callback_data": "guide_add"}, {"text": "➖ ባላንስ ቀንስ (/sub)", "callback_data": "guide_sub"}],
            [{"text": "📢 መልእክት አስላልፍ (/broadcast)", "callback_data": "guide_broadcast"}, {"text": "🚫 ብሎክ / ማጥፊያ", "callback_data": "guide_block_remove"}],
            [{"text": "🔄 ሰሌዳውን አድስ (Refresh)", "callback_data": "admin_refresh_dashboard"}]
        ]
    }

def get_player_play_keyboard(phone, referred_by=None):
    u_10 = f"{WEB_APP_URL}/10?phone={phone}"
    u_20 = f"{WEB_APP_URL}/20?phone={phone}"
    u_50 = f"{WEB_APP_URL}/50?phone={phone}"
    u_100 = f"{WEB_APP_URL}/100?phone={phone}"
    u_200 = f"{WEB_APP_URL}/200?phone={phone}"
    u_super = f"{WEB_APP_URL}/superbesh"
    if referred_by:
        u_super += f"?ref={referred_by}"
    else:
        u_super += f"?phone={phone}"

    return {
        "inline_keyboard": [
            [{"text": "🎮 PLAY | በ10 ብር", "web_app": {"url": u_10}}, {"text": "🎮 PLAY | በ20 ብር", "web_app": {"url": u_20}}],
            [{"text": "🎮 PLAY | በ50 ብር", "web_app": {"url": u_50}}, {"text": "🔥 PLAY | በ100 ብር", "web_app": {"url": u_100}}],
            [{"text": "💎 PLAY | በ200 ብር", "web_app": {"url": u_200}}, {"text": "🏆 BeshBingo (ሳምንታዊ) | 50 ብር", "web_app": {"url": u_super}}],
            [{"text": "⚽ BeshBingo Bonus", "callback_data": "Besh_bingo_bonus"}]
        ]
    }

game_states = {
    "10": {"status": "lobby", "timer": 30, "ball_timer": 6, "pot": 0, "players": {}, "sold_tickets": {}, "current_ball": "--", "drawn_balls": [], "winner": None, "winning_card": None, "winning_ticket_num": None, "winning_indices": None, "winning_line_name": None, "all_cards": {}},
    "20": {"status": "lobby", "timer": 30, "ball_timer": 6, "pot": 0, "players": {}, "sold_tickets": {}, "current_ball": "--", "drawn_balls": [], "winner": None, "winning_card": None, "winning_ticket_num": None, "winning_indices": None, "winning_line_name": None, "all_cards": {}},
    "50": {"status": "lobby", "timer": 30, "ball_timer": 6, "pot": 0, "players": {}, "sold_tickets": {}, "current_ball": "--", "drawn_balls": [], "winner": None, "winning_card": None, "winning_ticket_num": None, "winning_indices": None, "winning_line_name": None, "all_cards": {}},
    "100": {"status": "lobby", "timer": 30, "ball_timer": 6, "pot": 0, "players": {}, "sold_tickets": {}, "current_ball": "--", "drawn_balls": [], "winner": None, "winning_card": None, "winning_ticket_num": None, "winning_indices": None, "winning_line_name": None, "all_cards": {}},
    "200": {"status": "lobby", "timer": 30, "ball_timer": 6, "pot": 0, "players": {}, "sold_tickets": {}, "current_ball": "--", "drawn_balls": [], "winner": None, "winning_card": None, "winning_ticket_num": None, "winning_indices": None, "winning_line_name": None, "all_cards": {}},
    "super": {"status": "lobby", "timer": 30, "ball_timer": 6, "pot": 0, "players": {}, "sold_tickets": {}, "current_ball": "--", "drawn_balls": [], "winner": None, "winning_card": None, "winning_ticket_num": None, "winning_indices": None, "winning_line_name": None, "all_cards": {}}
}

ROOM_PRICES = {"10": 10, "20": 20, "50": 50, "100": 100, "200": 200, "super": 50}

loop_started = False
reset_task_references = {"10": None, "20": None, "50": None, "100": None, "200": None, "super": None}
pending_claims = {"10": [], "20": [], "50": [], "100": [], "200": [], "super": []}
claim_lock_active = {"10": False, "20": False, "50": False, "100": False, "200": False, "super": False}

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
    payload = {"chat_id": chat_id, "message_id": message_id, "text": new_text, "parse_mode": "Markdown"}
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

    today_game_comm = list(transactions.aggregate([{"$match": {"type": "game_commission", "timestamp": {"$gte": today_start}}}, {"$group": {"_id": None, "total": {"$sum": "$amount"}}}]))
    week_game_comm = list(transactions.aggregate([{"$match": {"type": "game_commission", "timestamp": {"$gte": week_start}}}, {"$group": {"_id": None, "total": {"$sum": "$amount"}}}]))
    today_wit = list(transactions.aggregate([{"$match": {"type": "withdrawal", "status": "approved", "timestamp": {"$gte": today_start}}}, {"$group": {"_id": None, "total": {"$sum": "$amount"}}}]))
    week_wit = list(transactions.aggregate([{"$match": {"type": "withdrawal", "status": "approved", "timestamp": {"$gte": week_start}}}, {"$group": {"_id": None, "total": {"$sum": "$amount"}}}]))

    t_comm = today_game_comm[0]["total"] if today_game_comm else 0.0
    w_comm = week_game_comm[0]["total"] if week_game_comm else 0.0
    t_out = today_wit[0]["total"] if today_wit else 0.0
    w_out = week_wit[0]["total"] if week_wit else 0.0

    today_profit = t_comm - t_out
    week_profit = w_comm - w_out

    return (
        f"📊 *የፋይናንስ ስታቲስቲክስ ሪፖርት*\n\n"
        f"📅 *የዛሬው ውሎ (Daily):*\n 🎮 ከጨዋታ የተገኘ (20%): `{t_comm:,.2f} ETB`\n 📤 የወጣ ወጪ: `{t_out:,.2f} ETB`\n 💰 *የእለቱ የተጣራ ትርፍ:* `{today_profit:,.2f} ETB`\n\n"
        f"🗓 *የባለፉት 7 ቀናት (Weekly):*\n 🎮 ከጨዋታ የተገኘ (20%): `{w_comm:,.2f} ETB`\n 📤 የወጣ ወጪ: `{w_out:,.2f} ETB`\n 💰 *የሳምንቱ የተጣራ ትርፍ:* `{week_profit:,.2f} ETB`"
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
    
    default_ball_timer = 6
    game_states[room_type].update({
        "status": "lobby", "winner": None, "winning_card": None, "winning_ticket_num": None, 
        "winning_indices": None, "winning_line_name": None, "pot": 0, "players": {}, 
        "sold_tickets": {}, "drawn_balls": [], "current_ball": "--", "timer": 30, "ball_timer": default_ball_timer, "all_cards": {}
    })
    broadcast_game_state(room_type) 

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
                state["ball_timer"] = 6
                shuffled = balls.copy()
                random.shuffle(shuffled)
                broadcast_game_state(room_type)
            else:
                state["timer"] = 30
                broadcast_game_state(room_type)
                continue

            if shuffled:
                for j in range(5, -1, -1):
                    if state["status"] != "playing":
                        break
                    state["ball_timer"] = j
                    broadcast_game_state(room_type) 
                    socketio.sleep(1)

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
                    socketio.sleep(6) 
            
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
                state["ball_timer"] = 6
                shuffled = balls.copy()
                random.shuffle(shuffled)
                broadcast_game_state("super")
            else:
                state["timer"] = 30
                broadcast_game_state("super")
                continue

            if shuffled:
                for j in range(5, -1, -1):
                    if state["status"] != "playing":
                        break
                    state["ball_timer"] = j
                    broadcast_game_state("super") 
                    socketio.sleep(1)

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
                    socketio.sleep(6) 
            
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

@app.route('/')
@app.route('/10')
@app.route('/ten')
def index_10(): 
    return render_template('index.html')

@app.route('/20')
@app.route('/twenty')
def index_20():
    return render_template('index2.html')

@app.route('/50')
@app.route('/fifty')
@app.route('/super')
def index_50():
    return render_template('index1.html')

@app.route('/100')
@app.route('/hundred')
def index_100():
    return render_template('index4.html')

@app.route('/200')
@app.route('/twohundred')
def index_200():
    return render_template('index5.html')

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
    if not user:
        return jsonify({"success": False, "user_exists": False, "msg": "መለያዎ ከሲስተሙ ተሰርዟል! እባክዎ እንደገና ይመዝገቡ።"})
        
    db_phone = user['phone']
    p_data = state["players"].get(db_phone, {"cards": {}})
    cards_list = list(p_data["cards"].values())
    clean_players = {k: {"username": v.get("username", ""), "cards": list(v.get("cards", {}).values())} for k, v in state["players"].items()}
    
    is_waiting = False
    if room == "super" and state["status"] in ["playing", "result"] and db_phone not in state["players"]:
        is_waiting = True

    return jsonify({
        "success": True, "user_exists": True, "status": state["status"], "timer": state["timer"],
        "ball_timer": state["ball_timer"], "pot": state["pot"], "sold_tickets": state["sold_tickets"],
        "current_ball": state["current_ball"], "drawn_balls": state["drawn_balls"], "winner": state["winner"],
        "winning_card": state["winning_card"], "winning_ticket_num": state["winning_ticket_num"],
        "winning_indices": state.get("winning_indices"), "winning_line_name": state.get("winning_line_name"),
        "all_cards": state.get("all_cards", {}), "players": clean_players, "balance": user.get('balance', 0), 
        "my_cards": cards_list, "active_players": len(state["players"]), "is_waiting": is_waiting
    })

@app.route('/buy_specific_ticket', methods=['POST'])
def buy_ticket():
    d = request.json or {}
    ph, t_num, uname = sanitize_input(d.get('phone')), str(d.get('ticket_num')), sanitize_input(d.get('username'))
    room = str(d.get('room', '10'))
    formatted_ph = format_phone_number(ph)
    
    for r_key, r_state in game_states.items():
        if r_key != room and formatted_ph in r_state["players"]:
            return jsonify({"success": False, "msg": f"አንድ ተጨዋች በአንድ ግዜ አንድ room ብቻ ነው መጨዋት ያለበት!"})

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
        
    # Rule: 4 ቁጥሮች ከተጠሩ በኋላ ማቅለም አይቻልም (Max 4 drawn balls check for claim)
    if len(current_drawn_balls) <= 4:
        return jsonify({"success": False, "msg": "አራት ቁጥሮች ከተጠሩ በኋላ ማቅለም አይቻልም!"})

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
            return jsonify({"success": False, "msg": "ቢንጎ ሙሉ በሙሉ አልሞላም!"})
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

                # Design: አንድ ሰዉ ሲያሸንፍ has ሁለት ena keziya belay ሲሆኑ have
                if num_winners == 1:
                    winner_display = f"{pending_claims[room][0]['username']} has won"
                else:
                    winner_display = f"{pending_claims[room][0]['username']} & 2 other have won" if num_winners > 2 else f"{pending_claims[room][0]['username']} & 1 other have won"

                state["winner"] = winner_display
                state["winning_card"] = pending_claims[room][0]["card"]  
                state["winning_ticket_num"] = pending_claims[room][0]["ticket_num"] 
                state["winning_indices"] = pending_claims[room][0]["indices"]
                state["winning_line_name"] = pending_claims[room][0]["line_name"] 

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
                        
                        success_msg = f"🏆 *WINNER (Room {room})!* \n👤 Name: {w['username']} has won | 📞 Phone: `{w['phone']}` | 🎫 Ticket: {w['ticket_num']} \n🎯 Winning Ball: {w['winning_ball']} \n💰 Prize Won: {total_prize:.2f} ETB{agent_info}"
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
                        
                        success_msg = f"🏆 *WINNERS (Shared Prize on Ball {pending_claims[room][0]['winning_ball']})!* \n💰 Total Pot Share: {share_prize:.2f} ETB each ({num_winners} winners) have won{agent_info}\n" + "\n".join(winner_texts)
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

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json or {}
    
    if "message" in data:
        msg = data["message"]
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "")
        first_name = msg.get("from", {}).get("first_name", "User")
        
        if chat_id != str(ADMIN_ID):
            wallets.update_one({"chat_id": chat_id}, {"$set": {"chat_id": chat_id}}, upsert=False)
        
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
                        "phone": formatted_phone, "telegram_id": str(telegram_id), "chat_id": str(chat_id),
                        "first_name": first_name, "username": username if username else first_name,
                        "status": "ACTIVE", "updated_at": datetime.utcnow()
                    },
                    "$setOnInsert": {"balance": 20.00, "created_at": datetime.utcnow()}
                },
                upsert=True
            )
            
            reg_success_msg = (
                "✅ **ምዝገባዎ በተሳካ ሁኔታ ተጠናቋል!**\n\n"
                f"📞 የተመዘገበ ስልክ ቁጥር፦ `{formatted_phone}`\n"
                "🎁 የ 20.00 ETB መነሻ ቀሪ ሂሳብ ተሰርዟል/ተሰጥቶዎታል። (ቦነስ withdrawn ማድረግ አይቻልም)\n\n"
                "አሁን ዲፖዚት ማድረግ (/deposit) ወይም ከታች ካሉት ክፍሎች መርጠው መጫወት ይችላሉ፦"
            )
            send_telegram_user(chat_id, reg_success_msg, reply_markup={"remove_keyboard": True})

            play_keyboard = get_player_play_keyboard(formatted_phone)
            send_telegram_user(chat_id, "🕹 *PLAY IN:*\nጨዋታ ለመጀመር ከታች ካሉት ክፍሎች አንዱን ይምረጡ፦", reply_markup=play_keyboard)
            return "OK", 200

        u_state = user_state.find_one({"chat_id": chat_id})
        
        if u_state and u_state.get("action") == "awaiting_deposit_sms":
            if text.startswith("/"):
                user_state.delete_one({"chat_id": chat_id})
            else:
                user_wallet, user_phone = get_user_wallet_and_phone(chat_id)
                if not user_phone:
                    user_state.delete_one({"chat_id": chat_id})
                    return "OK", 200

                if not check_rate_limit(user_phone):
                    send_telegram_user(chat_id, "⚠️ **ይቅርታ!** በ 5 ደቂቃ ውስጥ 2 ጊዜ ብቻ ጥያቄ መላክ ይቻላል።")
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
                    "phone": user_phone, "chat_id": chat_id, "first_name": first_name,
                    "type": "deposit", "method": method, "amount": extracted_amt,
                    "sms_body": text, "status": "pending", "timestamp": datetime.utcnow()
                })
                tx_id = str(tx_insert.inserted_id)
                now_str = (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")

                send_telegram_user(chat_id, "⏳ የዲፖዚት ጥያቄዎ ደርሷል!")
                admin_notif = f"📥 *አዲስ ዲፖዚት*\n📞 `{user_phone}`\n💵 `{extracted_amt}` ETB\n📄 `{text}`"
                admin_kb = {
                    "inline_keyboard": [
                        [{"text": "✅ Approve", "callback_data": f"app_dep_{tx_id}_{user_phone}_{extracted_amt}"}, {"text": "❌ Reject", "callback_data": f"rej_dep_{tx_id}_{user_phone}_{extracted_amt}"}],
                        [{"text": "🚫 Ban", "callback_data": f"ban_user_{user_phone}"}]
                    ]
                }
                send_telegram(admin_notif, reply_markup=admin_kb)
                return "OK", 200

        if u_state and u_state.get("action") == "awaiting_withdraw_details":
            if text.startswith("/"):
                user_state.delete_one({"chat_id": chat_id})
            else:
                user_wallet, user_phone = get_user_wallet_and_phone(chat_id)
                parts = text.strip().split(maxsplit=2)
                if len(parts) < 3:
                    send_telegram_user(chat_id, "❌ ፎርማት: `<መጠን> <አካውንት> <ስም>`")
                    return "OK", 200

                w_account = parts[1].strip()
                w_name = parts[2].strip()
                method = u_state.get("method", "telebirr")

                try:
                    w_amount = float(parts[0])
                    if w_amount <= 0:
                        raise ValueError()
                except ValueError:
                    send_telegram_user(chat_id, "❌ ትክክለኛ መጠን ያስገቡ!")
                    return "OK", 200

                current_bal = user_wallet.get("balance", 0.0) if user_wallet else 0.0
                if current_bal < w_amount:
                    send_telegram_user(chat_id, f"❌ በቂ ባላንስ የለም! ባላንስዎ: {current_bal} ETB")
                    user_state.delete_one({"chat_id": chat_id})
                    return "OK", 200

                deducted_user = wallets.find_one_and_update(
                    {"phone": user_phone, "balance": {"$gte": w_amount}},
                    {"$inc": {"balance": -w_amount}},
                    return_document=True
                )
                if not deducted_user:
                    user_state.delete_one({"chat_id": chat_id})
                    return "OK", 200

                notify_user_balance_update(user_phone, deducted_user.get("balance", 0))
                user_state.delete_one({"chat_id": chat_id})

                tx_insert = transactions.insert_one({
                    "phone": user_phone, "chat_id": chat_id, "first_name": first_name,
                    "type": "withdrawal", "method": method, "amount": w_amount,
                    "account_number": w_account, "account_name": w_name, "status": "pending", "timestamp": datetime.utcnow()
                })
                tx_id = str(tx_insert.inserted_id)
                send_telegram_user(chat_id, "⏳ ዊዝድሮዋል ጥያቄዎ ተመዝግቧል!")
                
                admin_wit_msg = f"📤 *አዲስ ዊዝድሮዋል*\n📞 `{user_phone}`\n💵 `{w_amount}` ETB"
                admin_wit_kb = {
                    "inline_keyboard": [
                        [{"text": "✅ Approve", "callback_data": f"app_wit_{tx_id}_{user_phone}_{w_amount}"}, {"text": "❌ Reject (Refund)", "callback_data": f"rej_wit_{tx_id}_{user_phone}_{w_amount}"}],
                        [{"text": "🚫 Ban", "callback_data": f"ban_user_{user_phone}"}]
                    ]
                }
                send_telegram(admin_wit_msg, reply_markup=admin_wit_kb)
                return "OK", 200

        # /transfer ሎጂክ (ትራንስፈር ከ10 ብር በላይ፣ ቦነስ 20 ብር ትራንስፈር ማድረግ አይቻልም)
        if text.lower().startswith("/transfer"):
            parts = text.split()
            if len(parts) == 3:
                rec_target = sanitize_input(parts[1])
                try:
                    amt_val = float(parts[2])
                    if amt_val <= 10:
                        send_telegram_user(chat_id, "❌ ትራንስፈር የሚፈቀደው ከ10 ብር በላይ ነው!")
                        return "OK", 200

                    sender_wallet = wallets.find_one({"chat_id": chat_id})
                    if not sender_wallet:
                        send_telegram_user(chat_id, "❌ መጀመሪያ /start ይበሉ!")
                        return "OK", 200
                    
                    sender_phone = sender_wallet.get("phone")
                    formatted_rec_phone = format_phone_number(rec_target)

                    if sender_phone == formatted_rec_phone:
                        send_telegram_user(chat_id, "❌ ራስዎ ለራስዎ ማስተላለፍ አይችሉም!")
                        return "OK", 200

                    sender_current_bal = sender_wallet.get("balance", 0.0)
                    
                    # ቦነስ 20 ብር ትራንስፈር ማድረግ አይቻልም (Checking if balance is strictly bonus 20 and not deposited)
                    if sender_current_bal == 20.0 and amt_val > 0:
                        send_telegram_user(chat_id, "❌ የመጀመሪያው ቦነስ 20 ብር ትራንስፈር ማድረግ አይቻልም!")
                        return "OK", 200

                    if sender_current_bal < amt_val:
                        send_telegram_user(chat_id, "❌ በቂ ቀሪ ሂሳብ የለዎትም!")
                        return "OK", 200

                    rec_wallet = wallets.find_one({"$or": [{"phone": formatted_rec_phone}, {"username": rec_target.lstrip("@")}]})
                    if not rec_wallet:
                        send_telegram_user(chat_id, "❌ ተቀባዩ ተጠቃሚ አልተገኘም!")
                        return "OK", 200

                    rec_phone = rec_wallet.get("phone")
                    rec_chat_id = rec_wallet.get("chat_id")

                    debit_res = wallets.find_one_and_update(
                        {"phone": sender_phone, "balance": {"$gte": amt_val}},
                        {"$inc": {"balance": -amt_val}},
                        return_document=True
                    )
                    if not debit_res:
                        send_telegram_user(chat_id, "❌ ዝውውሩ አልተሳካም!")
                        return "OK", 200

                    credit_res = wallets.find_one_and_update({"phone": rec_phone}, {"$inc": {"balance": amt_val}}, return_document=True)

                    transactions.insert_one({
                        "phone": sender_phone, "receiver_phone": rec_phone, "type": "transfer",
                        "amount": amt_val, "status": "completed", "timestamp": datetime.utcnow()
                    })

                    notify_user_balance_update(sender_phone, debit_res.get("balance", 0))
                    notify_user_balance_update(rec_phone, credit_res.get("balance", 0))

                    send_telegram_user(chat_id, f"✅ ትራንስፈር ተሳክቷል! የተላለፈው: {amt_val} ETB")
                    if rec_chat_id:
                        send_telegram_user(rec_chat_id, f"🎁 {amt_val} ETB ገቢ ተደርጎልዎታል!")
                except ValueError:
                    send_telegram_user(chat_id, "❌ ትክክለኛ መጠን ያስገቡ!")
            else:
                send_telegram_user(chat_id, "💸 ፎርማት: `/transfer <ስልክ> <መጠን>`\n(ትራንስፈር ከ10 ብር በላይ ብቻ)")
            return "OK", 200

        if text.lower() == "/balance":
            u_wallet = wallets.find_one({"chat_id": chat_id})
            if not u_wallet:
                send_telegram_user(chat_id, "❌ አካውንት የለም!")
            else:
                send_telegram_user(chat_id, f"💰 ቀሪ ባላንስዎ: {u_wallet.get('balance', 0):,.2f} ETB\n(ቦነስ withdrawn ማድረግ አይቻልም)")
            return "OK", 200

        if text.lower() in ["/instruction", "/instraction"]:
            send_telegram_user(chat_id, "ℹ️ የቢንጎ ህጎች እና መመሪያዎች...")
            return "OK", 200

        if text.lower() == "/history":
            u_wallet = wallets.find_one({"chat_id": chat_id})
            if not u_wallet:
                send_telegram_user(chat_id, "❌ አካውንት የለም!")
                return "OK", 200
            send_telegram_user(chat_id, "📜 የታሪክ ሪፖርት...")
            return "OK", 200

        if text.lower() == "/play" or text.lower() == "/start":
            user_exist, formatted_phone = get_user_wallet_and_phone(chat_id)
            if user_exist and formatted_phone:
                play_keyboard = get_player_play_keyboard(formatted_phone)
                send_telegram_user(chat_id, "🕹 *ROOMs ምርጫ:*", reply_markup=play_keyboard)
            else:
                contact_keyboard = {
                    "keyboard": [[{"text": "📱 ስልክ ቁጥርዎን ያጋሩ", "request_contact": True}]],
                    "resize_keyboard": True, "one_time_keyboard": True
                }
                send_telegram_user(chat_id, "👋 እንኳን ደህና መጡ! እባክዎ ስልክ ቁጥርዎን ያጋሩ።", reply_markup=contact_keyboard)
            return "OK", 200

    elif "callback_query" in data:
        cq = data["callback_query"]
        cq_id = cq["id"]
        chat_id = str(cq["message"]["chat"]["id"])
        message_id = cq["message"]["message_id"]
        msg_original_text = cq["message"].get("text", "")
        data_str = cq.get("data", "")
        answer_url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"

        if data_str == "Besh_bingo_bonus":
            requests.post(answer_url, json={"callback_query_id": cq_id, "text": "ቦነስ withdrawn ማድረግ አይቻልም!", "show_alert": True})
            return "OK", 200

        if data_str == "user_back_to_menu":
            u_doc, u_ph = get_user_wallet_and_phone(chat_id)
            requests.post(answer_url, json={"callback_query_id": cq_id})
            if u_ph:
                play_kb = get_player_play_keyboard(u_ph)
                edit_telegram_message(chat_id, message_id, "🕹 *ዋና ማውጫ*", reply_markup=play_kb)
            return "OK", 200

        if data_str in ["user_select_tele_dep", "user_select_cbe_dep"]:
            sel_method = "telebirr" if "tele" in data_str else "cbebirr"
            user_state.update_one({"chat_id": chat_id}, {"$set": {"action": "awaiting_deposit_sms", "method": sel_method}}, upsert=True)
            send_telegram_user(chat_id, f"📱 እባክዎ የ {sel_method.upper()} SMS ኮፒ አድርገው ይላኩ።")
            requests.post(answer_url, json={"callback_query_id": cq_id})
            return "OK", 200

        if data_str in ["user_select_tele_wit", "user_select_cbe_wit"]:
            sel_method = "telebirr" if "tele" in data_str else "cbebirr"
            user_state.update_one({"chat_id": chat_id}, {"$set": {"action": "awaiting_withdraw_details", "method": sel_method}}, upsert=True)
            send_telegram_user(chat_id, "📤 ፎርማት: `<መጠን> <አካውንት> <ስም>`\n(ቦነስ withdrawn ማድረግ አይቻልም)")
            requests.post(answer_url, json={"callback_query_id": cq_id})
            return "OK", 200

        if chat_id == str(ADMIN_ID):
            if data_str == "admin_refresh_dashboard":
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "🔄 ተአድሷል!"})
                edit_telegram_message(chat_id, message_id, "🛠 *አድሚን ሰሌዳ*", reply_markup=get_admin_dashboard_keyboard())
                return "OK", 200

            elif data_str.startswith("app_dep_"):
                parts = data_str.split("_")
                tx_id_str, phone_num, amt = parts[2], format_phone_number(parts[3]), float(parts[4])
                tx_updated = transactions.find_one_and_update({"_id": ObjectId(tx_id_str), "status": "pending"}, {"$set": {"status": "approved", "processed_at": datetime.utcnow()}})
                if tx_updated:
                    updated_wallet = wallets.find_one_and_update({"phone": phone_num}, {"$inc": {"balance": amt}}, return_document=True, upsert=True)
                    notify_user_balance_update(phone_num, updated_wallet.get("balance", 0))
                    requests.post(answer_url, json={"callback_query_id": cq_id, "text": "✅ Approved!"})
                    edit_telegram_message(chat_id, message_id, msg_original_text + "\n\n✅ **APPROVED**", reply_markup={"inline_keyboard": []})
                return "OK", 200

            elif data_str.startswith("rej_dep_"):
                parts = data_str.split("_")
                transactions.find_one_and_update({"_id": ObjectId(parts[2]), "status": "pending"}, {"$set": {"status": "rejected", "processed_at": datetime.utcnow()}})
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "❌ Rejected!"})
                edit_telegram_message(chat_id, message_id, msg_original_text + "\n\n❌ **REJECTED**", reply_markup={"inline_keyboard": []})
                return "OK", 200

            elif data_str.startswith("app_wit_"):
                parts = data_str.split("_")
                transactions.find_one_and_update({"_id": ObjectId(parts[2]), "status": "pending"}, {"$set": {"status": "approved", "processed_at": datetime.utcnow()}})
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "✅ Withdraw Approved!"})
                edit_telegram_message(chat_id, message_id, msg_original_text + "\n\n✅ **APPROVED**", reply_markup={"inline_keyboard": []})
                return "OK", 200

            elif data_str.startswith("rej_wit_"):
                parts = data_str.split("_")
                tx_id_str, phone_num, amt = parts[2], format_phone_number(parts[3]), float(parts[4])
                tx_updated = transactions.find_one_and_update({"_id": ObjectId(tx_id_str), "status": "pending"}, {"$set": {"status": "rejected", "processed_at": datetime.utcnow()}})
                if tx_updated:
                    refunded_wallet = wallets.find_one_and_update({"phone": phone_num}, {"$inc": {"balance": amt}}, return_document=True)
                    notify_user_balance_update(phone_num, refunded_wallet.get("balance", 0))
                    requests.post(answer_url, json={"callback_query_id": cq_id, "text": "❌ Rejected & Refunded!"})
                    edit_telegram_message(chat_id, message_id, msg_original_text + "\n\n❌ **REJECTED (REFUNDED)**", reply_markup={"inline_keyboard": []})
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
        socketio.start_background_task(super_game_loop)
        
    broadcast_game_state("10")
    broadcast_game_state("20")
    broadcast_game_state("50")
    broadcast_game_state("100")
    broadcast_game_state("200")
    broadcast_game_state("super")

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
