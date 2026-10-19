import os
import io
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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
PORT = int(os.environ.get("PORT", "8080"))

if not TELEGRAM_BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN environment variable not set!")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Super-fast Lite & Flash models in order of highest availability & zero 503 errors
GEMINI_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.7-flash"
]

# ================= 2. GEMINI REST API CLIENT =================
def call_gemini(parts, system_instruction=None):
    """Calls Gemini REST API directly with automatic model fallback & retry."""
    if not GEMINI_API_KEY:
        return "⚠️ Gemini API Key कॉन्फ़िगर नहीं है। कृपया Render पर GEMINI_API_KEY जोड़ें।"

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "maxOutputTokens": 1500,
            "temperature": 0.7
        }
    }

    if system_instruction:
        payload["systemInstruction"] = {
            "parts": [{"text": system_instruction}]
        }

    encoded_data = json.dumps(payload).encode("utf-8")

    last_error = ""
    for model_name in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_API_KEY}"
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
                    # Extract text from all parts, skipping thinking blocks
                    text_parts = [p.get("text", "") for p in parts_out if "text" in p and not p.get("thought", False)]
                    final_text = "".join(text_parts).strip()
                    if final_text:
                        return final_text
                return "माफ़ कीजिये, इस प्रश्न का उत्तर नहीं मिल पाया।"
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            logger.warning(f"Model {model_name} HTTP {e.code}: {err_body[:100]}")
            last_error = f"HTTP {e.code}"
            # If server busy (503), wait 1 sec and try next model
            if e.code == 503:
                time.sleep(1)
            continue
        except Exception as e:
            logger.warning(f"Error calling {model_name}: {e}")
            last_error = str(e)
            continue

    return f"❌ AI सर्वर व्यस्त है ({last_error}), कृपया 10 सेकंड बाद पुनः प्रयास करें।"

SYSTEM_PROMPT = (
    "You are an intelligent, friendly AI tutor for students in a Telegram study group. "
    "Provide clear, accurate, step-by-step explanations in simple Hindi / Hinglish. "
    "Always state the correct answer clearly, give the reason, and explain key points. "
    "Keep answers well-structured with bullet points and easy to read on mobile."
)

# ================= 3. OFFLINE QUIZ BANK (0 TOKENS) =================
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

# ================= 4. RENDER 24/7 WEB SERVER =================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Telegram AI Study Bot is alive and running on Render!")

    def log_message(self, format, *args):
        return

def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    logger.info(f"Health server listening on port {PORT}")
    server.serve_forever()

threading.Thread(target=run_health_server, daemon=True).start()

# ================= 5. TELEGRAM BOT HANDLERS =================

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    text = (
        "📚 *नमस्ते! मैं आपका AI Study & Quiz Bot हूँ।* 🎓\n\n"
        "✨ *मुख्य फीचर्स:*\n"
        "🔹 `/quiz` - Telegram क्विज़ खेलें (बिल्कुल फ्री, 0 टोकन)\n"
        "🔹 `/ask <सवाल>` - किसी भी विषय पर मुझसे पूछें\n"
        "🔹 *क्विज़ पर Reply करें* - किसी भी सवाल पर रिप्लाई करके `@Aantimpraharr_bot Explain me` लिखें\n"
        "🔹 *फोटो डाउट सॉल्विंग* - किताब या प्रश्न की फोटो भेजें, मैं हल समझाऊँगा\n"
        "🔹 `/help` - सहायता मेनू\n\n"
        "💡 *ग्रुप में इस्तेमाल:* सवाल पूछने के लिए मुझे मेंशन करें या रिप्लाई करें।"
    )
    bot.reply_to(message, text, parse_mode="Markdown")

@bot.message_handler(commands=['quiz'])
def send_quiz(message):
    q = random.choice(QUIZ_BANK)
    try:
        bot.send_poll(
            chat_id=message.chat.id,
            question=q["question"],
            options=q["options"],
            type="quiz",
            correct_option_id=q["correct_option_id"],
            explanation=q.get("explanation", ""),
            is_anonymous=False
        )
    except Exception as e:
        logger.error(f"Quiz send error: {e}")
        bot.reply_to(message, "⚠️ क्विज़ भेजने में समस्या हुई। कृपया दोबारा प्रयास करें।")

@bot.message_handler(commands=['ask'])
def handle_ask(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "कृपया सवाल भी लिखें। उदाहरण:\n`/ask प्रकाश वर्ष क्या है?`", parse_mode="Markdown")
        return
    query = parts[1].strip()
    answer_text_query(message, query)

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    is_private = (message.chat.type == "private")
    caption = message.caption or ""
    
    bot_username = ""
    try:
        bot_username = f"@{bot.get_me().username}"
    except Exception:
        pass

    is_tagged = bot_username and (bot_username.lower() in caption.lower())
    is_reply = (message.reply_to_message and message.reply_to_message.from_user.id == bot.get_me().id)

    # In groups, only respond if tagged, replied, or caption starts with /ask
    if not (is_private or is_tagged or is_reply or caption.startswith("/ask")):
        return

    status = bot.reply_to(message, "🔍 फोटो का विश्लेषण किया जा रहा है, कृपया प्रतीक्षा करें...")
    try:
        bot.send_chat_action(message.chat.id, "typing")
        
        # Download photo
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded = bot.download_file(file_info.file_path)
        base64_img = base64.b64encode(downloaded).decode("utf-8")

        prompt = caption.replace(bot_username, "").replace("/ask", "").strip()
        if not prompt:
            prompt = "इस फोटो में दिए गए प्रश्न को पहचानें और छात्र के लिए स्टेप-बाय-स्टेप सरल भाषा में इसका हल और व्याख्या समझाएं।"

        parts = [
            {"text": prompt},
            {
                "inlineData": {
                    "mimeType": "image/jpeg",
                    "data": base64_img
                }
            }
        ]

        answer = call_gemini(parts, SYSTEM_PROMPT)
        bot.delete_message(message.chat.id, status.message_id)
        bot.reply_to(message, answer)
    except Exception as e:
        logger.error(f"Photo handling error: {e}")
        bot.edit_message_text(f"❌ समस्या आई: {e}", chat_id=message.chat.id, message_id=status.message_id)

@bot.message_handler(content_types=['text'])
def handle_text(message):
    is_private = (message.chat.type == "private")
    
    bot_username = ""
    try:
        bot_username = f"@{bot.get_me().username}"
    except Exception:
        pass

    is_tagged = bot_username and (bot_username.lower() in message.text.lower())
    is_reply = bool(message.reply_to_message)

    if is_private or is_tagged or (is_reply and bot_username.lower() in message.text.lower()):
        clean_text = message.text.replace(bot_username, "").strip()

        # किसी भी मैसेज या दूसरे बॉट के क्विज़ पर रिप्लाई को हैंडल करना
        quoted_content = ""
        if message.reply_to_message:
            rep = message.reply_to_message
            if rep.poll:
                opts = ", ".join([opt.text for opt in rep.poll.options])
                quoted_content = f"क्विज़ पोल प्रश्न: {rep.poll.question}\nविकल्प: {opts}"
            elif rep.text:
                quoted_content = f"प्रश्न / मैसेज: {rep.text}"
            elif rep.caption:
                quoted_content = f"प्रश्न कैप्शन: {rep.caption}"

        if quoted_content:
            query = (
                f"छात्र ने ग्रुप में निम्नलिखित प्रश्न पर रिप्लाई करके डाउट पूछा है:\n\n"
                f"{quoted_content}\n\n"
                f"छात्र का सवाल / अनुरोध: {clean_text if clean_text else 'कृपया इस प्रश्न का सही उत्तर, कारण और पूरी व्याख्या आसान हिंदी में समझाएं।'}"
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
        bot.reply_to(message, answer)
    except Exception as e:
        logger.error(f"Text query error: {e}")
        bot.reply_to(message, f"❌ एरर: {e}")

# ================= 6. START BOT =================
if __name__ == "__main__":
    me = bot.get_me()
    print("=" * 50)
    print(f"✅ Bot successfully running as: @{me.username} (Name: {me.first_name})")
    print(f"🌐 Health server running on port {PORT} for Render 24/7 hosting")
    print("=" * 50)
    bot.infinity_polling(timeout=20, long_polling_timeout=20)
