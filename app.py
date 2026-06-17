from flask import Flask, request, jsonify, render_template
import sqlite3
from google import genai
import json
import requests
from datetime import datetime
from math import radians, sin, cos, sqrt, atan2
import sys
import re
import urllib.parse
import time
import os

# 👇 就是這裡！你漏掉的 LINE 官方套件匯入指令，我幫你補上了！
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# 強制 Python 的輸出入管線都使用 UTF-8 萬國碼
sys.stdout.reconfigure(encoding='utf-8')

app = Flask(__name__)
app.json.ensure_ascii = False
DB_NAME = "favorite_places.db"

# LINE Bot 憑證設定 (安全地從 Render 保險箱讀取)
configuration = Configuration(access_token=os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))
@app.route("/")
def index():
    return render_template("index.html")
# ----------------------
# 🤖 專門接收 LINE 訊息的路由
# ----------------------
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        return "Invalid signature", 400
    return 'OK'

# 當收到文字訊息時的處理邏輯
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_message = event.message.text
    
    # 目前先做簡單的回聲測試，確認串接成功
    reply_text = f"你說了：「{user_message}」！\n之後我們可以把這裡接上 Gemini AI 魔法！"
    
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )

# 1. AI 魔法新增
@app.route("/ai_add", methods=["POST"])
def ai_add():
    data = request.get_json()
    raw_text = data.get("text")
    if not raw_text:
        return jsonify({"error": "請提供文字"}), 400

    try:
        client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))
        prompt = f"""
        你是一個專業的資料整理機器人。
        請從以下輸入中，萃取出「店名」、「完整地址」、「搜尋用乾淨地址」與「標籤」。
        「標籤」請判斷它是什麼類型的店(如咖啡廳、日式料理)，以及如果文字有提到靠近哪個捷運站，請一併放入標籤，多個標籤請用逗號分隔。
        回傳嚴格的 JSON 格式，不要加上 ```json 等標記：
        {{"name": "店名", "address": "完整地址", "search_address": "乾淨地址", "category": "標籤"}}
        輸入：{raw_text}
        """
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=prompt
                )
                break
            except Exception as e:
                if "503" in str(e) and attempt < max_retries - 1:
                    time.sleep(2)
                else:
                    raise e

        clean_text = response.text.strip('` \njson')
        result_dict = json.loads(clean_text)
        
        store_name = result_dict.get("name", "")
        if not store_name or store_name.strip() == "":
            store_name = "未命名神秘美食 🕵️‍♂️"
            
        store_address = result_dict.get("address", "")
        search_address = result_dict.get("search_address", store_address)
        store_category = result_dict.get("category", "")
        
        lat, lon = get_coordinates(search_address)
        
        result_dict["latitude"] = lat
        result_dict["longitude"] = lon
        result_dict["category"] = store_category
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO stores (user_id, name, category, address, latitude, longitude, created_at, is_eaten, is_favorite)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)
        """, (1, store_name, store_category, store_address, lat, lon, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        
        return jsonify({"message": "✅ AI 解析與座標定位成功！", "data": result_dict}), 200
        
    except Exception as e:
        return jsonify({"error": f"AI 解析失敗: {str(e)}"}), 500

# 2. 變更店家狀態 (吃過/最愛)
@app.route("/update_status/<int:store_id>", methods=["POST"])
def update_status(store_id):
    data = request.get_json()
    field = data.get("field") # 'is_eaten' 或是 'is_favorite'
    value = data.get("value") # 1 或是 0
    
    if field not in ['is_eaten', 'is_favorite']:
        return jsonify({"error": "不支援的更新欄位"}), 400
        
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(f"UPDATE stores SET {field} = ? WHERE id = ?", (value, store_id))
    conn.commit()
    conn.close()
    return jsonify({"message": "狀態更新成功！"})

# 3. 綜合進階搜尋引擎 (取代原本的 /nearby 和 /search)
@app.route("/advanced_search", methods=["POST"])
def advanced_search():
    data = request.get_json()
    keyword = data.get("keyword", "").strip()
    user_lat = data.get("latitude")
    user_lon = data.get("longitude")
    radius = data.get("radius") # "unlimited" 或 數字
    is_eaten = data.get("is_eaten") # "all", "1", "0"
    is_favorite = data.get("is_favorite") # "all", "1"
    sort_by = data.get("sort_by") # "distance", "newest"

    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 準備 SQL 查詢語法
    query = "SELECT * FROM stores WHERE 1=1"
    params = []
    
    # 處理關鍵字
    if keyword:
        query += " AND (category LIKE ? OR name LIKE ? OR address LIKE ?)"
        params.extend([f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"])
    
    # 處理狀態過濾
    if is_eaten in ["0", "1"]:
        query += " AND is_eaten = ?"
        params.append(int(is_eaten))
        
    if is_favorite == "1":
        query += " AND is_favorite = 1"
        
    rows = cursor.execute(query, params).fetchall()
    conn.close()

    result = []
    for row in rows:
        store = dict(row)
        store["google_maps_url"] = get_google_maps_url(store["name"], store["address"])
        
        # 計算距離
        if store["latitude"] and store["longitude"] and user_lat and user_lon:
            dist = haversine(user_lat, user_lon, store["latitude"], store["longitude"])
            store["distance_km"] = round(dist, 2)
        else:
            store["distance_km"] = 9999 # 算不出來就設一個極大值
            
        # 處理距離過濾
        if radius != "unlimited":
            if store["distance_km"] > float(radius):
                continue # 超過距離，跳過這家店
                
        result.append(store)
        
    # 處理排序
    if sort_by == "distance":
        result.sort(key=lambda x: x["distance_km"])
    else: # 最新加入
        result.sort(key=lambda x: x.get("created_at", ""), reverse=True)

    return jsonify(result)

# 4. 取得所有店家 (用於清單)
@app.route("/stores", methods=["GET"])
def get_stores():
    conn = get_db_connection()
    cursor = conn.cursor()
    rows = cursor.execute("SELECT * FROM stores ORDER BY id DESC").fetchall()
    stores = [dict(row) for row in rows]
    conn.close()
    
    for store in stores:
        store["google_maps_url"] = get_google_maps_url(store["name"], store["address"])
        
    return jsonify(stores)

# ----------------------
# 資料分析與吃貨儀表板
# ----------------------
@app.route("/dashboard_data", methods=["POST"])
def dashboard_data():
    data = request.get_json()
    user_lat = data.get("latitude")
    user_lon = data.get("longitude")

    conn = get_db_connection()
    cursor = conn.cursor()
    rows = cursor.execute("SELECT * FROM stores").fetchall()
    conn.close()

    total_stores = len(rows)
    eaten_count = 0
    total_distance = 0
    category_counts = {}

    for row in rows:
        store = dict(row)
        
        # 1. 統計標籤 (圓餅圖要用的)
        cat_string = store.get("category", "") or "未分類"
        # 因為標籤可能是「咖啡廳, 中山站」，我們把它切開來算
        tags = [t.strip() for t in cat_string.split(",")]
        for tag in tags:
            if tag: # 如果標籤不是空的
                category_counts[tag] = category_counts.get(tag, 0) + 1

        # 2. 統計吃過的數量與累積距離
        if store.get("is_eaten") == 1:
            eaten_count += 1
            # 只有當我們有定位，而且店家有座標時才算距離
            if user_lat and user_lon and store["latitude"] and store["longitude"]:
                dist = haversine(user_lat, user_lon, store["latitude"], store["longitude"])
                total_distance += dist

    # 3. 遊戲化：根據「累積移動距離」頒發吃貨稱號
    level = "見習吃貨 🐣" # 預設稱號
    if total_distance >= 100:
        level = "米其林級美食博主 👑 (移動超過 100 公里)"
    elif total_distance >= 50:
        level = "城市美食獵人 🦅 (移動超過 50 公里)"
    elif total_distance >= 20:
        level = "巷弄貪吃鬼 🏃‍♂️ (移動超過 20 公里)"
    elif total_distance > 0:
        level = "快樂小吃貨 😋 (剛開始探索)"

    return jsonify({
        "total": total_stores,
        "eaten": eaten_count,
        "distance": round(total_distance, 1),
        "level": level,
        "categories": category_counts
    })

if __name__ == "__main__":
    init_db()
    app.run(debug=True)
