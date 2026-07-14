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
- `build_amxd.py` — generates a device `.amxd` from a Max patcher; the patchers
  are generated in-code so they're reproducible. The 4-byte form marker selects
  the device type (`aaaa` audio effect / `iiii` instrument / `mmmm` MIDI effect).
- `AbletonMCP_Analysis.amxd` — audio-analysis device (reports peak of the audio
  passing through it).
- `AbletonMCP_Synth.amxd` — a sine synth: **generates audio from nothing**. The
  bridge's note outlet (from `m4l_send_midi`) drives a `cycle~` oscillator;
  velocity sets the level, so a note plays a pitch and velocity 0 silences it.

Rebuild: `python build_amxd.py --device analysis` / `--device synth`

**Run only one AbletonMCP M4L device at a time** — they all host the bridge on
port 9878 and would collide.

## What's tested

**Verified in Live (device loaded on an audio track):**
- The device loads and the Node bridge auto-starts (`@autostart 1` + a
  `loadbang → "script start"`), running Node for Max's Node v20 inside Live and
  listening on 9878.
- **Audio analysis works**: `peak` reads 0 with no audio and tracks the signal
  while a clip plays (measured ~0.35–0.54). This is the `peakamp~ → node.script`
  path — data the LOM cannot provide.
- `m4l_status` / `m4l_get_analysis` from the MCP server return live values.

**Audio synthesis — verified in Live:**
- The synth device generates a tone on demand: the track's `output_meter_level`
  goes 0 → ~0.87 when a note is triggered via `m4l_send_midi`, back to 0 on
  velocity 0. This is real audio created from nothing — impossible via the LOM.

**Limitations gauged while building this:**
- Synths must be hand-authored as Max DSP patches, and can only be verified
  indirectly (the coarse `output_meter_level`, not the actual sound). This
  minimal synth has no ADSR envelope — a musical instrument would build one in
  the patch.
- MIDI/CC *generation into Live* (`send_midi`/`send_cc`) needs a dedicated
  **MIDI Effect** device (`mmmm` marker) wiring the Node outlet to
  `noteout`/`ctlout`; in the analysis/synth devices those outlets drive analysis
  or the oscillator, not Live's MIDI stream. (Next up.)

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
