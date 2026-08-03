# -*- coding: utf-8 -*-
"""
fatwa_api_server.py
--------------------
خادم خلفي بسيط بلغة Python/Flask يعمل كوسيط آمن بين صفحة الموقع (index.html)
وواجهة OpenRouter (بدل Anthropic مباشرة). الغرض منه إبقاء مفتاح API سرًا على
الخادم بدلًا من كشفه داخل كود المتصفح.

** تحديث: تم تحويل الخادم للعمل عبر OpenRouter بدل Anthropic **
OpenRouter بيدّيك وصول لموديلات كتير (من ضمنها موديلات مجانية) عبر واجهة
واحدة متوافقة مع صيغة OpenAI، فمش محتاج مكتبة anthropic ولا مفتاح Anthropic
خالص. تقدر تختار أي موديل من https://openrouter.ai/models (فلتر Free
للموديلات المجانية).

يوفر نقطتي وصول (endpoints):
  POST /api/fatwa    -> يُستخدم من قسم "الفتاوى" (بحث ذكي فوري)
  POST /api/murshid  -> يُستخدم من قسم "مرشد" (المساعد الإسلامي الذكي)

طريقة التشغيل:
  1) pip install -r requirements.txt
  2) عيّن مفتاح OpenRouter كمتغير بيئة OPENROUTER_API_KEY (انظر ملف .env.example)
  3) python fatwa_api_server.py
  الخادم سيعمل افتراضيًا على http://localhost:5000
"""

import os
import re
import json

import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

# ============================================================
# الإعدادات
# ============================================================
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()

# اختر أي موديل من https://openrouter.ai/models
# الموديلات المنتهية بـ ":free" مجانية تمامًا لكن بحدود طلبات صارمة.
MODEL_NAME = os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# اختياري لكن موصى به من OpenRouter (يظهر في لوحة تحكمهم فقط، لا يؤثر وظيفيًا)
SITE_URL = os.environ.get("SITE_URL", "").strip()
SITE_NAME = os.environ.get("SITE_NAME", "").strip()

app = Flask(__name__)

# ============================================================
# CORS: يحدد أي النطاقات يُسمح لها بالاتصال بهذا الخادم
# ------------------------------------------------------------
# محليًا (بدون ضبط المتغير) يُسمح للجميع لتسهيل التجربة والتطوير.
# في الإنتاج (سيرفر مجاني)، عيّن متغير البيئة ALLOWED_ORIGINS بنطاق
# موقعك الفعلي (مفصول بفواصل إن وجد أكثر من نطاق)، مثال:
#   ALLOWED_ORIGINS=https://your-site.pages.dev,https://yourdomain.com
# هذا يمنع أي موقع آخر من استخدام خادمك ومفتاح OpenRouter الخاص بك.
# ============================================================
_allowed_origins_env = os.environ.get("ALLOWED_ORIGINS", "").strip()
if _allowed_origins_env:
    ALLOWED_ORIGINS = [o.strip() for o in _allowed_origins_env.split(",") if o.strip()]
    CORS(app, origins=ALLOWED_ORIGINS)
else:
    CORS(app)  # وضع التطوير المحلي: يسمح لأي نطاق (غير موصى به في الإنتاج)


# ============================================================
# نصوص التوجيه (System Prompts) - يجب أن تطابق ما هو متوقع في الواجهة الأمامية
# ============================================================
AI_FATWA_SYSTEM_PROMPT = """أنت مساعد يقدم عرضًا تعليميًا مبسطًا لآراء الفقه الإسلامي السائدة عند أهل السنة حول سؤال يطرحه المستخدم.
أجب حصرًا بكائن JSON صالح دون أي نص إضافي قبله أو بعده وبدون علامات backticks، بالمخطط التالي بالضبط:
{
  "isFatwa": true أو false (false إذا كان السؤال لا يتعلق إطلاقًا بحكم شرعي فقهي),
  "category": واحدة من ["salah","tahara","muamalat","siyam","usra","zakah","hajj","muasira","digital","aqidah","akhlaq","other"],
  "ruling": واحدة من ["halal","haram","makruh","mustahab","jaiz","wajib","khilaf"] (استخدم "khilaf" عندما تختلف فيه آراء الفقهاء ولا يوجد قول واحد راجح بوضوح),
  "rulingLabel": التسمية العربية المطابقة تمامًا (حلال/حرام/مكروه/مستحب/جائز/واجب/فيه خلاف),
  "question": إعادة صياغة السؤال بوضوح بالعربية الفصحى,
  "shortAnswer": جملة أو جملتان تلخصان الإجابة,
  "detail": فقرة أوسع تشرح التفصيل والتوجيه العملي، مع الإشارة إلى اختلاف المذاهب إن وجد دون ترجيح متعصب لمذهب معين,
  "evidences": مصفوفة من 0 إلى 3 عناصر {"text": نص الآية أو الحديث, "source": المصدر مثل اسم السورة ورقم الآية أو من رواه}. أدرج فقط أدلة تثق أنها صحيحة ومعروفة، ولا تختلق نصوصًا أو تنسبها خطأ. إن لم تكن واثقًا من دليل محدد فاجعل المصفوفة فارغة بدلًا من التخمين
}
التزم بالاعتدال والموضوعية، وانسب المسائل الخلافية للخلاف الفقهي الحقيقي دون تحيز، ولا تفتِ في مسائل شخصية دقيقة تحتاج تفصيل حالة فردية بل وجّه لمراجعة عالم موثوق."""

MURSHID_SYSTEM_PROMPT = """أنت "مرشد"، مساعد إسلامي يجيب حصرًا اعتمادًا على القرآن الكريم والسنة النبوية الصحيحة وكتب الفقه المعتمدة عند أهل السنة، بأسلوب واضح ومختصر ومطمئن.
عند سؤالك عن حكم شرعي اذكر الحكم ثم دليله باختصار. عند طلب خطة عبادية (مثل ختم القرآن) اعرضها كخطوات عملية مرقمة. لا تفتِ في مسائل شخصية دقيقة تحتاج تفصيل حالة فردية بل وجّه لمراجعة عالم موثوق.
أجب بنص عادي دون Markdown ودون تنسيق زائد، بحد أقصى فقرة أو فقرتين."""


def call_llm(system_prompt, user_question, max_tokens=1000):
    """ينادي نموذج اللغة عبر OpenRouter ويعيد النص الكامل للإجابة."""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("missing_api_key")

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    # الترويسات التالية اختيارية (لإحصائيات OpenRouter فقط) لكن لا ضرر منها
    if SITE_URL:
        headers["HTTP-Referer"] = SITE_URL
    if SITE_NAME:
        headers["X-Title"] = SITE_NAME

    payload = {
        "model": MODEL_NAME,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_question},
        ],
    }

    try:
        resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=60)
    except requests.RequestException as e:
        raise ConnectionError(str(e))

    if resp.status_code >= 400:
        # نسجل نص الخطأ الكامل في اللوج للمساعدة على التشخيص، لكن لا نكشفه للمستخدم
        app.logger.error("OpenRouter API error (%s): %s", resp.status_code, resp.text[:500])
        raise RuntimeError("upstream_error")

    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("upstream_error")

    content = (choices[0].get("message") or {}).get("content") or ""
    return content.strip()


def extract_json(raw_text):
    """يحاول استخراج أول كائن JSON صالح من نص قد يحتوي على backticks أو نص زائد."""
    cleaned = raw_text.strip()
    cleaned = re.sub(r"^```(json)?", "", cleaned).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.S)
        if match:
            return json.loads(match.group(0))
        raise


# ============================================================
# نقطة وصول: الفتاوى الذكية
# ============================================================
@app.route("/api/fatwa", methods=["POST"])
def api_fatwa():
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "empty_question"}), 400

    try:
        raw = call_llm(AI_FATWA_SYSTEM_PROMPT, question, max_tokens=1200)
        if not raw:
            return jsonify({"error": "empty_model_response"}), 502
        parsed = extract_json(raw)
        return jsonify(parsed)
    except RuntimeError as e:
        if str(e) == "missing_api_key":
            return jsonify({"error": "missing_api_key"}), 500
        return jsonify({"error": "upstream_error"}), 502
    except json.JSONDecodeError:
        return jsonify({"error": "invalid_model_output"}), 502
    except ConnectionError as e:
        app.logger.error("OpenRouter connection error: %s", e)
        return jsonify({"error": "upstream_error"}), 502
    except Exception:  # حماية عامة من أي خطأ غير متوقع
        app.logger.exception("Unexpected error in /api/fatwa")
        return jsonify({"error": "upstream_error"}), 502


# ============================================================
# نقطة وصول: مرشد (المساعد الإسلامي الذكي)
# ============================================================
@app.route("/api/murshid", methods=["POST"])
def api_murshid():
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    system_prompt = (data.get("system") or MURSHID_SYSTEM_PROMPT).strip()
    if not question:
        return jsonify({"error": "empty_question"}), 400

    try:
        answer = call_llm(system_prompt, question, max_tokens=800)
        if not answer:
            return jsonify({"error": "empty_model_response"}), 502
        return jsonify({"answer": answer})
    except RuntimeError as e:
        if str(e) == "missing_api_key":
            return jsonify({"error": "missing_api_key"}), 500
        return jsonify({"error": "upstream_error"}), 502
    except ConnectionError as e:
        app.logger.error("OpenRouter connection error: %s", e)
        return jsonify({"error": "upstream_error"}), 502
    except Exception:
        app.logger.exception("Unexpected error in /api/murshid")
        return jsonify({"error": "upstream_error"}), 502


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "has_api_key": bool(OPENROUTER_API_KEY), "model": MODEL_NAME})


if __name__ == "__main__":
    if not OPENROUTER_API_KEY:
        print("تحذير: لم يتم العثور على متغير البيئة OPENROUTER_API_KEY.")
        print("عيّنه أولًا، مثال على لينكس/ماك:  export OPENROUTER_API_KEY=sk-or-...")
        print("أو على ويندوز (PowerShell):      $env:OPENROUTER_API_KEY='sk-or-...'")
    # PORT: تحدده منصات الاستضافة المجانية (Render, Railway...) تلقائيًا عبر
    # متغير بيئة، لذا نقرأه بدل تثبيت 5000. محليًا سيبقى 5000 كما هو.
    port = int(os.environ.get("PORT", 5000))
    # DEBUG: يُفعَّل تلقائيًا فقط عند التشغيل المحلي. في الإنتاج اضبط
    # FLASK_DEBUG=0 (أو اتركه فارغًا) واستخدم خادم WSGI حقيقي مثل gunicorn
    # (انظر Procfile) بدل الاعتماد على خادم التطوير المدمج في Flask.
    debug_mode = os.environ.get("FLASK_DEBUG", "1" if port == 5000 else "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
