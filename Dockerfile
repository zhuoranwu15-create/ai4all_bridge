FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY skills ./skills
COPY data/moderation/sensitive_terms.json ./data/moderation/sensitive_terms.json
COPY data/system ./data/system

EXPOSE 8180

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8180"]
