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


def wrap_amxd(patch_json_bytes: bytes) -> bytes:
    out = bytearray()
    out += b"ampf" + struct.pack("<I", 4) + b"aaaa"
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
        # the Node for Max bridge; @watch reloads on file change during dev
        _box("obj-5", "newobj", "node.script " + script_name + " @watch 1",
             220, 210, 230, 22, numin=1, numout=2, outtype=["", "bang"]),
        _box("obj-6", "comment", "AbletonMCP audio-analysis bridge (TCP 9878)",
             220, 250, 300, 20),
    ]
    lines = [
        _line("obj-1", 0, "obj-2", 0),   # audio passthrough L
        _line("obj-1", 1, "obj-2", 1),   # audio passthrough R
        _line("obj-1", 0, "obj-3", 0),   # L channel -> peakamp~
        _line("obj-3", 0, "obj-4", 0),   # peak float -> prepend peak
        _line("obj-4", 0, "obj-5", 0),   # "peak <v>" -> node.script
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


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--maxpat", help="Patcher JSON to wrap (default: built-in analysis device)")
    ap.add_argument("--out", default=os.path.join(here, "AbletonMCP_Analysis.amxd"))
    ap.add_argument("--script", default="mcp_bridge.js")
    args = ap.parse_args()

    if args.maxpat:
        with open(args.maxpat, "rb") as f:
            patch = json.loads(f.read().decode("utf-8"))
    else:
        patch = analysis_maxpat(args.script)

    patch_bytes = json.dumps(patch, indent=1).encode("utf-8")
    amxd = wrap_amxd(patch_bytes)
    with open(args.out, "wb") as f:
        f.write(amxd)
    print("Wrote %s (%d bytes, patch %d bytes)" % (args.out, len(amxd), len(patch_bytes)))


if __name__ == "__main__":
    main()
