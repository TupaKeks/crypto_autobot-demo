FROM python:3.12-slim

WORKDIR /app
COPY crypto_autobot /app/crypto_autobot

ENV PYTHONUNBUFFERED=1
ENV PORT=8090
ENV HOST=0.0.0.0

EXPOSE 8090
CMD ["python", "crypto_autobot/bot.py", "--config", "crypto_autobot/config.example.json"]
