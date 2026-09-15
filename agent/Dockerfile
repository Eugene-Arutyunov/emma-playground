FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Модели VAD и turn-detector скачиваются на этапе сборки, чтобы воркер
# стартовал сразу, а не тянул их при каждом перезапуске.
RUN python main.py download-files

# Токен-сервер запускается из этого же образа, у него в Railway
# переопределена команда старта: uvicorn server:app --host 0.0.0.0 --port 8080
CMD ["python", "main.py", "start"]
