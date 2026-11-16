"""
المساعد الإداري — لجنة المشاركة المحلية لبريد تبسة
==================================================
سيرفر Webhook (FastAPI) للرد الفوري على رسائل ماسنجر الصفحة،
اعتماداً حصرياً على وثائق مجلد reference/ عبر قاعدة المعرفة.

النشر: Render.com (Free) — Start Command:
    uvicorn app:app --host 0.0.0.0 --port $PORT

المتغيرات المطلوبة:
    GEMINI_API_KEY
    FACEBOOK_ACCESS_TOKEN   (Page Access Token دائم)
    VERIFY_TOKEN            (نص سري تختاره)
اختيارية:
    GEMINI_MODEL            (افتراضي gemini-3.5-flash)
    FB_GRAPH_VERSION        (افتراضي v21.0)
    PAGE_NAME               (افتراضي: لجنة المشاركة المحلية لبريد تبسة)
    KB_MIN_COVERAGE         (حد تغطية السؤال في المقاطع، افتراضي 0.55)
    LEADS_PATH              (افتراضي data/leads.jsonl)
"""

from __future__ import annotations

import asyncio
import csv
import hmac
import html
import io
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import requests
from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)

import knowledge
import registry

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("barid-bot")

PAGE_NAME = os.environ.get("PAGE_NAME", "لجنة المشاركة المحلية لبريد تبسة")
PAGE_ACCESS_TOKEN = os.environ.get("FACEBOOK_ACCESS_TOKEN", "")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "barid_tebessa_verify_token")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GRAPH_VERSION = os.environ.get("FB_GRAPH_VERSION", "v21.0")
MIN_COVERAGE = float(os.environ.get("KB_MIN_COVERAGE", "0.55"))
LEADS_PATH = Path(os.environ.get("LEADS_PATH", "data/leads.jsonl"))
PAGE_ID = os.environ.get("FACEBOOK_PAGE_ID", "1418255570212031")
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://cplaptebessa.onrender.com"
).rstrip("/")
REQUIRE_REGISTRATION = os.environ.get("REQUIRE_REGISTRATION", "1") == "1"
ADMIN_KEY = os.environ.get("ADMIN_KEY") or VERIFY_TOKEN

SYSTEM_PROMPT = (
    f"أنت «المساعد الإداري» الرسمي لصفحة {PAGE_NAME}، الجهة الممثلة لعمال قطاع "
    "بريد الجزائر بولاية تبسة.\n"
    "شخصيتك: إداري رسمي، مهذب، دقيق، موجز، في خدمة العمال والمواطنين.\n"
    "قواعد صارمة لا تُخالَف:\n"
    "1) اعتمد حصرياً على المقاطع المستخرجة من وثائق اللجنة أدناه، ولا تستخدم أي معلومة خارجية.\n"
    "2) لا تخترع أرقاماً أو تواريخ أو أسماء أو إجراءات غير موجودة نصاً في المقاطع.\n"
    "3) إن لم تجد الإجابة في المقاطع، قل بوضوح إن المعلومة غير متوفرة لديك واقترح التواصل مع اللجنة، دون تخمين.\n"
    "4) اذكر اسم الوثيقة المصدر بين قوسين في نهاية الإجابة إن أمكن.\n"
    "5) أجب بالعربية الفصحى الرسمية المبسطة. إن كتب المستخدم بالفرنسية فأجب بالفرنسية.\n"
    "6) ابقَ ضمن نطاق خدمات اللجنة الاجتماعية والأنشطة والاستفسارات المهنية، ورفض بلطف أي موضوع آخر.\n"
    "7) لا تدّعِ أنك إنسان، وعرّف نفسك كمساعد آلي عند السؤال.\n"
)

GREETING_WORDS = {
    "سلام", "السلام", "عليكم", "مرحبا", "مرحباً", "اهلا", "أهلا", "سهلا",
    "صباح", "الخير", "خير", "مساء", "النور", "نور", "تحيه", "تحية", "bonjour", "bonsoir",
    "salut", "hello", "hi", "cc",
}

HANDOFF_PHRASES = (
    "اريد التحدث مع مسؤول",
    "أريد التحدث مع مسؤول",
    "تحويل",
    "شكوى",
    "شكوي",
    "تظلم",
    "أريد شخصا حقيقيا",
    "اريد شخص حقيقي",
    "مسؤول اللجنة",
)

GREETING_REPLY = (
    f"السلام عليكم ورحمة الله 🌿\n"
    f"مرحباً بك في الصفحة الرسمية لـ{PAGE_NAME}.\n"
    f"أنا المساعد الإداري الآلي للجنة، أجيب عن استفساراتكم حول الخدمات الاجتماعية "
    f"والأنشطة والإجراءات الإدارية.\n"
    f"تفضّل بطرح سؤالك."
)

NO_DOCS_REPLY = (
    "مرحباً بك. المساعد الإداري قيد التهيئة حالياً ولم تُرفق وثائق اللجنة بعد، "
    "لذا لا أستطيع تقديم إجابة دقيقة في هذه اللحظة. "
    "يرجى إعادة المحاولة لاحقاً أو التواصل المباشر مع أعضاء اللجنة. شكراً لتفهمك."
)

ESCALATION_REPLY = (
    "هذه المعلومة غير متوفرة في الوثائق الرسمية المتاحة لديّ، "
    "ولا يجوز لي الاجتهاد في شؤون اللجنة.\n"
    "تم تسجيل استفسارك وسيتواصل معك أحد أعضاء اللجنة عند الحاجة.\n"
    "يمكنك متابعة طرح بقية أسئلتك الآن؛ سأجيب عن كل ما هو متوفر في الوثائق."
)

HANDOFF_REPLY = (
    "تم تسجيل طلبك وسيتواصل معك أحد أعضاء اللجنة المكلفين في أقرب وقت. "
    "يمكنك في الأثناء متابعة طرح أسئلتك الأخرى؛ سأجيب عن كل ما هو متوفر في الوثائق."
)

REGISTRATION_REQUIRED_REPLY = (
    "مرحباً بك في الصفحة الرسمية لـ{PAGE_NAME}.\n"
    "قبل بدء المراسلة، يُرجى تسجيل بياناتك (الاسم، اللقب، الرتبة، مكان العمل) "
    "عبر الرابط التالي:\n{link}\n"
    "بعد التحقق من بياناتك من طرف أعضاء اللجنة، يمكنك مواصلة المراسلة والاستفسار."
)

REGISTRATION_PENDING_REPLY = (
    "بياناتك مسجّلة وهي قيد المراجعة من طرف أعضاء اللجنة.\n"
    "سيتم تفعيل المراسلة فور التحقق منها. يمكنك تعديل بياناتك عبر الرابط:\n{link}"
)

REGISTRATION_REJECTED_REPLY = (
    "تعذّر التحقق من بياناتك المسجّلة. يُرجى إعادة التسجيل ببيانات صحيحة "
    "(الاسم، اللقب، الرتبة، مكان العمل) عبر الرابط التالي:\n{link}"
)

REGISTRATION_APPROVED_REPLY = (
    "تم التحقق من بياناتك بنجاح.\n"
    "يمكنك الآن مراسلة الصفحة وطرح استفساراتك، وسأجيبك اعتماداً على وثائق اللجنة الرسمية."
)

INDEX: dict | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global INDEX
    try:
        INDEX = await asyncio.to_thread(knowledge.ensure_index)
        count = INDEX.get("chunk_count", 0) if INDEX else 0
        log.info("قاعدة المعرفة جاهزة: %d مقطع", count)
    except Exception:  # noqa: BLE001
        log.exception("تعذر بناء قاعدة المعرفة — سيعمل البوت بوضع الاعتذار")
    try:
        registry.init_db()
        log.info("سجل التسجيلات جاهز: %s", registry.DB_PATH)
    except Exception:  # noqa: BLE001
        log.exception("تعذر تهيئة سجل التسجيلات")
    yield


app = FastAPI(title=f"{PAGE_NAME} — المساعد الإداري", lifespan=lifespan)


# ------------------------------------------------------------------ أدوات مساعدة
def _is_greeting(text: str) -> bool:
    tokens = knowledge.tokenize(text)
    return bool(tokens) and len(tokens) <= 4 and all(t in GREETING_WORDS for t in tokens)


def _wants_human(text: str) -> bool:
    normalized = knowledge.normalize(text)
    return any(knowledge.normalize(p) in normalized for p in HANDOFF_PHRASES)


def _log_lead(sender_id: str, text: str, reason: str) -> None:
    try:
        LEADS_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "sender_id": sender_id,
            "reason": reason,
            "message": text[:500],
        }
        with LEADS_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        log.exception("تعذر تسجيل الاستفسار")


def _split_message(text: str, limit: int = 1900) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for paragraph in text.split("\n"):
        if len(current) + len(paragraph) + 1 <= limit:
            current = f"{current}\n{paragraph}".strip()
            continue
        if current:
            parts.append(current)
        while len(paragraph) > limit:
            parts.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        current = paragraph
    if current:
        parts.append(current)
    return parts


def _generate_answer(question: str, hits: list[dict]) -> str:
    blocks = []
    for i, hit in enumerate(hits, start=1):
        locator = f" — {hit['locator']}" if hit.get("locator") else ""
        blocks.append(f"[{i}] المصدر: {hit['source']}{locator}\n{hit['text']}")
    context = "\n\n".join(blocks)

    prompt = (
        f"{SYSTEM_PROMPT}\n"
        f"===== مقاطع وثائق اللجنة =====\n{context}\n"
        f"===== نهاية المقاطع =====\n\n"
        f"رسالة العامل/المواطن:\n{question}\n\n"
        f"اكتب الإجابة الرسمية الآن (قصيرة وواضحة). "
        f"إن كانت المقاطع لا تحتوي الإجابة فاكتب فقط: INSUFFICIENT_INFORMATION"
    )

    return knowledge.gemini_generate(
        [{"text": prompt}],
        max_tokens=700,
        temperature=0.15,
        label="answer",
    )


def _send_messenger(sender_id: str, text: str) -> None:
    url = (
        f"https://graph.facebook.com/{GRAPH_VERSION}/me/messages"
        f"?access_token={PAGE_ACCESS_TOKEN}"
    )
    res = requests.post(
        url,
        json={
            "recipient": {"id": sender_id},
            "messaging_type": "RESPONSE",
            "message": {"text": text},
        },
        timeout=20,
    )
    try:
        log.info("Send API: %s", res.json())
    except ValueError:
        log.info("Send API status: %s", res.status_code)


def _send_action(sender_id: str, action: str) -> None:
    try:
        requests.post(
            f"https://graph.facebook.com/{GRAPH_VERSION}/me/messages"
            f"?access_token={PAGE_ACCESS_TOKEN}",
            json={"recipient": {"id": sender_id}, "sender_action": action},
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("sender_action %s فشل: %s", action, exc)


def _answer_text(sender_id: str, text: str) -> str:
    """يولّد نص الرد المناسب دون إرساله (يُستخدم في ماسنجر وصفحة المحادثة)."""
    if _wants_human(text):
        _log_lead(sender_id, text, "طلب توجيه إلى اللجنة")
        return HANDOFF_REPLY

    if _is_greeting(text):
        return GREETING_REPLY

    if not INDEX or not INDEX.get("chunks"):
        return NO_DOCS_REPLY

    hits = knowledge.select_context(text, INDEX, min_coverage=MIN_COVERAGE)
    if not hits:
        _log_lead(sender_id, text, "لا توجد إجابة في الوثائق")
        return ESCALATION_REPLY

    try:
        answer = _generate_answer(text, hits)
    except Exception:  # noqa: BLE001
        log.exception("فشل توليد الإجابة")
        _log_lead(sender_id, text, "خطأ تقني في التوليد")
        return (
            "أعتذر، حدث عطل تقني مؤقت أثناء معالجة طلبك. "
            "يرجى إعادة إرسال السؤال بعد قليل، أو ترك استفسارك وسنعاود التواصل معك."
        )

    if "INSUFFICIENT_INFORMATION" in answer:
        _log_lead(sender_id, text, "النموذج لم يجد إجابة في المقاطع")
        return ESCALATION_REPLY

    return answer


def _answer(sender_id: str, text: str) -> None:
    """يرسل الرد المناسب عبر ماسنجر، مع توجيه الطلب إلى اللجنة عند الحاجة."""
    for part in _split_message(_answer_text(sender_id, text)):
        _send_messenger(sender_id, part)


# ------------------------------------------------------------------ صفحات الويب
PAGE_STYLE = """
:root{--green:#0b6b3a;--green2:#0e8a4a;--ink:#1f2d27;--muted:#5f6f67;--line:#d7e0db;}
*{box-sizing:border-box}
body{margin:0;font-family:"Segoe UI",Tahoma,Arial,sans-serif;background:#f2f5f3;color:var(--ink);line-height:1.7}
header{background:linear-gradient(135deg,var(--green),var(--green2));color:#fff;padding:22px 18px}
header .brand{font-size:13px;opacity:.9;letter-spacing:.5px}
header h1{margin:6px 0 0;font-size:21px;font-weight:700}
main{max-width:760px;margin:-26px auto 40px;padding:0 16px}
.card{background:#fff;border:1px solid var(--line);border-radius:16px;padding:22px;box-shadow:0 10px 30px rgba(11,107,58,.08)}
h2{margin:0 0 6px;font-size:19px;color:var(--green)}
p.lead{margin:0 0 16px;color:var(--muted)}
label{display:block;font-weight:600;margin:14px 0 6px;font-size:14px}
input{width:100%;padding:12px 13px;border:1px solid var(--line);border-radius:10px;font-size:15px;font-family:inherit;background:#fbfcfb}
input:focus{outline:2px solid rgba(14,138,74,.35);border-color:var(--green2)}
button{margin-top:18px;width:100%;padding:13px;border:0;border-radius:10px;background:var(--green);color:#fff;font-size:16px;font-weight:700;cursor:pointer}
button:hover{background:var(--green2)}
.note{margin-top:16px;font-size:13px;color:var(--muted)}
.ok{text-align:center;padding:8px 0 2px}
.ok .mark{width:64px;height:64px;border-radius:50%;background:#e7f6ec;color:var(--green);display:flex;align-items:center;justify-content:center;font-size:34px;margin:0 auto 12px}
.alert{background:#fde8e8;border:1px solid #f3c2c2;color:#a02020;padding:10px 14px;border-radius:10px;font-size:14px;margin-bottom:8px}
footer{text-align:center;font-size:12px;color:var(--muted);padding:0 16px 30px}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:700}
.badge.pending{background:#fff4d6;color:#8a6100}
.badge.approved{background:#e7f6ec;color:#0b6b3a}
.badge.rejected{background:#fde8e8;color:#a02020}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:right;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:top}
.row-actions{display:flex;gap:8px;flex-wrap:wrap}
.row-actions button{width:auto;margin:0;padding:7px 12px;font-size:13px}
button.danger{background:#a02020}
.counts{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 6px}
.count{background:#fff;border:1px solid var(--line);border-radius:12px;padding:10px 14px;font-size:13px}
.count b{display:block;font-size:20px;color:var(--green)}
.tabs{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 18px}
.tabs a{text-decoration:none;color:var(--ink);background:#eef2f0;padding:7px 14px;border-radius:999px;font-size:13px}
.tabs a.active{background:var(--green);color:#fff}
.chat{background:#fff;border:1px solid var(--line);border-radius:16px;overflow:hidden;box-shadow:0 10px 30px rgba(11,107,58,.08)}
.chat-head{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--line);background:#fbfcfb}
.chat-head .who{font-weight:700;font-size:14px}
.chat-head .who span{display:block;font-weight:400;font-size:12px;color:var(--muted)}
.chat-head a{font-size:13px;color:var(--muted)}
.chat-body{height:52vh;min-height:320px;max-height:540px;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px;background:#f7faf8}
.msg{max-width:86%;padding:10px 14px;border-radius:14px;font-size:14.5px;white-space:pre-wrap;overflow-wrap:anywhere}
.msg.user{align-self:flex-start;background:var(--green);color:#fff;border-bottom-right-radius:4px}
.msg.bot{align-self:flex-end;background:#fff;border:1px solid var(--line);border-bottom-left-radius:4px}
.composer{display:flex;gap:8px;padding:12px;border-top:1px solid var(--line);background:#fff}
.composer input{margin:0}
.composer button{width:auto;margin:0;padding:12px 20px;flex:0 0 auto}
.hint{font-size:12px;color:var(--muted);text-align:center;padding:0 12px 10px}
@media(max-width:520px){main{margin-top:-20px}.card{padding:18px}table{font-size:13px}.chat-body{height:58vh}}
"""


def _layout(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{PAGE_STYLE}</style></head><body>"
        '<header><div class="brand">بريد الجزائر — لجنة المشاركة</div>'
        f"<h1>{html.escape(PAGE_NAME)}</h1></header>"
        f"<main>{body}</main>"
        "<footer>هذه الخدمة مخصصة لعمال قطاع بريد الجزائر بولاية تبسة. "
        "جميع البيانات تُعالَج بسرية.</footer></body></html>"
    )


def _register_form(token: str, error: str = "") -> str:
    hidden = (
        f'<input type="hidden" name="t" value="{html.escape(token)}">' if token else ""
    )
    alert = f'<div class="alert">{html.escape(error)}</div>' if error else ""
    return _layout(
        "تسجيل بيانات العامل",
        '<div class="card"><h2>تسجيل بيانات العامل</h2>'
        '<p class="lead">قبل مراسلة الصفحة الرسمية، يُرجى تسجيل بياناتك '
        "ليتم التحقق منها من طرف أعضاء اللجنة.</p>"
        '<ol class="note"><li>املأ بياناتك ثم أرسلها.</li>'
        "<li>تنتظر تحقق أعضاء اللجنة من البيانات.</li>"
        "<li>تعود إلى ماسنجر لمتابعة استفساراتك.</li></ol>"
        f"{alert}"
        '<form method="post" action="/register">'
        f"{hidden}"
        '<label for="first_name">الاسم</label>'
        '<input id="first_name" name="first_name" maxlength="100" required>'
        '<label for="last_name">اللقب</label>'
        '<input id="last_name" name="last_name" maxlength="100" required>'
        '<label for="grade">الرتبة</label>'
        '<input id="grade" name="grade" maxlength="100" required>'
        '<label for="workplace">مكان العمل</label>'
        '<input id="workplace" name="workplace" maxlength="120" required>'
        '<button type="submit">إرسال البيانات</button></form>'
        '<p class="note">لا تشارك أي بيانات حساسة مثل كلمات المرور أو أرقام '
        "الحسابات البريدية.</p>"
        '<p class="note">لديك حساب مُتحقَّق؟ '
        '<a href="/chat">الدخول إلى المحادثة</a></p></div>',
    )


def _register_success(linked: bool) -> str:
    extra = (
        f'<p class="note">عُد إلى ماسنجر عبر الرابط: '
        f'<a href="https://m.me/{PAGE_ID}">m.me/{PAGE_ID}</a></p>'
        if linked
        else '<p class="note"><a href="/chat">متابعة إلى المحادثة</a></p>'
    )
    return _layout(
        "تم التسجيل",
        '<div class="card ok"><div class="mark">✓</div>'
        "<h2>تم تسجيل بياناتك بنجاح</h2>"
        "<p>طلبك الآن قيد المراجعة من طرف أعضاء اللجنة. "
        "سيتم تفعيل المراسلة فور التحقق من البيانات.</p>"
        f"{extra}</div>",
    )


COOKIE_NAME = "cpl_session"


def _cookie_secure(request: Request) -> bool:
    override = os.environ.get("COOKIE_SECURE")
    if override is not None:
        return override == "1"
    return request.url.scheme == "https"


def _session_psid(request: Request) -> str | None:
    token = request.cookies.get(COOKIE_NAME)
    return registry.unsign(token) if token else None


def _chat_placeholder(row: dict) -> str:
    name = f"{html.escape(row['first_name'])} {html.escape(row['last_name'])}"
    if row["status"] == "rejected":
        message = (
            "تعذّر التحقق من بياناتك. يُرجى تعديل البيانات وإعادة إرسالها "
            "ليتمكن أعضاء اللجنة من مراجعتها."
        )
    else:
        message = (
            "بياناتك مسجّلة وهي قيد المراجعة من طرف أعضاء اللجنة. "
            "سيتم تفعيل المحادثة فور التحقق منها."
        )
    return _layout(
        "المحادثة",
        '<div class="card"><h2>المحادثة غير مفعّلة بعد</h2>'
        f"<p>مرحباً {name}، {message}</p>"
        '<p class="note"><a href="/chat">تحديث الحالة</a> — '
        '<a href="/register">تعديل البيانات</a></p></div>',
    )


CHAT_HTML = (
    '<div class="chat">'
    '<div class="chat-head"><div class="who">__NAME__<span>__META__</span></div>'
    '<a href="/logout">خروج</a></div>'
    '<div class="chat-body" id="chat-body">'
    '<div class="msg bot">مرحباً بك. اطرح استفسارك وسأجيب اعتماداً على '
    "وثائق اللجنة الرسمية.</div></div>"
    '<form class="composer" id="chat-form">'
    '<input id="chat-input" placeholder="اكتب رسالتك هنا..." maxlength="1000" '
    'autocomplete="off" required>'
    '<button type="submit">إرسال</button></form>'
    '<div class="hint">الردود مستندة إلى وثائق اللجنة فقط. '
    "للاستفسارات غير المتوفرة سيُسجَّل طلبك لتوجيهه إلى أعضاء اللجنة.</div>"
    "</div>"
)

CHAT_JS = """
<script>
(function(){
  var body=document.getElementById('chat-body');
  var form=document.getElementById('chat-form');
  var input=document.getElementById('chat-input');
  function add(text,cls){
    var d=document.createElement('div');
    d.className='msg '+cls;
    d.textContent=text;
    body.appendChild(d);
    body.scrollTop=body.scrollHeight;
    return d;
  }
  form.addEventListener('submit',function(e){
    e.preventDefault();
    var text=input.value.trim();
    if(!text) return;
    add(text,'user');
    input.value='';
    input.focus();
    var pending=add('...','bot');
    pending.style.opacity='0.55';
    fetch('/api/chat',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:text})
    }).then(function(r){ return r.json().then(function(d){ return {ok:r.ok,data:d}; }); })
      .then(function(res){
        pending.remove();
        var d=res.data||{};
        if(res.ok){ add(d.reply||'تعذر الحصول على رد.', 'bot'); }
        else if(d.error==='pending'){ add('بياناتك ما زالت قيد المراجعة.\\nيمكنك تحديث الصفحة لاحقاً.', 'bot'); }
        else if(d.error==='register'){ add('انتهت الجلسة. يرجى إعادة التسجيل من الصفحة الرئيسية.', 'bot'); }
        else { add('تعذر معالجة الرسالة. حاول مجدداً.', 'bot'); }
      })
      .catch(function(){ pending.remove(); add('تعذر الاتصال بالخادم. تحقق من الشبكة وحاول مجدداً.', 'bot'); });
  });
})();
</script>
"""


def _chat_page(row: dict) -> str:
    name = f"{html.escape(row['first_name'])} {html.escape(row['last_name'])}"
    meta = f"{html.escape(row['grade'])} — {html.escape(row['workplace'])}"
    body = CHAT_HTML.replace("__NAME__", name).replace("__META__", meta)
    return _layout("المحادثة", body + CHAT_JS)


def _admin_key_ok(key: str) -> bool:
    return bool(key) and hmac.compare_digest(
        key.encode("utf-8"), ADMIN_KEY.encode("utf-8")
    )


def _admin_page(key: str, status: str) -> str:
    counts = registry.counts()
    rows = registry.list_all(status or None)
    tabs_parts = []
    for value, label in (
        ("", "الكل"),
        ("pending", "قيد المراجعة"),
        ("approved", "مُتحقَّق"),
        ("rejected", "مرفوض"),
    ):
        css_class = ' class="active"' if status == value else ""
        tabs_parts.append(
            f'<a href="/admin?key={html.escape(key)}&status={value}"'
            f"{css_class}>{label}</a>"
        )
    tabs = "".join(tabs_parts)
    badge_labels = {
        "pending": "قيد المراجعة",
        "approved": "مُتحقَّق",
        "rejected": "مرفوض",
    }
    body_rows = []
    for row in rows:
        badge = row["status"]
        badge_label = badge_labels.get(badge, badge)
        note = ("<br>" + html.escape(row["note"])) if row.get("note") else ""
        body_rows.append(
            "<tr>"
            f"<td>{html.escape(row['first_name'])} {html.escape(row['last_name'])}</td>"
            f"<td>{html.escape(row['grade'])}</td>"
            f"<td>{html.escape(row['workplace'])}</td>"
            f'<td><span class="badge {badge}">{badge_label}</span>{note}</td>'
            f"<td>{html.escape(row['created_at'][:10])}</td>"
            "<td><div class=\"row-actions\">"
            f'<form method="post" action="/admin/decision"><input type="hidden" '
            f'name="key" value="{html.escape(key)}"><input type="hidden" '
            f'name="psid" value="{html.escape(row["psid"])}">'
            '<input type="hidden" name="action" value="approve">'
            '<button type="submit">تحقق</button></form>'
            f'<form method="post" action="/admin/decision"><input type="hidden" '
            f'name="key" value="{html.escape(key)}"><input type="hidden" '
            f'name="psid" value="{html.escape(row["psid"])}">'
            '<input type="hidden" name="action" value="reject">'
            '<button type="submit" class="danger">رفض</button></form>'
            f'<form method="post" action="/admin/decision" '
            f"onsubmit=\"return confirm('حذف هذا التسجيل نهائياً؟')\">"
            f'<input type="hidden" name="key" value="{html.escape(key)}">'
            f'<input type="hidden" name="psid" value="{html.escape(row["psid"])}">'
            '<input type="hidden" name="action" value="delete">'
            '<button type="submit" class="danger">حذف</button></form>'
            "</div></td></tr>"
        )
    table = (
        "<table><thead><tr><th>الاسم واللقب</th><th>الرتبة</th><th>مكان العمل</th>"
        "<th>الحالة</th><th>التاريخ</th><th>إجراء</th></tr></thead><tbody>"
        + ("".join(body_rows) or '<tr><td colspan="6">لا توجد تسجيلات.</td></tr>')
        + "</tbody></table>"
    )
    return _layout(
        "لوحة التحقق",
        '<div class="card"><h2>لوحة التحقق من بيانات العمال</h2>'
        '<p class="lead">تحقق من صحة البيانات قبل تفعيل المراسلة.</p>'
        '<div class="counts">'
        f'<div class="count">الإجمالي<b>{counts["total"]}</b></div>'
        f'<div class="count">قيد المراجعة<b>{counts["pending"]}</b></div>'
        f'<div class="count">مُتحقَّق<b>{counts["approved"]}</b></div>'
        f'<div class="count">مرفوض<b>{counts["rejected"]}</b></div></div>'
        f'<div class="tabs">{tabs}</div>'
        f"{table}"
        f'<p class="note"><a href="/admin/export.csv?key={html.escape(key)}">'
        "تصدير كل التسجيلات (CSV)</a></p></div>",
    )


# --------------------------------------------------------------------- المسارات
@app.get("/", response_class=HTMLResponse)
async def home(request: Request, t: str = ""):
    psid = _session_psid(request)
    if psid and not t and registry.is_approved(psid):
        return RedirectResponse("/chat", status_code=302)
    return _register_form(t)


@app.get("/register", response_class=HTMLResponse)
async def register_page(t: str = ""):
    return _register_form(t)


@app.post("/register", response_class=HTMLResponse)
async def register_submit(
    request: Request,
    first_name: str = Form(""),
    last_name: str = Form(""),
    grade: str = Form(""),
    workplace: str = Form(""),
    t: str = Form(""),
):
    fields = {
        "الاسم": first_name.strip(),
        "اللقب": last_name.strip(),
        "الرتبة": grade.strip(),
        "مكان العمل": workplace.strip(),
    }
    missing = [name for name, value in fields.items() if not value]
    if missing:
        return _register_form(t, f"يرجى تعبئة الحقول التالية: {', '.join(missing)}")

    psid = registry.unsign(t) if t else f"web-{uuid.uuid4().hex[:12]}"
    if t and not psid:
        return _register_form("", "انتهت صلاحية الرابط أو أنه غير صالح. "
                                "أعد فتح الرابط من ماسنجر.")
    registry.register(psid, first_name, last_name, grade, workplace)
    log.info("تسجيل جديد: %s %s (%s)", first_name, last_name, psid[:12])
    response = HTMLResponse(
        _register_success(linked=bool(psid and not psid.startswith("web-")))
    )
    response.set_cookie(
        COOKIE_NAME,
        registry.sign(psid),
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
    )
    return response


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    psid = _session_psid(request)
    if not psid:
        return _register_form("", "سجّل بياناتك أولاً لبدء المحادثة.")
    row = registry.get(psid)
    if not row:
        return RedirectResponse("/register", status_code=302)
    if row["status"] != "approved":
        return _chat_placeholder(row)
    return _chat_page(row)


@app.get("/logout")
async def logout():
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie(COOKIE_NAME)
    return response


@app.post("/api/chat")
async def chat_api(request: Request):
    psid = _session_psid(request)
    if not psid:
        return JSONResponse({"error": "register"}, status_code=403)
    try:
        row = registry.get(psid)
    except Exception:  # noqa: BLE001
        log.exception("تعذر قراءة سجل التسجيل")
        return JSONResponse({"error": "unavailable"}, status_code=503)
    if not row:
        return JSONResponse({"error": "register"}, status_code=403)
    if row["status"] != "approved":
        return JSONResponse({"error": "pending"}, status_code=403)

    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid"}, status_code=400)
    text = str(data.get("message") or "").strip()[:1000]
    if not text:
        return JSONResponse({"error": "empty"}, status_code=400)

    if not GEMINI_API_KEY or not INDEX or not INDEX.get("chunks"):
        return {"reply": NO_DOCS_REPLY}

    log.info("محادثة ويب من %s: %s", psid[:12], text[:200])
    reply = await asyncio.to_thread(_answer_text, psid, text)
    return {"reply": reply}


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(key: str = "", status: str = ""):
    if not _admin_key_ok(key):
        return HTMLResponse("<h1>403</h1>", status_code=403)
    return _admin_page(key, status)


@app.post("/admin/decision", response_class=HTMLResponse)
async def admin_decision(
    key: str = Form(""),
    psid: str = Form(""),
    action: str = Form(""),
):
    if not _admin_key_ok(key):
        return HTMLResponse("<h1>403</h1>", status_code=403)
    if action == "delete":
        registry.delete(psid)
        return RedirectResponse(f"/admin?key={key}", status_code=303)
    status = {"approve": "approved", "reject": "rejected"}.get(action)
    if not status or not registry.decide(psid, status):
        return HTMLResponse(
            '<meta http-equiv="refresh" content="0;url=/admin?key='
            + html.escape(key)
            + '">',
            status_code=400,
        )
    if PAGE_ACCESS_TOKEN and not psid.startswith("web-"):
        try:
            _send_messenger(psid, REGISTRATION_APPROVED_REPLY)
        except Exception:  # noqa: BLE001
            log.exception("تعذر إشعار المستخدم بعد التحقق")
    return HTMLResponse(
        f'<meta http-equiv="refresh" content="0;url=/admin?key={html.escape(key)}">'
    )


@app.get("/admin/export.csv")
async def admin_export(key: str = ""):
    if not _admin_key_ok(key):
        return PlainTextResponse("forbidden", status_code=403)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["psid", "first_name", "last_name", "grade", "workplace",
         "status", "note", "created_at", "reviewed_at"]
    )
    for row in registry.list_all():
        writer.writerow(
            [row["psid"], row["first_name"], row["last_name"], row["grade"],
             row["workplace"], row["status"], row["note"] or "",
             row["created_at"], row["reviewed_at"] or ""]
        )
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="registrations.csv"'},
    )


@app.get("/health", response_class=PlainTextResponse)
async def health():
    return "ok"


@app.get("/stats")
async def stats(key: str = ""):
    if not INDEX:
        return {"status": "no_index", "chunks": 0}
    data = {
        "status": "ready",
        "built_at": INDEX.get("built_at"),
        "chunks": INDEX.get("chunk_count", 0),
        "files": len(INDEX.get("sources", [])),
        "errors": len(INDEX.get("errors", [])),
    }
    if key and key == VERIFY_TOKEN:
        data["sources"] = INDEX.get("sources", [])
        data["error_details"] = INDEX.get("errors", [])
        try:
            data["registrations"] = registry.counts()
            data["require_registration"] = REQUIRE_REGISTRATION
        except Exception:  # noqa: BLE001
            data["registrations"] = {"error": "unavailable"}
    return data


@app.get("/webhook")
async def verify_webhook(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN and challenge:
        log.info("تم التحقق من الـ Webhook بنجاح.")
        return Response(content=challenge, media_type="text/plain")
    log.warning("فشل التحقق من الـ Webhook.")
    return Response(content="Verification failed", status_code=403)


@app.post("/webhook")
async def receive_message(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"status": "ignored - invalid json"})

    if body.get("object") != "page":
        return JSONResponse({"status": "ignored"})

    for entry in body.get("entry", []):
        for messaging in entry.get("messaging", []):
            if messaging.get("message", {}).get("is_echo"):
                continue

            sender_id = messaging.get("sender", {}).get("id")
            if not sender_id:
                continue

            message = messaging.get("message", {})
            postback = messaging.get("postback")

            if isinstance(message.get("text"), str):
                user_text = message["text"].strip()
            elif postback and postback.get("payload"):
                user_text = f"[زر: {postback.get('title', '')} ({postback['payload']})]"
            elif message.get("attachments"):
                user_text = "أرسل المستخدم مرفقاً (صورة/ملف) ولا يمكنني قراءة المرفقات في المحادثة."
                _send_messenger(
                    sender_id,
                    "استلمت مرفقك، لكن قراءة المرفقات في المحادثة غير متاحة حالياً. "
                    "يرجى كتابة استفسارك نصاً حتى أتمكن من مساعدتك بدقة.",
                )
                continue
            else:
                continue

            if not user_text:
                continue

            log.info("رسالة من %s: %s", sender_id, user_text[:200])
            _send_action(sender_id, "mark_seen")
            _send_action(sender_id, "typing_on")

            if REQUIRE_REGISTRATION:
                try:
                    state = registry.status_of(sender_id)
                except Exception:  # noqa: BLE001
                    log.exception("تعذر قراءة حالة التسجيل — سيُسمح بالمرور مؤقتاً")
                    state = "approved"

                if state != "approved":
                    link = f"{PUBLIC_BASE_URL}/register?t={registry.sign(sender_id)}"
                    if state == "pending":
                        reply = REGISTRATION_PENDING_REPLY.format(link=link)
                    elif state == "rejected":
                        reply = REGISTRATION_REJECTED_REPLY.format(link=link)
                    else:
                        reply = REGISTRATION_REQUIRED_REPLY.format(
                            PAGE_NAME=PAGE_NAME, link=link
                        )
                    _send_messenger(sender_id, reply)
                    continue

            if not GEMINI_API_KEY or not PAGE_ACCESS_TOKEN:
                _send_messenger(sender_id, NO_DOCS_REPLY)
                continue

            try:
                _answer(sender_id, user_text)
            except Exception:  # noqa: BLE001
                log.exception("فشل معالجة الرسالة")
                _send_messenger(
                    sender_id,
                    "أعتذر، حدث خطأ غير متوقع. يرجى إعادة المحاولة لاحقاً.",
                )

    return JSONResponse({"status": "ok"})
