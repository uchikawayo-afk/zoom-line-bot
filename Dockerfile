FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
# zoneinfo が Asia/Tokyo を引けるように tzdata を入れる
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .

# LINEのwebhookは短時間で返す必要があるため worker は増やさずスレッドで捌く
CMD exec gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 30
