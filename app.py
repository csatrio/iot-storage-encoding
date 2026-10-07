"""Flask Web Backend for ESP32 Flat-File Binary Buffer Visual Simulator.

Execution Guide:
----------------
  pip install -r requirements.txt
  python app.py
  # Open http://localhost:5100 in your browser
"""

from __future__ import annotations

import json
import os
import struct
import time
from typing import Any, Dict, List, Tuple

from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

import functions as ff

app = Flask(__name__)
CORS(app)

SIMULATOR_FILE = "simulator_buffer.bin"

# Standard teaching schema (Greenhouse Farm IoT telemetry)
DEFAULT_SCHEMA: List[Tuple[str, int]] = [
    ("timestamp", ff.TYPE_INT32),
    ("temp", ff.TYPE_FLOAT),
    ("humidity", ff.TYPE_FLOAT),
    ("node_id", ff.TYPE_STR),
    ("precision_lat", ff.TYPE_DOUBLE),
]

TYPE_NAMES = {
    ff.TYPE_INT32: "INT32",
    ff.TYPE_FLOAT: "FLOAT",
    ff.TYPE_STR: "STR",
    ff.TYPE_DOUBLE: "DOUBLE",
}


def ensure_initialized() -> Dict[str, Any]:
    """Ensures a valid simulator flat-file exists on disk."""
    if not os.path.exists(SIMULATOR_FILE):
        return ff.init_file(SIMULATOR_FILE, DEFAULT_SCHEMA, max_entries=5, total_size=1024)
    with open(SIMULATOR_FILE, "r+b") as f:
        return ff.read_header(f)


def analyze_state(f, state: Dict[str, Any]) -> Dict[str, Any]:
    """Analyzes the binary buffer state, resolving cells, pointers, and heap segments."""
    header_size = state["header_size"]
    col_count = state["col_count"]
    col_names = state["col_names"]
    col_types = state["col_types"]
    max_entries = state["max_entries"]
    entry_count = state["entry_count"]
    head_index = state["head_index"]
    heap_base = state["heap_base"]
    heap_watermark = state["heap_watermark"]
    max_heap_size = state["max_heap_size"]

    # 1. Analyze physical dataframe slots
    physical_rows = []
    live_heap_allocations: List[Dict[str, Any]] = []

    for r in range(max_entries):
        is_active = r < entry_count
        is_head = r == head_index
        row_offset = ff.calc_row_offset(header_size, col_count, r)

        cells = []
        for c in range(col_count):
            cname = col_names[c]
            ctype = col_types[c]
            type_name = TYPE_NAMES.get(ctype, "UNKNOWN")
            cell_offset = ff.calc_cell_offset(header_size, col_count, r, c)

            f.seek(cell_offset)
            cell_bytes = f.read(4)
            raw_hex = " ".join(f"{b:02X}" for b in cell_bytes)

            decoded_val = None
            pointer_info = None

            if is_active:
                if ctype == ff.TYPE_INT32:
                    decoded_val = struct.unpack("<i", cell_bytes)[0]
                    format_tag = "<i"
                elif ctype == ff.TYPE_FLOAT:
                    decoded_val = round(struct.unpack("<f", cell_bytes)[0], 2)
                    format_tag = "<f"
                elif ctype == ff.TYPE_STR:
                    format_tag = "<2H (ptr)"
                    offset, length = ff.unpack_heap_ptr(cell_bytes)
                    if length > 0:
                        raw = ff.read_heap_payload(f, state, offset, length)
                        decoded_val = raw.decode("utf-8", errors="replace")
                        pointer_info = {
                            "offset": offset,
                            "length": length,
                            "heap_abs_addr": heap_base + offset,
                            "heap_hex": " ".join(f"{b:02X}" for b in raw),
                        }
                        live_heap_allocations.append({
                            "row": r,
                            "col": c,
                            "col_name": cname,
                            "offset": offset,
                            "length": length,
                            "val_preview": decoded_val,
                            "type": "STR",
                        })
                elif ctype == ff.TYPE_DOUBLE:
                    format_tag = "<2H (ptr)"
                    offset, length = ff.unpack_heap_ptr(cell_bytes)
                    if length > 0:
                        raw = ff.read_heap_payload(f, state, offset, length)
                        decoded_val = round(struct.unpack("<d", raw)[0], 8)
                        pointer_info = {
                            "offset": offset,
                            "length": length,
                            "heap_abs_addr": heap_base + offset,
                            "heap_hex": " ".join(f"{b:02X}" for b in raw),
                        }
                        live_heap_allocations.append({
                            "row": r,
                            "col": c,
                            "col_name": cname,
                            "offset": offset,
                            "length": length,
                            "val_preview": str(decoded_val),
                            "type": "DOUBLE",
                        })
                else:
                    format_tag = "4B"
            else:
                format_tag = "4B (empty)"

            cells.append({
                "col_idx": c,
                "col_name": cname,
                "col_type": ctype,
                "type_name": type_name,
                "format_tag": format_tag,
                "cell_offset": cell_offset,
                "cell_offset_hex": f"0x{cell_offset:04X}",
                "raw_hex": raw_hex,
                "decoded_val": decoded_val,
                "pointer": pointer_info,
            })

        row_json_info = None
        if is_active:
            row_dict = {c["col_name"]: c["decoded_val"] for c in cells}
            row_json_str = json.dumps(row_dict, indent=2)
            row_json_min = json.dumps(row_dict, separators=(",", ":"))
            r_json_bytes = len(row_json_str.encode("utf-8"))
            r_json_min_bytes = len(row_json_min.encode("utf-8"))
            r_df_bytes = len(cells) * 4
            r_heap_bytes = sum(c["pointer"]["length"] for c in cells if c.get("pointer"))
            r_total_packed = r_df_bytes + r_heap_bytes
            r_savings = round((1.0 - (r_total_packed / r_json_min_bytes)) * 100.0, 1) if r_json_min_bytes > 0 else 0.0

            row_json_info = {
                "dict": row_dict,
                "formatted": row_json_str,
                "minified": row_json_min,
                "formatted_bytes": r_json_bytes,
                "minified_bytes": r_json_min_bytes,
                "df_bytes": r_df_bytes,
                "heap_bytes": r_heap_bytes,
                "total_packed_bytes": r_total_packed,
                "savings_pct": r_savings,
            }

        physical_rows.append({
            "row_idx": r,
            "is_active": is_active,
            "is_head": is_head,
            "row_offset": row_offset,
            "row_offset_hex": f"0x{row_offset:04X}",
            "cells": cells,
            "json_info": row_json_info,
        })

    # 2. Chronological rows
    chronological_rows = ff.read_all(f, state, chronological=True)

    # 3. Analyze heap segments (LIVE vs DEAD / ORPHANED vs FREE)
    live_heap_allocations.sort(key=lambda x: x["offset"])

    # Build contiguous segments from 0 to max_heap_size
    segments = []
    current_offset = 0

    # Merge overlapping/adjacent live allocations if any
    for live in live_heap_allocations:
        l_start = live["offset"]
        l_len = live["length"]
        l_end = l_start + l_len

        # If there is a gap between current_offset and live start below watermark, it's DEAD/ORPHANED
        if l_start > current_offset and current_offset < heap_watermark:
            dead_len = min(l_start, heap_watermark) - current_offset
            if dead_len > 0:
                f.seek(heap_base + current_offset)
                dead_bytes = f.read(dead_len)
                dead_preview = dead_bytes.decode("utf-8", errors="replace").strip("\x00") or "0x00..."
                segments.append({
                    "status": "DEAD",
                    "offset": current_offset,
                    "length": dead_len,
                    "abs_addr": heap_base + current_offset,
                    "preview": dead_preview,
                    "label": f"Orphaned Garbage ({dead_len} B)",
                })
                current_offset += dead_len

        # Live segment
        if l_start >= current_offset and l_start < heap_watermark:
            segments.append({
                "status": "LIVE",
                "offset": l_start,
                "length": l_len,
                "abs_addr": heap_base + l_start,
                "preview": str(live["val_preview"]),
                "label": f"Live {live['type']} (Row #{live['row']} - {live['col_name']})",
                "row": live["row"],
                "col_name": live["col_name"],
            })
            current_offset = l_end

    # Any leftover space between current_offset and heap_watermark is also DEAD/ORPHANED
    if current_offset < heap_watermark:
        dead_len = heap_watermark - current_offset
        f.seek(heap_base + current_offset)
        dead_bytes = f.read(dead_len)
        dead_preview = dead_bytes.decode("utf-8", errors="replace").strip("\x00") or "0x00..."
        segments.append({
            "status": "DEAD",
            "offset": current_offset,
            "length": dead_len,
            "abs_addr": heap_base + current_offset,
            "preview": dead_preview,
            "label": f"Orphaned Garbage ({dead_len} B)",
        })
        current_offset = heap_watermark

    # Free space segment
    if heap_watermark < max_heap_size:
        free_len = max_heap_size - heap_watermark
        segments.append({
            "status": "FREE",
            "offset": heap_watermark,
            "length": free_len,
            "abs_addr": heap_base + heap_watermark,
            "preview": "Unallocated",
            "label": f"Available Heap Space ({free_len} B)",
        })

    live_bytes = sum(s["length"] for s in segments if s["status"] == "LIVE")
    dead_bytes = sum(s["length"] for s in segments if s["status"] == "DEAD")
    free_bytes = max_heap_size - heap_watermark

    # 4. Storage & Savings Comparison
    json_formatted = json.dumps(chronological_rows, indent=2)
    json_minified = json.dumps(chronological_rows, separators=(",", ":"))
    json_bytes_fmt = len(json_formatted.encode("utf-8")) if chronological_rows else 0
    json_bytes_min = len(json_minified.encode("utf-8")) if chronological_rows else 0

    active_bin_bytes = heap_base + heap_watermark
    total_bin_bytes = state["total_file_size"]

    savings_pct_fmt = (
        round((1.0 - (active_bin_bytes / json_bytes_fmt)) * 100.0, 1) if json_bytes_fmt > 0 else 0.0
    )
    savings_pct_min = (
        round((1.0 - (active_bin_bytes / json_bytes_min)) * 100.0, 1) if json_bytes_min > 0 else 0.0
    )

    # 5. Raw Hex Preview of first 128 bytes
    hex_preview = ff.hex_dump(f, start_offset=0, length=min(128, state["total_file_size"]))

    # 6. Byte-by-Byte Header Architecture Decomposition
    f.seek(0)
    raw_header = f.read(header_size)

    # 6.1 Fixed 32-byte Superblock fields (<8I)
    superblock_spec = [
        ("total_file_size", "Total File Boundary", 0, 4, state["total_file_size"], "<I", "Total pre-allocated binary file size on NOR flash Medium (bytes)"),
        ("header_size", "Total Header Size", 4, 4, state["header_size"], "<I", "Total header bytes from base offset 0x00; also base offset where dataframe rows begin"),
        ("dataframe_size", "Dataframe Size", 8, 4, state["dataframe_size"], "<I", "Total bytes reserved for dataframe rows: max_entries * col_count * 4 bytes"),
        ("max_entries", "Max Entry Count", 12, 4, state["max_entries"], "<I", "Maximum row capacity of the circular ring buffer"),
        ("entry_count", "Valid Entry Count", 16, 4, state["entry_count"], "<I", "Number of currently active populated records logged in the buffer"),
        ("head_index", "Write Head Index", 20, 4, state["head_index"], "<I", "Next circular ring slot index targeted for overwriting"),
        ("col_count", "Column Count", 24, 4, state["col_count"], "<I", "Number of schema columns (each column occupies a fixed 4-byte cell per row)"),
        ("heap_watermark", "Heap Bump Watermark", 28, 4, state["heap_watermark"], "<I", "Current dynamic bump allocator offset inside the heap arena"),
    ]

    fixed_header_fields = []
    for fname, flabel, fstart, fsize, fval, ffmt, fdesc in superblock_spec:
        fbytes = raw_header[fstart : fstart + fsize]
        fhex = " ".join(f"{b:02X}" for b in fbytes)
        bytes_detail = [
            {
                "offset": fstart + i,
                "offset_hex": f"0x{fstart + i:02X}",
                "hex": f"{b:02X}",
                "val": b,
            }
            for i, b in enumerate(fbytes)
        ]
        fixed_header_fields.append({
            "name": fname,
            "label": flabel,
            "offset_start": fstart,
            "offset_end": fstart + fsize - 1,
            "offset_hex": f"0x{fstart:02X} .. 0x{fstart + fsize - 1:02X}",
            "size": fsize,
            "format": ffmt,
            "value": fval,
            "raw_hex": fhex,
            "bytes": bytes_detail,
            "description": fdesc,
        })

    # 6.2 Column Type Array (1 byte per column @ 0x20)
    col_type_start = 32
    col_type_fields = []
    for i in range(col_count):
        ct_offset = col_type_start + i
        ct_val = raw_header[ct_offset] if ct_offset < len(raw_header) else col_types[i]
        ct_name = TYPE_NAMES.get(ct_val, "UNKNOWN")
        col_type_fields.append({
            "col_idx": i,
            "col_name": col_names[i] if i < len(col_names) else f"col_{i}",
            "col_type": ct_val,
            "type_name": ct_name,
            "offset": ct_offset,
            "offset_hex": f"0x{ct_offset:02X}",
            "hex": f"{ct_val:02X}",
            "size": 1,
            "format": "B (uint8)",
            "description": f"Type identifier for column {i} ('{col_names[i] if i < len(col_names) else i}'): {ct_name} (0x{ct_val:02X})",
        })

    # 6.3 Column Names String (delimited by '|', ends with null terminator \x00)
    names_start = 32 + col_count
    names_slice = raw_header[names_start : header_size]
    col_names_bytes_detail = []
    for i, b in enumerate(names_slice):
        abs_off = names_start + i
        char_repr = chr(b) if 32 <= b <= 126 else ("\\0" if b == 0 else f"\\x{b:02x}")
        col_names_bytes_detail.append({
            "offset": abs_off,
            "offset_hex": f"0x{abs_off:02X}",
            "hex": f"{b:02X}",
            "char": char_repr,
            "is_delimiter": b == ord("|"),
            "is_null": b == 0,
        })

    header_layout = {
        "header_size": header_size,
        "fixed_superblock": {
            "offset_start": 0,
            "offset_end": 31,
            "offset_hex": "0x00 .. 0x1F",
            "size": 32,
            "format": "<8I (32 Bytes)",
            "fields": fixed_header_fields,
        },
        "column_types": {
            "offset_start": col_type_start,
            "offset_end": col_type_start + col_count - 1,
            "offset_hex": f"0x{col_type_start:02X} .. 0x{col_type_start + col_count - 1:02X}",
            "size": col_count,
            "fields": col_type_fields,
        },
        "column_names": {
            "offset_start": names_start,
            "offset_end": header_size - 1,
            "offset_hex": f"0x{names_start:02X} .. 0x{header_size - 1:02X}",
            "size": len(names_slice),
            "text": "|".join(col_names) + "\\0",
            "bytes": col_names_bytes_detail,
        },
        "derived_heap": {
            "formula": "total_file_size - (header_size + dataframe_size)",
            "equation": f"{state['total_file_size']} - ({header_size} + {state['dataframe_size']})",
            "heap_base": heap_base,
            "heap_base_hex": f"0x{heap_base:02X}",
            "max_heap_size": max_heap_size,
        },
    }

    return {
        "metadata": {
            "total_file_size": state["total_file_size"],
            "header_size": header_size,
            "dataframe_size": state["dataframe_size"],
            "max_entries": max_entries,
            "entry_count": entry_count,
            "head_index": head_index,
            "col_count": col_count,
            "heap_watermark": heap_watermark,
            "heap_base": heap_base,
            "max_heap_size": max_heap_size,
            "col_names": col_names,
            "col_types": col_types,
            "col_type_names": [TYPE_NAMES.get(ct, "UNKNOWN") for ct in col_types],
        },
        "physical_rows": physical_rows,
        "chronological_rows": chronological_rows,
        "heap_analysis": {
            "watermark": heap_watermark,
            "max_heap_size": max_heap_size,
            "live_bytes": live_bytes,
            "dead_bytes": dead_bytes,
            "free_bytes": free_bytes,
            "segments": segments,
        },
        "savings": {
            "json_bytes_formatted": json_bytes_fmt,
            "json_bytes_minified": json_bytes_min,
            "active_binary_bytes": active_bin_bytes,
            "total_binary_bytes": total_bin_bytes,
            "savings_percent_formatted": savings_pct_fmt,
            "savings_percent_minified": savings_pct_min,
            "avg_bytes_per_record": round(active_bin_bytes / max(1, entry_count), 1),
        },
        "batch_json": {
            "formatted": json_formatted,
            "minified": json_minified,
            "formatted_bytes": json_bytes_fmt,
            "minified_bytes": json_bytes_min,
            "record_count": len(chronological_rows),
        },
        "header_layout": header_layout,
        "hex_preview": hex_preview,
    }


# ==============================================================================
# REST API ENDPOINTS
# ==============================================================================

@app.route("/")
def index():
    """Serves the single-page visualizer frontend."""
    from flask import send_file
    return send_file(os.path.join(app.root_path, "templates", "index.html"))


@app.route("/index.js")
def serve_index_js():
    """Serves the decoupled client JavaScript module."""
    from flask import send_file
    return send_file(os.path.join(app.root_path, "static", "index.js"), mimetype="application/javascript")


@app.route("/api/init", methods=["POST"])
def api_init():
    """Initializes or resets the flat file with customized layout."""
    t0 = time.perf_counter()
    data = request.get_json() or {}
    max_entries = int(data.get("max_entries", 5))
    total_size = int(data.get("total_size", 1024))
    schema = data.get("schema") or DEFAULT_SCHEMA

    # Format schema tuples
    formatted_schema = []
    for col in schema:
        cname, ctype = col[0], int(col[1])
        formatted_schema.append((cname, ctype))

    ff.init_file(SIMULATOR_FILE, formatted_schema, max_entries=max_entries, total_size=total_size)
    with open(SIMULATOR_FILE, "r+b") as f:
        state = ff.read_header(f)
        result = analyze_state(f, state)
    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    return jsonify({"success": True, "state": result, "latency_ms": latency_ms})


@app.route("/api/append", methods=["POST"])
def api_append():
    """Appends a row into the circular buffer."""
    t0 = time.perf_counter()
    data = request.get_json() or {}
    row_values = data.get("row")
    deduplicate = bool(data.get("deduplicate", False))  # Default False so students can see GC

    if not row_values:
        return jsonify({"success": False, "error": "Missing row values"}), 400

    ensure_initialized()
    with open(SIMULATOR_FILE, "r+b") as f:
        state = ff.read_header(f)
        try:
            ff.append_row(f, state, row_values, deduplicate=deduplicate)
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 400

        result = analyze_state(f, state)

    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    return jsonify({"success": True, "state": result, "latency_ms": latency_ms})


@app.route("/api/compact", methods=["POST"])
def api_compact():
    """Forces manual mark-compact heap garbage collection."""
    t0 = time.perf_counter()
    ensure_initialized()
    with open(SIMULATOR_FILE, "r+b") as f:
        state = ff.read_header(f)
        pre_watermark = state["heap_watermark"]
        post_watermark = ff.compact_heap(f, state)
        reclaimed = pre_watermark - post_watermark
        result = analyze_state(f, state)

    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    return jsonify({
        "success": True,
        "pre_watermark": pre_watermark,
        "post_watermark": post_watermark,
        "reclaimed_bytes": reclaimed,
        "state": result,
        "latency_ms": latency_ms,
    })


@app.route("/api/state", methods=["GET"])
def api_state():
    """Returns the full runtime inspection state of the binary file."""
    t0 = time.perf_counter()
    ensure_initialized()
    with open(SIMULATOR_FILE, "r+b") as f:
        state = ff.read_header(f)
        result = analyze_state(f, state)
    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    return jsonify({"success": True, "state": result, "latency_ms": latency_ms})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    """Resets the simulator buffer back to initial clean state."""
    t0 = time.perf_counter()
    if os.path.exists(SIMULATOR_FILE):
        os.remove(SIMULATOR_FILE)
    ensure_initialized()
    with open(SIMULATOR_FILE, "r+b") as f:
        state = ff.read_header(f)
        result = analyze_state(f, state)
    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    return jsonify({"success": True, "state": result, "latency_ms": latency_ms})


if __name__ == "__main__":
    ensure_initialized()
    print("\n=======================================================")
    print("  ESP32 FLAT-FILE BINARY BUFFER & GC VISUAL SIMULATOR")
    print("=======================================================")
    print("  Local Server running at: http://localhost:5100")
    print("  Press Ctrl+C to stop.")
    print("=======================================================\n")
    app.run(host="0.0.0.0", port=5100, debug=True)
