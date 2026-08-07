# -*- coding: utf-8 -*-
"""
fatwa_api_server.py
------------------------------
نسخة مدمجة من خادمك الحالي (fatwa_api_server.py) + إشعارات الدفع
(push_notifications_backend.py)، مع فارق واحد مهم: الاشتراكات تُخزَّن
الآن في Supabase (قاعدة بيانات دائمة) بدل ملف JSON محلي، لأن التخزين
المحلي على Render يُمسح عند كل إعادة نشر/إعادة تشغيل.

[تعديل] أُضيف تسجيل تفصيلي (logging) لأي خطأ يحدث أثناء إرسال إشعار
push عبر webpush()، لتشخيص أسباب الفشل الحقيقية بدل تجاهلها بصمت.

الإضافات المطلوبة على requirements.txt:
    supabase
    pywebpush

متغيرات البيئة المطلوبة على Render (Environment):
    OPENROUTER_API_KEY   (موجود عندك مسبقًا)
    SUPABASE_URL          -> من Supabase: Project Settings > API > Project URL
    SUPABASE_SERVICE_KEY  -> من Supabase: Project Settings > API > service_role secret
    VAPID_PRIVATE_KEY     -> المفتاح الخاص الذي تولّده مرة واحدة (انظر التعليمات بالأسفل)
    PUSH_ADMIN_KEY         -> مفتاح سرّي من اختيارك، يُستخدم لحماية /api/push/broadcast

قبل الاستخدام لأول مرة: نفّذ سكربت supabase_push_table.sql داخل
SQL Editor في لوحة Supabase لإنشاء جدول push_subscriptions.
"""

import os
import re
import json

import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from pywebpush import webpush, WebPushException
from supabase import create_client, Client

# ============================================================
# إعدادات OpenRouter (كما في خادمك الحالي، بدون أي تغيير)
# ============================================================
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
MODEL_NAME = os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
SITE_URL = os.environ.get("SITE_URL", "").strip()
SITE_NAME = os.environ.get("SITE_NAME", "").strip()

app = Flask(__name__)

_allowed_origins_env = os.environ.get("ALLOWED_ORIGINS", "").strip()
if _allowed_origins_env:
    ALLOWED_ORIGINS = [o.strip() for o in _allowed_origins_env.split(",") if o.strip()]
    CORS(app, origins=ALLOWED_ORIGINS)
else:
    CORS(app)  # وضع التطوير المحلي فقط

# ============================================================
# إعدادات Supabase (لتخزين اشتراكات الإشعارات)
# ============================================================
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()

supabase: Client | None = None
if SUPABASE_URL and SUPABASE_SERVICE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

PUSH_TABLE = "push_subscriptions"

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_CLAIMS = {"sub": "mailto:avtffhg@gmail.com"}


# ============================================================
# نصوص التوجيه (System Prompts) — كما هي في خادمك الحالي
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
    if not OPENROUTER_API_KEY:
        raise RuntimeError("missing_api_key")

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
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
        app.logger.error("OpenRouter API error (%s): %s", resp.status_code, resp.text[:500])
        raise RuntimeError("upstream_error")

    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("upstream_error")

    content = (choices[0].get("message") or {}).get("content") or ""
    return content.strip()


def extract_json(raw_text):
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
    except Exception:
        app.logger.exception("Unexpected error in /api/fatwa")
        return jsonify({"error": "upstream_error"}), 502


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


# ============================================================
# إشعارات الدفع (Web Push) — مخزّنة في Supabase بدل ملف محلي
# ============================================================

@app.route("/api/push/subscribe", methods=["POST"])
def push_subscribe():
    """يستقبل بيانات اشتراك جهاز جديد ويخزّنها في جدول Supabase."""
    if not supabase:
        return jsonify({"error": "supabase_not_configured"}), 500

    subscription = request.get_json(force=True, silent=True)
    if not subscription or "endpoint" not in subscription or "keys" not in subscription:
        return jsonify({"error": "بيانات اشتراك غير صالحة"}), 400

    row = {
        "endpoint": subscription["endpoint"],
        "p256dh": subscription["keys"].get("p256dh", ""),
        "auth": subscription["keys"].get("auth", ""),
    }

    try:
        # upsert على عمود endpoint: يحدّث الصف إن كان موجودًا، أو يضيف جديدًا
        supabase.table(PUSH_TABLE).upsert(row, on_conflict="endpoint").execute()
    except Exception:
        app.logger.exception("فشل تخزين الاشتراك في Supabase")
        return jsonify({"error": "storage_error"}), 500

    return jsonify({"status": "subscribed"}), 201


def _subscription_dict(row):
    """يحوّل صف Supabase إلى شكل الاشتراك الذي تتوقعه مكتبة pywebpush."""
    return {
        "endpoint": row["endpoint"],
        "keys": {"p256dh": row["p256dh"], "auth": row["auth"]},
    }


@app.route("/api/push/broadcast", methods=["POST"])
def push_broadcast():
    """يرسل إشعارًا لكل الأجهزة المشتركة المخزّنة في Supabase. محمي بمفتاح سرّي."""
    admin_key = request.headers.get("X-Admin-Key", "")
    expected_key = os.environ.get("PUSH_ADMIN_KEY", "")
    if not expected_key or admin_key != expected_key:
        return jsonify({"error": "غير مصرح"}), 401

    if not VAPID_PRIVATE_KEY:
        return jsonify({"error": "لم يتم إعداد VAPID_PRIVATE_KEY على الخادم"}), 500
    if not supabase:
        return jsonify({"error": "supabase_not_configured"}), 500

    payload = request.get_json(force=True, silent=True) or {}
    title = payload.get("title", "منصة القرآن والسنة")
    body = payload.get("body", "")
    url = payload.get("url", "./")

    try:
        rows = supabase.table(PUSH_TABLE).select("*").execute().data or []
    except Exception:
        app.logger.exception("فشل قراءة الاشتراكات من Supabase")
        return jsonify({"error": "storage_error"}), 500

    sent, failed, removed = 0, 0, 0
    expired_endpoints = []

    for row in rows:
        sub = _subscription_dict(row)
        try:
            webpush(
                subscription_info=sub,
                data=json.dumps({"title": title, "body": body, "url": url}, ensure_ascii=False),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims=VAPID_CLAIMS.copy()
            )
            sent += 1
        except WebPushException as ex:
            status_code = getattr(ex.response, "status_code", None)
            response_text = getattr(ex.response, "text", None)
            # [تعديل] تسجيل تفصيلي لمعرفة سبب فشل الإرسال الحقيقي
            app.logger.error(
                "webpush failed: endpoint=%s status=%s body=%s error=%s",
                row.get("endpoint", "")[:80], status_code, response_text, str(ex)
            )
            if status_code in (404, 410, 403):
                # 404/410: الاشتراك لم يعد صالحًا (المستخدم ألغى الإذن أو أزال المتصفح)
                # 403: الاشتراك مربوط بمفتاح VAPID قديم مختلف عن المفتاح الحالي —
                # لن ينجح إرساله أبدًا حتى يعيد المستخدم الاشتراك من الفرونت-إند،
                # لذا نحذفه بدل تكرار عدّه "فشل" في كل بث قادم
                expired_endpoints.append(row["endpoint"])
                removed += 1
            else:
                failed += 1
        except Exception as ex:
            # [تعديل] التقاط أي خطأ آخر غير متوقع (مثل مشاكل في مفتاح VAPID نفسه)
            app.logger.exception(
                "webpush unexpected error for endpoint=%s: %s",
                row.get("endpoint", "")[:80], str(ex)
            )
            failed += 1

    if expired_endpoints:
        try:
            supabase.table(PUSH_TABLE).delete().in_("endpoint", expired_endpoints).execute()
        except Exception:
            app.logger.exception("فشل حذف الاشتراكات منتهية الصلاحية")

    return jsonify({
        "sent": sent,
        "failed": failed,
        "removed_expired": removed,
        "total_subscribers_now": len(rows) - removed
    }), 200


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "has_api_key": bool(OPENROUTER_API_KEY),
        "model": MODEL_NAME,
        "supabase_configured": bool(supabase),
        "vapid_configured": bool(VAPID_PRIVATE_KEY),
    })


if __name__ == "__main__":
    if not OPENROUTER_API_KEY:
        print("تحذير: لم يتم العثور على متغير البيئة OPENROUTER_API_KEY.")
    if not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
        print("تحذير: لم يتم إعداد SUPABASE_URL / SUPABASE_SERVICE_KEY — الإشعارات لن تعمل.")
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "1" if port == 5000 else "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
