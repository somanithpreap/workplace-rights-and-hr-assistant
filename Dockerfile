FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HR_DB_PATH=/data/hr.db \
    MEMORY_DB_PATH=/data/memory.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data && chmod 755 /data
EXPOSE 8000

CMD ["sh", "-c", "if [ ! -f \"$HR_DB_PATH\" ]; then python seed_db.py; fi; exec uvicorn server:app --host 0.0.0.0 --port 8000"]
