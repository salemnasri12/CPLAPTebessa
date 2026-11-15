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
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import requests
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

import knowledge

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("barid-bot")

PAGE_NAME = os.environ.get("PAGE_NAME", "لجنة المشاركة المحلية لبريد تبسة")
PAGE_ACCESS_TOKEN = os.environ.get("FACEBOOK_ACCESS_TOKEN", "")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "barid_tebessa_verify_token")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
GRAPH_VERSION = os.environ.get("FB_GRAPH_VERSION", "v21.0")
MIN_COVERAGE = float(os.environ.get("KB_MIN_COVERAGE", "0.55"))
LEADS_PATH = Path(os.environ.get("LEADS_PATH", "data/leads.jsonl"))

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
    "أعتذر، هذه المعلومة غير متوفرة في الوثائق الرسمية المتاحة لديّ، "
    "ولا يجوز لي الاجتهاد في شؤون اللجنة.\n"
    "تم تسجيل استفسارك وسيتولى أحد أعضاء اللجنة متابعته معك.\n"
    "يمكنك ترك: الاسم الكامل، رقم الهاتف، وموضوع الاستفسار في رسالة واحدة لتسهيل التواصل."
)

HANDOFF_REPLY = (
    "تم تسجيل طلبك لتحويل المحادثة إلى أحد أعضاء اللجنة المكلفين. "
    "يرجى ترك الاسم الكامل ورقم الهاتف وموضوع الطلب، وسيتم التواصل معك في أقرب وقت. شكراً."
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

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 700, "temperature": 0.15},
    }
    res = requests.post(
        url, json=payload, headers={"x-goog-api-key": GEMINI_API_KEY}, timeout=60
    )
    res.raise_for_status()
    data = res.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"رد غير متوقع من Gemini: {data}") from exc


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


def _answer(sender_id: str, text: str) -> None:
    """يولّد الرد المناسب ويرسله، مع التحويل البشري عند الحاجة."""
    if _wants_human(text):
        _log_lead(sender_id, text, "طلب تحويل بشري")
        _send_messenger(sender_id, HANDOFF_REPLY)
        return

    if _is_greeting(text):
        _send_messenger(sender_id, GREETING_REPLY)
        return

    if not INDEX or not INDEX.get("chunks"):
        _send_messenger(sender_id, NO_DOCS_REPLY)
        return

    hits = knowledge.select_context(text, INDEX, min_coverage=MIN_COVERAGE)
    if not hits:
        _log_lead(sender_id, text, "لا توجد إجابة في الوثائق")
        _send_messenger(sender_id, ESCALATION_REPLY)
        return

    try:
        answer = _generate_answer(text, hits)
    except Exception:  # noqa: BLE001
        log.exception("فشل توليد الإجابة")
        _log_lead(sender_id, text, "خطأ تقني في التوليد")
        _send_messenger(
            sender_id,
            "أعتذر، حدث عطل تقني مؤقت أثناء معالجة طلبك. "
            "يرجى إعادة إرسال السؤال بعد قليل، أو ترك استفسارك وسنعاود التواصل معك.",
        )
        return

    if "INSUFFICIENT_INFORMATION" in answer:
        _log_lead(sender_id, text, "النموذج لم يجد إجابة في المقاطع")
        _send_messenger(sender_id, ESCALATION_REPLY)
        return

    for part in _split_message(answer):
        _send_messenger(sender_id, part)


# --------------------------------------------------------------------- المسارات
@app.get("/", response_class=PlainTextResponse)
async def home():
    return f"{PAGE_NAME} — المساعد الإداري يعمل 24/7 ✅"


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
