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

def send_telegram(text, reply_markup=None, chat_id=None):
    target_chat = chat_id if chat_id else ADMIN_ID
    def _send():
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": target_chat, "text": text, "parse_mode": "Markdown"}
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
        {"command": "balance", "description": "💰 ቀሪ (Balance)"},
        {"command": "deposit", "description": "📥 ሂሳብ ለመሙላት (Deposit)"},
        {"command": "withdrawal", "description": "📤 ገንዘብ ለማውጣት (Withdraw)"},
        {"command": "transfer", "description": "💸 ገንዘብ ለማስተላለፍ"},
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
            {"command": "admin", "description": "🛠 Admin"},
            {"command": "pending", "description": "⏳ Approvals"},
            {"command": "agent", "description": "👥 Agent h"},
            {"command": "daily", "description": "📅 D/W ገቢና ወጪ"},
            {"command": "balance", "description": "💰 Balance"},
            {"command": "deposit", "description": "📥 Deposit"},
            {"command": "withdrawal", "description": "📤 Withdraw"},
            {"command": "transfer", "description": "💸 Transfer"},
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

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json or {}
    
    if "message" in data:
        msg = data["message"]
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "")
        text_lower = text.lower().strip()
        
        user_wallet = wallets.find_one({"chat_id": chat_id})
        user_phone = user_wallet.get("phone") if user_wallet else None

        user_state = admin_state.find_one({"chat_id": chat_id})
        if user_state:
            action = user_state.get("action")
            
            if action in ["awaiting_dep_info_cbe", "awaiting_dep_info_tele"]:
                method = "CBE BIRR" if action == "awaiting_dep_info_cbe" else "TELE BIRR"
                sms_text = text.strip()
                admin_state.delete_one({"chat_id": chat_id})
                
                tx_res = transactions.insert_one({
                    "phone": user_phone or chat_id,
                    "type": "deposit",
                    "status": "pending",
                    "method": method,
                    "sms_content": sms_text,
                    "timestamp": datetime.utcnow()
                })
                tx_ref = str(tx_res.inserted_id)

                send_telegram("✅ የዲፖዚት መረጃዎ ለአድሚን ተልኳል! አድሚኑ አረጋግጦ ባላንስ ይሞላልዎታል።", chat_id=chat_id)

                admin_msg = f"💰 *Deposit Request ({method})*\n📞 Phone: `{user_phone}`\n📩 *የደረሰው SMS/መረጃ፦*\n`{sms_text}`"
                kb = {
                    "inline_keyboard": [
                        [
                            {"text": "✅ approved", "callback_data": f"app_dep_{tx_ref}_{user_phone}"},
                            {"text": "❌ ሰርዝ (Reject)", "callback_data": f"rej_dep_{tx_ref}_{user_phone}"}
                        ]
                    ]
                }
                send_telegram(admin_msg, reply_markup=kb, chat_id=ADMIN_ID)
                return "OK", 200

            elif action in ["awaiting_with_info_cbe", "awaiting_with_info_tele"]:
                method = "CBE BIRR" if action == "awaiting_with_info_cbe" else "TELE BIRR"
                try:
                    amt = float(text.strip())
                    admin_state.delete_one({"chat_id": chat_id})
                    
                    if amt < 20:
                        send_telegram("❌ ቢያንስ 20 ETB ማውጣት ይችላሉ። እባክዎ እንደገና በ `/withdrawal` ይሞክሩ።", chat_id=chat_id)
                        return "OK", 200

                    if user_wallet and user_wallet.get("balance", 0) < amt:
                        send_telegram(f"❌ በቂ ባላንስ የለዎትም። ያሎት ባላንስ፦ `{user_wallet.get('balance', 0)} ETB`", chat_id=chat_id)
                        return "OK", 200

                    tx_res = transactions.insert_one({
                        "phone": user_phone or chat_id,
                        "type": "withdrawal",
                        "amount": amt,
                        "status": "pending",
                        "method": method,
                        "timestamp": datetime.utcnow()
                    })
                    tx_ref = str(tx_res.inserted_id)

                    send_telegram(f"✅ የውዝድሮዋል ጥያቄዎ ለአድሚን ተልኳል!\n💳 ዘዴ: `{method}`\n💵 መጠን፦ `{amt}` ETB", chat_id=chat_id)

                    admin_msg = f"📤 *Withdrawal Request*\n💳 Method: `{method}`\n📞 Phone: `{user_phone}`\n💵 Amount: `{amt}` ETB"
                    kb = {
                        "inline_keyboard": [
                            [
                                {"text": "✅ approved", "callback_data": f"app_wit_{tx_ref}_{user_phone}_{amt}"},
                                {"text": "❌ ሰርዝ (Reject)", "callback_data": f"rej_wit_{tx_ref}_{user_phone}_{amt}"}
                            ]
                        ]
                    }
                    send_telegram(admin_msg, reply_markup=kb, chat_id=ADMIN_ID)
                    return "OK", 200
                except ValueError:
                    send_telegram("❌ እባክዎ ትክክለኛ የብር መጠን ብቻ ያስገቡ (ምሳሌ፦ `50`)።", chat_id=chat_id)
                    return "OK", 200

            elif action == "awaiting_transfer_info":
                parts = text.split()
                if len(parts) >= 2:
                    receiver_ph = parts[0].strip()
                    try:
                        amt = float(parts[1])
                        admin_state.delete_one({"chat_id": chat_id})

                        if not user_wallet or user_wallet.get("balance", 0) < amt:
                            send_telegram("❌ በቂ ባላንስ የለዎትም!", chat_id=chat_id)
                            return "OK", 200

                        receiver = wallets.find_one({"phone": receiver_ph})
                        if not receiver:
                            send_telegram("❌ ተቀባዩ በሲስተሙ ውስጥ አልተገኘም!", chat_id=chat_id)
                            return "OK", 200

                        wallets.update_one({"phone": user_phone}, {"$inc": {"balance": -amt}})
                        wallets.update_one({"phone": receiver_ph}, {"$inc": {"balance": amt}})

                        send_telegram(f"✅ ለ `{receiver_ph}` ተጫዋች `{amt}` ETB በትክክል አስተላልፈዋል።", chat_id=chat_id)
                        
                        if receiver.get("chat_id"):
                            send_telegram(f"📥 ከ `{user_phone}` ተጫዋች `{amt}` ETB ገብቶልዎታል!", chat_id=receiver.get("chat_id"))

                        send_telegram(f"🔄 *Transfer Alert*\n📤 From: `{user_phone}`\n📥 To: `{receiver_ph}`\n💵 Amount: `{amt}` ETB", chat_id=ADMIN_ID)
                        return "OK", 200
                    except ValueError:
                        send_telegram("❌ እባክዎ ስልክ ቁጥር እና መጠን በትክክል ያስገቡ። ምሳሌ፦ `0912345678 50`", chat_id=chat_id)
                        return "OK", 200

        # --- TELEGRAM COMMAND HANDLERS ---
        if text_lower == "/play":
            play_keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "🎮 10 ETB Room", "web_app": {"url": WEB_APP_URL}},
                        {"text": "🎮 20 ETB Room", "web_app": {"url": f"{WEB_APP_URL}/twenty"}}
                    ],
                    [
                        {"text": "🎮 50 ETB Room", "web_app": {"url": f"{WEB_APP_URL}/super"}},
                        {"text": "⭐ Super Besh", "web_app": {"url": f"{WEB_APP_URL}/superbesh"}}
                    ]
                ]
            }
            send_telegram("🎮 *የቢንጎ ጨዋታ ክፍሎች*\n\nእባክዎ መጫወት የሚፈልጉትን ክፍል ይምረጡ፦", reply_markup=play_keyboard, chat_id=chat_id)
            return "OK", 200

        elif text_lower == "/deposit":
            dep_keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "CBE BIRR", "callback_data": "user_dep_cbe"},
                        {"text": "TELE BIRR", "callback_data": "user_dep_tele"}
                    ],
                    [
                        {"text": "CANCEL", "callback_data": "user_dep_cancel"}
                    ]
                ]
            }
            send_telegram("💳 *የማስቀመጫ መንገድ ይምረጡ*\n\nእባክዎ ሒሳብ ለመሙላት የሚጠቀሙበትን መንገድ ይምረጡ፦", reply_markup=dep_keyboard, chat_id=chat_id)
            return "OK", 200

        elif text_lower in ["/withdrawal", "/withdraw"]:
            with_keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "CBE BIRR", "callback_data": "user_wit_cbe"},
                        {"text": "TELE BIRR", "callback_data": "user_wit_tele"}
                    ],
                    [
                        {"text": "CANCEL", "callback_data": "user_dep_cancel"}
                    ]
                ]
            }
            send_telegram("📤 *የማውጫ መንገድ ይምረጡ*\n\nእባክዎ ገንዘብ ማውጣት የሚፈልጉበትን መንገድ ይምረጡ፦", reply_markup=with_keyboard, chat_id=chat_id)
            return "OK", 200

        elif text_lower == "/transfer":
            admin_state.update_one({"chat_id": chat_id}, {"$set": {"action": "awaiting_transfer_info"}}, upsert=True)
            send_telegram("💸 *TRANSFER MONEY*\n\nየተቀባዩን ስልክ ቁጥር እና የሚልኩትን የብር መጠን በክፍተት አስገብተው ይላኩ፦\n\n*ምሳሌ፦* `0912345678 50`", chat_id=chat_id)
            return "OK", 200

    elif "callback_query" in data:
        cq = data["callback_query"]
        cq_id = cq["id"]
        chat_id = str(cq["message"]["chat"]["id"])
        data_str = cq.get("data", "")
        answer_url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"

        if data_str == "user_dep_cbe":
            admin_state.update_one({"chat_id": chat_id}, {"$set": {"action": "awaiting_dep_info_cbe"}}, upsert=True)
            requests.post(answer_url, json={"callback_query_id": cq_id, "text": "CBE BIRR ተመርጧል"})
            send_telegram("💳 *የ CBE-Birr አካውንት*\n\n📱 *CBE-BIRR፦* `0960736061`\n👤 *(ZEHARA MOHAMMED)*\n\n📌 *መመሪያ፦*\nብሩን ከላኩ በኋላ የደረሰዎትን አጭር የጽሁፍ መልዕክት (SMS) ኮፒ በማድረግ እዚህ ጋር ይላኩ።", chat_id=chat_id)
            return "OK", 200

        elif data_str == "user_dep_tele":
            admin_state.update_one({"chat_id": chat_id}, {"$set": {"action": "awaiting_dep_info_tele"}}, upsert=True)
            requests.post(answer_url, json={"callback_query_id": cq_id, "text": "TELE BIRR ተመርጧል"})
            send_telegram("📱 *ለቴሌ ብር አካውንት*\n\n📱 *TELE-BIRR፦* `0960736061`\n👤 *(ZEHARA MOHAMMED)*\n\n📌 *መመሪያ፦*\nብሩን ከላኩ በኋላ የደረሰዎትን አጭር የጽሁፍ መልዕክት (SMS) ኮፒ በማድረግ እዚህ ጋር ይላኩ።", chat_id=chat_id)
            return "OK", 200

        elif data_str == "user_wit_cbe":
            admin_state.update_one({"chat_id": chat_id}, {"$set": {"action": "awaiting_with_info_cbe"}}, upsert=True)
            requests.post(answer_url, json={"callback_query_id": cq_id, "text": "CBE BIRR ተመርጧል"})
            send_telegram("📤 *WITHDRAW (CBE BIRR)*\n\nእባክዎ ማውጣት የሚፈልጉትን የብር መጠን ብቻ በሜሴጅ ይላኩ (ቢያንስ 20 ETB)፦\n\n*ምሳሌ፦* `50`", chat_id=chat_id)
            return "OK", 200

        elif data_str == "user_wit_tele":
            admin_state.update_one({"chat_id": chat_id}, {"$set": {"action": "awaiting_with_info_tele"}}, upsert=True)
            requests.post(answer_url, json={"callback_query_id": cq_id, "text": "TELE BIRR ተመርጧል"})
            send_telegram("📤 *WITHDRAW (TELE BIRR)*\n\nእባክዎ ማውጣት የሚፈልጉትን የብር መጠን ብቻ በሜሴጅ ይላኩ (ቢያንስ 20 ETB)፦\n\n*ምሳሌ፦* `50`", chat_id=chat_id)
            return "OK", 200

        elif data_str == "user_dep_cancel":
            admin_state.delete_one({"chat_id": chat_id})
            requests.post(answer_url, json={"callback_query_id": cq_id, "text": "የተሰረዘ"})
            try:
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage", json={
                    "chat_id": chat_id,
                    "message_id": cq["message"]["message_id"]
                }, timeout=2)
            except Exception:
                pass
            return "OK", 200

    return "OK", 200

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
