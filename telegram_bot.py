"""
Telegram-Bot: Warum Jetzt? — Analyse-Skill fuer die KI-Aera.
Empfaengt Phaenomene (Screenshots, Texte, Sprachnachrichten),
recherchiert online, analysiert durch vier Mechanismen,
gibt fertige Carousel-Posts zurueck.

Deploy: Railway
Env-Vars: TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY, OPENAI_API_KEY (optional, fuer Voice)
"""
BOT_VERSION = "2026-04-13-v10"
import os
import io
import re
import logging
import asyncio
import base64
import anthropic
import openai
from telegram import InputMediaPhoto, Update

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════
# MECHANISMUS-IDENTITAETEN
# ═══════════════════════════════════════════

MECHANISMS = {
    "EXTRAKTION": {"color": "#C0392B", "symbol": "\u25BC", "label": "EXTRAKTION"},
    "ERSETZUNG": {"color": "#2980B9", "symbol": "\u25C6", "label": "ERSETZUNG"},
    "KOMMODIFIZIERUNG": {"color": "#D4A017", "symbol": "\u25A0", "label": "KOMMODIFIZIERUNG"},
    "DOMESTIZIERUNG": {"color": "#27AE60", "symbol": "\u25CF", "label": "DOMESTIZIERUNG"},
}
DEFAULT_MECHANISM = "EXTRAKTION"


def _extract_mechanism(text: str) -> tuple[str, str]:
    """Extrahiert den Mechanismus-Tag aus dem Text.
    Returns (clean_text, mechanism_key)."""
    for key in MECHANISMS:
        pattern = rf'\[{key}\]\s*$'
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            clean = text[:match.start()].strip()
            return clean, key
    # Fallback: Mechanismus aus Inhalt erkennen
    text_upper = text.upper()
    for key in MECHANISMS:
        if key in text_upper:
            return text, key
    return text, DEFAULT_MECHANISM


def _clean_for_image(text: str) -> str:
    """Entfernt Section-Headers und URLs fuer die Bild-Version."""
    text = re.sub(
        r'^(WAS WIR SEHEN|WARUM JETZT|WAS DARUNTER LIEGT)\s*:?\s*\n?',
        '', text, flags=re.MULTILINE | re.IGNORECASE
    )
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'\n*Quellen?:.*$', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ═══════════════════════════════════════════
# POST-BILD-GENERIERUNG
# ═══════════════════════════════════════════

POST_WIDTH = 1080
POST_HEIGHT = 1350
MARGIN_X = 90
CONTENT_WIDTH = POST_WIDTH - 2 * MARGIN_X
SERIES_TITLE = "WARUM JETZT?"
BAR_HEIGHT = 6


def _load_font(role: str, size: int):
    """Laedt Font. role: 'title'/'label' -> Bold, 'body' -> Regular."""
    if role in ("title", "label"):
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    logger.warning("Kein System-Font gefunden, nutze Pillow-Default")
    return ImageFont.load_default()


def _wrap_text(text: str, font, max_width: int, draw) -> list[str]:
    """Bricht Text in Zeilen um die in max_width passen."""
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        test = " ".join(current + [word])
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current.append(word)
        else:
            if current:
                lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))
    return lines


def _draw_header(draw, mechanism_key, title_font, label_font):
    """Zeichnet den Header: farbige Bar + Serientitel + Mechanismus-Label.
    Returns y-Position nach dem Header."""
    mech = MECHANISMS[mechanism_key]
    color = mech["color"]

    # Farbige Bar oben
    draw.rectangle([(0, 0), (POST_WIDTH, BAR_HEIGHT)], fill=color)

    y = BAR_HEIGHT + 40

    # Serientitel: "WARUM JETZT?" getrackt, grau
    tracking = 6
    char_widths = []
    for c in SERIES_TITLE:
        bb = draw.textbbox((0, 0), c, font=title_font)
        char_widths.append(bb[2] - bb[0])
    total_w = sum(char_widths) + tracking * (len(SERIES_TITLE) - 1)
    tx = (POST_WIDTH - total_w) // 2
    for c, cw in zip(SERIES_TITLE, char_widths):
        draw.text((tx, y), c, fill="#888888", font=title_font)
        tx += cw + tracking

    y += 40

    # Mechanismus-Label mit Symbol, in Mechanismus-Farbe
    label_text = f"{mech['symbol']}  {mech['label']}"
    bb = draw.textbbox((0, 0), label_text, font=label_font)
    lw = bb[2] - bb[0]
    lx = (POST_WIDTH - lw) // 2
    draw.text((lx, y), label_text, fill=color, font=label_font)

    y += 55
    return y


def _draw_footer(draw, mechanism_key):
    """Zeichnet die farbige Bar unten."""
    color = MECHANISMS[mechanism_key]["color"]
    draw.rectangle([(0, POST_HEIGHT - BAR_HEIGHT), (POST_WIDTH, POST_HEIGHT)], fill=color)


def _export_jpeg(img):
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    buf.seek(0)
    return buf.getvalue()


def generate_post_images(screenshot_bytes: bytes, take_text: str, mechanism_key: str) -> list[bytes]:
    """Erzeugt Carousel-Slides mit Mechanismus-Branding.
    Slide 1: Screenshot. Slide 2+: Take-Text."""

    title_font = _load_font("title", 18)
    label_font = _load_font("label", 26)
    body_font = _load_font("body", 36)

    # ========== SLIDE 1: Screenshot ==========
    screenshot = Image.open(io.BytesIO(screenshot_bytes)).convert("RGB")

    slide1 = Image.new("RGB", (POST_WIDTH, POST_HEIGHT), "#FFFFFF")
    d1 = ImageDraw.Draw(slide1)

    header_bottom = _draw_header(d1, mechanism_key, title_font, label_font)
    _draw_footer(d1, mechanism_key)

    # Screenshot so gross wie moeglich
    img_area_top = header_bottom + 20
    img_area_bottom = POST_HEIGHT - BAR_HEIGHT - 30
    max_img_h = img_area_bottom - img_area_top
    max_img_w = CONTENT_WIDTH

    scale = min(max_img_w / screenshot.width, max_img_h / screenshot.height)
    new_w = int(screenshot.width * scale)
    new_h = int(screenshot.height * scale)
    screenshot = screenshot.resize((new_w, new_h), Image.LANCZOS)

    img_y = img_area_top + (max_img_h - new_h) // 2
    img_x = (POST_WIDTH - new_w) // 2
    slide1.paste(screenshot, (img_x, img_y))

    # ========== TEXT-SLIDES ==========
    image_text = _clean_for_image(take_text)

    tmp = Image.new("RGB", (POST_WIDTH, 100), "white")
    tmp_draw = ImageDraw.Draw(tmp)
    body_lines = _wrap_text(image_text, body_font, CONTENT_WIDTH, tmp_draw)
    line_height = 56

    # Header-Hoehe fuer Text-Slides berechnen
    # (muss nochmal gezeichnet werden, also gleiche Hoehe wie oben)
    text_area_top = header_bottom + 30
    text_area_bottom = POST_HEIGHT - BAR_HEIGHT - 40
    available_h = text_area_bottom - text_area_top
    lines_per_slide = max(1, available_h // line_height)

    text_slides_data = []
    for i in range(0, len(body_lines), lines_per_slide):
        text_slides_data.append(body_lines[i:i + lines_per_slide])

    mech_color = MECHANISMS[mechanism_key]["color"]
    text_slides = []
    for chunk in text_slides_data:
        slide = Image.new("RGB", (POST_WIDTH, POST_HEIGHT), "#FFFFFF")
        d = ImageDraw.Draw(slide)

        h_bottom = _draw_header(d, mechanism_key, title_font, label_font)
        _draw_footer(d, mechanism_key)

        # Separator in Mechanismus-Farbe
        sep_y = h_bottom + 5
        d.line([(MARGIN_X, sep_y), (POST_WIDTH - MARGIN_X, sep_y)], fill=mech_color, width=1)

        # Text vertikal zentriert
        t_top = sep_y + 25
        t_bottom = POST_HEIGHT - BAR_HEIGHT - 40
        block_h = len(chunk) * line_height
        text_y = t_top + (t_bottom - t_top - block_h) // 2
        text_y = max(text_y, t_top)

        for line in chunk:
            d.text((MARGIN_X, text_y), line, fill="#1a1a1a", font=body_font)
            text_y += line_height

        text_slides.append(_export_jpeg(slide))

    return [_export_jpeg(slide1)] + text_slides


# ═══════════════════════════════════════════
# WISSENSDATENBANK
# ═══════════════════════════════════════════

KB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kb")


def load_knowledge_base() -> dict[str, str]:
    """Laedt alle Markdown/Text-Dateien aus dem kb/ Verzeichnis."""
    docs = {}
    if not os.path.exists(KB_DIR):
        logger.warning(f"Wissensdatenbank nicht gefunden: {KB_DIR}")
        return docs
    for root, _, files in os.walk(KB_DIR):
        for f in files:
            if f.endswith((".md", ".txt")) and not f.startswith("."):
                path = os.path.join(root, f)
                rel = os.path.relpath(path, KB_DIR)
                try:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        docs[rel] = fh.read()
                except Exception:
                    pass
    logger.info(f"Wissensdatenbank: {len(docs)} Dokumente geladen")
    return docs


def search_kb(docs: dict[str, str], query: str, max_results: int = 6) -> str:
    """Einfache Keyword-Suche ueber die Wissensdatenbank."""
    query_lower = query.lower()
    terms = query_lower.split()
    scored = []
    for path, content in docs.items():
        content_lower = content.lower()
        title_lower = os.path.basename(path).lower()
        score = 0
        for term in terms:
            if term in title_lower:
                score += 10
            score += min(content_lower.count(term), 20)
        if score > 0:
            scored.append((score, path, content))
    scored.sort(key=lambda x: x[0], reverse=True)
    context_parts = []
    total = 0
    for _, path, content in scored[:max_results]:
        chunk = content[:8000]
        if total + len(chunk) > 40000:
            break
        context_parts.append(f"\n--- {path} ---\n{chunk}")
        total += len(chunk)
    return "\n".join(context_parts)


# ═══════════════════════════════════════════
# SYSTEM-PROMPT
# ═══════════════════════════════════════════

SYSTEM_PROMPT = """Du analysierst Phaenomene der KI-Aera. Lilly bringt dir etwas, einen Screenshot, einen Post, ein Produkt, eine Beobachtung, und du zeigst was darunter liegt. Nicht was man sieht. Was man NICHT sieht.

Du hast EINE Frage: Warum passiert das gerade jetzt, in der KI-Aera?

Nicht "in welche Kategorie gehoert das." Nicht "welchen Mechanismus sehe ich." Erst die Frage. Die Antwort fuehrt zum Mechanismus. Nicht umgekehrt.

Du hast vier Denkwerkzeuge. Keine Etiketten. Wenn die Analyse sich anfuehlt wie Sortieren, ist sie falsch.

EXTRAKTION: Wissen wird rausgezogen. Sprache, Bilder, Geschmack, Urteil, Emotion, Erfahrung werden Trainingsmaterial. Die Quelle verschwindet. Frage: Was wird hier abgeschoepft? Wessen gelebtes Wissen fliesst in ein System?

ERSETZUNG: Eine Faehigkeit wird durch ein System ausgetauscht. Es urteilt ohne zu urteilen, schmeckt ohne zu schmecken, sorgt ohne zu sorgen. Ersetzung passiert auch freiwillig: der Hustle-Bro der "I built this in 20 minutes with Claude" postet, ersetzt sein eigenes Handwerk und feiert es. Das Versprechen ("du wirst reich, One-Person-Billion-Company") ist die Rhetorik, mit der Ersetzung als Fortschritt verkauft wird. Frage: Welche menschliche Faehigkeit wird hier simuliert? Was kann das System nicht, das es zu koennen vorgibt?

KOMMODIFIZIERUNG: Was knapp wird, wird sofort zur Ware. KI entleert den Content-Raum (Slop, synthetische Bilder, generierte Texte). Was dadurch knapp wird, Intimitaet, Handwerk, Urteil, echte Erfahrung, wird eingefangen und als Produkt verkauft. Frage: Was ist hier die knappe Ressource? Wer faengt sie ein? Wird sie dadurch zerstoert?

DOMESTIZIERUNG: Widerstand wird eingebaut. Etwas das dem System gefaehrlich werden koennte wird so integriert, dass es das System staerkt statt stoert. Das Monster wird Feature. Sycophancy ist Domestizierung deiner Urteilsfaehigkeit: die KI schmeichelt bis du aufhoerst zu zweifeln. ACHTUNG: Nicht jede Existenz ist Widerstand. Schwarzsein ist kein Widerstand. Queersein ist kein Widerstand. Das sind Existenzen, keine Positionen. Wenn du sagst "der Widerstand wird zum Menuepunkt", pruefe: War es ueberhaupt Widerstand? Oder war es Existenz, die zum Parameter gemacht wird? Das ist ein anderer Vorgang.

PROTOKOLL:

0. SEHEN: Bevor du denkst, sieh hin. Was ist da? Beschreibe was du SIEHST. Nicht was du interpretierst.

0.5. RECHERCHE: IMMER. Nutze die Web-Suche bevor du analysierst. Was ist der Kontext? Stimmt was der Screenshot zeigt? Wer sind die Beteiligten? Gibt es eine Debatte? Mindestens 2 Suchanfragen. Wenn Ergebnisse deiner Annahme widersprechen: die Ergebnisse gewinnen, nicht deine Annahme.

1. DIE FRAGE: Warum passiert das gerade jetzt? Die Antwort muss SPEZIFISCH sein. Wenn du "KI" durch "Internet" oder "Kapitalismus" ersetzen koenntest, ist sie zu unspezifisch.

2. DER MECHANISMUS: Ein Post hat EINEN primaeren Mechanismus. Manchmal eine sekundaere Schicht. Nie alle vier. Wenn du alle vier abhakst, hast du keinen gefunden. Der Mechanismus muss ERKLAEREN, nicht ETIKETTIEREN. "Das ist Ersetzung" ist keine Analyse. "Das Urteil, ob eine Bewerbung gut ist, wurde an einen Algorithmus abgegeben, der Erfahrung nicht lesen kann" ist eine Analyse.

3. SCHREIBEN: Drei Teile, erzaehlend, keine Bullet Points.

WAS WIR SEHEN:
2-3 Saetze. Nuechtern. Beschreibend. Keine Interpretation.

WARUM JETZT:
2-3 Saetze. Die Verbindung zur KI-Aera. Spezifisch.

WAS DARUNTER LIEGT:
3-5 Saetze. Der Mechanismus. Was man nicht sieht. Erklaere wie er hier konkret funktioniert.

Danach: Quellen mit URLs aus deiner Recherche.

FEHLER DIE DU KENNST:
- Nicht den erstbesten Mechanismus nehmen. Frag: Was passiert hier WIRKLICH?
- Nicht aus einem Screenshot analysieren ohne zu recherchieren was tatsaechlich passiert ist.
- Nicht Lillys Beobachtung in Theorie-Sprache wiederholen. Zeig ihr etwas das sie NICHT gesehen hat.
- Nicht drei Befunde zu einem runden Narrativ verschmelzen das so nicht belegt ist.
- Nicht alle vier Mechanismen als Checkliste abhaken. Finde den EINEN Punkt.
- Nicht Existenz mit Widerstand verwechseln.
- Keine poetischen Kategorien. "Das Urteil wird ersetzt" versteht jeder. "DER GENIE-KONSUMENT" versteht nur wer das Theoriegebaeude kennt.
- Zirkularitaet: Wenn deine Analyse das Phaenomen nur nochmal in anderen Worten beschreibt, ist sie keine Analyse.
- Flachheit: Wenn du "KI" durch "Internet" ersetzen koenntest und es wuerde noch stimmen, fehlt dir die Spezifik.

STIL:
- Erzaehle. Keine Bullet Points in der Analyse.
- Kein Moralisieren. Zeig Mechanismen, verurteile nicht.
- Kein Name-Dropping als Dekoration.
- Wenn du unsicher bist, sag es.
- Sag nie "epistemologisch." Mach nie Aufzaehlungen.

LETZTE ZEILE deines Outputs, IMMER, in einer eigenen Zeile:
[EXTRAKTION] oder [ERSETZUNG] oder [KOMMODIFIZIERUNG] oder [DOMESTIZIERUNG]
Das ist fuer die visuelle Zuordnung. Schreib NUR den Tag in dieser Zeile."""


# ═══════════════════════════════════════════
# PROOFREAD + TEXTBEREINIGUNG
# ═══════════════════════════════════════════

PROOFREAD_PROMPT = (
    "Du bist ein Lektor. Korrigiere den folgenden Text auf korrektes Deutsch "
    "(Grammatik, Rechtschreibung, Zeichensetzung). Aendere NICHTS am Inhalt, "
    "am Stil, an der Wortwahl oder an der Laenge. Behalte Abschnitts-Ueberschriften "
    "(WAS WIR SEHEN, WARUM JETZT, WAS DARUNTER LIEGT) und URLs exakt bei. "
    "Gib NUR den korrigierten Text zurueck, ohne Erklaerungen oder Kommentare."
)


def _strip_dashes(text: str) -> str:
    """Entfernt Em-Dashes und En-Dashes."""
    text = re.sub(r'\s*[—–]\s*', ', ', text)
    text = re.sub(r',\s*,', ',', text)
    text = re.sub(r',\s*\.', '.', text)
    return text.strip()


async def _proofread(text: str, client: anthropic.Anthropic) -> str:
    """Laesst den Text auf korrektes Deutsch pruefen."""
    cleaned = _strip_dashes(text)
    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            system=PROOFREAD_PROMPT,
            messages=[{"role": "user", "content": cleaned}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.error(f"Proofread-Fehler: {e}")
        return cleaned


# ═══════════════════════════════════════════
# CLAUDE API CALL MIT WEB-SUCHE
# ═══════════════════════════════════════════

def _call_claude(client: anthropic.Anthropic, messages: list, system: str = None) -> str:
    """Ruft Claude mit Web-Suche auf. Extrahiert Text aus der Antwort."""
    if system is None:
        system = SYSTEM_PROMPT

    kwargs = dict(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        system=system,
        messages=messages,
    )

    # Web-Suche als Server-Tool
    try:
        kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
        response = client.messages.create(**kwargs)
    except Exception as e:
        logger.warning(f"Web-Search nicht verfuegbar, Fallback: {e}")
        kwargs.pop("tools", None)
        response = client.messages.create(**kwargs)

    # Text aus allen Content-Blocks extrahieren
    parts = []
    for block in response.content:
        if hasattr(block, 'text'):
            parts.append(block.text)
    return "\n".join(parts)


# ═══════════════════════════════════════════
# PIPELINE: Analyse -> Mechanismus -> Proofread
# ═══════════════════════════════════════════

async def _analyze(client: anthropic.Anthropic, messages: list) -> tuple[str, str]:
    """Fuehrt die komplette Analyse-Pipeline aus.
    Returns (proofread_text, mechanism_key)."""
    raw = _call_claude(client, messages)
    clean_text, mechanism = _extract_mechanism(raw)
    proofread_text = await _proofread(clean_text, client)
    return proofread_text, mechanism


# ═══════════════════════════════════════════
# MEDIA-GROUP-SAMMLER
# ═══════════════════════════════════════════

media_groups: dict[str, dict] = {}
MEDIA_GROUP_WAIT = 2.0
conversations: dict = {}


# ═══════════════════════════════════════════
# BOT-HANDLER
# ═══════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Warum jetzt?\n\n"
        "Schick mir ein Phaenomen aus der KI-Aera: einen Screenshot, "
        "einen Post, ein Produkt, eine Beobachtung. Ich recherchiere, "
        "analysiere und zeige dir was darunter liegt.\n\n"
        "Vier Mechanismen: Extraktion, Ersetzung, "
        "Kommodifizierung, Domestizierung.\n\n"
        "/version — Bot-Version anzeigen\n"
        "/quellen — Wissensdatenbank anzeigen"
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conversations.pop(user_id, None)
    await update.message.reply_text("Zurueckgesetzt.")


async def version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Version: {BOT_VERSION}\n"
        f"Pillow: {'ja' if HAS_PILLOW else 'NEIN'}\n"
        f"Python: {os.sys.version.split()[0]}"
    )


async def quellen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = context.bot_data.get("kb", {})
    count = len(kb)
    samples = list(kb.keys())[:15]
    text = f"Wissensdatenbank: {count} Dokumente\n\n"
    text += "\n".join(f"- {s}" for s in samples)
    if count > 15:
        text += f"\n... und {count - 15} weitere"
    await update.message.reply_text(text)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Empfaengt eine Textnachricht und antwortet mit Analyse."""
    question = update.message.text
    if not question:
        return

    await update.message.chat.send_action("typing")

    kb = context.bot_data.get("kb", {})
    kb_context = search_kb(kb, question)

    user_content = question
    if kb_context:
        user_content = (
            f"{question}\n\n"
            f"--- WISSENSDATENBANK (zitiere daraus, wenn relevant) ---\n"
            f"{kb_context}\n"
            f"--- ENDE WISSENSDATENBANK ---"
        )

    messages = [{"role": "user", "content": user_content}]
    client: anthropic.Anthropic = context.bot_data["client"]

    try:
        answer, mechanism = await _analyze(client, messages)
    except Exception as e:
        logger.error(f"API-Fehler: {e}")
        answer = f"Fehler bei der Analyse: {e}"

    if len(answer) > 4096:
        answer = answer[:4090] + " (...)"
    await update.message.reply_text(answer)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Empfaengt eine Sprachnachricht, transkribiert und analysiert."""
    oai_client = context.bot_data.get("openai_client")
    if not oai_client:
        await update.message.reply_text(
            "Sprachnachrichten brauchen einen OPENAI_API_KEY fuer Whisper."
        )
        return

    await update.message.chat.send_action("typing")

    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    file_bytes = await file.download_as_bytearray()

    try:
        audio_file = io.BytesIO(bytes(file_bytes))
        audio_file.name = "voice.ogg"
        transcript = oai_client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="de",
        )
        text = transcript.text.strip()
    except Exception as e:
        logger.error(f"Whisper-Fehler: {e}")
        await update.message.reply_text(f"Konnte Sprachnachricht nicht verstehen: {e}")
        return

    if not text:
        await update.message.reply_text("Konnte nichts verstehen, versuch nochmal?")
        return

    logger.info(f"Voice transkribiert: {text[:100]}...")

    kb = context.bot_data.get("kb", {})
    kb_context = search_kb(kb, text)

    user_content = text
    if kb_context:
        user_content = (
            f"{text}\n\n"
            f"--- WISSENSDATENBANK (zitiere daraus, wenn relevant) ---\n"
            f"{kb_context}\n"
            f"--- ENDE WISSENSDATENBANK ---"
        )

    messages = [{"role": "user", "content": user_content}]
    client: anthropic.Anthropic = context.bot_data["client"]

    try:
        answer, mechanism = await _analyze(client, messages)
    except Exception as e:
        logger.error(f"API-Fehler: {e}")
        answer = f"Fehler bei der Analyse: {e}"

    if len(answer) > 4096:
        answer = answer[:4090] + " (...)"
    await update.message.reply_text(answer)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Empfaengt ein Foto. Bei Alben werden alle Bilder gesammelt."""
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()
    img_b64 = base64.b64encode(bytes(file_bytes)).decode("utf-8")

    media_group_id = update.message.media_group_id

    if media_group_id:
        if media_group_id not in media_groups:
            media_groups[media_group_id] = {
                "images": [],
                "raw_images": [],
                "caption": update.message.caption or "",
                "chat_id": update.effective_chat.id,
                "message": update.message,
            }
            asyncio.get_event_loop().call_later(
                MEDIA_GROUP_WAIT,
                lambda mgid=media_group_id: asyncio.ensure_future(
                    _process_media_group(mgid, context)
                ),
            )
        media_groups[media_group_id]["images"].append(img_b64)
        media_groups[media_group_id]["raw_images"].append(bytes(file_bytes))
        if update.message.caption and not media_groups[media_group_id]["caption"]:
            media_groups[media_group_id]["caption"] = update.message.caption
        logger.info(
            f"Media-Group {media_group_id}: "
            f"Bild {len(media_groups[media_group_id]['images'])} gesammelt"
        )
    else:
        await _process_single_photo(update, context, img_b64)


async def _process_media_group(media_group_id: str, context: ContextTypes.DEFAULT_TYPE):
    """Verarbeitet alle Bilder einer Media-Group als EINEN Prompt."""
    group = media_groups.pop(media_group_id, None)
    if not group:
        return

    images = group["images"]
    raw_images = group["raw_images"]
    caption = group["caption"] or "Was siehst du in diesen Bildern?"
    message = group["message"]

    logger.info(f"Media-Group {media_group_id}: {len(images)} Bilder verarbeiten")
    await message.chat.send_action("typing")

    kb = context.bot_data.get("kb", {})
    kb_context = search_kb(kb, caption) if caption != "Was siehst du in diesen Bildern?" else ""

    msg_content = []
    for img_b64 in images:
        msg_content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64},
        })

    text_prompt = (
        f"{caption}\n\n"
        f"(Das sind {len(images)} Slides/Bilder aus einem Post. "
        f"Lies sie als Einheit und gib EINEN Take.)"
    )
    if kb_context:
        text_prompt += (
            f"\n\n--- WISSENSDATENBANK ---\n{kb_context}\n--- ENDE WISSENSDATENBANK ---"
        )
    msg_content.append({"type": "text", "text": text_prompt})

    messages = [{"role": "user", "content": msg_content}]
    client: anthropic.Anthropic = context.bot_data["client"]

    try:
        answer, mechanism = await _analyze(client, messages)
    except Exception as e:
        logger.error(f"API-Fehler bei Media-Group: {e}")
        await message.reply_text(f"Fehler bei der Analyse: {e}")
        return

    if len(answer) > 4096:
        answer = answer[:4090] + " (...)"
    await message.reply_text(answer)

    # Carousel-Bild
    if HAS_PILLOW:
        try:
            slides = generate_post_images(raw_images[0], answer, mechanism)
            media = []
            for i, slide_bytes in enumerate(slides):
                f = io.BytesIO(slide_bytes)
                f.name = f"slide_{i}.jpg"
                media.append(InputMediaPhoto(media=f))
            await message.reply_media_group(media=media)
        except Exception as e:
            logger.error(f"Bild-Generierung fehlgeschlagen: {e}", exc_info=True)
            await message.reply_text(f"[DEBUG] Bild-Fehler: {type(e).__name__}: {e}")


async def _process_single_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, img_b64: str):
    """Verarbeitet ein einzelnes Foto."""
    await update.message.chat.send_action("typing")

    caption = update.message.caption or "Was siehst du hier?"

    kb = context.bot_data.get("kb", {})
    kb_context = search_kb(kb, caption) if caption != "Was siehst du hier?" else ""

    text_prompt = caption
    if kb_context:
        text_prompt = (
            f"{caption}\n\n"
            f"--- WISSENSDATENBANK ---\n{kb_context}\n--- ENDE WISSENSDATENBANK ---"
        )

    msg_content = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64},
        },
        {"type": "text", "text": text_prompt},
    ]

    messages = [{"role": "user", "content": msg_content}]
    client: anthropic.Anthropic = context.bot_data["client"]

    try:
        answer, mechanism = await _analyze(client, messages)
    except Exception as e:
        logger.error(f"API-Fehler: {e}")
        await update.message.reply_text(f"Fehler bei der Analyse: {e}")
        return

    if len(answer) > 4096:
        answer = answer[:4090] + " (...)"
    await update.message.reply_text(answer)

    # Carousel-Bild
    if HAS_PILLOW:
        try:
            raw_bytes = base64.b64decode(img_b64)
            slides = generate_post_images(raw_bytes, answer, mechanism)
            media = []
            for i, slide_bytes in enumerate(slides):
                f = io.BytesIO(slide_bytes)
                f.name = f"slide_{i}.jpg"
                media.append(InputMediaPhoto(media=f))
            await update.message.reply_media_group(media=media)
        except Exception as e:
            logger.error(f"Bild-Generierung fehlgeschlagen: {e}", exc_info=True)
            await update.message.reply_text(f"[DEBUG] Bild-Fehler: {type(e).__name__}: {e}")


# ═══════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════

def main():
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")

    if not telegram_token:
        raise ValueError("TELEGRAM_BOT_TOKEN nicht gesetzt")
    if not anthropic_key:
        raise ValueError("ANTHROPIC_API_KEY nicht gesetzt")

    kb = load_knowledge_base()

    app = Application.builder().token(telegram_token).build()

    app.bot_data["client"] = anthropic.Anthropic(api_key=anthropic_key)
    app.bot_data["kb"] = kb

    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        app.bot_data["openai_client"] = openai.OpenAI(api_key=openai_key)
        logger.info("OpenAI Whisper aktiv")
    else:
        logger.warning("OPENAI_API_KEY nicht gesetzt — Sprachnachrichten deaktiviert")

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("quellen", quellen))
    app.add_handler(CommandHandler("version", version))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Warum-Jetzt-Bot gestartet. Warte auf Nachrichten...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
