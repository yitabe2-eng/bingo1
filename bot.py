import os
import time
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

try:
    wallets.create_index("phone", unique=True)
    blocked_phones.create_index("phone", unique=True)
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

# --- 🌐 WEB ROUTES & ADMIN PANEL APIs ---

@app.route('/')
def index(): 
    return render_template('index.html')

@app.route('/admin_panel')
def admin_panel():
    return render_template('admin.html')  # የአድሚን ማስተካከያ ፔጅ (HTML)

@app.route('/api/admin/check_auth', methods=['POST'])
def admin_check_auth():
    d = request.json or {}
    chat_id = str(d.get('chat_id', '')).strip()
    if chat_id == str(ADMIN_ID):
        return jsonify({"is_admin": True})
    return jsonify({"is_admin": False})

@app.route('/api/admin/stats', methods=['POST'])
def admin_stats():
    d = request.json or {}
    if str(d.get('chat_id', '')) != str(ADMIN_ID):
        return jsonify({"success": False, "msg": "Unauthorized"})
    
    all_users = list(wallets.find({}, {"_id": 0}))
    total_balance = sum(u.get("balance", 0) for u in all_users)
    blocked_list = list(blocked_phones.find({}, {"_id": 0}))
    
    return jsonify({
        "success": True,
        "users": all_users,
        "total_balance": total_balance,
        "blocked": blocked_list
    })

@app.route('/api/admin/action', methods=['POST'])
def admin_action():
    d = request.json or {}
    if str(d.get('chat_id', '')) != str(ADMIN_ID):
        return jsonify({"success": False, "msg": "Unauthorized"})
    
    action_type = d.get('action')
    phone = sanitize_input(d.get('phone'))
    amount = float(d.get('amount', 0) or 0)

    if action_type == 'add':
        updated = wallets.find_one_and_update(
            {"phone": phone},
            {"$inc": {"balance": amount}},
            return_document=True,
            upsert=True
        )
        new_bal = updated.get("balance", 0) if updated else 0
        notify_user_balance_update(phone, new_bal)
        return jsonify({"success": True, "msg": f"ባላንስ በ {amount} ተጨምሯል። አጠቃላይ: {new_bal}"})

    elif action_type == 'sub':
        updated = wallets.find_one_and_update(
            {"phone": phone},
            {"$inc": {"balance": -amount}},
            return_document=True
        )
        if updated:
            new_bal = updated.get("balance", 0)
            notify_user_balance_update(phone, new_bal)
            return jsonify({"success": True, "msg": f"ባላንስ በ {amount} ቀንሷል። አጠቃላይ: {new_bal}"})
        return jsonify({"success": False, "msg": "ተጠቃሚው አልተገኘም!"})

    elif action_type == 'block':
        blocked_phones.update_one({"phone": phone}, {"$set": {"phone": phone}}, upsert=True)
        return jsonify({"success": True, "msg": f"ቁጥር {phone} ታግዷል።"})

    elif action_type == 'unblock':
        blocked_phones.delete_one({"phone": phone})
        return jsonify({"success": True, "msg": f"ቁጥር {phone} ከብሎክ ወጥቷል።"})

    elif action_type == 'remove':
        wallets.delete_one({"phone": phone})
        return jsonify({"success": True, "msg": f"ተጠቃሚው {phone} ተሰርዟል።"})

    return jsonify({"success": False, "msg": "ልክ ያልሆነ ትዕዛዝ!"})

# --- ተራ ዌብ ራውቶች (Deposit, Withdrawal, Transfer) ---

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
    
    is_blocked = blocked_phones.find_one({"phone": db_phone})
    if is_blocked:
        notice_msg = "የነጻዉ አልቋል በቴሌ ብር ወይም ሲቢኢ ብር ወደ 0945880474 ላክ"
        return jsonify({"success": True, "msg": notice_msg})

    msg = f"💰 *Deposit Request*\n💳 Method: `{method}`\n📞 Phone: `{db_phone}`\n💵 Amount: `{amt}` ETB\n🆔 ID: `{t_id}`"
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ አረጋግጥ (Approve)", "callback_data": f"app_dep_{db_phone}_{amt}"},
                {"text": "❌ሰርዝ (Reject)", "callback_data": f"rej_dep_{db_phone}"}
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
    user = wallets.find_one({"phone": ph})
    if not user:
        return jsonify({"success": False, "msg": "ተጠቃሚው አልተገኘም!"})
    db_phone = user["phone"]
    
    if user.get("balance", 0) < amt:
        return jsonify({"success": False, "msg": "በቂ ባላንስ የለዎትም!"})

    msg = f"📤 *Withdrawal Request*\n💳 Method: `{method}`\n📞 Phone: `{db_phone}`\n💵 Amount: `{amt}` ETB"
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ አረጋግጥ (Approve)", "callback_data": f"app_wit_{db_phone}_{amt}"},
                {"text": "❌ሰርዝ (Reject)", "callback_data": f"rej_wit_{db_phone}_{amt}"}
            ]
        ]
    }
    send_telegram(msg, reply_markup=keyboard)
    return jsonify({"success": True, "msg": "የውዝድሮዋል ጥያቄዎ ለአድሚን ተልኳል!"})

@app.route('/request_transfer', methods=['POST'])
def request_transfer():
    d = request.json or {}
    raw_sender_ph = sanitize_input(str(d.get('phone', '')))
    raw_receiver_ph = sanitize_input(str(d.get('receiver_phone', '')))
    
    sender_ph = raw_sender_ph.replace("+", "").replace(" ", "")
    receiver_ph = raw_receiver_ph.replace("+", "").replace(" ", "")

    try:
        amt = float(d.get('amount', 0))
    except ValueError:
        return jsonify({"success": False, "msg": "ትክክለኛ መጠን ያስገቡ!"})
    
    if amt <= 0:
        return jsonify({"success": False, "msg": "ትክክለኛ የገንዘብ መጠን ያስገቡ!"})
    
    sender = wallets.find_one({
        "$or": [
            {"phone": sender_ph}, 
            {"phone": raw_sender_ph},
            {"phone": f"+{sender_ph}"}
        ]
    })
    
    if not sender:
        return jsonify({"success": False, "msg": "ላኪው ተጠቃሚ አልተገኘም!"})
    
    if sender.get("balance", 0) < amt:
        return jsonify({"success": False, "msg": f"በቂ ባላንስ የለዎትም! (ያሎት: {sender.get('balance', 0)} ETB)"})
    
    db_sender_phone = sender["phone"]

    receiver = wallets.find_one({
        "$or": [
            {"phone": receiver_ph}, 
            {"phone": raw_receiver_ph},
            {"phone": f"+{receiver_ph}"}
        ]
    })
    
    if not receiver:
        return jsonify({"success": False, "msg": "ተቀባዩ ተጠቃሚ በሲስተሙ ውስጥ አልተገኘም!"})
    
    db_receiver_phone = receiver["phone"]

    if db_sender_phone == db_receiver_phone:
        return jsonify({"success": False, "msg": "ወደ ራስዎ ቁጥር ማስተላለፍ አይችሉም!"})

    sender_updated = wallets.find_one_and_update(
        {"phone": db_sender_phone, "balance": {"$gte": amt}},
        {"$inc": {"balance": -amt}},
        return_document=True
    )
    if not sender_updated:
        return jsonify({"success": False, "msg": "ሂደቱ አልተሳካም፤ በቂ ባላንስ የለዎትም!"})

    receiver_updated = wallets.find_one_and_update(
        {"phone": db_receiver_phone},
        {"$inc": {"balance": amt}},
        return_document=True,
        upsert=True
    )

    notify_user_balance_update(db_sender_phone, sender_updated.get("balance", 0))
    if receiver_updated:
        notify_user_balance_update(db_receiver_phone, receiver_updated.get("balance", 0))

    msg = f"🔄 *Automatic Transfer Successful*\n📤 From: `{db_sender_phone}`\n📥 To: `{db_receiver_phone}`\n💵 Amount: `{amt}` ETB"
    send_telegram(msg)

    return jsonify({"success": True, "msg": f"ብር በအောင်မြင် ተላልፏል! {amt} ETB"})

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json or {}
    if "message" in data:
        msg = data["message"]
        text = msg.get("text", "")
        chat_id = str(msg.get("chat", {}).get("id", ""))
        
        if chat_id != str(ADMIN_ID):
            wallets.update_one({"chat_id": chat_id}, {"$set": {"chat_id": chat_id}}, upsert=False)
        
        if text.lower() == "/play":
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            keyboard = {
                "inline_keyboard": [
                    [{"text": "🎮 PLAY | 10 ብር", "web_app": {"url": WEB_APP_URL}}], 
                    [{"text": "🛠 Admin Panel", "web_app": {"url": f"{WEB_APP_URL}/admin_panel"}}] if chat_id == str(ADMIN_ID) else []
                ]
            }
            payload = {"chat_id": chat_id, "text": "🕹 *PLAY IN:*\nChoose an option:", "parse_mode": "Markdown", "reply_markup": keyboard}
            try:
                requests.post(url, json=payload, timeout=2)
            except Exception as e:
                print(f"Telegram Error: {e}")
            return "OK", 200

        if chat_id == str(ADMIN_ID):
            # አድሚን ቦት ትዕዛዞች (ቀደም ሲል የነበሩት /add, /sub, /block ወዘተ እዚህ ይቀጥላሉ)
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            if text.startswith("/block "):
                parts = text.split()
                if len(parts) >= 2:
                    target_phone = sanitize_input(parts[1])
                    blocked_phones.update_one({"phone": target_phone}, {"$set": {"phone": target_phone}}, upsert=True)
                    requests.post(url, json={"chat_id": ADMIN_ID, "text": f"✅ ቁጥር ({target_phone}) ታግዷል።"})
            elif text.startswith("/unblock "):
                parts = text.split()
                if len(parts) >= 2:
                    target_phone = sanitize_input(parts[1])
                    blocked_phones.delete_one({"phone": target_phone})
                    requests.post(url, json={"chat_id": ADMIN_ID, "text": f"✅ ቁጥር ({target_phone}) ከብሎክ ተነስቷል።"})
            elif text.startswith("/add "):
                parts = text.split()
                if len(parts) >= 3:
                    target_phone = sanitize_input(parts[1])
                    try:
                        add_amt = float(parts[2])
                        updated = wallets.find_one_and_update({"phone": target_phone}, {"$inc": {"balance": add_amt}}, return_document=True, upsert=True)
                        new_bal = updated.get("balance", 0) if updated else 0
                        notify_user_balance_update(target_phone, new_bal)
                        requests.post(url, json={"chat_id": ADMIN_ID, "text": f"✅ ባላንስ ተጨምሯል። አጠቃላይ: {new_bal} ETB"})
                    except ValueError:
                        pass
            elif text.startswith("/sub "):
                parts = text.split()
                if len(parts) >= 3:
                    target_phone = sanitize_input(parts[1])
                    try:
                        sub_amt = float(parts[2])
                        updated = wallets.find_one_and_update({"phone": target_phone}, {"$inc": {"balance": -sub_amt}}, return_document=True)
                        if updated:
                            new_bal = updated.get("balance", 0)
                            notify_user_balance_update(target_phone, new_bal)
                            requests.post(url, json={"chat_id": ADMIN_ID, "text": f"✅ ባላንስ ቀንሷል። አጠቃላይ: {new_bal} ETB"})
                    except ValueError:
                        pass
            elif text in ["/all", "/all_balances"]:
                all_users = list(wallets.find({}))
                msg_text = "📋 *የተጠቃሚዎች ዝርዝር:*\n\n"
                for u in all_users:
                    msg_text += f"📞 `{u.get('phone')}` | 💰 *{u.get('balance', 0)} ETB*\n"
                requests.post(url, json={"chat_id": ADMIN_ID, "text": msg_text, "parse_mode": "Markdown"})

    elif "callback_query" in data:
        cq = data["callback_query"]
        cq_id = cq["id"]
        chat_id = str(cq["message"]["chat"]["id"])
        data_str = cq.get("data", "")
        if chat_id == str(ADMIN_ID):
            answer_url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
            edit_url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
            if data_str.startswith("app_dep_"):
                _, _, phone_num, amt_str = data_str.split("_", 3)
                amt = float(amt_str)
                updated = wallets.find_one_and_update({"phone": phone_num}, {"$inc": {"balance": amt}}, return_document=True, upsert=True)
                new_bal = updated.get("balance", 0) if updated else 0
                notify_user_balance_update(phone_num, new_bal)
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": f"ዲፖዚት ጸድቋል!"})
                requests.post(edit_url, json={"chat_id": ADMIN_ID, "message_id": cq["message"]["message_id"], "text": cq["message"]["text"] + f"\n\n✅ APPROVED", "parse_mode": "Markdown", "reply_markup": {"inline_keyboard": []}})
            elif data_str.startswith("rej_dep_"):
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "ተሰርዟል።"})
                requests.post(edit_url, json={"chat_id": ADMIN_ID, "message_id": cq["message"]["message_id"], "text": cq["message"]["text"] + f"\n\n❌ REJECTED", "parse_mode": "Markdown", "reply_markup": {"inline_keyboard": []}})

    return "OK", 200

# የጨዋታው መሠረታዊ ሎጂኮች (Register, Game Loop, Claim Bingo ወዘተ...) 
# ቀደም ሲል በነበሩት ኮዶች መሠረት ይቀጥላሉ...

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

    wallets.update_one({"phone": clean_phone}, {"$set": update_data, "$setOnInsert": {"balance": 0}}, upsert=True)
    existing = wallets.find_one({"phone": clean_phone})
    return jsonify({"success": True, "balance": existing.get("balance", 0), "username": existing.get("username", fallback_name)})

# ጌም ሉፕ እና ሌሎች ሩቶች (እንዲሁም get_status, buy_specific_ticket, cancel_ticket, claim_bingo) ቀደም ባለው ኮድ እንዳሉ ይቆዩ።

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
