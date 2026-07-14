/*
 * AbletonMCP — Max for Live bridge (Node for Max)
 *
 * Runs inside a Max for Live device via [node.script]. It hosts a small TCP
 * server (default port 9878, mirroring the Remote Script's 9877) so the MCP
 * server can reach capabilities the Live Object Model can't provide — real
 * audio analysis and MIDI/CC generation from inside the signal path.
 *
 * Protocol (line-delimited JSON, same shape as the Remote Script):
 *   request:  {"type": <command>, "params": {...}}
 *   response: {"status": "success"|"error", "result": {...} | "message": ...}
 *
 * The device's Max patch feeds analysis values in through max-api messages
 * ("peak", "rms", "pitch", ...) and receives MIDI-out instructions back out
 * through max-api outlets ("note", ...). The file also runs standalone under
 * plain Node (max-api is mocked) so the bridge protocol can be tested without
 * Max — see test_bridge.js.
 */

'use strict';
const net = require('net');
const fs = require('fs');
const os = require('os');
const path = require('path');

// Log to a file as well as the Max console, so the bridge can be debugged from
// outside Live (the Max console isn't reachable over the socket).
const LOG_PATH = path.join(os.tmpdir(), 'abletonmcp_m4l.log');
function log(msg) {
  const line = new Date().toISOString() + ' ' + msg + '\n';
  try { fs.appendFileSync(LOG_PATH, line); } catch (e) {}
}
log('mcp_bridge.js starting (pid ' + process.pid + ', node ' + process.version + ')');
log('argv: ' + JSON.stringify(process.argv));
process.on('uncaughtException', (e) => log('uncaughtException: ' + (e && e.stack || e)));

// Each device passes its role (analysis / synth / midi) as the first script
// argument, so the MCP server can discover which device is on which port and
// route commands accordingly. A 'role' message can also set it (see below).
let role = 'unknown';
for (let i = 2; i < process.argv.length; i++) {
  const a = process.argv[i];
  if (a && a[0] !== '@' && a !== 'start') { role = a; break; }
}

// max-api is only present inside Node for Max; mock it when running standalone.
let Max;
try {
  Max = require('max-api');
  log('max-api loaded (running inside Node for Max)');
} catch (e) {
  log('max-api not found; running standalone');
  Max = {
    post: (...a) => console.log('[max.post]', ...a),
    addHandler: () => {},
    outlet: (...a) => console.log('[max.outlet]', ...a),
    MESSAGE_TYPES: {},
  };
}

// Bind the first free port in a small range so several M4L devices can run at
// once; the server scans the range and pings each to discover them.
const PORT_BASE = parseInt(process.env.MCP_M4L_PORT || '9878', 10);
const PORT_COUNT = 10;
let PORT = PORT_BASE;

// A 'role' message from the patch can set/override the role (belt-and-suspenders
// in case node.script doesn't forward argv on some Max builds).
// (Handler registered after Max is defined, below.)

// Latest analysis values pushed in from the Max patch.
const analysis = {
  peak: 0.0,        // linear peak amplitude (0..1+)
  rms: 0.0,         // linear RMS amplitude
  pitch: 0.0,       // detected fundamental in Hz (0 = none)
  centroid: 0.0,    // spectral centroid in Hz
  updated_at: 0,    // ms epoch of last update
};

// Max → Node: the patch sends "peak 0.42", "rms 0.1", "pitch 220", etc.
for (const key of ['peak', 'rms', 'pitch', 'centroid']) {
  Max.addHandler(key, (v) => {
    analysis[key] = Number(v);
    analysis.updated_at = Date.now();
  });
}
Max.addHandler('role', (r) => { role = String(r); log('role set to ' + role); });

function handle(cmd) {
  const type = cmd.type || '';
  const p = cmd.params || {};
  switch (type) {
    case 'ping':
      return {
        pong: true,
        port: PORT,
        role: role,
        capabilities: ['get_analysis', 'send_midi', 'send_cc'],
        analysis_fresh_ms: analysis.updated_at ? Date.now() - analysis.updated_at : null,
      };
    case 'get_analysis':
      return {
        peak: analysis.peak,
        rms: analysis.rms,
        pitch: analysis.pitch,
        centroid: analysis.centroid,
        age_ms: analysis.updated_at ? Date.now() - analysis.updated_at : null,
      };
    case 'send_midi': {
      const pitch = Math.max(0, Math.min(127, parseInt(p.pitch, 10)));
      const velocity = Math.max(0, Math.min(127, parseInt(p.velocity != null ? p.velocity : 100, 10)));
      const duration = Number(p.duration != null ? p.duration : 100); // ms
      const ch = Math.max(0, Math.min(15, parseInt(p.channel != null ? p.channel : 0, 10)));
      // 'note' outlet drives the synth device; 'midi' outlet (raw status/data
      // bytes) feeds the MIDI-effect device's [midiout] straight into Live.
      Max.outlet('note', pitch, velocity, duration);
      Max.outlet('midi', 0x90 + ch, pitch, velocity);            // note on
      setTimeout(() => Max.outlet('midi', 0x80 + ch, pitch, 0),  // note off
                 Math.max(1, duration));
      return { sent: true, pitch, velocity, duration, channel: ch };
    }
    case 'send_cc': {
      const controller = Math.max(0, Math.min(127, parseInt(p.controller, 10)));
      const value = Math.max(0, Math.min(127, parseInt(p.value, 10)));
      const ch = Math.max(0, Math.min(15, parseInt(p.channel != null ? p.channel : 0, 10)));
      Max.outlet('cc', controller, value);
      Max.outlet('midi', 0xB0 + ch, controller, value);          // control change
      return { sent: true, controller, value, channel: ch };
    }
    default:
      throw new Error('Unknown command: ' + type);
  }
}

const server = net.createServer((socket) => {
  socket.setEncoding('utf8');
  let buffer = '';
  socket.on('data', (chunk) => {
    buffer += chunk;
    // Parse as many complete JSON objects as the buffer holds (the MCP client
    // sends one object per request and waits for the reply).
    let obj;
    try {
      obj = JSON.parse(buffer);
    } catch (e) {
      return; // incomplete; wait for more
    }
    buffer = '';
    let response;
    try {
      response = { status: 'success', result: handle(obj) };
    } catch (err) {
      response = { status: 'error', message: String(err && err.message || err) };
    }
    socket.write(JSON.stringify(response));
  });
  socket.on('error', () => {});
});

// Try each port in the range until one binds free, so multiple M4L devices can
// coexist (each on its own port); the server discovers them by scanning.
function listenInRange(offset) {
  if (offset >= PORT_COUNT) {
    log('no free port in range ' + PORT_BASE + '..' + (PORT_BASE + PORT_COUNT - 1));
    Max.post('AbletonMCP bridge: no free port in range');
    return;
  }
  PORT = PORT_BASE + offset;
  server.listen(PORT, '127.0.0.1');
}

server.on('error', (err) => {
  if (err && err.code === 'EADDRINUSE') {
    log('port ' + PORT + ' in use, trying next');
    listenInRange((PORT - PORT_BASE) + 1);
    return;
  }
  Max.post('AbletonMCP bridge server error: ' + err.message);
  log('server error: ' + err.message);
});

server.on('listening', () => {
  Max.post('AbletonMCP Max bridge (' + role + ') on 127.0.0.1:' + PORT);
  log('listening on 127.0.0.1:' + PORT + ' role=' + role);
});

listenInRange(0);

module.exports = { handle, analysis, PORT };
