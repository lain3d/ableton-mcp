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
- `AbletonMCP_MIDI.amxd` — a MIDI-effect device that **injects MIDI into Live**.
  The bridge emits raw MIDI bytes on its `midi` outlet (from `m4l_send_midi` /
  `m4l_send_cc`) straight into `[midiout]`, so notes/CC flow downstream to the
  track's instrument in real time.

Rebuild: `python build_amxd.py --device analysis | synth | midi`

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

**MIDI generation — verified in Live:**
- With the MIDI-effect device before a stock instrument, `m4l_send_midi` plays a
  note through to the instrument: the track meter goes 0 → ~0.87 with the
  instrument's own decay envelope. So notes reach Live's MIDI stream. `send_cc`
  uses the same `[midiout]` path; its audible effect depends on a MIDI mapping.

**Limitations gauged while building these:**
- Devices are hand-authored Max patcher JSON and can only be verified by loading
  in Live and observing behavior — audio *timbre* only indirectly (the coarse
  `output_meter_level`, not the actual sound; a human can just listen). The
  minimal synth has no ADSR envelope.
- **One M4L device at a time** — they all bind the bridge on 9878; a second one
  logs `EADDRINUSE` and won't answer. Remove the other device to free the port.
- **M4L-generated MIDI isn't recorded** — Live records the track *input*, not
  MIDI injected mid-chain, so the MIDI device drives the instrument live but
  can't author clips (verified: recording captured 0 notes). To *write* notes
  into a clip, use the Remote Script's `add_notes_to_clip` (LOM) instead; the
  M4L device is for real-time / generative playing and CC modulation.

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
