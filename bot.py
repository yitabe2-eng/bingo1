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

game_states = {
    "10": {
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
    }
}

loop_started = False
reset_task_references = {"10": None, "50": None}
pending_claims = {"10": [], "50": []}
claim_lock_active = {"10": False, "50": False}

def sanitize_input(text):
    if not text:
        return ""
    return re.sub(r'[^\w\s\-\\.\@]', '', str(text)).strip()

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
    # 🌟 እያንዳንዱ ጨዋታ የየራሱን የተለየ ቻናል እንዲጠቀም ማድረግ (Collision ማጥፋት)
    socketio.emit(f'game_update_{room_type}', state_payload)

def notify_user_balance_update(phone_num, new_balance):
    socketio.emit('balance_update', {"phone": phone_num, "balance": new_balance})

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

    all_win_indices = set()
    line_types = []
    for i in range(5):
        row_indices = [i*5 + j for j in range(5)]
        if all(marked[idx] for idx in row_indices):
            all_win_indices.update(row_indices)
            line_types.append(f"ረድፍ {i+1}")
    for j in range(5):
        col_indices = [j + i*5 for i in range(5)]
        if all(marked[idx] for idx in col_indices):
            all_win_indices.update(col_indices)
            line_types.append(f"አምድ {j+1}")
    diag1_indices = [0, 6, 12, 18, 24]
    if all(marked[idx] for idx in diag1_indices):
        all_win_indices.update(diag1_indices)
        line_types.append("ዲያጎናል ↘")
    diag2_indices = [4, 8, 12, 16, 20]
    if all(marked[idx] for idx in diag2_indices):
        all_win_indices.update(diag2_indices)
        line_types.append("ዲያጎናል ↙")
    corner_indices = [0, 4, 20, 24]
    if all(marked[idx] for idx in corner_indices):
        all_win_indices.update(corner_indices)
        line_types.append("ኮርነር (4 ማዕዘኖች)")

    if all_win_indices:
        return list(all_win_indices), " + ".join(line_types)
    return None, None

def refund_all_sold_tickets(room_type):
    price = 10 if room_type == "10" else 50
    state = game_states[room_type]
    for t_num, phone_num in list(state["sold_tickets"].items()):
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
        "sold_tickets": {}, "drawn_balls": [], "current_ball": "--", "timer": 30, "ball_timer": 2, "all_cards": {}
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

@app.route('/super')
def index_50():
    return render_template('index1.html')

@app.route('/get_status')
def get_status():
    room = request.args.get('room', '10')
    state = game_states.get(room, game_states["10"])
    
    phone = sanitize_input(request.args.get('phone'))
    user = wallets.find_one({"phone": phone}) if phone else None
    db_phone = user['phone'] if user else phone
    p_data = state["players"].get(db_phone, {"cards": {}})
    cards_list = list(p_data["cards"].values())
    clean_players = {k: {"username": v.get("username", ""), "cards": list(v.get("cards", {}).values())} for k, v in state["players"].items()}
    
    return jsonify({
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
        "is_waiting": state["status"] in ["playing", "result"] and db_phone not in state["players"]
    })

@app.route('/buy_specific_ticket', methods=['POST'])
def buy_ticket():
    d = request.json or {}
    ph, t_num, uname = sanitize_input(d.get('phone')), str(d.get('ticket_num')), sanitize_input(d.get('username'))
    room = str(d.get('room', '10'))
    state = game_states.get(room, game_states["10"])
    price = 10 if room == "10" else 50

    if not ph or not t_num:
        return jsonify({"success": False})
    user = wallets.find_one({"phone": ph})
    if not user:
        return jsonify({"success": False})
    db_phone = user["phone"]

    if state["status"] != "lobby" or t_num in state["sold_tickets"]:
        return jsonify({"success": False})
    
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
    return jsonify({"success": False})

@app.route('/cancel_ticket', methods=['POST'])
def cancel_ticket():
    d = request.json or {}
    ph, t_num = sanitize_input(d.get('phone')), str(d.get('ticket_num'))
    room = str(d.get('room', '10'))
    state = game_states.get(room, game_states["10"])
    price = 10 if room == "10" else 50

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
                socketio.sleep(0.2)

                total_pot = state["pot"]
                total_prize = total_pot * 0.8  
                house_commission = total_pot * 0.2

                if house_commission > 0:
                    transactions.insert_one({
                        "type": "game_commission",
                        "amount": house_commission,
                        "pot_amount": total_pot,
                        "timestamp": datetime.utcnow()
                    })

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
                        
                        success_msg = f"🏆 *WINNER (Room {room} ETB)!* \n👤 Name: {w['username']} | 📞 Phone: `{w['phone']}` | 🎫 Ticket: {w['ticket_num']} \n🎯 Winning Ball: {w['winning_ball']} \n💰 Prize Won: {total_prize:.2f} ETB"
                        send_telegram(success_msg)
                    else:
                        share_prize = total_prize / num_winners
                        for w in pending_claims[room]:
                            w_res = wallets.find_one_and_update(
                                {"phone": w["phone"]}, 
                                {"$inc": {"balance": share_prize}}, 
                                return_document=True
                            )
                            if w_res:
                                gevent.spawn(notify_user_balance_update, w["phone"], w_res.get("balance", 0))
                        send_telegram(f"🏆 *WINNERS (Room {room}) Shared!* 💰 {share_prize:.2f} ETB each")
                        
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

    return jsonify({"success": True})

@app.route('/request_deposit', methods=['POST'])
def request_deposit():
    d = request.json or {}
    ph = sanitize_input(str(d.get('phone')))
    method = sanitize_input(str(d.get('method', 'TELE BIRR'))) 
    try:
        amt = float(d.get('amount', 0))
    except ValueError:
        amt = 0
    t_id = sanitize_input(d.get('transaction_id', 'N/A'))
    
    user = wallets.find_one({"phone": ph})
    db_phone = user["phone"] if user else ph
    
    tx_res = transactions.insert_one({"phone": db_phone, "type": "deposit", "amount": amt, "status": "pending", "method": method, "tx_id": t_id, "timestamp": datetime.utcnow()})
    tx_ref = str(tx_res.inserted_id)

    msg = f"💰 *Deposit Request*\n📞 Phone: `{db_phone}`\n💵 Amount: `{amt}` ETB\n🆔 ID: `{t_id}`"
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
    try:
        amt = float(d.get('amount', 0))
    except ValueError:
        return jsonify({"success": False, "msg": "ትክክለኛ የገንዘብ መጠን ያስገቡ!"})
    if amt < 51:
        return jsonify({"success": False, "msg": "ቢያንስ 51 ETB ነው!"})
    
    user = wallets.find_one({"phone": ph})
    if not user:
        return jsonify({"success": False, "msg": "ተጠቃሚው አልተገኘም!"})
    db_phone = user["phone"]
    
    if user.get("balance", 0) < amt:
        return jsonify({"success": False, "msg": "በቂ ባላንስ የለዎትም!"})

    tx_res = transactions.insert_one({"phone": db_phone, "type": "withdrawal", "amount": amt, "status": "pending", "timestamp": datetime.utcnow()})
    tx_ref = str(tx_res.inserted_id)

    msg = f"📤 *Withdrawal Request*\n📞 Phone: `{db_phone}`\n💵 Amount: `{amt}` ETB"
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
    
    sender = wallets.find_one({"phone": sender_ph})
    if not sender or sender.get("balance", 0) < amt:
        return jsonify({"success": False, "msg": "በቂ ባላንስ የለዎትም!"})
    db_sender_phone = sender["phone"]
    
    receiver = wallets.find_one({"phone": receiver_ph})
    if not receiver:
        return jsonify({"success": False, "msg": "ተቀባዩ አልተገኘም!"})
    db_receiver_phone = receiver["phone"]

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
    
    if not input_phone:
        return jsonify({"success": False, "msg": "እባክዎ ስልክ ቁጥር ያስገቡ!"}), 400
        
    clean_phone = input_phone.replace("+", "").replace(" ", "")
    fallback_name = input_username if input_username else f"User_{clean_phone[-4:]}"
    
    update_data = {"username": fallback_name, "name": fallback_name}
    if input_chat_id:
        update_data["chat_id"] = input_chat_id

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
        
        if text.lower() == "/play":
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            keyboard = {
                "inline_keyboard": [
                    [{"text": "🎮 PLAY | 10 ብር", "web_app": {"url": WEB_APP_URL}}], 
                    [{"text": "🎮 PLAY | 50 ብር", "web_app": {"url": f"{WEB_APP_URL}/super"}}], 
                    [{"text": "SuperbeshBingo | 50 ብር", "url": "http://t.me/superbeshbingobot"}],
                    [{"text": "⚽ BeshBingo Bonus", "callback_data": "Besh_bingo_bonus"}]
                ]
            }
            requests.post(url, json={
                "chat_id": chat_id,
                "text": "🕹 *PLAY IN:*\nChoose a room to join the game:",
                "parse_mode": "Markdown",
                "reply_markup": keyboard
            }, timeout=2)
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
        socketio.start_background_task(lambda: run_game_loop("50"))
    broadcast_game_state("10")
    broadcast_game_state("50")

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
