FROM python:3.11-slim

WORKDIR /app

# ffmpeg нужен для конвертации аудио/видео (и Vosk, и faster-whisper)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg wget unzip \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Модель Vosk качаем ОДИН РАЗ при сборке образа (а не при каждом старте контейнера).
# Маленькая русская модель — компактная и лёгкая по памяти, для точности среднего уровня.
# Список моделей: https://alphacephei.com/vosk/models
ARG VOSK_MODEL_URL=https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip
ARG VOSK_MODEL_DIR=vosk-model-small-ru-0.22
RUN wget -q "${VOSK_MODEL_URL}" -O /tmp/vosk-model.zip \
    && unzip -q /tmp/vosk-model.zip -d /app \
    && mv "/app/${VOSK_MODEL_DIR}" /app/vosk-model \
    && rm /tmp/vosk-model.zip
ENV VOSK_MODEL_PATH=/app/vosk-model

# Render (Web Service) сам передаст переменную PORT и постучится по ней для health-check
EXPOSE 10000

CMD ["python", "bot.py"]
