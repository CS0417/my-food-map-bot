from flask import Flask, request, jsonify, render_template
import sqlite3
import google.generativeai as genai
import json
import requests
from datetime import datetime
from math import radians, sin, cos, sqrt, atan2
import sys
import urllib.parse
import os

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent

sys.stdout.reconfigure(encoding='utf-8')
app = Flask(__name__)
app.json.ensure_ascii = False
DB_NAME = "favorite_places.db"

# --- 設定與初始化 ---
configuration = Configuration(access_token=os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))

def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS stores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, category TEXT, address TEXT, 
            latitude REAL, longitude REAL, google_maps_url TEXT,
            created_at TEXT, is_eaten INTEGER DEFAULT 0, is_favorite INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

# --- 核心邏輯函式 ---
def get_google_maps_url(name, address):
    query = urllib.parse.quote(f"{name} {address}")
    return f"https://www.google.com/maps/search/?api=1&query={query}"

def get_coordinates(address):
    return 25.0864, 121.4646 

def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1-a))

def process_and_save_store(text):
    try:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            return False, "缺少 GEMINI_API_KEY"

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-1.5-flash")

        prompt = f"""
請只回傳合法 JSON，不要加任何 markdown 標記（如 ```json）或說明文字。
格式必須是：
{{
  "name": "店名",
  "address": "地址",
  "category": "類別"
}}

請從以下文字萃取：
{text}
"""
        response = model.generate_content(prompt)
        raw = response.text.strip()

        # 強力清理 Markdown 的 code block 標記
        if raw.startswith("```"):
            raw = raw.replace("```json", "").replace("```", "").strip()

        data = json.loads(raw)

        name = data.get("name")
        address = data.get("address")
        category = data.get("category", "未分類")

        if not name or not address:
            return False, "AI 沒有抓到店名或地址"

        lat, lon = get_coordinates(address)
        url = get_google_maps_url(name, address)

        conn = get_db_connection()
        conn.execute("""
            INSERT INTO stores
            (name, category, address, latitude, longitude, google_maps_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            name,
            category,
            address,
            lat,
            lon,
            url,
            datetime.now().isoformat()
        ))
        conn.commit()
        conn.close()

        return True, name

    except json.JSONDecodeError as e:
        return False, f"AI 回傳的格式不是有效的 JSON: {e}"
    except Exception as e:
        return False, f"AI 解析失敗：{str(e)}"

# --- 路由與 LINE 邏輯 ---
@app.route("/")
def index(): return render_template("index.html")

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    if not signature:
        return "Missing signature",400

    body = request.get_data(as_text=True)
    
    try:
        handler.handle(body, signature)
    except InvalidSignatureError: 
        return "Invalid signature", 400
        
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    msg = event.message.text.strip().replace("：", ":")
    if msg.startswith("新增:"):
        success, name = process_and_save_store(msg.replace("新增:", ""))
        reply = f"✅ 成功儲存 {name}" if success else "❌ 新增失敗"
    elif msg.startswith("查詢"):
        keyword = msg.replace("查詢", "").strip()
        conn = get_db_connection()
        rows = con.execute("SELECT * FROM stores WHERE name LIKE ?", (f"%{keyword}%",)).fetchall()
        conn.close()
        if rows:
            reply = "\n".join([f"店名：{r['name']}\n🔗 {r['google_maps_url']}" for r in rows]) 
        else:
            reply = "找不到喔！"
    else:
        reply = "請點選選單或使用指令：新增: [資訊] / 查詢 [關鍵字]"
    
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )

@app.route("/stores", methods=["GET"])
def get_stores():
    conn = get_db_connection()
    stores = [dict(row) for row in conn.execute("SELECT * FROM stores").fetchall()]
    conn.close()
    return jsonify(stores)

@app.route("/dashboard_data", methods=["POST"])
def dashboard_data():
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM stores").fetchall()
    conn.close()
    # 這裡放你原本的統計邏輯...
    return jsonify({"total": len(rows), "status": "ok"})

if __name__ == "__main__":
    init_db()
    app.run(debug=True)
