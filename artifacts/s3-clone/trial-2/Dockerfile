FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Codex / GPT-5.5 wrote a dependency-free stdlib server (http.server + sqlite3),
# so no pip install is required.
COPY source/ /app/
COPY start-railway.sh /app/start-railway.sh
RUN chmod +x /app/start-railway.sh && mkdir -p /app/data

EXPOSE 8000
CMD ["/app/start-railway.sh"]
