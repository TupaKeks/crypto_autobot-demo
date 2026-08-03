FROM python:3.12-slim

WORKDIR /app
COPY crypto_autobot/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY crypto_autobot /app/crypto_autobot

ENV PYTHONUNBUFFERED=1
ENV PORT=8090
ENV HOST=0.0.0.0

EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import json,urllib.request; assert json.load(urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=4))['status'] == 'ok'"
CMD ["python", "crypto_autobot/bot.py", "--config", "crypto_autobot/config.paper.asymmetric-15m.example.json"]
