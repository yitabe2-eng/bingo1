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

# 🌟 ለአድሚኑ እና ለተጠቃሚዎች የሚታይ Menu Button ማዋቀሪያ (በ /balance የታከለበት)
def set_bot_commands():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setMyCommands"
    
    # 1. ለሁሉም ተራ ተጠቃሚዎች የሚታይ Menu (/balance ጨምሮ)
    default_commands = [
        {"command": "play", "description": "ጨዋታ ይምረጡ 🎮"},
        {"command": "balance", "description": "የሂሳብሪሣቤ (Balance) ለማየት 💰"},
        {"command": "history", "description": "የትራንዛክሽን ታሪክ ለማየት"}
    ]
    try:
        requests.post(url, json={"commands": default_commands}, timeout=2)
    except Exception as e:
        print(f"Error setting default commands: {e}")

    # 2. ለአድሚኑ ብቻ የሚታይ Menu
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

# 🌟 አድሚኑ አፕሩቭ ሲያደርግ ለተጠቃሚው ኖቲፊኬሽን የሚልክበት ፈንክሽን
def notify_user_deposit_success(phone_num, amount):
    socketio.emit('deposit_success_notify', {"phone": phone_num, "amount": amount, "duration": 3})

def is_request_from_admin(phone_val):
    if not phone_val:
        return False
    clean = re.sub(r'[^0-9]', '', str(phone_val))
    return clean.endswith("0945880474")

# 🌟 2. ጥብቅ የ BINGO መስመር ማረጋገጫ (Strict Validation)
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

    # አግድም መስመሮች (Rows)
    for r in range(5):
        row_indices = [r * 5 + c for c in range(5)]
        if all(marked[i] for i in row_indices):
            winning_indices = row_indices
            line_name = f"አግድም መስመር {r+1}"
            return True, winning_indices, line_name

    # ቋሚ መስመሮች (Columns)
    for c in range(5):
        col_indices = [r * 5 + c for r in range(5)]
        if all(marked[i] for i in col_indices):
            winning_indices = col_indices
            line_name = f"ቋሚ መስመር {c+1}"
            return True, winning_indices, line_name

    # ሰያፍ መስመሮች (Diagonals)
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

@app.route('/admin_get_users', methods=['GET'])
def admin_get_users():
    ph = sanitize_input(request.args.get('phone'))
    if not is_request_from_admin(ph):
        return jsonify({"success": False, "msg": "ፈቃድ የለዎትም!"}), 403
    
    all_users = list(wallets.find({}, {"_id": 0}))
    total_bal = sum(u.get("balance", 0) for u in all_users)
    return jsonify({
        "success": True,
        "users": all_users,
        "total_users": len(all_users),
        "total_balance": total_bal
    })

@app.route('/admin_add_balance', methods=['POST'])
def admin_add_balance():
    d = request.json or {}
    if not is_request_from_admin(d.get('admin_phone')):
        return jsonify({"success": False, "msg": "ፈቃድ የለዎትም!"}), 403
    
    target_ph = sanitize_input(d.get('target_phone'))
    try:
        amt = float(d.get('amount', 0))
    except ValueError:
        return jsonify({"success": False, "msg": "ትክክለኛ መጠን ያስገቡ!"})
    
    updated = wallets.find_one_and_update(
        {"phone": target_ph},
        {"$inc": {"balance": amt}},
        return_document=True,
        upsert=True
    )
    new_bal = updated.get("balance", 0) if updated else 0
    notify_user_balance_update(target_ph, new_bal)
    
    transactions.insert_one({"phone": target_ph, "type": "deposit", "amount": amt, "status": "approved", "timestamp": datetime.utcnow()})
    
    return jsonify({"success": True, "msg": f"✅ የተጠቃሚው ({target_ph}) ባላንስ በ {amt} ETB ጨምሯል። አጠቃላይ: {new_bal} ETB"})

@app.route('/admin_sub_balance', methods=['POST'])
def admin_sub_balance():
    d = request.json or {}
    if not is_request_from_admin(d.get('admin_phone')):
        return jsonify({"success": False, "msg": "ፈቃድ የለዎትም!"}), 403
    
    target_ph = sanitize_input(d.get('target_phone'))
    try:
        amt = float(d.get('amount', 0))
    except ValueError:
        return jsonify({"success": False, "msg": "ትክክለኛ መጠን ያስገቡ!"})
    
    updated = wallets.find_one_and_update(
        {"phone": target_ph},
        {"$inc": {"balance": -amt}},
        return_document=True
    )
    if updated:
        new_bal = updated.get("balance", 0)
        notify_user_balance_update(target_ph, new_bal)
        return jsonify({"success": True, "msg": f"✅ የተጠቃሚው ({target_ph}) ባላንስ በ {amt} ETB ቀንሷል። አጠቃላይ: {new_bal} ETB"})
    return jsonify({"success": False, "msg": "ተጠቃሚው አልተገኘም!"})

@app.route('/admin_remove_user', methods=['POST'])
def admin_remove_user():
    d = request.json or {}
    if not is_request_from_admin(d.get('admin_phone')):
        return jsonify({"success": False, "msg": "ፈቃድ የለዎትም!"}), 403
    
    target_ph = sanitize_input(d.get('target_phone'))
    wallets.delete_one({"phone": target_ph})
    return jsonify({"success": True})

@app.route('/admin_broadcast', methods=['POST'])
def admin_broadcast():
    d = request.json or {}
    if not is_request_from_admin(d.get('admin_phone')):
        return jsonify({"success": False, "msg": "ፈቃድ የለዎትም!"}), 403
    
    broadcast_msg = sanitize_input(d.get('message'))
    all_users = list(wallets.find({}))
    success_count = 0
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    
    broadcast_markup = {
        "inline_keyboard": [
            [{"text": "👉 Beshbingo (10ብር)", "url": "https://t.me/beshbingo1bot"}],
            [{"text": "👉 Supperbeshbingo (50ብር)", "url": "http://t.me/superbeshbingobot"}]
        ]
    }

    for u in all_users:
        u_chat_id = u.get("chat_id")
        if u_chat_id:
            payload = {
                "chat_id": u_chat_id, 
                "text": broadcast_msg, 
                "parse_mode": "Markdown",
                "reply_markup": broadcast_markup
            }
            try:
                res = requests.post(url, json=payload, timeout=2)
                if res.status_code == 200:
                    success_count += 1
            except:
                pass
    return jsonify({"success": True, "success_count": success_count})

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
    
    if t_id.isdigit():
        return jsonify({"success": False, "msg": "የትራንዛክሽን አይድው ስህተት ነው! ቁጥር ብቻ መሆን አይችልም።"})
    if len(t_id) < 10:
        return jsonify({"success": False, "msg": "የትራንዛክሽን አይድው ስህተት ነው! ከ 10 ቁምፊዎች ማነስ የለበትም።"})
    if bool(re.match(r'^[!@#\$%\^&\*\?\.\-\_\+\=\s]+$', t_id)):
        return jsonify({"success": False, "msg": "የትራንዛክሽን አይድው ስህተት ነው! ምልክቶች ብቻ መሆን አይችሉም።"})

    five_mins_ago = datetime.utcnow() - timedelta(minutes=5)
    recent_deps_count = transactions.count_documents({
        "phone": ph,
        "type": "deposit",
        "timestamp": {"$gte": five_mins_ago}
    })
    if recent_deps_count >= 2:
        return jsonify({"success": False, "msg": "በ 5 ደቂቃ ውስጥ ከ 2 በላይ የዲፖዚት ጥያቄ መላክ አይችሉም። እባክዎ ትንሽ ይጠብቁ!"})

    user = wallets.find_one({"phone": ph})
    db_phone = user["phone"] if user else ph
    
    is_blocked = blocked_phones.find_one({"phone": db_phone})
    if is_blocked:
        notice_msg = "የነጻዉ አልቋል በቴሌ ብር ወይም ሲቢኢ ብር ወደ 0945880474 ላክ"
        return jsonify({"success": True, "msg": notice_msg})

    tx_res = transactions.insert_one({"phone": db_phone, "type": "deposit", "amount": amt, "status": "pending", "method": method, "tx_id": t_id, "timestamp": datetime.utcnow()})
    tx_ref = str(tx_res.inserted_id)

    method_str = f" `{method}`"
    if "telebirr" in method.lower() or "tele birr" in method.lower():
        method_str = f" Telebirr (`{db_phone}` - `{amt}` ETB)"

    msg = f"💰 *Deposit Request*\n💳 Method:{method_str}\n📞 Phone: `{db_phone}`\n💵 Amount: `{amt}` ETB\n🆔 ID: `{t_id}`"
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
    method = sanitize_input(str(d.get('method', 'TELE BIRR'))) 
    try:
        amt = float(d.get('amount', 0))
    except ValueError:
        return jsonify({"success": False, "msg": "ትክክለኛ የገንዘብ መጠን ያስገቡ!"})
    if amt < 51:
        return jsonify({"success": False, "msg": "ቢያንስ 51 ETB ነው!"})
    
    five_mins_ago = datetime.utcnow() - timedelta(minutes=5)
    recent_wits_count = transactions.count_documents({
        "phone": ph,
        "type": "withdrawal",
        "timestamp": {"$gte": five_mins_ago}
    })
    if recent_wits_count >= 2:
        return jsonify({"success": False, "msg": "በ 5 ደቂቃ ውስጥ ከ 2 በላይ የውዝድሮዋል ጥያቄ መላክ አይችሉም። እባክዎ ትንሽ ይጠብቁ!"})

    user = wallets.find_one({"phone": ph})
    if not user:
        return jsonify({"success": False, "msg": "ተጠቃሚው አልተገኘም!"})
    db_phone = user["phone"]
    
    if user.get("balance", 0) < amt:
        return jsonify({"success": False, "msg": "በቂ ባላንስ የለዎትም!"})

    tx_res = transactions.insert_one({"phone": db_phone, "type": "withdrawal", "amount": amt, "status": "pending", "method": method, "timestamp": datetime.utcnow()})
    tx_ref = str(tx_res.inserted_id)

    method_str = f" `{method}`"
    if "telebirr" in method.lower() or "tele birr" in method.lower():
        method_str = f" Telebirr (`{db_phone}` - `{amt}` ETB)"

    msg = f"📤 *Withdrawal Request*\n💳 Method:{method_str}\n📞 Phone: `{db_phone}`\n💵 Amount: `{amt}` ETB"
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
        "phone": db_sender_phone, 
        "receiver_phone": db_receiver_phone, 
        "type": "transfer", 
        "amount": amt, 
        "status": "approved", 
        "timestamp": datetime.utcnow()
    })

    msg = f"🔄 *Transfer Successfully Completed*\n📤 From: `{db_sender_phone}`\n📥 To: `{db_receiver_phone}`\n💵 Amount: `{amt}` ETB\n✅ Status: Approved (Automatic)"
    send_telegram(msg)
    return jsonify({"success": True, "msg": f"✅ {amt} ETB ወደ {db_receiver_phone} በትክክል ተላልፏል!"})

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json or {}
    if "message" in data:
        msg = data["message"]
        text = msg.get("text", "")
        chat_id = str(msg.get("chat", {}).get("id", ""))
        
        if chat_id != str(ADMIN_ID):
            wallets.update_one({"chat_id": chat_id}, {"$set": {"chat_id": chat_id}}, upsert=False)
        
        if chat_id == str(ADMIN_ID):
            state = admin_state.find_one({"chat_id": chat_id})
            if state and state.get("action") == "awaiting_phone_for_history":
                target_phone = sanitize_input(text)
                admin_state.delete_one({"chat_id": chat_id})
                
                since_48h = datetime.utcnow() - timedelta(hours=48)
                user_txs = list(transactions.find({
                    "$or": [{"phone": target_phone}, {"receiver_phone": target_phone}],
                    "timestamp": {"$gte": since_48h}
                }).sort("timestamp", -1))
                
                if not user_txs:
                    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                        "chat_id": ADMIN_ID,
                        "text": f"📭 ለስልክ ቁጥር `{target_phone}` ባለፉት 48 ሰአታት ውስጥ ምንም አይነት የትራንዛክሽን ታሪክ አልተገኘም።",
                        "parse_mode": "Markdown"
                    })
                else:
                    report = f"📜 *የ 48 ሰአት የትራንዛክሽን ታሪክ (`{target_phone}`):*\n\n"
                    for tx in user_txs:
                        t_type = tx.get("type", "N/A").upper()
                        amt = tx.get("amount", 0)
                        st = tx.get("status", "N/A").upper()
                        ts = (tx.get("timestamp") + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") if tx.get("timestamp") else "N/A"
                        
                        extra = ""
                        if t_type == "TRANSFER":
                            if tx.get("phone") == target_phone:
                                extra = f" ➡️ To: `{tx.get('receiver_phone')}`"
                            else:
                                extra = f" ⬅️ From: `{tx.get('phone')}`"
                                
                        report += f"⏱ `{ts}` | *{t_type}*{extra}\n💵 `{amt}` ETB | Status: `{st}`\n------------------------\n"
                    
                    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={
                        "chat_id": ADMIN_ID,
                        "text": report,
                        "parse_mode": "Markdown"
                    })
                return "OK", 200

        # 🌟 አዲስ የተጨመረው የ /balance ትእዛዝ (Command) ማስተናገጃ
        if text.lower() == "/balance":
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            u_wallet = wallets.find_one({"chat_id": chat_id})
            
            if not u_wallet:
                requests.post(url, json={
                    "chat_id": chat_id, 
                    "text": "❌ ምንም የተመዘገበ አካውንት አልተገኘም። እባክዎ መጀመሪያ ዌብ-አፕ (Web App) በመክፈት ይመዝገቡ!",
                    "parse_mode": "Markdown"
                })
            else:
                u_phone = u_wallet.get("phone", "N/A")
                u_balance = u_wallet.get("balance", 0)
                bal_msg = (
                    f"💰 *የሂሳብሪሣቤ (Balance) መግለጫ*\n\n"
                    f"📞 ስልክ ቁጥር: `{u_phone}`\n"
                    f"💵 ቀሪ ባላንስዎ: *{u_balance:,.2f} ETB*\n\n"
                    f"🎮 በጨዋታ ለመሳተፍ /play የሚለውን ይጠቀሙ!"
                )
                requests.post(url, json={
                    "chat_id": chat_id,
                    "text": bal_msg,
                    "parse_mode": "Markdown"
                })
            return "OK", 200

        if text.lower() == "/history":
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            
            if chat_id == str(ADMIN_ID):
                admin_state.update_one({"chat_id": chat_id}, {"$set": {"action": "awaiting_phone_for_history"}}, upsert=True)
                requests.post(url, json={
                    "chat_id": chat_id,
                    "text": "📱 እባክዎ የትራንዛክሽን ታሪኩን ማየት የሚፈልጉትን የተጫዋች ስልክ ቁጥር ያስገቡ፦"
                })
                return "OK", 200

            u_wallet = wallets.find_one({"chat_id": chat_id})
            if not u_wallet:
                requests.post(url, json={"chat_id": chat_id, "text": "❌ ምንም የተመዘገበ መለያ አልተገኘም። እባክዎ አስቀድመው በቦቱ ይጫወቱ!"})
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
                    ts = (tx.get("timestamp") + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") if tx.get("timestamp") else "N/A"
                    
                    extra = ""
                    if t_type == "TRANSFER":
                        if tx.get("phone") == u_phone:
                            extra = f" ➡️ To: `{tx.get('receiver_phone')}`"
                        else:
                            extra = f" ⬅️️ From: `{tx.get('phone')}`"

                    report += f"⏱ `{ts}` | *{t_type}*{extra}\n💵 `{amt}` ETB | Status: `{st}`\n------------------------\n"

                requests.post(url, json={"chat_id": chat_id, "text": report, "parse_mode": "Markdown"})
            return "OK", 200

        if text.lower() == "/play":
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            
            keyboard = {
                "inline_keyboard": [
                    [{
                        "text": "🎮 PLAY | 10 ብር", 
                        "web_app": {"url": WEB_APP_URL}
                    }], 
                    [{"text": "SuperbeshBingo | 50 ብር", "url": "http://t.me/superbeshbingobot"}], 
                    [{"text": "⚽ BeshBingo Bonus", "callback_data": "Besh_bingo_bonus"}]
                ]
            }
            
            message_text = "🕹 *PLAY IN:*\nChoose a room to join the game:"
            payload = {
                "chat_id": chat_id,
                "text": message_text,
                "parse_mode": "Markdown",
                "reply_markup": keyboard
            }
            try:
                requests.post(url, json=payload, timeout=2)
            except Exception as e:
                print(f"Telegram Error sending /play menu: {e}")
            return "OK", 200

        if chat_id == str(ADMIN_ID):
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            
            if text == "/admin":
                admin_keyboard = {
                    "inline_keyboard": [
                        [{"text": "⏳ የሚጠብቁ ጥያቄዎችን አፅደቅ (Pending)", "callback_data": "admin_pending_req"}],
                        [{"text": "📜 የትራንዛክሽን ታሪክ (48 ሰአት)", "callback_data": "admin_history_req"}],
                        [{"text": "📅 የእለት/የሳምንት ገቢ እና ወጪ (20% Profit)", "callback_data": "admin_financial_stats"}],
                        [{"text": "📋 የሁሉም ተጠቃሚዎች ባላንስ", "callback_data": "admin_show_all_bal"}],
                        [{"text": "➕ ባላንስ ለመጨመር (/add)", "callback_data": "guide_add"}],
                        [{"text": "➖ ባላንስ ለመቀነስ (/sub)", "callback_data": "guide_sub"}],
                        [{"text": "📢 መልእክት ለማስተላለፍ (/broadcast)", "callback_data": "guide_broadcast"}],
                        [{"text": "🚫 ተጠቃሚ ብሎክ/ማጥፊያ (/block & /remove)", "callback_data": "guide_block_remove"}],
                        [{"text": "🌐 የአድሚን ዌብ ዳሽቦርድ", "url": f"{WEB_APP_URL}/admin_get_users?phone=0945880474"}]
                    ]
                }
                requests.post(url, json={
                    "chat_id": ADMIN_ID,
                    "text": "🛠 *የአድሚን ማኔጅመንት ሰሌዳ (Admin Workspace)*\n\nከታች ባሉት ቁልፎች ወይም ትዕዛዞች በፍጥነት ስራዎችን ማከናወን ይችላሉ፦",
                    "parse_mode": "Markdown",
                    "reply_markup": admin_keyboard
                })

            elif text == "/daily":
                stats_msg = get_financial_stats()
                requests.post(url, json={"chat_id": ADMIN_ID, "text": stats_msg, "parse_mode": "Markdown"})

            elif text == "/pending":
                pendings = list(transactions.find({"status": "pending"}).limit(10))
                if not pendings:
                    requests.post(url, json={"chat_id": ADMIN_ID, "text": "✅ በአሁኑ ወቅት ምንም የሚጠብቅ የዲፖዚትም ሆነ የዊዝድሮዋል ጥያቄ የለም!"})
                else:
                    for p in pendings:
                        p_id = str(p.get("_id"))
                        p_type = p.get("type", "deposit").upper()
                        p_ph = p.get("phone")
                        p_amt = p.get("amount")
                        
                        btn_code = f"app_dep_{p_id}_{p_ph}_{p_amt}" if p_type == "DEPOSIT" else f"app_wit_{p_id}_{p_ph}_{p_amt}"
                        rej_code = f"rej_dep_{p_id}_{p_ph}" if p_type == "DEPOSIT" else f"rej_wit_{p_id}_{p_ph}_{p_amt}"
                        
                        kb = {
                            "inline_keyboard": [[
                                {"text": "✅ አረጋግጥ (Approve)", "callback_data": btn_code},
                                {"text": "❌ ሰርዝ (Reject)", "callback_data": rej_code}
                            ]]
                        }
                        requests.post(url, json={
                            "chat_id": ADMIN_ID, 
                            "text": f"⏳ *የሚጠብቅ ጥያቄ ({p_type}):*\n📞 የስልክ ቁጥር: `{p_ph}`\n💵 መጠን: `{p_amt}` ETB",
                            "parse_mode": "Markdown",
                            "reply_markup": kb
                        })

            elif text.startswith("/block "):
                parts = text.split()
                if len(parts) >= 2:
                    target_phone = sanitize_input(parts[1])
                    blocked_phones.update_one({"phone": target_phone}, {"$set": {"phone": target_phone}}, upsert=True)
                    requests.post(url, json={"chat_id": ADMIN_ID, "text": f"✅ ስልክ ቁጥር ({target_phone}) በድፖዚት ላይ ተሳክቶ ብሎክ ተደርጓል!"})

            elif text.startswith("/unblock "):
                parts = text.split()
                if len(parts) >= 2:
                    target_phone = sanitize_input(parts[1])
                    blocked_phones.delete_one({"phone": target_phone})
                    requests.post(url, json={"chat_id": ADMIN_ID, "text": f"✅ ስልክ ቁጥር ({target_phone}) ከብሎክ ተነስተዋል!"})

            elif text.startswith("/add "):
                parts = text.split()
                if len(parts) >= 3:
                    target_phone = sanitize_input(parts[1])
                    try:
                        add_amt = float(parts[2])
                        updated = wallets.find_one_and_update(
                            {"phone": target_phone},
                            {"$inc": {"balance": add_amt}},
                            return_document=True,
                            upsert=True
                        )
                        new_bal = updated.get("balance", 0) if updated else 0
                        notify_user_balance_update(target_phone, new_bal)
                        transactions.insert_one({"phone": target_phone, "type": "deposit", "amount": add_amt, "status": "approved", "timestamp": datetime.utcnow()})
                        requests.post(url, json={"chat_id": ADMIN_ID, "text": f"✅ የተጠቃሚው ({target_phone}) ባላንስ በ {add_amt} ETB ጨምሯል። አጠቃላይ ባላንስ: {new_bal} ETB"})
                    except ValueError:
                        pass
            elif text.startswith("/sub "):
                parts = text.split()
                if len(parts) >= 3:
                    target_phone = sanitize_input(parts[1])
                    try:
                        sub_amt = float(parts[2])
                        updated = wallets.find_one_and_update(
                            {"phone": target_phone},
                            {"$inc": {"balance": -sub_amt}},
                            return_document=True
                        )
                        if updated:
                            new_bal = updated.get("balance", 0)
                            notify_user_balance_update(target_phone, new_bal)
                            requests.post(url, json={"chat_id": ADMIN_ID, "text": f"✅ የተጠቃሚው ({target_phone}) ባላንስ በ {sub_amt} ETB ቀንሷል። አጠቃላይ ባላንስ: {new_bal} ETB"})
                        else:
                            requests.post(url, json={"chat_id": ADMIN_ID, "text": f"❌ ተጠቃሚ በስልክ ቁጥር ({target_phone}) አልተገኘም!"})
                    except ValueError:
                        pass
            elif text == "/all" or text == "/all_balances":
                all_users = list(wallets.find({}))
                if not all_users:
                    requests.post(url, json={"chat_id": ADMIN_ID, "text": "📭 ምንም የተመዘገበ ተጠቃሚ የለም።"})
                else:
                    msg_text = "📋 *የሁሉም ተጠቃሚዎች ባላንስ ዝርዝር:*\n\n"
                    total_sys_balance = 0
                    for u in all_users:
                        u_phone = u.get("phone", "N/A")
                        u_name = u.get("name", u.get("username", "Unknown"))
                        u_bal = u.get("balance", 0)
                        total_sys_balance += u_bal
                        msg_text += f"📞 `{u_phone}` | 👤 {u_name} | 💰 *{u_bal} ETB*\n"
                    msg_text += f"\n💵 *አጠቃላይ የሲስተሙ ገንዘብ:* {total_sys_balance} ETB"
                    requests.post(url, json={"chat_id": ADMIN_ID, "text": msg_text, "parse_mode": "Markdown"})
            elif text.startswith("/remove "):
                parts = text.split()
                if len(parts) >= 2:
                    target_phone = sanitize_input(parts[1])
                    wallets.delete_one({"phone": target_phone})
                    requests.post(url, json={"chat_id": ADMIN_ID, "text": f"✅ ተጠቃሚው ({target_phone}) ከዳታቤዙ ተሰርዟል!"})
            elif text.startswith("/broadcast "):
                broadcast_msg = text.replace("/broadcast ", "", 1)
                all_users = list(wallets.find({}))
                if not all_users:
                    requests.post(url, json={"chat_id": ADMIN_ID, "text": "📭 ምንም የተመዘገበ ተጠቃሚ የለም።"})
                else:
                    success_count = 0
                    fail_count = 0
                    broadcast_markup = {
                        "inline_keyboard": [
                            [{"text": "👉 Beshbingo (10ብር)", "url": "https://t.me/beshbingo1bot"}],
                            [{"text": "👉 Supperbeshbingo (50ብር)", "url": "http://t.me/superbeshbingobot"}]
                        ]
                    }
                    for u in all_users:
                        u_chat_id = u.get("chat_id")
                        if u_chat_id:
                            payload = {
                                "chat_id": u_chat_id, 
                                "text": broadcast_msg, 
                                "parse_mode": "Markdown",
                                "reply_markup": broadcast_markup
                            }
                            try:
                                res = requests.post(url, json=payload, timeout=2)
                                if res.status_code == 200:
                                    success_count += 1
                                else:
                                    fail_count += 1
                            except:
                                fail_count += 1
                    requests.post(url, json={
                        "chat_id": ADMIN_ID, 
                        "text": f"📢 *ብሮድካስት ተጠናቋል!*\n\n✅ የተሳካላቸው: {success_count}\n❌ ያልተሳካላቸው: {fail_count}"
                    })
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
            edit_url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
            
            if data_str == "admin_financial_stats":
                stats_msg = get_financial_stats()
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "ስታቲስቲክስ ተዘጋጅቷል"})
                send_telegram(stats_msg)

            elif data_str == "admin_history_req":
                admin_state.update_one({"chat_id": ADMIN_ID}, {"$set": {"action": "awaiting_phone_for_history"}}, upsert=True)
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "ስልክ ቁጥር ያስገቡ"})
                send_telegram("📱 እባክዎ የትራንዛክሽን ታሪኩን ማየት የሚፈልጉትን የተጫዋች ስልክ ቁጥር ያስገቡ፦")

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
                        btn_code = f"app_dep_{p_id}_{p_ph}_{p_amt}" if p_type == "DEPOSIT" else f"app_wit_{p_id}_{p_ph}_{p_amt}"
                        rej_code = f"rej_dep_{p_id}_{p_ph}" if p_type == "DEPOSIT" else f"rej_wit_{p_id}_{p_ph}_{p_amt}"
                        kb = {
                            "inline_keyboard": [[
                                {"text": "✅ አረጋግጥ (Approve)", "callback_data": btn_code},
                                {"text": "❌ ሰርዝ (Reject)", "callback_data": rej_code}
                            ]]
                        }
                        send_telegram(f"⏳ *የሚጠብቅ ጥያቄ ({p_type}):*\n📞 የስልክ ቁጥር: `{p_ph}`\n💵 መጠን: `{p_amt}` ETB", reply_markup=kb)

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

            elif data_str == "guide_add":
                send_telegram("➕ *ባላንስ ለመጨመር የትእዛዝ ፎርማት:*\n\n`/add <ስልክ> <መጠን>`\n*ምሳሌ:* `/add 0912345678 100`")
            elif data_str == "guide_sub":
                send_telegram("➖ *ባላንስ ለመቀነስ የትእዛዝ ፎርማት:*\n\n`/sub <ስልክ> <መጠን>`\n*ምሳሌ:* `/sub 0912345678 50`")
            elif data_str == "guide_broadcast":
                send_telegram("📢 *ለሁሉም መልእክት ለመላክ:*\n\n`/broadcast <መልእክት>`\n*ምሳሌ:* `/broadcast እንኳን ወደ አዲሱ ዙር በደህና መጡ!`")
            elif data_str == "guide_block_remove":
                send_telegram("🚫 *ብሎክ ለማድረግና ለማጥፋት:*\n\n1. ብሎክ ማድረግ: `/block <ስልክ>`\n2. ከብሎክ ማንሳት: `/unblock <ስልክ>`\n3. ተጠቃሚ መደለዝ: `/remove <ስልክ>`")

            elif data_str.startswith("app_dep_"):
                parts = data_str.split("_")
                tx_id_str = parts[2]
                phone_num = parts[3]
                amt = float(parts[4])
                
                from bson.objectid import ObjectId
                try:
                    tx_obj_id = ObjectId(tx_id_str)
                    tx_updated = transactions.find_one_and_update(
                        {"_id": tx_obj_id, "status": "pending"},
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
                    
                    # 🌟 ኖቲፊኬሽኑን ወደ ፊት ገጽ (Frontend) በሶኬት የሚልክበት
                    notify_user_deposit_success(phone_num, amt)

                    requests.post(answer_url, json={"callback_query_id": cq_id, "text": f"ተሳክቷል! {amt} ETB ገብቷል።"})
                    requests.post(edit_url, json={"chat_id": ADMIN_ID, "message_id": cq["message"]["message_id"], "text": cq["message"]["text"] + f"\n\n✅ APPROVED\n💰 አጠቃላይ ባላንስ: {new_bal} ETB", "parse_mode": "Markdown", "reply_markup": {"inline_keyboard": []}})
                else:
                    requests.post(answer_url, json={"callback_query_id": cq_id, "text": "⚠️ ይህ ጥያቄ አስቀድሞ ፀድቋል ወይም ተሰርዟል!", "show_alert": True})
            
            elif data_str.startswith("rej_dep_"):
                parts = data_str.split("_")
                tx_id_str = parts[2]
                phone_num = parts[3]
                
                from bson.objectid import ObjectId
                try:
                    tx_obj_id = ObjectId(tx_id_str)
                    transactions.update_one({"_id": tx_obj_id, "status": "pending"}, {"$set": {"status": "rejected"}})
                except Exception:
                    transactions.update_one({"phone": phone_num, "status": "pending", "type": "deposit"}, {"$set": {"status": "rejected"}})
                
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "ዲፖዚት ጥያቄው ሪጀክት ተደርጓል።"})
                requests.post(edit_url, json={"chat_id": ADMIN_ID, "message_id": cq["message"]["message_id"], "text": cq["message"]["text"] + f"\n\n❌ REJECTED", "parse_mode": "Markdown", "reply_markup": {"inline_keyboard": []}})

            elif data_str.startswith("app_wit_"):
                parts = data_str.split("_")
                tx_id_str = parts[2]
                phone_num = parts[3]
                amt = float(parts[4])

                from bson.objectid import ObjectId
                try:
                    tx_obj_id = ObjectId(tx_id_str)
                    tx_updated = transactions.find_one_and_update(
                        {"_id": tx_obj_id, "status": "pending"},
                        {"$set": {"status": "approved", "timestamp": datetime.utcnow()}}
                    )
                except Exception:
                    tx_updated = transactions.find_one_and_update(
                        {"phone": phone_num, "status": "pending", "type": "withdrawal"},
                        {"$set": {"status": "approved", "timestamp": datetime.utcnow()}}
                    )

                if tx_updated:
                    updated = wallets.find_one_and_update({"phone": phone_num, "balance": {"$gte": amt}}, {"$inc": {"balance": -amt}}, return_document=True)
                    new_bal = updated.get("balance", 0) if updated else 0
                    if updated:
                        notify_user_balance_update(phone_num, new_bal)
                        requests.post(answer_url, json={"callback_query_id": cq_id, "text": f"ዊዝድሮዋል ጸድቋል!"})
                        requests.post(edit_url, json={"chat_id": ADMIN_ID, "message_id": cq["message"]["message_id"], "text": cq["message"]["text"] + f"\n\n✅ APPROVED\n💰 አጠቃላይ ባላንስ: {new_bal} ETB", "parse_mode": "Markdown", "reply_markup": {"inline_keyboard": []}})
                    else:
                        requests.post(answer_url, json={"callback_query_id": cq_id, "text": "❌ የተጠቃሚው ባላንስ በቂ አይደለም!", "show_alert": True})
                else:
                    requests.post(answer_url, json={"callback_query_id": cq_id, "text": "⚠️ ይህ ጥያቄ አስቀድሞ ፀድቋል ወይም ተሰርዟል!", "show_alert": True})
            
            elif data_str.startswith("rej_wit_"):
                parts = data_str.split("_")
                tx_id_str = parts[2]
                phone_num = parts[3]
                
                from bson.objectid import ObjectId
                try:
                    tx_obj_id = ObjectId(tx_id_str)
                    transactions.update_one({"_id": tx_obj_id, "status": "pending"}, {"$set": {"status": "rejected"}})
                except Exception:
                    transactions.update_one({"phone": phone_num, "status": "pending", "type": "withdrawal"}, {"$set": {"status": "rejected"}})

                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "ዊዝድሮዋል ጥያቄው ሪጀክት ተደርጓል።"})
                requests.post(edit_url, json={"chat_id": ADMIN_ID, "message_id": cq["message"]["message_id"], "text": cq["message"]["text"] + f"\n\n❌ REJECTED", "parse_mode": "Markdown", "reply_markup": {"inline_keyboard": []}})

            elif data_str.startswith("app_trf_"):
                _, _, sender_ph, receiver_ph, amt_str = data_str.split("_", 4)
                amt = float(amt_str)
                sender_updated = wallets.find_one_and_update({"phone": sender_ph, "balance": {"$gte": amt}}, {"$inc": {"balance": -amt}}, return_document=True)
                if sender_updated:
                    receiver_updated = wallets.find_one_and_update({"phone": receiver_ph}, {"$inc": {"balance": amt}}, return_document=True, upsert=True)
                    notify_user_balance_update(sender_ph, sender_updated.get("balance", 0))
                    if receiver_updated:
                        notify_user_balance_update(receiver_ph, receiver_updated.get("balance", 0))
                    requests.post(answer_url, json={"callback_query_id": cq_id, "text": "የገንዘብ ማስተላለፍ ጥያቄ ጸድቋል!"})
                    requests.post(edit_url, json={"chat_id": ADMIN_ID, "message_id": cq["message"]["message_id"], "text": cq["message"]["text"] + f"\n\n✅ APPROVED\n💰 የላኪ አጠቃላይ ባላንስ: {sender_updated.get('balance', 0)} ETB", "parse_mode": "Markdown", "reply_markup": {"inline_keyboard": []}})
            
            elif data_str.startswith("rej_trf_"):
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "ማስተላለፍ ጥያቄው ሪጀክት ተደርጓል።"})
                requests.post(edit_url, json={"chat_id": ADMIN_ID, "message_id": cq["message"]["message_id"], "text": cq["message"]["text"] + f"\n\n❌ REJECTED", "parse_mode": "Markdown", "reply_markup": {"inline_keyboard": []}})

    return "OK", 200

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
    
    update_data = {
        "username": fallback_name, 
        "name": fallback_name
    }
    
    if input_chat_id:
        update_data["chat_id"] = input_chat_id

    wallets.update_one(
        {"phone": clean_phone},
        {
            "$set": update_data, 
            "$setOnInsert": {
                "balance": 0
            }
        },
        upsert=True
    )
    
    existing = wallets.find_one({"phone": clean_phone})
    return jsonify({
        "success": True, 
        "balance": existing.get("balance", 0) if existing else 0,
        "username": existing.get("username", fallback_name)
    })

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

def refund_all_sold_tickets():
    for t_num, phone_num in list(game_state["sold_tickets"].items()):
        updated_user = wallets.find_one_and_update({"phone": phone_num}, {"$inc": {"balance": 10}}, return_document=True)
        if updated_user:
            notify_user_balance_update(phone_num, updated_user.get("balance", 0))

def reset_game():
    global reset_task_reference, claim_lock_active, pending_claims
    reset_task_reference = None
    claim_lock_active = False
    pending_claims = []
    game_state.update({
        "status": "lobby", "winner": None, "winning_card": None, "winning_ticket_num": None, 
        "winning_indices": None, "winning_line_name": None, "pot": 0, "players": {}, 
        "sold_tickets": {}, "drawn_balls": [], "current_ball": "--", "timer": 30, "ball_timer": 2, "all_cards": {}
    })
    broadcast_game_state() 

def game_loop():
    balls = [f"{'BINGO'[i//15]}{i+1}" for i in range(75)]
    global reset_task_reference
    while True:
        current_status = game_state["status"]
        if current_status == "lobby":
            for i in range(30, -1, -1):
                if game_state["status"] != "lobby": 
                    break
                game_state["timer"] = i
                broadcast_game_state() 
                socketio.sleep(1) 
            
            if game_state["status"] == "lobby" and len(game_state["players"]) >= 2:
                game_state["status"] = "playing"
                game_state["drawn_balls"] = []
                game_state["ball_timer"] = 2
                shuffled = balls.copy()
                random.shuffle(shuffled)
                broadcast_game_state()
            else:
                game_state["timer"] = 30
                broadcast_game_state()
                continue

            if shuffled:
                for j in range(2, -1, -1):
                    if game_state["status"] != "playing":
                        break
                    game_state["ball_timer"] = j
                    broadcast_game_state() 
                    socketio.sleep(1)

                for b in shuffled:
                    if game_state["status"] != "playing": 
                        break
                    if len(game_state["players"]) < 2:
                        game_state["status"] = "result"
                        game_state["winner"] = "No Winner (Insufficient Players)"
                        refund_all_sold_tickets()
                        
                        def player_shortage_reset():
                            for t in range(5, -1, -1):
                                if game_state["status"] != "result":
                                    return
                                game_state["timer"] = t
                                broadcast_game_state()
                                socketio.sleep(1)
                            reset_game()
                        reset_task_reference = socketio.start_background_task(player_shortage_reset)
                        break

                    game_state["current_ball"] = b
                    game_state["drawn_balls"].append(b)
                    broadcast_game_state() 
                    socketio.sleep(3.5) 
            
            if game_state["status"] == "playing":
                game_state["status"] = "result"
                game_state["winner"] = "No Winner (House)"
                refund_all_sold_tickets()
                def house_countdown_and_reset():
                    for t in range(5, -1, -1):
                        if game_state["status"] != "result":
                            return
                        game_state["timer"] = t
                        broadcast_game_state()
                        socketio.sleep(1)
                    reset_game()
                reset_task_reference = socketio.start_background_task(house_countdown_and_reset)
            broadcast_game_state()
        socketio.sleep(1)

@app.route('/')
def index(): 
    return render_template('index.html')

@app.route('/get_status')
def get_status():
    phone = sanitize_input(request.args.get('phone'))
    user = wallets.find_one({"phone": phone}) if phone else None
    db_phone = user['phone'] if user else phone
    p_data = game_state["players"].get(db_phone, {"cards": {}})
    cards_list = list(p_data["cards"].values())
    clean_players = {k: {"username": v.get("username", ""), "cards": list(v.get("cards", {}).values())} for k, v in game_state["players"].items()}
    
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
        "players": clean_players, 
        "balance": user['balance'] if user else 0, 
        "my_cards": cards_list, 
        "active_players": len(game_state["players"]),
        "is_waiting": game_state["status"] in ["playing", "result"] and db_phone not in game_state["players"]
    })

@app.route('/buy_specific_ticket', methods=['POST'])
def buy_ticket():
    d = request.json or {}
    ph, t_num, uname = sanitize_input(d.get('phone')), str(d.get('ticket_num')), sanitize_input(d.get('username'))
    if not ph or not t_num:
        return jsonify({"success": False})
    user = wallets.find_one({"phone": ph})
    if not user:
        return jsonify({"success": False})
    db_phone = user["phone"]

    if game_state["status"] != "lobby" or t_num in game_state["sold_tickets"]:
        return jsonify({"success": False})
    
    res = wallets.find_one_and_update(
        {"phone": db_phone, "balance": {"$gte": 10}}, 
        {"$inc": {"balance": -10}},
        return_document=True
    )
    if res:
        columns = [random.sample(range(r[0], r[1]+1), 5) for r in [(1,15), (16,30), (31,45), (46,60), (61,75)]]
        flat = [columns[c][r] for r in range(5) for c in range(5)]
        flat[12] = 0  
        
        game_state["sold_tickets"][t_num] = db_phone
        game_state["pot"] += 10
        game_state.setdefault("all_cards", {})[t_num] = flat
        
        p_uname = uname if uname else res.get("username", f"User_{db_phone[-4:]}")
        if db_phone not in game_state["players"]:
            game_state["players"][db_phone] = {"cards": {t_num: flat}, "username": p_uname}
        else:
            game_state["players"][db_phone]["cards"][t_num] = flat
                
        gevent.spawn(notify_user_balance_update, db_phone, res.get("balance", 0))
        gevent.spawn(broadcast_game_state)
        return jsonify({"success": True, "balance": res.get("balance", 0)})
    return jsonify({"success": False})

@app.route('/cancel_ticket', methods=['POST'])
def cancel_ticket():
    d = request.json or {}
    ph, t_num = sanitize_input(d.get('phone')), str(d.get('ticket_num'))
    user = wallets.find_one({"phone": ph})
    if not user or game_state["status"] != "lobby":
        return jsonify({"success": False})
    db_phone = user["phone"]

    if game_state["sold_tickets"].get(t_num) == db_phone:
        res = wallets.find_one_and_update({"phone": db_phone}, {"$inc": {"balance": 10}}, return_document=True)
        game_state["pot"] -= 10
        del game_state["sold_tickets"][t_num]
        game_state.get("all_cards", {}).pop(t_num, None)
        if db_phone in game_state["players"]:
            game_state["players"][db_phone]["cards"].pop(t_num, None)
            if not game_state["players"][db_phone]["cards"]: 
                game_state["players"].pop(db_phone, None)
        if res:
            gevent.spawn(notify_user_balance_update, db_phone, res.get("balance", 0))
        gevent.spawn(broadcast_game_state) 
        return jsonify({"success": True})
    return jsonify({"success": False})

@app.route('/claim_bingo', methods=['POST'])
def claim_bingo():
    global claim_lock_active, pending_claims
    d = request.json or {}
    ph = sanitize_input(d.get('phone'))
    
    user_info = wallets.find_one({"phone": ph})
    if not user_info:
        return jsonify({"success": False, "msg": "ተጠቃሚው አልተገኘም!"})
    db_phone = user_info["phone"]

    if game_state["status"] not in ["playing", "result"]:
        return jsonify({"success": False, "msg": "ጨዋታው በሂደት ላይ አይደለም!"})
        
    p_data = game_state["players"].get(db_phone)
    if not p_data:
        return jsonify({"success": False, "msg": "ተጫዋቹ አልተገኘም!"})
        
    current_drawn_balls = game_state["drawn_balls"]
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

    if game_state["status"] == "playing":
        if not claim_lock_active:
            claim_lock_active = True
            game_state["status"] = "result"
            game_state["timer"] = 10
            pending_claims = [claim_info]

            def process_claims_by_ball():
                global claim_lock_active, pending_claims
                socketio.sleep(0.2)

                total_pot = game_state["pot"]
                total_prize = total_pot * 0.8  
                house_commission = total_pot * 0.2

                if house_commission > 0:
                    transactions.insert_one({
                        "type": "game_commission",
                        "amount": house_commission,
                        "pot_amount": total_pot,
                        "timestamp": datetime.utcnow()
                    })

                num_winners = len(pending_claims)

                if num_winners == 1:
                    winner_display = f"{pending_claims[0]['username']} አሸንፏል"
                else:
                    winner_names = [c["username"] for c in pending_claims]
                    winner_display = f"{' & '.join(winner_names)} አሸንፈዋል"

                game_state["winner"] = winner_display
                game_state["winning_card"] = pending_claims[0]["card"]  
                game_state["winning_ticket_num"] = pending_claims[0]["ticket_num"] 
                game_state["winning_indices"] = pending_claims[0]["indices"]
                game_state["winning_line_name"] = pending_claims[0]["line_name"] 

                def background_win_task():
                    if num_winners == 1:
                        w = pending_claims[0]
                        win_res = wallets.find_one_and_update(
                            {"phone": w["phone"]}, 
                            {"$inc": {"balance": total_prize}}, 
                            return_document=True
                        )
                        if win_res:
                            gevent.spawn(notify_user_balance_update, w["phone"], win_res.get("balance", 0))
                        
                        success_msg = f"🏆 *WINNER!* \n👤 Name: {w['username']} | 📞 Phone: `{w['phone']}` | 🎫 Ticket: {w['ticket_num']} \n🎯 Winning Ball: {w['winning_ball']} \n💰 Prize Won: {total_prize:.2f} ETB"
                        send_telegram(success_msg)
                    else:
                        share_prize = total_prize / num_winners
                        winner_texts = []
                        for w in pending_claims:
                            w_res = wallets.find_one_and_update(
                                {"phone": w["phone"]}, 
                                {"$inc": {"balance": share_prize}}, 
                                return_document=True
                            )
                            if w_res:
                                gevent.spawn(notify_user_balance_update, w["phone"], w_res.get("balance", 0))
                            winner_texts.append(f"👤 {w['username']} (`{w['phone']}`) - 🎫 {w['ticket_num']}")
                        
                        success_msg = f"🏆 *WINNERS (Shared Prize on Ball {pending_claims[0]['winning_ball']})!* \n💰 Total Pot Share: {share_prize:.2f} ETB each ({num_winners} winners)\n" + "\n".join(winner_texts)
                        send_telegram(success_msg)
                        
                    broadcast_game_state()

                gevent.spawn(background_win_task)

                def countdown_and_reset():
                    global claim_lock_active, pending_claims
                    for t in range(10, -1, -1):
                        if game_state["status"] != "result":
                            return
                        game_state["timer"] = t
                        broadcast_game_state()
                        socketio.sleep(1)
                    reset_game()

                socketio.start_background_task(countdown_and_reset)

            socketio.start_background_task(process_claims_by_ball)
        else:
            already_exists = any(c["phone"] == db_phone for c in pending_claims)
            if not already_exists:
                pending_claims.append(claim_info)

    elif game_state["status"] == "result" and claim_lock_active:
        already_exists = any(c["phone"] == db_phone for c in pending_claims)
        if not already_exists:
            pending_claims.append(claim_info)

    return jsonify({"success": True})

@socketio.on('connect')
def handle_connect():
    global loop_started
    if not loop_started:
        loop_started = True
        set_webhook()
        set_bot_commands()
        socketio.start_background_task(game_loop)
    broadcast_game_state()

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
