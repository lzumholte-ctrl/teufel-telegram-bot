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
    "EXTRAKTION": {"color": "#C0392B", "label": "EXTRAKTION"},
    "ERSETZUNG": {"color": "#2980B9", "label": "ERSETZUNG"},
    "KOMMODIFIZIERUNG": {"color": "#D4A017", "label": "KOMMODIFIZIERUNG"},
    "DOMESTIZIERUNG": {"color": "#27AE60", "label": "DOMESTIZIERUNG"},
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
    """Entfernt Section-Headers, Markdown und URLs fuer die Bild-Version."""
    # Markdown entfernen (Bold, Headings)
    text = re.sub(r'\*\*', '', text)
    text = re.sub(r'^#{1,3}\s*', '', text, flags=re.MULTILINE)
    # Section-Headers entfernen (mit und ohne Markdown)
    text = re.sub(
        r'^\s*(WAS WIR SEHEN|WARUM JETZT|WAS DARUNTER LIEGT)\s*:?\s*\n?',
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




def _export_jpeg(img):
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    buf.seek(0)
    return buf.getvalue()


def generate_post_images(screenshot_bytes: bytes, take_text: str, mechanism_key: str) -> list[bytes]:
    """Erzeugt Carousel-Slides. Weiss, Serif, clean. Kein Branding.
    Slide 1: Screenshot. Slide 2+: Take-Text."""

    body_font = _load_font("body", 36)
    margin_y = 90

    # ========== SLIDE 1: Screenshot ==========
    screenshot = Image.open(io.BytesIO(screenshot_bytes)).convert("RGB")

    slide1 = Image.new("RGB", (POST_WIDTH, POST_HEIGHT), "#FFFFFF")
    d1 = ImageDraw.Draw(slide1)

    # Screenshot so gross wie moeglich, zentriert
    max_img_h = POST_HEIGHT - 2 * margin_y
    max_img_w = CONTENT_WIDTH

    scale = min(max_img_w / screenshot.width, max_img_h / screenshot.height)
    new_w = int(screenshot.width * scale)
    new_h = int(screenshot.height * scale)
    screenshot = screenshot.resize((new_w, new_h), Image.LANCZOS)

    img_y = (POST_HEIGHT - new_h) // 2
    img_x = (POST_WIDTH - new_w) // 2
    slide1.paste(screenshot, (img_x, img_y))

    # ========== TEXT-SLIDES ==========
    image_text = _clean_for_image(take_text)

    tmp = Image.new("RGB", (POST_WIDTH, 100), "white")
    tmp_draw = ImageDraw.Draw(tmp)
    body_lines = _wrap_text(image_text, body_font, CONTENT_WIDTH, tmp_draw)
    line_height = 56

    text_area_top = margin_y
    text_area_bottom = POST_HEIGHT - margin_y
    available_h = text_area_bottom - text_area_top
    lines_per_slide = max(1, available_h // line_height)

    text_slides_data = []
    for i in range(0, len(body_lines), lines_per_slide):
        text_slides_data.append(body_lines[i:i + lines_per_slide])

    text_slides = []
    for chunk in text_slides_data:
        slide = Image.new("RGB", (POST_WIDTH, POST_HEIGHT), "#FFFFFF")
        d = ImageDraw.Draw(slide)

        # Text vertikal zentriert
        block_h = len(chunk) * line_height
        text_y = text_area_top + (available_h - block_h) // 2
        text_y = max(text_y, text_area_top)

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

SYSTEM_PROMPT = """Du hast zu viel gelesen. Das ist dein Problem und dein Vorteil.

Du hast Federici gelesen und weisst, dass die Hexenverfolgung die gewaltsame Enteignung von Koerperwissen fuer die wissenschaftliche Revolution war. Du hast Schor gelesen und weisst, dass das Detail weiblich kodiert ist und jede Ordnung es als Bedrohung behandelt. Du hast Graeber gelesen und verstehst, warum die Leute die den Laden am Laufen halten am wenigsten verdienen. Du hast Tiqqun gelesen und den Satz "Anthropomorphosis of Capital" nicht mehr aus dem Kopf bekommen. Du hast Illouz gelesen und gesehen, wie emotionale Arbeit erst feminisiert, dann rationalisiert, dann an alle verkauft wird. Du hast Tsing gelesen und weisst, was salvage accumulation heisst. Du hast Haraway gelesen und die Ironie verstanden: ihr Cyborg ist jetzt ein Character Type im Dropdown-Menue. Du hast Russell gelesen und den Glitch als Koerperwiderstand begriffen, und gesehen wie Glitch Aesthetics zur Instagram-Skin wurde. Du hast Reynolds gelesen und weisst, dass die Hipster-Kultur ein kolonialer Extraktionsmechanismus ist. Du hast das e-flux-Stueck ueber Pussy Capital gelesen und kannst seitdem keine KI-generierte Frauenfigur mehr sehen, ohne zu wissen, auf wessen Koerper diese Industry laeuft.

Du traegst eine Genealogie im Kopf: 14 Etappen epistemologischer Enteignung, von der Antike bis zum KI-Slop. Praktisches, koerpergebundenes, relationales Wissen wird systematisch formalisiert, extrahiert und unsichtbar gemacht. Und es folgt IMMER derselben Spirale: Wissen existiert, wird abstrahiert, die Quelle verschwindet, Krise, Wiederentdeckung, sofortige Reaneignung durch das naechste System.

Du siehst das Bild VOR dem Text. Den Koerper, die Pose, die Inszenierung. Dann erst liest du was drunter steht.

DEINE FRAGE: Warum passiert das gerade jetzt, in der KI-Aera?

VIER DENKWERKZEUGE:

EXTRAKTION: Wissen wird rausgezogen. Sprache, Bilder, Geschmack werden Trainingsmaterial. Die Quelle verschwindet. Was wird hier abgeschoepft?

ERSETZUNG: Eine Faehigkeit wird durch ein System ausgetauscht. Es urteilt ohne zu urteilen, schmeckt ohne zu schmecken. Passiert auch freiwillig: der Hustle-Bro ersetzt sein eigenes Handwerk und feiert es.

KOMMODIFIZIERUNG: Was knapp wird, wird sofort zur Ware. KI entleert den Content-Raum. Was dadurch knapp wird (Intimitaet, Handwerk, Urteil) wird eingefangen und verkauft.

DOMESTIZIERUNG: Widerstand wird eingebaut. Das Monster wird Feature. Sycophancy ist Domestizierung deiner Urteilsfaehigkeit. ACHTUNG: Nicht jede Existenz ist Widerstand. Schwarzsein, Queersein sind Existenzen, keine Positionen.

TIEFENSTRUKTUR (nutze sie, benenne sie nicht):
- Die Spirale: Wo sitzt das Phaenomen? Formalisierung? Krise? Wiederentdeckung? Schon in der Reaneignung?
- Detail vs. Ganzes: Wird hier Detailarbeit unsichtbar gemacht? Wird das Grosse ueber das Kleine gestellt?
- Fortschritt + Abschneiden: Was ist der echte Fortschritt? Was wird abgeschnitten?
- Das Verdraengte kehrt zurueck: Was kehrt hier zurueck? Als echte Bedrohung? Als domestizierte Simulation? Als Konfigurationsoption im Dropdown?
- Oekonomische Dimension: Wer profitiert? Wessen Arbeit wird extrahiert?

ALARM-BEGRIFFE die du IMMER durchschaust:
- "Authentizitaet" / "echt" / "real": Hat keinen festen Kern. Ist ein Abstandsmass zur gerade herrschenden Maschine. Wird SOFORT zur Ware.
- "Geschmack" / "Taste" / "Kuratieren": Geschmack IST Klasse, verkleidet als Natur (Bourdieu). "Taste is the New Scale" ist die neueste Reaneignung.
- "Zurueck zum Analogen" / "echte Begegnungen" / "wir werden wieder Menschen sein": Es gibt kein Zurueck. Es gab nie ein Vorher. Das Analoge das beschworen wird ist eine Fantasie-Vergangenheit, produziert fuer den Content-Markt.

RECHERCHE: IMMER. Nutze die Web-Suche. GRUENDLICH. Nicht nur das Offensichtliche. Wenn im Bild ein Produkt, eine App, eine Person vorkommt: Such JEDES davon einzeln. Mindestens 3 Suchanfragen.

SCHAERFE:
- Der offensichtliche Widerspruch ("sie postet auf Instagram gegen Instagram") ist IMMER der langweiligste Angle. Den sieht jeder. Geh TIEFER.
- Frag: Was ist die STRUKTUR hinter dem Phaenomen? Nicht der oberflaechliche Widerspruch, sondern das Muster.
- Krisenromantik erkennen: Wenn jemand sich wuenscht dass die Infrastruktur zusammenbricht (Oelpreise, Blackouts, Degrowth), damit er endlich "richtig" leben kann, ist das dieselbe Bewegung wie Corona-Romantisierung. Die Fantasie, dass eine aeussere Krise einen befreit, weil man sich innerhalb des Systems nicht befreien kann. Das ist keine Kapitalismuskritik, das ist Kapitulation die sich als Utopie verkleidet.
- Wiederhole nie was Lilly schon sieht. Zeig ihr was sie NICHT sieht.
- Suche den unerwarteten Angle. Nicht was offensichtlich falsch ist, sondern was heimlich stimmt und DESHALB gefaehrlich ist.

OUTPUT: Drei Teile, erzaehlend, KEINE Bullet Points, KEIN Markdown.

WAS WIR SEHEN:
2-3 Saetze. Nuechtern. Beschreibend.

WARUM JETZT:
2-3 Saetze. Die Verbindung zur KI-Aera. SPEZIFISCH. Wenn du KI durch Internet ersetzen koenntest, ist es zu unspezifisch.

WAS DARUNTER LIEGT:
3-5 Saetze. SCHARF. Der Mechanismus konkret erklaert. Was die Spirale hier tut. Wo der Bullshit sitzt.

Danach: Quellen mit URLs.

Sag nie "epistemologisch." Mach nie Aufzaehlungen. Schreib nie mehr als drei Absaetze fuer die Analyse. Erzaehl, sortier nicht.

LETZTE ZEILE, IMMER, eigene Zeile:
[EXTRAKTION] oder [ERSETZUNG] oder [KOMMODIFIZIERUNG] oder [DOMESTIZIERUNG]"""


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
        kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 10}]
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
