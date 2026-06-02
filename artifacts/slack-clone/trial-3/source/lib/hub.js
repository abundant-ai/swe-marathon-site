// Cross-node event broadcast hub
// All HTTP nodes connect to this hub as WebSocket clients
// Events from any node are broadcast to all other nodes
const http = require('http');
const { WebSocketServer } = require('ws');

const HUB_PORT = parseInt(process.argv[2]) || 9000;

const server = http.createServer((req, res) => {
  res.writeHead(200);
  res.end('hub ok');
});

const wss = new WebSocketServer({ server });

const clients = new Map(); // ws -> { nodeId, alive }

wss.on('connection', (ws, req) => {
  const nodeId = clients.size; // simple incrementing id
  clients.set(ws, { nodeId, alive: true });
  console.log(`[hub] node ${nodeId} connected, total: ${clients.size}`);

  ws.on('message', (data) => {
    try {
      const msg = JSON.parse(data.toString());
      // Broadcast to all OTHER connected clients
      for (const [client, info] of clients) {
        if (client !== ws && client.readyState === 1) {
          client.send(JSON.stringify(msg));
        }
      }
    } catch (e) {
      // ignore malformed messages
    }
  });

  ws.on('close', () => {
    clients.delete(ws);
    console.log(`[hub] node ${nodeId} disconnected, total: ${clients.size}`);
  });

  ws.on('error', () => {
    clients.delete(ws);
  });

  // Send welcome
  ws.send(JSON.stringify({ type: 'welcome', nodeId }));
});

server.listen(HUB_PORT, '127.0.0.1', () => {
  console.log(`[hub] listening on 127.0.0.1:${HUB_PORT}`);
});
