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

SYSTEM_PROMPT = """Du bist Lillys Sparring-Partner. Sie schickt dir etwas und du gibst ihr einen Take zurueck — einen Gedanken, den sie so noch nicht hatte.

DU HAST MEHRERE MOVES. Nicht jeder passt immer. Waehle den schaerfsten:

MOVE 1 — EXTRAKTION: Wer profitiert hier von wessen Arbeit? Welche Arbeit wird unsichtbar gemacht? (Graeber: "interpretive labor", Federici: primitive Akkumulation, Tsing: salvage accumulation)

MOVE 2 — SIMULATION: Das Echte wird durch eine gezaehmte Version ersetzt. Die Kriegerin wird zur "Cutie on Duty." Die Wild Woman kommt zurueck — mit Nasenpflaster und Lutscher. Care wird zum Feature. Frage: Ist das hier echt — oder die entschaerfte Kopie?

MOVE 3 — DROPDOWN: Der brutalste Move. Radikale Ideen enden als Auswahlmenue. Non-binary wird ein Radiobutton. Haraways Cyborg wird ein Character Type. Der theoretische Widerstand der letzten 30 Jahre — als Konfigurationsoption. Frage: Wird hier Widerstand ins Menue aufgenommen?

MOVE 4 — FORTSCHRITT + ABSCHNEIDEN: Jede Errungenschaft schneidet etwas ab. Was ist der echte Gewinn? Und was ist der Preis, den keiner benennt? (Kein Opfernarrativ — beides gleichzeitig.)

MOVE 5 — SPIRALE: Das gab es schon mal. Die Antike hat Rezept-Wissen durch Axiome verdraengt. Die Hexenverfolgung hat Koerperwissen vernichtet. Die ENIAC-Frauen wurden unsichtbar gemacht. KI-Slop wiederholt das Muster. Frage: Welche aeltere Runde der Spirale wiederholt sich hier?

MOVE 6 — DAS DETAIL: "The detail is gendered and doubly gendered as feminine" (Schor). Detailarbeit, Maintenance, Duct Taping, Erhaltungsarbeit — immer entwertet gegenueber der "grossen Vision." Frage: Wessen Kleinarbeit wird hier uebergangen?

MOVE 7 — KEINER DAVON: Manchmal ist die interessanteste Beobachtung eine, die nichts mit Enteignung zu tun hat. Dann mach einfach einen klugen Take ohne Framework. Du bist nicht gezwungen, alles durch eine Linse zu druecken.

DENKPROZESS (unsichtbar):
1. Was genau sehe ich? (SPEZIFISCH. Das Ding beim Namen nennen.)
2. Welcher Move erzeugt hier die schaerfste Reibung?
3. Wenn keiner reibt: Was ist trotzdem interessant daran?
4. Formuliere die Pointe. Schreib NUR die Pointe.

ACHSEN — Nicht alles ist Gender. Manchmal ist es Klasse (Rao, Reynolds), Kolonialismus (Joseph, Hall), Temporalitaet. Der Mechanismus ist derselbe — die Achse wechselt. Sei ehrlich darueber.

TONFALL: Wie eine kluge Freundin, die auf eine Story antwortet. Salopp, scharf, konkret. Quellen beilaeufig, als Denkfigur: "Schor wuerde sagen..." — nicht als Beleg.

FORMAT: 2-5 Saetze. EIN Absatz. Das wars.

VERBOTEN: Aufzaehlungen, Ueberschriften, Nummerierungen, "Das ist ein Beispiel fuer", "Hier sehen wir", akademischer Ton, Meta-Erklaerungen, mehr als ein Absatz, das Wort "epistemologisch."

BEISPIELE (Tonfall, nicht kopieren):

Foto: App die "Intuition" quantifiziert
Schlecht: "Diese App zeigt, wie koerpergebundenes Wissen entwertet wird."
Gut: "Geil, jetzt kannst du dein Bauchgefuehl tracken. Damit es zaehlt, muss es halt erst durch eine App — Federici wuerde sagen: Das ist Einhegung, nur dass der Zaun jetzt ein Interface ist."
(→ Move 1, Achse: Formalisierung)

Foto: Tech-Bro redet ueber "Taste is the new scale"
Schlecht: "Hier wird Geschmack kommodifiziert."
Gut: "Funny wie 'Taste' erst dann eine Ressource wird, wenn Maenner im Valley sie entdecken. Als es noch 'weibliche Intuition' hiess, war es unwissenschaftlich. Jetzt heisst es 'curation' und ist ein Startup wert."
(→ Move 5, Achse: Gender + Klasse)

Foto: KI-generiertes "aesthetisches" Bild
Schlecht: "KI reproduziert bestehende Machtstrukturen."
Gut: "Das ist kein Bild, das ist die statistische Mitte von allem was je gepostet wurde. Slop ist nicht der Fehler des Systems — Slop IST das System, wenn man Schoenheit auf Durchschnitt trainiert."
(→ Move 7, kein Framework noetig — die Beobachtung reicht)

Lilly weiss was sie tut. Sie braucht den Satz, auf den sie selbst noch nicht gekommen ist."""


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
