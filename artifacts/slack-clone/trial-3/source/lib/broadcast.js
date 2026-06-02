// Broadcast client - connects to hub and handles fallback
const WebSocket = require('ws');
const http = require('http');

const HUB_PORT = 9000;
const PEER_PORTS = [8000, 8001, 8002, 6700]; // Including IRC internal HTTP

class BroadcastClient {
  constructor(nodePort, handler) {
    this.nodePort = nodePort;
    this.handler = handler;
    this.ws = null;
    this.connected = false;
    this.reconnectTimer = null;
    this.peers = PEER_PORTS.filter(p => p !== nodePort);
    // Deduplication: track recently seen (channel_id, seq)
    this.seen = new Map(); // channelId -> Set of seqs
    this.connect();
  }

  connect() {
    try {
      this.ws = new WebSocket(`ws://127.0.0.1:${HUB_PORT}`);
      this.ws.on('open', () => {
        this.connected = true;
        console.log(`[broadcast] connected to hub`);
      });
      this.ws.on('message', (data) => {
        try {
          const msg = JSON.parse(data.toString());
          if (msg.type === 'welcome') {
            this.nodeId = msg.nodeId;
          } else {
            this.handleEvent(msg);
          }
        } catch (e) {}
      });
      this.ws.on('close', () => {
        this.connected = false;
        console.log(`[broadcast] hub disconnected, will retry`);
        this.scheduleReconnect();
      });
      this.ws.on('error', () => {
        this.connected = false;
        this.scheduleReconnect();
      });
    } catch (e) {
      this.scheduleReconnect();
    }
  }

  scheduleReconnect() {
    if (this.reconnectTimer) return;
    this.reconnectTimer = setInterval(() => {
      if (this.connected) {
        clearInterval(this.reconnectTimer);
        this.reconnectTimer = null;
        return;
      }
      this.connect();
    }, 2000);
  }

  // Check if we've already seen this event
  isDuplicate(event) {
    if (!event.channel_id || event.seq === undefined) return false;
    if (!this.seen.has(event.channel_id)) {
      this.seen.set(event.channel_id, new Set());
    }
    const channelSeqs = this.seen.get(event.channel_id);
    if (channelSeqs.has(event.seq)) {
      return true;
    }
    channelSeqs.add(event.seq);
    // Prune old entries (keep last 10000)
    if (channelSeqs.size > 10000) {
      const arr = Array.from(channelSeqs);
      arr.sort((a,b) => a-b);
      for (let i = 0; i < arr.length - 5000; i++) {
        channelSeqs.delete(arr[i]);
      }
    }
    return false;
  }

  handleEvent(event) {
    if (!this.isDuplicate(event)) {
      this.handler(event);
    }
  }

  // Broadcast event to other nodes (via hub + direct fallback)
  broadcast(event) {
    // Try hub first
    if (this.connected && this.ws && this.ws.readyState === 1) {
      this.ws.send(JSON.stringify(event));
    }
    // Also send directly to peers as fallback (ensures delivery even if hub is down)
    this.broadcastDirect(event);
  }

  broadcastDirect(event) {
    const data = JSON.stringify(event);
    for (const peerPort of this.peers) {
      const req = http.request({
        hostname: '127.0.0.1',
        port: peerPort,
        path: '/__internal/event',
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) },
        timeout: 1000
      }, (res) => {});
      req.on('error', () => {});
      req.write(data);
      req.end();
    }
  }
}

module.exports = BroadcastClient;
