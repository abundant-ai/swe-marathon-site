FROM node:22-slim

ENV NODE_ENV=production
WORKDIR /app

# Install Node deps (express, multer, uuid, ws) from the agent's lockfile first
# so Docker can cache the layer independently of the app source.
COPY source/package.json source/package-lock.json /app/
RUN npm ci --omit=dev || npm install --omit=dev

COPY source/ /app/
COPY start-railway.sh /app/start-railway.sh
RUN chmod +x /app/start-railway.sh && mkdir -p /app/data

EXPOSE 8000
CMD ["/app/start-railway.sh"]
