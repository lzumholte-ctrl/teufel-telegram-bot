FROM python:3.12-slim

WORKDIR /app

ARG CACHEBUST=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && python -c "from PIL import Image; print('Pillow OK')"

COPY telegram_bot.py .
COPY kb/ kb/

CMD ["python", "telegram_bot.py"]
