FROM python:3.11-slim

WORKDIR /app

# ffmpeg нужен faster-whisper для декодирования аудио/видео
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Модель Whisper качаем ОДИН РАЗ при сборке образа, а не при каждом старте контейнера.
# Так на Render не будет "холодного" скачивания модели на каждый рестарт/деплой.
# Меняй модель здесь и в .env синхронно (LOCAL_WHISPER_MODEL).
ARG LOCAL_WHISPER_MODEL=tiny
ENV LOCAL_WHISPER_MODEL=${LOCAL_WHISPER_MODEL}
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('${LOCAL_WHISPER_MODEL}', device='cpu', compute_type='int8')"

# Render (Web Service) сам передаст переменную PORT и постучится по ней для health-check
EXPOSE 10000

CMD ["python", "bot.py"]
