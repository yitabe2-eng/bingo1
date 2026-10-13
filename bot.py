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

client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=2000)
db = client['bingo_db']
wallets = db['wallets']
blocked_phones = db['blocked_phones'] 
transactions = db['transactions']
admin_state = db['admin_state']
user_state = db['user_state']

try:
    wallets.create_index("phone", unique=True)
    wallets.create_index("telegram_id", unique=True, sparse=True)
    blocked_phones.create_index("phone", unique=True)
    transactions.create_index("timestamp")
    transactions.create_index("status")
    user_state.create_index("chat_id", unique=True)
except Exception as e:
    print(f"Index creation notice: {e}")

# የስልክ ቁጥር ትክክለኛነት ማረጋገጫ (Valid Ethiopian Phone check)
def is_valid_ethiopian_phone(phone_str):
    if not phone_str:
        return False
    clean = re.sub(r'[^0-9]', '', str(phone_str))
    # የኢትዮጵያ ስልክ ቁጥር ፎርማት፡
    # 2519... ወይም 2517... (12 አሃዝ)
    # 09... ወይም 07... (10 አሃዝ)
    # 9... ወይም 7... (9 አሃዝ)
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

# የተጠቃሚውን ዋሌት እና ትክክለኛ ስልክ ቁጥር ማግኛ (Get User Wallet & Verified Phone)
def get_user_wallet_and_phone(chat_id_val):
    if not chat_id_val:
        return None, None
    chat_str = str(chat_id_val)
    
    # 1. መጀመሪያ በ chat_id ወይም telegram_id መፈለግ
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
            
    # 2. በ integer chat_id መሞከር
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

# የ SMS ፎርማት ማረጋገጫ (SMS Validation Rules)
def validate_sms_text(sms_text):
    clean = sms_text.strip()
    
    # 1. ቁጥር ብቻ ከሆነ
    if clean.isdigit():
        return False, "❌ ያስገቡት መረጃ ቁጥር ብቻ ነው። እባክዎ ከቴሌብር/ሲቢኢ የደረሰዎትን ሙሉ SMS ኮፒ አድርገው ይላኩ!"
    
    # 3. ፊደል ብቻ ከሆነ (ምንም አሃዝ የሌለው)
    digits = re.findall(r'\d', clean)
    if not digits:
        return False, "❌ ያስገቡት መረጃ ፊደል ብቻ ነው። እባክዎ ከቴሌብር/ሲቢኢ የደረሰዎትን ሙሉ SMS ኮፒ አድርገው ይላኩ!"
    
    # 2. አሃዙ ከዘጠኝ ብቻ ከሆነ (የቁጥሮች ብዛት 9 ብቻ ወይም ከዚያ በታች ከሆነ - ለምሳሌ ስልክ ቁጥር ብቻ)
    if len(digits) <= 9:
        return False, "❌ ያስገቡት መረጃ ትክክል አይደለም (ስልክ ቁጥር ብቻ መሆን የለበትም)። እባክዎ ከቴሌብር/ሲቢኢ የደረሰዎትን ሙሉ SMS ኮፒ አድርገው ይላኩ!"
    
    if len(clean) < 15:
        return False, "❌ ያስገቡት የSMS መረጃ በጣም አጭር ነው። እባክዎ ከቴሌብር/ሲቢኢ የደረሰዎትን ሙሉ SMS ኮፒ አድርገው ይላኩ!"
        
    return True, ""

# ከ SMS ውስጥ የገንዘብ መጠን ማውጫ (Amount extraction helper)
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

# የ Pending ጥያቄዎች ቁጥር ማሳያ በተን ፅሁፍ (Dynamic Counter/Emoji)
def get_pending_button_text():
    count = transactions.count_documents({"status": "pending"})
    if count == 0:
        return "⏳ የሚጠብቁ ጥያቄዎች (0)"
    else:
        return f"🚨 የሚጠብቁ ጥያቄዎች ({count})"

game_states = {
    "10": {
        "status": "lobby", "timer": 30, "ball_timer": 2, "pot": 0, "players": {}, 
        "sold_tickets": {}, "current_ball": "--", "drawn_balls": [], "winner": None,
        "winning_card": None, "winning_ticket_num": None, "winning_indices": None,
        "winning_line_name": None, "all_cards": {}
    },
    "20": {
        "status": "lobby", "timer": 30, "ball_timer": 2, "pot": 0, "players": {}, 
        "sold_tickets": {}, "current_ball": "--", "drawn_balls": [], "winner": None,
        "winning_card": None, "winning_ticket_num": None, "winning_indices": None,
        "winning_line_name": None, "all_cards": {}
    },
    "50": {
        "status": "lobby", "timer": 30, "ball_timer": 2, "pot": 0, "players": {}, 
        "sold_tickets": {}, "current_ball": "--", "drawn_balls": [], "winner": None,
        "winning_card": None, "winning_ticket_num": None, "winning_indices": None,
        "winning_line_name": None, "all_cards": {}
    },
    "super": { 
        "status": "lobby", "timer": 30, "ball_timer": 3, "pot": 0, "players": {}, 
        "sold_tickets": {}, "current_ball": "--", "drawn_balls": [], "winner": None,
        "winning_card": None, "winning_ticket_num": None, "winning_indices": None,
        "winning_line_name": None, "all_cards": {}
    }
}

loop_started = False
reset_task_references = {"10": None, "20": None, "50": None, "super": None}
pending_claims = {"10": [], "20": [], "50": [], "super": []}
claim_lock_active = {"10": False, "20": False, "50": False, "super": False}

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
    price = 10 if room_type == "10" else (20 if room_type == "20" else 50)
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
    
    default_ball_timer = 3 if room_type == "super" else 2
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
                state["ball_timer"] = 2
                shuffled = balls.copy()
                random.shuffle(shuffled)
                broadcast_game_state(room_type)
            else:
                state["timer"] = 30
                broadcast_game_state(room_type)
                continue

            if shuffled:
                for j in range(2, -1, -1):
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

@app.route('/')
def index_10(): 
    return render_template('index.html')

@app.route('/twenty')
def index_20():
    return render_template('index2.html')

@app.route('/super')
def index_50():
    return render_template('index1.html')

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
        return jsonify({
            "success": False,
            "user_exists": False,
            "msg": "መለያዎ ከሲስተሙ ተሰርዟል! እባክዎ እንደገና ይመዝገቡ።"
        })
        
    db_phone = user['phone']
    p_data = state["players"].get(db_phone, {"cards": {}})
    cards_list = list(p_data["cards"].values())
    clean_players = {k: {"username": v.get("username", ""), "cards": list(v.get("cards", {}).values())} for k, v in state["players"].items()}
    
    is_waiting = False
    if room == "super" and state["status"] in ["playing", "result"] and db_phone not in state["players"]:
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
        "winning_card": state["winning_card"],
        "winning_ticket_num": state["winning_ticket_num"],
        "winning_indices": state.get("winning_indices"),
        "winning_line_name": state.get("winning_line_name"),
        "all_cards": state.get("all_cards", {}),
        "players": clean_players, 
        "balance": user.get('balance', 0), 
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
            return jsonify({"success": False, "msg": f"በአንድ ጊዜ ከ 1 ሩም በላይ መጫወት አይቻልም!"})

    state = game_states.get(room, game_states["10"])
    price = 10 if room == "10" else (20 if room == "20" else 50)

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
    price = 10 if room == "10" else 20

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

                if num_winners == 1:
                    winner_display = f"{pending_claims[room][0]['username']} አሸንፏል"
                else:
                    winner_names = [c["username"] for c in pending_claims[room]]
                    winner_display = f"{' & '.join(winner_names)} አሸንፈዋል"

                state["winner"] = winner_display
                state["winning_card"] = pending_claims[room][0]["card"]  
                state["winning_ticket_num"] = pending_claims[room][0]["ticket_num"] 
                state["winning_indices"] = pending_claims[room][0]["indices"]
                state["winning_line_name"] = pending_claims[room][0]["line_name"] 

                def background_win_task():
                    if num_winners == 1:
                        w = pending_claims[room][0]
                        win_res = wallets.find_one_and_update(
                            {"phone": w["phone"]}, 
                            {"$inc": {"balance": total_prize}}, 
                            return_document=True
                        )
                        if win_res:
                            gevent.spawn(notify_user_balance_update, w["phone"], win_res.get("balance", 0))
                        
                        success_msg = f"🏆 *WINNER (Room {room})!* \n👤 Name: {w['username']} | 📞 Phone: `{w['phone']}` | 🎫 Ticket: {w['ticket_num']} \n🎯 Winning Ball: {w['winning_ball']} \n💰 Prize Won: {total_prize:.2f} ETB"
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
                        
                        success_msg = f"🏆 *WINNERS (Shared Prize on Ball {pending_claims[room][0]['winning_ball']})!* \n💰 Total Pot Share: {share_prize:.2f} ETB each ({num_winners} winners)\n" + "\n".join(winner_texts)
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
        
        # ተጠቃሚዎችን በ chat_id ማገናኘት
        if chat_id != str(ADMIN_ID):
            wallets.update_one(
                {"chat_id": chat_id}, 
                {"$set": {"chat_id": chat_id}}, 
                upsert=False
            )
        
        # 1. ተጠቃሚው ስልክ ቁጥሩን በ Contact Sharing ሲያጋራ (Share Phone Number)
        if "contact" in msg:
            contact = msg["contact"]
            raw_phone = contact.get("phone_number", "")
            formatted_phone = format_phone_number(raw_phone)
            telegram_id = str(contact.get("user_id", chat_id))
            first_name = contact.get("first_name", msg.get("from", {}).get("first_name", "User"))
            username = msg.get("from", {}).get("username", "")
            
            # የድሮ ወይም ያልተሟሉ የዚህ chat_id ሰነዶችን ማጽዳት (Clean up old records where phone was telegram id)
            wallets.delete_many({
                "$and": [
                    {"$or": [{"chat_id": str(chat_id)}, {"telegram_id": str(telegram_id)}]},
                    {"phone": {"$ne": formatted_phone}}
                ]
            })
            
            # ዋሌቱን በትክክለኛው ስልክ ቁጥር መመዝገብ (Save wallet with verified Ethiopian phone)
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
                        "balance": 20.00,  # መነሻ ቀሪ ሂሳብ በነባሪ 20.00 ETB
                        "created_at": datetime.utcnow()
                    }
                },
                upsert=True
            )
            
            reg_success_msg = (
                "✅ **ምዝገባዎ በተሳካ ሁኔታ ተጠናቋል!**\n\n"
                f"📞 የተመዘገበ ስልክ ቁጥር፦ `{formatted_phone}`\n"
                "🎁 የ 20.00 ETB መነሻ ቀሪ ሂሳብ ተሰጥቶዎታል።\n\n"
                "አሁን ዲፖዚት ማድረግ (/deposit)፣ ወጪ ማድረግ (/withdraw) ወይም ከታች ካሉት ክፍሎች መርጠው መጫወት ይችላሉ፦"
            )
            send_telegram_user(chat_id, reg_success_msg, reply_markup={"remove_keyboard": True})

            u_10 = f"{WEB_APP_URL}?phone={formatted_phone}"
            u_20 = f"{WEB_APP_URL}/twenty?phone={formatted_phone}"
            u_50 = f"{WEB_APP_URL}/super?phone={formatted_phone}"
            u_super = f"{WEB_APP_URL}/superbesh?phone={formatted_phone}"

            play_keyboard = {
                "inline_keyboard": [
                    [{"text": "🎮 PLAY | በ10 ብር", "web_app": {"url": u_10}}], 
                    [{"text": "🎮 PLAY | በ20 ብር", "web_app": {"url": u_20}}], 
                    [{"text": "🎮 PLAY | በ50 ብር", "web_app": {"url": u_50}}], 
                    [{"text": "🎮 BeshBingo (ሳምንታዊ) | 50 ብር", "web_app": {"url": u_super}}],
                    [{"text": "⚽ BeshBingo Bonus", "callback_data": "Besh_bingo_bonus"}]
                ]
            }
            send_telegram_user(chat_id, "🕹 *PLAY IN:*\nጨዋታ ለመጀመር ከታች ካሉት ክፍሎች አንዱን ይምረጡ፦", reply_markup=play_keyboard)
            return "OK", 200

        # የአድሚን ስቴት አያያዝ (Admin States)
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

        # የተጠቃሚ ስቴት አያያዝ (User Multistep State: Deposit SMS / Withdraw input)
        u_state = user_state.find_one({"chat_id": chat_id})
        
        # 1. ዴፖዚት የ SMS ማስገቢያ ፍሰት (Awaiting Deposit SMS)
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

                # Rate limiting check (በ5 ደቂቃ ውስጥ ቢበዛ 2 ጥያቄ ብቻ)
                if not check_rate_limit(user_phone):
                    send_telegram_user(
                        chat_id, 
                        "⚠️ **ይቅርታ!**\nአንድ ተጠቃሚ በ 5 ደቂቃ ውስጥ 2 ጊዜ ብቻ ነው የዲፖዚት ወይም የወጪ (Withdraw) ጥያቄ መላክ የሚችለው። እባክዎ ጥቂት ደቂቃዎች ቆይተው እንደገና ይሞክሩ።"
                    )
                    user_state.delete_one({"chat_id": chat_id})
                    return "OK", 200

                # SMS validation rules (ቁጥር ብቻ፣ ፊደል ብቻ፣ አሃዙ ከ 9 በታች ወይም እኩል ከሆነ መከልከል)
                is_valid, err_msg = validate_sms_text(text)
                if not is_valid:
                    send_telegram_user(chat_id, err_msg)
                    return "OK", 200

                # ትክክለኛ SMS ከሆነ
                method = u_state.get("method", "telebirr")
                extracted_amt = extract_amount_from_sms(text)
                user_state.delete_one({"chat_id": chat_id})

                # ወደ Transactions ዳታቤዝ PENDING ተደርጎ በትክክለኛው ስልክ ቁጥር ይመዘገባል
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

                # ለተጫዋቹ የሚላክ ማረጋገጫ
                ack_msg = (
                    "⏳ **የዲፖዚት ጥያቄዎ በተሳካ ሁኔታ ደርሷል!**\n\n"
                    f"📞 የተመዘገበ ስልክ፦ `{user_phone}`\n"
                    f"💳 መንገድ፦ *{method.upper()}*\n"
                    f"📅 ቀን፦ `{now_str}`\n"
                    "📌 ሁኔታ፦ *PENDING (በማረጋገጥ ላይ)*\n\n"
                    "አስተዳዳሪው የከፈሉትን መረጃ አረጋግጦ ሲያጸድቀው (Approve) የተጨመረው ብር እና አጠቃላይ ባላንስዎ በቅጽበት ይላክሎታል።"
                )
                send_telegram_user(chat_id, ack_msg)

                # ለአድሚን የሚላክ የ Approval ማሳወቂያ - ትክክለኛው የስልክ ቁጥር እንጂ Telegram ID እንዳይሆን
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

        # 2. ዊዝድሮዋል መረጃ ማስገቢያ ፍሰት (Awaiting Withdraw details)
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

                # ስልክ ቁጥር ካልተገኘ የተላከው አካውንት ትክክለኛ ስልክ ከሆነ መመዝገብ
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

                # Rate limiting check (በ5 ደቂቃ ውስጥ ቢበዛ 2 ጥያቄ ብቻ)
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

                # ቀሪ ሂሳብ ማረጋገጫ (Balance check)
                current_bal = user_wallet.get("balance", 0.0) if user_wallet else 0.0
                if current_bal < w_amount:
                    send_telegram_user(
                        chat_id, 
                        f"❌ **በቂ ቀሪ ሂሳብ (Balance) የለዎትም!**\n\nየእርስዎ ቀሪ ሂሳብ፦ `{current_bal:,.2f} ETB`\nለመውጣት የጠየቁት፦ `{w_amount:,.2f} ETB`"
                    )
                    user_state.delete_one({"chat_id": chat_id})
                    return "OK", 200

                # እንዳይባክን እና ሁለቴ እንዳይጠየቅ ወዲያውኑ ከቀሪ ሂሳቡ ላይ ይቀነሳል (Pending Debit)
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

                # ዳታቤዝ ውስጥ PENDING ሆኖ በትክክለኛው ስልክ ቁጥር ይመዘገባል
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

                # ለተጠቃሚው ማረጋገጫ
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

                # ለአድሚን የሚላክ የወጪ ማሳወቂያ - የተመዘገበው ስልክ እንጂ Telegram ID እንዳይሆን
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

        # /deposit ትዕዛዝ - መጀመሪያ ስልክ ቁጥር መኖሩን አረጋግጦ Inline Buttons ማሳየት
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
                        {"text": "📱 TELE-BIRR", "callback_data": "user_select_tele_dep"},
                        {"text": "🏦 CBE-BIRR", "callback_data": "user_select_cbe_dep"}
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

        # /withdraw ትዕዛዝ - መጀመሪያ ስልክ ቁጥር መኖሩን አረጋግጦ Inline Buttons ማሳየት
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
                        {"text": "📱 TELE-BIRR", "callback_data": "user_select_tele_wit"},
                        {"text": "🏦 CBE-BIRR", "callback_data": "user_select_cbe_wit"}
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

        # 3. ትራንስፈር (Transfer / P2P) - ሙሉ በሙሉ አውቶማቲክ (በሰከንዶች ውስጥ ያለምንም አድሚን ጣልቃገብነት)
        if text.lower().startswith("/transfer"):
            parts = text.split()
            if len(parts) == 3:
                rec_target = sanitize_input(parts[1])
                try:
                    amt_val = float(parts[2])
                    if amt_val <= 0:
                        send_telegram_user(chat_id, "❌ የብር መጠን ከ 0 በላይ መሆን አለበት!")
                        return "OK", 200

                    sender_wallet = wallets.find_one({"chat_id": chat_id})
                    if not sender_wallet:
                        send_telegram_user(chat_id, "❌ አካውንትዎ አልተገኘም። እባክዎ መጀመሪያ /start ብለው ይመዝገቡ።")
                        return "OK", 200
                    
                    sender_phone = sender_wallet.get("phone")
                    formatted_rec_phone = format_phone_number(rec_target)

                    # ህግ A: ራሱን ለራስ መከልከል (sender != receiver)
                    if sender_phone == formatted_rec_phone:
                        send_telegram_user(chat_id, "❌ ራስዎ ለራስዎ ብር ማስተላለፍ አይችሉም!")
                        return "OK", 200

                    # ህግ B: ቀሪ ሂሳብ ማረጋገጫ (sender.balance >= transfer_amount)
                    sender_current_bal = sender_wallet.get("balance", 0.0)
                    if sender_current_bal < amt_val:
                        send_telegram_user(
                            chat_id, 
                            f"❌ **በቂ ቀሪ ሂሳብ የለዎትም!**\nየእርስዎ ባላንስ፦ `{sender_current_bal:,.2f} ETB`\nሊያስተላልፉ የጠየቁት፦ `{amt_val:,.2f} ETB`"
                        )
                        return "OK", 200

                    # ህግ C: ተቀባይ መኖሩን ማረጋገጥ (በስልክ ወይም በተጠቃሚ ስም)
                    rec_wallet = wallets.find_one({
                        "$or": [
                            {"phone": formatted_rec_phone},
                            {"username": rec_target.lstrip("@")}
                        ]
                    })
                    if not rec_wallet:
                        send_telegram_user(chat_id, "❌ ተቀባዩ ተጠቃሚ በሲስተሙ ውስጥ አልተገኘም! እባክዎ የስልክ ቁጥሩን ያረጋግጡ።")
                        return "OK", 200

                    rec_phone = rec_wallet.get("phone")
                    rec_chat_id = rec_wallet.get("chat_id")

                    # ህግ D: የአፈፃፀም ሂደት (Atomic Transaction)
                    # 1. ከላኪው አካውንት ገንዘቡን ይቀንሳል (Debit)
                    debit_res = wallets.find_one_and_update(
                        {"phone": sender_phone, "balance": {"$gte": amt_val}},
                        {"$inc": {"balance": -amt_val}},
                        return_document=True
                    )
                    if not debit_res:
                        send_telegram_user(chat_id, "❌ ዝውውሩ አልተሳካም። እባክዎ ቀሪ ሂሳብዎን ፈትሸው እንደገና ይሞክሩ።")
                        return "OK", 200

                    # 2. በተቀባዩ አካውንት ላይ ገንዘቡን ይጨምራል (Credit)
                    credit_res = wallets.find_one_and_update(
                        {"phone": rec_phone},
                        {"$inc": {"balance": amt_val}},
                        return_document=True
                    )

                    now_time = datetime.utcnow()
                    now_str = (now_time + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")

                    # 3. የዝውውር ታሪክ (Transfer Log) ዳታቤዝ ውስጥ ይመዘገባል
                    transactions.insert_one({
                        "phone": sender_phone,
                        "receiver_phone": rec_phone,
                        "type": "transfer",
                        "amount": amt_val,
                        "status": "completed",
                        "timestamp": now_time
                    })

                    # Real-time Web Socket Balance Updates
                    notify_user_balance_update(sender_phone, debit_res.get("balance", 0))
                    notify_user_balance_update(rec_phone, credit_res.get("balance", 0))

                    # ለላኪው የሚላክ ፈጣን ማረጋገጫ
                    sender_msg = (
                        "✅ **ትራንስፈር በተሳካ ሁኔታ ተከናውኗል! (Transfer Success)**\n\n"
                        f"📤 የተላከለት፦ `{rec_phone}` ({rec_wallet.get('first_name', 'User')})\n"
                        f"💵 የተላለፈው ብር፦ `*{amt_val:,.2f} ETB*`\n"
                        f"💰 አዲሱ ቀሪ ሂሳብዎ፦ `*{debit_res.get('balance', 0):,.2f} ETB*`\n"
                        f"📅 ቀን፦ `{now_str}`"
                    )
                    send_telegram_user(chat_id, sender_msg)

                    # ለተቀባዩ የሚላክ ፈጣን ማሳወቂያ
                    if rec_chat_id:
                        rec_msg = (
                            "🎁 **ገንዘብ ገቢ ተደርጎልዎታል! (Transfer Received)**\n\n"
                            f"📥 የላከው ሰው፦ `{sender_phone}` ({sender_wallet.get('first_name', 'User')})\n"
                            f"💵 የገባው ብር፦ `*{amt_val:,.2f} ETB*`\n"
                            f"💰 አጠቃላይ ቀሪ ሂሳብዎ፦ `*{credit_res.get('balance', 0):,.2f} ETB*`\n"
                            f"📅 ቀን፦ `{now_str}`"
                        )
                        send_telegram_user(rec_chat_id, rec_msg)

                except ValueError:
                    send_telegram_user(chat_id, "❌ የብር መጠን ስህተት ነው። ምሳሌ፦ `/transfer 0921523653 20`")
            else:
                msg_txt = (
                    "💸 **ብር ለማስተላለፍ (Transfer / P2P)**\n\n"
                    "ምንም አይነት የአድሚን እጅ ጣልቃገብነት ሳይፈልግ በሰከንዶች ውስጥ አውቶማቲክ ይከናወናል!\n\n"
                    "የሚልኩለትን ስልክ እና ብር ያስገቡ፦\n"
                    "`/transfer 0921523653 20`"
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
                bal_msg = f"💰 *የሂሳብ መግለጫ (Balance)*\n\n📞 ስልክ ቁጥር: `{u_phone}`\n💵 ቀሪ ባላንስዎ: *{u_balance:,.2f} ETB*"
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
                send_telegram("📢 እባክዎ ለሁሉም ተጠቃሚዎች ማስተላለፍ የሚፈልጉትን **ፎቶ ብቻ** (ካፕሽን ሳይጨምሩ) አሁን ይላኩ፦")
            return "OK", 200

        # የአድሚን ትዕዛዞች (Admin Commands)
        if chat_id == str(ADMIN_ID):
            parts = text.split()
            cmd = parts[0] if parts else ""

            if cmd == "/admin":
                pending_btn_txt = get_pending_button_text()
                admin_keyboard = {
                    "inline_keyboard": [
                        [{"text": pending_btn_txt, "callback_data": "admin_pending_req"}],
                        [{"text": "🔗 የኤጀንት ሊንክ ማመንጫ (Gen Link)", "callback_data": "admin_gen_agent_link"}],
                        [{"text": "👥 የኤጀንት ስታቲስቲክስ (/agent)", "callback_data": "admin_agent_stats_guide"}],
                        [{"text": "📜 የትራንዛክሽን ታሪክ (በቀን እና ዓይነት)", "callback_data": "admin_history_req"}],
                        [{"text": "📅 የእለት/የሳምንት ገቢ እና ወጪ (20% Profit)", "callback_data": "admin_financial_stats"}],
                        [{"text": "📋 የሁሉም ተጠቃሚዎች ባላንስ", "callback_data": "admin_show_all_bal"}],
                        [{"text": "➕ ባላንስ ለመጨመር (/add)", "callback_data": "guide_add"}],
                        [{"text": "➖ ባላንስ ለመቀነስ (/sub)", "callback_data": "guide_sub"}],
                        [{"text": "📢 መልእክት ለማስተላለፍ (/broadcast)", "callback_data": "guide_broadcast"}],
                        [{"text": "🚫 ተጠቃሚ ብሎክ/ማጥፊያ (/block & /remove)", "callback_data": "guide_block_remove"}],
                    ]
                }
                send_telegram("🛠 *የአድሚን ማኔጅመንት ሰሌዳ (Admin Workspace)*", reply_markup=admin_keyboard)
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
                u_10 = f"{WEB_APP_URL}?phone={formatted_phone}"
                u_20 = f"{WEB_APP_URL}/twenty?phone={formatted_phone}"
                u_50 = f"{WEB_APP_URL}/super?phone={formatted_phone}"
                u_super = f"{WEB_APP_URL}/superbesh?phone={formatted_phone}"

                play_keyboard = {
                    "inline_keyboard": [
                        [{"text": "🎮 PLAY | በ10 ብር", "web_app": {"url": u_10}}], 
                        [{"text": "🎮 PLAY | በ20 ብር", "web_app": {"url": u_20}}], 
                        [{"text": "🎮 PLAY | በ50 ብር", "web_app": {"url": u_50}}], 
                        [{"text": "🎮 BeshBingo (ሳምንታዊ) | 50 ብር", "web_app": {"url": u_super}}],
                        [{"text": "⚽ BeshBingo Bonus", "callback_data": "Besh_bingo_bonus"}]
                    ]
                }
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
                    "👋 እንኳን ወደ **Besh Bingo** በደህና መጡ!\n\nእባክዎ ለመጀመር ከታች ያለውን የ **«📱 ስልክ ቁጥርዎን ያጋሩ»** ቁልፍ በመንካት ስልክ ቁጥርዎን ያጋሩ።",
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

        if data_str == "Besh_bingo_bonus":
            requests.post(answer_url, json={"callback_query_id": cq_id, "text": "የቦነስ ፕሮግራም በቅርቡ ይጀመራል!", "show_alert": True})
            return "OK", 200

        # ተጠቃሚው የዲፖዚት መንገድ ሲመርጥ (User selects Deposit method)
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

        # ተጠቃሚው የዊዝድሮዋል መንገድ ሲመርጥ (User selects Withdraw method)
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
                "`100 0960736061 Abebe Kebede`"
            )
            send_telegram_user(chat_id, wit_guide)
            requests.post(answer_url, json={"callback_query_id": cq_id})
            return "OK", 200

        # የአድሚን Action Handlers (Approve, Reject, Ban)
        if chat_id == str(ADMIN_ID):
            now_action_str = (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")

            # 1. ተጠቃሚን Ban ማድረግ (Ban User & remove buttons)
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
                send_telegram("📱 እባክዎ ሊንክ ማመንጨት የሚፈልጉትን **የኤጀንት ስልክ ቁጥር** ያስገቡ፦")
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

            # 2. ዲፖዚት ማፅደቅ (Approve Deposit)
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

            # 3. ዲፖዚት ውድቅ ማድረግ (Reject Deposit)
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

            # 4. ዊዝድሮዋል ማፅደቅ (Approve Withdrawal)
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

            # 5. ዊዝድሮዋል ውድቅ ሲደረግ ገንዘቡ ወደ ተጠቃሚው ሂሳብ መመለስ (Reject Withdrawal & Refund)
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
        
    broadcast_game_state("10")
    broadcast_game_state("20")
    broadcast_game_state("50")

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
