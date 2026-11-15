"""بناء قاعدة المعرفة من مجلد reference/.

الاستخدام:
    python ingest.py
"""

import logging
import sys

import knowledge

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")


def main() -> None:
    files = knowledge.iter_reference_files()
    if not files:
        print(f"لا توجد ملفات مدعومة داخل: {knowledge.REFERENCE_DIR}")
        print("ضع ملفات PDF / DOCX / TXT / صور ثم أعد التشغيل.")
        sys.exit(0)

    index = knowledge.build_index()

    print("\n===== نتائج الفهرسة =====")
    print(f"عدد الملفات      : {len(index['sources'])}")
    print(f"عدد المقاطع      : {index['chunk_count']}")
    print(f"حجم المفردات     : {len(index['df'])}")
    print(f"ملف الفهرس       : {knowledge.INDEX_PATH}")

    if index["errors"]:
        print("\nملفات تعذّر استخراجها:")
        for err in index["errors"]:
            print(f" - {err['source']}: {err['error']}")

    print("\nالملفات المفهرسة:")
    for item in index["sources"]:
        print(f" - {item['source']} ({item['chunks']} مقطع)")


if __name__ == "__main__":
    main()
