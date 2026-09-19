// Read the authentication token from stdin; never place it in argv or logs.
import fs from 'node:fs';
const { url, token } = JSON.parse(fs.readFileSync(0, 'utf8'));
const ws = new WebSocket(`${url}?token=${encodeURIComponent(token)}`);
const timer = setTimeout(() => { ws.close(); process.exit(1); }, 30000);
ws.addEventListener('open', () => { clearTimeout(timer); ws.close(); process.exit(0); });
ws.addEventListener('error', () => { clearTimeout(timer); process.exit(1); });
