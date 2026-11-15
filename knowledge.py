"""
قاعدة معرفة "لجنة المشاركة المحلية لبريد تبسة"
=================================================
تستخرج النصوص من كل الملفات داخل مجلد reference/ (PDF, DOCX, TXT/MD/CSV,
وصور JPG/PNG عبر Gemini Vision)، تقسّمها إلى مقاطع، تبني فهرساً محلياً
وتسترجعه عند ورود سؤال.

الاستخدام:
    python ingest.py                # بناء الفهرس
    from knowledge import ensure_index, retrieve
"""

from __future__ import annotations

import base64
import json
import logging
import io
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger("kb")

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    """تحميل مبسط لملف .env للتشغيل المحلي (Render يضبط المتغيرات بنفسه)."""
    path = BASE_DIR / ".env"
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except OSError:
        pass


_load_dotenv()

REFERENCE_DIR = Path(os.environ.get("REFERENCE_DIR", str(BASE_DIR / "reference")))
INDEX_PATH = Path(os.environ.get("KB_INDEX", str(BASE_DIR / "knowledge_index.json")))
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
OCR_MODEL = os.environ.get("OCR_MODEL", "gemini-3.5-flash-lite")
GEMINI_FALLBACK_MODELS = [
    name.strip()
    for name in os.environ.get(
        "GEMINI_FALLBACK_MODELS",
        "gemini-3.5-flash-lite,gemini-3.1-flash-lite,"
        "gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash",
    ).split(",")
    if name.strip()
]

TEXT_EXT = {".txt", ".md", ".csv", ".json", ".html", ".htm"}
PDF_EXT = {".pdf"}
DOCX_EXT = {".docx"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff"}
SUPPORTED_EXT = TEXT_EXT | PDF_EXT | DOCX_EXT | IMAGE_EXT

CHUNK_SIZE = 900
CHUNK_OVERLAP = 150
MAX_OCR_TOKENS = 4096

_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")
_NON_WORD = re.compile(r"[^\w\u0621-\u064A\u0671-\u06D3]+", re.UNICODE)

STOPWORDS = {
    # عربية
    "في", "من", "على", "الى", "إلى", "عن", "مع", "هو", "هي", "هم", "انه", "أنه",
    "ان", "أن", "او", "أو", "و", "ثم", "قد", "كان", "كانت", "هذا", "هذه", "ذلك",
    "تلك", "التي", "الذي", "الذين", "ما", "ماذا", "كيف", "متى", "اين", "أين", "هل",
    "لماذا", "لم", "لا", "نعم", "كل", "بعض", "اي", "أي", "ايضا", "أيضا", "حتى",
    "بين", "عند", "لدى", "لكن", "بل", "اذا", "إذا", "فقط", "تم", "يتم", "يكون",
    "تكون", "عليه", "عليها", "لها", "له", "لهم", "بها", "به", "منه", "منها",
    "وقد", "وهو", "وهي", "كما", "حيث", "إن", "وأن", "هناك", "بشان", "بشأن",
    "كم", "ماهو", "ماهي", "ممكن", "اذكر", "اخبرني", "لو", "سمحت", "فضلك",
    "اريد", "أريد", "احتاج", "أحتاج", "من فضلك", "برك",
    # فرنسية/إنجليزية
    "le", "la", "les", "des", "de", "du", "un", "une", "et", "ou", "au", "aux",
    "en", "pour", "par", "sur", "dans", "que", "qui", "est", "sont", "the", "of",
    "and", "to", "in", "for", "on", "is", "are", "what", "how", "when", "where",
    "why", "ce", "cette", "avec", "vous", "nous", "je", "il", "elle",
}

IMAGE_MIME = {    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
}


# ---------------------------------------------------------------- نصوص عربية
def normalize(text: str) -> str:
    """توحيد شكل الحروف العربية لتحسين المطابقة (بدون تغيير المعنى)."""
    text = _DIACRITICS.sub("", text)
    text = re.sub("[إأآٱ]", "ا", text)
    text = text.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    text = _NON_WORD.sub(" ", text.lower())
    return " ".join(text.split())


_STOPWORDS = {normalize(word) for word in STOPWORDS}


def tokenize(text: str) -> list[str]:
    """تقطيع + إزالة كلمات الوقف + تجذيع خفيف للسوابق العربية."""
    tokens: list[str] = []
    for raw in normalize(text).split():
        if len(raw) <= 1 or raw in _STOPWORDS:
            continue
        word = raw
        if len(word) > 4:
            for prefix in ("وال", "بال", "كال", "فال", "ال", "لل"):
                if word.startswith(prefix) and len(word) - len(prefix) >= 3:
                    word = word[len(prefix):]
                    break
        if len(word) <= 1 or word in _STOPWORDS:
            continue
        tokens.append(word)
    return tokens


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """تقسيم النص إلى مقاطع مع تداخل بسيط للحفاظ على السياق."""
    text = (text or "").strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
        current = ""

    for para in paragraphs:
        if len(current) + len(para) + 1 <= size:
            current = f"{current}\n{para}".strip()
            continue
        flush()
        if len(para) <= size:
            current = para
            continue
        sentences = re.split(r"(?<=[.!?؟])\s+", para)
        for sentence in sentences:
            if len(current) + len(sentence) + 1 <= size:
                current = f"{current} {sentence}".strip()
                continue
            flush()
            while len(sentence) > size:
                chunks.append(sentence[:size])
                sentence = sentence[size - overlap:]
            current = sentence
    flush()
    return chunks


# ------------------------------------------------------------ استخراج الملفات
def _extract_pdf(path: Path) -> list[tuple[str, str]]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages: list[tuple[int, str]] = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            log.warning("تعذر قراءة صفحة %s في %s: %s", i, path.name, exc)
            text = ""
        pages.append((i, text))

    scanned = [i for i, text in pages if len(text.strip()) < 30]
    if scanned and os.environ.get("PDF_OCR", "1") != "0":
        limit = int(os.environ.get("PDF_OCR_MAX_PAGES", "30"))
        if len(scanned) > limit:
            log.warning(
                "%s: %d صفحة تحتاج قراءة بصرية، سيتم اقتصارها على %d",
                path.name, len(scanned), limit,
            )
            scanned = scanned[:limit]
        log.info("%s: قراءة بصرية لـ %d صفحة (PDF ممسوح)", path.name, len(scanned))
        ocr_results = _render_and_ocr_pdf(path, scanned)
        for index, text in pages:
            if index in ocr_results:
                pages[index - 1] = (index, ocr_results[index])

    return [(text, f"صفحة {i}") for i, text in pages if text.strip()]


def _render_and_ocr_pdf(path: Path, page_numbers: list[int]) -> dict[int, str]:
    """يرسم صفحات PDF كصور ويقرأها عبر Gemini (للمستندات الممسوحة)."""
    results: dict[int, str] = {}
    try:
        import pypdfium2 as pdfium
    except ImportError:
        log.warning("pypdfium2 غير مثبت — تخطي القراءة البصرية لـ PDF")
        return results

    document = pdfium.PdfDocument(str(path))
    try:
        for number in page_numbers:
            try:
                bitmap = document[number - 1].render(scale=1.6)
                image = bitmap.to_pil().convert("RGB")

                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                results[number] = _gemini_ocr_bytes(
                    buffer.getvalue(), "image/png", f"{path.name} صفحة {number}"
                )
                log.info("قراءة بصرية ناجحة: %s صفحة %d", path.name, number)
            except Exception as exc:  # noqa: BLE001
                log.warning("فشل OCR لصفحة %d في %s: %s", number, path.name, exc)
            time.sleep(10)
    finally:
        document.close()
    return results


def _extract_docx(path: Path) -> list[tuple[str, str]]:
    import docx

    document = docx.Document(str(path))
    parts = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return [("\n".join(parts), "المستند")] if parts else []


def _gemini_ocr(path: Path) -> str:
    mime = IMAGE_MIME.get(path.suffix.lower(), "image/jpeg")
    return _gemini_ocr_bytes(path.read_bytes(), mime, path.name)


def gemini_generate(
    parts: list[dict],
    *,
    max_tokens: int,
    temperature: float = 0.15,
    model: str | None = None,
    label: str = "gemini",
) -> str:
    """يولّد نصاً عبر Gemini، وينتقل تلقائياً لموديل بديل عند نفاد الحصة اليومية."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY غير مضبوط")

    chain: list[str] = []
    for candidate in [model, GEMINI_MODEL, *GEMINI_FALLBACK_MODELS]:
        if candidate and candidate not in chain:
            chain.append(candidate)

    last_error: Exception | None = None
    for name in chain:
        for thinking in (0, None):
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{name}:generateContent"
            )
            config: dict = {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
            }
            if thinking == 0:
                config["thinkingConfig"] = {"thinkingBudget": 0}
            payload = {"contents": [{"parts": parts}], "generationConfig": config}
            try:
                res = requests.post(
                    url,
                    json=payload,
                    headers={"x-goog-api-key": GEMINI_API_KEY},
                    timeout=180,
                )
            except requests.RequestException as exc:
                last_error = exc
                break
            if res.status_code == 400 and thinking == 0:
                continue
            if res.status_code in (404, 429, 503):
                last_error = RuntimeError(
                    f"{name}: {res.status_code} {res.text[:160]}"
                )
                log.warning(
                    "الموديل %s غير متاح (%s) — تجربة الموديل التالي",
                    name,
                    res.status_code,
                )
                break
            res.raise_for_status()
            data = res.json()
            try:
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
            except (KeyError, IndexError) as exc:
                raise RuntimeError(
                    f"رد غير متوقع من Gemini ({label}): {data}"
                ) from exc

    raise RuntimeError(f"فشل توليد النص عبر Gemini ({label}): {last_error}")


def _gemini_ocr_bytes(data: bytes, mime: str, label: str) -> str:
    """قراءة بصرية لأي صورة (ملف صورة أو صفحة PDF مرسومة)."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY غير مضبوط — لا يمكن قراءة الصور")
    encoded = base64.b64encode(data).decode("ascii")
    parts = [
        {
            "text": (
                "استخرج كل النصوص الظاهرة في هذه الصورة كما هي دون أي تعليق "
                "أو إضافة. حافظ على ترتيب الأسطر والعناوين والجداول، "
                "وبنفس لغة الوثيقة (عربية/فرنسية). "
                "إذا لم توجد نصوص اكتب: لا يوجد نص."
            )
        },
        {"inline_data": {"mime_type": mime, "data": encoded}},
    ]
    text = gemini_generate(
        parts,
        max_tokens=MAX_OCR_TOKENS,
        temperature=0,
        model=OCR_MODEL,
        label=label,
    )
    return re.sub(r"^\s*لا يوجد نص\.?\s*$", "", text)


def extract_segments(path: Path) -> list[tuple[str, str]]:
    """يعيد قائمة (نص، موضع) لأي ملف مدعوم."""
    suffix = path.suffix.lower()
    if suffix in PDF_EXT:
        return _extract_pdf(path)
    if suffix in DOCX_EXT:
        return _extract_docx(path)
    if suffix in TEXT_EXT:
        return [(path.read_text(encoding="utf-8", errors="ignore"), "النص")]
    if suffix in IMAGE_EXT:
        text = _gemini_ocr(path)
        return [(text, "صورة")] if text.strip() else []
    raise RuntimeError(f"صيغة غير مدعومة: {suffix}")


# -------------------------------------------------------------------- الفهرسة
def iter_reference_files(reference_dir: Path = REFERENCE_DIR) -> list[Path]:
    if not reference_dir.exists():
        return []
    return sorted(
        p
        for p in reference_dir.rglob("*")
        if p.is_file()
        and p.suffix.lower() in SUPPORTED_EXT
        and not p.name.startswith(".")
        and not p.name.lower().startswith("readme")
    )


def build_index(reference_dir: Path = REFERENCE_DIR, index_path: Path = INDEX_PATH) -> dict:
    files = iter_reference_files(reference_dir)
    chunks: list[dict] = []
    sources: list[dict] = []
    errors: list[dict] = []

    for path in files:
        rel = str(path.relative_to(reference_dir))
        try:
            segments = extract_segments(path)
        except Exception as exc:  # noqa: BLE001
            log.warning("فشل استخراج %s: %s", rel, exc)
            errors.append({"source": rel, "error": str(exc)})
            continue
        count = 0
        for text, locator in segments:
            for piece in chunk_text(text):
                chunks.append(
                    {
                        "source": rel,
                        "locator": locator,
                        "text": piece,
                        "tokens": tokenize(piece),
                    }
                )
                count += 1
        sources.append({"source": rel, "chunks": count})
        log.info("تمت فهرسة %s (%d مقطع)", rel, count)

    df: dict[str, int] = {}
    for chunk in chunks:
        for token in set(chunk["tokens"]):
            df[token] = df.get(token, 0) + 1

    index = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "model": GEMINI_MODEL,
        "reference_dir": str(reference_dir),
        "sources": sources,
        "errors": errors,
        "chunk_count": len(chunks),
        "avg_len": (sum(len(c["tokens"]) for c in chunks) / len(chunks)) if chunks else 0,
        "df": df,
        "chunks": chunks,
    }

    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    return index


def load_index(index_path: Path = INDEX_PATH) -> dict | None:
    try:
        return json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def index_is_stale(reference_dir: Path = REFERENCE_DIR, index_path: Path = INDEX_PATH) -> bool:
    index = load_index(index_path)
    if not index:
        return True
    try:
        built_at = datetime.fromisoformat(index["built_at"])
    except (KeyError, ValueError):
        return True
    for path in iter_reference_files(reference_dir):
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if mtime > built_at:
            return True
    return False


def ensure_index(
    reference_dir: Path = REFERENCE_DIR, index_path: Path = INDEX_PATH
) -> dict | None:
    """يبني الفهرس إن كان مفقوداً أو قديماً، ويعيد الفهرس المحمّل."""
    if index_is_stale(reference_dir, index_path):
        log.info("بناء قاعدة المعرفة من %s ...", reference_dir)
        return build_index(reference_dir, index_path)
    return load_index(index_path)


def select_context(
    query: str,
    index: dict | None,
    top_k: int = 5,
    min_coverage: float = 0.55,
    small_corpus: int = 10,
) -> list[dict]:
    """يختار المقاطع التي يراها النموذج.

    - مطابقة قوية → أفضل المقاطع.
    - مجموعة وثائق صغيرة (≤ small_corpus) → يمرّر الجميع ليحكم النموذج بنفسه.
    - خلاف ذلك → لا سياق (تحويل بشري بدل التخمين).
    """
    if not index or not index.get("chunks"):
        return []

    hits = retrieve(query, index, top_k)
    if hits and hits[0]["coverage"] >= min_coverage:
        return hits

    chunks = index["chunks"]
    if len(chunks) <= small_corpus:
        ranked = list(hits)
        seen = {(h["source"], h["locator"], h["text"][:60]) for h in ranked}
        for chunk in chunks:
            key = (chunk["source"], chunk.get("locator", ""), chunk["text"][:60])
            if key in seen:
                continue
            ranked.append(
                {
                    "score": 0.0,
                    "coverage": 0.0,
                    "source": chunk["source"],
                    "locator": chunk.get("locator", ""),
                    "text": chunk["text"],
                }
            )
        return ranked[: max(top_k, len(chunks))]

    return []


# ------------------------------------------------------------------ الاسترجاع
def retrieve(query: str, index: dict | None, top_k: int = 5) -> list[dict]:
    """BM25 للترتيب + معيار تغطية الكلمات للثقة (يعمل حتى مع مستند واحد)."""
    if not index or not index.get("chunks"):
        return []
    query_tokens = list(dict.fromkeys(tokenize(query)))
    if not query_tokens:
        return []

    chunks = index["chunks"]
    df = index.get("df", {})
    total = len(chunks)
    avg_len = index.get("avg_len") or (
        sum(len(c["tokens"]) for c in chunks) / total if total else 1
    ) or 1
    k1, b = 1.5, 0.75

    weights = {
        token: 1.0 + math.log(1 + (total - df.get(token, 0) + 0.5) / (df.get(token, 0) + 0.5))
        for token in query_tokens
    }
    total_weight = sum(weights.values()) or 1.0

    scored: list[tuple[float, dict, float]] = []
    for chunk in chunks:
        tokens = chunk.get("tokens") or []
        if not tokens:
            continue
        tf: dict[str, int] = {}
        for token in tokens:
            tf[token] = tf.get(token, 0) + 1

        score = 0.0
        matched_weight = 0.0
        for token in query_tokens:
            freq = tf.get(token)
            if not freq:
                continue
            matched_weight += weights[token]
            doc_freq = df.get(token, 0)
            idf = math.log(1 + (total - doc_freq + 0.5) / (doc_freq + 0.5))
            denom = freq + k1 * (1 - b + b * len(tokens) / avg_len)
            score += idf * freq * (k1 + 1) / denom

        if matched_weight <= 0:
            continue
        coverage = matched_weight / total_weight
        scored.append((score, chunk, coverage))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        {
            "score": round(score, 3),
            "coverage": round(coverage, 3),
            "source": chunk["source"],
            "locator": chunk.get("locator", ""),
            "text": chunk["text"],
        }
        for score, chunk, coverage in scored[:top_k]
    ]
