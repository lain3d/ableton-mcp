# AbletonMCP — Max for Live bridge (v0.8.0, foundation)

The Remote Script controls Live through the **Live Object Model (LOM)** — great
for structure, mixer, devices, and automation, but it can't touch the actual
audio/MIDI *signal*. Max for Live runs a device **inside the signal path**, so it
reaches what the LOM fundamentally can't:

- **Audio analysis** — real peak/RMS, pitch detection, spectral centroid of the
  audio on a track (the LOM only exposes coarse meters).
- **MIDI / CC generation** — emit notes and CC into a track (the practical route
  to MIDI CC, which clip envelopes can't write).

## Architecture

```
  MCP server (Python)  ──TCP 9877──▶  Remote Script   (LOM: structure/mixer/devices)
        │
        └────────────  ──TCP 9878──▶  M4L device → Node for Max → mcp_bridge.js
                                       (signal: audio analysis, MIDI/CC out)
```

The M4L device embeds a [`node.script`] running `mcp_bridge.js`, which hosts a
small TCP server on **127.0.0.1:9878** using the same line-delimited JSON
protocol as the Remote Script (`{"type", "params"}` → `{"status", "result"}`).
The MCP server's `m4l_*` tools connect to it. Audio-analysis values flow Max →
Node via `max-api` messages; MIDI/CC instructions flow Node → Max via outlets.

## Files

- `mcp_bridge.js` — the Node-for-Max bridge (also runs standalone for testing).
- `build_amxd.py` — generates the device `.amxd` from a Max patcher; the patcher
  for the analysis device is generated in-code so it's reproducible.
- `AbletonMCP_Analysis.amxd` — the built audio-analysis device (Audio Effect).

Rebuild the device with: `python build_amxd.py`

## What's tested vs. pending

**Tested (standalone, no Max needed):**
- `.amxd` generation produces a structurally valid device (header + patcher JSON).
- The bridge protocol end-to-end: `ping`, `get_analysis`, `send_midi`, `send_cc`,
  error handling — driven from the MCP server's `m4l_*` tools against the bridge.
- Node→Max direction (MIDI/CC outlets fire with the right values).

**Pending in-Live verification (can't be socket-tested from outside Live):**
- The hand-authored Max patch wiring (`peakamp~` → `node.script`, and the
  MIDI-out routing). Load the device and confirm analysis values update and
  notes/CC actually reach the track.

## Install & test in Live

1. Put `AbletonMCP_Analysis.amxd` **and** `mcp_bridge.js` in the same folder, and
   add that folder to Max's search path: in Live, open the device's Max editor
   (the device's edit button) → Options → File Preferences → add the folder; or
   drop both into your Ableton User Library's `Max Audio Effect` area.
2. Drag `AbletonMCP_Analysis.amxd` onto an **audio track** (one with sound
   playing through it).
3. The Node bridge starts automatically. Confirm from the MCP side:
   - `m4l_status` → should report connected with capabilities.
   - `m4l_get_analysis` → `peak` should rise/fall with the audio.
   - `m4l_send_midi` / `m4l_send_cc` → the device emits MIDI/CC.

If `m4l_status` says "not available", the device isn't loaded (or Node for Max
is still starting — give it a few seconds on first load).

## Roadmap (M4L chapter)

- Wire more analysis: RMS, pitch (`sigmund~`/`fzero~`), spectral centroid.
- A dedicated **MIDI Effect** device for clean note/CC generation and recording
  (CC into clips via record).
- Audio generation / resynthesis device.
- Embed the Node script in the `.amxd` (freeze) so it's a single-file install.
