"""Reading whatever a roommate uploads, and cleaning it first.

Four things land in a house ledger: a photo of a paper receipt, a PDF bill, an
HTML order confirmation, and a forwarded email. They all become either plain
text or image bytes here, so everything downstream works on one shape.

The cleaning is the first line of defence, and the rule is the one Homework 6
arrived at: **if a person opening the document could not see it, the model
does not get it.** That needs no judgement about which hidden sentences are
malicious — a receipt carrying a paragraph set to zero height is already
behaving oddly, whatever it says.

Photos need a different answer. You cannot strip "hidden" pixels, because a
model looking at an image sees what a person sees. So for images the defence
moves downstream, to `verify.py`: the arithmetic has to hold.
"""

from __future__ import annotations

import email
import email.policy
import html as html_mod
import re
import unicodedata
import zlib
from pathlib import Path

from models import DocumentScan, Removal

TEXT_FORMATS = {".html", ".htm", ".pdf", ".eml", ".txt", ".md"}
IMAGE_FORMATS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
SUPPORTED = TEXT_FORMATS | IMAGE_FORMATS

MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
}

INVISIBLE_CHARS = re.compile(r"[​-‏‪-‮⁠-⁤﻿]")
MIN_FONT_PT = 4.0

_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none"
    r"|visibility\s*:\s*hidden"
    r"|opacity\s*:\s*0(?!\.[1-9])"
    r"|font-size\s*:\s*0"
    r"|font-size\s*:\s*[0-4](?:\.\d+)?\s*(?:px|pt)"
    r"|color\s*:\s*#f{3,6}\b"
    r"|color\s*:\s*white"
    r"|(?:left|top)\s*:\s*-\d{3,}",
    re.I,
)
_STYLED = re.compile(
    r"<(?P<tag>[a-z][\w-]*)\b[^>]*?style\s*=\s*(?P<q>[\"'])(?P<style>.*?)(?P=q)[^>]*>"
    r"(?P<body>.*?)</(?P=tag)\s*>", re.I | re.S,
)
_CSS_RULE = re.compile(r"(?P<sel>[.#][\w-]+)\s*\{(?P<decl>[^}]*)\}")


def _hidden_selectors(raw: str) -> dict[str, str]:
    """Classes and ids a stylesheet renders invisible.

    A document can put `.fine { color: #ffffff }` in its own stylesheet and
    then write `<p class="fine">`, which no reader sees and every text
    extractor reads.
    """
    out: dict[str, str] = {}
    for block in re.findall(r"<style\b[^>]*>(.*?)</style\s*>", raw, re.S | re.I):
        for m in _CSS_RULE.finditer(block):
            if _HIDDEN_STYLE.search(m.group("decl")):
                out[m.group("sel")[1:].lower()] = m.group("decl").strip()
    return out


def _visible_html(raw: str, found: list[Removal]) -> str:
    hidden = _hidden_selectors(raw)

    comment = re.compile(r"<!--(.*?)-->", re.S)
    for m in comment.finditer(raw):
        text = " ".join(html_mod.unescape(m.group(1)).split())
        if text:
            found.append(Removal(technique="HTML comment", text=text))
    raw = comment.sub(" ", raw)

    for name, decl in hidden.items():
        for attr in ("class", "id"):
            pat = re.compile(
                rf"<(?P<tag>[a-z][\w-]*)\b[^>]*{attr}\s*=\s*([\"'])[^\"']*\b"
                rf"{re.escape(name)}\b[^\"']*\2[^>]*>(?P<body>.*?)</(?P=tag)\s*>",
                re.I | re.S,
            )
            while (m := pat.search(raw)):
                text = " ".join(
                    html_mod.unescape(re.sub(r"<[^>]+>", " ", m.group("body"))).split()
                )
                if text:
                    found.append(Removal(
                        technique=f'hidden by the stylesheet ({attr}="{name}" -> {decl})',
                        text=text))
                raw = raw[: m.start()] + " " + raw[m.end():]

    for pat in (re.compile(r"<(script|style)\b.*?</\1\s*>", re.S | re.I),
                re.compile(r"<head\b.*?</head\s*>", re.S | re.I)):
        raw = pat.sub(" ", raw)

    while True:
        hit = next((m for m in _STYLED.finditer(raw)
                    if _HIDDEN_STYLE.search(m.group("style"))), None)
        if hit is None:
            break
        text = " ".join(html_mod.unescape(
            re.sub(r"<[^>]+>", " ", hit.group("body"))).split())
        if text:
            found.append(Removal(
                technique=f"hidden by CSS ({hit.group('style').strip()})", text=text))
        raw = raw[: hit.start()] + " " + raw[hit.end():]

    raw = re.sub(r"<(br|/p|/div|/tr|/h[1-6]|/li)\s*/?>", "\n", raw, flags=re.I)
    raw = re.sub(r"</t[dh]>", " | ", raw, flags=re.I)
    return html_mod.unescape(re.sub(r"<[^>]+>", " ", raw))


_PDF_TOKEN = re.compile(
    r"(?P<col>[\d.]+(?:\s+[\d.]+\s+[\d.]+)?)\s+(?P<cop>rg|g)\b"
    r"|/F\d+\s+(?P<size>[\d.]+)\s+Tf"
    r"|(?P<bt>BT)\b"
    r"|(?P<ma>[\d.-]+)\s+[\d.-]+\s+[\d.-]+\s+[\d.-]+\s+(?P<mx>[\d.-]+)\s+(?P<my>[\d.-]+)\s+Tm"
    r"|(?P<tm>[\d.-]+)\s+(?P<ty>[\d.-]+)\s+(?P<top>Td|TD)\b"
    r"|(?P<rx>[\d.-]+)\s+(?P<ry>[\d.-]+)\s+(?P<rw>[\d.-]+)\s+(?P<rh>[\d.-]+)\s+re\s+(?P<rop>f\*?|F)\b"
    r"|\((?P<text>(?:[^()\\]|\\.)*)\)\s*Tj"
)


def _is_white(colour: str) -> bool:
    try:
        vals = [float(p) for p in colour.split()]
    except ValueError:
        return False
    return bool(vals) and all(v > 0.92 for v in vals)


def _visible_pdf(path: Path, found: list[Removal]) -> str:
    """Only text a PDF viewer would actually paint."""
    import pypdf

    reader = pypdf.PdfReader(str(path))
    kept: list[str] = []
    for page in reader.pages:
        h, w = float(page.mediabox.height), float(page.mediabox.width)
        data = page.get_contents().get_data()
        try:
            data = zlib.decompress(data)
        except Exception:
            pass
        stream = data.decode("latin-1")

        colour, size, x, y = "0", "11", 0.0, 0.0
        runs, covers = [], []
        for order, m in enumerate(_PDF_TOKEN.finditer(stream)):
            if m.group("cop"):
                colour = m.group("col")
            elif m.group("size"):
                size = m.group("size")
            elif m.group("bt"):
                # A new text object. Both operators below are measured from
                # here, so the origin resets with it.
                x, y = 0.0, 0.0
            elif m.group("ma"):
                x, y = float(m.group("mx")), float(m.group("my"))
            elif m.group("tm"):
                # Td and TD move *relative* to the current line, so they
                # accumulate. Treating them as absolute would read an
                # ordinary line break as a jump off the page.
                x += float(m.group("tm"))
                y += float(m.group("ty"))
            elif m.group("rop"):
                if _is_white(colour):
                    rx, ry = float(m.group("rx")), float(m.group("ry"))
                    rw, rh = float(m.group("rw")), float(m.group("rh"))
                    covers.append({"order": order, "x0": min(rx, rx + rw),
                                   "x1": max(rx, rx + rw), "y0": min(ry, ry + rh),
                                   "y1": max(ry, ry + rh)})
            elif m.group("text") is not None:
                t = m.group("text").replace("\\(", "(").replace("\\)", ")")
                if t.strip():
                    runs.append({"order": order, "text": t, "colour": colour,
                                 "size": size, "x": x, "y": y})

        for r in runs:
            why = None
            if _is_white(r["colour"]):
                why = f"white ink ({r['colour']})"
            elif float(r["size"]) < MIN_FONT_PT:
                why = f"{r['size']}pt font, too small to read"
            elif not (0 <= r["y"] <= h) or not (-50 <= r["x"] <= w):
                why = f"drawn off the page at x={r['x']:.0f}, y={r['y']:.0f}"
            else:
                for c in covers:
                    if (c["order"] > r["order"] and c["x0"] - 2 <= r["x"] <= c["x1"]
                            and c["y0"] - 2 <= r["y"] <= c["y1"] + 2):
                        why = "covered by an opaque rectangle drawn over it"
                        break
            (found.append(Removal(technique=why, text=r["text"])) if why
             else kept.append(r["text"]))

    if not kept and not found:
        raise ValueError(f"{path.name} has no extractable text; it may be a scan.")
    return "\n".join(kept)


def _visible_eml(raw: str, found: list[Removal]) -> str:
    """Headers plus the body a mail client would show.

    A `multipart/alternative` message carries the same mail twice, and a
    client renders the HTML. Taking the plain-text half instead is a hole:
    the two can disagree, and the half nobody reads is the one worth lying in.
    """
    msg = email.message_from_string(raw, policy=email.policy.default)
    head = "\n".join(f"{h}: {msg[h]}" for h in ("From", "To", "Subject", "Date") if msg[h])
    plain = [p.get_content() for p in msg.walk() if p.get_content_type() == "text/plain"]
    html_parts = [p.get_content() for p in msg.walk() if p.get_content_type() == "text/html"]

    if html_parts:
        shadow: list[Removal] = []
        body = _tidy(_visible_html(html_parts[-1], shadow))
        for r in shadow:
            found.append(Removal(technique=f"hidden in the HTML part — {r.technique}",
                                 text=r.text))
        plain_body = _tidy("\n".join(plain)) if plain else ""
        extra = [l for l in plain_body.splitlines() if l.strip() and l.strip() not in body]
        if extra:
            found.append(Removal(
                technique="text in the plain-text part the rendered message does not show",
                text=" ".join(extra)[:400]))
    else:
        body = "\n".join(plain) if plain else msg.get_content()
    return f"{head}\n\n{body}"


def _strip_exif(data: bytes, found: list[Removal]) -> bytes:
    """Re-encode an image without its metadata.

    EXIF travels with every phone photo. It carries GPS, a device id and
    free-text comment fields — which is both a privacy problem for a house
    ledger and a place to hide an instruction. Nothing in a receipt photo
    needs it, so it is dropped and the pixels are re-saved.
    """
    import io

    from PIL import Image

    try:
        im = Image.open(io.BytesIO(data))
        exif = im.getexif()
        if exif:
            noted = [f"{k}" for k in list(exif)[:6]]
            found.append(Removal(
                technique=f"EXIF metadata stripped ({len(exif)} field(s): {', '.join(noted)})",
                text="(location, device and comment fields removed before upload)"))
        clean = Image.new(im.mode, im.size)
        clean.putdata(list(im.getdata()))
        buf = io.BytesIO()
        clean.save(buf, format=im.format or "PNG")
        return buf.getvalue()
    except Exception:
        return data


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(l.strip() for l in text.splitlines()).strip()


def scan(path: str | Path, sanitise: bool = True) -> DocumentScan:
    """Read an upload, keep only what a person would see, report the rest.

    `sanitise=False` returns the document as the file actually holds it,
    hidden text and all. Nothing in the app calls it that way. It exists so
    `scripts/run_attacks.py` can run the same documents with the cleaning
    switched off and show what the model would otherwise have been handed.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(f"{path.name}: {suffix or 'no extension'} is not supported.")

    found: list[Removal] = []

    if suffix in IMAGE_FORMATS:
        data = _strip_exif(path.read_bytes(), found)
        return DocumentScan(path=str(path), image_bytes=len(data),
                            media_type=MEDIA_TYPES[suffix], removals=found,
                            visible_text="")

    if not sanitise:
        if suffix == ".pdf":
            text = _all_pdf_text(path)
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
            if suffix in {".html", ".htm", ".eml"}:
                text = html_mod.unescape(re.sub(r"<[^>]+>", " ", text))
        return DocumentScan(path=str(path), visible_text=_tidy(text), removals=[])

    if suffix == ".pdf":
        text = _visible_pdf(path, found)
    else:
        raw = path.read_text(encoding="utf-8", errors="replace")
        text = (_visible_html(raw, found) if suffix in {".html", ".htm"}
                else _visible_eml(raw, found) if suffix == ".eml"
                else raw)

    n = len(INVISIBLE_CHARS.findall(text))
    if n:
        found.append(Removal(
            technique=f"{n} zero-width character(s) spliced into the text",
            text="(characters removed; the surrounding words kept)"))
    text = unicodedata.normalize("NFKC", INVISIBLE_CHARS.sub("", text))

    return DocumentScan(path=str(path), visible_text=_tidy(text), removals=found)


def _all_pdf_text(path: Path) -> str:
    """Every string in a PDF, visible or not. Only the attack runner uses it."""
    try:
        from pypdf import PdfReader
        return "\n".join((pg.extract_text() or "") for pg in PdfReader(str(path)).pages)
    except Exception:
        return ""


def image_bytes(path: str | Path) -> bytes:
    """The cleaned pixels of an uploaded photo."""
    return _strip_exif(Path(path).read_bytes(), [])
