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

# ለብሎክ የተደረጉ ስልክ ቁጥሮች የሚሆን ክምችት (Collection)
blocked_users = db['blocked_users']

try:
    wallets.create_index("phone", unique=True)
    blocked_users.create_index("phone", unique=True)
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

@app.route('/request_deposit', methods=['POST'])
def request_deposit():
    d = request.json or {}
    ph = sanitize_input(str(d.get('phone')))
    
    # ቁጥሩ ብሎክ መደረጉን እንፈትሻለን
    is_blocked = blocked_users.find_one({"phone": ph})
    if is_blocked:
        # ብሎክ ከሆነ ለአድሚን አናስተላልፍም፤ ነገር ግን ተጠቃሚው ሲሚት ሲል ትክክል ተልኳል የሚል ኖቲፊኬሽን እንዲያሳይ 
        # እና የሚፈልጉትን መልእክት እንመልሳለን።
        return jsonify({
            "success": True, 
            "msg": "የነጻው አልቋል በቴሌ ብር ወይም ሲቢኢ ብር ወደ 0945880474 ላክ"
        })

    try:
        amt = float(d.get('amount', 0))
    except ValueError:
        amt = 0
    t_id = sanitize_input(d.get('transaction_id', 'N/A'))
    user = wallets.find_one({"phone": ph})
    db_phone = user["phone"] if user else ph
    
    msg = f"💰 *Deposit Request*\n📞 Phone: `{db_phone}`\n💵 Amount: `{amt}` ETB\n🆔 ID: `{t_id}`"
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
    try:
        amt = float(d.get('amount', 0))
    except ValueError:
        return jsonify({"success": False, "msg": "ትክክለኛ የገንዘብ መጠን ያስገቡ!"})
    if amt < 20:
        return jsonify({"success": False, "msg": "ቢያንስ 20 ETB ነው!"})
    user = wallets.find_one({"phone": ph})
    if not user:
        return jsonify({"success": False, "msg": "ተጠቃሚው አልተገኘም!"})
    db_phone = user["phone"]
    
    if user.get("balance", 0) < amt:
        return jsonify({"success": False, "msg": "በቂ ባላንስ የለዎትም!"})

    msg = f"📤 *Withdrawal Request*\n📞 Phone: `{db_phone}`\n💵 Amount: `{amt}` ETB"
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

    msg = f"🔄 *Transfer Request*\n📤 From: `{db_sender_phone}`\n📥 To: `{db_receiver_phone}`\n💵 Amount: `{amt}` ETB"
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ አረጋግጥ (Approve)", "callback_data": f"app_trf_{db_sender_phone}_{db_receiver_phone}_{amt}"},
                {"text": "❌ሰርዝ (Reject)", "callback_data": f"rej_trf_{db_sender_phone}_{amt}"}
            ]
        ]
    }
    send_telegram(msg, reply_markup=keyboard)
    return jsonify({"success": True, "msg": "የገንዘብ ማስተላለፍ ጥያቄ ለአድሚን ተልኳል!"})

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
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            
            # --- አዲስ የተጨመሩ /block እና /unblock ትዕዛዞች ---
            if text.startswith("/block "):
                parts = text.split()
                if len(parts) >= 2:
                    target_phone = sanitize_input(parts[1])
                    try:
                        blocked_users.update_one({"phone": target_phone}, {"$set": {"phone": target_phone}}, upsert=True)
                        requests.post(url, json={"chat_id": ADMIN_ID, "text": f"🚫 ስልክ ቁጥር ({target_phone}) በተሳካ ሁኔታ ተዘግቷል (Blocked)።"})
                    except Exception as e:
                        requests.post(url, json={"chat_id": ADMIN_ID, "text": f"❌ ስህተት አጋጥሟል: {e}"})
            
            elif text.startswith("/unblock "):
                parts = text.split()
                if len(parts) >= 2:
                    target_phone = sanitize_input(parts[1])
                    result = blocked_users.delete_one({"phone": target_phone})
                    if result.deleted_count > 0:
                        requests.post(url, json={"chat_id": ADMIN_ID, "text": f"✅ ስልክ ቁጥር ({target_phone}) ክልከላው ተነስቷል (Unblocked)።"})
                    else:
                        requests.post(url, json={"chat_id": ADMIN_ID, "text": f"⚠️ ስልክ ቁጥር ({target_phone}) በብሎክ ዝርዝር ውስጥ አልተገኘም!"})
            # ------------------------------------------------
            
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
        if chat_id == str(ADMIN_ID):
            answer_url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
            edit_url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
            
            if data_str.startswith("app_dep_"):
                _, _, phone_num, amt_str = data_str.split("_", 3)
                amt = float(amt_str)
                updated = wallets.find_one_and_update({"phone": phone_num}, {"$inc": {"balance": amt}}, return_document=True, upsert=True)
                new_bal = updated.get("balance", 0) if updated else 0
                notify_user_balance_update(phone_num, new_bal)
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": f"ተሳክቷል! {amt} ETB ገብቷል።"})
                requests.post(edit_url, json={"chat_id": ADMIN_ID, "message_id": cq["message"]["message_id"], "text": cq["message"]["text"] + f"\n\n✅ APPROVED\n💰 አጠቃላይ ባላንስ: {new_bal} ETB", "parse_mode": "Markdown", "reply_markup": {"inline_keyboard": []}})
            
            elif data_str.startswith("rej_dep_"):
                _, _, phone_num = data_str.split("_", 2)
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "ዲፖዚት ጥያቄው ሪጀክት ተደርጓል።"})
                requests.post(edit_url, json={"chat_id": ADMIN_ID, "message_id": cq["message"]["message_id"], "text": cq["message"]["text"] + f"\n\n❌ REJECTED", "parse_mode": "Markdown", "reply_markup": {"inline_keyboard": []}})

            elif data_str.startswith("app_wit_"):
                _, _, phone_num, amt_str = data_str.split("_", 3)
                amt = float(amt_str)
                updated = wallets.find_one_and_update({"phone": phone_num, "balance": {"$gte": amt}}, {"$inc": {"balance": -amt}}, return_document=True)
                new_bal = updated.get("balance", 0) if updated else 0
                if updated:
                    notify_user_balance_update(phone_num, new_bal)
                    requests.post(answer_url, json={"callback_query_id": cq_id, "text": f"ዊዝድሮዋል ጸድቋል!"})
                    requests.post(edit_url, json={"chat_id": ADMIN_ID, "message_id": cq["message"]["message_id"], "text": cq["message"]["text"] + f"\n\n✅ APPROVED\n💰 አጠቃላይ ባላንስ: {new_bal} ETB", "parse_mode": "Markdown", "reply_markup": {"inline_keyboard": []}})
            elif data_str.startswith("rej_wit_"):
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
                    requests.post(edit_url, json={"chat_id": ADMIN_ID, "message_id": cq["message"]["message_id"], "text": cq["message"]["text"] + f"\n\n✅ APPROVED", "parse_mode": "Markdown", "reply_markup": {"inline_keyboard": []}})
            elif data_str.startswith("rej_trf_"):
                requests.post(answer_url, json={"callback_query_id": cq_id, "text": "የገንዘብ ማስተላለፍ ጥያቄ ሪጀክት ተደርጓል።"})
                requests.post(edit_url, json={"chat_id": ADMIN_ID, "message_id": cq["message"]["message_id"], "text": cq["message"]["text"] + f"\n\n❌ REJECTED", "parse_mode": "Markdown", "reply_markup": {"inline_keyboard": []}})

    return jsonify({"success": True})

def game_loop():
    global game_state, reset_task_reference, claim_lock_active, pending_claims
    while True:
        game_state["status"] = "lobby"
        game_state["timer"] = 30
        game_state["pot"] = 0
        game_state["players"] = {}
        game_state["sold_tickets"] = {}
        game_state["current_ball"] = "--"
        game_state["drawn_balls"] = []
        game_state["winner"] = None
        game_state["winning_card"] = None
        game_state["winning_ticket_num"] = None
        game_state["winning_indices"] = None
        game_state["winning_line_name"] = None
        game_state["all_cards"] = {}
        claim_lock_active = False
        pending_claims = []

        broadcast_game_state()

        for t in range(30, -1, -1):
            if game_state["status"] != "lobby":
                break
            game_state["timer"] = t
            broadcast_game_state()
            socketio.sleep(1)

        game_state["status"] = "ball_prep"
        game_state["ball_timer"] = 3
        broadcast_game_state()
        for t in range(3, 0, -1):
            game_state["ball_timer"] = t
            broadcast_game_state()
            socketio.sleep(1)

        game_state["status"] = "playing"
        all_nums = list(range(1, 76))
        random.shuffle(all_nums)

        for ball in all_nums:
            if game_state["status"] != "playing":
                break
            game_state["current_ball"] = str(ball)
            game_state["drawn_balls"].append(str(ball))
            broadcast_game_state()
            socketio.sleep(3.5)

@app.route('/claim_bingo', methods=['POST'])
def claim_bingo():
    global claim_lock_active, pending_claims
    d = request.json or {}
    ph = sanitize_input(str(d.get('phone')))
    ticket_num = d.get('ticket_num')
    marked_indices = d.get('marked_indices', [])
    
    user = wallets.find_one({"phone": ph})
    db_phone = user["phone"] if user else ph
    
    if game_state["status"] != "playing" and game_state["status"] != "result":
        return jsonify({"success": False, "msg": "አሁን ቢንጎ ማለት አይቻልም!"})

    claim_info = {
        "phone": db_phone,
        "ticket_num": ticket_num,
        "marked_indices": marked_indices
    }

    if game_state["status"] == "playing":
        if not claim_lock_active:
            claim_lock_active = True
            
            def process_claims_by_ball():
                global claim_lock_active, pending_claims
                socketio.sleep(1.5)
                
                all_claims = [claim_info] + pending_claims
                pending_claims = []
                
                valid_winner = None
                drawn_set = set(game_state["drawn_balls"])
                
                for c in all_claims:
                    c_ph = c["phone"]
                    c_t_num = c["ticket_num"]
                    c_indices = c["marked_indices"]
                    
                    user_cards = game_state["all_cards"].get(c_ph, {})
                    card_data = user_cards.get(str(c_t_num))
                    
                    if not card_data:
                        continue
                        
                    flat_card = []
                    for col in ['B', 'I', 'N', 'G', 'O']:
                        flat_card.extend(card_data.get(col, []))
                        
                    is_valid = True
                    for idx in c_indices:
                        if idx < 0 or idx >= len(flat_card):
                            is_valid = False
                            break
                        val = str(flat_card[idx])
                        if val != "FREE" and val not in drawn_set:
                            is_valid = False
                            break
                            
                    if is_valid:
                        valid_winner = c
                        break
                
                if valid_winner:
                    w_ph = valid_winner["phone"]
                    w_t_num = valid_winner["ticket_num"]
                    w_indices = valid_winner["marked_indices"]
                    
                    winner_user = wallets.find_one({"phone": w_ph})
                    winner_name = winner_user.get("name", "ተጫዋች") if winner_user else "ተጫዋች"
                    
                    pot_amt = game_state["pot"]
                    
                    updated = wallets.find_one_and_update(
                        {"phone": w_ph},
                        {"$inc": {"balance": pot_amt}},
                        return_document=True,
                        upsert=True
                    )
                    new_bal = updated.get("balance", 0) if updated else 0
                    notify_user_balance_update(w_ph, new_bal)
                    
                    game_state["status"] = "result"
                    game_state["winner"] = winner_name
                    game_state["winning_ticket_num"] = w_t_num
                    game_state["winning_indices"] = w_indices
                    game_state["winning_card"] = game_state["all_cards"].get(w_ph, {}).get(str(w_t_num))
                    
                    success_msg = f"🏆 *BINGO WINNER!*\n👤 Winner: `{winner_name}`\n📞 Phone: `{w_ph}`\n🎟️ Ticket: `{w_t_num}`\n💰 Prize: `{pot_amt}` ETB"
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
        socketio.start_background_task(game_loop)
    broadcast_game_state()

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
