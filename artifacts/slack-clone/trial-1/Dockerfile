FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN pip install --no-cache-dir aiohttp

COPY source/ /app/
COPY start-railway.sh /app/start-railway.sh
RUN chmod +x /app/start-railway.sh && mkdir -p /app/data/files /app/data/uploads

EXPOSE 8000
CMD ["/app/start-railway.sh"]
