import os
import io
import re
import json
import time
import base64
import random
import logging
import threading
from datetime import datetime
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

# Multi-Key Rotation: Render Environment Variables
RAW_KEYS = os.environ.get("GEMINI_API_KEYS", "") or os.environ.get("GEMINI_API_KEY", "")
GEMINI_KEYS_LIST = [k.strip() for k in RAW_KEYS.split(",") if k.strip()]

current_key_index = 0

# Allowed Group IDs
ALLOWED_GROUP_IDS_STR = os.environ.get("ALLOWED_GROUP_IDS", "").strip()
ALLOWED_GROUP_IDS = [gid.strip() for gid in ALLOWED_GROUP_IDS_STR.split(",") if gid.strip()]

if not TELEGRAM_BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN environment variable not set!")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# ⭐ Bot info
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

# High Rate-Limit Free Models
GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-3.5-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash"
]

def get_system_prompt():
    today_str = datetime.now().strftime("%d %B %Y")
    return (
        f"You are an elite, highly knowledgeable AI Study Tutor and Exam Specialist for Indian competitive exams (Railway RRB, SSC, State PSC, Police, Banking, Defence, UPSC).\n"
        f"Current Date: {today_str}.\n\n"
        "CORE STUDY & EXAM DIRECTIVES:\n"
        "- Give 100% accurate, authentic, and conceptual answers based on standard reference material (NCERT, authentic history/science/polity/maths facts).\n"
        "- If a student asks about vacancies (e.g., RRB NTPC, SSC, Railway) with an approximate year or typo (e.g. 'NTPC 2026'), understand the intent and relate to the active recruitment notifications (e.g. RRB NTPC CEN 05/2024 Graduate & CEN 06/2024 Undergraduate 11,558+ posts).\n\n"
        "TELEGRAM HTML DESIGN RULES:\n"
        "- Wrap the entire response in <blockquote>...</blockquote>.\n"
        "- Header: 🎯 <b>STUDY CAPSULE | अंतिम प्रहार™</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "- First line: ✅ <b>प्रामाणिक उत्तर / हल:</b> [Direct crisp answer in bold]\n"
        "- Next: 📌 <b>मुख्य बिंदु व स्पष्टीकरण:</b>\n"
        "- Use 3-4 bullet points starting with 🔹. Bold key keywords using <b>...</b>.\n"
        "- Use <i>italic</i> for English technical terms/dates.\n"
        "- End with: ━━━━━━━━━━━━━━━━━━━\n💡 <b>याद रखने योग्य ट्रिक / टिप:</b> <i>[One-line powerful revision tip]</i>\n\n"
        "- Keep it SHORT, CRISP, TO-THE-POINT.\n"
        "- ONLY use valid Telegram HTML: <blockquote>, <b>, <i>, <code>. Do NOT use markdown (** or __). Do NOT use <br> tags."
    )

AUTO_TRIGGERS = [
    "explain", "samjhao", "batao", "solution", "solve", "kaise", 
    "why", "answer", "uttar", "हल", "समझाओ", "बताओ", "स्पष्ट", "details", "?",
    "kya", "kyu", "kyun", "kyon", "kon", "kaun", "kab", "kaha",
    "bta", "btao", "smjhao", "smjha", "hint", "help", "vacancy", "form", "date",
    "क्यों", "क्या", "कौन", "कब", "कैसे", "कहाँ", "सही", "गलत", "वैकेंसी", "भर्ती"
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
    if not BOT_USERNAME or not text:
        return False
    return BOT_USERNAME in text.lower()

def has_trigger_word(text):
    if not text:
        return False
    text_lower = text.lower().strip()
    for trigger in AUTO_TRIGGERS:
        if trigger in text_lower:
            return True
    return False

# ⭐⭐⭐ HTML SANITIZER & CONVERTER ⭐⭐⭐
def md_to_html(text):
    if not text:
        return text
    
    has_blockquote = "<blockquote>" in text.lower()
    
    protected = []
    def protect_tag(match):
        protected.append(match.group(0))
        return f"__PROTECTED_{len(protected)-1}__"
    
    # Protect valid HTML tags
    text = re.sub(r'</?(?:b|i|code|blockquote)>', protect_tag, text, flags=re.IGNORECASE)
    
    # Escape special chars
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    
    for idx, tag in enumerate(protected):
        text = text.replace(f"__PROTECTED_{idx}__", tag)
    
    # Clean broken markdown lines
    text = re.sub(r'[_]{3,}', '━━━━━━━━━━━━━━━━━━━', text)
    text = re.sub(r'[-]{3,}', '━━━━━━━━━━━━━━━━━━━', text)
    
    # Convert markdown tags
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', text)
    text = re.sub(r'`(.+?)`', r'<b>\1</b>', text)
    text = re.sub(r'^#{1,3}\s*(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'^[\-]\s+', '🔹 ', text, flags=re.MULTILINE)
    text = re.sub(r'^\*\s+', '🔹 ', text, flags=re.MULTILINE)

    if not has_blockquote:
        text = f"<blockquote>{text.strip()}</blockquote>"
    
    return text.strip()

def safe_send_html(chat_id, text, reply_to_message_id=None):
    html_text = md_to_html(text)
    try:
        if reply_to_message_id:
            bot.send_message(chat_id, html_text, parse_mode="HTML", reply_to_message_id=reply_to_message_id)
        else:
            bot.send_message(chat_id, html_text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"HTML send failed with reply ({e}), trying fallback")
        try:
            bot.send_message(chat_id, html_text, parse_mode="HTML")
        except Exception as e2:
            plain = re.sub(r'<[^>]+>', '', html_text)
            try:
                bot.send_message(chat_id, plain)
            except Exception as e3:
                logger.error(f"Final send failed: {e3}")

# ================= 2. GEMINI REST API (FAIL-SAFE 429 RECOVERY) =================
def call_gemini(parts, system_instruction=None):
    total_keys = max(1, len(GEMINI_KEYS_LIST))
    last_error = ""

    for _ in range(total_keys):
        active_key = get_current_api_key()
        if not active_key:
            return "⚠️ कोई Gemini API Key उपलब्ध नहीं है।"

        key_quota_exhausted = False

        for model_name in GEMINI_MODELS:
            payload = {
                "contents": [{"parts": parts}],
                "generationConfig": {
                    "maxOutputTokens": 600,
                    "temperature": 0.3
                }
            }
            if system_instruction:
                payload["systemInstruction"] = {
                    "parts": [{"text": system_instruction}]
                }

            encoded_data = json.dumps(payload).encode("utf-8")
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
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="ignore")
                logger.warning(f"Key #{current_key_index + 1} | Model {model_name} HTTP {e.code}")
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

        if key_quota_exhausted and len(GEMINI_KEYS_LIST) > 1:
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

# Quiz Poll Tracking
POLL_TRACKER = {}

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
        "<blockquote>"
        "📚 <b>अंतिम प्रहार™ AI Study Assistant</b> 🎓\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "✨ <b>कमांड्स और फीचर्स:</b>\n"
        "🔹 /quiz — महत्वपूर्ण क्विज़ हल करें\n"
        "🔹 /ask &lt;सवाल&gt; — कोई भी डाउट या वैकेंसी अपडेट पूछें\n"
        "🔹 <b>Reply Feature:</b> किसी भी प्रश्न, फोटो या क्विज़ पर स्वाइप करके सवाल पूछें!\n"
        "🔹 <b>Photo Solver:</b> सवाल की फोटो भेजें, बॉट तुरंत हल देगा 📸\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "⚡ <i>24/7 Active AI Study Partner</i>"
        "</blockquote>"
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
        bot.reply_to(message, "कृपया सवाल भी लिखें। उदाहरण:\n<code>/ask RRB NTPC vacancy update</code>", parse_mode="HTML")
        return
    query = parts[1].strip()
    answer_text_query(message, query)

# सीधे फोटो भेजने पर (Direct Photo)
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

    status = bot.reply_to(message, "🔍 <i>फोटो व तथ्यों का विश्लेषण किया जा रहा है...</i>", parse_mode="HTML")
    try:
        bot.send_chat_action(message.chat.id, "typing")
        
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded = bot.download_file(file_info.file_path)
        base64_img = base64.b64encode(downloaded).decode("utf-8")

        prompt = caption.replace(BOT_USERNAME, "").replace("/ask", "").strip()
        if not prompt:
            prompt = "इस फोटो में दिए गए प्रश्न/सूचना की गहन जांच करें, सही उत्तर सत्यापित करें और 3-4 बुलेट पॉइंट्स में स्पष्टीकरण दें।"

        gemini_parts = [
            {"text": prompt},
            {
                "inlineData": {
                    "mimeType": "image/jpeg",
                    "data": base64_img
                }
            }
        ]

        answer = call_gemini(gemini_parts, get_system_prompt())
        try:
            bot.delete_message(message.chat.id, status.message_id)
        except Exception:
            pass
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
    elif has_reply:
        should_respond = True

    if not should_respond:
        return
    
    clean_text = text.replace(BOT_USERNAME, "").replace(BOT_USERNAME.replace("@", ""), "").strip()
    if BOT_INFO:
        clean_text = clean_text.replace(f"@{BOT_INFO.username}", "").strip()
    
    rep = message.reply_to_message

    # ⭐ CASE 1: PHOTO REPLY
    if rep and rep.photo:
        status = bot.reply_to(message, "🔍 <i>फोटो व तथ्यों का विश्लेषण किया जा रहा है...</i>", parse_mode="HTML")
        try:
            bot.send_chat_action(message.chat.id, "typing")
            file_info = bot.get_file(rep.photo[-1].file_id)
            downloaded = bot.download_file(file_info.file_path)
            base64_img = base64.b64encode(downloaded).decode("utf-8")

            user_query = clean_text if clean_text else "इस फोटो में दिए गए प्रश्न/सूचना की गहन जांच करें, सही उत्तर सत्यापित करें और 3-4 बुलेट पॉइंट्स में स्पष्टीकरण दें।"
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

            answer = call_gemini(gemini_parts, get_system_prompt())
            try:
                bot.delete_message(message.chat.id, status.message_id)
            except Exception:
                pass
            safe_send_html(message.chat.id, answer, reply_to_message_id=message.message_id)
        except Exception as e:
            logger.error(f"Replied photo error: {e}")
            try:
                bot.edit_message_text(f"❌ समस्या आई: {e}", chat_id=message.chat.id, message_id=status.message_id)
            except:
                pass
        return

    # ⭐ CASE 2: QUIZ POLL / TEXT REPLY
    quoted_content = ""
    if rep:
        if rep.poll:
            opts = ", ".join([opt.text for opt in rep.poll.options])
            quoted_content = f"क्विज़ पोल प्रश्न: {rep.poll.question}\nदिए गए विकल्प: {opts}"
        elif rep.message_id in POLL_TRACKER:
            q_data = POLL_TRACKER[rep.message_id]
            opts = ", ".join(q_data["options"])
            quoted_content = f"क्विज़ पोल प्रश्न: {q_data['question']}\nदिए गए विकल्प: {opts}"
        elif rep.text:
            quoted_content = f"प्रश्न / मैसेज: {rep.text}"
        elif rep.caption:
            quoted_content = f"प्रश्न कैप्शन: {rep.caption}"

    # ⭐ AGAR TELEGRAM NE REPLY CONTENT BLANK DIYA HAI
    if rep and not quoted_content:
        simple_triggers = ["explain", "samjhao", "batao", "solution", "solve", "?", "हल", "समझाओ", "बताओ"]
        is_only_trigger = clean_text.lower().strip() in simple_triggers or len(clean_text) <= 2
        
        if is_only_trigger:
            guide_msg = (
                "<blockquote>"
                "ℹ️ <b>क्विज़ डाउट गाइड:</b>\n"
                "━━━━━━━━━━━━━━━━━━━\n"
                "Telegram इस पोल का प्रश्न फॉरवर्ड नहीं कर रहा है।\n\n"
                "💡 <b>समाधान:</b>\n"
                "1. स्वाइप करके प्रश्न का 1-2 मुख्य शब्द लिखें:\n"
                "   👉 <i>उदा: explain राइबोसोम?</i>\n"
                "2. या क्विज़ का <b>स्क्रीनशॉट / फोटो</b> भेजें!"
                "</blockquote>"
            )
            bot.reply_to(message, guide_msg, parse_mode="HTML")
            return
        else:
            query = clean_text
    elif quoted_content:
        query = (
            f"छात्र ने निम्नलिखित प्रश्न/विषय पर सहायता मांगी है। कृपया सटीक व प्रामाणिक जानकारी दें:\n\n"
            f"{quoted_content}\n\n"
            f"छात्र का विशेष अनुरोध: {clean_text if clean_text else 'कृपया प्रामाणिक सही उत्तर और मुख्य बिंदु बताएं।'}"
        )
    else:
        query = clean_text

    if query:
        answer_text_query(message, query)

def answer_text_query(message, query):
    try:
        bot.send_chat_action(message.chat.id, "typing")
        parts = [{"text": query}]
        answer = call_gemini(parts, get_system_prompt())
        safe_send_html(message.chat.id, answer, reply_to_message_id=message.message_id)
    except Exception as e:
        logger.error(f"Text query error: {e}")
        try:
            bot.reply_to(message, f"❌ एरर: {e}")
        except:
            pass

# ================= 6. START BOT =================
if __name__ == "__main__":
    print("=" * 50)
    print(f"✅ Bot successfully running as: {BOT_USERNAME} (ID: {BOT_ID})")
    print(f"🔑 Loaded {len(GEMINI_KEYS_LIST)} Gemini API Key(s) from Environment Variables")
    print(f"🔒 Group Whitelisting Active: {ALLOWED_GROUP_IDS if ALLOWED_GROUP_IDS else 'All Groups Allowed'}")
    print(f"🌐 Health server running on port {PORT} for Render 24/7 hosting")
    print("=" * 50)
    bot.infinity_polling(timeout=20, long_polling_timeout=20)
