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
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
PORT = int(os.environ.get("PORT", "8080"))

# Multi-Key Rotation: Render Environment Variables
RAW_KEYS = os.environ.get("GEMINI_API_KEYS", "") or os.environ.get("GEMINI_API_KEY", "")
GEMINI_KEYS_LIST = [k.strip() for k in RAW_KEYS.split(",") if k.strip()]
current_key_index = 0

# Allowed Group IDs (खाली होने पर सभी ग्रुप्स में चलेगा)
ALLOWED_GROUP_IDS_STR = os.environ.get("ALLOWED_GROUP_IDS", "").strip()
ALLOWED_GROUP_IDS = [gid.strip() for gid in ALLOWED_GROUP_IDS_STR.split(",") if gid.strip()]

if not TELEGRAM_BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN environment variable not set!")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode=None)

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
    logger.warning(f"Failed to get bot info at startup: {e}")

# ⭐ OFFICIAL REAL GEMINI MODELS (Fast & Stable)
GEMINI_MODELS = [
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-pro"
]

# ⭐ GLOBAL QUIZ CACHE
GLOBAL_QUIZ_CACHE = {}

def get_system_prompt():
    today_str = datetime.now().strftime("%d %B %Y")
    return (
        f"You are an elite, highly knowledgeable AI Study Tutor and Exam Specialist for Indian competitive exams (Railway RRB, SSC, State PSC, Police, Banking, Defence, UPSC).\n"
        f"Current Date: {today_str}.\n\n"
        "CORE STUDY & EXAM DIRECTIVES:\n"
        "- When explaining a quiz question, clearly identify the correct option and explain WHY it is correct in simple, interesting Hindi / Hinglish.\n"
        "- Break down complex terms into student-friendly language.\n\n"
        "TELEGRAM HTML DESIGN RULES:\n"
        "- Wrap the entire response inside <blockquote>...</blockquote>.\n"
        "- Header: 🎯 <b>STUDY CAPSULE | अंतिम प्रहार™</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "- First line: ✅ <b>प्रामाणिक उत्तर:</b> [State the direct correct option clearly in bold]\n"
        "- Next: 📝 <b>सरल भाषा में व्याख्या:</b>\n"
        "- Use 3-4 bullet points starting with 🔹. Bold key keywords using <b>...</b>.\n"
        "- Use <i>italic</i> for English technical terms/dates.\n"
        "- End with: ━━━━━━━━━━━━━━━━━━━\n💡 <b>याद रखने योग्य ट्रिक / मुख्य बिंदु:</b> <i>[One-line powerful revision tip]</i>\n\n"
        "- Keep it CRISP, IMPACTFUL, NO unnecessary filler sentences.\n"
        "- ONLY use valid Telegram HTML: <blockquote>, <b>, <i>, <code>. Do NOT use markdown (** or __). Ensure all tags are properly opened and closed."
    )

AUTO_TRIGGERS = [
    "explain", "samjhao", "batao", "solution", "solve", "kaise",
    "why", "answer", "uttar", "हल", "समझाओ", "बताओ", "स्पष्ट", "details", "?",
    "kya", "kyu", "kyun", "kyon", "kon", "kaun", "kab", "kaha",
    "bta", "btao", "smjhao", "smjha", "hint", "help", "vacancy", "form", "date",
    "samjh", "samajh", "bataiye", "sahi", "galat",
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

# ⭐⭐⭐ SAFE HTML SANITIZER & AUTO-CLOSER ⭐⭐⭐
def md_to_html(text):
    if not text:
        return ""

    has_blockquote = "<blockquote>" in text.lower()
    protected = []

    def protect_tag(match):
        protected.append(match.group(0))
        return f"___SAFE_TAG_{len(protected)-1}___"

    # Protect allowed HTML tags
    text = re.sub(r'</?(?:b|i|code|blockquote)>', protect_tag, text, flags=re.IGNORECASE)

    # Escape HTML special chars
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Restore allowed tags
    for idx, tag in enumerate(protected):
        text = text.replace(f"___SAFE_TAG_{idx}___", tag)

    # Dividers
    text = re.sub(r'[_]{3,}', '━━━━━━━━━━━━━━━━━━━', text)
    text = re.sub(r'[-]{3,}', '━━━━━━━━━━━━━━━━━━━', text)

    # Convert Markdown to HTML
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', text)
    text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
    text = re.sub(r'^#{1,3}\s*(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'^[\-\*]\s+', '🔹 ', text, flags=re.MULTILINE)

    if not has_blockquote:
        text = f"<blockquote>{text.strip()}</blockquote>"

    return text.strip()

def safe_send_html(chat_id, text, reply_to_message_id=None):
    html_text = md_to_html(text)

    # ⭐ ऑटो-क्लोज़ टैग्स ताकि Telegram Parse Error न आए
    for tag in ['b', 'i', 'code', 'blockquote']:
        open_tags = len(re.findall(f'<{tag}>', html_text, re.IGNORECASE))
        close_tags = len(re.findall(f'</{tag}>', html_text, re.IGNORECASE))
        if open_tags > close_tags:
            html_text += f"</{tag}>" * (open_tags - close_tags)

    if len(html_text) > 4000:
        html_text = html_text[:3980] + "...\n</blockquote>"

    try:
        if reply_to_message_id:
            bot.send_message(chat_id, html_text, parse_mode="HTML", reply_to_message_id=reply_to_message_id)
        else:
            bot.send_message(chat_id, html_text, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"HTML send failed: {e}. Falling back to plain text.")
        clean_plain = re.sub(r'<[^>]+>', '', html_text)
        try:
            bot.send_message(chat_id, clean_plain, reply_to_message_id=reply_to_message_id)
        except Exception as e2:
            logger.error(f"Plain text fallback failed: {e2}")

# ================= 2. GEMINI REST API =================
def call_gemini(parts, system_instruction=None):
    total_keys = max(1, len(GEMINI_KEYS_LIST))

    # ⭐ 2500 Tokens ताकि पूरा उत्तर विस्तार से आए और कभी न कटे
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "maxOutputTokens": 2500,
            "temperature": 0.3
        }
    }
    if system_instruction:
        payload["systemInstruction"] = {
            "parts": [{"text": system_instruction}]
        }
    encoded_data = json.dumps(payload).encode("utf-8")

    detailed_errors = []

    for key_attempt in range(total_keys):
        active_key = get_current_api_key()
        if not active_key:
            return "⚠️ Render में कोई Gemini API Key नहीं मिली। कृपया GEMINI_API_KEY सेट करें।"

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
                    detailed_errors.append(f"{model_name}: Empty candidates")
            except urllib.error.HTTPError as e:
                err_body = ""
                try:
                    err_body = e.read().decode("utf-8", errors="replace")[:150]
                except Exception:
                    pass
                detailed_errors.append(f"{model_name}: HTTP {e.code}")
                logger.warning(f"Key #{current_key_index + 1} | {model_name} HTTP {e.code}: {err_body}")
                if e.code == 429:
                    time.sleep(1)
                continue
            except Exception as e:
                detailed_errors.append(f"{model_name}: {str(e)[:40]}")
                continue

        if len(GEMINI_KEYS_LIST) > 1:
            rotate_to_next_key()

    err_summary = " | ".join(detailed_errors[-2:]) if detailed_errors else "Unknown"
    return (
        "<blockquote>"
        "💡 <b>उत्तर तैयार करने में तकनीकी समस्या आई!</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>{err_summary}</i>\n\n"
        "कृपया 5 सेकंड बाद पुनः स्वाइप (Reply) करके <code>explain</code> लिखें।"
        "</blockquote>"
    )

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
        "question": "भारतीय संविधान का जनक किसे कहा जाता है?",
        "options": ["महात्मा गांधी", "डॉ. बी. आर. अम्बेडकर", "जवाहरलाल नेहरू", "डॉ. राजेंद्र प्रसाद"],
        "correct_option_id": 1,
        "explanation": "डॉ. भीमराव अम्बेडकर को भारतीय संविधान का जनक माना जाता है।"
    }
]

QUIZ_BANK = DEFAULT_QUIZZES

# ================= 4. RENDER 24/7 WEB SERVER =================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        status_msg = (
            f"Bot: {BOT_USERNAME} (Running 24/7)\n"
            f"Keys: {len(GEMINI_KEYS_LIST)}\n"
            f"Cached Polls: {len(GLOBAL_QUIZ_CACHE)}\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self.wfile.write(status_msg.encode("utf-8"))

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        self.do_GET()

    def log_message(self, format, *args):
        return

def run_health_server():
    try:
        server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
        logger.info(f"Health server listening on port {PORT}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"Health server error: {e}")

threading.Thread(target=run_health_server, daemon=True).start()

# ================= 5. TELEGRAM BOT HANDLERS =================

# ⭐ LIVE POLL LISTENER (MAHIQUIZBOT & ANY POLL AUTO-CAPTURE)
@bot.message_handler(content_types=['poll'])
def auto_capture_group_polls(message):
    try:
        p = message.poll
        opts = [opt.text for opt in p.options]
        correct_opt = ""
        if hasattr(p, 'correct_option_id') and p.correct_option_id is not None:
            if p.correct_option_id < len(opts):
                correct_opt = opts[p.correct_option_id]

        GLOBAL_QUIZ_CACHE[message.message_id] = {
            "question": p.question,
            "options": opts,
            "correct": correct_opt,
            "explanation": getattr(p, 'explanation', ''),
            "from_user": message.from_user.first_name if message.from_user else "QuizBot"
        }
        logger.info(f"Captured Poll #{message.message_id}: {p.question[:40]}...")

        # Keep cache trimmed to last 200 polls
        if len(GLOBAL_QUIZ_CACHE) > 200:
            for k in sorted(GLOBAL_QUIZ_CACHE.keys())[:50]:
                del GLOBAL_QUIZ_CACHE[k]
    except Exception as e:
        logger.error(f"Error capturing poll: {e}")

@bot.message_handler(commands=['id', 'groupid'])
def send_group_id(message):
    bot.reply_to(message, f"📌 चैट ID: <code>{message.chat.id}</code>", parse_mode="HTML")

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    if not is_chat_allowed(message.chat.id):
        return

    text = (
        "<blockquote>"
        "📚 <b>अंतिम प्रहार™ AI Study Assistant</b> 🎓\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "✨ <b>इस्तेमाल करने का तरीका:</b>\n"
        "🔹 <b>क्विज़ हल:</b> MAHIQUIZBOT या किसी भी क्विज़ पर स्वाइप (Reply) करके लिखें: <code>explain in hindi</code>\n"
        "🔹 <b>डाउट पूछें:</b> <code>/ask प्रकाश संश्लेषण क्या है?</code> या मुझे टैग करें\n"
        "🔹 <b>फोटो सवाल:</b> प्रश्न की फोटो भेजें, बॉट तुरंत हल देगा 📸\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "⚡ <i>24/7 Active Study Partner</i>"
        "</blockquote>"
    )
    safe_send_html(message.chat.id, text, reply_to_message_id=message.message_id)

@bot.message_handler(commands=['quiz'])
def send_quiz(message):
    if not is_chat_allowed(message.chat.id):
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
        GLOBAL_QUIZ_CACHE[sent.message_id] = {
            "question": q["question"],
            "options": q["options"],
            "correct": q["options"][q["correct_option_id"]],
            "explanation": q.get("explanation", ""),
            "from_user": BOT_USERNAME
        }
    except Exception as e:
        logger.error(f"Quiz send error: {e}")

@bot.message_handler(commands=['ask'])
def handle_ask(message):
    if not is_chat_allowed(message.chat.id):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        safe_send_html(message.chat.id, "कृपया सवाल भी लिखें। उदाहरण:\n<code>/ask प्रकाश वर्ष क्या है?</code>", reply_to_message_id=message.message_id)
        return
    answer_text_query(message, parts[1].strip())

# सीधे फोटो भेजने पर
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    if not is_chat_allowed(message.chat.id):
        return

    caption = message.caption or ""
    is_bot_tagged = is_tagged(caption)
    is_reply_to_bot = (message.reply_to_message and
                       message.reply_to_message.from_user and
                       message.reply_to_message.from_user.id == BOT_ID)

    if not (is_bot_tagged or is_reply_to_bot or caption.lower().startswith("/ask") or message.chat.type == "private"):
        return

    try:
        bot.send_chat_action(message.chat.id, "typing")
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded = bot.download_file(file_info.file_path)
        base64_img = base64.b64encode(downloaded).decode("utf-8")

        prompt = caption.replace(BOT_USERNAME, "").replace("/ask", "").strip()
        if not prompt:
            prompt = "इस फोटो में दिए गए प्रश्न की गहन जांच करें, सही उत्तर पहचानें और 3-4 बुलेट पॉइंट्स में सरल हिंदी में समझाएं।"

        gemini_parts = [
            {"text": prompt},
            {"inlineData": {"mimeType": "image/jpeg", "data": base64_img}}
        ]

        answer = call_gemini(gemini_parts, get_system_prompt())
        safe_send_html(message.chat.id, answer, reply_to_message_id=message.message_id)
    except Exception as e:
        logger.error(f"Photo handling error: {e}")

# ⭐⭐⭐ MAIN TEXT HANDLER: REPLIES & QUESTIONS ⭐⭐⭐
@bot.message_handler(content_types=['text'])
def handle_text(message):
    if not is_chat_allowed(message.chat.id):
        return

    text = message.text or ""
    bot_tagged = is_tagged(text)
    has_reply = bool(message.reply_to_message)
    has_trigger = has_trigger_word(text)
    is_private = message.chat.type == "private"

    should_respond = False
    if is_private or bot_tagged or (has_reply and has_trigger):
        should_respond = True
    elif has_reply and message.reply_to_message.from_user and message.reply_to_message.from_user.id == BOT_ID:
        should_respond = True

    if not should_respond:
        return

    clean_text = text
    if BOT_USERNAME:
        clean_text = re.sub(re.escape(BOT_USERNAME), "", clean_text, flags=re.IGNORECASE).strip()
    if BOT_INFO and BOT_INFO.username:
        clean_text = re.sub(re.escape(f"@{BOT_INFO.username}"), "", clean_text, flags=re.IGNORECASE).strip()

    rep = message.reply_to_message

    # 1. PHOTO REPLY
    if rep and rep.photo:
        try:
            bot.send_chat_action(message.chat.id, "typing")
            file_info = bot.get_file(rep.photo[-1].file_id)
            downloaded = bot.download_file(file_info.file_path)
            base64_img = base64.b64encode(downloaded).decode("utf-8")

            user_query = clean_text if clean_text else "इस फोटो में दिए गए प्रश्न का सही उत्तर और स्पष्टीकरण दें।"
            if rep.caption:
                user_query = f"फोटो कैप्शन: {rep.caption}\nछात्र का सवाल: {user_query}"

            gemini_parts = [
                {"text": user_query},
                {"inlineData": {"mimeType": "image/jpeg", "data": base64_img}}
            ]
            answer = call_gemini(gemini_parts, get_system_prompt())
            safe_send_html(message.chat.id, answer, reply_to_message_id=message.message_id)
        except Exception as e:
            logger.error(f"Replied photo error: {e}")
        return

    # 2. POLL / QUIZ REPLY
    quoted_content = ""
    if rep:
        rep_id = rep.message_id
        # Cache check
        if rep_id in GLOBAL_QUIZ_CACHE:
            q_data = GLOBAL_QUIZ_CACHE[rep_id]
            opts_str = ", ".join(q_data["options"])
            quoted_content = f"क्विज़ पोल प्रश्न: {q_data['question']}\nविकल्प: {opts_str}"
            if q_data.get("correct"):
                quoted_content += f"\nमार्क किया गया उत्तर: {q_data['correct']}"
            if q_data.get("explanation"):
                quoted_content += f"\nनोट्स/संकेत: {q_data['explanation']}"
        # Telegram direct poll check
        elif rep.poll:
            opts = [opt.text for opt in rep.poll.options]
            quoted_content = f"क्विज़ पोल प्रश्न: {rep.poll.question}\nविकल्प: {', '.join(opts)}"
            if hasattr(rep.poll, 'correct_option_id') and rep.poll.correct_option_id is not None:
                if rep.poll.correct_option_id < len(opts):
                    quoted_content += f"\nसही विकल्प: {opts[rep.poll.correct_option_id]}"
            if getattr(rep.poll, 'explanation', None):
                quoted_content += f"\nसंकेत: {rep.poll.explanation}"
        # Standard text message reply
        elif rep.text:
            quoted_content = f"संदर्भ प्रश्न/मैसेज: {rep.text}"
        elif rep.caption:
            quoted_content = f"संदर्भ कैप्शन: {rep.caption}"

    generic_words = [
        "explain", "explain in hindi", "samjhao", "batao", "solution", "solve", "?",
        "हल", "समझाओ", "बताओ", "please explain", "bata do", "samjha do"
    ]
    is_empty_or_generic = clean_text.lower().strip() in generic_words or len(clean_text) <= 2

    if not rep and is_empty_or_generic:
        guide_msg = (
            "<blockquote>"
            "👋 <b>नमस्ते! आपने कोई सवाल नहीं लिखा।</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "💡 <b>मुझसे पूछने के 2 आसान तरीके:</b>\n\n"
            "1. किसी भी क्विज़ पर <b>स्वाइप (Reply)</b> करके लिखें: <code>explain</code>\n"
            f"2. या सवाल लिखकर मुझे टैग करें: <i>{BOT_USERNAME} पानीपत का युद्ध कब हुआ?</i>"
            "</blockquote>"
        )
        safe_send_html(message.chat.id, guide_msg, reply_to_message_id=message.message_id)
        return

    if rep and not quoted_content:
        guide_msg = (
            "<blockquote>"
            "ℹ️ <b>क्विज़ डाउट समाधान:</b>\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            "इस पोल का टेक्स्ट Telegram प्राइवेसी के कारण लोड नहीं हुआ।\n\n"
            "💡 <b>तुरंत उत्तर पाने के लिए:</b>\n"
            "प्रश्न का 1-2 मुख्य शब्द साथ में लिखें (उदा: <code>explain पार्किंसंस</code>) या <b>स्क्रीनशॉट (फोटो)</b> भेजें! 📸"
            "</blockquote>"
        )
        safe_send_html(message.chat.id, guide_msg, reply_to_message_id=message.message_id)
        return

    if quoted_content:
        user_demand = clean_text if clean_text else "कृपया इस प्रश्न का सही उत्तर पहचानें और बहुत ही सरल, आसान हिंदी में विस्तार से समझाएं।"
        query = (
            f"छात्र ने निम्नलिखित क्विज़/प्रश्न पर सहायता मांगी है:\n\n"
            f"{quoted_content}\n\n"
            f"छात्र का अनुरोध: {user_demand}\n\n"
            f"निर्देश: छात्र के लिए बिल्कुल आसान और रोचक हिंदी में समझाइए ताकि एक बार में कॉन्सेप्ट समझ आ जाए।"
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

# ================= 6. START BOT (CRASH-PROOF POLLING) =================
if __name__ == "__main__":
    print("=" * 50)
    print(f"✅ Bot successfully running as: {BOT_USERNAME} (ID: {BOT_ID})")
    print(f"🔑 Loaded {len(GEMINI_KEYS_LIST)} Gemini API Key(s)")
    print(f"🧠 Active Models: {', '.join(GEMINI_MODELS)}")
    print(f"🌐 Health server running on port {PORT}")
    print("=" * 50)

    # Webhook को हटाना ताकि Polling टकराव न करे
    try:
        bot.delete_webhook(drop_pending_updates=True)
        time.sleep(1)
    except Exception as e:
        logger.warning(f"delete_webhook error: {e}")

    # Polling में सभी ज़रूरी updates को प्राप्त करना
    bot.infinity_polling(
        timeout=30,
        long_polling_timeout=30,
        skip_pending=True,
        allowed_updates=['message', 'edited_message', 'poll', 'poll_answer']
    )
