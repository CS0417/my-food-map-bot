from flask import Flask, request, jsonify, render_template
import psycopg2
from psycopg2.extras import RealDictCursor
import json
from google import genai
from google.genai import types
import requests
from datetime import datetime    
from math import radians, sin, cos, sqrt, atan2
import sys
import urllib.parse
import os
import re
#爬蟲
from bs4 import BeautifulSoup
from urllib.parse import urljoin

# LINE Bot v3 SDK 相關套件
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, 
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, LocationMessageContent

# 強制 Python 輸出入管線使用 UTF-8 編碼
sys.stdout.reconfigure(encoding="utf-8")

app = Flask(__name__)
app.json.ensure_ascii = False


# =========================================================
# 1. 環境變數與 LINE、Gemini 初始化設定
# =========================================================
line_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
line_secret = os.getenv("LINE_CHANNEL_SECRET")
gemini_key = os.getenv("GEMINI_API_KEY")
database_url = os.getenv("DATABASE_URL") # 讀取 Supabase 連線字串

if not all([line_token, line_secret, gemini_key]):
    print("⚠️ 警告：環境變數未設定完整 (請檢查 LINE_TOKEN, LINE_SECRET, 或 GEMINI_API_KEY)")

configuration = Configuration(access_token=line_token)
handler = WebhookHandler(line_secret)
# =========================================================
# 2. Supabase (PostgreSQL) 資料庫連接與初始化
# =========================================================
def get_db_connection():
    # 使用 RealDictCursor 讓讀取出來的資料能像 SQLite 的 Row 一樣，直接用欄位名稱當作 Key 存取
    conn = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    return conn

def init_db():
    print("🚀 正在檢查並初始化資料庫...")
    try:
        conn = get_db_connection()
        cur=conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS stores (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT,
                address TEXT,
                latitude REAL,
                longitude REAL,
                google_maps_url TEXT,
                source_type TEXT DEFAULT 'manual',
                source_url TEXT,
                source_title TEXT,
                created_at TEXT,
                is_eaten INTEGER DEFAULT 0,
                is_favorite INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("✅ Supabase 資料庫檢查與初始化完成！")
    except Exception as e:
        print(f"❌ 資料庫初始化失敗: {e}")
if database_url:
    init_db()
# =========================================================
# 3. 工具與輔助函式
# =========================================================
def get_google_maps_url(name, address):
    """將店名與地址轉換為可點擊的 Google Maps 搜尋網址"""
    query = urllib.parse.quote(f"{name} {address}")
    return f"https://www.google.com/maps/search/?api=1&query={query}"

def get_coordinates(address):
    """利用 OpenStreetMap API 取得真實經緯度座標"""
    try:
        clean_address = re.sub(r'[^縣市區鄉鎮]+里', '', address)
        clean_address = re.sub(r'\d+鄰', '', clean_address)
        
        print(f"🔍 準備查詢座標，原始地址: {address} -> 清洗後: {clean_address}")
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            "q": clean_address,
            "format": "json",
            "limit": 1
        }
        headers = {
            "User-Agent": "FoodGuideBot/1.0"
        }
        res = requests.get(url, params=params, headers=headers, timeout=10)
        data = res.json()
        if len(data) > 0:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        print(f"座標轉換失敗: {e}")
    return None, None

def haversine(lat1, lon1, lat2, lon2):
    """
    計算兩個經緯度座標之間的直線距離（公里）
    """
    R = 6371  # 地球半徑，單位：公里
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))

    return R * c

# =========================================================
# 4. 核心商業邏輯：AI 解析與資料寫入
# =========================================================
def process_and_save_store(text):
    try:
        if not gemini_key:
            return False, "伺服器缺少 GEMINI_API_KEY"
        client = genai.Client(api_key=gemini_key)

        prompt = f"""請只回傳合法 JSON，不要加任何說明文字。格式必須是：{{"name":"店名","address":"完整地址","category":"類別標籤"}}
請從以下文字萃取：{text}"""
        response = client.models.generate_content(model="gemini-2.5-flash",contents=prompt,)
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.replace("```json", "").replace("```", "").strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return False, f"AI 回傳的格式異常，請重試！(回傳內容: {raw[:20]}...)"
        
        print("Gemini raw response:", repr(response.text))

        name = data.get("name")
        address = data.get("address")
        category = data.get("category", "未分類")

        if not name or not address:
            return False, "AI 沒有抓到店名或地址"

        lat, lon = get_coordinates(address)
        url = get_google_maps_url(name, address)

        conn = get_db_connection()
        cur=conn.cursor()
        cur.execute("""
            INSERT INTO stores
            (name, category, address, latitude, longitude, google_maps_url, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (name, category, address, lat, lon, url, datetime.now().isoformat()))
        conn.commit()
        cur.close()
        
        conn.close()

        return True, name

    except Exception as e:
        print("Gemini error:", repr(e))
        return False, f"系統處理失敗：{str(e)}"

def get_stores_with_distance(user_lat, user_lon):
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT * FROM stores")
    rows = cur.fetchall()
    
    cur.close()
    conn.close()

    result = []

    for row in rows:
        store = dict(row)
        store_lat = store.get("latitude")
        store_lon = store.get("longitude")

        if store_lat is not None and store_lon is not None:
            distance = haversine(user_lat, user_lon, store_lat, store_lon)
            store["distance_km"] = round(distance, 2)
        else:
            store["distance_km"] = None

        result.append(store)

    # 依距離由近到遠排序，沒有距離的排最後
    result.sort(key=lambda x: x["distance_km"] if x["distance_km"] is not None else 999999)

    return result
    
def get_nearby_stores(user_lat, user_lon, max_distance_km=3):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM stores")
    rows = cur.fetchall()
    conn.close()

    result = []

    for row in rows:
        store = dict(row)
        store_lat = store.get("latitude")
        store_lon = store.get("longitude")

        if store_lat is not None and store_lon is not None:
            distance = haversine(user_lat, user_lon, store_lat, store_lon)

            if distance <= max_distance_km:
                store["distance_km"] = round(distance, 2)
                result.append(store)

    result.sort(key=lambda x: x["distance_km"])
    return result
def search_nearby_places_osm_no_sort(lat, lon, radius_km=3, limit=10):
    """
    使用免費的 OpenStreetMap (Overpass API) 搜尋附近餐廳
    不排序，直接回傳符合條件的結果
    """
    radius_meters = int(radius_km * 1000)

    overpass_query = f"""
    [out:json][timeout:25];
    (
      node["amenity"~"restaurant|cafe|fast_food|bar"](around:{radius_meters},{lat},{lon});
      way["amenity"~"restaurant|cafe|fast_food|bar"](around:{radius_meters},{lat},{lon});
      relation["amenity"~"restaurant|cafe|fast_food|bar"](around:{radius_meters},{lat},{lon});
    );
    out center;
    """

    overpass_url = "https://overpass-api.de/api/interpreter"

    try:
        headers = {
            "User-Agent": "FoodGuideBot/1.0 (your_email@example.com)"
        }

        response = requests.post(
            overpass_url,
            data={"data": overpass_query},
            headers=headers,
            timeout=30
        )
        response.raise_for_status()

        data = response.json()
        results = []

        for element in data.get("elements", []):
            tags = element.get("tags", {})
            name = tags.get("name:zh") or tags.get("name")
            if not name:
                continue

            # node 有 lat/lon；way / relation 用 center
            if element.get("type") == "node":
                lat_store = element.get("lat")
                lon_store = element.get("lon")
            else:
                center = element.get("center", {})
                lat_store = center.get("lat")
                lon_store = center.get("lon")

            if lat_store is None or lon_store is None:
                continue

            category = tags.get("cuisine") or tags.get("amenity", "未分類")
            dist_km = round(haversine(lat, lon, lat_store, lon_store), 2)

            gmap_url = get_google_maps_url(name, f"{lat_store},{lon_store}")

            results.append({
                "name": name,
                "category": category,
                "distance_km": dist_km,
                "google_maps_url": gmap_url,
                "latitude": lat_store,
                "longitude": lon_store
            })

            if len(results) >= limit:
                break

        return results

    except Exception as e:
        print(f"Overpass API 搜尋失敗: {repr(e)}")
        return []
# =========================================================
# 5. 網頁端 Web API 路由 (給前端 JS 呼叫)
# =========================================================
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/stores", methods=["GET"])
def get_stores():
    """取得所有餐廳資料供地圖標記"""
    conn = get_db_connection()
    cur = conn.cursor()  # 🌟 關鍵：建立游標
    
    cur.execute("SELECT * FROM stores ORDER BY id DESC")
    rows = cur.fetchall()
    
    cur.close()          # 🌟 關鍵：關閉游標
    conn.close()
    return jsonify([dict(row) for row in rows])

@app.route("/ai_add", methods=["POST"])
def ai_add():
    """網頁前端呼叫的 AI 新增接口"""
    print("hit /ai_add")
    data = request.get_json()
    success, result = process_and_save_store(data.get("text", ""))
    
    if success:
        return jsonify({"message": f"成功新增：{result}"}), 200
    else:
        return jsonify({"error": result}), 400

@app.route("/update_status/<int:store_id>", methods=["POST"])
def update_status(store_id):
    """更新吃過或最愛狀態"""
    data = request.get_json()
    field = data.get("field")
    value = data.get("value")

    if field not in ["is_eaten", "is_favorite"]:
        return jsonify({"error": "不支援的狀態更新"}), 400

    conn = get_db_connection()
    cur = conn.cursor() # 🌟 已加入游標與 %s
    allowed_fields = {
    "is_eaten",
    "is_favorite"
    }
    
    if field not in allowed_fields:
        return jsonify(...)
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"success": True})

@app.route("/advanced_search", methods=["POST"])
def advanced_search():
    data = request.get_json()

    keyword = data.get("keyword", "").strip()
    latitude = data.get("latitude")
    longitude = data.get("longitude")
    
    radius_input = data.get("radius", 5)
    if radius_input == "unlimited":
        radius = float('inf')
    else:
        radius = float(radius_input)
    is_eaten = data.get("is_eaten", "all")
    sort_by = data.get("sort_by", "distance")

    query = "SELECT * FROM stores WHERE (name LIKE %s OR category LIKE %s OR address LIKE %s)"
    params = [f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"]

    if is_eaten in ["0", "1"]:
        query += " AND is_eaten = %s"
        params.append(int(is_eaten))

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(query, params)
    rows = cur.fetchall()
    
    cur.close()          # 🌟 關鍵：關閉游標
    conn.close()

    result = []

    for row in rows:
        store = dict(row)

        if latitude is not None and longitude is not None and store["latitude"] is not None and store["longitude"] is not None:
            dist = haversine(float(latitude), float(longitude), float(store["latitude"]), float(store["longitude"]))
            store["distance_km"] = round(dist, 2)

            if dist <= radius:
                result.append(store)
        else:
            store["distance_km"] = None
            result.append(store)

    if sort_by == "distance":
        result.sort(key=lambda x: x["distance_km"] if x["distance_km"] is not None else 999999)

    return jsonify(result)

@app.route("/dashboard_data", methods=["POST"])
def dashboard_data():
    conn = get_db_connection()
    cur = conn.cursor()  # 🌟 關鍵 1：建立游標
    
    # 🌟 關鍵 2：改用 cur.execute，並用 as count 命名，然後用 ['count'] 取值
    cur.execute("SELECT COUNT(*) as count FROM stores")
    total = cur.fetchone()['count']
    
    cur.execute("SELECT COUNT(*) as count FROM stores WHERE is_eaten=1")
    eaten = cur.fetchone()['count']
    
    cur.execute("SELECT category, COUNT(*) as count FROM stores GROUP BY category")
    rows = cur.fetchall()
    
    cur.close()
    conn.close()

    categories = {r["category"]: r["count"] for r in rows if r["category"]}

    if total < 10: level = "美食初心者 🐣"
    elif total < 30: level = "城市探險家 🚶"
    elif total < 50: level = "老饕達人 😋"
    else: level = "傳說級吃貨 👑"

    return jsonify({
        "total": total,
        "eaten": eaten,
        "distance": 0,
        "level": level,
        "categories": categories
    })

@app.route("/nearby_foods", methods=["POST"])
def nearby_foods():
    data = request.get_json()
    lat = data.get("latitude")
    lon = data.get("longitude")
    radius_km = float(data.get("radius_km", 3))
    limit = int(data.get("limit", 10))

    if lat is None or lon is None:
        return jsonify({"error": "缺少座標"}), 400

    try:
        results = search_nearby_places_osm_no_sort(lat, lon, radius_km, limit)
        return jsonify(results), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =========================================================
# 6. LINE Bot 接收與處理核心
# =========================================================
@app.route("/callback", methods=["POST"])
def callback():
    """LINE 官方伺服器 Webhook 接口"""
    signature = request.headers.get("X-Line-Signature", "")
    if not signature:return "Missing signature",400
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        return "Invalid signature", 400
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    """處理使用者傳送的 LINE 文字訊息"""
    # 統一全形冒號為半形，增加容錯率
    user_msg = event.message.text.strip().replace("：", ":")
    reply_text = ""

    try:
        # A. 新增指令
        if user_msg.startswith("新增:"):
            content = user_msg.replace("新增:", "").strip()
            success, result = process_and_save_store(content)
            reply_text = f"✅ 成功儲存：{result}" if success else f"❌ 新增失敗：{result}"

        # B. 查詢指令
        elif user_msg.startswith("查詢"):
            keyword = (
                user_msg
                .replace("查詢:", "")
                .replace("查詢", "")
                .strip()
            )
            conn = get_db_connection()
            cur = conn.cursor() # 🌟 已加入游標與 %s
            cur.execute(
                "SELECT * FROM stores WHERE name LIKE %s OR category LIKE %s OR address LIKE %s",
                (f"%{keyword}%", f"%{keyword}%", f"%{keyword}%")
            )
            rows = cur.fetchall()
            cur.close()
            conn.close()

            if rows:
                lines = []
                for r in rows:
                    lines.append(
                        f"🍽️ 店名：{r['name']}\n"
                        f"🏷️ 分類：{r['category']}\n"
                        f"📍 地址：{r['address']}\n"
                        f"🔗 導航：{r['google_maps_url']}"
                    )
                reply_text = f"🔍 關於「{keyword}」的搜尋結果：\n\n" + "\n\n".join(lines)
            else:
                reply_text = f"😢 找不到與「{keyword}」相關的餐廳喔！"

        # C. 選單引導與預設回覆
        elif user_msg == "新增餐廳":
            reply_text = "✨ 請輸入「新增:」加上店名與地址\n範例：新增:一蘭拉麵 信義區松壽路11號"
        elif user_msg == "查詢餐廳":
            reply_text = "🔍 請輸入「查詢」加上關鍵字\n範例：查詢 拉麵"
        else:
            reply_text = "歡迎使用美食地圖！\n請點擊下方選單，或直接輸入：\n👉 新增: [店家資訊]\n👉 查詢 [關鍵字]"

    except Exception as e:
        reply_text = f"❌ 系統處理發生異常：{str(e)}"

    # 透過最新的 v3 SDK 寫法回傳訊息
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )

@app.route("/search_nearby_osm", methods=["POST"])
def api_search_nearby_osm():
    data = request.get_json()
    lat = data.get("latitude")
    lon = data.get("longitude")
    radius = data.get("radius_km", 3)
    
    if not lat or not lon:
        return jsonify({"error": "缺少經緯度"}), 400
        
    results = search_nearby_places_osm_no_sort(lat, lon, radius_km=radius, limit=10)
    return jsonify(results)   
@handler.add(MessageEvent, message=LocationMessageContent)
def handle_location(event):
    user_lat = event.message.latitude
    user_lon = event.message.longitude

    stores = get_nearby_stores(user_lat, user_lon, max_distance_km=3)[:5]

    if not stores:
        reply = "附近沒有找到店家"
    else:
        lines = []
        for s in stores:
            lines.append(
                f"店名：{s['name']}\n"
                f"距離：{s['distance_km']} 公里\n"
                f"Google Maps：{s['google_maps_url']}"
            )
        reply = "\n\n".join(lines)

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply)]
            )
        )
@app.route("/add_custom_store", methods=["POST"])
def add_custom_store():
    """接收前端傳來的完整店家資料，直接寫入 Supabase 雲端資料庫"""
    data = request.get_json()
    
    name = data.get("name")
    address = data.get("address")
    category = data.get("category", "未分類")
    lat = data.get("latitude")
    lon = data.get("longitude")
    url = data.get("google_maps_url")

    # 基本防呆驗證
    if not name or not address:
        return jsonify({"error": "缺少必要的店名或地址"}), 400

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 使用 PostgreSQL 的 %s 預留字元安全寫入
        cur.execute("""
            INSERT INTO stores 
            (name, category, address, latitude, longitude, google_maps_url, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (name, category,address, lat, lon, url, datetime.now().isoformat()))
        
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({"message": f"🎉 已成功將「{name}」收藏至你的美食清單！"}), 200
        
    except Exception as e:
        print(f"手動寫入雲端資料庫失敗: {e}")
        return jsonify({"error": f"伺服器寫入失敗：{str(e)}"}), 500
# =========================================================
# 7. 啟動伺服器
# =========================================================
if __name__ == "__main__":
    # 本地測試時啟動，Render 部署時將由 gunicorn 接管
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
