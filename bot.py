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
from bson.objectid import ObjectId
from flask_socketio import SocketIO

# ==========================================
# 0. ማዋቀሪያዎች እና አጠቃላይ ተለዋዋጮች
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "7963283307:AAEqj8R955f171GqW-3XvPqXlI4vP2b-10o")
ADMIN_ID = os.environ.get("ADMIN_ID", "7880946221")
WEB_APP_URL = os.environ.get("WEB_APP_URL", "https://bingo1-pjyb.onrender.com")

MONGO_URI = os.environ.get("MONGO_URI", "mongodb+srv://yabebal98:Yabebal98@cluster0.p7ovn.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")
client = MongoClient(MONGO_URI)
db = client['bingo_database']
wallets = db['wallets']
transactions = db['transactions']
blocked_users = db['blocked_users']

app = Flask(__name__)
app.config['SECRET_KEY'] = 'besh-bingo-ultra-secure-key-2026'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

user_states = {}
user_pages = {}
user_history_pages = {}

def sanitize_input(text):
    if not text:
        return ""
    return re.sub(r'[^a-zA-Z0-9_\-\.\+]', '', str(text).strip())

def format_phone_number(phone):
    if not phone:
        return None
    phone = str(phone).strip().replace(" ", "").replace("-", "")
    if phone.startswith('+'):
        phone = phone[1:]
    if phone.startswith('0'):
        phone = '251' + phone[1:]
    elif phone.startswith('9') and len(phone) == 9:
        phone = '251' + phone
    return phone

def is_valid_ethiopian_phone(phone):
    if not phone:
        return False
    return bool(re.match(r'^251[79]\d{8}$', phone))

def get_pending_button_text():
    count = transactions.count_documents({"status": "pending"})
    if count == 0:
        return "⏳ የሚጠብቁ ጥያቄዎች (0)"
    else:
        return f"🚨 የሚጠብቁ ጥያቄዎች ({count})"

# የተስተካከለ የአድሚን ኪቦርድ
def get_admin_dashboard_keyboard():
    pending_btn_txt = get_pending_button_text()
    return {
        "inline_keyboard": [
            [
                {"text": pending_btn_txt, "callback_data": "admin_pending_req"},
                {"text": "📅 የእለት/የሳምንት ገቢ (20%)", "callback_data": "admin_financial_stats"}
            ],
            [
                {"text": "🔗 የኤጀንት ሊንክ (Gen Link)", "callback_data": "admin_gen_agent_link"},
                {"text": "👥 የኤጀንት ስታቲስቲክስ", "callback_data": "admin_agent_stats_guide"}
            ],
            [
                {"text": "📋 የሁሉም ተጠቃሚዎች ባላንስ", "callback_data": "admin_show_all_bal"},
                {"text": "📜 የትራንዛክሽን ታሪክ (ፈልግ)", "callback_data": "admin_history_req"}
            ],
            [
                {"text": "➕ ባላንስ ጨምር (/add)", "callback_data": "guide_add"},
                {"text": "➖ ባላንስ ቀንስ (/sub)", "callback_data": "guide_sub"}
            ],
            [
                {"text": "📢 መልእክት አስላልፍ (/broadcast)", "callback_data": "guide_broadcast"},
                {"text": "🚫 ብሎክ / ማጥፊያ", "callback_data": "guide_block_remove"}
            ],
            [
                {"text": "🔄 ሰሌዳውን አድስ (Refresh)", "callback_data": "admin_refresh_dashboard"}
            ]
        ]
    }

# ለተጨዋቾች የጨዋታ ክፍሎች ብቻ ማሳያ (Game Rooms: 10, 20, 50, 100, 200, Super, Bonus)
def get_player_play_keyboard(phone, referred_by=None):
    u_10 = f"{WEB_APP_URL}/10?phone={phone}"
    u_20 = f"{WEB_APP_URL}/20?phone={phone}"
    u_50 = f"{WEB_APP_URL}/50?phone={phone}"
    u_100 = f"{WEB_APP_URL}/100?phone={phone}"
    u_200 = f"{WEB_APP_URL}/200?phone={phone}"
    u_super = f"{WEB_APP_URL}/superbesh"
    if referred_by:
        u_super += f"?ref={referred_by}"
    else:
        u_super += f"?phone={phone}"

    return {
        "inline_keyboard": [
            [
                {"text": "🎮 PLAY | በ10 ብር", "web_app": {"url": u_10}},
                {"text": "🎮 PLAY | በ20 ብር", "web_app": {"url": u_20}}
            ],
            [
                {"text": "🎮 PLAY | በ50 ብር", "web_app": {"url": u_50}},
                {"text": "🔥 PLAY | በ100 ብር", "web_app": {"url": u_100}}
            ],
            [
                {"text": "💎 PLAY | በ200 ብር", "web_app": {"url": u_200}},
                {"text": "🏆 BeshBingo (ሳምንታዊ) | 50 ብር", "web_app": {"url": u_super}}
            ],
            [
                {"text": "⚽ BeshBingo Bonus", "callback_data": "Besh_bingo_bonus"}
            ]
        ]
    }

# ==========================================
# 1. የጨዋታ ስቴቶች (Game State Configuration)
# ==========================================
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
    "100": {
        "status": "lobby", "timer": 30, "ball_timer": 2, "pot": 0, "players": {}, 
        "sold_tickets": {}, "current_ball": "--", "drawn_balls": [], "winner": None,
        "winning_card": None, "winning_ticket_num": None, "winning_indices": None,
        "winning_line_name": None, "all_cards": {}
    },
    "200": {
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

reset_task_references = {"10": None, "20": None, "50": None, "100": None, "200": None, "super": None}
pending_claims = {"10": [], "20": [], "50": [], "100": [], "200": [], "super": []}
claim_lock_active = {"10": False, "20": False, "50": False, "100": False, "200": False, "super": False}

ROOM_PRICES = {
    "10": 10,
    "20": 20,
    "50": 50,
    "100": 100,
    "200": 200,
    "super": 50
}

def send_telegram(text, reply_markup=None):
    def _send():
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": ADMIN_ID, "text": text, "parse_mode": "Markdown"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            requests.post(url, json=payload, timeout=3)
        except Exception as e:
            print(f"Telegram Error: {e}")
    gevent.spawn(_send)

def send_telegram_user(chat_id, text, reply_markup=None):
    def _send():
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            requests.post(url, json=payload, timeout=3)
        except Exception as e:
            print(f"Telegram User Error: {e}")
    gevent.spawn(_send)

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

def reset_game(room_type):
    game_states[room_type] = {
        "status": "lobby",
        "timer": 30,
        "ball_timer": 3 if room_type == "super" else 2,
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
    pending_claims[room_type] = []
    claim_lock_active[room_type] = False
    broadcast_game_state(room_type)

def refund_all_sold_tickets(room_type):
    state = game_states[room_type]
    price = ROOM_PRICES.get(room_type, 10)
    for phone_num, p_data in state["players"].items():
        count = len(p_data.get("cards", {}))
        if count > 0:
            refund_amount = count * price
            wallets.update_one({"phone": phone_num}, {"$inc": {"balance": refund_amount}})
            transactions.insert_one({
                "phone": phone_num,
                "type": "refund",
                "amount": refund_amount,
                "room": room_type,
                "timestamp": datetime.utcnow()
            })

# ==========================================
# 2. የዌብ-አፕ ሩቶች (Web App Routes)
# ==========================================
@app.route('/')
@app.route('/10')
@app.route('/ten')
def index_10(): 
    return render_template('index.html')

@app.route('/20')
@app.route('/twenty')
def index_20():
    return render_template('index2.html')

@app.route('/50')
@app.route('/fifty')
@app.route('/super')
def index_50():
    return render_template('index1.html')

@app.route('/100')
@app.route('/hundred')
def index_100():
    return render_template('index4.html')

@app.route('/200')
@app.route('/twohundred')
def index_200():
    return render_template('index5.html')

@app.route('/superbesh')
def index_superbesh():
    return render_template('index3.html')

# ==========================================
# 3. የሱፐር ጨዋታ ዋና ሎፕ (Super Game Loop)
# ==========================================
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

# ==========================================
# 4. መደበኛ የጨዋታ ሎፕ (Standard Rooms 10, 20, 50, 100, 200)
# ==========================================
def run_game_loop(room_type):
    balls = [f"{'BINGO'[i//15]}{i+1}" for i in range(75)]
    global reset_task_references
    while True:
        state = game_states[room_type]
        current_status = state["status"]

        if current_status == "lobby":
            last_broadcasted_timer = None
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

# ==========================================
# 5. Endpoints Logic (get_status, buy_ticket, claim, ወዘተ)
# ==========================================
@app.route('/get_status')
def get_status():
    room = request.args.get('room', '10')
    state = game_states.get(room, game_states["10"])
    
    phone = sanitize_input(request.args.get('phone'))
    formatted_phone = format_phone_number(phone) if phone else None
    user = wallets.find_one({"phone": formatted_phone}) if formatted_phone else None
    
    if not user and room == "super":
        return jsonify({
            "success": False,
            "user_exists": False,
            "msg": "መለያዎ ከሲስተሙ ተሰርዟል! እባክዎ እንደገና ይመዝገቡ።"
        })
    elif not user:
        return jsonify({
            "success": False,
            "user_exists": False,
            "msg": "መለያዎ ከሲስተሙ ተሰርዟል! እባክዎ እንደገና ይመዝገቡ።"
        })
        
    db_phone = user['phone'] if user else (formatted_phone or phone)
    p_data = state["players"].get(db_phone, {"cards": {}})
    cards_list = list(p_data["cards"].values())
    clean_players = {k: {"username": v.get("username", ""), "cards": list(v.get("cards", {}).values())} for k, v in state["players"].items()}
    
    return jsonify({
        "success": True,
        "user_exists": True,
        "balance": user.get('balance', 0),
        "status": state["status"],
        "timer": state["timer"],
        "ball_timer": state["ball_timer"],
        "pot": state["pot"],
        "current_ball": state["current_ball"],
        "drawn_balls": state["drawn_balls"],
        "winner": state["winner"],
        "winning_card": state["winning_card"],
        "winning_ticket_num": state["winning_ticket_num"],
        "winning_indices": state.get("winning_indices"),
        "winning_line_name": state.get("winning_line_name"),
        "sold_tickets": state["sold_tickets"],
        "my_cards": cards_list,
        "all_cards": state.get("all_cards", {}),
        "players": clean_players
    })

@app.route('/buy_ticket', methods=['POST'])
def buy_ticket():
    data = request.json or {}
    phone = sanitize_input(data.get('phone'))
    ticket_num = str(data.get('ticket_num'))
    card = data.get('card')
    room = data.get('room', '10')

    if not phone or not ticket_num or not card:
        return jsonify({"success": False, "msg": "ያልተሟላ መረጃ!"})

    formatted_phone = format_phone_number(phone)
    user = wallets.find_one({"phone": formatted_phone})
    if not user:
        return jsonify({"success": False, "msg": "መለያዎ አልተገኘም!"})

    state = game_states.get(room)
    if not state or state["status"] != "lobby":
        return jsonify({"success": False, "msg": "ጨዋታው ተጀምሯል ወይም ክፍሉ አልተገኘም!"})

    ticket_price = ROOM_PRICES.get(room, 10)
    current_balance = user.get('balance', 0)
    if current_balance < ticket_price:
        return jsonify({"success": False, "msg": "በቂ ቀሪ ሂሳብ የለዎትም!"})

    db_phone = user['phone']
    if ticket_num in state["sold_tickets"]:
        return jsonify({"success": False, "msg": "ይህ ካርድ አስቀድሞ ተሽጧል!"})

    res = wallets.update_one(
        {"phone": db_phone, "balance": {"$gte": ticket_price}},
        {"$inc": {"balance": -ticket_price}}
    )
    if res.modified_count == 0:
        return jsonify({"success": False, "msg": "ሂሳብዎ በቂ አይደለም!"})

    new_bal = current_balance - ticket_price
    state["sold_tickets"][ticket_num] = db_phone
    if db_phone not in state["players"]:
        state["players"][db_phone] = {
            "username": user.get("username", "Player"),
            "cards": {}
        }
    state["players"][db_phone]["cards"][ticket_num] = card
    state["all_cards"][ticket_num] = card

    if room == "super":
        state["pot"] += int(ticket_price * 0.90) # 90% for super pot
    else:
        state["pot"] += int(ticket_price * 0.80) # 80% for regular pot

    broadcast_game_state(room)
    return jsonify({"success": True, "balance": new_bal, "ticket_num": ticket_num})

@app.route('/claim', methods=['POST'])
def claim():
    data = request.json or {}
    phone = sanitize_input(data.get('phone'))
    ticket_num = str(data.get('ticket_num'))
    room = data.get('room', '10')
    line_name = data.get('line_name', 'ቢንጎ')
    indices = data.get('indices', [])

    state = game_states.get(room)
    if not state or state["status"] != "playing":
        return jsonify({"success": False, "msg": "ጨዋታው በሂደት ላይ አይደለም!"})

    formatted_phone = format_phone_number(phone)
    user = wallets.find_one({"phone": formatted_phone})
    if not user:
        return jsonify({"success": False, "msg": "መለያዎ አልተገኘም!"})

    db_phone = user['phone']
    player_card = state["players"].get(db_phone, {}).get("cards", {}).get(ticket_num)
    if not player_card:
        return jsonify({"success": False, "msg": "ይህ ካርድ የእርስዎ አይደለም!"})

    # አሸናፊውን ማረጋገጥ
    state["status"] = "result"
    state["winner"] = user.get("username", db_phone)
    state["winning_card"] = player_card
    state["winning_ticket_num"] = ticket_num
    state["winning_indices"] = indices
    state["winning_line_name"] = line_name

    win_amount = state["pot"]
    wallets.update_one({"phone": db_phone}, {"$inc": {"balance": win_amount}})
    transactions.insert_one({
        "phone": db_phone,
        "type": "game_win",
        "amount": win_amount,
        "room": room,
        "timestamp": datetime.utcnow()
    })

    # አሸናፊ ማሳወቂያ ለቴሌግራም
    congrats_text = (
        f"🏆 **ታላቅ አሸናፊ ተገኝቷል!**\n\n"
        f"👤 ተጠቃሚ፦ `{user.get('username', 'Player')}`\n"
        f"📱 ስልክ፦ `{db_phone}`\n"
        f"🎮 ክፍል፦ `{room} ብር ሩም`\n"
        f"🎫 የካርድ ቁጥር፦ `{ticket_num}`\n"
        f"💰 ያሸነፉት ገንዘብ፦ `{win_amount} ብር`"
    )
    send_telegram(congrats_text)

    def result_countdown_and_reset():
        for t in range(10, -1, -1):
            if state["status"] != "result":
                return
            state["timer"] = t
            broadcast_game_state(room)
            socketio.sleep(1)
        reset_game(room)

    reset_task_references[room] = socketio.start_background_task(result_countdown_and_reset)
    broadcast_game_state(room)
    return jsonify({"success": True, "win_amount": win_amount})

# ==========================================
# 6. የቴሌግራም ዌብሁክ እና ቦት ትዕዛዞች (Webhook Handler)
# ==========================================
@app.route('/webhook', methods=['POST'])
def webhook():
    update = request.json or {}
    
    # የ Inline Button ጥያቄዎችን ማስተናገድ (Callback Query)
    if "callback_query" in update:
        cq = update["callback_query"]
        chat_id = cq["message"]["chat"]["id"]
        data = cq.get("data", "")
        msg_id = cq["message"]["message_id"]

        if data == "admin_refresh_dashboard":
            send_telegram_user(chat_id, "🔄 የአድሚን ሰሌዳ ታድሷል!", get_admin_dashboard_keyboard())
        elif data == "admin_pending_req":
            count = transactions.count_documents({"status": "pending"})
            send_telegram_user(chat_id, f"📋 በአሁኑ ሰዓት {count} የሚጠብቁ ጥያቄዎች አሉ። በ /pending ማየት ይችላሉ።")
        elif data == "guide_add":
            send_telegram_user(chat_id, "💡 ለተጠቃሚ ባላንስ ለመጨመር፦\n`/add 251911223344 100`")
        elif data == "guide_sub":
            send_telegram_user(chat_id, "💡 ከተጠቃሚ ባላንስ ለመቀነስ፦\n`/sub 251911223344 50`")
        elif data == "guide_broadcast":
            send_telegram_user(chat_id, "💡 ለሁሉም ተጠቃሚዎች መልእክት ለማስተላለፍ፦\n`/broadcast የፈለጉት መልእክት እዚህ`")
        elif data == "guide_block_remove":
            send_telegram_user(chat_id, "💡 ተጠቃሚን ለማገድ ወይም ለማጥፋት፦\n`/block 251911223344` ወይም `/remove 251911223344`")
        elif data == "Besh_bingo_bonus":
            send_telegram_user(chat_id, "⚽ በቅርብ ቀን የሚለቀቅ አስደሳች ቦነስ ጨዋታ!")
        return jsonify({"status": "ok"})

    # የጽሑፍ ትዕዛዞችን ማስተናገድ (Message Handling)
    if "message" in update:
        msg = update["message"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "").strip()
        contact = msg.get("contact")

        # ስልክ ቁጥር ሲላክ (Contact Sharing)
        if contact:
            user_phone = format_phone_number(contact.get("phone_number"))
            wallets.update_one(
                {"phone": user_phone},
                {"$setOnInsert": {"balance": 0, "username": msg["chat"].get("first_name", "Player")}},
                upsert=True
            )
            send_telegram_user(
                chat_id,
                f"✅ እንኳን ደህና መጡ! ስልክዎ `{user_phone}` በተሳካ ሁኔታ ተመዝግቧል።\nየሚጫወቱበትን ክፍል ይምረጡ፦",
                get_player_play_keyboard(user_phone)
            )
            return jsonify({"status": "ok"})

        if text == "/start" or text == "/play":
            user = wallets.find_one({"chat_id": chat_id})
            phone = user["phone"] if user else None
            if not phone:
                phone_btn = {
                    "keyboard": [[{"text": "📱 ስልክ ቁጥርዎን ያጋሩ", "request_contact": True}]],
                    "resize_keyboard": True,
                    "one_time_keyboard": True
                }
                send_telegram_user(chat_id, "እንኳን ወደ Besh Bingo በደህና መጡ! ለመጫወት እባክዎ ስልክ ቁጥርዎን ያጋሩ፦", phone_btn)
            else:
                send_telegram_user(chat_id, "🎮 የሚፈልጉትን የጨዋታ ክፍል ይምረጡ፦", get_player_play_keyboard(phone))

        elif text == "/admin" and str(chat_id) == str(ADMIN_ID):
            send_telegram_user(chat_id, "🛠 **የአድሚን አስተዳደር ሰሌዳ (Admin Workspace)**", get_admin_dashboard_keyboard())

        elif text == "/balance":
            user = wallets.find_one({"chat_id": chat_id})
            bal = user.get("balance", 0) if user else 0
            send_telegram_user(chat_id, f"💰 የአሁኑ ቀሪ ሂሳብዎ፦ `{bal} ብር` ነው::")

    return jsonify({"status": "ok"})

# ==========================================
# 7. አፕሊኬሽኑን ማስጀመር (App Bootstrap)
# ==========================================
@app.before_first_request
def initialize_system():
    # ዌብሁክ እና የቦት ትዕዛዞችን ማስተካከል
    set_webhook()
    set_bot_commands()
    
    # የጨዋታ ሎፖችን በጀርባ ማስጀመር (Background Tasks)
    socketio.start_background_task(lambda: run_game_loop("10"))
    socketio.start_background_task(lambda: run_game_loop("20"))
    socketio.start_background_task(lambda: run_game_loop("50"))
    socketio.start_background_task(lambda: run_game_loop("100"))
    socketio.start_background_task(lambda: run_game_loop("200"))
    socketio.start_background_task(super_game_loop)
    
    broadcast_game_state("10")
    broadcast_game_state("20")
    broadcast_game_state("50")
    broadcast_game_state("100")
    broadcast_game_state("200")
    broadcast_game_state("super")

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
