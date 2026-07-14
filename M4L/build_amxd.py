#!/usr/bin/env python3
"""Build a Max for Live device (.amxd) from a Max patcher (.maxpat) JSON.

An .amxd is a 32-byte chunked header followed by the patcher JSON:

    'ampf' u32(form_version=4) 'aaaa'
    'meta' u32(size=4) u32(0)
    'ptch' u32(len(json)) <json bytes>

This script also generates the AbletonMCP audio-analysis device patcher, so the
device is fully reproducible from source (Max patch JSON is otherwise opaque).

Usage:
    python build_amxd.py                 # writes AbletonMCP_Analysis.amxd
    python build_amxd.py --maxpat x.maxpat --out x.amxd
"""
import argparse
import json
import os
import struct


# The 4-byte form marker after the version selects the device type.
DEVICE_MARKER = {
    "audio_effect": b"aaaa",
    "instrument": b"iiii",
    "midi_effect": b"mmmm",
}


def wrap_amxd(patch_json_bytes: bytes, device_kind: str = "audio_effect") -> bytes:
    marker = DEVICE_MARKER.get(device_kind, b"aaaa")
    out = bytearray()
    out += b"ampf" + struct.pack("<I", 4) + marker
    out += b"meta" + struct.pack("<I", 4) + struct.pack("<I", 0)
    out += b"ptch" + struct.pack("<I", len(patch_json_bytes))
    out += patch_json_bytes
    return bytes(out)


def _box(box_id, maxclass, text, x, y, w, h, numin=0, numout=0, outtype=None, extra=None):
    b = {
        "id": box_id,
        "maxclass": maxclass,
        "patching_rect": [x, y, w, h],
    }
    if text is not None:
        b["text"] = text
    if numin:
        b["numinlets"] = numin
    if numout:
        b["numoutlets"] = numout
    if outtype is not None:
        b["outlettype"] = outtype
    if extra:
        b.update(extra)
    return {"box": b}


def _line(src_id, src_outlet, dst_id, dst_inlet):
    return {"patchline": {"source": [src_id, src_outlet],
                          "destination": [dst_id, dst_inlet]}}


def analysis_maxpat(script_name="mcp_bridge.js"):
    """An Audio Effect device: passes audio through and reports peak amplitude
    to the Node bridge (which serves the MCP server on TCP 9878).

    NOTE: hand-authored patcher — must be verified by loading in Live. The Node
    bridge and its protocol are tested independently (see test_bridge.py)."""
    boxes = [
        _box("obj-1", "newobj", "plugin~", 40, 60, 130, 22, numin=2, numout=3,
             outtype=["signal", "signal", "list"]),
        _box("obj-2", "newobj", "plugout~", 40, 320, 130, 22, numin=2, numout=0),
        # peak analysis on the left channel, reported every 50 ms
        _box("obj-3", "newobj", "peakamp~ 50", 220, 120, 110, 22, numin=1, numout=1,
             outtype=["float"]),
        _box("obj-4", "newobj", "prepend peak", 220, 160, 90, 22, numin=1, numout=1,
             outtype=[""]),
        # the Node for Max bridge; @autostart runs it on load, @watch reloads
        # the script when the file changes during development
        _box("obj-5", "newobj",
             "node.script " + script_name + " @autostart 1 @watch 1",
             220, 210, 300, 22, numin=1, numout=2, outtype=["", "bang"]),
        # belt-and-suspenders: also send "script start" on device load
        _box("obj-7", "newobj", "loadbang", 480, 120, 60, 22, numin=1, numout=1),
        _box("obj-8", "message", "script start", 480, 160, 80, 22, numin=2, numout=1),
        _box("obj-6", "comment", "AbletonMCP audio-analysis bridge (TCP 9878)",
             220, 250, 300, 20),
    ]
    lines = [
        _line("obj-1", 0, "obj-2", 0),   # audio passthrough L
        _line("obj-1", 1, "obj-2", 1),   # audio passthrough R
        _line("obj-1", 0, "obj-3", 0),   # L channel -> peakamp~
        _line("obj-3", 0, "obj-4", 0),   # peak float -> prepend peak
        _line("obj-4", 0, "obj-5", 0),   # "peak <v>" -> node.script
        _line("obj-7", 0, "obj-8", 0),   # loadbang -> "script start" message
        _line("obj-8", 0, "obj-5", 0),   # "script start" -> node.script
    ]
    return {
        "patcher": {
            "fileversion": 1,
            "appversion": {"major": 8, "minor": 1, "revision": 2,
                           "architecture": "x64", "modernui": 1},
            "rect": [80.0, 80.0, 640.0, 400.0],
            "boxes": boxes,
            "lines": lines,
            "originid": "pat-1",
        }
    }


def synth_maxpat(script_name="mcp_bridge.js"):
    """An audio-effect device that GENERATES a tone — proof that the tool can
    create audio from nothing. The Node bridge's note outlet (from m4l_send_midi)
    drives a sine oscillator; velocity sets the level, so send a note to play a
    pitch and send velocity 0 to silence it.

    NOTE: hand-authored patcher — verify by loading in Live. Uses the same
    mcp_bridge.js, so run only one AbletonMCP M4L device at a time (they'd both
    try to bind the bridge port)."""
    boxes = [
        _box("obj-1", "newobj",
             "node.script " + script_name + " @autostart 1 @watch 1",
             40, 60, 300, 22, numin=1, numout=2, outtype=["", "bang"]),
        _box("obj-2", "newobj", "loadbang", 380, 20, 60, 22, numin=1, numout=1),
        _box("obj-3", "message", "script start", 380, 60, 80, 22, numin=2, numout=1),
        _box("obj-4", "newobj", "route note", 40, 110, 90, 22, numin=1, numout=2,
             outtype=["", ""]),
        _box("obj-5", "newobj", "unpack 0 0 0", 40, 150, 110, 22, numin=1, numout=3,
             outtype=["int", "int", "int"]),
        _box("obj-6", "newobj", "mtof", 40, 190, 50, 22, numin=1, numout=1, outtype=["float"]),
        _box("obj-7", "newobj", "cycle~", 40, 230, 60, 22, numin=2, numout=1, outtype=["signal"]),
        _box("obj-8", "newobj", "/ 127.", 170, 190, 60, 22, numin=2, numout=1, outtype=["float"]),
        _box("obj-9", "newobj", "*~", 40, 280, 80, 22, numin=2, numout=1, outtype=["signal"]),
        _box("obj-10", "newobj", "plugout~", 40, 330, 90, 22, numin=2, numout=0),
        _box("obj-11", "comment", "AbletonMCP synth — bridge note -> tone (TCP 9878)",
             40, 370, 320, 20),
    ]
    lines = [
        _line("obj-2", 0, "obj-3", 0),   # loadbang -> "script start"
        _line("obj-3", 0, "obj-1", 0),   # -> node.script
        _line("obj-1", 0, "obj-4", 0),   # bridge messages -> route note
        _line("obj-4", 0, "obj-5", 0),   # note args -> unpack pitch vel dur
        _line("obj-5", 0, "obj-6", 0),   # pitch -> mtof
        _line("obj-6", 0, "obj-7", 0),   # freq -> cycle~
        _line("obj-5", 1, "obj-8", 0),   # velocity -> / 127.
        _line("obj-7", 0, "obj-9", 0),   # osc -> *~
        _line("obj-8", 0, "obj-9", 1),   # gain -> *~ right inlet
        _line("obj-9", 0, "obj-10", 0),  # -> plugout~ L
        _line("obj-9", 0, "obj-10", 1),  # -> plugout~ R
    ]
    return {
        "patcher": {
            "fileversion": 1,
            "appversion": {"major": 8, "minor": 1, "revision": 2,
                           "architecture": "x64", "modernui": 1},
            "rect": [80.0, 80.0, 640.0, 440.0],
            "boxes": boxes,
            "lines": lines,
            "originid": "pat-2",
        }
    }


def midi_maxpat(script_name="mcp_bridge.js"):
    """A MIDI-effect device that INJECTS MIDI into Live. The Node bridge emits
    raw MIDI bytes on its 'midi' outlet (from m4l_send_midi / m4l_send_cc); the
    patch routes them straight to [midiout], which sends them downstream to the
    track's instrument and into recording.

    NOTE: hand-authored patcher — verify by loading in Live. Uses mcp_bridge.js,
    so run only one AbletonMCP M4L device at a time."""
    boxes = [
        _box("obj-1", "newobj",
             "node.script " + script_name + " @autostart 1 @watch 1",
             40, 60, 300, 22, numin=1, numout=2, outtype=["", "bang"]),
        _box("obj-2", "newobj", "loadbang", 380, 20, 60, 22, numin=1, numout=1),
        _box("obj-3", "message", "script start", 380, 60, 80, 22, numin=2, numout=1),
        _box("obj-4", "newobj", "route midi", 40, 110, 90, 22, numin=1, numout=2,
             outtype=["", ""]),
        # midiout sends a received list of bytes as raw MIDI into Live's stream
        _box("obj-5", "newobj", "midiout", 40, 160, 70, 22, numin=1, numout=0),
        _box("obj-6", "comment", "AbletonMCP MIDI out — bridge -> Live (TCP 9878)",
             40, 200, 320, 20),
    ]
    lines = [
        _line("obj-2", 0, "obj-3", 0),   # loadbang -> "script start"
        _line("obj-3", 0, "obj-1", 0),   # -> node.script
        _line("obj-1", 0, "obj-4", 0),   # bridge messages -> route midi
        _line("obj-4", 0, "obj-5", 0),   # raw bytes -> midiout
    ]
    return {
        "patcher": {
            "fileversion": 1,
            "appversion": {"major": 8, "minor": 1, "revision": 2,
                           "architecture": "x64", "modernui": 1},
            "rect": [80.0, 80.0, 620.0, 280.0],
            "boxes": boxes,
            "lines": lines,
            "originid": "pat-3",
        }
    }


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--maxpat", help="Patcher JSON to wrap (default: built-in device)")
    ap.add_argument("--device", choices=["analysis", "synth", "midi"], default="analysis",
                    help="Which built-in device patcher to build")
    ap.add_argument("--out", default=None)
    ap.add_argument("--script", default="mcp_bridge.js")
    args = ap.parse_args()

    if args.maxpat:
        with open(args.maxpat, "rb") as f:
            patch = json.loads(f.read().decode("utf-8"))
        kind = "audio_effect"
    elif args.device == "synth":
        patch = synth_maxpat(args.script)
        kind = "audio_effect"   # generates a tone; loads on an audio track
    elif args.device == "midi":
        patch = midi_maxpat(args.script)
        kind = "midi_effect"    # injects MIDI; loads on a MIDI track
    else:
        patch = analysis_maxpat(args.script)
        kind = "audio_effect"

    if args.out is None:
        name = {"synth": "AbletonMCP_Synth.amxd",
                "midi": "AbletonMCP_MIDI.amxd"}.get(args.device, "AbletonMCP_Analysis.amxd")
        args.out = os.path.join(here, name)

    patch_bytes = json.dumps(patch, indent=1).encode("utf-8")
    amxd = wrap_amxd(patch_bytes, kind)
    with open(args.out, "wb") as f:
        f.write(amxd)
    print("Wrote %s (%d bytes, patch %d bytes)" % (args.out, len(amxd), len(patch_bytes)))


if __name__ == "__main__":
    main()
