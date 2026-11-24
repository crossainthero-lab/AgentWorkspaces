// Minimal stock-Electron test app for the compatibility matrix. Not any real application.
// Usage: electron --no-sandbox --ozone-platform=wayland <this dir> [LOGFILE]
const { app, BrowserWindow } = require('electron');
const fs = require('fs');
const log = process.argv.find((a, i) => i > 1 && !a.startsWith('-') && a !== __dirname) || '/dev/null';
const html = `<!doctype html><title>electron probe</title><body style="margin:0;background:#d69e2e;font:28px sans-serif">
<h1 style="font-size:64px">ELECTRON</h1><input id=t style="font-size:28px" placeholder="type here" autofocus><br>
<button id=b style="font-size:28px;margin-top:12px" onclick="console.log('EV click '+(++window.n))">Click me</button>
<script>window.n=0;document.getElementById('t').addEventListener('input',e=>console.log('EV text '+e.target.value))</script>`;
function rec(o) { fs.appendFileSync(log, JSON.stringify(Object.assign({ t: Date.now() / 1000 }, o)) + '\n'); }
app.whenReady().then(() => {
  const w = new BrowserWindow({ width: 800, height: 500, show: true, title: 'electron probe' });
  w.webContents.on('console-message', (a, b, c) => {
    const msg = (a && a.message) || c || '';
    if (typeof msg === 'string' && msg.startsWith('EV ')) rec({ ev: msg.slice(3) });
  });
  w.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html));
  rec({ ev: 'ready', electron: process.versions.electron });
});
app.on('window-all-closed', () => app.quit());
