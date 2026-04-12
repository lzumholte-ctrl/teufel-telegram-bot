FROM python:3.12-slim

WORKDIR /app

# Fonts herunterladen: Source Serif 4 (Body) + Instrument Serif (Title)
RUN apt-get update && apt-get install -y --no-install-recommends wget unzip fontconfig \
    && mkdir -p /app/fonts \
    && wget -q "https://fonts.google.com/download?family=Source+Serif+4" -O /tmp/sourceserif.zip \
    && unzip -j /tmp/sourceserif.zip "*.ttf" -d /app/fonts/sourceserif/ 2>/dev/null || true \
    && wget -q "https://fonts.google.com/download?family=Instrument+Serif" -O /tmp/instrument.zip \
    && unzip -j /tmp/instrument.zip "*.ttf" -d /app/fonts/instrument/ 2>/dev/null || true \
    && rm -f /tmp/*.zip \
    && apt-get purge -y wget unzip && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY telegram_bot.py .
COPY kb/ kb/

CMD ["python", "telegram_bot.py"]
