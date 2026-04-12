FROM python:3.12-slim

WORKDIR /app

# Cache-bust: v8
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY telegram_bot.py .
COPY kb/ kb/

CMD ["python", "telegram_bot.py"]
