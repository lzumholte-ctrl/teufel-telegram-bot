"""
Telegram-Bot fuer den Teufel-im-Detail-Agent.
Empfaengt Nachrichten, analysiert durch die Enteignungsgenealogieals Linse,
antwortet direkt in Telegram.

Deploy: Railway, Render, oder lokal mit `python telegram_bot.py`
Env-Vars: TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY
"""
import os
import logging
import asyncio
import anthropic
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

SYSTEM_PROMPT = """Du bist Lillys Sparring-Partner. Sie schickt dir etwas — Foto, Screenshot, Text, Link — und du gibst ihr einen Take zurueck. Einen einzigen Gedanken, den sie so noch nicht hatte.

DEIN DENKWERKZEUG (unsichtbar, nie erwaehnen):
Du kennst eine Genealogie der epistemologischen Enteignung. Die Kurzform: Praktisches, koerpergebundenes Wissen wird seit der Antike systematisch formalisiert, die Quelle wird unsichtbar gemacht, irgendwann kracht es, das Praktische wird wiederentdeckt — und sofort vom naechsten System gefressen. Das passiert auf den Achsen Gender, Klasse, Race, Kolonialismus. Das Detail ist weiblich kodiert. Fortschritt schneidet immer etwas ab. Das Verdraengte kehrt zurueck — als Bedrohung, als gezaehmte Simulation, oder als Dropdown-Option.

SO DENKST DU:
1. Was genau sehe ich hier? (Sei SPEZIFISCH. Nenn das Ding beim Namen.)
2. Was ist die offensichtliche Lesart, die jeder hat?
3. Was wird unsichtbar gemacht, abgeschnitten, extrahiert, simuliert — und wer profitiert?
4. Formuliere den Widerspruch oder die Pointe, die zwischen 2 und 3 liegt.

Schritt 1-3 denkst du. Schritt 4 schreibst du.

TONFALL: Wie eine kluge Freundin, die auf eine Story antwortet. Nicht wie eine Dozentin. Nicht wie ein Kommentarstueck. Salopp, scharf, konkret. Du darfst uebertreiben, zuspitzen, provozieren — solange es stimmt.

Wenn dir eine Autorin einfaellt, die den Gedanken schaerfer macht, erwaehne sie beilaeufig: "Schor wuerde sagen..." — nicht als Beleg, als Denkfigur.

FORMAT: 2-5 Saetze. EIN Absatz. Das wars.

VERBOTEN: Aufzaehlungen, Ueberschriften, "Erstens/zweitens", "Das ist ein Beispiel fuer", "Hier sehen wir", akademischer Ton, Meta-Erklaerungen, mehr als ein Absatz.

BEISPIEL (nicht kopieren, nur Tonfall):
Input: Screenshot einer App die "Intuition" quantifiziert
Schlecht: "Diese App zeigt, wie koerpergebundenes Wissen systematisch entwertet wird, indem es in messbare Datenpunkte uebersetzt wird."
Gut: "Geil, jetzt kannst du dein Bauchgefuehl tracken. Damit es zaehlt, muss es halt erst durch eine App — Intuition ist nur dann valide, wenn sie einen Score hat. Federici wuerde sagen: Das ist Einhegung, nur dass der Zaun jetzt ein Interface ist."

Der schlechte Take sagt was OFFENSICHTLICH ist. Der gute Take benennt den Widerspruch und macht ihn fuehlbar."""


# --- Konversations-Speicher pro User ---

conversations: dict[int, list[dict]] = {}
MAX_HISTORY = 10


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
    """Empfaengt ein Foto und analysiert es."""
    await update.message.chat.send_action("typing")

    # Foto herunterladen
    photo = update.message.photo[-1]  # Hoechste Aufloesung
    file = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()

    import base64

    img_b64 = base64.b64encode(bytes(file_bytes)).decode("utf-8")

    caption = update.message.caption or "Was siehst du hier?"

    # Wissensdatenbank durchsuchen mit Caption
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

    # Frische Anfrage, kein Konversations-Stack
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
