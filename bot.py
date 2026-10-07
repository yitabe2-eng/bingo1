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
    blocked_phones.create_index("phone", unique=True)
    transactions.create_index("timestamp")
except Exception as e:
    print(f"Index creation notice: {e}")

# ለ 10፣ 20 እና 50 ብር ሩሞች የተዘጋጀ የጨዋታ ሁኔታዎች (Game States)
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
        {"command": "play", "description": "ጨዋታ ይምረጡ 🎮"},
        {"command": "balance", "description": "የሂሳብሪሣቤ (Balance) ለማየት 💰"},
        {"command": "history", "description": "የትራንዛክሽን ታሪክ ለማየት"},
        {"command": "instruction", "description": "ℹ️ የጨዋታ ህጎች እና የማሸነፊያ መንገዶች"}
    ]
    try:
        requests.post(url, json={"commands": default_commands}, timeout=2)
    except Exception as e:
        print(f"Error setting default commands: {e}")

    if ADMIN_ID:
        admin_commands = [
            {"command": "play", "description": "ጨዋታ ይምረጡ 🎮"},
            {"command": "balance", "description": "የሂሳብሪሣቤ (Balance) ለማየት 💰"},
            {"command": "history", "description": "የትራንዛክሽን ታሪክ ለማየት"},
            {"command": "instruction", "description": "ℹ️ የጨዋታ ህጎች እና የማሸነፊያ መንገዶች"},
            {"command": "admin", "description": "🛠 የአድሚን ማውጫ / Dashboard"},
            {"command": "pending", "description": "⏳ ጥያቄዎችን ለማፅደቅ (Approvals)"},
            {"command": "daily", "description": "📅 የእለት/የሳምንት ገቢና ወጪ"},
            {"command": "agent", "description": "👥 የኤጀንት ስታቲስቲክስ እና ሪፖርት"}
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

# ከ bot 59 የተወሰደ ጥብቅ የማሸነፊያ መስመር ማረጋገጫ (Strict Winning Line Logic)
def check_bingo_win_strict(card, drawn_balls):
    drawn_set = set()
    for b in drawn_balls:
        clean_b = re.sub(r'[^0-9]', '', str(b))
        if clean_b.isdigit():
            drawn_set.add(int(clean_b))

    marked = []
    for idx, val in enumerate(card):
        if idx == 12 or str(val).upper() in ["FREE", "★"] or str(val) == "0":
            marked.append(True)
        else:
            try:
                num_val = int(re.sub(r'[^0-9]', '', str(val)))
                marked.append(num_val in drawn_set)
            except:
                marked.append(False)

    winning_indices = []
    line_name = None

    # 1. አግድም መስመሮች (Horizontal Lines)
    for r in range(5):
        row_indices = [r * 5 + c for c in range(5)]
        if all(marked[i] for i in row_indices):
            winning_indices = row_indices
            line_name = f"አግድም መስመር {r+1}"
            return True, winning_indices, line_name

    # 2. ቋሚ መስመሮች (Vertical Lines)
    for c in range(5):
        col_indices = [r * 5 + c for r in range(5)]
        if all(marked[i] for i in col_indices):
            winning_indices = col_indices
            line_name = f"ቋሚ መስመር {c+1}"
            return True, winning_indices, line_name

    # 3. ዋና ሰያፍ መስመር (Main Diagonal)
    diag1 = [0, 6, 12, 18, 24]
    if all(marked[i] for i in diag1):
        winning_indices = diag1
        line_name = "ዋና ሰያፍ መስመር"
        return True, winning_indices, line_name

    # 4. ሁለተኛ ሰያፍ መስመር (Secondary Diagonal)
    diag2 = [4, 8, 12, 16, 20]
    if all(marked[i] for i in diag2):
        winning_indices = diag2
        line_name = "ሁለተኛ ሰያፍ መስመር"
        return True, winning_indices, line_name

    return False, [], None

def check_winning_line(card, drawn_numbers, player_marked_numbers=None):
    return check_bingo_win_strict(card, drawn_numbers)

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
    if not user and room == "super":
        return jsonify({
            "success": False,
            "user_exists": False,
            "msg": "መለያዎ ከሲስተሙ ተሰርዟል! እባክዎ እንደገና ይመዝገቡ።"
        })
        
    db_phone = user['phone'] if user else phone
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
        "balance": user['balance'] if user else 0, 
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
            is_win, win_indices, line_type = check_bingo_win_strict(card, current_drawn_balls)
            if is_win:
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
                socketio.sleep(0.2 if room != "super" else 1.5)

                total_pot = state["pot"]
                total_prize = total_pot * 0.8  
                house_commission = total_pot * 0.2

                if house_commission > 0 and room != "super":
                    transactions.insert_one({
                        "type": "game_commission",
                        "amount": house_commission,
                        "pot_amount": total_pot,
                        "timestamp": datetime.utcnow()
                    })

                num_winners = len(pending_claims[room])

                if num_winners == 1:
                    winner_display = f"{pending_claims[room][0]['username']} አሸንፏል" if room != "super" else pending_claims[room][0]["username"]
                else:
                    winner_names = [c["username"] for c in pending_claims[room]]
                    winner_display = f"{' & '.join(winner_names)} አሸንፈዋል" if room != "super" else " & ".join(winner_names)

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
                        
                        w_user_doc = wallets.find_one({"phone": w["phone"]})
                        agent_info = f"\n📲 የያዘው ኤጀንት: `{w_user_doc.get('referred_by', 'የለም')}`" if w_user_doc and "referred_by" in w_user_doc and room == "super" else ""

                        success_msg = f"🏆 *WINNER (Room {room} ETB)!* \n👤 Name: {w['username']} | 📞 Phone: `{w['phone']}` | 🎫 Ticket: {w['ticket_num']}{agent_info} \n🎯 Winning Ball: {w['winning_ball']} \n💰 Prize Won: {total_prize:.2f} ETB"
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
                            if room == "super":
                                w_user_doc = wallets.find_one({"phone": w["phone"]})
                                ag_ph = f" (ኤጀንት: `{w_user_doc.get('referred_by')}`)" if w_user_doc and "referred_by" in w_user_doc else ""
                                winner_texts.append(f"👤 {w['username']} (`{w['phone']}`){ag_ph} - 🎫 {w['ticket_num']}")
                                
                        if room == "super":
                            success_msg = f"🏆 *WINNERS (Shared Prize on Ball {pending_claims[room][0]['winning_ball']})!* \n💰 Total Pot Share: {share_prize:.2f} ETB each ({num_winners} winners)\n" + "\n".join(winner_texts)
                        else:
                            success_msg = f"🏆 *WINNERS (Room {room}) Shared!* 💰 {share_prize:.2f} ETB each"
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
            if not any(c["phone"] == db_phone for c in pending_claims[room]):
                pending_claims[room].append(claim_info)

    elif state["status"] == "result" and claim_lock_active[room]:
        if not any(c["phone"] == db_phone for c in pending_claims[room]):
            pending_claims[room].append(claim_info)

    return jsonify({"success": True})

@app.route('/request_deposit', methods=['POST'])
def request_deposit():
    d = request.json or {}
    ph = sanitize_input(str(d.get('phone')))
    method = sanitize_input(str(d.get('method', 'TELE BIRR'))) 
    room = sanitize_input(str(d.get('room', '10')))
    try:
        amt = float(d.get('amount', 0))
    except ValueError:
        amt = 0
    t_id = sanitize_input(d.get('transaction_id', 'N/A'))
    
    if t_id.isdigit():
        return jsonify({"success": False, "msg": "የትራንዛክሽን አይድው ስህተት ነው! ቁጥር ብቻ መሆን አይችልም።"})
    if len(t_id) < 10:
        return jsonify({"success": False, "msg": "የትራንዛክሽን አይድው ስህተት ነው! ከ 10 ቁምፊዎች ማነስ የለበትም።"})

    user = wallets.find_one({"phone": ph})
    db_phone = user["phone"] if user else ph
    
    is_blocked = blocked_phones.find_one({"phone": db_phone})
    if is_blocked:
        return jsonify({"success": True, "msg": "የነጻዉ አልቋል በቴሌ ብር ወይም ሲቢኢ ብር ወደ 0945880474 ላክ"})

    tx_res = transactions.insert_one({
        "phone": db_phone, 
        "type": "deposit", 
        "amount": amt, 
        "status": "pending", 
        "method": method, 
        "tx_id": t_id, 
        "room": room, 
        "timestamp": datetime.utcnow()
    })
    tx_ref = str(tx_res.inserted_id)

    msg = f"💰 *Deposit Request (Room: {room})*\n📞 Phone: `{db_phone}`\n💵 Amount: `{amt}` ETB\n🆔 ID: `{t_id}`"
        
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ አረጋግጥ (Approve)", "callback_data": f"app_dep_{tx_ref}_{db_phone}_{amt}"},
                {"text": "❌ ሰርዝ (Reject)", "callback_data": f"rej_dep_{tx_ref}_{db_phone}"}
            ]
        ]
    }
    send_telegram(msg, reply_markup=keyboard)
    return jsonify({"success": True})

@app.route('/request_withdrawal', methods=['POST'])
def request_withdrawal():
    d = request.json or {}
    ph = sanitize_input(str(d.get('phone')))
    room = sanitize_input(str(d.get('room', '10')))
    try:
        amt = float(d.get('amount', 0))
    except ValueError:
        return jsonify({"success": False, "msg": "ትክክለኛ የገንዘብ መጠን ያስገቡ!"})
    if amt < 20: 
        return jsonify({"success": False, "msg": "ቢያንስ 20 ETB ማውጣት ይችላሉ!"})
    
    user = wallets.find_one({"phone": ph})
    if not user:
        return jsonify({"success": False, "msg": "ተጠቃሚው አልተገኘም!"})
    db_phone = user["phone"]
    
    if user.get("balance", 0) < amt:
        return jsonify({"success": False, "msg": "በቂ ባላንስ የለዎትም!"})

    tx_res = transactions.insert_one({
        "phone": db_phone, 
        "type": "withdrawal", 
        "amount": amt, 
        "status": "pending", 
        "room": room, 
        "timestamp": datetime.utcnow()
    })
    tx_ref = str(tx_res.inserted_id)

    msg = f"📤 *Withdrawal Request (Room: {room})*\n📞 Phone: `{db_phone}`\n💵 Amount: `{amt}` ETB"
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ አረጋግጥ (Approve)", "callback_data": f"app_wit_{tx_ref}_{db_phone}_{amt}"},
                {"text": "❌ ሰርዝ (Reject)", "callback_data": f"rej_wit_{tx_ref}_{db_phone}_{amt}"}
            ]
        ]
    }
    send_telegram(msg, reply_markup=keyboard)
    return jsonify({"success": True, "msg": "የውዝድሮዋል ጥያቄዎ ለአድሚን ተልኳል!"})

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

    notify_user_balance_update(db_sender_phone, sender_updated.get("balance", 0))
    if receiver_updated:
        notify_user_balance_update(db_receiver_phone, receiver_updated.get("balance", 0))

    transactions.insert_one({
        "phone": db_sender_phone, "receiver_phone": db_receiver_phone, 
        "type": "transfer", "amount": amt, "status": "approved", "timestamp": datetime.utcnow()
    })

    send_telegram(f"🔄 *Transfer Done*\n📤 From: `{db_sender_phone}`\n📥 To: `{db_receiver_phone}`\n💵 Amount: `{amt}` ETB")
    return jsonify({"success": True, "msg": f"✅ {amt} ETB በትክክል ተላልፏል!"})

@app.route('/register_or_login', methods=['POST'])
def register_or_login():
    data = request.json or {}
    input_phone = sanitize_input(data.get('phone'))
    input_username = sanitize_input(data.get('username'))
    input_chat_id = str(data.get('chat_id', '')).strip()
    referred_by = sanitize_input(data.get('referred_by'))
    
    if not input_phone:
        return jsonify({"success": False, "msg": "እባክዎ ስልክ ቁጥር ያስገቡ!"}), 400

    clean_phone = input_phone.replace("+", "").replace(" ", "")
    fallback_name = input_username if input_username else f"User_{clean_phone[-4:]}"
    
    update_data = {
        "username": fallback_name, 
        "name": fallback_name,
        "phone": clean_phone
    }
    if input_chat_id:
        update_data["chat_id"] = input_chat_id
        
    if referred_by:
        update_data["referred_by"] = referred_by

    wallets.update_one(
        {"phone": clean_phone},
        {"$set": update_data, "$setOnInsert": {"balance": 0}},
        upsert=True
    )
    
    existing = wallets.find_one({"phone": clean_phone})
    return jsonify({
        "success": True, 
        "balance": existing.get("balance", 0) if existing else 0,
        "username": existing.get("username", fallback_name)
    })

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json or {}
    
    if "message" in data:
        msg = data["message"]
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "")
        
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

        if chat_id != str(ADMIN_ID):
            wallets.update_one({"chat_id": chat_id}, {"$set": {"chat_id": chat_id}}, upsert=False)
        
        if text.lower() == "/balance":
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            u_wallet = wallets.find_one({"chat_id": chat_id})
            if not u_wallet:
                requests.post(url, json={"chat_id": chat_id, "text": "❌ ምንም የተመዘገበ አካውንት አልተገኘም። እባክዎ መጀመሪያ ዌብ-አፕ (Web App) በመክፈት ይመዝገቡ!", "parse_mode": "Markdown"})
            else:
                u_phone = u_wallet.get("phone", "N/A")
                u_balance = u_wallet.get("balance", 0)
                bal_msg = f"💰 *የሂሳብሪሣቤ (Balance) መግለጫ*\n\n📞 ስልክ ቁጥር: `{u_phone}`\n💵 ቀሪ ባላንስዎ: *{u_balance:,.2f} ETB*\n\n🎮 በጨዋታ ለመሳተፍ /play የሚለውን ይጠቀሙ!"
                requests.post(url, json={"chat_id": chat_id, "text": bal_msg, "parse_mode": "Markdown"})
            return "OK", 200

        if text.lower() in ["/instruction", "/instraction"]:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            rules_msg = (
                "ℹ️ **የጨዋታ ህጎች እና የማሸነፊያ መንገዶች (Game Rules)**\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "ጨዋታውን ለማሸነፍ ከዚህ በታች ከተዘረዘሩት መንገዶች አንዱን ቀድሞ ማግኘት ይበቃል!\n\n"
                "1️⃣ **አግድም መስመሮች (Horizontal Lines - 5 መንገዶች)**\n"
                "የማንኛውንም አንድ ሙሉ ረድፍ (ከላይ ወደ ታች) በአግድም ሲሞሉ፡\n"
                "B  I  N  G  O\n"
                "+-----+----+-----+\n"
                "|✅ ✅ ✅ ✅ ✅|  <- ማንኛውም 1 ሙሉ ረድፍ\n"
                "+-----+----+-----+\n\n"
                "2️⃣ **አቀባዊ መስመሮች (Vertical Lines - 5 መንገዶች)**\n"
                "የማንኛውንም አንድ ሙሉ አምድ (ከግራ ወደ ቀኝ) በአቀባዊ ሲሞሉ፡\n"
                "B  I  N  G  O\n"
                "+-----+----+-----+\n"
                "|✅   |    |     |\n"
                "|✅   |    |     |  <- ማንኛውም 1 ሙሉ አምድ\n"
                "|✅   |    |     |     (የ N አምድ መሃከለኛውን ነፃ ቦታ ጨምሮ)\n"
                "|✅   |    |     |\n"
                "|✅   |    |     |\n"
                "+-----+----+-----+\n\n"
                "3️⃣ **ሰያፍ መስመሮች (Diagonal Lines - 2 መንገዶች)**\n"
                "ካርቴላውን ከማዕዘን እስከ ማዕዘን diagonally ሲያቋርጡ፡\n"
                "B  I  N  G  O\n"
                "+-----+----+-----+\n"
                "|✅   |    |     |\n"
                "|  ✅ |    |     |  <ከግራ-ላይ እስከ ቀኝ-ታች\n"
                "|     |  ✅|     |    ወይም ከቀኝ-ላይእስከግራ-ታች\n"
                "|     |    |✅   |\n"
                "|     |    |   ✅|\n"
                "+-----+----+-----+\n\n"
                "4️⃣ **አራቱ ኮርነሮች (4 Corners - 1 መንገድ)**\n"
                "የካርቴላውን አራቱን ጽንፎች (ማዕዘኖች) ብቻ ሲሞሉ፦\n"
                "B  I  N  G  O\n"
                "+-----+----+-----+\n"
                "|✅         ✅|\n"
                "|             |  <- አራቱ የውጪ ማዕዘኖች\n"
                "|             |     ብቻ ሲሞሉ\n"
                "|             |\n"
                "|✅         ✅|\n"
                "+-----+----+-----+\n\n"
                "5️⃣ **ሙሉ ዝግ (Full House / ለሱፐር በሽ ብቻ)**\n"
                "የካርቴላውን 25ቱንም ሳጥኖች ሙሉ በሙሉ (100%) ሲዘጉ፦\n"
                "B  I  N  G  O\n"
                "+-----+----+-----+\n"
                "|✅ ✅ ✅ ✅ ✅|\n"
                "|✅ ✅ ✅ ✅ ✅|  <- 25ቱንም ሳጥኖች\n"
                "|✅ ✅  ★  ✅ ✅|     (ሙሉ ካርቴላ) ሲዘጉ\n"
                "|✅ ✅ ✅ ✅ ✅|\n"
                "|✅ ✅ ✅ ✅ ✅|\n"
                "+-----+----+-----+\n\n"
                "💰 **የሽልማት ህግ**\n"
                "- አሸናፊዎች: ከላይ ከተዘረዘሩት የማሸነፊያ መንገዶች አንዱን ቀድሞ የሚያረጋግጠው ተጫዋች አሸናፊ ይሆናል! (ለሱፐር በሽ ሙሉ ዝግ ብቻ ግዴታ ነው)\n"
                "- የሽልማት ክፍፍል: ከአንድ በላይ አሸናፊዎች በአንድ ዙር ካሉ የደራሽ (የሽልማት) ገንዘቡ በእኩል መጠን ይከፋፈላሉ።"
            )
            requests.post(url, json={
                "chat_id": chat_id,
                "text": rules_msg,
                "parse_mode": "Markdown"
            }, timeout=2)
            return "OK", 200

        if text.lower() == "/history":
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            if chat_id == str(ADMIN_ID):
                admin_state.update_one({"chat_id": ADMIN_ID}, {"$set": {"action": "awaiting_history_dates"}}, upsert=True)
                requests.post(url, json={
                    "chat_id": chat_id, 
                    "text": "📅 እባክዎ ፍለጋ የሚፈልጉትን **የመጀመሪያ ቀን**፣ **የመጨረሻ ቀን** እና **የታሪክ ዓይነት** በዚህ ፎርማት ይጻፉ:\n\n`YYYY-MM-DD YYYY-MM-DD deposit`\nወይም `withdrawal`\n\n*ምሳሌ:* `2026-10-01 2026-10-07 deposit`"
                })
                return "OK", 200

            u_wallet = wallets.find_one({"chat_id": chat_id})
            if not u_wallet:
                requests.post(url, json={"chat_id": chat_id, "text": "❌ ምንም የተመዘገበ መለያ አልተገኘም።"})
                return "OK", 200

            u_phone = u_wallet.get("phone")
            since_24h = datetime.utcnow() - timedelta(hours=24)
            user_txs = list(transactions.find({
                "$or": [{"phone": u_phone}, {"receiver_phone": u_phone}],
                "timestamp": {"$gte": since_24h}
            }).sort("timestamp", -1))

            if not user_txs:
                requests.post(url, json={"chat_id": chat_id, "text": "📭 ባለፉት 24 ሰአታት ውስጥ ምንም አይነት የትራንዛክሽን ታሪክ የለዎትም።", "parse_mode": "Markdown"})
            else:
                report = f"📜 *የ ባለፉት 24 ሰአታት የትራንዛክሽን ታሪክዎ:*\n\n"
                for tx in user_txs:
                    t_type = tx.get("type", "N/A").upper()
                    amt = tx.get("amount", 0)
                    st = tx.get("status", "N/A").upper()
                    r_name = tx.get("room", "N/A")
                    ts = (tx.get("timestamp") + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") if tx.get("timestamp") else "N/A"
                    report += f"⏱ `{ts}` | 🏷 ሩም: Room {r_name} | *{t_type}*\n💵 `{amt}` ETB | Status: `{st}`\n------------------------\n"
                requests.post(url, json={"chat_id": chat_id, "text": report, "parse_mode": "Markdown"})
            return "OK", 200

        if chat_id == str(ADMIN_ID):
            parts = text.split()
            cmd = parts[0] if parts else ""

            if cmd == "/admin":
                admin_keyboard = {
                    "inline_keyboard": [
                        [{"text": "⏳ የሚጠብቁ ጥያቄዎችን አፅደቅ (Pending)", "callback_data": "admin_pending_req"}],
                        [{"text": "🔗 የኤጀንት ሊንክ ማመንጫ (Gen Link)", "callback_data": "admin_gen_agent_link"}],
                        [{"text": "👥 የኤጀንት ስታቲስቲክስ (/agent)", "callback_data": "admin_agent_stats_guide"}],
                        [{"text": "📜 የትራንዛክሽን ታሪክ (በቀን እና ዓይነት)", "callback_data": "admin_history_req"}],
                        [{"text": "📅 የእለት/የሳምንት ገቢ እና ወጪ (20% Profit)", "callback_data": "admin_financial_stats"}],
                        [{"text": "📋 የሁሉም ተጠቃሚዎች ባላንስ", "callback_data": "admin_show_all_bal"}],
                        [{"text": "➕ ባላንስ ለመጨመር (/add)", "callback_data": "guide_add"}],
                        [{"text": "➖ ባላንስ ለመቀነስ (/sub)", "callback_data": "guide_sub"}],
                        [{"text": "📢 መልእክት ለማስተላለፍ (/broadcast & /broadcast1)", "callback_data": "guide_broadcast"}],
                        [{"text": "🚫 ተጠቃሚ ብሎክ/ማጥፊያ (/block & /remove)", "callback_data": "guide_block_remove"}],
                        [{"text": "🌐 የአድሚን ዌብ ዳሽቦርድ", "url": f"{WEB_APP_URL}/admin_get_users?phone=0945880474"}]
                    ]
                }
                send_telegram("🛠 *የአድሚን ማኔጅመንት ሰሌዳ (Admin Workspace)*\n\nከታች ባሉት ቁልፎች ወይም ትዕዛዞች በፍጥነት ስራዎችን ማከናወን ይችላሉ፦", reply_markup=admin_keyboard)
                return "OK", 200

            elif cmd == "/daily":
                send_telegram(get_financial_stats())
                return "OK", 200

            elif cmd == "/agent":
                if len(parts) > 1:
                    agent_ph = sanitize_input(parts[1])
                    referred_users = list(wallets.find({"referred_by": agent_ph}))
                    total_referred = len(referred_users)
                    
                    user_phones = [u["phone"] for u in referred_users]
                    
                    now = datetime.utcnow()
                    week_start = now - timedelta(days=7)
                    
                    deposits = list(transactions.find({
                        "phone": {"$in": user_phones},
                        "type": "deposit",
                        "status": "approved"
                    }))
                    total_dep_count = len(deposits)
                    total_dep_amt = sum([d.get("amount", 0) for d in deposits])
                    
                    withdrawals = list(transactions.find({
                        "phone": {"$in": user_phones},
                        "type": "withdrawal",
                        "status": "approved"
                    }))
                    total_wit_amt = sum([w.get("amount", 0) for w in withdrawals])

                    week_comm = list(transactions.aggregate([
                        {"$match": {"type": "game_commission", "timestamp": {"$gte": week_start}}},
                        {"$group": {"_id": None, "total": {"$sum": "$amount"}}}
                    ]))
                    w_comm_val = week_comm[0]["total"] if week_comm else 0.0
                    week_profit = w_comm_val - total_wit_amt

                    rep_msg = (
                        f"👥 *የኤጀንት ስታቲስቲክስ ሪፖርት*\n"
                        f"📞 የኤጀንት ስልክ: `{agent_ph}`\n\n"
                        f"👤 የተመዘገቡ ሰዎች ብዛት: *{total_referred} ተጠቃሚዎች*\n"
                        f"📥 ዲፖዚት ያደረጉ: *{total_dep_count} ግብይቶች* (ጠቅላላ: `{total_dep_amt:,.2f} ETB`)\n"
                        f"📤 ዊዝድሮው የተደረገ: `{total_wit_amt:,.2f} ETB`\n"
                        f"💰 የሳምንቱ የተጣራ ትርፍ (Weekly Net Profit): `{week_profit:,.2f} ETB`"
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
                        p_amt = p.get("amount")
                        p_room = p.get("room", "N/A")
                        btn_code = f"app_dep_{p_id}_{p_ph}_{p_amt}" if p_type == "DEPOSIT" else f"app_wit_{p_id}_{p_ph}_{p_amt}"
                        rej_code = f"rej_dep_{p_id}_{p_ph}" if p_type == "DEPOSIT" else f"rej_wit_{p_id}_{p_ph}_{p_amt}"
                        kb = {
                            "inline_keyboard": [[
                                {"text": "✅ አረጋግጥ (Approve)", "callback_data": btn_code},
                                {"text": "❌ ሰርዝ (Reject)", "callback_data": rej_code}
                            ]]
                        }
                        send_telegram(f"⏳ *የሚጠብቅ ጥያቄ ({p_type} - Room {p_room}):*\n📞 የስልክ ቁጥር: `{p_ph}`\n💵 መጠን: `{p_amt}` ETB", reply_markup=kb)
                return "OK", 200

            elif cmd == "/block" and len(parts) > 1:
                target_phone = sanitize_input(parts[1])
                blocked_phones.update_one({"phone": target_phone}, {"$set": {"phone": target_phone}}, upsert=True)
                send_telegram(f"✅ ስልክ ቁጥር ({target_phone}) ብሎክ ተደርጓል!")
                return "OK", 200

            elif cmd == "/unblock" and len(parts) > 1:
                target_phone = sanitize_input(parts[1])
                blocked_phones.delete_one({"phone": target_phone})
                send_telegram(f"✅ ስልክ ቁጥር ({target_phone}) ከብሎክ ተነስተዋል!")
                return "OK", 200

            elif cmd == "/add" and len(parts) > 2:
                target_ph = sanitize_input(parts[1])
                try:
                    amt = float(parts[2])
                    u = wallets.find_one_and_update(
                        {"phone": target_ph},
                        {"$inc": {"balance": amt}},
                        return_document=True,
                        upsert=True
                    )
                    if u:
                        real_phone = u.get('phone', target_ph)
                        send_telegram(f"✅ ለ `{real_phone}` ተጠቃሚ {amt} ETB ተጨምሯል። አዲስ ባላንስ፦ {u.get('balance')} ETB")
                        notify_user_balance_update(real_phone, u.get('balance', 0))
                except ValueError:
                    send_telegram("❌ ትክክለኛ የገንዘብ መጠን ያስገቡ!")
                return "OK", 200

            elif cmd == "/sub" and len(parts) > 2:
                target_ph = sanitize_input(parts[1])
                try:
                    amt = float(parts[2])
                    u = wallets.find_one_and_update({"phone": target_ph}, {"$inc": {"balance": -amt}}, return_document=True)
                    if u:
                        send_telegram(f"✅ ከ `{target_ph}` ተጠቃሚ ላይ {amt} ETB ተቀንሷል። አዲስ ባላንስ፦ {u.get('balance')} ETB")
                        notify_user_balance_update(target_ph, u.get('balance', 0))
                    else:
                        send_telegram("❌ ተጠቃሚው አልተገኘም!")
                except ValueError:
                    send_telegram("❌ ትክክለኛ መጠን ያስገቡ!")
                return "OK", 200

            elif cmd == "/remove" and len(parts) > 1:
                target_ph = sanitize_input(parts[1])
                res = wallets.delete_one({"phone": target_ph})
                if res.deleted_count > 0:
                    send_telegram(f"🗑️ ተጠቃሚ `{target_ph}` ከዳታቤዝ ተሰርዟል።")
                else:
                    send_telegram("❌ ተጠቃሚው አልተገኘም!")
                return "OK", 200

            elif cmd in ["/all", "/all_balances"]:
                all_users = list(wallets.find())
                rep_text = "📊 *የሁሉም ተጠቃሚዎች ዝርዝር፦*\n\n"
                total_sys_balance = 0
                for u in all_users:
                    bal = u.get('balance', 0)
                    total_sys_balance += bal
                    rep_text += f"👤 {u.get('username', 'N/A')} | 📞 `{u.get('phone')}` | 💰 {bal} ETB\n"
                rep_text += f"\n💎 **ጠቅላላ የሲስተም ባላንስ፦** {total_sys_balance} ETB"
                send_telegram(rep_text)
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

    elif "callback_query" in data:
        cq = data["callback_query"]
        cq_id = cq["id"]
        chat_id = str(cq["message"]["chat"]["id"])
        data_str = cq.get("data", "")
        
        if data_str == "Besh_bingo_bonus":
            answer_url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
            requests.post(answer_url, json={"callback_query_id": cq_id, "text": "የቦነስ ፕሮግራም በቅርቡ ይጀመራል!", "show_alert": True})

        if chat_id == str(ADMIN_ID):
            answer_url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
            
            if data_str == "admin_gen_agent_link":
                admin_state.update_one({"chat_id": ADMIN_ID}, {"$set": {"action": "awaiting_agent_phone"}}, upsert=True)
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "ስልክ ቁጥር ያስገቡ"})
                send_telegram("📱 እባክዎ ሊንክ ማመንጨት የሚፈልጉትን **የኤጀንት ስልክ ቁጥር** ያስገቡ፦")

            elif data_str == "admin_agent_stats_guide":
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "ትዕዛዝ ይጠቀሙ"})
                send_telegram("👥 የኤጀንት ስታቲስቲክስ ለማየት የሚከተለውን ትዕዛዝ ይጠቀሙ:\n\n`/agent <የኤጀንት_ስልክ>`")

            elif data_str == "admin_financial_stats":
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "ስታቲስቲክስ ተዘጋጅቷል"})
                send_telegram(get_financial_stats())

            elif data_str == "admin_history_req":
                admin_state.update_one({"chat_id": ADMIN_ID}, {"$set": {"action": "awaiting_history_dates"}}, upsert=True)
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "ቀን እና ዓይነት ያስገቡ"})
                send_telegram("📅 እባክዎ ፍለጋ የሚፈልጉትን **የመጀመሪያ ቀን**፣ **የመጨረሻ ቀን** እና **የታሪክ ዓይነት** በዚህ ፎርማት ይጻፉ:\n\n`YYYY-MM-DD YYYY-MM-DD deposit`\nወይም `withdrawal`")

            elif data_str == "guide_add":
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "መመሪያ"})
                send_telegram("➕ *ባላንስ ለመጨመር የትእዛዝ ፎርማት:*\n\n`/add <ስልክ> <መጠን>`\n*ምሳሌ:* `/add 0912345678 100`")

            elif data_str == "guide_sub":
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "መመሪያ"})
                send_telegram("➖ *ባላንስ ለመቀነስ የትእዛዝ ፎርማት:*\n\n`/sub <ስልክ> <መጠን>`\n*ምሳሌ:* `/sub 0912345678 50`")

            elif data_str == "guide_broadcast":
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "መመሪያ"})
                send_telegram("📢 *ለሁሉም መልእክት ለመላክ:*\n\n1. ፎቶ ያለው: `/broadcast`\n2. ጽሁፍ ብቻ: `/broadcast1 <መልእክትዎ>`")

            elif data_str == "guide_block_remove":
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "መመሪያ"})
                send_telegram("🚫 *ብሎክ ለማድረግና ለማጥፋት:*\n\n1. ብሎክ ማድረግ: `/block <ስልክ>`\n2. ከብሎክ ማንሳት: `/unblock <ስልክ>`\n3. ተጠቃሚ መደለዝ: `/remove <ስልክ>`")

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
                        p_room = p.get("room", "N/A")
                        btn_code = f"app_dep_{p_id}_{p_ph}_{p_amt}" if p_type == "DEPOSIT" else f"app_wit_{p_id}_{p_ph}_{p_amt}"
                        rej_code = f"rej_dep_{p_id}_{p_ph}" if p_type == "DEPOSIT" else f"rej_wit_{p_id}_{p_ph}_{p_amt}"
                        kb = {
                            "inline_keyboard": [[
                                {"text": "✅ አረጋግጥ (Approve)", "callback_data": btn_code},
                                {"text": "❌ ሰርዝ (Reject)", "callback_data": rej_code}
                            ]]
                        }
                        send_telegram(f"⏳ *የሚጠብቅ ጥያቄ ({p_type} - Room {p_room}):*\n📞 የስልክ ቁጥር: `{p_ph}`\n💵 መጠን: `{p_amt}` ETB", reply_markup=kb)

            elif data_str == "admin_show_all_bal":
                all_users = list(wallets.find({}))
                msg_text = "📋 *የሁሉም ተጠቃሚዎች ባላንስ ዝርዝር:*\n\n"
                total_sys_balance = 0
                for u in all_users:
                    u_phone = u.get("phone", "N/A")
                    u_name = u.get("name", u.get("username", "Unknown"))
                    u_bal = u.get("balance", 0)
                    total_sys_balance += u_bal
                    msg_text += f"📞 `{u_phone}` | 👤 {u_name} | 💰 *{u_bal} ETB*\n"
                msg_text += f"\n💵 *አጠቃላይ የሲስተሙ ገንዘብ:* {total_sys_balance} ETB"
                send_telegram(msg_text)

            elif data_str.startswith("app_dep_"):
                parts = data_str.split("_")
                tx_id_str = parts[2]
                phone_num = parts[3]
                amt = float(parts[4])
                
                try:
                    tx_updated = transactions.find_one_and_update(
                        {"_id": ObjectId(tx_id_str), "status": "pending"},
                        {"$set": {"status": "approved", "timestamp": datetime.utcnow()}}
                    )
                except Exception:
                    tx_updated = transactions.find_one_and_update(
                        {"phone": phone_num, "status": "pending", "type": "deposit"},
                        {"$set": {"status": "approved", "timestamp": datetime.utcnow()}}
                    )

                if tx_updated:
                    updated = wallets.find_one_and_update({"phone": phone_num}, {"$inc": {"balance": amt}}, return_document=True, upsert=True)
                    new_bal = updated.get("balance", 0) if updated else 0
                    notify_user_balance_update(phone_num, new_bal)
                    
                    user_chat_id = updated.get("chat_id")
                    if user_chat_id:
                        try:
                            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                                "chat_id": user_chat_id,
                                "text": f"✅ *የዲፖዚት ጥያቄዎ ጸድቋል!*\n\n💵 በሂሳብዎ ላይ *{amt} ETB* ተጨምሯል።\n💰 አጠቃላይ ቀሪ ባላንስዎ: *{new_bal} ETB*",
                                "parse_mode": "Markdown"
                            }, timeout=2)
                        except Exception:
                            pass

                    requests.post(answer_url, json={"callback_query_id": cq_id, "text": f"ተሳክቷል! {amt} ETB ገብቷል።"})
                else:
                    requests.post(answer_url, json={"callback_query_id": cq_id, "text": "⚠ ይህ ጥያቄ አስቀድሞ ፀድቋል ወይም ተሰርዟል!", "show_alert": True})
            
            elif data_str.startswith("rej_dep_"):
                parts = data_str.split("_")
                try:
                    transactions.update_one({"_id": ObjectId(parts[2]), "status": "pending"}, {"$set": {"status": "rejected"}})
                except Exception:
                    transactions.update_one({"phone": parts[3], "status": "pending", "type": "deposit"}, {"$set": {"status": "rejected"}})
                
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "ዲፖዚት ጥያቄው ሪጀክት ተደርጓል።"})

            elif data_str.startswith("app_wit_"):
                parts = data_str.split("_")
                tx_id_str = parts[2]
                phone_num = parts[3]
                amt = float(parts[4])

                try:
                    tx_updated = transactions.find_one_and_update(
                        {"_id": ObjectId(tx_id_str), "status": "pending"},
                        {"$set": {"status": "approved", "timestamp": datetime.utcnow()}}
                    )
                except Exception:
                    tx_updated = transactions.find_one_and_update(
                        {"phone": phone_num, "status": "pending", "type": "withdrawal"},
                        {"$set": {"status": "approved", "timestamp": datetime.utcnow()}}
                    )

                if tx_updated:
                    updated = wallets.find_one_and_update({"phone": phone_num, "balance": {"$gte": amt}}, {"$inc": {"balance": -amt}}, return_document=True)
                    if updated:
                        new_bal = updated.get("balance", 0)
                        notify_user_balance_update(phone_num, new_bal)
                        requests.post(answer_url, json={"callback_query_id": cq_id, "text": "ዊዝድሮዋል ጸድቋል!"})
                    else:
                        requests.post(answer_url, json={"callback_query_id": cq_id, "text": "❌ የተጠቃሚው ባላንስ በቂ አይደለም!", "show_alert": True})
                else:
                    requests.post(answer_url, json={"callback_query_id": cq_id, "text": "⚠ ይህ ጥያቄ አስቀድሞ ፀድቋል ወይም ተሰርዟል!", "show_alert": True})
            
            elif data_str.startswith("rej_wit_"):
                parts = data_str.split("_")
                try:
                    transactions.update_one({"_id": ObjectId(parts[2]), "status": "pending"}, {"$set": {"status": "rejected"}})
                except Exception:
                    transactions.update_one({"phone": parts[3], "status": "pending", "type": "withdrawal"}, {"$set": {"status": "rejected"}})

                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "ዊዝድሮዋል ጥያቄው ሪጀክት ተደርጓል።"})

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
