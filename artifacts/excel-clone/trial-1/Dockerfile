FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Claude Opus 4.7 (Claude Code) wrote an aiohttp server with an openpyxl-backed
# .xlsx import/export path.
RUN pip install --no-cache-dir aiohttp openpyxl

COPY source/ /app/
COPY start-railway.sh /app/start-railway.sh
RUN chmod +x /app/start-railway.sh && mkdir -p /app/data

EXPOSE 8000
CMD ["/app/start-railway.sh"]
