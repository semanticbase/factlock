# FactLock on Cloud Run
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY web ./web
COPY data ./data
# Cloud Run provides PORT; 8080 is the local default.
# Cloud Run ends TLS at Google's front end; trust its X-Forwarded-Proto so the app sees HTTPS.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --forwarded-allow-ips "*"
