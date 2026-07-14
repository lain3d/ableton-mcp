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

## What's tested

**Verified in Live (device loaded on an audio track):**
- The device loads and the Node bridge auto-starts (`@autostart 1` + a
  `loadbang → "script start"`), running Node for Max's Node v20 inside Live and
  listening on 9878.
- **Audio analysis works**: `peak` reads 0 with no audio and tracks the signal
  while a clip plays (measured ~0.35–0.54). This is the `peakamp~ → node.script`
  path — data the LOM cannot provide.
- `m4l_status` / `m4l_get_analysis` from the MCP server return live values.

**Verified standalone, wiring pending in this device:**
- The bridge's `send_midi` / `send_cc` protocol works, but this *audio-analysis*
  device doesn't wire the Node MIDI outlet to a `noteout`/`ctlout` — MIDI/CC
  generation belongs in a dedicated **MIDI Effect** device (roadmap). Calling
  them here reports success but emits nothing into Live.

## Install (how it's loaded)

The device is staged in the Ableton **User Library** so it shows up in Live's
browser and can be loaded programmatically (or by drag-and-drop):

```
User Library/Presets/Audio Effects/AbletonMCP/
    AbletonMCP_Analysis.amxd
    mcp_bridge.js            # must sit next to the .amxd so node.script finds it
```

`node.script` resolves `mcp_bridge.js` relative to the device, so no Max
search-path change is needed. After rebuilding (`python build_amxd.py`), copy
both files back into that User Library folder. Then load it onto an **audio
track** (MCP: `load_browser_item` with the User Library URI, or drag it on).

## Debugging

The bridge logs startup and errors to `abletonmcp_m4l.log` in the OS temp dir
(`%LOCALAPPDATA%\Temp` on Windows), so you can see what happened inside Live
even though the Max console isn't reachable over the socket.

## Roadmap (M4L chapter)

- Wire more analysis: RMS, pitch (`sigmund~`/`fzero~`), spectral centroid.
- A dedicated **MIDI Effect** device for clean note/CC generation and recording
  (CC into clips via record).
- Audio generation / resynthesis device.
- Embed the Node script in the `.amxd` (freeze) so it's a single-file install.
