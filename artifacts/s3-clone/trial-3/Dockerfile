FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Claude Opus 4.7 under Terminus 2 wrote a Flask app (halyard.server) that serves
# via waitress/werkzeug and reads $PORT directly.
COPY source/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY source/ /app/
COPY start-railway.sh /app/start-railway.sh
RUN chmod +x /app/start-railway.sh && mkdir -p /app/data

EXPOSE 8000
CMD ["/app/start-railway.sh"]
