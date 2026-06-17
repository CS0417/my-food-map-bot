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

# ----------------------
# 網頁首頁路由 (幫你把重複的整理成只留一個在這裡)
# ----------------------
@app.route("/")
def index():
    return render_template("index.html")
