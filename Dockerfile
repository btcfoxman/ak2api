FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates chromium ffmpeg xauth xvfb \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY main.py .

RUN mkdir -p /app/data/ak-chrome-profiles

EXPOSE 8795

CMD ["sh", "-c", "Xvfb :99 -screen 0 1280x960x24 -nolisten tcp >/tmp/xvfb.log 2>&1 & export DISPLAY=:99; sleep 1; exec uvicorn app.main:app --host 0.0.0.0 --port 8795"]
