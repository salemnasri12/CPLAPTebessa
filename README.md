# المساعد الإداري الآلي — لجنة المشاركة المحلية لبريد تبسة

بوت ماسنجر رسمي يجيب عن استفسارات عمال ومواطني قطاع بريد الجزائر بولاية تبسة
**حصرياً من وثائق اللجنة** (قاعدة معرفة محلية)، على مدار الساعة وبتكلفة صفر.

- الصفحة: **لجنة المشاركة المحلية لبريد تبسة**
- `Page ID`: `1418255570212031`
- الخدمة الحية: https://cplaptebessa.onrender.com
- الاستضافة: Render.com (مجاني) — الذكاء الاصطناعي: Google Gemini (مجاني)

## كيف يعمل

```
رسالة ماسنجر → سيرفر FastAPI → استرجاع المقاطع الأنسب من reference/
            → إجابة Gemini مستندة إلى المقاطع فقط → إرسال الرد
                                    ↘ لا توجد إجابة → تسجيل الاستفسار + تحويل بشري
```

## 📁 الملفات

| الملف | الوظيفة |
|---|---|
| `app.py` | سيرفر الـ Webhook والرد الفوري (شخصية إدارية رسمية) |
| `knowledge.py` | استخراج النصوص + الفهرسة + الاسترجاع |
| `ingest.py` | بناء قاعدة المعرفة يدوياً |
| `reference/` | **ضع هنا كل وثائق اللجنة** (يُقرأ تلقائياً) |
| `requirements.txt` | المكتبات |
| `render.yaml` | إعداد النشر |
| `.env.example` | قالب المتغيرات |

## 📄 الصيغ المدعومة في `reference/`

| الصيغة | ملاحظة |
|---|---|
| `.pdf` نصي | نص كل صفحة |
| `.pdf` ممسوح ضوئياً | تحويل الصفحات إلى صور + قراءة بصرية عبر Gemini تلقائياً |
| `.docx` | الفقرات + الجداول |
| `.txt` `.md` `.csv` | نص مباشر |
| `.jpg` `.png` `.webp` `.bmp` `.tiff` | قراءة بصرية (OCR) |

> `.doc` القديم غير مدعوم — احفظه بصيغة `.docx`.

## 🚀 التشغيل

### 1) ضع الوثائق
انسخ ملفات اللجنة إلى `reference/` (نظام الخدمات الاجتماعية، المنشورات، الإعلانات،
الأسئلة الشائعة…). ملفات `README` تُستثنى تلقائياً.

### 2) بناء قاعدة المعرفة محلياً
```bash
pip install -r requirements.txt
python ingest.py
```
يطبع عدد الملفات والمقاطع وأي ملف تعذّر استخراجه.

### 3) متغيرات البيئة
| المتغير | إلزامي | القيمة |
|---|---|---|
| `GEMINI_API_KEY` | ✅ | مفتاح Gemini |
| `FACEBOOK_ACCESS_TOKEN` | ✅ | توكن الصفحة الدائم (`EAA...`) |
| `VERIFY_TOKEN` | ✅ | `barid_tebessa_verify_token_2026` (أو نص تختاره) |
| `PAGE_NAME` | — | `لجنة المشاركة المحلية لبريد تبسة` |
| `GEMINI_MODEL` | — | `gemini-3.5-flash` |
| `OCR_MODEL` | — | `gemini-3.5-flash-lite` (للقراءة البصرية) |
| `GEMINI_FALLBACK_MODELS` | — | قائمة موديلات بديلة عند نفاد الحصة اليومية |
| `KB_MIN_COVERAGE` | — | `0.55` (حد ثقة الاسترجاع) |
| `PDF_OCR` | — | `1` لتفعيل قراءة PDF الممسوح |
| `PDF_OCR_MAX_PAGES` | — | `30` |

### 4) Render
- Build Command:
  ```
  pip install -r requirements.txt && python ingest.py
  ```
  (يفهرس أي وثائق جديدة عند كل نشر)
- Start Command:
  ```
  uvicorn app:app --host 0.0.0.0 --port $PORT
  ```
- Health Check Path: `/health`

### 5) ربط فيسبوك (Meta Developers)
1. تطبيق Business + منتج Messenger.
2. اربط الصفحة (`1418255570212031`) وولّد **Page Access Token دائم**
   بصلاحيات `pages_messaging` + `pages_manage_metadata` + `pages_read_engagement`.
3. Messenger ← Webhooks:
   - Callback URL: `https://cplaptebessa.onrender.com/webhook`
   - Verify Token: نفس `VERIFY_TOKEN`
   - اشترك في `messages` و`messaging_postbacks`.

> الحصة المجانية من Gemini محدودة (20 طلباً/يوم لكل موديل)؛ الكود ينتقل تلقائياً بين موديلات بديلة. للاستخدام المكثف فعّل الفوترة.
> في وضع Development يرد البوت على مدراء التطبيق فقط.
> للجمهور تحتاج App Review للصلاحيات ثم التحويل إلى Live.

### بديل: Botpress
Integrations ← Messenger ← Connect ثم ارفع نفس الوثائق إلى Knowledge Base.
هذا المشروع لا يحتاج أي توكن Botpress — **ولا تشارك توكن `bp_pat_...` مع أي جهة**.

## 🔐 الأمان

- لا تضع أي مفتاح في الكود أو الريبو — فقط `.env` محلياً أو Secrets/Env.
- الصفحة رسمية: لا تنشر محتوى آلياً غير مُراجع بشرياً.
- الريبو **عام** حالياً لأن Render المجاني يسحب الكود من رابط عام. الموجود فيه وثائق رسمية/نماذج فارغة فقط — لا ترفع أي بيانات شخصية للعمال.
- `/stats` يعرض التفاصيل فقط عند تمرير `?key=VERIFY_TOKEN`.

## ✅ فحوصات

| الرابط | المتوقع |
|---|---|
| `/health` | `ok` |
| `/stats` | عدد المقاطع والملفات |
| `/webhook?hub.mode=subscribe&hub.verify_token=...&hub.challenge=123` | `123` |

## تشغيل محلي
```bash
cp .env.example .env   # املأ المفاتيح
python ingest.py
uvicorn app:app --reload --port 8000
```
