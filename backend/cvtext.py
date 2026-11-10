"""Extract plain text from uploaded CV files (stdlib only).

- .txt  : UTF-8, falls back to Windows-1256 (common for Arabic files)
- .docx : reads word/document.xml from the zip
- .pdf  : uses pypdf if it is installed, otherwise returns ''
- .doc  : legacy binary format, not supported ('')
"""

import html
import os
import re
import zipfile

MAX_CHARS = 20000


def _from_txt(path):
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "cp1256", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return ""


def _from_docx(path):
    try:
        with zipfile.ZipFile(path) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > 20 * 1024 * 1024:  # zip-bomb guard
                return ""
            xml = z.read(info).decode("utf-8", errors="ignore")
    except (zipfile.BadZipFile, KeyError, OSError):
        return ""
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", " ", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    return html.unescape(text)


def _from_pdf(path):
    try:
        from pypdf import PdfReader  # optional dependency
    except ImportError:
        return ""
    try:
        reader = PdfReader(path)
        return "\n".join((page.extract_text() or "") for page in reader.pages[:10])
    except Exception:
        return ""


def has_pdf_reader():
    try:
        import pypdf  # noqa: F401
        return True
    except ImportError:
        return False


def extract_bytes(data, ext):
    """Same as extract_text() but from memory: the Telegram bot never writes a CV to disk."""
    import io
    text = ""
    try:
        if ext == "txt":
            for enc in ("utf-8-sig", "cp1256", "latin-1"):
                try:
                    text = data.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
        elif ext == "docx":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                info = z.getinfo("word/document.xml")
                if info.file_size <= 20 * 1024 * 1024:  # zip-bomb guard
                    xml = z.read(info).decode("utf-8", errors="ignore")
                    xml = re.sub(r"</w:p>", "\n", xml)
                    xml = re.sub(r"<w:tab/>", " ", xml)
                    text = html.unescape(re.sub(r"<[^>]+>", "", xml))
        elif ext == "pdf":
            from pypdf import PdfReader  # optional dependency
            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:10])
    except Exception:
        return ""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text[:MAX_CHARS]


def extract_text(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".txt":
        text = _from_txt(path)
    elif ext == ".docx":
        text = _from_docx(path)
    elif ext == ".pdf":
        text = _from_pdf(path)
    else:
        text = ""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text[:MAX_CHARS]
