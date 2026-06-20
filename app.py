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

sys.stdout.reconfigure(encoding="utf-8")
app = Flask(__name__)
app.json.ensure_ascii = False
DB_NAME = "favorite_places.db"

# 設定環境變數
configuration = Configuration(access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.getenv("LINE_CHANNEL_SECRET"))
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# --- 資料庫初始化 ---
def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS stores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT,
            address TEXT,
            latitude REAL,
            longitude REAL,
            google_maps_url TEXT,
            created_at TEXT,
            is_eaten INTEGER DEFAULT 0,
            is_favorite INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

# --- 工具與外部 API ---
def get_google_maps_url(name, address):
    query = urllib.parse.quote(f"{name} {address}")
    return f"https://www.google.com/maps/search/?api=1&query={query}"

def get_coordinates(address):
    try:
        url = "https://nominatim.openstreetmap.org/search"
        params = {"q": address, "format": "json", "limit": 1}
        headers = {"User-Agent": "FoodGuideBot/1.0"}
        res = requests.get(url, params=params, headers=headers, timeout=10)
        data = res.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except:
        pass
    return None, None

# --- 核心邏輯 ---
def process_and_save_store(text):
    model = genai.GenerativeModel("gemini-1.5-flash")
    prompt = f"""你是一個餐廳資訊擷取器，請只輸出JSON。格式：{{"name":"", "address":"", "category":""}} 內容：{text}"""
    
    response = model.generate_content(prompt, generation_config={"response_mime_type": "application/json"})
    data = json.loads(response.text)
    
    name, address = data.get("name"), data.get("address")
    if not name or not address: return False, "AI 解析失敗"
    
    lat, lon = get_coordinates(address)
    url = get_google_maps_url(name, address)
    
    conn = get_db_connection()
    conn.execute("INSERT INTO stores (name, category, address, latitude, longitude, google_maps_url, created_at) VALUES (?,?,?,?,?,?,?)",
                 (name, data.get("category", "未分類"), address, lat, lon, url, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return True, name

# --- API 路由 ---
@app.route("/")
def index(): return render_template("index.html")

@app.route("/ai_add", methods=["POST"])
def ai_add():
    success, res = process_and_save_store(request.json["text"])
    return jsonify({"message": f"成功新增 {res}"}) if success else jsonify({"error": res}), 400 if not success else 200

@app.route("/update_status/<int:store_id>", methods=["POST"])
def update_status(store_id):
    data = request.json
    conn = get_db_connection()
    conn.execute(f"UPDATE stores SET {data['field']} = ? WHERE id = ?", (data['value'], store_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/dashboard_data", methods=["POST"])
def dashboard_data():
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM stores").fetchall()
    total = len(rows)
    eaten = sum(1 for r in rows if r["is_eaten"] == 1)
    cats = {r["category"]: sum(1 for row in rows if row["category"] == r["category"]) for r in rows if r["category"]}
    conn.close()
    return jsonify({"total": total, "eaten": eaten, "distance": 0, "categories": cats, "level": "美食探險家" if total > 10 else "美食初心者"})

@app.route("/advanced_search", methods=["POST"])
def advanced_search():
    data = request.json
    k = f"%{data.get('keyword', '')}%"
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM stores WHERE name LIKE ? OR category LIKE ? OR address LIKE ?", (k, k, k)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# --- LINE Bot ---
@app.route("/callback", methods=["POST"])
def callback():
    try:
        handler.handle(request.get_data(as_text=True), request.headers.get("X-Line-Signature", ""))
    except InvalidSignatureError: return "Error", 400
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    msg = event.message.text.strip().replace("：", ":")
    if msg.startswith("新增:"):
        success, res = process_and_save_store(msg.replace("新增:", ""))
        reply = f"✅ 成功儲存 {res}" if success else f"❌ {res}"
    elif msg.startswith("查詢"):
        keyword = msg.replace("查詢", "").strip()
        conn = get_db_connection()
        rows = conn.execute("SELECT * FROM stores WHERE name LIKE ? OR category LIKE ?", (f"%{keyword}%", f"%{keyword}%")).fetchall()
        conn.close()
        reply = "\n\n".join([f"店名：{r['name']}\n📍 {r['address']}\n🔗 {r['google_maps_url']}" for r in rows]) if rows else "找不到餐廳喔！"
    else:
        reply = "請使用選單或輸入「新增: 店家資訊」/「查詢 關鍵字」"
    
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(
            reply_token=event.reply_token, messages=[TextMessage(text=reply)]))

if __name__ == "__main__":
    init_db()
    app.run(debug=True)
