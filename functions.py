"""ESP32 Flat-File Circular Buffer & Mark-Compact Heap Engine.

This module provides a standalone, zero-dependency educational engine designed to
teach computer systems and embedded systems students how binary serialization,
fixed-stride circular ring buffers, and mark-compact garbage collection operate
directly on raw byte storage (such as NOR flash memory on ESP32 microcontrollers).

================================================================================
PHYSICAL & HARDWARE PRINCIPLES
================================================================================

1. Embedded NOR Flash Constraints:
   - NOR Flash supports random-access byte-level reads with zero latency penalty.
   - Programming (writing): Bits can only transition from 1 to 0. Writing over existing
     data without an erase operation requires writing to freshly zeroed or erased bytes.
   - Sector Erase: Setting bits from 0 back to 1 cannot be done at the byte level; it
     requires erasing an entire sector (typically 4 KB on SPI NOR flash).
   - Flat-file design pattern: By laying out fixed-stride rows in a circular ring
     buffer and pairing them with a bump-allocated heap arena, we eliminate arbitrary
     dynamic heap fragmentation on microcontrollers lacking a Memory Management Unit (MMU).

2. Little-Endian Architecture (<):
   - The ESP32 utilizes Tensilica Xtensa LX6/LX7 dual-core or 32-bit RISC-V cores.
   - Both Xtensa and RISC-V architectures are Little-Endian: the least significant
     byte (LSB) is stored at the lowest byte memory address.
     Example: 32-bit integer 0x12345678 is arranged in memory as [0x78, 0x56, 0x34, 0x12].
   - All struct format strings strictly enforce '<' to match hardware word alignment
     and avoid byte-swapping instruction overhead (e.g., bswap/rev) on microcontrollers.

3. Partitioned Flat-File Memory Map:
   +-----------------------+-----------------------+-----------------------------+
   |        Header         |       Dataframe       |            Heap             |
   | (Schema & Metadata)   | (Fixed-width rows)    | (Variable-width allocations)|
   +-----------------------+-----------------------+-----------------------------+
   0x00               Header_End              Dataframe_End                  File_End
                      (= Dataframe_Base)      (= Heap_Base)

   - Header: Fixed 32-byte superblock followed by column types and pipe-delimited schema names.
   - Dataframe: Fixed-stride circular buffer. Every cell is exactly 4 bytes wide.
     Direct values (INT32, FLOAT) reside in-place. Variable-length (STR) and 8-byte
     (DOUBLE) types store a 4-byte packed fat pointer (<2H) into the Heap Arena.
   - Heap Arena: Bump-allocated linear arena. When circular buffer overwrites orphan
     old heap entries, a Mark-Compact pass slides live allocations to offset 0.
"""

from __future__ import annotations

import os
import struct
from typing import Any, BinaryIO, Dict, List, Optional, Tuple, Union

# ==============================================================================
# BINARY TYPE CONSTANTS & FORMAT DEFINITIONS
# ==============================================================================

# 1-byte schema type identifiers
TYPE_INT32: int = 0x01   # 32-bit signed integer (4 bytes in-place: '<i')
TYPE_FLOAT: int = 0x02   # 32-bit IEEE-754 single float (4 bytes in-place: '<f')
TYPE_STR: int = 0x03     # Variable-length UTF-8 string (4 bytes fat pointer: '<2H')
TYPE_DOUBLE: int = 0x04  # 64-bit IEEE-754 double (4 bytes fat pointer: '<2H' -> 8B heap '<d')

# Header structure format: 8 unsigned 32-bit integers = 32 bytes fixed superblock
# Field Order:
#   [0] total_file_size (uint32, 4B)
#   [1] header_size     (uint32, 4B)
#   [2] dataframe_size  (uint32, 4B)
#   [3] max_entries     (uint32, 4B)
#   [4] entry_count     (uint32, 4B)
#   [5] head_index      (uint32, 4B)
#   [6] col_count       (uint32, 4B)
#   [7] heap_watermark  (uint32, 4B)
HEADER_FIXED_FORMAT: str = "<8I"
HEADER_FIXED_SIZE: int = struct.calcsize(HEADER_FIXED_FORMAT)  # 32 bytes

# 4-byte indirect heap fat pointer format: (uint16 offset, uint16 length)
HEAP_PTR_FORMAT: str = "<2H"
HEAP_PTR_SIZE: int = struct.calcsize(HEAP_PTR_FORMAT)  # 4 bytes


# ==============================================================================
# SECTION 1: HEADER & LAYOUT SETUP
# ==============================================================================

def init_file(
    filepath: str,
    schema: List[Tuple[str, int]],
    max_entries: int,
    total_size: int,
) -> Dict[str, Any]:
    """Formats and initializes a new flat binary file on disk.

    Hardware / Memory Principle:
    -----------------------------
    Embedded flash systems (SPIFFS, LittleFS, or raw flash partitions) mandate
    deterministic, contiguous file allocation. Dynamically extending files
    causes NOR flash fragmentation and sector-erase spikes.
    By pre-allocating the entire file boundary up to `total_size` and initializing
    unused space with zeroes (0x00), this function establishes constant-time
    pointer arithmetic boundaries before any sensor telemetry is logged.

    Binary Layout & Byte Offsets:
    -----------------------------
    0x00 .. 0x1F (32 bytes): Fixed Superblock Header (HEADER_FIXED_FORMAT = '<8I')
      - Offset 0x00 (4B, uint32): total_file_size
      - Offset 0x04 (4B, uint32): header_size
      - Offset 0x08 (4B, uint32): dataframe_size
      - Offset 0x0C (4B, uint32): max_entries (ring capacity)
      - Offset 0x10 (4B, uint32): entry_count (initially 0)
      - Offset 0x14 (4B, uint32): head_index  (initially 0, write head)
      - Offset 0x18 (4B, uint32): col_count
      - Offset 0x1C (4B, uint32): heap_watermark (initially 0)
    0x20 .. (0x20 + col_count): Raw column type tags (1 byte per column: 'B')
    (0x20 + col_count) .. header_size: Pipe-delimited UTF-8 column names + b'\\x00'
    header_size .. heap_base: Dataframe block (max_entries * col_count * 4 bytes)
    heap_base .. total_size: Heap Arena (total_size - heap_base bytes)

    Args:
        filepath: Filesystem path to create.
        schema: List of (column_name, type_tag) tuples defining the schema.
        max_entries: Maximum number of rows in the circular buffer ring.
        total_size: Total file boundary in bytes allocated on the storage medium.

    Returns:
        A dictionary containing the layout state and partition offsets.

    Raises:
        ValueError: If the header and dataframe requirements exceed total_size.
    """
    col_names = [col[0] for col in schema]
    col_types = [col[1] for col in schema]
    col_count = len(schema)

    # Encode schema column names as pipe-delimited UTF-8 with a trailing null terminator
    col_names_raw = "|".join(col_names).encode("utf-8") + b"\x00"

    # Compute header and dataframe partition boundaries
    header_size = HEADER_FIXED_SIZE + col_count + len(col_names_raw)
    dataframe_size = max_entries * col_count * 4
    heap_base = header_size + dataframe_size

    if heap_base >= total_size:
        raise ValueError(
            f"Header ({header_size} B) + Dataframe ({dataframe_size} B) = {heap_base} B "
            f"exceeds or equals total file boundary ({total_size} B). No heap space remaining."
        )

    max_heap_size = total_size - heap_base

    # Write binary layout to disk
    with open(filepath, "wb") as f:
        # Step 1: Write 32-byte fixed superblock
        f.write(
            struct.pack(
                HEADER_FIXED_FORMAT,
                total_size,
                header_size,
                dataframe_size,
                max_entries,
                0,  # entry_count (starts empty)
                0,  # head_index (starts at slot 0)
                col_count,
                0,  # heap_watermark (starts at 0)
            )
        )

        # Step 2: Write column types array (1 byte per column)
        f.write(bytes(col_types))

        # Step 3: Write pipe-delimited schema string + null byte
        f.write(col_names_raw)

        # Step 4: Zero-pad remainder of file up to total_size
        bytes_written = f.tell()
        remaining_padding = total_size - bytes_written
        f.write(b"\x00" * remaining_padding)

    state = {
        "filepath": filepath,
        "total_file_size": total_size,
        "header_size": header_size,
        "dataframe_size": dataframe_size,
        "max_entries": max_entries,
        "entry_count": 0,
        "head_index": 0,
        "col_count": col_count,
        "heap_watermark": 0,
        "col_types": col_types,
        "col_names": col_names,
        "heap_base": heap_base,
        "max_heap_size": max_heap_size,
    }
    return state


def read_header(f: BinaryIO) -> Dict[str, Any]:
    """Reads and parses the binary header and schema metadata from a flat file.

    Hardware / Memory Principle:
    -----------------------------
    On boot or file-open, an embedded OS (e.g. ESP-IDF) mounts the filesystem
    and reads the superblock at physical flash offset 0x00 into a memory-resident
    control structure (struct FlatFileState). This provides O(1) in-memory
    access to layout boundaries without repeatedly querying flash metadata.

    Binary Unpacking Details:
    -------------------------
    1. Seeks to offset 0x00 and reads 32 bytes (HEADER_FIXED_SIZE).
    2. Unpacks '<8I' (8 little-endian uint32 words).
    3. Reads `col_count` bytes to recover column types list.
    4. Streams bytes until encountering null terminator b'\\x00' to recover schema names.

    Args:
        f: An open binary file object supporting seek() and read().

    Returns:
        A dictionary containing all layout metadata and runtime state counters.

    Raises:
        ValueError: If file content is smaller than the required 32-byte header.
    """
    f.seek(0)
    fixed_buf = f.read(HEADER_FIXED_SIZE)
    if len(fixed_buf) < HEADER_FIXED_SIZE:
        raise ValueError(
            f"File corrupted or too small: expected {HEADER_FIXED_SIZE} bytes header, "
            f"read only {len(fixed_buf)} bytes."
        )

    (
        total_file_size,
        header_size,
        dataframe_size,
        max_entries,
        entry_count,
        head_index,
        col_count,
        heap_watermark,
    ) = struct.unpack(HEADER_FIXED_FORMAT, fixed_buf)

    # Read column type identifiers (1 byte each)
    col_types = list(f.read(col_count))

    # Read null-terminated pipe-delimited column names (guarded with safety watchdog)
    names_raw = bytearray()
    max_scan_bytes = max(1024, header_size)
    bytes_read = 0
    while bytes_read < max_scan_bytes:
        ch = f.read(1)
        bytes_read += 1
        if ch == b"\x00" or ch == b"":
            break
        names_raw.extend(ch)

    col_names = names_raw.decode("utf-8").split("|") if names_raw else []

    heap_base = header_size + dataframe_size
    max_heap_size = total_file_size - heap_base

    return {
        "total_file_size": total_file_size,
        "header_size": header_size,
        "dataframe_size": dataframe_size,
        "max_entries": max_entries,
        "entry_count": entry_count,
        "head_index": head_index,
        "col_count": col_count,
        "heap_watermark": heap_watermark,
        "col_types": col_types,
        "col_names": col_names,
        "heap_base": heap_base,
        "max_heap_size": max_heap_size,
    }


def write_header(f: BinaryIO, state: Dict[str, Any]) -> None:
    """Synchronizes dynamic metadata counters back to the 32-byte fixed header on disk.

    Hardware / Memory Principle:
    -----------------------------
    In embedded flash logging, updates to mutable metadata (entry_count, head_index,
    heap_watermark) must be committed reliably. Calling flush() ensures OS write
    buffers are flushed to underlying flash controller pages, guarding against
    metadata corruption during unexpected microcontroller power loss (brownout).

    Binary Packing Details:
    -------------------------
    Seeks to byte offset 0x00 and overwrites only the 32-byte fixed superblock
    using '<8I'. Schema types and names remain immutable after initialization.

    Args:
        f: An open binary file object supporting seek() and write().
        state: State dictionary containing current counters.
    """
    f.seek(0)
    f.write(
        struct.pack(
            HEADER_FIXED_FORMAT,
            state["total_file_size"],
            state["header_size"],
            state["dataframe_size"],
            state["max_entries"],
            state["entry_count"],
            state["head_index"],
            state["col_count"],
            state["heap_watermark"],
        )
    )
    f.flush()


# ==============================================================================
# SECTION 2: ADDRESS & OFFSET CALCULATIONS
# ==============================================================================

def calc_row_offset(header_size: int, col_count: int, row_idx: int) -> int:
    """Calculates the absolute byte offset of a row within the binary file.

    Hardware / Memory Principle (O(1) Row Stride Arithmetic):
    ----------------------------------------------------------
    A core tenet of systems programming and database storage engines is
    deterministic O(1) random-access indexing.
    Because every cell in our dataframe is strictly 4 bytes wide, the byte
    stride of each row is an invariant constant:
        Row_Stride = col_count * 4 bytes

    Computing the target address requires only a single Multiply-Accumulate (MAC)
    operation in CPU hardware:
        Absolute_Row_Address = header_size + (row_idx * Row_Stride)

    Unlike variable-width storage formats (such as JSON, CSV, or SQLite B-Trees)
    which require parsing intermediate delimiters or following index trees,
    the CPU can seek directly to any row in constant O(1) time without reading
    prior rows.

    Args:
        header_size: Size in bytes of the preceding header partition.
        col_count: Number of columns in each row.
        row_idx: Physical 0-indexed row slot in the circular buffer.

    Returns:
        The exact byte offset from the start of the file.
    """
    stride = col_count * 4
    return header_size + (row_idx * stride)


def calc_cell_offset(header_size: int, col_count: int, row_idx: int, col_idx: int) -> int:
    """Calculates the exact absolute byte address for a specific cell (row, col).

    Hardware / Memory Principle:
    -----------------------------
    Given that all row strides are identical and every cell is aligned to a
    4-byte boundary, cell coordinates (row_idx, col_idx) map directly to:
        Cell_Address = calc_row_offset(row_idx) + (col_idx * 4)

    This 4-byte natural alignment corresponds directly to 32-bit word loads
    (e.g., Xtensa 'L32I' or RISC-V 'LW' instructions), preventing unaligned
    memory access exceptions on embedded processors.

    Args:
        header_size: Size in bytes of the preceding header partition.
        col_count: Number of columns in each row.
        row_idx: Physical row slot index (0 .. max_entries - 1).
        col_idx: Column index (0 .. col_count - 1).

    Returns:
        The exact byte offset from the start of the file for the cell.
    """
    return calc_row_offset(header_size, col_count, row_idx) + (col_idx * 4)


def pack_heap_ptr(offset: int, length: int) -> bytes:
    """Packs a heap arena offset and payload length into a 4-byte indirect fat pointer.

    Hardware / Memory Principle (Fat Pointer / Double-Indirection):
    ----------------------------------------------------------------
    Directly storing variable-length data (strings) or wide types (8-byte doubles)
    inside a table row causes severe fragmentation or requires variable-stride rows,
    destroying O(1) row access.
    To maintain uniform 4-byte cell alignment across all columns, we store an
    indirect fat pointer inside the 4-byte dataframe cell:
        - Bits 0..15  (2 bytes, uint16): offset relative to heap_base (0 .. 65,535 bytes)
        - Bits 16..31 (2 bytes, uint16): length of payload in bytes (0 .. 65,535 bytes)

    Format String:
    --------------
    '<2H':
      - '<' Little-Endian byte order.
      - '2H' Two unsigned 16-bit integers (2 * 2 bytes = 4 bytes total).

    Args:
        offset: Byte offset relative to heap_base (0 <= offset <= 65535).
        length: Byte length of the payload in heap memory (0 <= length <= 65535).

    Returns:
        4 bytes containing the packed fat pointer.

    Raises:
        struct.error: If offset or length exceeds 16-bit range (65,535).
    """
    if not (0 <= offset <= 0xFFFF):
        raise ValueError(f"Heap offset {offset} exceeds 16-bit boundary (0..65535).")
    if not (0 <= length <= 0xFFFF):
        raise ValueError(f"Heap payload length {length} exceeds 16-bit boundary (0..65535).")
    return struct.pack(HEAP_PTR_FORMAT, offset, length)


def unpack_heap_ptr(cell_bytes: bytes) -> Tuple[int, int]:
    """Unpacks a 4-byte indirect fat pointer into (offset, length).

    Hardware / Memory Principle:
    -----------------------------
    Extracts the heap relative offset and length stored inside a dataframe cell.
    The absolute physical flash address of the payload is computed as:
        Absolute_Payload_Address = heap_base + offset

    Format String:
    --------------
    '<2H': Unpacks two unsigned 16-bit integers from 4 bytes.

    Args:
        cell_bytes: 4 bytes read from a dataframe cell.

    Returns:
        Tuple of (offset: int, length: int).
    """
    if len(cell_bytes) != HEAP_PTR_SIZE:
        raise ValueError(f"Expected {HEAP_PTR_SIZE} bytes for heap pointer, got {len(cell_bytes)}.")
    return struct.unpack(HEAP_PTR_FORMAT, cell_bytes)


# ==============================================================================
# SECTION 3: HEAP ARENA ALLOCATION & MARK-COMPACT COMPACTOR
# ==============================================================================

def allocate_heap_payload(
    f: BinaryIO,
    state: Dict[str, Any],
    payload: bytes,
    deduplicate: bool = True,
) -> Tuple[int, int]:
    """Allocates a contiguous block of bytes in the heap arena using a bump allocator.

    Hardware / Memory Principle:
    -----------------------------
    A Bump (or Arena) Allocator increments a watermark pointer linearly:
        new_watermark = heap_watermark + len(payload)
    Bump allocation is O(1) and requires minimal CPU instructions, making it the
    fastest possible allocator on embedded microcontrollers lacking an MMU.

    Constant Pooling & String Interning:
    ------------------------------------
    When `deduplicate=True`, this function checks an in-memory heap cache for identical
    byte payloads (e.g., repeating sensor node IDs or fixed device calibration coordinates).
    If found, it returns the existing (offset, length) without allocating additional
    flash storage, effectively implementing an embedded Constant Pool / Symbol Table.

    Step-by-Step Memory Transition:
    -------------------------------
    1. Check `state['_heap_cache']` for existing payload match if deduplication is enabled.
    2. If missing, read current `state['heap_watermark']`.
    3. Absolute write address: `abs_addr = state['heap_base'] + offset`.
    4. Seek to `abs_addr` and write raw `payload` bytes.
    5. Advance `state['heap_watermark'] += len(payload)`.
    6. Record payload in `state['_heap_cache']` and return `(offset, len(payload))`.

    Args:
        f: Open binary file handle.
        state: Layout state dictionary.
        payload: Raw bytes to write into the heap.
        deduplicate: If True, reuses existing heap allocations for identical payloads.

    Returns:
        Tuple of (heap_relative_offset, payload_length).

    Raises:
        MemoryError: If allocation exceeds max_heap_size.
    """
    cache = state.setdefault("_heap_cache", {})
    if deduplicate and payload in cache:
        return cache[payload]

    length = len(payload)
    offset = state["heap_watermark"]

    if offset + length > state["max_heap_size"]:
        raise MemoryError(
            f"Heap out of memory: current watermark {offset} B + requested {length} B "
            f"> max heap capacity {state['max_heap_size']} B."
        )

    abs_addr = state["heap_base"] + offset
    f.seek(abs_addr)
    f.write(payload)

    state["heap_watermark"] += length
    cache[payload] = (offset, length)
    return (offset, length)


def read_heap_payload(
    f: BinaryIO,
    state: Dict[str, Any],
    offset: int,
    length: int,
) -> bytes:
    """Reads a contiguous block of bytes from the heap arena given offset and length.

    Args:
        f: Open binary file handle.
        state: Layout state dictionary.
        offset: Offset in bytes relative to heap_base.
        length: Number of bytes to read.

    Returns:
        Raw bytes read from heap.
    """
    abs_addr = state["heap_base"] + offset
    f.seek(abs_addr)
    return f.read(length)


def compact_heap(f: BinaryIO, state: Dict[str, Any]) -> int:
    """Performs a Mark-Compact Garbage Collection pass over the heap arena.

    Hardware / Memory Principle:
    -----------------------------
    In a circular ring buffer, overwriting an existing slot orphans any heap
    allocations (strings, doubles) previously associated with that slot.
    Because the bump allocator only moves forward, the heap arena would rapidly
    exhaust memory unless reclaimed.

    Why Mark-Compact?
    - Free-Lists cause external fragmentation: small holes cannot fit larger strings.
    - Reference Counting requires per-block headers and cannot eliminate fragmentation.
    - Mark-Compact slides all live allocations contiguously towards offset 0x00
      of the heap arena, defragmenting all free space into one large contiguous
      block at the end of the heap.

    Step-by-Step Memory Transitions:
    ---------------------------------
    1. MARK PHASE:
       - Traverses all currently live rows (0 .. entry_count - 1).
       - For every column of type TYPE_STR or TYPE_DOUBLE, inspects the 4-byte cell.
       - Decodes `<2H` fat pointer (old_offset, length).
       - Groups referencing cells by unique `(old_offset, length)`.

    2. SORT PHASE:
       - Sorts unique live allocations in ascending order of `old_offset`.
       - Crucial invariant: Sorting by `old_offset` ensures destination address
         (`new_abs_addr`) is ALWAYS <= source address (`old_abs_addr`).

    3. SLIDE & REWIRE PHASE:
       - Initializes `new_watermark = 0`.
       - For each unique allocation:
         a. Reads `len` bytes from `heap_base + old_offset`.
         b. Writes `len` bytes to `heap_base + new_watermark`.
         c. Rewires all dataframe cells referencing this allocation with
            `pack_heap_ptr(new_watermark, len)`.
         d. Updates intern cache and advances `new_watermark += len`.

    4. SWEEP / ZERO DEAD MEMORY:
       - Clears dead garbage between `new_watermark` and old `heap_watermark` with `b'\\x00'`.
       - Updates `state['heap_watermark'] = new_watermark`.
       - Commits updated watermark to disk header via `write_header(f, state)`.

    Args:
        f: Open binary file handle.
        state: State dictionary containing layout and counters.

    Returns:
        The new compacted heap watermark in bytes.
    """
    # --------------------------------------------------------------------------
    # Step 1: MARK Phase (Collect live pointers and group cells by unique allocation)
    # --------------------------------------------------------------------------
    live_map: Dict[Tuple[int, int], List[int]] = {}

    for r in range(state["entry_count"]):
        for c, ctype in enumerate(state["col_types"]):
            if ctype in (TYPE_STR, TYPE_DOUBLE):
                cell_pos = calc_cell_offset(state["header_size"], state["col_count"], r, c)
                f.seek(cell_pos)
                cell_bytes = f.read(HEAP_PTR_SIZE)
                offset, length = unpack_heap_ptr(cell_bytes)
                if length > 0:
                    live_map.setdefault((offset, length), []).append(cell_pos)

    # --------------------------------------------------------------------------
    # Step 2: SORT Phase (Preserve relative order of heap allocations)
    # --------------------------------------------------------------------------
    unique_allocs = sorted(live_map.keys(), key=lambda item: item[0])

    # --------------------------------------------------------------------------
    # Step 3: SLIDE & REWIRE Phase (Relocate live bytes and update cell pointers)
    # --------------------------------------------------------------------------
    new_watermark = 0
    heap_base = state["heap_base"]
    new_cache: Dict[bytes, Tuple[int, int]] = {}

    for old_offset, length in unique_allocs:
        old_abs_addr = heap_base + old_offset
        f.seek(old_abs_addr)
        payload = f.read(length)

        new_abs_addr = heap_base + new_watermark
        f.seek(new_abs_addr)
        f.write(payload)

        # Rewire all cells referencing this unique payload
        packed_ptr = pack_heap_ptr(new_watermark, length)
        for cell_pos in live_map[(old_offset, length)]:
            f.seek(cell_pos)
            f.write(packed_ptr)

        new_cache[payload] = (new_watermark, length)
        new_watermark += length

    # --------------------------------------------------------------------------
    # Step 4: ZERO DEAD MEMORY & UPDATE SUPERBLOCK
    # --------------------------------------------------------------------------
    old_watermark = state["heap_watermark"]
    if new_watermark < old_watermark:
        dead_bytes = old_watermark - new_watermark
        f.seek(heap_base + new_watermark)
        f.write(b"\x00" * dead_bytes)

    state["heap_watermark"] = new_watermark
    state["_heap_cache"] = new_cache
    write_header(f, state)
    return new_watermark



# ==============================================================================
# SECTION 4: CIRCULAR RING BUFFER OPERATIONS (CRUD)
# ==============================================================================

def append_row(
    f: BinaryIO,
    state: Dict[str, Any],
    row_values: List[Any],
    deduplicate: bool = True,
) -> None:
    """Inserts a record into the circular ring buffer, compacting heap if needed.

    Hardware / Memory Principle (Circular Ring Buffer Overwrite):
    --------------------------------------------------------------
    Telemetry and sensor loggers on embedded systems must run continuously
    without crashing due to full storage.
    A circular ring buffer achieves this by maintaining a write head pointer
    (`head_index`) that wraps around using modular arithmetic:
        head_index = (head_index + 1) % max_entries

    When `entry_count < max_entries`, new rows fill empty slots.
    When `entry_count == max_entries`, each new row overwrites the oldest slot.
    Any strings or doubles belonging to the overwritten slot become orphaned
    garbage in the heap arena.
    If the heap capacity is insufficient for the incoming record, Mark-Compact
    compaction is triggered immediately before writing.

    Step-by-Step Memory Transitions:
    ---------------------------------
    1. Validates that `len(row_values) == col_count`.
    2. Encodes primitive values directly:
       - TYPE_INT32: struct.pack('<i', int(v)) -> 4 bytes.
       - TYPE_FLOAT: struct.pack('<f', float(v)) -> 4 bytes IEEE-754 single float.
       - TYPE_STR: UTF-8 encoded bytes -> staged for heap allocation.
       - TYPE_DOUBLE: struct.pack('<d', float(v)) -> 8 bytes staged for heap.
    3. Checks required heap memory. If heap watermark exceeds boundary, triggers
       `compact_heap()`. If memory is still exhausted after compaction, raises MemoryError.
    4. Writes staged payloads into the heap arena and generates 4-byte packed
       pointers (`<2H`).
    5. Writes the array of 4-byte cells directly into physical row `head_index`
       at `calc_row_offset(head_index)`.
    6. Advances circular pointers:
       - `head_index = (head_index + 1) % max_entries`
       - `entry_count = min(entry_count + 1, max_entries)`
    7. Syncs header to disk via `write_header(f, state)`.

    Args:
        f: Open binary file handle (mode 'r+b').
        state: Layout state dictionary.
        row_values: List of values matching the defined schema columns.
        deduplicate: If True, reuses identical strings/doubles in heap cache.

    Raises:
        ValueError: If `len(row_values)` does not match `col_count`.
        MemoryError: If the heap arena is exhausted even after compaction.
    """
    col_count = state["col_count"]
    if len(row_values) != col_count:
        raise ValueError(f"Schema mismatch: expected {col_count} values, got {len(row_values)}")

    # Step 1: Pre-encode cells and separate heap payloads
    encoded_cells: List[Union[bytes, Tuple[int, bytes]]] = []
    pending_heap_allocations: List[bytes] = []

    for val, ctype in zip(row_values, state["col_types"]):
        if ctype == TYPE_INT32:
            encoded_cells.append(struct.pack("<i", int(val)))
        elif ctype == TYPE_FLOAT:
            encoded_cells.append(struct.pack("<f", float(val)))
        elif ctype == TYPE_STR:
            raw_bytes = str(val).encode("utf-8")
            encoded_cells.append((TYPE_STR, raw_bytes))
            pending_heap_allocations.append(raw_bytes)
        elif ctype == TYPE_DOUBLE:
            raw_bytes = struct.pack("<d", float(val))
            encoded_cells.append((TYPE_DOUBLE, raw_bytes))
            pending_heap_allocations.append(raw_bytes)
        else:
            raise ValueError(f"Unsupported column type tag: {ctype}")

    # Step 2: Capacity check and proactive compaction
    cache = state.setdefault("_heap_cache", {}) if deduplicate else {}
    required_heap = sum(len(b) for b in pending_heap_allocations if b not in cache)
    if state["heap_watermark"] + required_heap > state["max_heap_size"]:
        # If writing over an existing slot, invalidate its old pointers so GC reclaims them now
        dest_row = state["head_index"]
        if state["entry_count"] == state["max_entries"]:
            for c, ctype in enumerate(state["col_types"]):
                if ctype in (TYPE_STR, TYPE_DOUBLE):
                    pos = calc_cell_offset(state["header_size"], state["col_count"], dest_row, c)
                    f.seek(pos)
                    f.write(pack_heap_ptr(0, 0))  # zero out pointer length

        compact_heap(f, state)
        if state["heap_watermark"] + required_heap > state["max_heap_size"]:
            raise MemoryError(
                f"Heap arena exhausted even after compaction: required {required_heap} bytes, "
                f"available {state['max_heap_size'] - state['heap_watermark']} bytes."
            )

    # Step 3: Write heap payloads and convert tuples to 4-byte packed pointers
    finalized_cells: List[bytes] = []
    for cell in encoded_cells:
        if isinstance(cell, tuple):
            _, raw_bytes = cell
            offset, length = allocate_heap_payload(f, state, raw_bytes, deduplicate=deduplicate)
            finalized_cells.append(pack_heap_ptr(offset, length))
        else:
            finalized_cells.append(cell)

    # Step 4: Write row to circular buffer dataframe
    dest_row = state["head_index"]
    row_pos = calc_row_offset(state["header_size"], state["col_count"], dest_row)
    f.seek(row_pos)
    for cell_bytes in finalized_cells:
        f.write(cell_bytes)

    # Step 5: Advance circular pointers
    state["head_index"] = (state["head_index"] + 1) % state["max_entries"]
    state["entry_count"] = min(state["entry_count"] + 1, state["max_entries"])

    # Step 6: Synchronize metadata superblock to storage
    write_header(f, state)


def read_row(f: BinaryIO, state: Dict[str, Any], row_idx: int) -> Dict[str, Any]:
    """Reads and deserializes a single row by physical slot index.

    Hardware / Memory Principle:
    -----------------------------
    Demonstrates in-place word reads vs pointer dereferencing:
    - Direct cells (INT32, FLOAT): read 4 bytes and unpack immediately.
    - Indirect cells (STR, DOUBLE): read 4-byte fat pointer, compute absolute
      flash address `heap_base + offset`, seek, and read payload bytes.

    Args:
        f: Open binary file handle.
        state: Layout state dictionary.
        row_idx: Physical slot index (0 .. max_entries - 1).

    Returns:
        Dictionary mapping column names to deserialized Python values.
    """
    row_dict: Dict[str, Any] = {}
    for c, (cname, ctype) in enumerate(zip(state["col_names"], state["col_types"])):
        cell_pos = calc_cell_offset(state["header_size"], state["col_count"], row_idx, c)
        f.seek(cell_pos)
        cell_bytes = f.read(4)

        if ctype == TYPE_INT32:
            val = struct.unpack("<i", cell_bytes)[0]
        elif ctype == TYPE_FLOAT:
            val = struct.unpack("<f", cell_bytes)[0]
        elif ctype == TYPE_STR:
            offset, length = unpack_heap_ptr(cell_bytes)
            raw = read_heap_payload(f, state, offset, length)
            val = raw.decode("utf-8")
        elif ctype == TYPE_DOUBLE:
            offset, length = unpack_heap_ptr(cell_bytes)
            raw = read_heap_payload(f, state, offset, length)
            val = struct.unpack("<d", raw)[0]
        else:
            val = None

        row_dict[cname] = val
    return row_dict


def read_all(
    f: BinaryIO,
    state: Dict[str, Any],
    chronological: bool = True,
) -> List[Dict[str, Any]]:
    """Reads all valid rows currently stored in the circular ring buffer.

    Hardware / Memory Principle (Circular Buffer Chronological Unwrapping):
    ------------------------------------------------------------------------
    Because a circular ring buffer continuously wraps around, physical slot 0
    is NOT necessarily the oldest entry!
    - Case A (Buffer not yet full, entry_count < max_entries):
      Entries were inserted sequentially at slots 0, 1, ..., entry_count - 1.
      Chronological order is simply range(entry_count).
    - Case B (Buffer full, entry_count == max_entries):
      The next write slot `head_index` points to the OLDEST entry (which will
      be overwritten next).
      To read chronologically from oldest to newest, we unwrap the ring starting
      from `head_index` using modular arithmetic:
          logical_index[i] = (head_index + i) % max_entries

    Args:
        f: Open binary file handle.
        state: Layout state dictionary.
        chronological: If True, returns rows sorted from oldest to newest.
                       If False, returns physical slot order [0 .. entry_count - 1].

    Returns:
        List of dictionaries containing row records.
    """
    results: List[Dict[str, Any]] = []
    if state["entry_count"] == 0:
        return results

    if chronological and state["entry_count"] == state["max_entries"]:
        indices = [(state["head_index"] + i) % state["max_entries"] for i in range(state["max_entries"])]
    else:
        indices = list(range(state["entry_count"]))

    for r in indices:
        results.append(read_row(f, state, r))

    return results


# Descriptive educational aliases
read_all_rows = read_all


# ==============================================================================
# SECTION 5: EDUCATIONAL DIAGNOSTICS & HEX DUMP
# ==============================================================================

def dump_layout(state: Dict[str, Any], filepath: str = "") -> None:
    """Displays low-level arena metrics and partition memory boundaries.

    Hardware / Memory Principle:
    -----------------------------
    Visualizes the exact address ranges and byte boundaries allocated across
    the Header, Dataframe, and Heap Arena partitions.

    Args:
        state: Layout state dictionary.
        filepath: Optional path to display.
    """
    fp = filepath or state.get("filepath", "flatfile.bin")
    print(f"--- [Layout Diagnostic: {fp}] ---")
    print(f"Header:       0x00 .. 0x{state['header_size']:02X} ({state['header_size']} bytes)")
    print(f"Dataframe:    0x{state['header_size']:02X} .. 0x{state['heap_base']:02X} ({state['dataframe_size']} bytes)")
    print(
        f"Heap Arena:   0x{state['heap_base']:02X} .. 0x{state['total_file_size']:02X} "
        f"(Watermark: {state['heap_watermark']}/{state['max_heap_size']} bytes)"
    )
    print(
        f"Ring State:   Entries={state['entry_count']}/{state['max_entries']} "
        f"| Next Write Head Index={state['head_index']}"
    )
    print("--------------------------------------------------")


# Descriptive educational alias
inspect_layout = dump_layout


def hex_dump(f: BinaryIO, start_offset: int = 0, length: int = 128) -> str:
    """Produces an educational hexdump of raw binary storage.

    Hardware / Memory Principle:
    -----------------------------
    Simulates viewing embedded flash pages with a logic analyzer or JTAG debugger
    (like `x/bx` in GDB or `hexdump -C`). Shows address offsets, raw hex bytes,
    and printable ASCII characters.

    Args:
        f: Open binary file handle.
        start_offset: Byte offset in file to begin reading.
        length: Number of bytes to dump.

    Returns:
        Formatted multi-line hexdump string.
    """
    f.seek(start_offset)
    data = f.read(length)
    lines: List[str] = []

    for i in range(0, len(data), 16):
        chunk = data[i : i + 16]
        hex_bytes = " ".join(f"{b:02x}" for b in chunk)
        ascii_chars = "".join(chr(b) if 32 <= b <= 126 else "." for b in chunk)
        lines.append(f"{start_offset + i:08x}  {hex_bytes:<48}  |{ascii_chars}|")

    return "\n".join(lines)


# ==============================================================================
# SECTION 6: EDUCATIONAL OOP WRAPPER (OPTIONAL CONVENIENCE)
# ==============================================================================

class FlatFileBuffer:
    """Educational OOP facade wrapping the standalone flat-file engine functions.

    This class enables students to explore both low-level procedural memory
    functions and object-oriented abstractions side-by-side.
    """

    def __init__(self, filepath: str):
        self.filepath: str = filepath
        self.file: Optional[BinaryIO] = None
        self.state: Dict[str, Any] = {}

    @classmethod
    def create(
        cls,
        filepath: str,
        schema: List[Tuple[str, int]],
        max_entries: int,
        total_size: int,
    ) -> "FlatFileBuffer":
        """Initializes binary file and opens buffer instance."""
        init_file(filepath, schema, max_entries, total_size)
        instance = cls(filepath)
        instance.open()
        return instance

    def open(self) -> None:
        """Opens file in read/write binary mode and loads header state."""
        self.file = open(self.filepath, "r+b")
        self.state = read_header(self.file)
        self.state["filepath"] = self.filepath

    def close(self) -> None:
        """Flushes and closes the file handle."""
        if self.file and not self.file.closed:
            self.file.flush()
            self.file.close()

    def __enter__(self) -> "FlatFileBuffer":
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    @property
    def heap_watermark(self) -> int:
        return self.state["heap_watermark"]

    @property
    def entry_count(self) -> int:
        return self.state["entry_count"]

    @property
    def max_entries(self) -> int:
        return self.state["max_entries"]

    @property
    def head_index(self) -> int:
        return self.state["head_index"]

    @property
    def max_heap_size(self) -> int:
        return self.state["max_heap_size"]

    @property
    def header_size(self) -> int:
        return self.state["header_size"]

    @property
    def dataframe_size(self) -> int:
        return self.state["dataframe_size"]

    @property
    def heap_base(self) -> int:
        return self.state["heap_base"]

    @property
    def total_file_size(self) -> int:
        return self.state["total_file_size"]

    def append(self, row_values: List[Any]) -> None:
        """Appends row into buffer."""
        if not self.file:
            raise IOError("File not open.")
        append_row(self.file, self.state, row_values)

    def compact_heap(self) -> int:
        """Manually compacts heap arena."""
        if not self.file:
            raise IOError("File not open.")
        return compact_heap(self.file, self.state)

    def read_all(self, chronological: bool = True) -> List[Dict[str, Any]]:
        """Reads all active rows."""
        if not self.file:
            raise IOError("File not open.")
        return read_all(self.file, self.state, chronological=chronological)

    def dump_layout(self) -> None:
        """Displays layout diagnostics."""
        dump_layout(self.state, self.filepath)


# ==============================================================================
# SECTION 7: RUNNABLE DEMO & VERIFICATION
# ==============================================================================

if __name__ == "__main__":
    DEMO_FILE = "demo_sensor_buffer.bin"

    if os.path.exists(DEMO_FILE):
        os.remove(DEMO_FILE)

    demo_schema = [
        ("timestamp", TYPE_INT32),
        ("temp", TYPE_FLOAT),
        ("node_id", TYPE_STR),
        ("precision_lat", TYPE_DOUBLE),
    ]

    print("=== Step 1: Initializing Flat File with init_file ===")
    state = init_file(DEMO_FILE, demo_schema, max_entries=3, total_size=512)
    dump_layout(state, DEMO_FILE)

    with open(DEMO_FILE, "r+b") as f:
        print("\n=== Step 2: Appending 3 Initial Rows (Filling Buffer) ===")
        append_row(f, state, [1001, 25.4, "NODE-ALPHA", -8.54321012])
        append_row(f, state, [1002, 26.1, "NODE-BETA", -8.54321055])
        append_row(f, state, [1003, 25.9, "NODE-GAMMA", -8.54321099])

        print("\nCurrent records (chronological):")
        for row in read_all(f, state, chronological=True):
            print(" ", row)
        dump_layout(state, DEMO_FILE)

        print("\n=== Step 3: Appending 4th Row (Overwriting Oldest Entry at Slot 0) ===")
        print("Note: 'NODE-ALPHA' becomes orphaned dead garbage on the heap.")
        append_row(f, state, [1004, 27.2, "NODE-DELTA-OVERWRITE", -8.54321200])

        print("\nCurrent records (chronological, oldest is NODE-BETA):")
        for row in read_all(f, state, chronological=True):
            print(" ", row)
        dump_layout(state, DEMO_FILE)

        print("\n=== Step 4: Triggering Mark-Compact Compaction ===")
        pre_watermark = state["heap_watermark"]
        print(f"Pre-compaction watermark:  {pre_watermark} bytes")
        post_watermark = compact_heap(f, state)
        print(f"Post-compaction watermark: {post_watermark} bytes (Reclaimed {pre_watermark - post_watermark} bytes!)")

        print("\nRecords after compaction (verifying zero corruption):")
        for row in read_all(f, state, chronological=True):
            print(" ", row)
        dump_layout(state, DEMO_FILE)

        print("\n=== Step 5: Educational Hex Dump of Superblock (First 48 Bytes) ===")
        print(hex_dump(f, start_offset=0, length=48))

    # Clean up demo artifact
    if os.path.exists(DEMO_FILE):
        os.remove(DEMO_FILE)
    print("\n[OK] Educational module demonstration completed successfully!")
