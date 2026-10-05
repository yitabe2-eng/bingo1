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

game_state = {
    "status": "lobby", 
    "timer": 30,
    "ball_timer": 3,      
    "pot": 0, 
    "players": {},        
    "sold_tickets": {},  
    "current_ball": "--", 
    "drawn_balls": [], 
    "winner": None,
    "winning_card": None,
    "winning_ticket_num": None,
    "winning_indices": None,
    "winning_line_name": None,  
    "all_cards": {}  
}

loop_started = False
reset_task_reference = None
pending_claims = []
claim_lock_active = False

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
        {"command": "history", "description": "የትራንዛክሽን ታሪክ ለማየት"}
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
            {"command": "admin", "description": "🛠 የአድሚን ማውጫ / Dashboard"},
            {"command": "pending", "description": "⏳ ጥያቄዎችን ለማፅደቅ (Approvals)"},
            {"command": "daily", "description": "📅 የእለት/የሳምንት ገቢና ወጪ"}
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

def broadcast_game_state():
    state_payload = {
        "status": game_state["status"],
        "timer": game_state["timer"],
        "ball_timer": game_state["ball_timer"],
        "pot": game_state["pot"],
        "sold_tickets": game_state["sold_tickets"],
        "current_ball": game_state["current_ball"],
        "drawn_balls": game_state["drawn_balls"],
        "winner": game_state["winner"],
        "winning_card": game_state["winning_card"],
        "winning_ticket_num": game_state["winning_ticket_num"],
        "winning_indices": game_state.get("winning_indices"),
        "winning_line_name": game_state.get("winning_line_name"), 
        "all_cards": game_state.get("all_cards", {}), 
        "active_players": len(game_state["players"])
    }
    socketio.emit('game_update', state_payload)

def notify_user_balance_update(phone_num, new_balance):
    socketio.emit('balance_update', {"phone": phone_num, "balance": new_balance})

# 🌟 አድሚኑ 'Approve' ሲጫን ተጠቃሚው ዘንድ ዌብሶኬት ኖቲፊኬሽን እና አውቶ ባላንስ ሪፍሬሽ እንዲኖር የሚልክ ፈንክሽን
def notify_user_deposit_success(phone_num, amount):
    socketio.emit('deposit_success_notify', {"phone": phone_num, "amount": amount, "duration": 3})

def is_request_from_admin(phone_val):
    if not phone_val:
        return False
    clean = re.sub(r'[^0-9]', '', str(phone_val))
    return clean.endswith("0945880474")

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

    for r in range(5):
        row_indices = [r * 5 + c for c in range(5)]
        if all(marked[i] for i in row_indices):
            winning_indices = row_indices
            line_name = f"አግድም መስመር {r+1}"
            return True, winning_indices, line_name

    for c in range(5):
        col_indices = [r * 5 + c for r in range(5)]
        if all(marked[i] for i in col_indices):
            winning_indices = col_indices
            line_name = f"ቋሚ መስመር {c+1}"
            return True, winning_indices, line_name

    diag1 = [0, 6, 12, 18, 24]
    if all(marked[i] for i in diag1):
            winning_indices = diag1
            line_name = "ዋና ሰያፍ መስመር"
            return True, winning_indices, line_name

    diag2 = [4, 8, 12, 16, 20]
    if all(marked[i] for i in diag2):
            winning_indices = diag2
            line_name = "ሁለተኛ ሰያፍ መስመር"
            return True, winning_indices, line_name

    return False, [], None

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

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/register_or_login', methods=['POST'])
def register_or_login():
    data = request.json or {}
    phone = sanitize_input(data.get('phone'))
    username = sanitize_input(data.get('username') or "User")
    chat_id = sanitize_input(data.get('chat_id'))

    if not phone:
        return jsonify({"success": False, "msg": "ስልክ ቁጥር ያስፈልጋል!"}), 400

    user = wallets.find_one({"phone": phone})
    if not user:
        wallets.insert_one({
            "phone": phone,
            "username": username,
            "balance": 0.0,
            "chat_id": chat_id,
            "created_at": datetime.utcnow()
        })
    else:
        if chat_id and user.get("chat_id") != chat_id:
            wallets.update_one({"phone": phone}, {"$set": {"chat_id": chat_id}})

    return jsonify({"success": True})

@app.route('/get_status', methods=['GET'])
def get_status():
    phone = sanitize_input(request.args.get('phone'))
    balance = 0.0
    if phone:
        user = wallets.find_one({"phone": phone})
        if user:
            balance = user.get("balance", 0.0)
            game_state["players"][phone] = time.time()

    return jsonify({
        "status": game_state["status"],
        "timer": game_state["timer"],
        "ball_timer": game_state["ball_timer"],
        "pot": game_state["pot"],
        "sold_tickets": game_state["sold_tickets"],
        "current_ball": game_state["current_ball"],
        "drawn_balls": game_state["drawn_balls"],
        "winner": game_state["winner"],
        "winning_card": game_state["winning_card"],
        "winning_ticket_num": game_state["winning_ticket_num"],
        "winning_indices": game_state.get("winning_indices"),
        "winning_line_name": game_state.get("winning_line_name"),
        "all_cards": game_state.get("all_cards", {}),
        "active_players": len(game_state["players"]),
        "balance": balance
    })

@app.route('/request_deposit', methods=['POST'])
def request_deposit():
    d = request.json or {}
    phone = sanitize_input(d.get('phone'))
    amount = float(d.get('amount', 0))
    tx_id = sanitize_input(d.get('transaction_id'))
    method = sanitize_input(d.get('method', 'TELE BIRR'))

    if not phone or amount < 10 or not tx_id:
        return jsonify({"success": False, "msg": "እባክዎ ትክክለኛ መረጃ ያስገቡ!"}), 400

    existing_tx = transactions.find_one({"transaction_id": tx_id})
    if existing_tx:
        return jsonify({"success": False, "msg": "ይህ Transaction ID ከዚህ በፊት ጥቅም ላይ ውሏል!"}), 400

    tx_doc = {
        "phone": phone,
        "amount": amount,
        "transaction_id": tx_id,
        "method": method,
        "type": "deposit",
        "status": "pending",
        "timestamp": datetime.utcnow()
    }
    transactions.insert_one(tx_doc)

    user = wallets.find_one({"phone": phone})
    uname = user.get("username", "User") if user else "User"

    admin_msg = (
        f"📥 *አዲስ የዲፖዚት ጥያቄ (Deposit Request)*\n\n"
        f"👤 ተጠቃሚ: `{uname}`\n"
        f"📞 ስልክ: `{phone}`\n"
        f"💰 መጠን: `{amount} ETB`\n"
        f"💳 መንገድ: `{method}`\n"
        f"🔢 TxID: `{tx_id}`\n"
    )
    
    markup = {
        "inline_keyboard": [
            [
                {"text": "✅ አረጋግጥ (Approve)", "callback_data": f"dep_app_{tx_id}"},
                {"text": "❌ ሰርዝ (Reject)", "callback_data": f"dep_rej_{tx_id}"}
            ]
        ]
    }
    send_telegram(admin_msg, reply_markup=markup)
    return jsonify({"success": True, "msg": "የዲፖዚት ጥያቄዎ በተሳካ ሁኔታ ተልኳል።"})

@app.route('/request_withdrawal', methods=['POST'])
def request_withdrawal():
    d = request.json or {}
    phone = sanitize_input(d.get('phone'))
    amount = float(d.get('amount', 0))
    method = sanitize_input(d.get('method', 'TELE BIRR'))

    if not phone or amount < 51:
        return jsonify({"success": False, "msg": "ቢያንስ 51 ETB ማውጣት ይችላሉ!"}), 400

    user = wallets.find_one({"phone": phone})
    if not user or user.get("balance", 0) < amount:
        return jsonify({"success": False, "msg": "በቂ ባላንስ የለዎትም!"}), 400

    wallets.update_one({"phone": phone}, {"$inc": {"balance": -amount}})
    new_bal = wallets.find_one({"phone": phone}).get("balance", 0)
    notify_user_balance_update(phone, new_bal)

    tx_id = f"WIT_{int(time.time())}_{random.randint(100,999)}"
    transactions.insert_one({
        "phone": phone,
        "amount": amount,
        "transaction_id": tx_id,
        "method": method,
        "type": "withdrawal",
        "status": "approved",
        "timestamp": datetime.utcnow()
    })

    admin_msg = (
        f"📤 *አዲስ የገንዘብ ማውጣት ጥያቄ (Withdrawal)*\n\n"
        f"📞 ስልክ: `{phone}`\n"
        f"💰 መጠን: `{amount} ETB`\n"
        f"💳 መንገድ: `{method}`\n"
        f"🔢 ID: `{tx_id}`"
    )
    send_telegram(admin_msg)
    return jsonify({"success": True, "msg": "የማውጣት ጥያቄዎ ተፈፅሟል!", "balance": new_bal})

@app.route('/buy_specific_ticket', methods=['POST'])
def buy_specific_ticket():
    d = request.json or {}
    phone = sanitize_input(d.get('phone'))
    ticket_num = int(d.get('ticket_num', 0))
    
    if game_state["status"] != "lobby":
        return jsonify({"success": False, "msg": "ጨዋታው ተጀምሯል!"}), 400

    user = wallets.find_one({"phone": phone})
    if not user or user.get("balance", 0) < 10:
        return jsonify({"success": False, "msg": "በቂ ባላንስ (10 ETB) የለዎትም!"}), 400

    my_owned_count = sum(1 for p in game_state["sold_tickets"].values() if p == phone)
    if my_owned_count >= 2:
        return jsonify({"success": False, "msg": "ከ 2 ካርቴላ በላይ መግዛት አይችሉም!"}), 400

    if str(ticket_num) in game_state["sold_tickets"]:
        return jsonify({"success": False, "msg": "ይህ ካርቴላ ተሽጧል!"}), 400

    wallets.update_one({"phone": phone}, {"$inc": {"balance": -10}})
    new_bal = wallets.find_one({"phone": phone}).get("balance", 0)

    game_state["sold_tickets"][str(ticket_num)] = phone
    game_state["pot"] += 10

    if str(ticket_num) not in game_state["all_cards"]:
        card_matrix = [random.randint(1, 75) for _ in range(25)]
        card_matrix[12] = 0
        game_state["all_cards"][str(ticket_num)] = card_matrix

    notify_user_balance_update(phone, new_bal)
    broadcast_game_state()
    return jsonify({"success": True, "balance": new_bal})

@app.route('/cancel_ticket', methods=['POST'])
def cancel_ticket():
    d = request.json or {}
    phone = sanitize_input(d.get('phone'))
    ticket_num = str(d.get('ticket_num'))

    if game_state["status"] != "lobby":
        return jsonify({"success": False, "msg": "ጨዋታው ተጀምሯል!"}), 400

    if game_state["sold_tickets"].get(ticket_num) == phone:
        del game_state["sold_tickets"][ticket_num]
        game_state["pot"] = max(0, game_state["pot"] - 10)
        wallets.update_one({"phone": phone}, {"$inc": {"balance": 10}})
        new_bal = wallets.find_one({"phone": phone}).get("balance", 0)
        notify_user_balance_update(phone, new_bal)
        broadcast_game_state()
        return jsonify({"success": True, "balance": new_bal})

    return jsonify({"success": False, "msg": "ካርቴላው አልተገኘም!"}), 400

@app.route('/claim_bingo', methods=['POST'])
def claim_bingo():
    global claim_lock_active, pending_claims
    d = request.json or {}
    phone = sanitize_input(d.get('phone'))

    if game_state["status"] != "playing":
        return jsonify({"success": False, "msg": "አሁን ጨዋታ አይካሄድም!"}), 400

    user = wallets.find_one({"phone": phone})
    uname = user.get("username", "Player") if user else "Player"

    user_cards = []
    for t_id, owner_phone in game_state["sold_tickets"].items():
        if owner_phone == phone:
            matrix = game_state["all_cards"].get(t_id)
            if matrix:
                user_cards.append((t_id, matrix))

    if not user_cards:
        return jsonify({"success": False, "msg": "የእርስዎ ካርቴላ አልተገኘም!"}), 400

    valid_win = False
    winning_t_id = None
    winning_matrix = None
    winning_indices = []
    winning_line_name = None

    for t_id, matrix in user_cards:
        is_win, w_idxs, l_name = check_bingo_win_strict(matrix, game_state["drawn_balls"])
        if is_win:
            valid_win = True
            winning_t_id = t_id
            winning_matrix = matrix
            winning_indices = w_idxs
            winning_line_name = l_name
            break

    if not valid_win:
        return jsonify({"success": False, "msg": "❌ ትክክለኛ የ BINGO መስመር አልሞላም!"}), 400

    total_pot = game_state["pot"]
    winner_prize = int(total_pot * 0.8)
    commission = total_pot - winner_prize

    game_state["status"] = "result"
    game_state["winner"] = uname
    game_state["winning_card"] = winning_matrix
    game_state["winning_ticket_num"] = winning_t_id
    game_state["winning_indices"] = winning_indices
    game_state["winning_line_name"] = winning_line_name
    game_state["timer"] = 10

    wallets.update_one({"phone": phone}, {"$inc": {"balance": winner_prize}})
    new_bal = wallets.find_one({"phone": phone}).get("balance", 0)
    notify_user_balance_update(phone, new_bal)

    if commission > 0:
        transactions.insert_one({
            "type": "game_commission",
            "amount": commission,
            "timestamp": datetime.utcnow()
        })

    broadcast_game_state()
    return jsonify({"success": True, "prize": winner_prize})

@app.route('/webhook', methods=['POST'])
def telegram_webhook():
    data = request.json or {}
    if "callback_query" in data:
        cb = data["callback_query"]
        cb_data = cb.get("data", "")
        chat_id = cb["message"]["chat"]["id"]
        msg_id = cb["message"]["message_id"]

        if cb_data.startswith("dep_app_") or cb_data.startswith("dep_rej_"):
            parts = cb_data.split("_")
            action = parts[1]
            tx_id = "_".join(parts[2:])

            tx = transactions.find_one({"transaction_id": tx_id, "status": "pending"})
            if not tx:
                try:
                    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb["id"], "text": "ይህ ጥያቄ ቀደም ብሎ ተይዟል!"})
                except:
                    pass
                return "OK"

            phone = tx["phone"]
            amount = tx["amount"]

            if action == "app":
                transactions.update_one({"transaction_id": tx_id}, {"$set": {"status": "approved"}})
                wallets.update_one({"phone": phone}, {"$inc": {"balance": amount}})
                user = wallets.find_one({"phone": phone})
                new_bal = user.get("balance", 0) if user else 0
                
                notify_user_balance_update(phone, new_bal)
                # 🌟 አድሚኑ አፕሩቭ ሲያደርግ ለተጠቃሚው ዌብሶኬት ኖቲፊኬሽን ይልካል
                notify_user_deposit_success(phone, amount)

                try:
                    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText", json={
                        "chat_id": chat_id, "message_id": msg_id,
                        "text": cb["message"]["text"] + f"\n\n✅ *APPROVED* by Admin", "parse_mode": "Markdown"
                    })
                except:
                    pass
            else:
                transactions.update_one({"transaction_id": tx_id}, {"$set": {"status": "rejected"}})
                try:
                    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText", json={
                        "chat_id": chat_id, "message_id": msg_id,
                        "text": cb["message"]["text"] + f"\n\n❌ *REJECTED* by Admin", "parse_mode": "Markdown"
                    })
                except:
                    pass

    return "OK"

if __name__ == '__main__':
    set_webhook()
    set_bot_commands()
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port)
