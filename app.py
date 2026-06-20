from flask import Flask, request, jsonify, render_template
import sqlite3
import google.generativeai as genai
import json
import requests
from datetime import datetime
from math import radians, sin, cos, sqrt, atan2
import sys
import urllib.parse
import time
import os

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent

sys.stdout.reconfigure(encoding='utf-8')

app = Flask(__name__)
app.json.ensure_ascii = False
DB_NAME = "favorite_places.db"

configuration = Configuration(access_token=os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))

# --- 輔助函式 ---
def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    print("🚀 正在檢查資料庫...")
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS stores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT,
            category TEXT,
            address TEXT,
            latitude REAL,
            longitude REAL,
            created_at TEXT,
            is_eaten INTEGER,
            is_favorite INTEGER
        )
    ''')
    conn.commit()
    conn.close()
    print("✅ 資料庫檢查完成！")

def get_coordinates(address):
    # 實際專案建議串接 Google Maps Geocoding API
    return 25.0864, 121.4646 

def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1-a))

def get_google_maps_url(name, address):
    query = urllib.parse.quote(f"{name} {address}")
    return f"https://www.google.com/maps/search/?api=1&query={query}"

# --- 路由 ---
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        return "Invalid signature", 400
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_message = event.message.text.strip().replace("：", ":")
    reply_text = ""

    if user_message == "新增餐廳":
        reply_text = "✨ 請輸入「新增：」加上餐廳名稱與地址\n範例：新增：一蘭拉麵 台北市信義區松壽路11號"
    elif user_message == "查詢餐廳":
        reply_text = "🔍 請輸入「查詢」加上關鍵字\n範例：查詢 拉麵"
    elif user_message.startswith("新增:"):
        content = user_message.replace("新增:", "").strip()
        try:
            # 請將下方的網址改為你在 Render 上的實際網址
            res = requests.post(f"https://my-food-map-bot.onrender.com/ai_add", json={"text": content}, timeout=60)
            reply_text = "✅ 成功！AI 已經幫你儲存餐廳資訊。" if res.status_code == 200 else "❌ 新增失敗。"
        except Exception as e:
            reply_text = f"❌ 連線失敗: {str(e)}"
    elif user_message.startswith("查詢"):
        keyword = user_message.replace("查詢", "").strip()
        conn = get_db_connection()
        rows = conn.execute("SELECT * FROM stores WHERE name LIKE ? OR category LIKE ?", (f"%{keyword}%", f"%{keyword}%")).fetchall()
        conn.close()
        if rows:
            reply_text = f"🍽️ 找到關於「{keyword}」的結果：\n" + "\n".join([f"\n店名：{r['name']}\n📍 {r['address']}\n🔗 {get_google_maps_url(r['name'], r['address'])}" for r in rows])
        else:
            reply_text = "😢 沒有找到相關餐廳喔！"
    else:
        reply_text = "你好！請點選下方圖文選單，或輸入「新增: 店家資訊」/「查詢 關鍵字」開始使用！"

    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply_text)]))

@app.route("/ai_add", methods=["POST"])
def ai_add():
    data = request.get_json()
    api_key = os.getenv('GEMINI_API_KEY')
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-1.5-flash')
    
    prompt = f"請從以下文字萃取店名、地址、標籤，回傳 JSON：{data.get('text')}"
    response = model.generate_content(prompt)
    result = json.loads(response.text.replace('```json', '').replace('```', ''))
    
    conn = get_db_connection()
    conn.execute("INSERT INTO stores (name, address, category, created_at, is_eaten, is_favorite) VALUES (?, ?, ?, ?, 0, 0)",
                 (result['name'], result['address'], result['category'], datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return jsonify({"message": "success"}), 200

# (其他原有的路由如 /advanced_search, /dashboard_data 請保留在下方)

if __name__ == "__main__":
    init_db()
    app.run(debug=True)
