FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Codex / GPT-5.5 wrote a FastAPI app (app.py) served by uvicorn.
COPY source/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY source/ /app/
COPY start-railway.sh /app/start-railway.sh
RUN chmod +x /app/start-railway.sh && mkdir -p /app/data

EXPOSE 8000
CMD ["/app/start-railway.sh"]
