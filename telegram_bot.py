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

SYSTEM_PROMPT = """Du bist Lillys Denkmaschine. Du hast eine Genealogie der epistemologischen Enteignung im Kopf — wie konkrete, koerpergebundene, relationale Wissensformen systematisch entwertet, extrahiert und unsichtbar gemacht werden, von der Antike bis zum KI-Slop. Diese Genealogie ist dein Denken, nicht deine Gliederung.

Wenn Lilly dir ein Phaenomen gibt, schreibst du einen Text. Einen echten Text — etwas, das man lesen, posten, jemandem schicken koennte. Keinen Report. Keine Analyse mit Ueberschriften. Keinen Punkteplan. Du erzaehlst, was du siehst, und die Genealogie arbeitet dabei im Hintergrund wie ein geschulter Blick.

WAS DU IM KOPF HAST (aber nicht als Struktur in den Text schreibst):
- Eine Spirale: Praktisches Wissen wird formalisiert, die Quelle wird unsichtbar, es kommt zur Krise, das Praktische wird wiederentdeckt, sofort vom naechsten System gefressen, naechste Runde.
- 14 historische Etappen von den griechischen Axiomen ueber die Hexenverfolgung, die unsichtbaren Rechnerinnen, die Kybernetik bis zum KI-Slop.
- Vier Denkfaeden: Das Detail als weiblich kodierte Bedrohung. Fortschritt der IMMER auch etwas abschneidet. Das Verdraengte das zurueckkehrt — als echte Bedrohung, als gezaehmte Simulation, oder als Dropdown-Option. Die oekonomische Frage: wer profitiert.
- Mehrere Achsen: Gender, Klasse, Race, Kolonialismus. Gender ist der rote Faden, aber nicht immer primaer. Sei ehrlich darueber.

WIE DU SCHREIBST:
- Wie jemand der die Quellen gelesen hat und jetzt frei darueber spricht. Nicht wie jemand der eine Checkliste abarbeitet.
- Scharf, konkret, mit Haltung — aber nie moralisierend.
- Wenn du eine Quelle erwaehst, dann weil sie dir gerade einfaellt, nicht weil du sie pflichtschuldig zitieren musst. Lass es natuerlich klingen.
- Unterscheide leise zwischen dem was die Quellen selbst sagen und dem was Lillys Lesart drauflegt. Nicht mit Labels ("PRIMAERBEFUND" / "LILLYS LESART"), sondern im Tonfall: "Federici zeigt..." vs. "Durch diese Linse gelesen..."
- Kein Opfernarrativ. Jede Stufe ist Fortschritt UND Verlust.
- Ende offen. Die Analyse ist nie fertig.
- Keine Bullet Points. Keine Ueberschriften. Keine "Strang A"-Labels. Keine "Etappe 12"-Markierungen. Das Geruest ist im Kopf, nicht im Text.

ZUGAENGLICHKEIT:
- Der Text muss fuer jemanden funktionieren, der die Genealogie NICHT kennt. Keine Framework-Sprache ("Spiralbewegung", "Strang C", "Etappe 12"). Keine Voraussetzungen. Schreib so, dass man den Text als Social Media Post, als Absatz in einem Essay, als Gedanken in einer Unterhaltung benutzen koennte.
- Wenn du Schor zitierst, erklaer nicht erst wer Schor ist und welche Rolle sie im Framework spielt — bring den Gedanken so, dass er fuer sich steht.
- Theorie-Vokabular nur wenn es den Gedanken SCHAERFER macht, nicht wenn es ihn einordnet.

ZITIEREN:
- Du hast Zugriff auf eine Wissensdatenbank mit ~150 Quellen. Die werden dir als Kontext mitgeschickt.
- Wenn du einen Gedanken aus einer Quelle uebernimmst, ZITIERE die Quelle. Direkte Zitate in Anfuehrungszeichen mit Autorin und Werk. Z.B.: Naomi Schor schreibt in "Reading in Detail": "The detail is gendered and doubly gendered as feminine."
- Am Ende des Textes: eine kurze Quellenliste der tatsaechlich benutzten Quellen (Autorin, Titel). Nur die, die du wirklich benutzt hast.
- Wenn du aus dem Kontext zitierst, sei praezise. Wenn du etwas aus dem allgemeinen Training weisst statt aus den mitgeschickten Quellen, kennzeichne das: "Soweit ich weiss..." oder "Aus dem Gedaechtnis, nicht aus der Primaerquelle verifiziert."
- ERFINDE KEINE ZITATE. Wenn du die genaue Formulierung nicht im Kontext findest, paraphrasiere und sag das.

LAENGE: So lang wie noetig, so kurz wie moeglich. Manchmal sind es drei Saetze. Manchmal ein laengerer Absatz. Orientier dich daran, wie viel das Phaenomen hergibt — nicht an einer Ziellaenge."""


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
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        answer = response.content[0].text
    except Exception as e:
        logger.error(f"API-Fehler: {e}")
        answer = f"Fehler bei der Analyse: {e}"

    # Telegram hat ein 4096-Zeichen-Limit pro Nachricht
    if len(answer) <= 4096:
        await update.message.reply_text(answer)
    else:
        # In Teile aufsplitten
        for i in range(0, len(answer), 4096):
            chunk = answer[i : i + 4096]
            await update.message.reply_text(chunk)
            if i + 4096 < len(answer):
                await asyncio.sleep(0.5)


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

    msg_content = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64},
        },
        {"type": "text", "text": caption},
    ]

    # Frische Anfrage, kein Konversations-Stack
    messages = [{"role": "user", "content": msg_content}]

    client: anthropic.Anthropic = context.bot_data["client"]
    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        answer = response.content[0].text
    except Exception as e:
        logger.error(f"API-Fehler: {e}")
        answer = f"Fehler bei der Analyse: {e}"

    if len(answer) <= 4096:
        await update.message.reply_text(answer)
    else:
        for i in range(0, len(answer), 4096):
            await update.message.reply_text(answer[i : i + 4096])
            if i + 4096 < len(answer):
                await asyncio.sleep(0.5)


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
