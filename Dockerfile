FROM python:3.13-slim
WORKDIR /bot
COPY mechfight.py bot.py nav.py params.json ./
COPY brain ./brain
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
