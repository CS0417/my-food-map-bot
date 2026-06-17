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

# 強制 Python 的輸出入管線都使用 UTF-8 萬國碼
sys.stdout.reconfigure(encoding='utf-8')

app = Flask(__name__)
app.json.ensure_ascii = False
DB_NAME = "favorite_places.db"
configuration = Configuration(access_token=os.getenv('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.getenv('LINE_CHANNEL_SECRET'))

# ----------------------
# 🤖 新增：專門接收 LINE 訊息的路由
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
# ----------------------
# 資料庫初始化與升級
# ----------------------
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        line_user_id TEXT UNIQUE,
        display_name TEXT,
        created_at TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS stores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        name TEXT NOT NULL,
        category TEXT,
        address TEXT,
        latitude REAL,
        longitude REAL,
        note TEXT,
        source TEXT,
        rating REAL,
        created_at TEXT,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )
    """)
    
    # 💡 這次升級的核心：安全地加上「吃過」與「最愛」兩個新欄位
    try:
        cursor.execute("ALTER TABLE stores ADD COLUMN is_eaten INTEGER DEFAULT 0")
    except Exception:
        pass # 如果欄位已經存在就會報錯跳過，這是正常的
        
    try:
        cursor.execute("ALTER TABLE stores ADD COLUMN is_favorite INTEGER DEFAULT 0")
    except Exception:
        pass

    conn.commit()
    conn.close()

# ----------------------
# 輔助函數區
# ----------------------
def haversine(lat1, lon1, lat2, lon2):
    R = 6371  # 地球半徑 (km)
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return R * c

def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def get_google_maps_url(name, address):
    safe_name = name if name else ""
    safe_address = address if address else ""
    query = f"{safe_name} {safe_address}".strip()
    encoded_query = urllib.parse.quote(query)
    return f"https://www.google.com/maps/search/?api=1&query={encoded_query}"

def get_coordinates(address):
    if not address:
        return None, None
        
    def fetch_api(query):
        url = "https://nominatim.openstreetmap.org/search"
        params = {"q": query, "format": "jsonv2", "limit": 1}
        headers = {"User-Agent": "my_food_map_bot_by_caitlin"}
        try:
            response = requests.get(url, params=params, headers=headers)
            data = response.json()
            if data:
                return float(data[0]["lat"]), float(data[0]["lon"])
        except Exception as e:
            print(f"地圖轉換失敗: {e}")
        return None, None

    lat, lon = fetch_api(address)
    if lat and lon:
        return lat, lon
        
    fallback_match = re.search(r"(.+?[縣市].+?[市區鄉鎮].+?[路街大道])", address)
    if fallback_match:
        short_address = fallback_match.group(1)
        return fetch_api(short_address)

    return None, None
# ... (前面的 import 和 init_db 保持不變)

# ----------------------
# 蜘蛛情報員：PTT 爬蟲模組
# ----------------------
def crawl_ptt_food():
    url = "https://www.ptt.cc/bbs/Food/index.html"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        
        results = []
        divs = soup.find_all("div", class_="r-ent")
        
        for div in reversed(divs): 
            a_tag = div.find("a")
            if a_tag:
                title = a_tag.text
                if "[公告]" in title or "Fw:" in title:
                    continue
                
                # 去文章裡面抓第一段內文，讓 AI 有更多資訊可以分析
                link = "https://www.ptt.cc" + a_tag["href"]
                try:
                    article_resp = requests.get(link, headers=headers)
                    article_soup = BeautifulSoup(article_resp.text, "html.parser")
                    # PTT 文章內容通常在 main-content
                    main_content = article_soup.find("div", id="main-content")
                    # 簡單過濾掉不需要的標籤，只保留文字前 300 字
                    if main_content:
                        texts = main_content.find_all(text=True, recursive=False)
                        content = "".join(texts).strip()[:300]
                    else:
                        content = ""
                except Exception:
                    content = ""

                results.append({
                    "title": title, 
                    "content": content,
                    "source_url": link # 記錄來源網址
                })
                
                if len(results) >= 3: # 測試時先抓 3 篇就好，免得 AI 等太久
                    break
        return results
    except Exception as e:
        print(f"PTT 爬蟲失敗: {e}")
        return []

# ... (中間你原本的 haversine, get_coordinates 保持不變)

# ----------------------
# 自動化管線：一鍵爬蟲 + AI 處理
# ----------------------
@app.route("/auto_crawl_and_add", methods=["POST"])
def auto_crawl_and_add():
    print("🕷️ 啟動自動情報管線...")
    
    # 1. 啟動爬蟲去 PTT 抓文章
    articles = crawl_ptt_food()
    if not articles:
        return jsonify({"error": "爬蟲沒抓到資料，請稍後再試。"}), 500

    client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))
    added_stores = []
    conn = get_db_connection()
    cursor = conn.cursor()

    # 2. 把抓到的每一篇文章丟給 AI 處理
    for article in articles:
        print(f"🤖 AI 正在分析文章：{article['title']}")
        
        raw_text = f"標題：{article['title']}\n內文：{article['content']}"
        prompt = f"""
        你是一個專業的資料整理機器人。
        請從以下輸入中，萃取出「店名」、「完整地址」、「搜尋用乾淨地址」與「標籤」。
        「標籤」請判斷它是什麼類型的店(如咖啡廳、日式料理)，以及如果文字有提到靠近哪個捷運站，請一併放入標籤，多個標籤請用逗號分隔。
        回傳嚴格的 JSON 格式，不要加上 ```json 等標記。
        **如果這篇文章不是食記，或是完全找不到店名和地址，請回傳 {{"error": "找不到店家"}}**
        
        輸入：{raw_text}
        """
        
        try:
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt
            )
            clean_text = response.text.strip('` \njson')
            result_dict = json.loads(clean_text)
            
            # 如果 AI 判斷這篇不是食記，就跳過
            if "error" in result_dict:
                print("⏭️ AI 判斷這篇不是店家資訊，跳過。")
                continue

            store_name = result_dict.get("name", "")
            if not store_name or store_name.strip() == "":
                continue
            
            # 👇 --- 從這裡開始是你漏掉的下半身，請補上去 --- 👇
            store_address = result_dict.get("address", "")
            search_address = result_dict.get("search_address", store_address)
            store_category = result_dict.get("category", "")
            
            lat, lon = get_coordinates(search_address)
            
            # 存入資料庫
            cursor.execute("""
                INSERT INTO stores (user_id, name, category, address, latitude, longitude, created_at, is_eaten, is_favorite, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?)
            """, (1, store_name, store_category, store_address, lat, lon, datetime.now().isoformat(), article['source_url']))
            
            added_stores.append(store_name)
            
        # 這裡就是 Python 苦苦尋找的 except！
        except Exception as e:
            print(f"處理文章 {article['title']} 時發生錯誤: {e}")
            continue # 就算這篇出錯，也繼續處理下一篇

    conn.commit()
    conn.close()
    
    # 爬蟲跑完後，必須告訴網頁結果
    if added_stores:
        return jsonify({"message": f"✅ 管線執行完成！成功新增 {len(added_stores)} 家餐廳：{', '.join(added_stores)}"})
    else:
        return jsonify({"message": "🤷‍♂️ 爬蟲有抓到文章，但 AI 沒有從裡面找到任何新餐廳資訊。"})
    # 👆 --- 到這裡結束 --- 👆
# ----------------------
# 路由區
# ----------------------
@app.route("/")
def index():
    return render_template("index.html")

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
