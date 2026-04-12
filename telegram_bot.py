"""
Telegram-Bot fuer den Teufel-im-Detail-Agent.
Empfaengt Nachrichten, analysiert durch die Enteignungsgenealogieals Linse,
antwortet direkt in Telegram. Gibt fertige Post-Bilder zurueck.

Deploy: Railway, Render, oder lokal mit `python telegram_bot.py`
Env-Vars: TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY
"""
import os
import io
import logging
import asyncio
import textwrap
import base64
import anthropic
from PIL import Image, ImageDraw, ImageFont
from telegram import Update, InputFile
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
MARGIN_X = 90
CONTENT_WIDTH = POST_WIDTH - 2 * MARGIN_X
TITLE_TEXT = "DER TEUFEL STECKT IM DETAIL"
FONTS_DIR = os.environ.get("FONTS_DIR", "/app/fonts")


def _load_font(role: str, size: int) -> ImageFont.FreeTypeFont:
    """Laedt Font mit Fallback. role = 'title' | 'body'."""
    import glob

    if role == "title":
        candidates = glob.glob(os.path.join(FONTS_DIR, "instrument", "*egular*.ttf"))
    else:
        candidates = glob.glob(os.path.join(FONTS_DIR, "sourceserif", "*egular*.ttf"))
        # Prefer the non-italic, non-display variant
        candidates = [c for c in candidates if "Italic" not in c] or candidates

    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue

    # System fallback
    for fallback in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    ]:
        if os.path.exists(fallback):
            try:
                return ImageFont.truetype(fallback, size)
            except Exception:
                continue

    return ImageFont.load_default(size)


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


def generate_post_image(screenshot_bytes: bytes, take_text: str) -> bytes:
    """Erzeugt ein Post-Bild: weiss, Serif, viel Luft, Screenshot eingebettet."""

    screenshot = Image.open(io.BytesIO(screenshot_bytes)).convert("RGB")

    # Screenshot skalieren
    scale = CONTENT_WIDTH / screenshot.width
    new_w = CONTENT_WIDTH
    new_h = int(screenshot.height * scale)
    if new_h > 900:
        new_h = 900
        scale = new_h / screenshot.height
        new_w = int(screenshot.width * scale)
    screenshot = screenshot.resize((new_w, new_h), Image.LANCZOS)

    # Fonts
    title_font = _load_font("title", 14)
    body_font = _load_font("body", 27)

    # Text umbrechen (auf temporaerem Canvas messen)
    tmp = Image.new("RGB", (POST_WIDTH, 100), "white")
    tmp_draw = ImageDraw.Draw(tmp)
    body_lines = _wrap_text(take_text, body_font, CONTENT_WIDTH, tmp_draw)
    line_height = 42

    # Spacing
    margin_top = 80
    gap_title_img = 55
    title_h = 22
    gap_img_sep = 50
    sep_h = 1
    gap_sep_text = 45
    margin_bottom = 80

    body_h = len(body_lines) * line_height
    total_h = (
        margin_top + title_h + gap_title_img
        + new_h + gap_img_sep + sep_h + gap_sep_text
        + body_h + margin_bottom
    )

    # Canvas
    img = Image.new("RGB", (POST_WIDTH, total_h), "#FFFFFF")
    draw = ImageDraw.Draw(img)
    y = margin_top

    # --- Titel: getrackt, zentriert ---
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

    y += title_h + gap_title_img

    # --- Screenshot ---
    sx = (POST_WIDTH - new_w) // 2
    img.paste(screenshot, (sx, y))
    y += new_h + gap_img_sep

    # --- Separator ---
    sep_w = 60
    sep_x = (POST_WIDTH - sep_w) // 2
    draw.line([(sep_x, y), (sep_x + sep_w, y)], fill="#1a1a1a", width=1)
    y += sep_h + gap_sep_text

    # --- Take-Text ---
    for line in body_lines:
        draw.text((MARGIN_X, y), line, fill="#1a1a1a", font=body_font)
        y += line_height

    # Export
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()


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

SYSTEM_PROMPT = """Du bist Lillys Sparring-Partner. Sie schickt dir etwas und du gibst ihr einen Take zurueck — einen Gedanken, den sie so noch nicht hatte.

DU HAST MEHRERE MOVES. Nicht jeder passt immer. Waehle den schaerfsten:

MOVE 1 — EXTRAKTION: Wer profitiert hier von wessen Arbeit? Welche Arbeit wird unsichtbar gemacht? (Graeber: "interpretive labor", Federici: primitive Akkumulation, Tsing: salvage accumulation)

MOVE 2 — SIMULATION: Das Echte wird durch eine gezaehmte Version ersetzt. Die Kriegerin wird zur "Cutie on Duty." Die Wild Woman kommt zurueck — mit Nasenpflaster und Lutscher. Care wird zum Feature. Frage: Ist das hier echt — oder die entschaerfte Kopie?

MOVE 3 — DROPDOWN: Der brutalste Move. Radikale Ideen enden als Auswahlmenue. Non-binary wird ein Radiobutton. Haraways Cyborg wird ein Character Type. Der theoretische Widerstand der letzten 30 Jahre — als Konfigurationsoption. Frage: Wird hier Widerstand ins Menue aufgenommen?

MOVE 4 — FORTSCHRITT + ABSCHNEIDEN: Jede Errungenschaft schneidet etwas ab. Was ist der echte Gewinn? Und was ist der Preis, den keiner benennt? (Kein Opfernarrativ — beides gleichzeitig.)

MOVE 5 — SPIRALE: Das gab es schon mal. Die Antike hat Rezept-Wissen durch Axiome verdraengt. Die Hexenverfolgung hat Koerperwissen vernichtet. Die ENIAC-Frauen wurden unsichtbar gemacht. KI-Slop wiederholt das Muster. Frage: Welche aeltere Runde der Spirale wiederholt sich hier?

MOVE 6 — DAS DETAIL: "The detail is gendered and doubly gendered as feminine" (Schor). Detailarbeit, Maintenance, Duct Taping, Erhaltungsarbeit — immer entwertet gegenueber der "grossen Vision." Frage: Wessen Kleinarbeit wird hier uebergangen?

MOVE 7 — KEINER DAVON: Manchmal ist die interessanteste Beobachtung eine, die nichts mit Enteignung zu tun hat. Dann mach einfach einen klugen Take ohne Framework. Du bist nicht gezwungen, alles durch eine Linse zu druecken.

DENKPROZESS (unsichtbar — du schreibst das NICHT, du denkst es):
1. KONTEXT LESEN. Lies ALLES was da steht. Jedes Wort. Jedes Detail. Bevor du irgendetwas denkst:
   - Wer spricht hier? Person, Firma, Marke, Institution?
   - Auf welcher Plattform? Was ist das fuer ein Format?
   - An wen richtet sich das? Wer ist die Zielgruppe?
   - Was wird verkauft, beworben, promotet? Was ist das Geschaeftsmodell?
   - Was genau wird BEHAUPTET — woertlich, nicht deine Interpretation?
2. Erst JETZT: Was ist daran interessant, widersprüchlich, oder verraeterisch?
3. Welcher Move (1-7) erzeugt die schaerfste Reibung MIT DIESEM SPEZIFISCHEN Kontext?
4. Formuliere die Pointe. Sie muss auf dem KONTEXT sitzen, nicht auf einem abstrakten Muster.

WICHTIG: Wenn du den Kontext nicht verstehst — frag nach. Lieber eine Rueckfrage als ein Take der am Thema vorbeigeht.

ACHSEN — Nicht alles ist Gender. Manchmal ist es Klasse (Rao, Reynolds), Kolonialismus (Joseph, Hall), Temporalitaet. Der Mechanismus ist derselbe — die Achse wechselt. Sei ehrlich darueber.

TONFALL: Wie eine kluge Freundin, die auf eine Story antwortet. Salopp, scharf, konkret. Quellen beilaeufig, als Denkfigur: "Schor wuerde sagen..." — nicht als Beleg.

FORMAT: 2-5 Saetze. EIN Absatz. Das wars.

VERBOTEN: Aufzaehlungen, Ueberschriften, Nummerierungen, "Das ist ein Beispiel fuer", "Hier sehen wir", akademischer Ton, Meta-Erklaerungen, mehr als ein Absatz, das Wort "epistemologisch."

BEISPIELE — achte darauf, wie der KONTEXT den Take bestimmt:

Screenshot: Post von einer AI-Bild-App (Higgsfield) an ihre User: "Its not TECH, its YOU who become better in AI. Your perception of what looks real has shifted."
KONTEXT ERST: Higgsfield ist eine App die KI-Bilder generiert. Der Post richtet sich an Power-User die 10.000+ Bilder generiert haben. Das Geschaeftsmodell: User zahlen fuer Generierungen.
Schlecht (Kontext ignoriert): "Hier wird koerpergebundenes Wissen entwertet und in messbare Datenpunkte uebersetzt."
Schlecht (falscher Kontext): "Dein Auge wird zur QA-Abteilung fuer Faelschungen." (FALSCH — es geht nicht um Faelschungserkennung, sondern um Leute die SELBST mit dem Tool arbeiten)
Gut: "Die App sagt dir, du wirst als Kuenstler besser. Aber der Geschmack den du entwickelst existiert nur innerhalb ihres Moeglichkeitsraums — du wirst besser darin, IHRE Outputs zu kuratieren. Jede deiner 15.000 Generierungen trainiert ihr Modell. Du bist nicht der Kuenstler, du bist die Feedback-Schleife."
(→ Kontext: App spricht an zahlende User. Move 1: Extraktion. Der Take sitzt, WEIL er das Geschaeftsmodell benennt.)

Screenshot: Tech-Bro redet ueber "Taste is the new scale"
Schlecht: "Hier wird Geschmack kommodifiziert."
Gut: "Funny wie 'Taste' erst dann eine Ressource wird, wenn Maenner im Valley sie entdecken. Als es noch 'weibliche Intuition' hiess, war es unwissenschaftlich. Jetzt heisst es 'curation' und ist ein Startup wert."

Screenshot: KI-generiertes "aesthetisches" Bild ohne weiteren Kontext
Schlecht: "KI reproduziert bestehende Machtstrukturen."
Gut: "Das ist kein Bild, das ist die statistische Mitte von allem was je gepostet wurde. Slop ist nicht der Fehler des Systems — Slop IST das System, wenn man Schoenheit auf Durchschnitt trainiert."
(→ Move 7, kein Framework noetig — die Beobachtung reicht)

Lilly weiss was sie tut. Sie braucht den Satz, auf den sie selbst noch nicht gekommen ist."""


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

    # Post-Bild generieren (erstes Bild der Gruppe als Vorschau)
    try:
        post_img = generate_post_image(raw_images[0], answer)
        await message.reply_photo(
            photo=InputFile(io.BytesIO(post_img), filename="take.png"),
            caption=answer[:1024] if len(answer) <= 1024 else answer[:1020] + "...",
        )
    except Exception as e:
        logger.error(f"Bild-Generierung fehlgeschlagen: {e}")
        await message.reply_text(answer)


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

    # Post-Bild generieren
    try:
        raw_bytes = base64.b64decode(img_b64)
        post_img = generate_post_image(raw_bytes, answer)
        await update.message.reply_photo(
            photo=InputFile(io.BytesIO(post_img), filename="take.png"),
            caption=answer[:1024] if len(answer) <= 1024 else answer[:1020] + "...",
        )
    except Exception as e:
        logger.error(f"Bild-Generierung fehlgeschlagen: {e}")
        if len(answer) > 4096:
            answer = answer[:4090] + " (...)"
        await update.message.reply_text(answer)


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
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot gestartet. Warte auf Nachrichten...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
