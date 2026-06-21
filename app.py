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
DB_NAME = "favorite_places_v2.db"

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
    
# =========================================================
# 2. 資料庫連接與初始化
# =========================================================
def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    print("🚀 正在檢查並初始化資料庫...")
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
            source_type TEXT DEFAULT 'manual',
            source_url TEXT,
            source_title TEXT,
            created_at TEXT,
            is_eaten INTEGER DEFAULT 0,
            is_favorite INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()
    print("✅ 資料庫檢查完成！")
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
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            "q": address,
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

from math import radians, sin, cos, sqrt, atan2

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
        data = json.loads(raw)
        print("Gemini raw response:", repr(response.text))

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
        """, (name, category, address, lat, lon, url, datetime.now().isoformat()))
        conn.commit()
        conn.close()

        return True, name

    except Exception as e:
        print("Gemini error:", repr(e))
        return False, f"系統處理失敗：{str(e)}"
def get_top_n_nearby_stores(user_lat, user_lon, top_n=5):
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM stores").fetchall()
    conn.close()

    result = []

    for row in rows:
        store = dict(row)
        lat = store.get("latitude")
        lon = store.get("longitude")

        if lat is not None and lon is not None:
            store["distance_km"] = round(haversine(user_lat, user_lon, lat, lon), 2)
            result.append(store)

    result.sort(key=lambda x: x["distance_km"])
    return result[:top_n]


def get_stores_with_distance(user_lat, user_lon):
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM stores").fetchall()
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
    rows = conn.execute("SELECT * FROM stores").fetchall()
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
#爬蟲
def crawl_top5_food_recommendations(target_url):
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; FoodGuideBot/1.0)"
    }
    try:
        res = requests.get(target_url, headers=headers, timeout=10)
        print("status_code =", res.status_code)
        print("final_url =", res.url)
        print("content_type =", res.headers.get("Content-Type"))
        print("html 前 500 字：")
        print(res.text[:500])
    
        res.raise_for_status()

    soup = BeautifulSoup(res.text, "html.parser")
    items = []

    for i,card in enumerate(cards[:5],start=1):
        title_tag = card.select_one(".title")
            summary_tag = card.select_one(".summary")
            link_tag = card.select_one("a")

            print(f"第 {i} 筆 title_tag =", title_tag)
            print(f"第 {i} 筆 summary_tag =", summary_tag)
            print(f"第 {i} 筆 link_tag =", link_tag)

        if not title_tag:
            continue

        title = title_tag.get_text(strip=True)
        summary = summary_tag.get_text(strip=True) if summary_tag else ""
        link = urljoin(target_url, link_tag.get("href")) if link_tag and link_tag.get("href") else target_url

        items.append({
                "title": title,
                "summary": summary,
                "url": link
            })

        print("items =", items)
        return items

    except Exception as e:
        print("crawl error:", repr(e))
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
    rows = conn.execute("SELECT * FROM stores ORDER BY id DESC").fetchall()
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
@app.route("/auto_crawl", methods=["POST"])
def auto_crawl():
    """接收網址，爬取網頁內容後交給 Gemini 解析並存入資料庫"""
    data = request.get_json()
    url = data.get("url", "").strip()

    if not url:
        return jsonify({"error": "請提供網址"}), 400

    try:
        # 1. 偽裝成正常瀏覽器發送請求，避免被網站阻擋
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        res = requests.get(url, headers=headers, timeout=10)
        res.encoding = 'utf-8'

        # 2. 使用 BeautifulSoup 解析 HTML，把網頁標籤濾掉，只留下純文字
        soup = BeautifulSoup(res.text, "html.parser")
        text_content = soup.get_text(separator=" ", strip=True)

        # 為了避免網頁文字太多塞爆 Gemini 的 Token 上限，我們只取前 3000 字
        truncated_text = text_content[:3000]

        # 3. 直接呼叫我們之前寫好的 AI 處理函式！
        success, result = process_and_save_store(f"這是從網頁擷取下來的內容，請從中找出一間介紹的餐廳並輸出JSON：{truncated_text}")

        if success:
            return jsonify({"message": f"爬蟲成功！已自動從網頁抓取並新增：{result}"}), 200
        else:
            return jsonify({"error": f"爬蟲有抓到網頁，但 AI 找不到餐廳資訊：{result}"}), 400

    except Exception as e:
        return jsonify({"error": f"爬蟲擷取失敗：{str(e)}"}), 500
@app.route("/update_status/<int:store_id>", methods=["POST"])
def update_status(store_id):
    """更新吃過或最愛狀態"""
    data = request.get_json()
    field = data.get("field")
    value = data.get("value")

    if field not in ["is_eaten", "is_favorite"]:
        return jsonify({"error": "不支援的狀態更新"}), 400

    conn = get_db_connection()
    conn.execute(f"UPDATE stores SET {field} = ? WHERE id = ?", (value, store_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/advanced_search", methods=["POST"])
def advanced_search():
    data = request.get_json()

    keyword = data.get("keyword", "").strip()
    latitude = data.get("latitude")
    longitude = data.get("longitude")
    radius = float(data.get("radius", 5))
    is_eaten = data.get("is_eaten", "all")
    sort_by = data.get("sort_by", "distance")

    query = "SELECT * FROM stores WHERE (name LIKE ? OR category LIKE ? OR address LIKE ?)"
    params = [f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"]

    if is_eaten in ["0", "1"]:
        query += " AND is_eaten = ?"
        params.append(int(is_eaten))

    conn = get_db_connection()
    rows = conn.execute(query, params).fetchall()
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
    """產生儀表板所需的統計資料"""
    conn = get_db_connection()
    total = conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0]
    eaten = conn.execute("SELECT COUNT(*) FROM stores WHERE is_eaten=1").fetchone()[0]
    
    # 統計各類別數量
    rows = conn.execute("SELECT category, COUNT(*) as count FROM stores GROUP BY category").fetchall()
    conn.close()

    categories = {r["category"]: r["count"] for r in rows if r["category"]}

    # 稱號判定
    if total < 10: level = "美食初心者 🐣"
    elif total < 30: level = "城市探險家 🚶"
    elif total < 50: level = "老饕達人 😋"
    else: level = "傳說級吃貨 👑"

    return jsonify({
        "total": total,
        "eaten": eaten,
        "distance": 0, # 未來可結合 GPS 計算累積里程
        "level": level,
        "categories": categories
    })
#爬蟲
@app.route("/crawl_recommendations", methods=["GET"])
def crawl_recommendations():
    target_url = request.args.get("url", "").strip()

    if not target_url:
        return jsonify({"error": "缺少 url 參數"}), 400

    try:
        items = crawl_top5_food_recommendations(target_url)
        return jsonify(items), 200
    except Exception as e:
        print("crawl error:", repr(e))
        return jsonify({"error": str(e)}), 500
@app.route("/add_recommendation", methods=["POST"])
def add_recommendation():
    data = request.get_json()

    title = data.get("title", "").strip()
    url = data.get("url", "").strip()
    summary = data.get("summary", "").strip()

    if not title:
        return jsonify({"error": "缺少 title"}), 400

    try:
        conn = get_db_connection()
        conn.execute("""
            INSERT INTO stores
            (name, category, address, google_maps_url, source_type, source_url, source_title, created_at, is_eaten, is_favorite)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            title,
            "網路推薦",
            summary,          # 這裡先把 summary 放 address 欄位當備用說明
            url,
            "crawled",
            url,
            title,
            datetime.now().isoformat(),
            0,
            1
        ))
        conn.commit()
        conn.close()

        return jsonify({"message": f"已加入清單：{title}"}), 200

    except Exception as e:
        print("add error:", repr(e))
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
            keyword = user_msg.replace("查詢", "").strip()
            conn = get_db_connection()
            rows = conn.execute(
                "SELECT * FROM stores WHERE name LIKE ? OR category LIKE ? OR address LIKE ?",
                (f"%{keyword}%", f"%{keyword}%", f"%{keyword}%")
            ).fetchall()
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
@app.route("/nearby_stores", methods=["POST"])
def nearby_stores():
    data = request.get_json()
    user_lat = data.get("latitude")
    user_lon = data.get("longitude")
    max_distance = float(data.get("max_distance_km", 3))

    if user_lat is None or user_lon is None:
        return jsonify({"error": "缺少座標"}), 400

    stores = get_nearby_stores(user_lat, user_lon, max_distance)
    return jsonify(stores)
@handler.add(MessageEvent, message=LocationMessageContent)
def handle_location(event):
    user_lat = event.message.latitude
    user_lon = event.message.longitude

    stores = get_top_n_nearby_stores(user_lat, user_lon, top_n=5)

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

# =========================================================
# 7. 啟動伺服器
# =========================================================
if __name__ == "__main__":
    # 本地測試時啟動，Render 部署時將由 gunicorn 接管
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
