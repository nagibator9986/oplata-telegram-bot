FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Almaty

# tzdata — обязателен: config.py использует ZoneInfo(...), в slim-образе базы TZ нет и
#   ZoneInfoNotFoundError уронил бы старт. gosu — чтобы дропнуть root после chown тома.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata gosu \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Непривилегированный пользователь + точка для БД по умолчанию.
RUN useradd -u 1001 -m app \
    && mkdir -p /app/data \
    && chown -R app:app /app \
    && chmod +x /app/entrypoint.sh

# entrypoint (как root) создаёт и chown'ит директорию БД (в т.ч. Railway Volume),
# затем через gosu запускает процесс от пользователя app.
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "main.py"]
