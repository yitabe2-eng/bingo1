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

ADMIN_ID = os.getenv("ADMIN_ID", "123456789") 
BOT_TOKEN = os.getenv("BOT_TOKEN", "") 
MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
WEB_APP_URL = os.getenv("WEB_APP_URL", "https://bingo1-pjyb.onrender.com") 

client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=2000)
db = client['bingo_db']
wallets = db['wallets']
blocked_phones = db['blocked_phones'] 
transactions = db['transactions']
admin_state = db['admin_state']

try:
    wallets.create_index("telegram_id", unique=True, sparse=True)
    wallets.create_index("phone", unique=True, sparse=True)
    blocked_phones.create_index("phone", unique=True)
    transactions.create_index("timestamp")
    transactions.create_index("status")
except Exception as e:
    print(f"Index creation notice: {e}")

# Rate limiter: max 2 requests per 5 minutes per user
rate_limit_cache = {}

def check_rate_limit(user_key):
    now = time.time()
    user_requests = rate_limit_cache.get(user_key, [])
    user_requests = [t for t in user_requests if now - t < 300]
    if len(user_requests) >= 2:
        rate_limit_cache[user_key] = user_requests
        return False
    user_requests.append(now)
    rate_limit_cache[user_key] = user_requests
    return True

def format_ethiopian_phone(phone_str):
    if not phone_str:
        return ""
    clean = re.sub(r'[^0-9]', '', str(phone_str))
    if clean.startswith("251") and len(clean) == 12:
        return clean
    elif clean.startswith("0") and len(clean) == 10:
        return "251" + clean[1:]
    elif (clean.startswith("9") or clean.startswith("7")) and len(clean) == 9:
        return "251" + clean
    return clean

def is_valid_deposit_sms(text):
    if not text:
        return False, "ምንም ጽሁፍ አልገባም!"
    cleaned = text.strip()
    
    # 1. ቁጥር ብቻ ከሆነ
    if cleaned.isdigit():
        return False, "ትክክል አይደለም"
    
    # 2. አሃዙ ከዘጠኝ ብቻ ከሆነ
    digits_only = re.sub(r'[^0-9]', '', cleaned)
    if len(digits_only) <= 9 and cleaned.replace(" ", "").isdigit():
        return False, "ትክክል አይደለም"
    
    # 3. ፊደል ብቻ ከሆነ
    if not re.search(r'\d', cleaned):
        return False, "ትክክል አይደለም"
        
    if len(cleaned) < 15:
        return False, "ትክክል አይደለም"
        
    return True, "valid"

def count_pending_requests():
    try:
        return transactions.count_documents({"status": "pending"})
    except Exception:
        return 0

game_states = {
    "10": {
        "status": "lobby", "timer": 30, "ball_timer": 3, "pot": 0, "players": {}, 
        "sold_tickets": {}, "current_ball": "--", "drawn_balls": [], "winner": None,
        "winning_card": None, "winning_ticket_num": None, "winning_indices": None,
        "winning_line_name": None, "all_cards": {}, "disqualified": {}
    },
    "20": {
        "status": "lobby", "timer": 30, "ball_timer": 3, "pot": 0, "players": {}, 
        "sold_tickets": {}, "current_ball": "--", "drawn_balls": [], "winner": None,
        "winning_card": None, "winning_ticket_num": None, "winning_indices": None,
        "winning_line_name": None, "all_cards": {}, "disqualified": {}
    },
    "50": {
        "status": "lobby", "timer": 30, "ball_timer": 3, "pot": 0, "players": {}, 
        "sold_tickets": {}, "current_ball": "--", "drawn_balls": [], "winner": None,
        "winning_card": None, "winning_ticket_num": None, "winning_indices": None,
        "winning_line_name": None, "all_cards": {}, "disqualified": {}
    },
    "super": { 
        "status": "lobby", "timer": 30, "ball_timer": 3, "pot": 0, "players": {}, 
        "sold_tickets": {}, "current_ball": "--", "drawn_balls": [], "winner": None,
        "winning_card": None, "winning_ticket_num": None, "winning_indices": None,
        "winning_line_name": None, "all_cards": {}, "disqualified": {}
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

def send_telegram(text, reply_markup=None, target_chat=None):
    chat = target_chat if target_chat else ADMIN_ID
    def _send():
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": chat, "text": text, "parse_mode": "Markdown"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            requests.post(url, json=payload, timeout=3)
        except Exception as e:
            print(f"Telegram Error: {e}")
    gevent.spawn(_send)

def set_webhook():
    if not BOT_TOKEN:
        return
    webhook_url = f"{WEB_APP_URL}/webhook"
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={webhook_url}"
    try:
        requests.get(url, timeout=3)
    except Exception as e:
        print(f"Webhook set failed: {e}")

def set_bot_commands():
    if not BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setMyCommands"
    default_commands = [
        {"command": "play", "description": "🎮 ጨዋታ ይምረጡ "},
        {"command": "deposit", "description": "📥 ሂሳብ ለመሙላት (Deposit)"},
        {"command": "withdraw", "description": "📤 ብር ወጪ ለማድረግ (Withdraw)"},
        {"command": "transfer", "description": "💸 ብር ለማስተላለፍ (Transfer)"},
        {"command": "balance", "description": "💰 ቀሪ (Balance) "},
        {"command": "history", "description": "📥 የትራንዛክሽን ታሪክ "},
        {"command": "instruction", "description": "ℹ️ የጨዋታ ህጎች "}
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
        "active_players": len(state["players"]),
        "disqualified": state.get("disqualified", {})
    }
    socketio.emit(f'game_update_{room_type}' if room_type != "super" else 'game_update', state_payload)

def notify_user_balance_update(phone_num, new_balance):
    socketio.emit('balance_update', {"phone": phone_num, "balance": new_balance})

def check_winning_line(card, drawn_numbers):
    drawn_set = set()
    for b in drawn_numbers:
        if len(b) > 1:
            try:
                drawn_set.add(int(b[1:]))
            except ValueError:
                pass
    drawn_set.add(0) 

    def is_hit(idx):
        val = card[idx]
        if idx == 12 or val == 0 or val == "Besh" or val == "★":
            return True
        try:
            return int(val) in drawn_set
        except:
            return False

    all_win_indices = set()
    line_types = []
    
    # 1. አግድም መስመር (Horizontal Line)
    for i in range(5):
        row_indices = [i*5 + j for j in range(5)]
        if all(is_hit(idx) for idx in row_indices):
            all_win_indices.update(row_indices)
            line_types.append(f"አግድም መስመር {i+1}")
            
    # 2. ቋሚ መስመር (Vertical Line)
    for j in range(5):
        col_indices = [j + i*5 for i in range(5)]
        if all(is_hit(idx) for idx in col_indices):
            all_win_indices.update(col_indices)
            line_types.append(f"ቋሚ መስመር {j+1}")
            
    # 3. ሰያፍ መስመር (Diagonal Line)
    diag1_indices = [0, 6, 12, 18, 24]
    if all(is_hit(idx) for idx in diag1_indices):
        all_win_indices.update(diag1_indices)
        line_types.append("ሰያፍ መስመር ↘")
        
    diag2_indices = [4, 8, 12, 16, 20]
    if all(is_hit(idx) for idx in diag2_indices):
        all_win_indices.update(diag2_indices)
        line_types.append("ሰያፍ መስመር ↙")
        
    # 4. አራት ማዕዘን (Four Corners)
    corner_indices = [0, 4, 20, 24]
    if all(is_hit(idx) for idx in corner_indices):
        all_win_indices.update(corner_indices)
        line_types.append("አራት ማዕዘን (4 Corners)")

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
    
    game_states[room_type].update({
        "status": "lobby", "winner": None, "winning_card": None, "winning_ticket_num": None, 
        "winning_indices": None, "winning_line_name": None, "pot": 0, "players": {}, 
        "sold_tickets": {}, "drawn_balls": [], "current_ball": "--", "timer": 30, "ball_timer": 3, 
        "all_cards": {}, "disqualified": {}
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
            # 30 ወደ 0 ሰከንድ countdown
            for i in range(30, -1, -1):
                if state["status"] != "lobby": 
                    break
                state["timer"] = i
                if last_broadcasted_timer != i:
                    last_broadcasted_timer = i
                    broadcast_game_state(room_type) 
                socketio.sleep(1) 
            
            # 0 ሲደርስ 2 እና ከዚያ በላይ ተጫዋች ካለ ወደ game ui ይገባል፤ ካልሆነ እንደገና 30 ይቆጥራል
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

            # 3 ሰከንድ ክብ ላይ ይቆጥራል የመጀመሪያው ቁጥር እስከሚጠራ
            if shuffled:
                for j in range(3, -1, -1):
                    if state["status"] != "playing":
                        break
                    state["ball_timer"] = j
                    broadcast_game_state(room_type) 
                    socketio.sleep(1)

                # በየ 5 ሰከንድ ጋፕ አዲስ ቁጥር ይጠራል
                for b in shuffled:
                    if state["status"] != "playing": 
                        break
                    if len(state["players"]) < 2:
                        state["status"] = "result"
                        state["winner"] = "ተጫዋቾች በቂ ስላልሆኑ ጨዋታው ተሰርዟል"
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
                    socketio.sleep(5) 
            
            if state["status"] == "playing":
                state["status"] = "result"
                state["winner"] = "አሸናፊ አልተገኘም (No Winner)"
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

# ምዝገባ & ሎጊን - በራሱ ስም/ዩዘርናም የሚያስተካክል
@app.route('/register_or_login', methods=['POST'])
def register_or_login():
    data = request.json or {}
    telegram_id = sanitize_input(str(data.get('telegram_id', '')).strip())
    raw_phone = sanitize_input(data.get('phone', ''))
    phone = format_ethiopian_phone(raw_phone)
    first_name = sanitize_input(data.get('first_name', ''))
    username = sanitize_input(data.get('username', ''))
    referred_by = sanitize_input(data.get('referred_by', ''))

    if not telegram_id and not phone:
        return jsonify({"success": False, "msg": "Telegram ID ወይም ስልክ ቁጥር ያስፈልጋል!"}), 400

    query = {}
    if telegram_id:
        query["telegram_id"] = telegram_id
    elif phone:
        query["phone"] = phone

    user = wallets.find_one(query)

    update_fields = {
        "status": "ACTIVE",
        "last_login": datetime.utcnow()
    }
    if first_name:
        update_fields["first_name"] = first_name
        update_fields["name"] = first_name
    if username:
        update_fields["username"] = username
    if telegram_id:
        update_fields["telegram_id"] = telegram_id
        update_fields["chat_id"] = telegram_id
    if phone:
        update_fields["phone"] = phone

    if user:
        wallets.update_one({"_id": user["_id"]}, {"$set": update_fields})
        current_user = wallets.find_one({"_id": user["_id"]})
    else:
        insert_fields = {
            **update_fields,
            "balance": 20.00, # መነሻ ቀሪ ሂሳብ 20.00
            "status": "ACTIVE",
            "created_at": datetime.utcnow(),
            "first_name": first_name or f"Player_{telegram_id[-4:] if telegram_id else 'Guest'}",
            "username": username or "",
        }
        if referred_by:
            insert_fields["referred_by"] = referred_by
        wallets.insert_one(insert_fields)
        current_user = insert_fields

    return jsonify({
        "success": True,
        "balance": current_user.get("balance", 20.00),
        "username": current_user.get("username", ""),
        "first_name": current_user.get("first_name", ""),
        "phone": current_user.get("phone", ""),
        "has_phone": bool(current_user.get("phone")),
        "telegram_id": current_user.get("telegram_id", "")
    })

@app.route('/update_phone_number', methods=['POST'])
def update_phone_number():
    data = request.json or {}
    telegram_id = sanitize_input(str(data.get('telegram_id', '')).strip())
    raw_phone = sanitize_input(data.get('phone', ''))
    phone = format_ethiopian_phone(raw_phone)

    if not phone or len(phone) < 9:
        return jsonify({"success": False, "msg": "ትክክለኛ ስልክ ቁጥር ያስገቡ!"}), 400

    existing_phone_user = wallets.find_one({"phone": phone})
    if existing_phone_user and str(existing_phone_user.get("telegram_id")) != telegram_id:
        return jsonify({"success": False, "msg": "ይህ ስልክ ቁጥር ለሌላ ተጠቃሚ አስቀድሞ ተመዝግቧል!"}), 400

    if telegram_id:
        wallets.update_one(
            {"telegram_id": telegram_id},
            {"$set": {"phone": phone, "last_updated": datetime.utcnow()}},
            upsert=True
        )
    return jsonify({"success": True, "phone": phone, "msg": "ስልክ ቁጥርዎ በተሳካ ሁኔታ ተመዝግቧል!"})

@app.route('/get_status')
def get_status():
    room = request.args.get('room', '10')
    state = game_states.get(room, game_states["10"])
    
    phone = sanitize_input(request.args.get('phone'))
    telegram_id = sanitize_input(request.args.get('telegram_id'))

    user = None
    if telegram_id:
        user = wallets.find_one({"telegram_id": telegram_id})
    if not user and phone:
        user = wallets.find_one({"phone": format_ethiopian_phone(phone)})

    user_balance = user.get('balance', 20.00) if user else 20.00
    user_phone = user.get('phone', '') if user else ''

    user_identifier = user_phone or (user.get('telegram_id') if user else '')
    p_data = state["players"].get(user_identifier, {"cards": {}})
    clean_players = {k: {"username": v.get("username", ""), "cards": list(v.get("cards", {}).values())} for k, v in state["players"].items()}

    return jsonify({
        "success": True,
        "user_exists": bool(user),
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
        "balance": user_balance, 
        "my_cards": list(p_data["cards"].values()), 
        "active_players": len(state["players"]),
        "has_phone": bool(user_phone),
        "phone": user_phone,
        "disqualified": state.get("disqualified", {}).get(user_identifier, [])
    })

# ካርቴላ መግዛት - በቂ ባላንስ ከሌለው ይከለክላል፤ ከፍተኛው 2 ካርድ
@app.route('/buy_specific_ticket', methods=['POST'])
def buy_ticket():
    d = request.json or {}
    raw_phone = sanitize_input(d.get('phone'))
    telegram_id = sanitize_input(str(d.get('telegram_id', '')))
    t_num = str(d.get('ticket_num'))
    uname = sanitize_input(d.get('username'))
    first_name = sanitize_input(d.get('first_name'))
    room = str(d.get('room', '10'))

    user = None
    if telegram_id:
        user = wallets.find_one({"telegram_id": telegram_id})
    if not user and raw_phone:
        user = wallets.find_one({"phone": format_ethiopian_phone(raw_phone)})

    if not user:
        return jsonify({"success": False, "msg": "ተጠቃሚው አልተገኘም!"})

    user_key = user.get("phone") or user.get("telegram_id")
    state = game_states.get(room, game_states["10"])
    price = 10 if room == "10" else (20 if room == "20" else 50)

    if state["status"] != "lobby":
        return jsonify({"success": False, "msg": "ጨዋታ ተጀምሯል!"})

    if t_num in state["sold_tickets"]:
        return jsonify({"success": False, "msg": "ይህ ካርቴላ ቀድሞ ተይዟል!"})
        
    if user_key in state["players"] and len(state["players"][user_key]["cards"]) >= 2:
        return jsonify({"success": False, "msg": "ከ 2 ካርቴላ በላይ መግዛት አይቻልም!"})

    # በቂ ሂሳብ ከሌለው ካርቴላ ሲነካ "በቂ ሒሳብ የለህም" ይበለው
    if user.get("balance", 0) < price:
        return jsonify({"success": False, "msg": "በቂ ሒሳብ የለህም! እባክዎ Deposit ያድርጉ።"})

    state["sold_tickets"][t_num] = "RESERVED_LOCK"

    res = wallets.find_one_and_update(
        {"_id": user["_id"], "balance": {"$gte": price}}, 
        {"$inc": {"balance": -price}},
        return_document=True
    )
    if res:
        columns = [random.sample(range(r[0], r[1]+1), 5) for r in [(1,15), (16,30), (31,45), (46,60), (61,75)]]
        flat = [columns[c][r] for r in range(5) for c in range(5)]
        flat[12] = 0  
        
        state["sold_tickets"][t_num] = user_key
        state["pot"] += price
        state.setdefault("all_cards", {})[t_num] = flat
        
        player_name = first_name or uname or res.get("first_name") or f"User_{user_key[-4:]}"
        if user_key not in state["players"]:
            state["players"][user_key] = {"cards": {t_num: flat}, "username": player_name}
        else:
            state["players"][user_key]["cards"][t_num] = flat
                
        if user.get("phone"):
            gevent.spawn(notify_user_balance_update, user.get("phone"), res.get("balance", 0))
        gevent.spawn(broadcast_game_state, room)
        return jsonify({"success": True, "balance": res.get("balance", 0), "card": flat})
    
    if state["sold_tickets"].get(t_num) == "RESERVED_LOCK":
        del state["sold_tickets"][t_num]
    return jsonify({"success": False, "msg": "በቂ ሒሳብ የለህም!"})

# ካርድ ደግመው በመንካት ሰርዝ ማድረግ (Refund)
@app.route('/cancel_ticket', methods=['POST'])
def cancel_ticket():
    d = request.json or {}
    raw_phone = sanitize_input(d.get('phone'))
    telegram_id = sanitize_input(str(d.get('telegram_id', '')))
    t_num = str(d.get('ticket_num'))
    room = str(d.get('room', '10'))

    user = None
    if telegram_id:
        user = wallets.find_one({"telegram_id": telegram_id})
    if not user and raw_phone:
        user = wallets.find_one({"phone": format_ethiopian_phone(raw_phone)})

    state = game_states.get(room, game_states["10"])
    price = 10 if room == "10" else (20 if room == "20" else 50)

    if not user or state["status"] != "lobby":
        return jsonify({"success": False, "msg": "ጨዋታው ከጀመረ በኋላ ካርቴላ መመለስ አይቻልም!"})

    user_key = user.get("phone") or user.get("telegram_id")
    if state["sold_tickets"].get(t_num) == user_key:
        res = wallets.find_one_and_update({"_id": user["_id"]}, {"$inc": {"balance": price}}, return_document=True)
        state["pot"] -= price
        del state["sold_tickets"][t_num]
        state.get("all_cards", {}).pop(t_num, None)
        if user_key in state["players"]:
            state["players"][user_key]["cards"].pop(t_num, None)
            if not state["players"][user_key]["cards"]: 
                state["players"].pop(user_key, None)
        if res and user.get("phone"):
            gevent.spawn(notify_user_balance_update, user.get("phone"), res.get("balance", 0))
        gevent.spawn(broadcast_game_state, room) 
        return jsonify({"success": True, "balance": res.get("balance", 0) if res else 0})
    return jsonify({"success": False})

# BINGO Claim - 0.5s Micro-buffer & Bogus Bingo Disqualification
@app.route('/claim_bingo', methods=['POST'])
def claim_bingo():
    global claim_lock_active, pending_claims
    d = request.json or {}
    raw_phone = sanitize_input(d.get('phone'))
    telegram_id = sanitize_input(str(d.get('telegram_id', '')))
    t_num = str(d.get('ticket_num'))
    room = str(d.get('room', '10'))
    state = game_states.get(room, game_states["10"])

    user = None
    if telegram_id:
        user = wallets.find_one({"telegram_id": telegram_id})
    if not user and raw_phone:
        user = wallets.find_one({"phone": format_ethiopian_phone(raw_phone)})

    if not user:
        return jsonify({"success": False, "msg": "ተጠቃሚው አልተገኘም!"})
    user_key = user.get("phone") or user.get("telegram_id")

    if state["status"] not in ["playing", "result"]:
        return jsonify({"success": False, "msg": "ጨዋታው በሂደት ላይ አይደለም!"})
        
    p_data = state["players"].get(user_key)
    if not p_data:
        return jsonify({"success": False, "msg": "ተጫዋቹ አልተገኘም!"})

    disqualified_cards = state.setdefault("disqualified", {}).setdefault(user_key, [])
    if t_num in disqualified_cards:
        return jsonify({"success": False, "msg": "ይህ ካርቴላ ውድቅ (Disqualified) ሆኗል!"})

    current_drawn_balls = state["drawn_balls"]
    card = p_data["cards"].get(t_num)
    if not card:
        return jsonify({"success": False, "msg": "ካርቴላው አልተገኘም!"})

    win_indices, line_type = check_winning_line(card, current_drawn_balls)

    # ሳይሞሉ BINGO ከነኩ Disqualified ይደረጋል
    if win_indices is None:
        disqualified_cards.append(t_num)
        broadcast_game_state(room)
        return jsonify({
            "success": False, 
            "disqualified": True, 
            "msg": f"ካርቴላ #{t_num} የማሸነፊያ መስመር ስላልሞላ ውድቅ (Disqualified) ተደርጓል!"
        })

    claim_info = {
        "user_key": user_key,
        "user_id": user["_id"],
        "phone": user.get("phone", ""),
        "username": p_data["username"],
        "ticket_num": str(t_num),
        "card": card,
        "indices": win_indices,
        "line_name": line_type,
        "winning_ball": current_drawn_balls[-1] if current_drawn_balls else "--"
    }

    # 0.5 ሰከንድ Server Micro-Buffer
    if state["status"] == "playing":
        if not claim_lock_active[room]:
            claim_lock_active[room] = True
            state["status"] = "result"
            state["timer"] = 10
            pending_claims[room] = [claim_info]

            def process_claims_with_buffer():
                global claim_lock_active, pending_claims
                socketio.sleep(0.5) # 0.5s Race Condition Micro-buffer

                total_prize = state["pot"] * 0.8  
                num_winners = len(pending_claims[room])

                if num_winners == 1:
                    winner_display = f"{pending_claims[room][0]['username']} አሸንፏል"
                else:
                    winner_names = [c["username"] for c in pending_claims[room]]
                    winner_display = f"{' & '.join(winner_names)} አሸንፈዋል (በእኩል ተካፍለዋል)"

                state["winner"] = winner_display
                state["winning_card"] = pending_claims[room][0]["card"]  
                state["winning_ticket_num"] = pending_claims[room][0]["ticket_num"] 
                state["winning_indices"] = pending_claims[room][0]["indices"]
                state["winning_line_name"] = pending_claims[room][0]["line_name"] 

                def background_win_task():
                    if num_winners == 1:
                        w = pending_claims[room][0]
                        win_res = wallets.find_one_and_update(
                            {"_id": w["user_id"]}, 
                            {"$inc": {"balance": total_prize}}, 
                            return_document=True
                        )
                        if win_res and w["phone"]:
                            gevent.spawn(notify_user_balance_update, w["phone"], win_res.get("balance", 0))
                        send_telegram(f"🏆 *BINGO WINNER!*\n👤 {w['username']} | 📞 `{w['phone']}` | 🎫 #{w['ticket_num']}\n💰 Prize: `{total_prize:.2f} ETB`")
                    else:
                        share_prize = total_prize / num_winners
                        winner_texts = []
                        for w in pending_claims[room]:
                            w_res = wallets.find_one_and_update(
                                {"_id": w["user_id"]}, 
                                {"$inc": {"balance": share_prize}}, 
                                return_document=True
                            )
                            if w_res and w["phone"]:
                                gevent.spawn(notify_user_balance_update, w["phone"], w_res.get("balance", 0))
                            winner_texts.append(f"👤 {w['username']} (`{w['phone']}`) - 🎫 #{w['ticket_num']}")
                        send_telegram(f"🏆 *SHARED WINNERS ({num_winners})!*\n💰 Share: `{share_prize:.2f} ETB` each\n" + "\n".join(winner_texts))
                        
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

            socketio.start_background_task(process_claims_with_buffer)
        else:
            already_exists = any(c["user_key"] == user_key and c["ticket_num"] == t_num for c in pending_claims[room])
            if not already_exists:
                pending_claims[room].append(claim_info)

    return jsonify({"success": True})

# Deposit Request with SMS Validation & Rate Limit
@app.route('/request_deposit', methods=['POST'])
def request_deposit():
    d = request.json or {}
    telegram_id = sanitize_input(str(d.get('telegram_id', '')))
    raw_phone = sanitize_input(str(d.get('phone', '')))
    phone = format_ethiopian_phone(raw_phone)
    method = sanitize_input(str(d.get('method', 'TELE BIRR'))) 
    sms_text = str(d.get('sms_text', '')).strip()

    user = wallets.find_one({"$or": [{"telegram_id": telegram_id}, {"phone": phone}]})
    if not user or not user.get("phone"):
        return jsonify({"success": False, "phone_required": True, "msg": "ክፍያ ለመፈጸም እባክዎን ስልክ ቁጥርዎን ያጋሩ"})

    user_key = user.get("phone") or user.get("telegram_id")
    if not check_rate_limit(user_key):
        return jsonify({"success": False, "msg": "አንድ ሰው በ5 ደቂቃ ውስጥ 2 ጊዜ ብቻ ነው ጥያቄ መላክ የሚችለው።"})

    is_valid, validation_msg = is_valid_deposit_sms(sms_text)
    if not is_valid:
        return jsonify({"success": False, "msg": validation_msg})

    amt = 50.0
    match = re.search(r'(\d+(?:\.\d+)?)\s*(?:ETB|Birr|ብር)', sms_text, re.IGNORECASE)
    if match:
        amt = float(match.group(1))

    db_phone = user.get("phone")
    first_name = user.get("first_name", "Player")

    tx_res = transactions.insert_one({
        "telegram_id": user.get("telegram_id", ""),
        "phone": db_phone, 
        "first_name": first_name,
        "type": "deposit", 
        "amount": amt, 
        "status": "pending", 
        "method": method, 
        "sms_text": sms_text, 
        "timestamp": datetime.utcnow()
    })
    tx_ref = str(tx_res.inserted_id)

    pending_count = count_pending_requests()
    pending_btn_label = f"🚨 የሚጠብቁ ጥያቄዎች ({pending_count})" if pending_count > 0 else "⏳ የሚጠብቁ ጥያቄዎች (0)"
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    msg = (
        f"📥 *Deposit Request*\n"
        f"👤 First-name: {first_name}\n"
        f"📞 Phone: `{db_phone}`\n"
        f"💵 Amount: `{amt:,.2f} ETB`\n"
        f"💳 Method: {method}\n"
        f"📅 Date: {now_str}\n"
        f"📝 SMS: `{sms_text[:120]}`"
    )
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"app_dep_{tx_ref}_{db_phone}_{amt}"},
                {"text": "❌ Reject", "callback_data": f"rej_dep_{tx_ref}_{db_phone}"},
                {"text": "🚫 Ban", "callback_data": f"ban_user_{db_phone}"}
            ],
            [
                {"text": pending_btn_label, "callback_data": "admin_pending_req"}
            ]
        ]
    }
    send_telegram(msg, reply_markup=keyboard)
    return jsonify({"success": True, "msg": "የዲፖዚት ጥያቄዎ ለአድሚን ተልኳል!"})

# Withdrawal Request
@app.route('/request_withdrawal', methods=['POST'])
def request_withdrawal():
    d = request.json or {}
    telegram_id = sanitize_input(str(d.get('telegram_id', '')))
    raw_phone = sanitize_input(str(d.get('phone', '')))
    phone = format_ethiopian_phone(raw_phone)
    method = sanitize_input(str(d.get('method', 'TELE BIRR')))
    account_number = sanitize_input(str(d.get('account_number', '')))
    account_holder = sanitize_input(str(d.get('account_holder', '')))

    try:
        amt = float(d.get('amount', 0))
    except ValueError:
        return jsonify({"success": False, "msg": "ትክክለኛ የገንዘብ መጠን ያስገቡ!"})

    if amt < 20: 
        return jsonify({"success": False, "msg": "ዝቅተኛው የማውጣት መጠን 20 ETB ነው!"})

    user = wallets.find_one({"$or": [{"telegram_id": telegram_id}, {"phone": phone}]})
    if not user or not user.get("phone"):
        return jsonify({"success": False, "phone_required": True, "msg": "ክፍያ ለመፈጸም እባክዎን ስልክ ቁጥርዎን ያጋሩ"})

    user_key = user.get("phone") or user.get("telegram_id")
    if not check_rate_limit(user_key):
        return jsonify({"success": False, "msg": "በ5 ደቂቃ ውስጥ 2 ጊዜ ብቻ ነው መጠየቅ የሚቻለው።"})

    if user.get("balance", 0) < amt:
        return jsonify({"success": False, "msg": "በቂ ቀሪ ሂሳብ የለዎትም!"})

    # ባላንስ እንዳይባክን ወዲያውኑ ይቀነሳል
    wallets.update_one({"_id": user["_id"]}, {"$inc": {"balance": -amt}})
    notify_user_balance_update(user.get("phone"), user.get("balance", 0) - amt)

    db_phone = user.get("phone")
    first_name = user.get("first_name", "Player")

    tx_res = transactions.insert_one({
        "telegram_id": user.get("telegram_id", ""),
        "phone": db_phone, 
        "first_name": first_name,
        "type": "withdrawal", 
        "amount": amt, 
        "status": "pending", 
        "method": method,
        "account_number": account_number or db_phone,
        "account_holder": account_holder or first_name,
        "timestamp": datetime.utcnow()
    })
    tx_ref = str(tx_res.inserted_id)

    pending_count = count_pending_requests()
    pending_btn_label = f"🚨 የሚጠብቁ ጥያቄዎች ({pending_count})" if pending_count > 0 else "⏳ የሚጠብቁ ጥያቄዎች (0)"
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    msg = (
        f"📤 *Withdrawal Request*\n"
        f"👤 First-name: {first_name}\n"
        f"📞 Phone: `{db_phone}`\n"
        f"💵 Amount: `{amt:,.2f} ETB`\n"
        f"💳 Method: {method}\n"
        f"🏦 Account: `{account_number or db_phone}` ({account_holder or first_name})\n"
        f"📅 Date: {now_str}"
    )
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"app_wit_{tx_ref}_{db_phone}_{amt}"},
                {"text": "❌ Reject", "callback_data": f"rej_wit_{tx_ref}_{db_phone}_{amt}"},
                {"text": "🚫 Ban", "callback_data": f"ban_user_{db_phone}"}
            ],
            [
                {"text": pending_btn_label, "callback_data": "admin_pending_req"}
            ]
        ]
    }
    send_telegram(msg, reply_markup=keyboard)
    return jsonify({"success": True, "msg": "የውዝድሮዋል ጥያቄዎ ለአድሚን ተልኳል!"})

# አውቶማቲክ P2P ትራንስፈር
@app.route('/request_transfer', methods=['POST'])
def request_transfer():
    d = request.json or {}
    sender_ph = format_ethiopian_phone(sanitize_input(d.get('phone')))
    sender_tg = sanitize_input(str(d.get('telegram_id', '')))
    receiver_input = sanitize_input(d.get('receiver_phone', ''))
    
    try:
        amt = float(d.get('amount', 0))
    except ValueError:
        return jsonify({"success": False, "msg": "ትክክለኛ የገንዘብ መጠን ያስገቡ!"})
    
    if amt <= 0:
        return jsonify({"success": False, "msg": "እባክዎ ከ 0 በላይ መጠን ያስገቡ!"})

    sender = wallets.find_one({"$or": [{"telegram_id": sender_tg}, {"phone": sender_ph}]})
    if not sender or sender.get("balance", 0) < amt:
        return jsonify({"success": False, "msg": "በቂ ቀሪ ሂሳብ የለዎትም!"})

    receiver_clean_phone = format_ethiopian_phone(receiver_input)
    receiver = wallets.find_one({
        "$or": [
            {"phone": receiver_clean_phone},
            {"telegram_id": receiver_input},
            {"username": receiver_input.replace("@", "")}
        ]
    })
    if not receiver:
        return jsonify({"success": False, "msg": "ተቀባዩ አልተገኘም!"})

    # sender != receiver
    if str(sender["_id"]) == str(receiver["_id"]):
        return jsonify({"success": False, "msg": "ለራስዎ ማስተላለፍ አይችሉም!"})

    # Atomic Transaction
    wallets.update_one({"_id": sender["_id"]}, {"$inc": {"balance": -amt}})
    wallets.update_one({"_id": receiver["_id"]}, {"$inc": {"balance": amt}})

    transactions.insert_one({
        "type": "transfer",
        "sender_id": str(sender["_id"]),
        "receiver_id": str(receiver["_id"]),
        "amount": amt,
        "status": "success",
        "timestamp": datetime.utcnow()
    })

    if sender.get("phone"):
        notify_user_balance_update(sender.get("phone"), sender.get("balance", 0) - amt)
    if receiver.get("phone"):
        notify_user_balance_update(receiver.get("phone"), receiver.get("balance", 0) + amt)

    return jsonify({"success": True, "msg": f"{amt:,.2f} ETB በሰከንዶች ውስጥ በተሳካ ሁኔታ ተላልፏል!"})

# Webhook & Bot Menu
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json or {}
    if "message" in data:
        msg = data["message"]
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "").strip()
        first_name = msg.get("from", {}).get("first_name", "Player")
        username = msg.get("from", {}).get("username", "")

        # Contact sharing
        if "contact" in msg:
            c_phone = format_ethiopian_phone(msg["contact"].get("phone_number"))
            wallets.update_one({"telegram_id": chat_id}, {"$set": {"phone": c_phone}}, upsert=True)
            send_telegram(f"✅ ስልክ ቁጥርዎ `{c_phone}` ተመዝግቧል!", target_chat=chat_id)
            return "OK", 200

        # Auto-update user name
        wallets.update_one(
            {"telegram_id": chat_id},
            {"$set": {"first_name": first_name, "username": username, "last_seen": datetime.utcnow()},
             "$setOnInsert": {"balance": 20.00, "status": "ACTIVE", "created_at": datetime.utcnow()}},
            upsert=True
        )

        # 3 የመልእክት ማውጫ Menu ከታች
        reply_menu = {
            "keyboard": [
                [{"text": "🎮 ጨዋታ ጀምር (Play)"}],
                [{"text": "📥 Deposit (ገንዘብ ጨምር)"}, {"text": "📤 Withdraw (ገንዘብ አውጣ)"}],
                [{"text": "💰 ቀሪ ሂሳብ (Balance)"}, {"text": "💸 Transfer (አስተላልፍ)"}]
            ],
            "resize_keyboard": True
        }

        if text.startswith("/start") or text == "🎮 ጨዋታ ጀምር (Play)":
            play_kb = {
                "inline_keyboard": [
                    [{"text": "🎮 PLAY | VIP (10 ETB)", "web_app": {"url": f"{WEB_APP_URL}"}}],
                    [{"text": "🎮 PLAY | 20 ETB", "web_app": {"url": f"{WEB_APP_URL}/twenty"}}],
                    [{"text": "🎮 PLAY | 50 ETB", "web_app": {"url": f"{WEB_APP_URL}/super"}}]
                ]
            }
            send_telegram(f"እንኳን ወደ *Besh Bingo* በደህና መጡ! 🎉\n👤 ስም: *{first_name}*\n💰 መነሻ ስጦታ: *20.00 ETB*", reply_markup=reply_menu, target_chat=chat_id)
            send_telegram("🎮 ለመጫወት ክፍል ይምረጡ፦", reply_markup=play_kb, target_chat=chat_id)
            return "OK", 200

        if text in ["/deposit", "📥 Deposit (ገንዘብ ጨምር)"]:
            dep_kb = {
                "inline_keyboard": [
                    [{"text": "📱 TELE-BIRR", "callback_data": "user_select_tele_dep"},
                     {"text": "🏦 CBE-BIRR", "callback_data": "user_select_cbe_dep"}]
                ]
            }
            send_telegram("💳 *የማስገቢያ መንገድ ይምረጡ፦*", reply_markup=dep_kb, target_chat=chat_id)
            return "OK", 200

    # Callback Query (Admin Approvals & Edit buttons)
    elif "callback_query" in data:
        cq = data["callback_query"]
        cq_id = cq["id"]
        chat_id = str(cq["message"]["chat"]["id"])
        data_str = cq.get("data", "")
        message_id = cq["message"]["message_id"]

        answer_url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
        edit_url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"

        if data_str == "user_select_tele_dep":
            tele_msg = (
                "የ TELE-Birr አካውንት  0960736061   (ZEHARA)\n\n"
                "መመሪያ\n"
                "1. ከላይ ባለው የ TELE-Birr አካውንት (ለግብይት ለመክፈል) ወይም (Send to-individual) በሚለው አማራጭ ገንዘቡን ያስገቡ\n"
                "2. ብሩን ስትልኩ የከፈላችሁበትን መረጃ የያዘ አጭር የጹሁፍ መልክት(sms) ከ TELE-Birr ይደርሳችኋል\n"
                "3. የደረሳችሁን አጭር የጹሁፍ መለክት(sms) ሙሉዉን ኮፒ(copy) በማረግ ከታች ባለው የቴሌግራም የጹሁፍ ማስገቢአው ላይ ፔስት(paste) በማረግ ይላኩት\n\n"
                "የሚያጋጥማቹ የክፍያ ችግር ካለ @BeshBingoSupport በዚ ሰፖርት ማዉራት ይችላሉ"
            )
            send_telegram(tele_msg, target_chat=chat_id)
            requests.post(answer_url, json={"callback_query_id": cq_id})
            return "OK", 200

        if data_str == "user_select_cbe_dep":
            cbe_msg = (
                "የ CBE-Birr አካውንት\nCBE-BIRR - 0960736061   (ZEHARA)\n\n"
                "መመሪያ\n"
                "1. ከላይ ባለው የ CBE-Birr አካውንት Send money-individual በሚለው ገንዘቡን ያስገቡ\n"
                "2. ብሩን ስትልኩ የከፈላችሁበትን መረጃ የያዘ አጭር የጹሁፍ መልክት(sms) ከ CBE ይደርሳችኋል\n"
                "3. የደረሳችሁን አጭር የጹሁፍ መለክት(sms) ሙሉዉን ኮፒ(copy) በማረግ ከታች ባለው የቴሌግራም የጹሁፍ ማስገቢአው ላይ ፔስት(paste) በማረግ ይላኩት\n\n"
                "የሚያጋጥማቹ የክፍያ ችግር ካለ @BeshBingoSupport በዚ ሳፖርት ማዉራት ይችላሉ"
            )
            send_telegram(cbe_msg, target_chat=chat_id)
            requests.post(answer_url, json={"callback_query_id": cq_id})
            return "OK", 200

        # Admin Actions: Approve Deposit
        if chat_id == str(ADMIN_ID) and data_str.startswith("app_dep_"):
            parts = data_str.split("_")
            tx_id_str, phone_num, amt = parts[2], parts[3], float(parts[4])
            tx_updated = transactions.find_one_and_update(
                {"_id": ObjectId(tx_id_str), "status": "pending"},
                {"$set": {"status": "approved", "processed_at": datetime.utcnow()}}
            )
            if tx_updated:
                user_updated = wallets.find_one_and_update({"phone": phone_num}, {"$inc": {"balance": amt}}, return_document=True)
                new_bal = user_updated.get("balance", 0) if user_updated else amt
                notify_user_balance_update(phone_num, new_bal)
                now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

                # Inline buttons ይወገዳሉ/ቋሚ ጽሁፍ ይሆናሉ እንዳያሳስት
                requests.post(edit_url, json={
                    "chat_id": ADMIN_ID,
                    "message_id": message_id,
                    "text": f"✅ *APPROVED (ተቀባይነት አግኝቷል)*\n📞 `{phone_num}` | 💵 `{amt:.2f} ETB` | 📅 {now_str}\n💰 አዲስ ባላንስ: `{new_bal:.2f} ETB`\n*(Buttons ተወግደዋል)*",
                    "parse_mode": "Markdown"
                })

                # ለተጫዋቹ መረጃ ይላካል
                if user_updated and user_updated.get("telegram_id"):
                    send_telegram(
                        f"✅ *የዲፖዚት ጥያቄዎ ጸድቋል!*\n\n💵 የተጨመረው ብር: *{amt:.2f} ETB*\n📅 ቀን: *{now_str}*\n💳 መንገድ: *{tx_updated.get('method')}*\n💰 አጠቃላይ ቀሪ ሂሳብዎ: *{new_bal:.2f} ETB*",
                        target_chat=user_updated.get("telegram_id")
                    )
            return "OK", 200

        # Admin Actions: Reject Deposit
        if chat_id == str(ADMIN_ID) and data_str.startswith("rej_dep_"):
            parts = data_str.split("_")
            transactions.update_one({"_id": ObjectId(parts[2]), "status": "pending"}, {"$set": {"status": "rejected"}})
            requests.post(edit_url, json={
                "chat_id": ADMIN_ID,
                "message_id": message_id,
                "text": f"❌ *REJECTED (ውድቅ ተደርጓል)*\n📞 `{parts[3]}`\n*(Buttons ተወግደዋል)*",
                "parse_mode": "Markdown"
            })
            return "OK", 200

        # Admin Actions: Approve Withdrawal
        if chat_id == str(ADMIN_ID) and data_str.startswith("app_wit_"):
            parts = data_str.split("_")
            transactions.update_one({"_id": ObjectId(parts[2]), "status": "pending"}, {"$set": {"status": "approved"}})
            requests.post(edit_url, json={
                "chat_id": ADMIN_ID,
                "message_id": message_id,
                "text": f"✅ *WITHDRAWAL APPROVED*\n📞 `{parts[3]}` | 💵 `{parts[4]} ETB`\n*(Buttons ተወግደዋል)*",
                "parse_mode": "Markdown"
            })
            return "OK", 200

        # Admin Actions: Reject Withdrawal (ገንዘቡ ይመለሳል)
        if chat_id == str(ADMIN_ID) and data_str.startswith("rej_wit_"):
            parts = data_str.split("_")
            amt = float(parts[4])
            transactions.update_one({"_id": ObjectId(parts[2]), "status": "pending"}, {"$set": {"status": "rejected"}})
            wallets.update_one({"phone": parts[3]}, {"$inc": {"balance": amt}})
            requests.post(edit_url, json={
                "chat_id": ADMIN_ID,
                "message_id": message_id,
                "text": f"❌ *WITHDRAWAL REJECTED (ብር ተመልሷል)*\n📞 `{parts[3]}` | 💵 `{amt:.2f} ETB` ተመልሷል\n*(Buttons ተወግደዋል)*",
                "parse_mode": "Markdown"
            })
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
    broadcast_game_state("10")

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
