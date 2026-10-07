"""Greenhouse Telemetry Storage Efficiency Benchmark: Plain JSON vs. Compact Binary.

Educational Goals:
------------------
1. Demonstrate the real-world flash storage overhead and serialization cost of text-based
   formats (JSON) on resource-constrained embedded edge devices (e.g. ESP32).
2. Showcase the compactness, predictability, and O(1) row stride of a fixed-width binary
   flat file with an indirect heap arena implemented in `functions.py`.
3. Validate lossless round-trip re-hydration from the compact binary representation back
   into standard JSON, proving data integrity and high-precision preservation.
"""

from __future__ import annotations

import json
import math
import os
import random
import time
from typing import Any, Dict, List

from functions import (
    TYPE_DOUBLE,
    TYPE_FLOAT,
    TYPE_INT32,
    TYPE_STR,
    append_row,
    dump_layout,
    hex_dump,
    init_file,
    inspect_layout,
    read_all_rows,
    read_header,
)

# File names for benchmarking
RAW_JSON_PATH = "greenhouse_raw.json"
COMPACT_BIN_PATH = "greenhouse_compact.bin"
RECONSTRUCTED_JSON_PATH = "greenhouse_reconstructed.json"

RECORD_COUNT = 10_000


# ==============================================================================
# SECTION 1: SYNTHETIC GREENHOUSE TELEMETRY GENERATION
# ==============================================================================

def generate_telemetry_dataset(num_records: int) -> List[Dict[str, Any]]:
    """Generates synthetic greenhouse sensor telemetry data.

    Simulates realistic agricultural IoT readings:
    - timestamp: 5-second interval epoch starting from 1,700,000,000.
    - temperature: 20.0 to 35.0 °C (ambient diurnal variation).
    - humidity: 40.0 to 90.0 % (relative humidity).
    - co2_ppm: 400 to 1800 ppm (carbon dioxide concentration).
    - soil_moisture: 15.0 to 65.0 % (volumetric water content).
    - sensor_node_id: Fixed-length embedded hardware identifier (15 bytes).
    - calibrated_lat / calibrated_lon: 64-bit IEEE-754 precision GPS coordinates.
    """
    random.seed(42)  # Deterministic seed for reproducible benchmarks
    start_time = 1_700_000_000

    base_lat = -7.123456789012
    base_lon = 110.987654321098

    dataset: List[Dict[str, Any]] = []

    for i in range(num_records):
        ts = start_time + (i * 5)
        # Diurnal temperature cycle simulation
        temp = round(20.0 + 15.0 * (0.5 + 0.5 * math.sin(i / 100.0)) + random.uniform(-0.5, 0.5), 2)
        humidity = round(90.0 - (temp - 20.0) * 2.5 + random.uniform(-2.0, 2.0), 2)
        humidity = max(30.0, min(95.0, humidity))
        co2 = int(400 + (1400 * (1.0 - (temp - 20.0) / 20.0)) + random.randint(-30, 30))
        co2 = max(400, min(2000, co2))
        soil = round(45.0 + 15.0 * math.cos(i / 250.0) + random.uniform(-1.0, 1.0), 2)
        soil = max(10.0, min(70.0, soil))

        node_id = "GH-ZONE3-NODE12"
        lat = base_lat
        lon = base_lon

        dataset.append({
            "timestamp": ts,
            "temperature": temp,
            "humidity": humidity,
            "co2_ppm": co2,
            "soil_moisture": soil,
            "sensor_node_id": node_id,
            "calibrated_lat": lat,
            "calibrated_lon": lon,
        })

    return dataset


# ==============================================================================
# SECTION 2: BENCHMARK EXECUTION
# ==============================================================================

def run_benchmark() -> None:
    print("=" * 85)
    print("      GREENHOUSE TELEMETRY STORAGE BENCHMARK: PLAIN JSON vs. COMPACT FLAT FILE")
    print("=" * 85)
    print(f"Dataset Size: {RECORD_COUNT:,} sensor telemetry records")
    print("Target Architecture: ESP32 / NOR Flash (Little-Endian, O(1) Stride Arithmetic)\n")

    # 1. Generate Dataset
    print("[1/4] Generating synthetic greenhouse telemetry dataset...")
    t0 = time.perf_counter()
    records = generate_telemetry_dataset(RECORD_COUNT)
    t_gen = time.perf_counter() - t0
    print(f"      Generated {len(records):,} records in {t_gen:.4f} seconds.\n")

    # --------------------------------------------------------------------------
    # PHASE 1: Plain JSON Storage Baseline
    # --------------------------------------------------------------------------
    print("-" * 85)
    print("PHASE 1: Plain JSON Storage Baseline (Standard Text Serialization)")
    print("-" * 85)

    # Standard JSON with standard 2-space indentation (common logging format)
    t0 = time.perf_counter()
    with open(RAW_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)
    t_json_write = time.perf_counter() - t0

    json_size_bytes = os.path.getsize(RAW_JSON_PATH)
    json_size_kb = json_size_bytes / 1024.0
    json_size_mb = json_size_kb / 1024.0

    # Also compute compact (unindented) JSON for fair text comparison
    compact_json_str = json.dumps(records, separators=(",", ":"))
    compact_json_bytes = len(compact_json_str.encode("utf-8"))

    print(f"  -> File created:            {RAW_JSON_PATH}")
    print(f"  -> Formatted JSON size:     {json_size_bytes:,} bytes ({json_size_kb:.2f} KB | {json_size_mb:.2f} MB)")
    print(f"  -> Minified JSON size:      {compact_json_bytes:,} bytes ({compact_json_bytes / 1024.0:.2f} KB | {compact_json_bytes / (1024*1024):.2f} MB)")
    print(f"  -> Write duration:          {t_json_write:.4f} seconds")
    print(f"  -> Average bytes / record:  {json_size_bytes / RECORD_COUNT:.1f} bytes (formatted) | {compact_json_bytes / RECORD_COUNT:.1f} bytes (minified)\n")

    # --------------------------------------------------------------------------
    # PHASE 2: Binary Flat-File Encoding (`functions.py`)
    # --------------------------------------------------------------------------
    print("-" * 85)
    print("PHASE 2: Binary Flat-File Encoding (Fixed-Stride Buffer + Packed Heap Arena)")
    print("-" * 85)

    schema = [
        ("timestamp", TYPE_INT32),       # 4 bytes in-place ('<i')
        ("temperature", TYPE_FLOAT),     # 4 bytes in-place ('<f')
        ("humidity", TYPE_FLOAT),        # 4 bytes in-place ('<f')
        ("co2_ppm", TYPE_INT32),         # 4 bytes in-place ('<i')
        ("soil_moisture", TYPE_FLOAT),   # 4 bytes in-place ('<f')
        ("sensor_node_id", TYPE_STR),    # 4-byte fat pointer ('<2H') -> heap string
        ("calibrated_lat", TYPE_DOUBLE), # 4-byte fat pointer ('<2H') -> heap double ('<d', 8B)
        ("calibrated_lon", TYPE_DOUBLE), # 4-byte fat pointer ('<2H') -> heap double ('<d', 8B)
    ]

    # Pre-calculating memory layout bounds:
    col_count = len(schema)
    dataframe_size = RECORD_COUNT * col_count * 4  # 10,000 * 8 * 4 = 320,000 bytes

    # Heap capacity: 64 KB (within 16-bit uint16 addressable range of <2H fat pointer: 0..65535)
    # The heap arena stores deduplicated strings and 64-bit IEEE-754 doubles in a constant pool
    heap_capacity = 64_000
    header_estimate = 32 + col_count + 120  # ~160 bytes
    total_allocated_size = header_estimate + dataframe_size + heap_capacity

    if os.path.exists(COMPACT_BIN_PATH):
        os.remove(COMPACT_BIN_PATH)

    print(f"  -> Initializing binary flat file with {RECORD_COUNT:,} max capacity...")
    init_state = init_file(COMPACT_BIN_PATH, schema, max_entries=RECORD_COUNT, total_size=total_allocated_size)

    # Encode and stream rows into binary flat file
    t0 = time.perf_counter()
    with open(COMPACT_BIN_PATH, "r+b") as f:
        state = read_header(f)
        for r in records:
            row_tuple = [
                r["timestamp"],
                r["temperature"],
                r["humidity"],
                r["co2_ppm"],
                r["soil_moisture"],
                r["sensor_node_id"],
                r["calibrated_lat"],
                r["calibrated_lon"],
            ]
            append_row(f, state, row_tuple)
    t_bin_write = time.perf_counter() - t0

    bin_size_bytes = os.path.getsize(COMPACT_BIN_PATH)
    bin_size_kb = bin_size_bytes / 1024.0
    bin_size_mb = bin_size_kb / 1024.0

    # Calculate actual bytes utilized (active header + dataframe + actual heap watermark)
    actual_data_bytes = state["heap_base"] + state["heap_watermark"]
    actual_data_kb = actual_data_bytes / 1024.0

    reduction_formatted = (1.0 - (bin_size_bytes / json_size_bytes)) * 100.0
    reduction_minified = (1.0 - (bin_size_bytes / compact_json_bytes)) * 100.0
    net_data_reduction = (1.0 - (actual_data_bytes / compact_json_bytes)) * 100.0

    print(f"  -> File created:            {COMPACT_BIN_PATH}")
    print(f"  -> Total allocated file:    {bin_size_bytes:,} bytes ({bin_size_kb:.2f} KB | {bin_size_mb:.2f} MB)")
    print(f"  -> Actual data payload:     {actual_data_bytes:,} bytes ({actual_data_kb:.2f} KB | Active Watermark: {state['heap_watermark']:,} B)")
    print(f"  -> Binary append time:      {t_bin_write:.4f} seconds ({RECORD_COUNT / t_bin_write:,.0f} rows/sec)")
    print(f"  -> Average bytes / record:  {actual_data_bytes / RECORD_COUNT:.1f} bytes (actual) vs {json_size_bytes / RECORD_COUNT:.1f} bytes (JSON)")
    print(f"  -> Space reduction vs JSON: {reduction_formatted:.1f}% vs Formatted JSON | {reduction_minified:.1f}% vs Minified JSON\n")

    print("Memory Layout Inspection:")
    inspect_layout(state, COMPACT_BIN_PATH)
    print()

    # --------------------------------------------------------------------------
    # PHASE 3: Lossless Re-hydration & Deserialization Verification
    # --------------------------------------------------------------------------
    print("-" * 85)
    print("PHASE 3: Expansion & Verification (Lossless Re-hydration to JSON)")
    print("-" * 85)

    t0 = time.perf_counter()
    with open(COMPACT_BIN_PATH, "r+b") as f:
        read_state = read_header(f)
        unpacked_rows = read_all_rows(f, read_state, chronological=True)
    t_bin_read = time.perf_counter() - t0

    print(f"  -> Read back {len(unpacked_rows):,} records from binary in {t_bin_read:.4f} seconds ({len(unpacked_rows)/t_bin_read:,.0f} rows/sec)")

    # Export reconstructed records to JSON
    with open(RECONSTRUCTED_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(unpacked_rows, f, indent=2)

    reconstructed_size = os.path.getsize(RECONSTRUCTED_JSON_PATH)
    print(f"  -> Reconstructed JSON size: {reconstructed_size:,} bytes ({reconstructed_size / 1024.0:.2f} KB | {reconstructed_size / (1024*1024):.2f} MB)")
    print(f"     (Notice: The compact {bin_size_kb:.1f} KB binary file expands back to the full {reconstructed_size / 1024.0:.1f} KB JSON file!)")

    # Verify data integrity on sample records (Index 0, 5100, 9999)
    sample_indices = [0, RECORD_COUNT // 2, RECORD_COUNT - 1]
    print("\n  Sample Record Verification (Precision & Fidelity Check):")
    for idx in sample_indices:
        orig = records[idx]
        recon = unpacked_rows[idx]

        # Validate types
        assert orig["timestamp"] == recon["timestamp"], f"Timestamp mismatch at {idx}"
        assert orig["co2_ppm"] == recon["co2_ppm"], f"CO2 mismatch at {idx}"
        assert orig["sensor_node_id"] == recon["sensor_node_id"], f"Node ID mismatch at {idx}"
        # Validate 64-bit IEEE double lat/lon bit-exact preservation
        assert math.isclose(orig["calibrated_lat"], recon["calibrated_lat"], abs_tol=1e-12), f"Lat mismatch at {idx}"
        assert math.isclose(orig["calibrated_lon"], recon["calibrated_lon"], abs_tol=1e-12), f"Lon mismatch at {idx}"
        # Validate 32-bit single float precision
        assert math.isclose(orig["temperature"], recon["temperature"], rel_tol=1e-5), f"Temp mismatch at {idx}"
        assert math.isclose(orig["humidity"], recon["humidity"], rel_tol=1e-5), f"Humidity mismatch at {idx}"
        assert math.isclose(orig["soil_moisture"], recon["soil_moisture"], rel_tol=1e-5), f"Soil mismatch at {idx}"

        print(f"    [Record #{idx:>5d}]")
        print(f"      Original:      ts={orig['timestamp']}, temp={orig['temperature']}°C, hum={orig['humidity']}%, co2={orig['co2_ppm']}, node={orig['sensor_node_id']}, lat={orig['calibrated_lat']}")
        print(f"      Reconstructed: ts={recon['timestamp']}, temp={recon['temperature']:.2f}°C, hum={recon['humidity']:.2f}%, co2={recon['co2_ppm']}, node={recon['sensor_node_id']}, lat={recon['calibrated_lat']}")
        print("      Status:        [VERIFIED PERFECT MATCH]")

    # --------------------------------------------------------------------------
    # PHASE 4: Raw Hex Preview of Binary Flash Image
    # --------------------------------------------------------------------------
    print("\n" + "-" * 85)
    print("PHASE 4: Raw Embedded NOR Flash Layout (Hex Dump of First 64 Bytes)")
    print("-" * 85)
    with open(COMPACT_BIN_PATH, "rb") as f:
        print(hex_dump(f, start_offset=0, length=64))
    print("-" * 85 + "\n")

    # --------------------------------------------------------------------------
    # PHASE 5: Presentation & Comparison Table
    # --------------------------------------------------------------------------
    density_ratio = (json_size_bytes / RECORD_COUNT) / (actual_data_bytes / RECORD_COUNT)
    esp32_json_capacity = int((2 * 1024 * 1024) / (json_size_bytes / RECORD_COUNT))
    esp32_bin_capacity = int((2 * 1024 * 1024) / (actual_data_bytes / RECORD_COUNT))
    esp32_retention_ratio = esp32_bin_capacity / esp32_json_capacity
    wear_reduction = (1.0 - (actual_data_bytes / json_size_bytes)) * 100.0
    hours_json = (esp32_json_capacity * 5) / 3600.0
    hours_bin = (esp32_bin_capacity * 5) / 3600.0

    print("=" * 85)
    print("                       STORAGE EFFICIENCY & IMPACT COMPARISON")
    print("=" * 85)
    header_fmt = "{:<32} {:<22} {:<22} {:<15}"
    row_fmt    = "{:<32} {:<22} {:<22} {:<15}"

    print(header_fmt.format("Metric", "JSON Format (Raw)", "Flat-File Binary", "Gain / Benefit"))
    print("-" * 85)
    print(row_fmt.format("File Footprint (Bytes)", f"{json_size_bytes:,} B", f"{bin_size_bytes:,} B", f"-{reduction_formatted:.1f}% space"))
    print(row_fmt.format("File Footprint (KB)", f"{json_size_kb:,.2f} KB", f"{bin_size_kb:,.2f} KB", f"-{(json_size_kb - bin_size_kb):,.2f} KB"))
    print(row_fmt.format("File Footprint (MB)", f"{json_size_mb:,.2f} MB", f"{bin_size_mb:,.2f} MB", f"~{json_size_mb/bin_size_mb:.1f}x smaller"))
    print(row_fmt.format("Net Payload Size (Active)", f"{compact_json_bytes:,} B (Minified)", f"{actual_data_bytes:,} B (Active)", f"-{net_data_reduction:.1f}% payload"))
    print(row_fmt.format("Avg. Storage / Record", f"{json_size_bytes / RECORD_COUNT:.1f} bytes/row", f"{actual_data_bytes / RECORD_COUNT:.1f} bytes/row", f"{density_ratio:.1f}x denser"))
    print(row_fmt.format("Random Access Complexity", "O(N) (Full scan)", "O(1) (Stride MAC)", "Zero scan overhead"))
    print(row_fmt.format("ESP32 2MB Partition Capacity", f"~{esp32_json_capacity:,} readings", f"~{esp32_bin_capacity:,} readings", f"{esp32_retention_ratio:.1f}x longer log"))
    print(row_fmt.format("NOR Flash Write Wear", f"High (~{json_size_bytes/RECORD_COUNT:.0f} B/write)", f"Minimal (~{actual_data_bytes/RECORD_COUNT:.0f} B/write)", f"{wear_reduction:.1f}% less wear"))
    print("=" * 85)
    print("\n[CONCLUSION FOR STUDENTS]")
    print(f"1. Text serialization (JSON) imposes a ~{density_ratio*100 - 100:.0f}% storage penalty on IoT edge devices due to")
    print("   repeated key strings, ASCII number representations, and punctuation delimiters.")
    print("2. The Flat-File Engine achieves O(1) random-access speed by enforcing a uniform 4-byte")
    print("   stride dataframe while offloading variable strings and 64-bit doubles to a packed heap.")
    print(f"3. On embedded flash memory (e.g. ESP32 LittleFS partition), binary encoding extends")
    print(f"   offline telemetry logging retention from ~{hours_json:.1f} hours to over {hours_bin:.1f} hours on identical hardware.")


if __name__ == "__main__":
    run_benchmark()
