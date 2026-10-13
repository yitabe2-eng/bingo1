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

def format_phone_number(phone_str):
    if not phone_str:
        return ""
    clean = re.sub(r'[^0-9]', '', str(phone_str))
    if clean.startswith("251") and len(clean) == 12:
        return clean
    elif clean.startswith("0") and len(clean) == 10:
        return "251" + clean[1:]
    elif len(clean) == 9 and (clean.startswith("9") or clean.startswith("7")):
        return "251" + clean
    return clean

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
        {"command": "play", "description": "🎮 ጨዋታ ይምረጡ"},
        {"command": "deposit", "description": "📥 ሂሳብ ለመሙላት (Deposit)"},
        {"command": "withdraw", "description": "📤 ብር ወጪ ለማድረግ (Withdraw)"},
        {"command": "transfer", "description": "💸 ብር ለማስተላለፍ (Transfer)"},
        {"command": "balance", "description": "💰 ቀሪ (Balance)"},
        {"command": "history", "description": "📥 የትራንዛክሽን ታሪክ"},
        {"command": "instruction", "description": "ℹ️ የጨዋታ ህጎች"}
    ]
    try:
        requests.post(url, json={"commands": default_commands}, timeout=2)
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
        
        if chat_id != str(ADMIN_ID):
            wallets.update_one(
                {"chat_id": chat_id}, 
                {"$set": {"chat_id": chat_id}}, 
                upsert=False
            )
        
        # ሌላ ትዕዛዝ ሲነካ (ለምሳሌ /play, /withdraw ወዘተ) የነበረውን የደፖሲት ሂደት/ስቴት ማጽዳት
        if text.startswith("/"):
            admin_state.delete_one({"chat_id": chat_id})
        
        # 1. ተጠቃሚው ስልክ ቁጥሩን በ Contact Sharing ሲያጋራ
        if "contact" in msg:
            contact = msg["contact"]
            raw_phone = contact.get("phone_number", "")
            formatted_phone = format_phone_number(raw_phone)
            telegram_id = str(contact.get("user_id", chat_id))
            first_name = contact.get("first_name", "User")
            username = msg.get("from", {}).get("username", "")
            
            existing_user = wallets.find_one({"phone": formatted_phone})
            if existing_user and existing_user.get("telegram_id") and existing_user.get("telegram_id") != telegram_id:
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
                requests.post(url, json={
                    "chat_id": chat_id,
                    "text": "❌ ይህ ስልክ ቁጥር አስቀድሞ በሌላ የቴሌግራም አካውንት ተመዝግቧል!"
                }, timeout=2)
                return "OK", 200
            
            wallets.update_one(
                {"phone": formatted_phone},
                {
                    "$set": {
                        "telegram_id": telegram_id,
                        "first_name": first_name,
                        "username": username if username else first_name,
                        "chat_id": chat_id,
                        "status": "ACTIVE"
                    },
                    "$setOnInsert": {
                        "balance": 20.00,
                        "created_at": datetime.utcnow()
                    }
                },
                upsert=True
            )
            
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            requests.post(url, json={
                "chat_id": chat_id,
                "text": "✅ ምዝገባዎ ተጠናቋል!\n🎁 የ 20.00 ETB መነሻ ቀሪ ሂሳብ ተሰጥቶዎታል።",
                "parse_mode": "Markdown",
                "reply_markup": {"remove_keyboard": True}
            }, timeout=2)

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
            requests.post(url, json={
                "chat_id": chat_id,
                "text": "🕹 *PLAY IN:*\nጨዋታ ለመጀመር ከታች ካሉት ክፍሎች አንዱን ይምረጡ፦",
                "parse_mode": "Markdown",
                "reply_markup": play_keyboard
            }, timeout=2)
            return "OK", 200

        # የአድሚን ስቴት (ለምሳሌ የደፖሲት SMS ተጠቃሚው ሲልክ)
        state = admin_state.find_one({"chat_id": chat_id})
        if chat_id != str(ADMIN_ID) and not text.startswith("/"):
            # ተጠቃሚው የደፖሲት SMS (Transaction ID / Text) ልኳል ተብሎ ይታሰባል
            # 1ቁጥር ብቻ ከሆነ, ከዘጠኝ ብቻ ከሆነ, ወይም ፊደል ብቻ ከሆነ የተሳሳተ ማሳሰቢያ እንዳያሳይ ችላ ይላል
            if text.isdigit() and len(set(text)) == 1:
                return "OK", 200  # 1 ቁጥር ብቻ (ለምሳሌ 111111) ከሆነ ዝም ይላል
            if text.isalpha():
                return "OK", 200  # ፊደል ብቻ ከሆነ ዝም ይላል
            
            # 5 ደቂቃ ውስጥ 2 ጊዜ ብቻ ሪኬስት መላክ መቻሉን ማረጋገጥ
            five_mins_ago = datetime.utcnow() - timedelta(minutes=5)
            recent_reqs_count = transactions.count_documents({
                "chat_id": chat_id,
                "timestamp": {"$gte": five_mins_ago}
            })
            if recent_reqs_count >= 2:
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": "❌ በ 5 ደቂቃ ውስጥ ከ 2 ጊዜ በላይ ጥያቄ መላክ አይችሉም! እባክዎ ትንሽ ይጠብቁ።"
                }, timeout=2)
                return "OK", 200

            user_wallet = wallets.find_one({"chat_id": chat_id})
            if user_wallet:
                db_phone = user_wallet.get("phone")
                first_name = user_wallet.get("first_name", "User")
                
                # ከ SMS ውስጥ መጠንን እና ሜቶድ መፈለግ (በናሙና 50 ብር እና Telebirr ተደርጎ ይወሰዳል)
                amount = 50.0  
                method = "TELE BIRR"
                if "cbe" in text.lower():
                    method = "CBE BIRR"

                tx_res = transactions.insert_one({
                    "chat_id": chat_id,
                    "phone": db_phone,
                    "first_name": first_name,
                    "type": "deposit",
                    "amount": amount,
                    "status": "pending",
                    "method": method,
                    "tx_id": text,
                    "timestamp": datetime.utcnow()
                })
                tx_ref = str(tx_res.inserted_id)
                date_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

                admin_msg = (
                    f"📥 *새 የዲፖዚት ጥያቄ (New Deposit Request)*\n"
                    f"👤 First Name: {first_name}\n"
                    f"📞 Phone: `{db_phone}`\n"
                    f"💵 Amount: `{amount}` ETB\n"
                    f"💳 Method: {method}\n"
                    f"⏱ Date: {date_str}\n"
                    f"🆔 SMS: `{text}`"
                )
                
                kb = {
                    "inline_keyboard": [
                        [
                            {"text": "✅ Approve", "callback_data": f"app_dep_{tx_ref}_{db_phone}_{amount}"},
                            {"text": "❌ Reject", "callback_data": f"rej_dep_{tx_ref}_{db_phone}"}
                        ],
                        [
                            {"text": "🚫 Ban", "callback_data": f"ban_user_{db_phone}"}
                        ]
                    ]
                }
                if ADMIN_ID:
                    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                        "chat_id": ADMIN_ID,
                        "text": admin_msg,
                        "parse_mode": "Markdown",
                        "reply_markup": kb
                    }, timeout=2)

                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": "⏳ የዲፖዚት ጥያቄዎ ተመዝግቧል (PENDING)፤ አድሚኑ ሲያጸድቀው ይገለጽልዎታል።"
                }, timeout=2)
                return "OK", 200

        if chat_id == str(ADMIN_ID):
            state = admin_state.find_one({"chat_id": chat_id})
            if state and state.get("action") == "awaiting_agent_phone":
                agent_phone = sanitize_input(text)
                admin_state.delete_one({"chat_id": chat_id})
                bot_info = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe").json()
                bot_username = bot_info.get("result", {}).get("username", "superbeshbingobot")
                agent_link = f"https://t.me/{bot_username}?start={agent_phone}"
                send_telegram(f"🔗 *ለኤጀንት የተዘጋጀ ሊንክ፦*\n\n📞 የኤጀንት ስልክ: `{agent_phone}`\n👇 የሪፈራል ሊንክ:\n`{agent_link}`")
                return "OK", 200

        if text.lower() in ["/deposit", "/withdraw"]:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            action_name = "ለማስገባት" if text.lower() == "/deposit" else "ወጪ ለማድረግ"
            dep_keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "CBE BIRR", "callback_data": f"user_select_cbe_{text[1:]}"},
                        {"text": "TELE BIRR", "callback_data": f"user_select_tele_{text[1:]}"}
                    ]
                ]
            }
            msg_text = f"💳 **የማስገቢያ መንገድ ይምረጡ**\n\nእባክዎ ሂሳብ {action_name} የሚጠቀሙበትን መንገድ ይምረጡ፦ እንዲሁም የደረሰዎትን SMS እዚህ ይላኩ።"
            requests.post(url, json={
                "chat_id": chat_id,
                "text": msg_text,
                "parse_mode": "Markdown",
                "reply_markup": dep_keyboard
            }, timeout=2)
            return "OK", 200

        if text.lower() == "/balance":
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            u_wallet = wallets.find_one({"chat_id": chat_id})
            if not u_wallet:
                requests.post(url, json={"chat_id": chat_id, "text": "❌ ምንም የተመዘገበ አካውንት አልተገኘም።"}, timeout=2)
            else:
                u_phone = u_wallet.get("phone", "N/A")
                u_balance = u_wallet.get("balance", 0)
                bal_msg = f"💰 *የሂሳብ መግለጫ (Balance)*\n\n📞 ስልክ ቁጥር: `{u_phone}`\n💵 ቀሪ ባላንስዎ: *{u_balance:,.2f} ETB*"
                requests.post(url, json={"chat_id": chat_id, "text": bal_msg, "parse_mode": "Markdown"}, timeout=2)
            return "OK", 200

        if text.lower().startswith("/play") or text.lower().startswith("/start"):
            user_exist = wallets.find_one({"chat_id": chat_id})
            if user_exist:
                formatted_phone = user_exist.get("phone")
                u_10 = f"{WEB_APP_URL}?phone={formatted_phone}"
                play_keyboard = {
                    "inline_keyboard": [
                        [{"text": "🎮 PLAY | በ10 ብር", "web_app": {"url": u_10}}], 
                    ]
                }
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": "🕹 *PLAY IN:*",
                    "parse_mode": "Markdown",
                    "reply_markup": play_keyboard
                }, timeout=2)
            else:
                contact_keyboard = {
                    "keyboard": [
                        [{"text": "📱 ስልክ ቁጥርዎን ያጋሩ", "request_contact": True}]
                    ],
                    "resize_keyboard": True,
                    "one_time_keyboard": True
                }
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": "👋 እባክዎ ስልክ ቁጥርዎን ያጋሩ።",
                    "reply_markup": contact_keyboard
                }, timeout=2)
            return "OK", 200

    elif "callback_query" in data:
        cq = data["callback_query"]
        cq_id = cq["id"]
        chat_id = str(cq["message"]["chat"]["id"])
        data_str = cq.get("data", "")
        
        answer_url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
        msg_send_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

        if chat_id == str(ADMIN_ID):
            if data_str.startswith("app_dep_"):
                parts = data_str.split("_")
                tx_id_str = parts[2]
                phone_num = format_phone_number(parts[3])
                amt = float(parts[4])

                tx_updated = transactions.find_one_and_update(
                    {"_id": ObjectId(tx_id_str), "status": "pending"},
                    {"$set": {"status": "approved", "timestamp": datetime.utcnow()}}
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
                    
                    user_chat_id = updated.get("chat_id")
                    if user_chat_id:
                        date_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                        method = tx_updated.get("method", "TELE BIRR")
                        user_msg = (
                            f"✅ *የዲፖዚት ጥያቄዎ ጸድቋል!*\n\n"
                            f"💵 የተጨመረው ብር: *{amt} ETB*\n"
                            f"💳 វិធី (Method): {method}\n"
                            f"⏱ ቀን (Date): {date_str}\n"
                            f"💰 አጠቃላይ ቀሪ ባላንስዎ: *{new_bal} ETB*"
                        )
                        requests.post(msg_send_url, json={
                            "chat_id": user_chat_id,
                            "text": user_msg,
                            "parse_mode": "Markdown"
                        }, timeout=2)

                    requests.post(answer_url, json={"callback_query_id": cq_id, "text": f"Approved! {amt} ETB ገብቷል።"})

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
