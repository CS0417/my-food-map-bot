from flask import Flask, request, jsonify, render_template
import sqlite3
import google.generativeai as genai
import json
from datetime import datetime
from math import radians, sin, cos, sqrt, atan2
import sys
import urllib.parse
import os

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, 
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

sys.stdout.reconfigure(encoding="utf-8")

app = Flask(__name__)
app.json.ensure_ascii = False
DB_NAME = "favorite_places.db"

# =========================================================
# 環境變數與 LINE 設定
# =========================================================
line_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
line_secret = os.getenv("LINE_CHANNEL_SECRET")
gemini_key = os.getenv("GEMINI_API_KEY")

if not all([line_token, line_secret]):
    raise ValueError("LINE 憑證設定不完整")

configuration = Configuration(access_token=line_token)
handler = WebhookHandler(line_secret)

# =========================================================
# 核心功能與資料庫
# =========================================================
def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute("""
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
    """)
    conn.commit()
    conn.close()

def get_google_maps_url(name, address):
    query = urllib.parse.quote(f"{name} {address}")
    return f"https://www.google.com/maps/search/?api=1&query={query}"

def extract_json_from_text(text):
    """安全解析 Gemini 回傳的 JSON"""
    clean_text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(clean_text)

def process_and_save_store(text):
    """核心邏輯：AI 解析 + 資料庫儲存"""
    try:
        genai.configure(api_key=gemini_key)
        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = f"萃取店名、地址、類別為 JSON：{text}"
        response = model.generate_content(prompt)
        data = extract_json_from_text(response.text)
        
        name, address = data.get("name"), data.get("address")
        if not name or not address: return False, "缺少店名或地址"
        
        conn = get_db_connection()
        conn.execute("INSERT INTO stores (name, category, address, google_maps_url, created_at) VALUES (?, ?, ?, ?, ?)",
                     (name, data.get("category", "未分類"), address, get_google_maps_url(name, address), datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return True, name
    except Exception as e:
        return False, str(e)

# =========================================================
# 路由 (網頁 + LINE)
# =========================================================
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/stores", methods=["GET"])
def get_stores():
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM stores ORDER BY id DESC").fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    try:
        handler.handle(request.get_data(as_text=True), signature)
    except InvalidSignatureError:
        return "Invalid signature", 400
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    msg = event.message.text.strip().replace("：", ":")
    try:
        if msg.startswith("新增:"):
            success, result = process_and_save_store(msg.replace("新增:", ""))
            reply = f"✅ 成功儲存 {result}" if success else f"❌ {result}"
        elif msg.startswith("查詢"):
            keyword = msg.replace("查詢", "").strip()
            conn = get_db_connection()
            rows = conn.execute("SELECT * FROM stores WHERE name LIKE ? OR category LIKE ?", (f"%{keyword}%", f"%{keyword}%")).fetchall()
            conn.close()
            reply = "\n\n".join([f"店名：{r['name']}\n📍 {r['address']}\n🔗 {r['google_maps_url']}" for r in rows]) if rows else "找不到餐廳喔！"
        else:
            reply = "指令說明：\n新增: [店家資訊]\n查詢 [關鍵字]"
    except Exception as e:
        reply = f"系統錯誤：{e}"
    
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(
            reply_token=event.reply_token, messages=[TextMessage(text=reply)]))

if __name__ == "__main__":
    init_db()
    app.run(debug=True)
