import os
import io
import re
import json
import time
import base64
import random
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request
import urllib.error
import telebot

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ================= 1. CONFIGURATION =================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", "8080"))

# Multi-Key Rotation: Render Environment Variables से लोड होगी
RAW_KEYS = os.environ.get("GEMINI_API_KEYS", "") or os.environ.get("GEMINI_API_KEY", "")
GEMINI_KEYS_LIST = [k.strip() for k in RAW_KEYS.split(",") if k.strip()]

current_key_index = 0

# Allowed Group IDs
ALLOWED_GROUP_IDS_STR = os.environ.get("ALLOWED_GROUP_IDS", "").strip()
ALLOWED_GROUP_IDS = [gid.strip() for gid in ALLOWED_GROUP_IDS_STR.split(",") if gid.strip()]

if not TELEGRAM_BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN environment variable not set!")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# ⭐ Bot info एक बार ही लोड करो (हर बार get_me नहीं करना पड़ेगा)
BOT_INFO = None
BOT_ID = 0
BOT_USERNAME = ""
try:
    BOT_INFO = bot.get_me()
    BOT_ID = BOT_INFO.id
    BOT_USERNAME = f"@{BOT_INFO.username}".lower()
    logger.info(f"Bot loaded: {BOT_USERNAME} (ID: {BOT_ID})")
except Exception as e:
    logger.error(f"Failed to get bot info: {e}")

# Super-fast Lite & Flash models
GEMINI_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.7-flash"
]

SYSTEM_PROMPT = (
    "You are a smart, concise AI study tutor for a Telegram student group. "
    "Your answers will be sent via Telegram HTML formatting.\n\n"
    "STRICT FORMAT RULES:\n"
    "1. First line: ✅ <b>सही उत्तर:</b> [answer] — always bold the label.\n"
    "2. Then a blank line, then 📝 <b>व्याख्या:</b> as a heading.\n"
    "3. Use 3-4 bullet points with • symbol. Bold key terms with <b>term</b> tags.\n"
    "4. Use <i>italic</i> for English scientific terms.\n"
    "5. End with a 💡 <b>याद रखें:</b> one-liner tip.\n"
    "6. Keep total answer SHORT (max 5-6 lines). No long paragraphs.\n"
    "7. Use simple Hindi / Hinglish.\n"
    "8. ONLY use these HTML tags: <b>bold</b>, <i>italic</i>, <code>code</code>. "
    "Do NOT use markdown ** or __ syntax. Do NOT use <br> tags. Use \\n for new lines.\n"
    "9. Do NOT use any other HTML tags like <ul>, <li>, <p>, <h1> etc.\n\n"
    "EXAMPLE FORMAT:\n"
    "✅ <b>सही उत्तर:</b> B) यकृत (Liver)\n\n"
    "📝 <b>व्याख्या:</b>\n"
    "• यकृत (<i>Liver</i>) मानव शरीर की <b>सबसे बड़ी ग्रंथि</b> है\n"
    "• इसका वजन लगभग <b>1.5 kg</b> होता है\n"
    "• यह <b>पित्त रस</b> (<i>Bile Juice</i>) का निर्माण करती है\n\n"
    "💡 <b>याद रखें:</b> सबसे बड़ी ग्रंथि = यकृत, सबसे छोटी ग्रंथि = पीयूष"
)

AUTO_TRIGGERS = [
    "explain", "samjhao", "batao", "solution", "solve", "kaise", 
    "why", "answer", "uttar", "हल", "समझाओ", "बताओ", "स्पष्ट", "details", "?",
    "kya", "kyu", "kyun", "kyon", "kon", "kaun", "kab", "kaha",
    "bta", "btao", "smjhao", "smjha", "hint", "help",
    "क्यों", "क्या", "कौन", "कब", "कैसे", "कहाँ", "सही", "गलत"
]

def is_chat_allowed(chat_id):
    if not ALLOWED_GROUP_IDS:
        return True
    return str(chat_id) in ALLOWED_GROUP_IDS

def get_current_api_key():
    global current_key_index
    if not GEMINI_KEYS_LIST:
        return ""
    return GEMINI_KEYS_LIST[current_key_index % len(GEMINI_KEYS_LIST)]

def rotate_to_next_key():
    global current_key_index
    if len(GEMINI_KEYS_LIST) > 1:
        current_key_index = (current_key_index + 1) % len(GEMINI_KEYS_LIST)
        logger.info(f"Switched to next API Key (Key #{current_key_index + 1} of {len(GEMINI_KEYS_LIST)})")

def is_tagged(text):
    """Check if bot is tagged in the text"""
    if not BOT_USERNAME or not text:
        return False
    return BOT_USERNAME in text.lower()

def has_trigger_word(text):
    """Check if text contains any trigger word"""
    if not text:
        return False
    text_lower = text.lower().strip()
    for trigger in AUTO_TRIGGERS:
        if trigger in text_lower:
            return True
    return False

# ⭐⭐⭐ MARKDOWN → TELEGRAM HTML CONVERTER ⭐⭐⭐
def md_to_html(text):
    """Convert Gemini's markdown output to Telegram-safe HTML"""
    if not text:
        return text
    
    # Step 1: Escape bare HTML special chars that aren't part of our allowed tags
    protected = []
    def protect_tag(match):
        protected.append(match.group(0))
        return f"__PROTECTED_{len(protected)-1}__"
    
    # Protect existing valid HTML tags: <b>, </b>, <i>, </i>, <code>, </code>
    text = re.sub(r'</?(?:b|i|code)>', protect_tag, text)
    
    # Escape remaining < and > and &
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    
    # Restore protected tags
    for idx, tag in enumerate(protected):
        text = text.replace(f"__PROTECTED_{idx}__", tag)
    
    # Step 2: Convert markdown bold **text** → <b>text</b>
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    
    # Step 3: Convert markdown italic *text* → <i>text</i>
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', text)
    
    # Step 4: Convert markdown `code` → <code>code</code>
    text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
    
    # Step 5: Convert markdown headers ### text → bold text
    text = re.sub(r'^#{1,3}\s*(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    
    # Step 6: Convert - bullet to • bullet
    text = re.sub(r'^[\-]\s+', '• ', text, flags=re.MULTILINE)
    text = re.sub(r'^\*\s+', '• ', text, flags=re.MULTILINE)
    
    return text.strip()

def safe_send_html(chat_id, text, reply_to_message_id=None):
    """Send message with HTML formatting, fallback to plain text if HTML fails"""
    html_text = md_to_html(text)
    try:
        if reply_to_message_id:
            bot.send_message(chat_id, html_text, parse_mode="HTML", reply_to_message_id=reply_to_message_id)
        else:
            bot.send_message(chat_id, html_text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"HTML send failed ({e}), sending as plain text")
        plain = re.sub(r'<[^>]+>', '', html_text)
        if reply_to_message_id:
            bot.send_message(chat_id, plain, reply_to_message_id=reply_to_message_id)
        else:
            bot.send_message(chat_id, plain)

# ================= 2. GEMINI REST API CLIENT =================
def call_gemini(parts, system_instruction=None):
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "maxOutputTokens": 450,
            "temperature": 0.4
        }
    }

    if system_instruction:
        payload["systemInstruction"] = {
            "parts": [{"text": system_instruction}]
        }

    encoded_data = json.dumps(payload).encode("utf-8")

    total_keys = len(GEMINI_KEYS_LIST)
    last_error = ""

    for _ in range(total_keys):
        active_key = get_current_api_key()
        if not active_key:
            return "⚠️ कोई Gemini API Key उपलब्ध नहीं है।"

        key_quota_exhausted = False

        for model_name in GEMINI_MODELS:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={active_key}"
            req = urllib.request.Request(
                url,
                data=encoded_data,
                headers={"Content-Type": "application/json"}
            )
            try:
                with urllib.request.urlopen(req, timeout=25) as response:
                    result = json.loads(response.read().decode("utf-8"))
                    candidates = result.get("candidates", [])
                    if candidates:
                        parts_out = candidates[0].get("content", {}).get("parts", [])
                        text_parts = [p.get("text", "") for p in parts_out if "text" in p and not p.get("thought", False)]
                        final_text = "".join(text_parts).strip()
                        if final_text:
                            return final_text
                    return "माफ़ कीजिये, इस प्रश्न का उत्तर नहीं मिल पाया।"
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="ignore")
                logger.warning(f"Key #{current_key_index + 1} | Model {model_name} HTTP {e.code}: {err_body[:100]}")
                last_error = f"HTTP {e.code}"
                if e.code == 429:
                    key_quota_exhausted = True
                    break
                elif e.code == 503:
                    time.sleep(1)
                continue
            except Exception as e:
                logger.warning(f"Error calling {model_name}: {e}")
                last_error = str(e)
                continue

        if key_quota_exhausted and total_keys > 1:
            rotate_to_next_key()
            continue

    return f"❌ AI सर्वर व्यस्त है ({last_error}), कृपया कुछ देर बाद पुनः प्रयास करें।"

# ================= 3. OFFLINE QUIZ BANK =================
DEFAULT_QUIZZES = [
    {
        "question": "मानव शरीर की सबसे बड़ी ग्रंथि (Largest Gland) कौन सी है?",
        "options": ["यकृत (Liver)", "थायरॉयड (Thyroid)", "अग्न्याशय (Pancreas)", "पीयूष (Pituitary)"],
        "correct_option_id": 0,
        "explanation": "मानव शरीर की सबसे बड़ी ग्रंथि यकृत (Liver) है, जिसका वजन लगभग 1.5 किलोग्राम होता है।"
    },
    {
        "question": "प्रकाश वर्ष (Light Year) किसकी इकाई (Unit) है?",
        "options": ["समय (Time)", "दूरी (Distance)", "प्रकाश की गति", "ऊर्जा (Energy)"],
        "correct_option_id": 1,
        "explanation": "प्रकाश वर्ष खगोलीय दूरी (Astronomical Distance) मापने की इकाई है।"
    },
    {
        "question": "भारतीय संविधान का जनक (Father of Indian Constitution) किसे कहा जाता है?",
        "options": ["महात्मा गांधी", "डॉ. बी. आर. अम्बेडकर", "जवाहरलाल नेहरू", "डॉ. राजेंद्र प्रसाद"],
        "correct_option_id": 1,
        "explanation": "डॉ. भीमराव अम्बेडकर को भारतीय संविधान का जनक माना जाता है।"
    },
    {
        "question": "पानीपत की पहली लड़ाई (First Battle of Panipat) किस वर्ष हुई थी?",
        "options": ["1526", "1556", "1761", "1530"],
        "correct_option_id": 0,
        "explanation": "पानीपत का पहला युद्ध 1526 में बाबर और इब्राहिम लोदी के बीच हुआ था।"
    },
    {
        "question": "इसरो (ISRO) का मुख्यालय (Headquarters) कहाँ स्थित है?",
        "options": ["नई दिल्ली", "मुंबई", "बेंगलुरु", "हैदराबाद"],
        "correct_option_id": 2,
        "explanation": "ISRO का मुख्यालय बेंगलुरु, कर्नाटक में है।"
    },
    {
        "question": "पौधों में प्रकाश संश्लेषण (Photosynthesis) के लिए कौन सी गैस आवश्यक है?",
        "options": ["ऑक्सीजन (O2)", "नाइट्रोजन (N2)", "कार्बन डाइऑक्साइड (CO2)", "हीलियम"],
        "correct_option_id": 2,
        "explanation": "पौधे सूर्य के प्रकाश और CO2 की सहायता से अपना भोजन बनाते हैं।"
    }
]

QUIZ_BANK = DEFAULT_QUIZZES
try:
    q_file = os.path.join(os.path.dirname(__file__), "questions.json")
    if os.path.exists(q_file):
        with open(q_file, "r", encoding="utf-8") as f:
            QUIZ_BANK = json.load(f)
except Exception as e:
    logger.warning(f"Using default quiz bank: {e}")

# ⭐ Quiz Poll Tracking
POLL_TRACKER = {}   # message_id -> quiz dict

# ================= 4. RENDER 24/7 WEB SERVER =================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Telegram AI Study Bot is alive and running on Render!")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()

    def do_POST(self):
        self.do_GET()

    def log_message(self, format, *args):
        return

def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    logger.info(f"Health server listening on port {PORT}")
    server.serve_forever()

threading.Thread(target=run_health_server, daemon=True).start()

# ================= 5. TELEGRAM BOT HANDLERS =================

@bot.message_handler(commands=['id', 'groupid'])
def send_group_id(message):
    bot.reply_to(message, f"📌 इस चैट का ID है: <code>{message.chat.id}</code>", parse_mode="HTML")

@bot.message_handler(content_types=['new_chat_members'])
def on_user_joins(message):
    if any(m.id == BOT_ID for m in message.new_chat_members):
        if not is_chat_allowed(message.chat.id):
            bot.send_message(message.chat.id, "❌ यह बॉट केवल अधिकृत स्टडी ग्रुप में कार्य करने के लिए आरक्षित है।")
            bot.leave_chat(message.chat.id)

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if message.chat.type == "private":
        bot.reply_to(
            message,
            "🔒 <b>यह बॉट केवल ग्रुप में कार्य करता है!</b>\n\n"
            "निजी चैट में यह कोई उत्तर नहीं देता।",
            parse_mode="HTML"
        )
        return

    if not is_chat_allowed(message.chat.id):
        return

    text = (
        "📚 <b>नमस्ते! मैं आपका AI Study &amp; Quiz Bot हूँ।</b> 🎓\n\n"
        "✨ <b>कमांड्स और फीचर्स:</b>\n"
        "🔹 /quiz — Telegram क्विज़ खेलें\n"
        "🔹 /ask &lt;सवाल&gt; — किसी भी विषय पर मुझसे पूछें\n"
        "🔹 <b>किसी भी मैसेज/क्विज़/फोटो पर Reply करें</b> और <code>explain</code> / <code>समझाओ</code> / <code>?</code> लिखें — बॉट बिना टैग जवाब देगा!\n"
        "🔹 <b>बॉट को टैग करें</b> — कहीं भी " + BOT_USERNAME + " लिखकर सवाल पूछें\n"
        "🔹 <b>फोटो डाउट सॉल्विंग</b> — किताब या प्रश्न की फोटो भेजें 📸\n"
        "🔹 /id — ग्रुप की ID देखने के लिए\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "🤖 <i>Powered by Gemini AI</i> ⚡"
    )
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['quiz'])
def send_quiz(message):
    if message.chat.type == "private" or not is_chat_allowed(message.chat.id):
        return

    q = random.choice(QUIZ_BANK)
    try:
        sent = bot.send_poll(
            chat_id=message.chat.id,
            question=q["question"],
            options=q["options"],
            type="quiz",
            correct_option_id=q["correct_option_id"],
            explanation=q.get("explanation", ""),
            is_anonymous=False
        )
        POLL_TRACKER[sent.message_id] = q
        logger.info(f"Quiz sent and tracked: msg_id={sent.message_id}")
    except Exception as e:
        logger.error(f"Quiz send error: {e}")
        bot.reply_to(message, "⚠️ क्विज़ भेजने में समस्या हुई। कृपया दोबारा प्रयास करें।")

@bot.message_handler(commands=['ask'])
def handle_ask(message):
    if message.chat.type == "private" or not is_chat_allowed(message.chat.id):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "कृपया सवाल भी लिखें। उदाहरण:\n<code>/ask प्रकाश वर्ष क्या है?</code>", parse_mode="HTML")
        return
    query = parts[1].strip()
    answer_text_query(message, query)

# सीधे फोटो भेजने पर हैंडल करना (Direct Photo)
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    if message.chat.type == "private" or not is_chat_allowed(message.chat.id):
        return

    caption = message.caption or ""

    is_bot_tagged = is_tagged(caption)
    is_reply_to_bot = (message.reply_to_message and 
                       message.reply_to_message.from_user and 
                       message.reply_to_message.from_user.id == BOT_ID)

    if not (is_bot_tagged or is_reply_to_bot or caption.lower().startswith("/ask")):
        return

    status = bot.reply_to(message, "🔍 <i>फोटो देखी जा रही है...</i>", parse_mode="HTML")
    try:
        bot.send_chat_action(message.chat.id, "typing")
        
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded = bot.download_file(file_info.file_path)
        base64_img = base64.b64encode(downloaded).decode("utf-8")

        prompt = caption.replace(BOT_USERNAME, "").replace("/ask", "").strip()
        if not prompt:
            prompt = "इस फोटो में दिए गए प्रश्न को पहचानें और छात्र के लिए 3-4 बुलेट पॉइंट्स में संक्षेप में हल समझाएं।"

        gemini_parts = [
            {"text": prompt},
            {
                "inlineData": {
                    "mimeType": "image/jpeg",
                    "data": base64_img
                }
            }
        ]

        answer = call_gemini(gemini_parts, SYSTEM_PROMPT)
        bot.delete_message(message.chat.id, status.message_id)
        safe_send_html(message.chat.id, answer, reply_to_message_id=message.message_id)
    except Exception as e:
        logger.error(f"Photo handling error: {e}")
        try:
            bot.edit_message_text(f"❌ समस्या आई: {e}", chat_id=message.chat.id, message_id=status.message_id)
        except:
            pass

# ⭐⭐⭐ MAIN TEXT HANDLER ⭐⭐⭐
@bot.message_handler(content_types=['text'])
def handle_text(message):
    if message.chat.type == "private" or not is_chat_allowed(message.chat.id):
        return

    text = message.text or ""
    text_lower = text.lower().strip()
    
    bot_tagged = is_tagged(text)
    has_reply = bool(message.reply_to_message)
    has_trigger = has_trigger_word(text)

    should_respond = False
    
    if bot_tagged:
        should_respond = True
        logger.info(f"Responding: bot tagged by {message.from_user.first_name}")

    elif has_reply:
        rep = message.reply_to_message
        
        if rep.from_user and rep.from_user.id == BOT_ID:
            should_respond = True
            logger.info("Responding: reply to bot's own message")
        
        elif rep.poll:
            should_respond = True
            logger.info("Responding: reply to poll (poll data available)")
        
        elif rep.message_id in POLL_TRACKER:
            should_respond = True
            logger.info(f"Responding: reply to tracked quiz poll (msg_id={rep.message_id})")
        
        elif rep.from_user and rep.from_user.is_bot and has_trigger:
            should_respond = True
            logger.info("Responding: reply to bot message with trigger")
        
        elif rep.photo and (has_trigger or bot_tagged):
            should_respond = True
            logger.info("Responding: reply to photo with trigger")
        
        elif has_trigger:
            should_respond = True
            logger.info("Responding: reply to any message with trigger word")

    if not should_respond:
        return
    
    clean_text = text.replace(BOT_USERNAME, "").replace(BOT_USERNAME.replace("@", ""), "").strip()
    if BOT_INFO:
        clean_text = clean_text.replace(f"@{BOT_INFO.username}", "").strip()
    
    rep = message.reply_to_message

    # ⭐ PHOTO REPLY
    if rep and rep.photo:
        status = bot.reply_to(message, "🔍 <i>फोटो देखी जा रही है...</i>", parse_mode="HTML")
        try:
            bot.send_chat_action(message.chat.id, "typing")
            file_info = bot.get_file(rep.photo[-1].file_id)
            downloaded = bot.download_file(file_info.file_path)
            base64_img = base64.b64encode(downloaded).decode("utf-8")

            user_query = clean_text if clean_text else "इस फोटो में दिए गए प्रश्न को पहचानें और छात्र के लिए 3-4 बुलेट पॉइंट्स में संक्षेप में हल समझाएं।"
            if rep.caption:
                user_query = f"फोटो कैप्शन: {rep.caption}\nछात्र का सवाल: {user_query}"

            gemini_parts = [
                {"text": user_query},
                {
                    "inlineData": {
                        "mimeType": "image/jpeg",
                        "data": base64_img
                    }
                }
            ]

            answer = call_gemini(gemini_parts, SYSTEM_PROMPT)
            bot.delete_message(message.chat.id, status.message_id)
            safe_send_html(message.chat.id, answer, reply_to_message_id=message.message_id)
        except Exception as e:
            logger.error(f"Replied photo error: {e}")
            try:
                bot.edit_message_text(f"❌ समस्या आई: {e}", chat_id=message.chat.id, message_id=status.message_id)
            except:
                pass
        return

    # ⭐ QUIZ POLL / TEXT REPLY
    quoted_content = ""
    if rep:
        if rep.poll:
            opts = ", ".join([opt.text for opt in rep.poll.options])
            correct_idx = getattr(rep.poll, 'correct_option_id', None)
            correct_ans = ""
            if correct_idx is not None and correct_idx < len(rep.poll.options):
                correct_ans = rep.poll.options[correct_idx].text
            quoted_content = f"क्विज़ पोल प्रश्न: {rep.poll.question}\nविकल्प: {opts}"
            if correct_ans:
                quoted_content += f"\nसही उत्तर: {correct_ans}"
            expl = getattr(rep.poll, 'explanation', None)
            if expl:
                quoted_content += f"\nव्याख्या: {expl}"
        
        elif rep.message_id in POLL_TRACKER:
            q_data = POLL_TRACKER[rep.message_id]
            opts = ", ".join(q_data["options"])
            correct_ans = q_data["options"][q_data["correct_option_id"]]
            quoted_content = f"क्विज़ पोल प्रश्न: {q_data['question']}\nविकल्प: {opts}\nसही उत्तर: {correct_ans}"
            if q_data.get("explanation"):
                quoted_content += f"\nव्याख्या: {q_data['explanation']}"
        
        elif rep.text:
            quoted_content = f"प्रश्न / मैसेज: {rep.text}"
        elif rep.caption:
            quoted_content = f"प्रश्न कैप्शन: {rep.caption}"

    if quoted_content:
        query = (
            f"छात्र ने ग्रुप में निम्नलिखित प्रश्न पर संक्षेप में उत्तर माँगा है:\n\n"
            f"{quoted_content}\n\n"
            f"छात्र का सवाल / अनुरोध: {clean_text if clean_text else 'कृपया सिर्फ सही उत्तर और 3-4 मुख्य बिंदु संक्षेप में बताएं।'}"
        )
    else:
        query = clean_text

    if query:
        answer_text_query(message, query)

def answer_text_query(message, query):
    try:
        bot.send_chat_action(message.chat.id, "typing")
        parts = [{"text": query}]
        answer = call_gemini(parts, SYSTEM_PROMPT)
        safe_send_html(message.chat.id, answer, reply_to_message_id=message.message_id)
    except Exception as e:
        logger.error(f"Text query error: {e}")
        bot.reply_to(message, f"❌ एरर: {e}")

# ================= 6. START BOT =================
if __name__ == "__main__":
    print("=" * 50)
    print(f"✅ Bot successfully running as: {BOT_USERNAME} (ID: {BOT_ID})")
    print(f"🔑 Loaded {len(GEMINI_KEYS_LIST)} Gemini API Key(s) from Environment Variables")
    print(f"🔒 Group Whitelisting Active: {ALLOWED_GROUP_IDS if ALLOWED_GROUP_IDS else 'All Groups Allowed'}")
    print(f"🌐 Health server running on port {PORT} for Render 24/7 hosting")
    print("=" * 50)
    bot.infinity_polling(timeout=20, long_polling_timeout=20)
