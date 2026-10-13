import os
import time
from datetime import datetime, timedelta
import gevent
from gevent import monkey
monkey.patch_all()

import random
import requests
import re
import hmac
import hashlib
from urllib.parse import parse_qsl
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

client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=2000)
db = client['bingo_db']
wallets = db['wallets']
blocked_phones = db['blocked_phones'] 
transactions = db['transactions']
admin_state = db['admin_state']

try:
    wallets.create_index("phone", unique=True)
    wallets.create_index("telegram_id", unique=True, sparse=True)
    blocked_phones.create_index("phone", unique=True)
    transactions.create_index("timestamp")
except Exception as e:
    print(f"Index creation notice: {e}")

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

def format_phone_number(phone_str):
    if not phone_str:
        return ""
    cleaned = re.sub(r'\D', '', str(phone_str))
    if cleaned.startswith('251') and len(cleaned) == 12:
        return cleaned
    elif cleaned.startswith('09') and len(cleaned) == 10:
        return '251' + cleaned[1:]
    elif cleaned.startswith('9') and len(cleaned) == 9:
        return '251' + cleaned
    return cleaned

def verify_telegram_init_data(init_data):
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        if 'hash' not in parsed:
            return None
        check_hash = parsed.pop('hash')
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if calculated_hash == check_hash:
            import json
            if 'user' in parsed:
                return json.loads(parsed['user'])
    except Exception as e:
        print(f"initData verification error: {e}")
    return None

def send_telegram(text, reply_markup=None):
    def _send():
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": ADMIN_ID, "text": text, "parse_mode": "Markdown"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            requests.post(url, json=payload, timeout=2)
        except Exception as e:
            print(f"Telegram Error: {e}")
    gevent.spawn(_send)

def set_webhook():
    webhook_url = f"{WEB_APP_URL}/webhook"
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={webhook_url}"
    try:
        requests.get(url, timeout=2)
    except Exception as e:
        print(f"Webhook set failed: {e}")

def set_bot_commands():
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
        requests.post(url, json={"commands": default_commands}, timeout=2)
    except Exception as e:
        print(f"Error setting default commands: {e}")

    if ADMIN_ID:
        admin_commands = [
            {"command": "play", "description": "🎮 play "},
            {"command": "deposit", "description": "📥 Deposit"},
            {"command": "withdraw", "description": "📤 Withdraw"},
            {"command": "transfer", "description": "💸 Transfer"},
            {"command": "admin", "description": "🛠 Admin"},
            {"command": "pending", "description": "⏳ Approvals"},
            {"command": "agent", "description": "👥 Agent h"},
            {"command": "daily", "description": "📅 D/W ገቢና ወጪ"},
            {"command": "balance", "description": "💰 Balance"},
            {"command": "history", "description": "📥 History "},
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
            requests.post(url, json=payload, timeout=2)
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
        {"$match": {"type": "withdrawal", "status": "success", "timestamp": {"$gte": today_start}}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
    ]))
    week_wit = list(transactions.aggregate([
        {"$match": {"type": "withdrawal", "status": "success", "timestamp": {"$gte": week_start}}},
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
                for j in range(3, -1, -1):
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
    user = wallets.find_one({"phone": phone}) if phone else None
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
    
    for r_key, r_state in game_states.items():
        if r_key != room and ph in r_state["players"]:
            return jsonify({"success": False, "msg": f"በአንድ ጊዜ ከ 1 ሩም በላይ መጫወት አይቻልም! (አሁን ሌላ ጨዋታ ውስጥ አሉ)"})

    state = game_states.get(room, game_states["10"])
    price = 10 if room == "10" else (20 if room == "20" else 50)

    if not ph or not t_num:
        return jsonify({"success": False, "msg": "የተሳሳተ መረጃ!"})
    user = wallets.find_one({"phone": ph})
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
    
    if room == "super":
        return jsonify({"success": False, "msg": "ካርተላ ከተገዛ በኋላ መመለስ (ሰረዝ ማድረግ) አይቻልም!"})

    state = game_states.get(room, game_states["10"])
    price = 10 if room == "10" else 20

    user = wallets.find_one({"phone": ph})
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
    room = str(d.get('room', '10'))
    state = game_states.get(room, game_states["10"])
    
    user_info = wallets.find_one({"phone": ph})
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

@app.route('/request_deposit', methods=['POST'])
def request_deposit():
    return jsonify({"success": True})

@app.route('/request_withdrawal', methods=['POST'])
def request_withdrawal():
    return jsonify({"success": True})

@app.route('/request_transfer', methods=['POST'])
def request_transfer():
    d = request.json or {}
    sender_ph = sanitize_input(d.get('phone'))
    receiver_ph = sanitize_input(d.get('receiver_phone'))
    try:
        amt = float(d.get('amount', 0))
    except ValueError:
        return jsonify({"success": False, "msg": "ትክክለኛ መጠን ያስገቡ!"})
    
    if amt <= 0:
        return jsonify({"success": False, "msg": "እባክዎ ትክክለኛ የብር መጠን ያስገቡ!"})

    sender = wallets.find_one({"phone": sender_ph})
    if not sender or sender.get("balance", 0) < amt:
        return jsonify({"success": False, "msg": "በቂ ባላንስ የለዎትም!"})
    db_sender_phone = sender["phone"]
    
    receiver = wallets.find_one({"phone": receiver_ph})
    if not receiver:
        return jsonify({"success": False, "msg": "ተቀባዩ አልተገኘም!"})
    db_receiver_phone = receiver["phone"]

    if db_sender_phone == db_receiver_phone:
        return jsonify({"success": False, "msg": "ለራስዎ ገንዘብ ማስተላለፍ አይችሉም!"})

    sender_updated = wallets.find_one_and_update(
        {"phone": db_sender_phone, "balance": {"$gte": amt}},
        {"$inc": {"balance": -amt}},
        return_document=True
    )
    if not sender_updated:
        return jsonify({"success": False, "msg": "በቂ ባላንስ የለዎትም!"})

    receiver_updated = wallets.find_one_and_update(
        {"phone": db_receiver_phone},
        {"$inc": {"balance": amt}},
        return_document=True,
        upsert=True
    )

    transactions.insert_one({
        "phone": db_sender_phone,
        "receiver_phone": db_receiver_phone,
        "type": "transfer",
        "amount": amt,
        "status": "success",
        "timestamp": datetime.utcnow()
    })

    notify_user_balance_update(db_sender_phone, sender_updated.get("balance", 0))
    if receiver_updated:
        notify_user_balance_update(db_receiver_phone, receiver_updated.get("balance", 0))

    return jsonify({"success": True, "msg": f"በተሳካ ሁኔታ {amt} ETB ተላልፏል!"})

@app.route('/register_or_login', methods=['POST'])
def register_or_login():
    data = request.json or {}
    init_data = data.get('initData', '')
    raw_phone = data.get('phone', '')
    referred_by = sanitize_input(data.get('referred_by'))

    telegram_user = verify_telegram_init_data(init_data)
    if not telegram_user:
        return jsonify({"success": False, "msg": "የቴሌግራም ማረጋገጫ (initData) አልተሳካም!"}), 400

    telegram_id = str(telegram_user.get('id'))
    first_name = sanitize_input(telegram_user.get('first_name', 'User'))
    username = sanitize_input(telegram_user.get('username', ''))

    clean_phone = format_phone_number(raw_phone)
    if not clean_phone:
        return jsonify({"success": False, "msg": "ትክክለኛ ስልክ ቁጥር አልተገኘም!"}), 400

    existing_phone_user = wallets.find_one({"phone": clean_phone})
    if existing_phone_user and str(existing_phone_user.get("telegram_id")) != telegram_id:
        return jsonify({"success": False, "msg": "ይህ ስልክ ቁጥር ከሌላ የቴሌግራም አካውንት ጋር ተመዝግቧል!"}), 400

    update_doc = {
        "$set": {
            "telegram_id": telegram_id,
            "phone": clean_phone,
            "first_name": first_name,
            "username": username,
            "status": "ACTIVE"
        },
        "$setOnInsert": {
            "balance": 20.00,
            "created_at": datetime.utcnow()
        }
    }
    if referred_by:
        update_doc["$set"]["referred_by"] = referred_by

    wallets.update_one(
        {"telegram_id": telegram_id},
        update_doc,
        upsert=True
    )

    updated_user = wallets.find_one({"telegram_id": telegram_id})
    return jsonify({
        "success": True, 
        "balance": updated_user.get("balance", 20.00),
        "username": updated_user.get("username", username)
    })

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json or {}
    
    if "message" in data:
        msg = data["message"]
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "")
        
        if chat_id != str(ADMIN_ID):
            wallets.update_one(
                {"chat_id": chat_id}, 
                {"$set": {"chat_id": chat_id}}, 
                upsert=False
            )
        
        if "contact" in msg:
            contact = msg["contact"]
            raw_phone = contact.get("phone_number", "")
            telegram_id = str(msg.get("from", {}).get("id", ""))
            first_name = sanitize_input(msg.get("from", {}).get("first_name", "User"))
            username = sanitize_input(msg.get("from", {}).get("username", ""))
            
            clean_phone = format_phone_number(raw_phone)
            
            if not clean_phone:
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": "❌ ስልክ ቁጥርዎን ማንበብ አልተቻለም። እባክዎ እንደገና ይሞክሩ።"
                })
                return "OK", 200

            existing_phone_user = wallets.find_one({"phone": clean_phone})
            if existing_phone_user and str(existing_phone_user.get("telegram_id")) != telegram_id:
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": "❌ ይህ ስልክ ቁጥር ከሌላ የቴሌግራም አካውንት ጋር ተመዝግቧል!"
                })
                return "OK", 200

            update_doc = {
                "$set": {
                    "telegram_id": telegram_id,
                    "phone": clean_phone,
                    "first_name": first_name,
                    "username": username,
                    "status": "ACTIVE",
                    "chat_id": chat_id
                },
                "$setOnInsert": {
                    "balance": 20.00,
                    "created_at": datetime.utcnow()
                }
            }

            wallets.update_one(
                {"telegram_id": telegram_id},
                update_doc,
                upsert=True
            )

            u_10 = f"{WEB_APP_URL}"
            u_20 = f"{WEB_APP_URL}/twenty"
            u_50 = f"{WEB_APP_URL}/super"
            u_super = f"{WEB_APP_URL}/superbesh"

            keyboard = {
                "inline_keyboard": [
                    [{"text": "🎮 PLAY | በ10 ብር", "web_app": {"url": u_10}}], 
                    [{"text": "🎮 PLAY | በ20 ብር", "web_app": {"url": u_20}}], 
                    [{"text": "🎮 PLAY | በ50 ብር", "web_app": {"url": u_50}}], 
                    [{"text": "🎮 BeshBingo (ሳምንታዊ) | 50 ብር", "web_app": {"url": u_super}}]
                ]
            }
            
            success_txt = (
                "✅ *ምዝገባዎ በተሳካ ሁኔታ ተጠናቋል!*\n"
                "💰 የ 20.00 ETB መነሻ ቦነስ ተሰጥაዎታል።\n\n"
                "🕹 *ጨዋታ ለመጀመር የሚፈልጉትን ሩም ይምረጡ፦*"
            )
            
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                "chat_id": chat_id,
                "text": success_txt,
                "parse_mode": "Markdown",
                "reply_markup": keyboard
            }, timeout=2)
            return "OK", 200

        user_wallet = wallets.find_one({"chat_id": chat_id})
        
        if user_wallet and user_wallet.get("state") == "awaiting_withdraw_amount":
            wallets.update_one({"chat_id": chat_id}, {"$unset": {"state": "", "method": ""}})
            method = user_wallet.get("pending_method", "TELEBIRR")
            try:
                amt = float(text)
            except ValueError:
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": "❌ ትክክለኛ የብር መጠን ቁጥር ብቻ ያስገቡ!"
                })
                return "OK", 200

            if amt < 20:
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": "❌ ቢያንስ 20 ETB ማውጣት ይችላሉ!"
                })
                return "OK", 200

            db_phone = user_wallet.get("phone")
            if user_wallet.get("balance", 0) < amt:
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": "❌ በቂ ባላንስ የለዎትም!"
                })
                return "OK", 200

            five_mins_ago = datetime.utcnow() - timedelta(minutes=5)
            recent_wits_count = transactions.count_documents({
                "phone": db_phone,
                "type": "withdrawal",
                "timestamp": {"$gte": five_mins_ago}
            })
            if recent_wits_count >= 2:
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": "❌ በ 5 ደቂቃ ውስጥ ከ 2 ጊዜ በላይ የዊዝድሮዋል ጥያቄ መላክ አይቻልም!"
                })
                return "OK", 200

            updated_user = wallets.find_one_and_update(
                {"phone": db_phone, "balance": {"$gte": amt}},
                {"$inc": {"balance": -amt}},
                return_document=True
            )
            if not updated_user:
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": "❌ በቂ ባላንስ የለዎትም!"
                })
                return "OK", 200

            notify_user_balance_update(db_phone, updated_user.get("balance", 0))

            tx_res = transactions.insert_one({
                "phone": db_phone,
                "type": "withdrawal",
                "amount": amt,
                "status": "pending",
                "method": method,
                "timestamp": datetime.utcnow()
            })
            tx_ref = str(tx_res.inserted_id)

            admin_msg = (
                f"📤 *Withdrawal Request*\n"
                f"👤 Name: {user_wallet.get('first_name', 'N/A')}\n"
                f"📞 Phone: `{db_phone}`\n"
                f"💵 Amount: `{amt}` ETB\n"
                f"💳 Method: {method}\n"
                f"📅 Date: {(datetime.utcnow() + timedelta(hours=3)).strftime('%Y-%m-%d %H:%M')}"
            )
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Approve", "callback_data": f"app_wit_{tx_ref}_{db_phone}_{amt}"},
                        {"text": "❌ Reject", "callback_data": f"rej_wit_{tx_ref}_{db_phone}_{amt}"},
                        {"text": "🚫 Ban", "callback_data": f"ban_user_{db_phone}"}
                    ]
                ]
            }
            send_telegram(admin_msg, reply_markup=keyboard)

            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                "chat_id": chat_id,
                "text": f"✅ የ **{amt} ETB** ዊዝድሮዋል ጥያቄዎ ለአድሚን ተልኳል። ሲረጋገጥ ይለቀቅልዎታል!"
            })
            return "OK", 200

        if user_wallet and text and chat_id != str(ADMIN_ID):
            is_only_digits = text.isdigit()
            is_only_letters = text.isalpha()
            is_only_9s = bool(re.match(r'^[9]+$', text))

            if is_only_digits or is_only_letters or is_only_9s:
                return "OK", 200

            db_phone = user_wallet.get("phone")
            
            five_mins_ago = datetime.utcnow() - timedelta(minutes=5)
            recent_deps_count = transactions.count_documents({
                "phone": db_phone,
                "type": "deposit",
                "timestamp": {"$gte": five_mins_ago}
            })
            if recent_deps_count >= 2:
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": "❌ በ 5 ደቂቃ ውስጥ ከ 2 ጊዜ በላይ ዲፖዚት ጥያቄ መላክ አይቻልም!"
                })
                return "OK", 200

            amt_match = re.search(r'(\d+[\d\.]*)\s*(ETB|ብር)', text, re.IGNORECASE)
            amt = float(amt_match.group(1)) if amt_match else 0.0

            tx_res = transactions.insert_one({
                "phone": db_phone,
                "type": "deposit",
                "amount": amt,
                "status": "pending",
                "method": "TELEBIRR/CBE",
                "sms_text": text,
                "timestamp": datetime.utcnow()
            })
            tx_ref = str(tx_res.inserted_id)

            admin_msg = (
                f"📥 *Deposit SMS Request*\n"
                f"👤 Name: {user_wallet.get('first_name', 'N/A')}\n"
                f"📞 Phone: `{db_phone}`\n"
                f"💵 Amount: `{amt}` ETB\n"
                f"📅 Date: {(datetime.utcnow() + timedelta(hours=3)).strftime('%Y-%m-%d %H:%M')}\n"
                f"💬 SMS: {text}"
            )
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Approve", "callback_data": f"app_dep_{tx_ref}_{db_phone}_{amt}"},
                        {"text": "❌ Reject", "callback_data": f"rej_dep_{tx_ref}_{db_phone}"},
                        {"text": "🚫 Ban", "callback_data": f"ban_user_{db_phone}"}
                    ]
                ]
            }
            send_telegram(admin_msg, reply_markup=keyboard)

            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                "chat_id": chat_id,
                "text": "✅ የዲፖዚት ኤስኤምኤስ ጥያቄዎ ለአድሚን ተልኳል። ሲረጋገጥ ባላንስዎ ይሞላል!"
            })
            return "OK", 200

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

        if text.lower() == "/deposit":
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            dep_keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "CBE BIRR", "callback_data": "user_select_cbe"},
                        {"text": "TELE BIRR", "callback_data": "user_select_tele"}
                    ]
                ]
            }
            requests.post(url, json={
                "chat_id": chat_id,
                "text": "💳 **የማስገቢያ መንገድ ይምረጡ (Select Deposit Method)**\n\nእባክዎ ሂሳብ ለማስገባት የሚጠቀሙበትን መንገድ ይምረጡ፦",
                "parse_mode": "Markdown",
                "reply_markup": dep_keyboard
            }, timeout=2)
            return "OK", 200

        if text.lower() == "/withdraw":
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            wit_keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "CBE BIRR", "callback_data": "user_wit_cbe"},
                        {"text": "TELE BIRR", "callback_data": "user_wit_tele"}
                    ]
                ]
            }
            requests.post(url, json={
                "chat_id": chat_id,
                "text": "📤 **የማውጫ መንገድ ይምረጡ (Select Withdrawal Method)**\n\nእባክዎ ገንዘብ ማውጣት የሚፈልጉበትን መንገድ ይምረጡ፦",
                "parse_mode": "Markdown",
                "reply_markup": wit_keyboard
            }, timeout=2)
            return "OK", 200

        if text.lower().startswith("/play") or text.lower().startswith("/start"):
            args = text.split()
            referred_by = sanitize_input(args[1]) if len(args) > 1 else None
            
            u_10 = f"{WEB_APP_URL}"
            u_20 = f"{WEB_APP_URL}/twenty"
            u_50 = f"{WEB_APP_URL}/super"
            u_super = f"{WEB_APP_URL}/superbesh"
            
            if referred_by:
                u_10 += f"?ref={referred_by}"
                u_20 += f"?ref={referred_by}"
                u_50 += f"?ref={referred_by}"
                u_super += f"?ref={referred_by}"

            keyboard = {
                "inline_keyboard": [
                    [{"text": "🎮 PLAY | በ10 ብር", "web_app": {"url": u_10}}], 
                    [{"text": "🎮 PLAY | በ20 ብር", "web_app": {"url": u_20}}], 
                    [{"text": "🎮 PLAY | በ50 ብር", "web_app": {"url": u_50}}], 
                    [{"text": "🎮 BeshBingo (ሳምንታዊ) | 50 ብር", "web_app": {"url": u_super}}],
                    [{"text": "⚽ BeshBingo Bonus", "callback_data": "Besh_bingo_bonus"}]
                ]
            }
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            requests.post(url, json={
                "chat_id": chat_id,
                "text": "🕹 *PLAY IN:*\nChoose a room to join the game:",
                "parse_mode": "Markdown",
                "reply_markup": keyboard
            }, timeout=2)
            return "OK", 200

        if text.lower().startswith("/transfer"):
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            parts = text.split()
            if len(parts) == 3:
                rec_phone = sanitize_input(parts[1])
                try:
                    amt_val = float(parts[2])
                    u_wallet = wallets.find_one({"chat_id": chat_id})
                    if not u_wallet:
                        requests.post(url, json={"chat_id": chat_id, "text": "❌ አካውንትዎ አልተገኘም። እባክዎ መጀመሪያ /play ብለው ይግቡ።"}, timeout=2)
                        return "OK", 200
                    
                    sender_phone = u_wallet.get("phone")
                    if sender_phone == rec_phone:
                        requests.post(url, json={"chat_id": chat_id, "text": "❌ ለራስዎ ገንዘብ ማስተላለፍ አይችሉም!"}, timeout=2)
                        return "OK", 200

                    if u_wallet.get("balance", 0) < amt_val:
                        requests.post(url, json={"chat_id": chat_id, "text": "❌ በቂ ባላንስ የለዎትም!"}, timeout=2)
                        return "OK", 200

                    rec_wallet = wallets.find_one({"phone": rec_phone})
                    if not rec_wallet:
                        requests.post(url, json={"chat_id": chat_id, "text": "❌ ተቀባዩ ተጠቃሚ በሲስተሙ አልተገኘም!"}, timeout=2)
                        return "OK", 200

                    sender_updated = wallets.find_one_and_update(
                        {"phone": sender_phone, "balance": {"$gte": amt_val}},
                        {"$inc": {"balance": -amt_val}},
                        return_document=True
                    )
                    if not sender_updated:
                        requests.post(url, json={"chat_id": chat_id, "text": "❌ በቂ ባላንስ የለዎትም!"}, timeout=2)
                        return "OK", 200

                    rec_updated = wallets.find_one_and_update(
                        {"phone": rec_phone},
                        {"$inc": {"balance": amt_val}},
                        return_document=True,
                        upsert=True
                    )

                    transactions.insert_one({
                        "phone": sender_phone,
                        "receiver_phone": rec_phone,
                        "type": "transfer",
                        "amount": amt_val,
                        "status": "success",
                        "timestamp": datetime.utcnow()
                    })

                    notify_user_balance_update(sender_phone, sender_updated.get("balance", 0))
                    if rec_updated:
                        notify_user_balance_update(rec_phone, rec_updated.get("balance", 0))

                    requests.post(url, json={
                        "chat_id": chat_id,
                        "text": f"✅ በተሳካ ሁኔታ **{amt_val} ETB** ለ `{rec_phone}` ተላልፏል!\n💰 ቀሪ ባላንስዎ፦ {sender_updated.get('balance', 0):,.2f} ETB",
                        "parse_mode": "Markdown"
                    }, timeout=2)
                except ValueError:
                    requests.post(url, json={"chat_id": chat_id, "text": "❌ የብር መጠን ስህተት ነው። ምሳሌ፦ `/transfer 0921523653 20`"}, timeout=2)
            else:
                msg_txt = (
                    "💸 **ብር ለማስተላለፍ (Transfer)**\n\n"
                    "የሚልኩለትን ስልክ እና ብር ያስገቡ ምሳሌ፦\n"
                    "`/transfer 0921523653 20`"
                )
                requests.post(url, json={"chat_id": chat_id, "text": msg_txt, "parse_mode": "Markdown"}, timeout=2)
            return "OK", 200

        if text.lower() == "/balance":
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            u_wallet = wallets.find_one({"chat_id": chat_id})
            if not u_wallet:
                requests.post(url, json={"chat_id": chat_id, "text": "❌ ምንም የተመዘገበ አካውንት አልተገኘም። እባክዎ መጀመሪያ /play በመጫን ዌብ-አፕ (Web App) ይክፈቱ!", "parse_mode": "Markdown"})
            else:
                u_phone = u_wallet.get("phone", "N/A")
                u_balance = u_wallet.get("balance", 0)
                bal_msg = f"💰 *የሂሳብ መግለጫ (Balance)*\n\n📞 ስልክ ቁጥር: `{u_phone}`\n💵 ቀሪ ባላንስዎ: *{u_balance:,.2f} ETB*"
                requests.post(url, json={"chat_id": chat_id, "text": bal_msg, "parse_mode": "Markdown"})
            return "OK", 200

        if text.lower() in ["/instruction", "/instraction"]:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            rules_msg = "ℹ️ **የጨዋታ ህጎች (Game Rules)**\n..."
            requests.post(url, json={"chat_id": chat_id, "text": rules_msg, "parse_mode": "Markdown"}, timeout=2)
            return "OK", 200

        if text.lower() == "/history":
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            if chat_id == str(ADMIN_ID):
                admin_state.update_one({"chat_id": ADMIN_ID}, {"$set": {"action": "awaiting_history_dates"}}, upsert=True)
                requests.post(url, json={"chat_id": chat_id, "text": "📅 የታሪክ ቀን እና ዓይነት ያስገቡ"})
                return "OK", 200
            return "OK", 200

        if text.lower().startswith("/broadcast") or text.lower().startswith("/broadcast1"):
            if chat_id == str(ADMIN_ID):
                admin_state.update_one({"chat_id": ADMIN_ID}, {"$set": {"action": "awaiting_broadcast"}}, upsert=True)
                send_telegram("📢 ፎቶ ብቻ ይላኩ፦")
            return "OK", 200

        if chat_id == str(ADMIN_ID):
            parts = text.split()
            cmd = parts[0] if parts else ""

            if cmd == "/admin":
                pending_count = transactions.count_documents({"status": "pending"})
                pending_label = f"🚨 የሚጠብቁ ጥያቄዎች ({pending_count})" if pending_count > 0 else "⏳ የሚጠብቁ ጥያቄዎች (0)"
                
                admin_keyboard = {
                    "inline_keyboard": [
                        [{"text": pending_label, "callback_data": "admin_pending_req"}],
                        [{"text": "🔗 የኤጀንት ሊንክ ማመንጫ", "callback_data": "admin_gen_agent_link"}],
                        [{"text": "👥 የኤጀንት ስታቲስቲክስ", "callback_data": "admin_agent_stats_guide"}],
                        [{"text": "📜 የትራንዛክሽን ታሪክ", "callback_data": "admin_history_req"}],
                        [{"text": "📅 የእለት/የሳምንት ገቢ እና ወጪ", "callback_data": "admin_financial_stats"}],
                        [{"text": "📋 የሁሉም ተጠቃሚዎች ባላንስ", "callback_data": "admin_show_all_bal"}]
                    ]
                }
                send_telegram("🛠 *የአድሚን ማኔጅመንት ሰሌዳ*", reply_markup=admin_keyboard)
                return "OK", 200

            elif cmd == "/daily":
                send_telegram(get_financial_stats())
                return "OK", 200

            elif cmd == "/agent":
                if len(parts) > 1:
                    agent_ph = sanitize_input(parts[1])
                    send_telegram(f"👥 የኤጀንት ሪፖርት ለ `{agent_ph}`")
                return "OK", 200

            elif cmd == "/pending":
                pendings = list(transactions.find({"status": "pending"}).limit(10))
                if not pendings:
                    send_telegram("✅ ምንም የሚጠብቅ ጥያቄ የለም!")
                else:
                    for p in pendings:
                        p_id = str(p.get("_id"))
                        p_type = p.get("type", "deposit").upper()
                        p_ph = p.get("phone")
                        p_amt = p.get("amount")
                        u_info = wallets.find_one({"phone": p_ph})
                        f_name = u_info.get("first_name", "N/A") if u_info else "N/A"
                        method_name = p.get("method", "TELEBIRR/CBE")
                        p_date = (p.get("timestamp") + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M") if p.get("timestamp") else "N/A"

                        btn_code = f"app_dep_{p_id}_{p_ph}_{p_amt}" if p_type == "DEPOSIT" else f"app_wit_{p_id}_{p_ph}_{p_amt}"
                        rej_code = f"rej_dep_{p_id}_{p_ph}" if p_type == "DEPOSIT" else f"rej_wit_{p_id}_{p_ph}_{p_amt}"
                        
                        kb = {
                            "inline_keyboard": [
                                [
                                    {"text": "✅ Approve", "callback_data": btn_code},
                                    {"text": "❌ Reject", "callback_data": rej_code},
                                    {"text": "🚫 Ban", "callback_data": f"ban_user_{p_ph}"}
                                ]
                            ]
                        }
                        send_telegram(
                            f"⏳ *የሚጠብቅ ጥያቄ ({p_type}):*\n"
                            f"👤 First Name: {f_name}\n"
                            f"📞 Phone: `{p_ph}`\n"
                            f"💵 Amount: `{p_amt}` ETB\n"
                            f"💳 Method: {method_name}\n"
                            f"📅 Date: {p_date}",
                            reply_markup=kb
                        )
                return "OK", 200

            elif cmd == "/block" and len(parts) > 1:
                blocked_phones.update_one({"phone": parts[1]}, {"$set": {"phone": parts[1]}}, upsert=True)
                send_telegram(f"✅ ስልክ ቁጥር ({parts[1]}) ብሎክ ተደርጓል!")
                return "OK", 200

            elif cmd == "/unblock" and len(parts) > 1:
                blocked_phones.delete_one({"phone": parts[1]})
                send_telegram(f"✅ ስልክ ቁጥር ({parts[1]}) ከብሎክ ተነስቷል!")
                return "OK", 200

    elif "callback_query" in data:
        cq = data["callback_query"]
        cq_id = cq["id"]
        chat_id = str(cq["message"]["chat"]["id"])
        data_str = cq.get("data", "")
        
        answer_url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
        msg_send_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

        if data_str == "Besh_bingo_bonus":
            requests.post(answer_url, json={"callback_query_id": cq_id, "text": "የቦነስ ፕሮግራም በቅርቡ ይጀመራል!", "show_alert": True})

        if data_str == "user_select_cbe":
            cbe_msg = "የ CBE-Birr አካውንት\nCBE-BIRR - 0960736061   (ZEHARA)\n\nመመሪያ:\n1. ገንዘቡን ያስገቡ\n2. የደረሰዎትን አጭር የጽሁፍ መልእክት (sms) ኮፒ በማድረግ እዚህ ላይ ይለጥፉ (paste)."
            requests.post(msg_send_url, json={"chat_id": chat_id, "text": cbe_msg}, timeout=2)
            requests.post(answer_url, json={"callback_query_id": cq_id})
            return "OK", 200

        if data_str == "user_select_tele":
            tele_msg = "የ TELE-Birr አካውንት\n0960736061   (ZEHARA)\n\nመመሪያ:\n1. ገንዘቡን ያስገቡ\n2. የደረሰዎትን አጭር የጽሁፍ መልእክት (sms) ኮፒ በማድረግ እዚህ ላይ ይለጥፉ (paste)."
            requests.post(msg_send_url, json={"chat_id": chat_id, "text": tele_msg}, timeout=2)
            requests.post(answer_url, json={"callback_query_id": cq_id})
            return "OK", 200

        if data_str in ["user_wit_cbe", "user_wit_tele"]:
            method_name = "CBE BIRR" if data_str == "user_wit_cbe" else "TELE BIRR"
            wallets.update_one(
                {"chat_id": chat_id},
                {"$set": {"state": "awaiting_withdraw_amount", "pending_method": method_name}}
            )
            requests.post(msg_send_url, json={
                "chat_id": chat_id,
                "text": f"📤 እባክዎ ከ **{method_name}** አካውንትዎ ማውጣት የሚፈልጉትን **የብር መጠን ብቻ** (ምሳሌ፦ 50) ጽሁፍ አድርገው ይላኩ፦"
            }, timeout=2)
            requests.post(answer_url, json={"callback_query_id": cq_id})
            return "OK", 200

        if chat_id == str(ADMIN_ID):
            if data_str.startswith("ban_user_"):
                b_phone = data_str.replace("ban_user_", "")
                blocked_phones.update_one({"phone": b_phone}, {"$set": {"phone": b_phone}}, upsert=True)
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": f"ስልክ ቁጥር {b_phone} ታግዷል!", "show_alert": True})
                send_telegram(f"🚫 ስልክ ቁጥር `{b_phone}` በባን (Ban) ተደርጓል!")
                return "OK", 200

            elif data_str == "admin_pending_req":
                pendings = list(transactions.find({"status": "pending"}).limit(10))
                if not pendings:
                    requests.post(answer_url, json={"callback_query_id": cq_id, "text": "ምንም የሚጠብቅ ጥያቄ የለም!", "show_alert": True})
                else:
                    requests.post(answer_url, json={"callback_query_id": cq_id, "text": "የሚጠብቁ ጥያቄዎች"})
                    for p in pendings:
                        p_id = str(p.get("_id"))
                        p_type = p.get("type", "deposit").upper()
                        p_ph = p.get("phone")
                        p_amt = p.get("amount")
                        u_info = wallets.find_one({"phone": p_ph})
                        f_name = u_info.get("first_name", "N/A") if u_info else "N/A"
                        method_name = p.get("method", "TELEBIRR/CBE")
                        p_date = (p.get("timestamp") + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M") if p.get("timestamp") else "N/A"

                        btn_code = f"app_dep_{p_id}_{p_ph}_{p_amt}" if p_type == "DEPOSIT" else f"app_wit_{p_id}_{p_ph}_{p_amt}"
                        rej_code = f"rej_dep_{p_id}_{p_ph}" if p_type == "DEPOSIT" else f"rej_wit_{p_id}_{p_ph}_{p_amt}"
                        
                        kb = {
                            "inline_keyboard": [
                                [
                                    {"text": "✅ Approve", "callback_data": btn_code},
                                    {"text": "❌ Reject", "callback_data": rej_code},
                                    {"text": "🚫 Ban", "callback_data": f"ban_user_{p_ph}"}
                                ]
                            ]
                        }
                        send_telegram(
                            f"⏳ *የሚጠብቅ ጥያቄ ({p_type}):*\n"
                            f"👤 First Name: {f_name}\n"
                            f"📞 Phone: `{p_ph}`\n"
                            f"💵 Amount: `{p_amt}` ETB\n"
                            f"💳 Method: {method_name}\n"
                            f"📅 Date: {p_date}",
                            reply_markup=kb
                        )

            elif data_str.startswith("app_dep_"):
                parts = data_str.split("_")
                tx_id_str = parts[2]
                phone_num = parts[3]
                amt = float(parts[4])
                
                try:
                    tx_updated = transactions.find_one_and_update(
                        {"_id": ObjectId(tx_id_str), "status": "pending"},
                        {"$set": {"status": "success", "timestamp": datetime.utcnow()}}
                    )
                except Exception:
                    tx_updated = transactions.find_one_and_update(
                        {"phone": phone_num, "status": "pending", "type": "deposit"},
                        {"$set": {"status": "success", "timestamp": datetime.utcnow()}}
                    )

                if tx_updated:
                    updated = wallets.find_one_and_update(
                        {"phone": phone_num}, 
                        {"$inc": {"balance": amt}}, 
                        return_document=True, 
                        upsert=True
                    )
                    new_bal = updated.get("balance", 0) if updated else 0
                    notify_user_balance_update(phone_num, new_bal)
                    
                    today_str = (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")
                    
                    try:
                        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText", json={
                            "chat_id": ADMIN_ID,
                            "message_id": cq["message"]["message_id"],
                            "text": f"✅ *DEPOSIT APPROVED*\n📞 Phone: `{phone_num}`\n💵 Amount: `{amt}` ETB\n📅 Date: {today_str}\nStatus: *SUCCESS*",
                            "parse_mode": "Markdown",
                            "reply_markup": {"inline_keyboard": []}
                        }, timeout=2)
                    except Exception:
                        pass

                    user_chat_id = updated.get("chat_id")
                    if user_chat_id:
                        try:
                            user_msg = (
                                f"✅ *የዲፖዚት ጥያቄዎ ጸድቋል!*\n\n"
                                f"📅 ቀን፦ {today_str}\n"
                                f"💳 ዘዴ፦ TELEBIRR/CBE\n"
                                f"💵 የተጨመረው ብር፦ *{amt} ETB*\n"
                                f"💰 አጠቃላይ ቀሪ ባላንስዎ፦ *{new_bal} ETB*"
                            )
                            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": user_chat_id,
                                "text": user_msg,
                                "parse_mode": "Markdown"
                            }, timeout=2)
                        except Exception as e:
                            print(f"Error sending user deposit notification: {e}")

                    requests.post(answer_url, json={"callback_query_id": cq_id, "text": f"Approved! {amt} ETB ገብቷል።"})
                else:
                    requests.post(answer_url, json={"callback_query_id": cq_id, "text": "⚠ ይህ ጥያቄ አስቀድሞ ፀድቋል ወይም ተሰርዟል!", "show_alert": True})
            
            elif data_str.startswith("rej_dep_"):
                parts = data_str.split("_")
                try:
                    tx_updated = transactions.find_one_and_update(
                        {"_id": ObjectId(parts[2]), "status": "pending"}, 
                        {"$set": {"status": "rejected"}}
                    )
                except Exception:
                    tx_updated = transactions.find_one_and_update(
                        {"phone": parts[3], "status": "pending", "type": "deposit"}, 
                        {"$set": {"status": "rejected"}}
                    )
                
                if tx_updated:
                    try:
                        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText", json={
                            "chat_id": ADMIN_ID,
                            "message_id": cq["message"]["message_id"],
                            "text": f"❌ *DEPOSIT REJECTED*\n📞 Phone: `{parts[3]}`\nStatus: *REJECTED*",
                            "parse_mode": "Markdown",
                            "reply_markup": {"inline_keyboard": []}
                        }, timeout=2)
                    except Exception:
                        pass
                
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "ዲፖዚት ጥያቄው ሪጀክት ተደርጓል።"})

            elif data_str.startswith("app_wit_"):
                parts = data_str.split("_")
                tx_id_str = parts[2]
                phone_num = parts[3]
                amt = float(parts[4])

                try:
                    tx_updated = transactions.find_one_and_update(
                        {"_id": ObjectId(tx_id_str), "status": "pending"},
                        {"$set": {"status": "success", "timestamp": datetime.utcnow()}}
                    )
                except Exception:
                    tx_updated = transactions.find_one_and_update(
                        {"phone": phone_num, "status": "pending", "type": "withdrawal"},
                        {"$set": {"status": "success", "timestamp": datetime.utcnow()}}
                    )

                if tx_updated:
                    today_str = (datetime.utcnow() + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")
                    try:
                        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText", json={
                            "chat_id": ADMIN_ID,
                            "message_id": cq["message"]["message_id"],
                            "text": f"✅ *WITHDRAWAL APPROVED*\n📞 Phone: `{phone_num}`\n💵 Amount: `{amt}` ETB\n📅 Date: {today_str}\nStatus: *SUCCESS*",
                            "parse_mode": "Markdown",
                            "reply_markup": {"inline_keyboard": []}
                        }, timeout=2)
                    except Exception:
                        pass

                    u_info = wallets.find_one({"phone": phone_num})
                    if u_info and u_info.get("chat_id"):
                        try:
                            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": u_info.get("chat_id"),
                                "text": f"✅ የውዝድሮዋል ጥያቄዎ ጸድቆ ገንዘቡ ተልኳል!\n📅 ቀን፦ {today_str}\n💵 መጠን፦ {amt} ETB",
                                "parse_mode": "Markdown"
                            }, timeout=2)
                        except: pass

                    requests.post(answer_url, json={"callback_query_id": cq_id, "text": "Withdrawal Approved!"})
                else:
                    requests.post(answer_url, json={"callback_query_id": cq_id, "text": "⚠ ይህ ጥያቄ አስቀድሞ ፀድቋል ወይም ተሰርዟል!", "show_alert": True})
            
            elif data_str.startswith("rej_wit_"):
                parts = data_str.split("_")
                tx_id_str = parts[2]
                phone_num = parts[3]
                amt = float(parts[4])

                try:
                    tx_updated = transactions.find_one_and_update(
                        {"_id": ObjectId(tx_id_str), "status": "pending"},
                        {"$set": {"status": "rejected"}}
                    )
                except Exception:
                    tx_updated = transactions.find_one_and_update(
                        {"phone": phone_num, "status": "pending", "type": "withdrawal"},
                        {"$set": {"status": "rejected"}}
                    )

                if tx_updated:
                    u_up = wallets.find_one_and_update(
                        {"phone": phone_num},
                        {"$inc": {"balance": amt}},
                        return_document=True
                    )
                    if u_up:
                        notify_user_balance_update(phone_num, u_up.get("balance", 0))

                    try:
                        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText", json={
                            "chat_id": ADMIN_ID,
                            "message_id": cq["message"]["message_id"],
                            "text": f"❌ *WITHDRAWAL REJECTED*\n📞 Phone: `{phone_num}`\n💵 Amount: `{amt}` ETB (Refunded)\nStatus: *REJECTED*",
                            "parse_mode": "Markdown",
                            "reply_markup": {"inline_keyboard": []}
                        }, timeout=2)
                    except Exception:
                        pass

                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "ዊዝድሮዋል ጥያቄው ሪጀክት ተደርጎ ብሩ ተመልሷል።"})

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
        socketio.start_background_task(super_game_loop) 
        
    broadcast_game_state("10")
    broadcast_game_state("20")
    broadcast_game_state("50")
    broadcast_game_state("super")

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
