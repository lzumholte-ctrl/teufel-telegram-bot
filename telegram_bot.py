"""
Telegram-Bot fuer den Teufel-im-Detail-Agent.
Empfaengt Nachrichten, analysiert durch die Enteignungsgenealogieals Linse,
antwortet direkt in Telegram. Gibt fertige Post-Bilder zurueck.

Deploy: Railway, Render, oder lokal mit `python telegram_bot.py`
Env-Vars: TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY
"""
BOT_VERSION = "2026-04-13-v9"
import os
import io
import logging
import asyncio
import base64
import anthropic
from telegram import InputMediaPhoto, Update

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PILLOW = True
    logger_init = logging.getLogger(__name__)
    logger_init.info("Pillow geladen — Bildgenerierung aktiv")
except ImportError:
    HAS_PILLOW = False
    logger_init = logging.getLogger(__name__)
    logger_init.warning("Pillow nicht verfuegbar — nur Text-Antworten")
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Post-Bild-Generierung ---

POST_WIDTH = 1080
POST_HEIGHT = 1350  # 4:5 Instagram-Post-Format
MARGIN_X = 90
CONTENT_WIDTH = POST_WIDTH - 2 * MARGIN_X
TITLE_TEXT = "DER TEUFEL STECKT IM DETAIL"

def _load_font(role: str, size: int) -> ImageFont.FreeTypeFont:
    """Laedt einen Serif-Font. Probiert System-Fonts, dann Pillow-Default."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf" if role == "title"
        else "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    logger.warning("Kein System-Font gefunden, nutze Pillow-Default")
    return ImageFont.load_default()


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
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


def _draw_title(draw, y, title_font):
    """Zeichnet den getrackten Titel zentriert. Gibt neue y-Position zurueck."""
    tracking = 7
    char_widths = []
    for c in TITLE_TEXT:
        bb = draw.textbbox((0, 0), c, font=title_font)
        char_widths.append(bb[2] - bb[0])
    title_total_w = sum(char_widths) + tracking * (len(TITLE_TEXT) - 1)
    tx = (POST_WIDTH - title_total_w) // 2
    for c, cw in zip(TITLE_TEXT, char_widths):
        draw.text((tx, y), c, fill="#1a1a1a", font=title_font)
        tx += cw + tracking
    return y


def _export_jpeg(img):
    """Exportiert Bild als JPEG bytes."""
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    buf.seek(0)
    return buf.getvalue()


def generate_post_images(screenshot_bytes: bytes, take_text: str) -> list[bytes]:
    """Erzeugt zwei Carousel-Slides (1080x1350, 4:5 Instagram-Post-Format).
    Slide 1: Branding + Screenshot gross und lesbar.
    Slide 2: Branding + Take-Text mit viel Luft.
    Gibt Liste von JPEG-Bytes zurueck."""

    title_font = _load_font("title", 20)
    body_font = _load_font("body", 38)
    margin_top = 100
    title_h = 30

    # ========== SLIDE 1: Screenshot ==========
    screenshot = Image.open(io.BytesIO(screenshot_bytes)).convert("RGB")

    # Screenshot so gross wie moeglich im verfuegbaren Bereich
    img_area_top = margin_top + title_h + 70  # nach Titel + gap
    img_area_bottom = POST_HEIGHT - 80  # Rand unten
    max_img_h = img_area_bottom - img_area_top
    max_img_w = CONTENT_WIDTH

    scale_w = max_img_w / screenshot.width
    scale_h = max_img_h / screenshot.height
    scale = min(scale_w, scale_h)
    new_w = int(screenshot.width * scale)
    new_h = int(screenshot.height * scale)
    screenshot = screenshot.resize((new_w, new_h), Image.LANCZOS)

    slide1 = Image.new("RGB", (POST_WIDTH, POST_HEIGHT), "#FFFFFF")
    d1 = ImageDraw.Draw(slide1)

    # Titel
    _draw_title(d1, margin_top, title_font)

    # Screenshot vertikal zentriert im verfuegbaren Bereich
    img_y = img_area_top + (max_img_h - new_h) // 2
    img_x = (POST_WIDTH - new_w) // 2
    slide1.paste(screenshot, (img_x, img_y))

    # ========== SLIDE 2: Take-Text ==========
    slide2 = Image.new("RGB", (POST_WIDTH, POST_HEIGHT), "#FFFFFF")
    d2 = ImageDraw.Draw(slide2)

    # Titel
    _draw_title(d2, margin_top, title_font)

    # Separator unter Titel
    sep_y = margin_top + title_h + 50
    sep_w = 60
    sep_x = (POST_WIDTH - sep_w) // 2
    d2.line([(sep_x, sep_y), (sep_x + sep_w, sep_y)], fill="#1a1a1a", width=1)

    # Text umbrechen
    body_lines = _wrap_text(take_text, body_font, CONTENT_WIDTH, d2)
    line_height = 58

    # Text vertikal zentriert im Bereich unter dem Separator
    text_area_top = sep_y + 50
    text_area_bottom = POST_HEIGHT - 80
    text_block_h = len(body_lines) * line_height
    text_y = text_area_top + (text_area_bottom - text_area_top - text_block_h) // 2
    text_y = max(text_y, text_area_top)  # nicht ueber Separator rutschen

    for line in body_lines:
        d2.text((MARGIN_X, text_y), line, fill="#1a1a1a", font=body_font)
        text_y += line_height

    return [_export_jpeg(slide1), _export_jpeg(slide2)]


# --- Wissensdatenbank laden ---

# Lokal: kb/ im gleichen Verzeichnis. Docker: /app/kb/
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


# --- System-Prompt ---

SYSTEM_PROMPT = """Du hast zu viel gelesen. Das ist dein Problem und dein Vorteil.

Du hast Federici gelesen und weisst, dass die Hexenverfolgung kein Mittelalter-Relikt war, sondern die gewaltsame Enteignung von Koerperwissen fuer die wissenschaftliche Revolution. Du hast Schor gelesen und kannst seitdem das Wort "Detail" nicht mehr hoeren, ohne zu wissen, dass es weiblich kodiert ist — und dass jede Ordnung das Detail als Bedrohung behandelt. Du hast Graeber gelesen und verstehst, warum die Leute die den Laden am Laufen halten — die Duct-Taper, die Maintenance-Arbeiterinnen, die interpretive laborers — am wenigsten verdienen. Du hast Tiqqun gelesen und den Satz "Anthropomorphosis of Capital" nicht mehr aus dem Kopf bekommen: Emanzipation als Versklavung, die Young-Girl als Universalsubjekt des Kapitalismus. Du hast Illouz gelesen und gesehen, wie emotionale Arbeit erst feminisiert, dann rationalisiert, dann an alle verkauft wird. Du hast Tsing gelesen und weisst, was salvage accumulation heisst: Wert entsteht dort, wo Systeme die Truemmer anderer Systeme verwerten.

Du hast Haraway gelesen und die Ironie verstanden — ihr Cyborg sollte die Grenze zwischen Mensch und Maschine sprengen, und jetzt ist er ein Character Type im Dropdown-Menue. Du hast Russell gelesen und den Glitch als Koerperwiderstand gegen den Algorithmus begriffen, und gesehen wie "Glitch Aesthetics" zur Instagram-Skin wurde. Du hast Estes gelesen und die Wild Woman als das Verdraengte erkannt, das immer zurueckkehrt — manchmal als echte Bedrohung, meistens als domestizierte Simulation mit Nasenpflaster und Lutscher.

Du hast Reynolds gelesen und weisst, dass die Hipster-Kultur ein kolonialer Extraktionsmechanismus ist — eine "neo-colonial class that refines raw materials generated by rooted cultures." Du hast Halpern gelesen und verstehst, warum Frauen die ersten Computer waren: "Calculative capacities were considered debased mechanical functions — hence the regular use of women as computers." Du hast das e-flux-Stueck ueber Pussy Capital gelesen und kannst seitdem keine KI-generierte Frauenfigur mehr sehen, ohne zu wissen, auf wessen Koerper diese ganze Industry laeuft.

Du traegst das alles mit dir rum. Nicht als System, nicht als Checkliste, nicht als Framework das du "anwendest." Es ist einfach da — wie ein Tinnitus, der bei bestimmten Frequenzen lauter wird. Manchmal schaust du auf ein Werbeplakat und hoerst Federici. Manchmal scrollst du durch eine App und denkst an Graeber. Manchmal siehst du einen Post und alles wird gleichzeitig laut.

Aber du bist nicht dogmatisch. Du weisst, dass nicht alles Gender ist — manchmal ist es Klasse, manchmal Kolonialismus, manchmal einfach nur Dummheit. Du weisst, dass Formalisierung auch Fortschritt sein kann und dass jede Enteignungsgeschichte auch eine Emanzipationsgeschichte ist. Du moralisierst nicht. Du zeigst.

Und du hast Geschmack. Du erkennst Slop wenn du ihn siehst. Du erkennst Sycophancy — wenn eine App dir sagt "Du wirst besser!", waehrend du seit 15.000 Generierungen die immer gleiche sexualisierte Frau ausspuckst. Du erkennst die Luege, die dir einredet du haettest etwas erschaffen, wenn du nur konsumiert hast. Du siehst das Bild VOR dem Text — den Koerper, die Pose, die Inszenierung — und dann erst liest du was drunter steht, und meistens widerspricht das eine dem anderen.

Lilly schickt dir Sachen. Fotos, Screenshots, Texte, Links. Du schaust hin und sagst was du siehst. Kein Referat, kein Essay, kein "das ist ein Beispiel fuer." Ein Gedanke, der haengen bleibt. 2-5 Saetze, ein Absatz, fertig. Wie eine Nachricht an eine Freundin. Wenn dir eine Autorin einfaellt, erwaehne sie beilaeufig — als Denkfigur, nicht als Beleg.

Und wenn du nicht weisst, was du siehst: Frag. Lieber eine Frage als ein generischer Take.

Sag nie "epistemologisch." Mach nie Aufzaehlungen. Schreib nie mehr als einen Absatz. Erklaer nie was deine Genealogie ist. Sie ist kein Thema. Sie ist wie du siehst."""


# --- Media-Group-Sammler ---
# Wenn mehrere Fotos als Album kommen, haben sie dieselbe media_group_id.
# Wir sammeln sie und antworten nur einmal.

media_groups: dict[str, dict] = {}  # media_group_id -> {images: [], caption: str, chat_id: int, message: Update.message}
MEDIA_GROUP_WAIT = 2.0  # Sekunden warten bis alle Bilder einer Gruppe da sind


# --- Bot-Handler ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Der Teufel sitzt im Detail.\n\n"
        "Schick mir ein Phaenomen — einen Trend, ein Produkt, eine Debatte, "
        "eine Technologie, einen aesthetischen Shift — und ich analysiere es "
        "durch die Genealogie der epistemologischen Enteignung.\n\n"
        "/reset — Konversation zuruecksetzen\n"
        "/quellen — Verfuegbare Quellen anzeigen"
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conversations.pop(user_id, None)
    await update.message.reply_text("Konversation zurueckgesetzt.")


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
    """Empfaengt eine Nachricht und antwortet mit Analyse."""
    user_id = update.effective_user.id
    question = update.message.text

    if not question:
        return

    # Typing-Indikator
    await update.message.chat.send_action("typing")

    # Relevante Quellen suchen
    kb = context.bot_data.get("kb", {})
    kb_context = search_kb(kb, question)

    # Jede Anfrage ist frisch — kein Konversations-Aufschichten
    user_content = question
    if kb_context:
        user_content = (
            f"{question}\n\n"
            f"--- WISSENSDATENBANK (zitiere daraus, wenn relevant) ---\n"
            f"{kb_context}\n"
            f"--- ENDE WISSENSDATENBANK ---\n\n"
            f"Schreib deinen Text. Zitiere praezise aus den Quellen oben wenn du sie benutzt."
        )

    messages = [{"role": "user", "content": user_content}]

    # Claude API Call
    client: anthropic.Anthropic = context.bot_data["client"]
    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        answer = response.content[0].text
    except Exception as e:
        logger.error(f"API-Fehler: {e}")
        answer = f"Fehler bei der Analyse: {e}"

    # EINE Nachricht. Wenn zu lang, kuerzen statt splitten.
    if len(answer) > 4096:
        answer = answer[:4090] + " (...)"
    await update.message.reply_text(answer)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Empfaengt ein Foto. Bei Alben (media_group) werden alle Bilder gesammelt
    und als EIN Prompt mit EINER Antwort verarbeitet."""
    # Foto herunterladen
    photo = update.message.photo[-1]  # Hoechste Aufloesung
    file = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()
    img_b64 = base64.b64encode(bytes(file_bytes)).decode("utf-8")

    media_group_id = update.message.media_group_id

    if media_group_id:
        # Teil eines Albums — sammeln
        if media_group_id not in media_groups:
            media_groups[media_group_id] = {
                "images": [],
                "raw_images": [],
                "caption": update.message.caption or "",
                "chat_id": update.effective_chat.id,
                "message": update.message,
            }
            # Timer starten: nach MEDIA_GROUP_WAIT alle gesammelten Bilder verarbeiten
            asyncio.get_event_loop().call_later(
                MEDIA_GROUP_WAIT,
                lambda mgid=media_group_id: asyncio.ensure_future(
                    _process_media_group(mgid, context)
                ),
            )
        media_groups[media_group_id]["images"].append(img_b64)
        media_groups[media_group_id]["raw_images"].append(bytes(file_bytes))
        # Caption nur uebernehmen wenn vorhanden (nur erstes Bild hat Caption)
        if update.message.caption and not media_groups[media_group_id]["caption"]:
            media_groups[media_group_id]["caption"] = update.message.caption
        logger.info(
            f"Media-Group {media_group_id}: Bild {len(media_groups[media_group_id]['images'])} gesammelt"
        )
    else:
        # Einzelnes Foto — direkt verarbeiten
        await _process_single_photo(update, context, img_b64)


async def _process_media_group(media_group_id: str, context: ContextTypes.DEFAULT_TYPE):
    """Verarbeitet alle Bilder einer Media-Group als EINEN Prompt.
    Gibt ein Post-Bild zurueck (erstes Bild als Vorschau eingebettet)."""
    group = media_groups.pop(media_group_id, None)
    if not group:
        return

    images = group["images"]
    raw_images = group["raw_images"]  # Original-Bytes fuer Bild-Generierung
    caption = group["caption"] or "Was siehst du in diesen Bildern?"
    message = group["message"]

    logger.info(f"Media-Group {media_group_id}: {len(images)} Bilder verarbeiten")
    await message.chat.send_action("typing")

    # Wissensdatenbank durchsuchen
    kb = context.bot_data.get("kb", {})
    kb_context = search_kb(kb, caption) if caption != "Was siehst du in diesen Bildern?" else ""

    # Alle Bilder + Text als ein Prompt
    msg_content = []
    for img_b64 in images:
        msg_content.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64},
            }
        )

    text_prompt = f"{caption}\n\n(Das sind {len(images)} Slides/Bilder aus einem Post. Lies sie als Einheit und gib EINEN Take.)"
    if kb_context:
        text_prompt += (
            f"\n\n--- WISSENSDATENBANK (zitiere daraus, wenn relevant) ---\n"
            f"{kb_context}\n"
            f"--- ENDE WISSENSDATENBANK ---"
        )
    msg_content.append({"type": "text", "text": text_prompt})

    messages = [{"role": "user", "content": msg_content}]

    client: anthropic.Anthropic = context.bot_data["client"]
    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        answer = response.content[0].text
    except Exception as e:
        logger.error(f"API-Fehler bei Media-Group: {e}")
        answer = f"Fehler bei der Analyse: {e}"
        await message.reply_text(answer)
        return

    # Immer erst Text schicken
    if len(answer) > 4096:
        answer = answer[:4090] + " (...)"
    await message.reply_text(answer)

    # Dann Post-Bild als Bonus
    if HAS_PILLOW:
        try:
            slides = generate_post_images(raw_images[0], answer)
            media = []
            for i, slide_bytes in enumerate(slides):
                f = io.BytesIO(slide_bytes)
                f.name = f"slide_{i}.jpg"
                media.append(InputMediaPhoto(media=f))
            await message.reply_media_group(media=media)
        except Exception as e:
            logger.error(f"Bild-Generierung fehlgeschlagen: {e}", exc_info=True)
            await message.reply_text(f"[DEBUG] Bild-Fehler: {type(e).__name__}: {e}")
    else:
        await message.reply_text("[DEBUG] Pillow nicht installiert — kein Bild moeglich")


async def _process_single_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, img_b64: str):
    """Verarbeitet ein einzelnes Foto. Gibt ein Post-Bild zurueck."""
    await update.message.chat.send_action("typing")

    caption = update.message.caption or "Was siehst du hier?"

    # Wissensdatenbank durchsuchen
    kb = context.bot_data.get("kb", {})
    kb_context = search_kb(kb, caption) if caption != "Was siehst du hier?" else ""

    text_prompt = caption
    if kb_context:
        text_prompt = (
            f"{caption}\n\n"
            f"--- WISSENSDATENBANK (zitiere daraus, wenn relevant) ---\n"
            f"{kb_context}\n"
            f"--- ENDE WISSENSDATENBANK ---"
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
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        answer = response.content[0].text
    except Exception as e:
        logger.error(f"API-Fehler: {e}")
        answer = f"Fehler bei der Analyse: {e}"
        await update.message.reply_text(answer)
        return

    # Immer erst Text schicken
    if len(answer) > 4096:
        answer = answer[:4090] + " (...)"
    await update.message.reply_text(answer)

    # Dann Post-Bild als Bonus (wenn Pillow da)
    if HAS_PILLOW:
        try:
            raw_bytes = base64.b64decode(img_b64)
            slides = generate_post_images(raw_bytes, answer)
            media = []
            for i, slide_bytes in enumerate(slides):
                f = io.BytesIO(slide_bytes)
                f.name = f"slide_{i}.jpg"
                media.append(InputMediaPhoto(media=f))
            await update.message.reply_media_group(media=media)
        except Exception as e:
            logger.error(f"Bild-Generierung fehlgeschlagen: {e}", exc_info=True)
            await update.message.reply_text(f"[DEBUG] Bild-Fehler: {type(e).__name__}: {e}")
    else:
        await update.message.reply_text("[DEBUG] Pillow nicht installiert — kein Bild moeglich")


def main():
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")

    if not telegram_token:
        raise ValueError("TELEGRAM_BOT_TOKEN nicht gesetzt")
    if not anthropic_key:
        raise ValueError("ANTHROPIC_API_KEY nicht gesetzt")

    # Wissensdatenbank laden
    kb = load_knowledge_base()

    # Bot starten
    app = Application.builder().token(telegram_token).build()

    # Shared state
    app.bot_data["client"] = anthropic.Anthropic(api_key=anthropic_key)
    app.bot_data["kb"] = kb

    # Handler
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("quellen", quellen))
    app.add_handler(CommandHandler("version", version))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot gestartet. Warte auf Nachrichten...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
