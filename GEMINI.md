# ESP32 Flat File Circular Buffer & Mark-Compact Arena Engine

## Overview & Architecture

This specification outlines a binary flat-file engine designed to teach embedded memory layout, circular ring buffering, and mark-compact garbage collection using Python (prototyped for ESP32 / NOR Flash architectures).

```text
+-----------------------+-----------------------+-----------------------------+
|        Header         |       Dataframe       |            Heap             |
| (Schema & Metadata)   | (Fixed-width rows)    | (Variable-width allocations)|
+-----------------------+-----------------------+-----------------------------+
0                 Header_End              Dataframe_End                  File_End

```

All numeric multi-byte fields strictly adhere to **Little-Endian (`<`)** format to align with Xtensa LX6/LX7 and RISC-V hardware architectures.

---

## 1. Binary Specification

### 1.1 Header Structure (Fixed Offset: `0x00`)

| Offset | Size | Type | Format Tag | Field Name | Description |
| --- | --- | --- | --- | --- | --- |
| `0x00` | 4 B | `uint32` | `<I` # ## ### $C$ ($0 ($C$) ($N="5$)" (4 (64-bit (Single (Stored (UTF-8 (`uint16`, (e.g., ) * **Byte - --- 0–1:** 1 1.2 1.3 1024, 2. 2–3:** 32 32-bit 4 4$) 4-byte 50 65,535$). < @classmethod Any, B Byte Bytes) B | C Capacity Core Current Data Dict, Dynamic | FlatFileBuffer: For HEADER_FIXED_FORMAT="<8I" HEADER_FIXED_SIZE="struct.calcsize(HEADER_FIXED_FORMAT)" Heap IEEE-754 Identifiers Implementation KB) Layout List, List[Tuple[str, List[int]="[]" List[str]="[]" Max N$) Next Number Offset Packed Payload Pipe-delimited Pointer Python Save TYPE_DOUBLE="0x04" TYPE_FLOAT="0x02" TYPE_INT32="0x01" TYPE_STR="0x03" Total Tuple Type Var \dots \le \text{count} \text{head} \times **init**(self, `0x01` `0x02` `0x03` `0x04` `0x08` `0x0C` `0x10` `0x14` `0x18` `0x1C` `0x20` `<H`) `<I` `<f`) `<i`) `B` `FLOAT` `HEAP_DOUBLE` `HEAP_DOUBLE`, `HEAP_STR` `INT32` ```python `bytes` `col_count` `col_names` `col_types` `dataframe_size` `entry_count` `flatfile_engine.py`: `head_index` `header_size` `heap_watermark` `length` `max_entries` `offset` `temp |

```
    """Formats a new flat binary file with given schema and boundaries."""
    col_names = [c[0] for c in columns]
    col_types = [c[1] for c in columns]
    col_count = len(columns)

    col_names_raw = "|".join(col_names).encode("utf-8") + b"\x00"
    header_size = HEADER_FIXED_SIZE + col_count + len(col_names_raw)
    dataframe_size = max_entries * col_count * 4

    if header_size + dataframe_size >= total_size:
        raise ValueError("Header and Dataframe exceed total file boundary.")

    with open(filepath, "wb") as f:
        # 1. Fixed Header block
        f.write(
            struct.pack(
                HEADER_FIXED_FORMAT,
                total_size,
                header_size,
                dataframe_size,
                max_entries,
                0,  # entry_count
                0,  # head_index
                col_count,
                0,  # heap_watermark
            )
        )
        # 2. Column Types
        f.write(bytes(col_types))
        # 3. Column Names
        f.write(col_names_raw)
        # 4. Zero-pad Dataframe and Heap
        remaining = total_size - f.tell()
        f.write(b"\x00" * remaining)

    instance = cls(filepath)
    instance.open()
    return instance

def open(self):
    """Opens the flat file and maps metadata headers into memory."""
    self.file = open(self.filepath, "r+b")
    self._read_header()

def close(self):
    if self.file and not self.file.closed:
        self.file.flush()
        self.file.close()

def _read_header(self):
    self.file.seek(0)
    fixed_buf = self.file.read(HEADER_FIXED_SIZE)
    (
        self.total_file_size,
        self.header_size,
        self.dataframe_size,
        self.max_entries,
        self.entry_count,
        self.head_index,
        self.col_count,
        self.heap_watermark,
    ) = struct.unpack(HEADER_FIXED_FORMAT, fixed_buf)

    self.col_types = list(self.file.read(self.col_count))
    names_raw = b""
    while True:
        ch = self.file.read(1)
        if ch == b"\x00" or ch == b"":
            break
        names_raw += ch
    self.col_names = names_raw.decode("utf-8").split("|")

def _sync_header(self):
    self.file.seek(0)
    self.file.write(
        struct.pack(
            HEADER_FIXED_FORMAT,
            self.total_file_size,
            self.header_size,
            self.dataframe_size,
            self.max_entries,
            self.entry_count,
            self.head_index,
            self.col_count,
            self.heap_watermark,
        )
    )
    self.file.flush()

@property
def heap_base(self) -> int:
    return self.header_size + self.dataframe_size

@property
def max_heap_size(self) -> int:
    return self.total_file_size - self.heap_base

def _row_offset(self, row_idx: int) -> int:
    stride = self.col_count * 4
    return self.header_size + (row_idx * stride)

def _cell_offset(self, row_idx: int, col_idx: int) -> int:
    return self._row_offset(row_idx) + (col_idx * 4)

def compact_heap(self):
    """Mark-compact algorithm: slides live allocations to offset 0 and rewires cells."""
    live_blocks: List[Dict[str, Any]] = []

    # 1. MARK: Gather all live heap references from current active rows
    for r in range(self.entry_count):
        for c, ctype in enumerate(self.col_types):
            if ctype in (TYPE_STR, TYPE_DOUBLE):
                pos = self._cell_offset(r, c)
                self.file.seek(pos)
                offset, length = struct.unpack("<2H", self.file.read(4))
                if length > 0:
                    live_blocks.append(
                        {
                            "row": r,
                            "col": c,
                            "old_offset": offset,
                            "len": length,
                            "cell_pos": pos,
                        }
                    )

    # 2. SORT: Preserve relative allocation positions
    live_blocks.sort(key=lambda b: b["old_offset"])

    # 3. SLIDE: Relocate bytes and update pointers
    new_watermark = 0
    for block in live_blocks:
        old_abs_addr = self.heap_base + block["old_offset"]
        self.file.seek(old_abs_addr)
        data = self.file.read(block["len"])

        new_abs_addr = self.heap_base + new_watermark
        self.file.seek(new_abs_addr)
        self.file.write(data)

        # Update dataframe cell pointer
        self.file.seek(block["cell_pos"])
        self.file.write(struct.pack("<2H", new_watermark, block["len"]))

        new_watermark += block["len"]

    # Clear dead memory after the compacted watermark
    if new_watermark < self.heap_watermark:
        dead_bytes = self.heap_watermark - new_watermark
        self.file.seek(self.heap_base + new_watermark)
        self.file.write(b"\x00" * dead_bytes)

    self.heap_watermark = new_watermark
    self._sync_header()

def append(self, row_values: List[Any]):
    """Inserts a record into the circular ring buffer, compacting heap if needed."""
    if len(row_values) != self.col_count:
        raise ValueError(f"Expected {self.col_count} values, got {len(row_values)}")

    encoded_cells: List[bytes] = []
    pending_heap_allocations: List[bytes] = []

    # Step 1: Pre-process cells & encode payloads
    for val, ctype in zip(row_values, self.col_types):
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

    # Step 2: Check capacity & compact if watermark exceeds boundary
    required_heap = sum(len(b) for b in pending_heap_allocations)
    if self.heap_watermark + required_heap > self.max_heap_size:
        self.compact_heap()
        if self.heap_watermark + required_heap > self.max_heap_size:
            raise MemoryError("Heap exhausted even after compaction.")

    # Step 3: Write heap payloads and produce 4-byte packed pointers
    finalized_cells: List[bytes] = []
    for cell in encoded_cells:
        if isinstance(cell, tuple):
            ctype, raw_bytes = cell
            length = len(raw_bytes)
            offset = self.heap_watermark

            # Write to heap arena
            self.file.seek(self.heap_base + offset)
            self.file.write(raw_bytes)
            self.heap_watermark += length

            finalized_cells.append(struct.pack("<2H", offset, length))
        else:
            finalized_cells.append(cell)

    # Step 4: Write row to circular buffer
    dest_row = self.head_index
    row_pos = self._row_offset(dest_row)
    self.file.seek(row_pos)
    for cell_data in finalized_cells:
        self.file.write(cell_data)

    # Step 5: Advance circular pointers
    self.head_index = (self.head_index + 1) % self.max_entries
    self.entry_count = min(self.entry_count + 1, self.max_entries)
    self._sync_header()

def read_all(self, chronological: bool = True) -> List[Dict[str, Any]]:
    """Reads rows. If chronological=True, starts from oldest valid entry."""
    results = []
    if self.entry_count == 0:
        return results

    if chronological and self.entry_count == self.max_entries:
        indices = [(self.head_index + i) % self.max_entries for i in range(self.max_entries)]
    else:
        indices = list(range(self.entry_count))

    for r in indices:
        row_dict = {}
        for c, (cname, ctype) in enumerate(zip(self.col_names, self.col_types)):
            self.file.seek(self._cell_offset(r, c))
            cell_bytes = self.file.read(4)

            if ctype == TYPE_INT32:
                val = struct.unpack("<i", cell_bytes)[0]
            elif ctype == TYPE_FLOAT:
                val = struct.unpack("<f", cell_bytes)[0]
            elif ctype == TYPE_STR:
                offset, length = struct.unpack("<2H", cell_bytes)
                self.file.seek(self.heap_base + offset)
                val = self.file.read(length).decode("utf-8")
            elif ctype == TYPE_DOUBLE:
                offset, length = struct.unpack("<2H", cell_bytes)
                self.file.seek(self.heap_base + offset)
                val = struct.unpack("<d", self.file.read(length))[0]
            row_dict[cname] = val
        results.append(row_dict)
    return results

def dump_layout(self):
    """Displays low-level arena metrics."""
    print(f"--- [Layout Diagnostic: {self.filepath}] ---")
    print(f"Header:       0x00 .. 0x{self.header_size:02X} ({self.header_size} bytes)")
    print(f"Dataframe:    0x{self.header_size:02X} .. 0x{self.heap_base:02X} ({self.dataframe_size} bytes)")
    print(f"Heap Arena:   0x{self.heap_base:02X} .. 0x{self.total_file_size:02X} (Watermark: {self.heap_watermark}/{self.max_heap_size} bytes)")
    print(f"Ring State:   Entries={self.entry_count}/{self.max_entries} | Next Write Head Index={self.head_index}")
    print("--------------------------------------------------")

```

```

---

## 3. Teaching Verification Script

Save this script as `run_demo.py` to demonstrate the circular ring overwrite and compaction mechanism to students:

```python
import os
from flatfile_engine import (
    FlatFileBuffer,
    TYPE_INT32,
    TYPE_FLOAT,
    TYPE_STR,
    TYPE_DOUBLE,
)

DEMO_FILE = "sensor_buffer.bin"

if os.path.exists(DEMO_FILE):
    os.remove(DEMO_FILE)

schema = [
    ("timestamp", TYPE_INT32),
    ("temp", TYPE_FLOAT),
    ("node_id", TYPE_STR),
    ("precision_lat", TYPE_DOUBLE),
]

# Max 3 entries, 512 bytes total allocation to easily trigger compaction
buf = FlatFileBuffer.create(DEMO_FILE, schema, max_entries=3, total_size=512)
buf.dump_layout()

print("\n--- 1. Appending Initial 3 Rows (Filling Buffer) ---")
buf.append([1001, 25.4, "NODE-ALPHA", -8.54321012])
buf.append([1002, 26.1, "NODE-BETA", -8.54321055])
buf.append([1003, 25.9, "NODE-GAMMA", -8.54321099])

for row in buf.read_all(chronological=True):
    print(" ", row)
buf.dump_layout()

print("\n--- 2. Appending 4th Row (Overwriting Oldest Entry: Slot 0) ---")
# 'NODE-ALPHA' becomes orphaned garbage on the heap
buf.append([1004, 27.2, "NODE-DELTA-OVERWRITE", -8.54321200])

for row in buf.read_all(chronological=True):
    print(" ", row)
buf.dump_layout()

print("\n--- 3. Triggering Manual Heap Compaction (Reclaiming Lost Arena Memory) ---")
print(f"Pre-compaction watermark: {buf.heap_watermark} bytes")
buf.compact_heap()
print(f"Post-compaction watermark: {buf.heap_watermark} bytes (Decreased!)")

for row in buf.read_all(chronological=True):
    print(" ", row)
buf.dump_layout()

buf.close()

```

---

## 4. Key Takeaways for Students

1. **Deterministic Access Times:** Because row strides are constant ($C \times 4\text{ bytes}$), computing cell offsets runs in $\mathcal{O}(1)$ time using basic arithmetic (`base + row * stride + col * 4`).
2. **Double-Indirection Without Fragmentation:** Allocating small strings to an arena avoids variable-sized rows in the dataframe, making pointer indexing trivial on microcontrollers.
3. **Sliding Compactor:** Bump allocators are fast ($\mathcal{O}(1)$ insertion), but in circular workloads they leak space. The mark-compact pass demonstrates the mechanics of real memory runtimes without external dependencies.