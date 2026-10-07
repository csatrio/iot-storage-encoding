# ESP32 Flat-File Binary Buffer & Mark-Compact GC Simulator

An interactive, educational simulator designed to teach computer systems and IoT students how embedded operating systems manage storage on **NOR Flash** and **microcontrollers (such as ESP32, Xtensa, and RISC-V)**.

Students will explore:
1. **Binary Data Encoding:** Storing numeric and text telemetry in compact, Little-Endian (`<`) binary formats instead of bloated text formats like JSON.
2. **Circular Ring Buffering:** Logging continuous time-series records into fixed-stride slots with $\mathcal{O}(1)$ deterministic addressing.
3. **Mark-Compact Heap Garbage Collection:** Defragmenting an arena allocator when circular buffer overwrites orphan old variable-length strings and doubles.

---

## 🚀 Quickstart Guide

### 1. Prerequisites & Installation

Make sure you have **Python 3.9+** installed.

```bash
# Clone or navigate to the project directory
cd iot-demo

# Optional: Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate    # On Windows: .venv\Scripts\activate

# Install minimal backend dependencies (Flask & Flask-CORS)
pip install -r requirements.txt
```

---

### 2. Launch the Web Visualizer

Run the local web server:

```bash
python3 app.py
```

Open your web browser and go to:
👉 **[http://localhost:5100](http://localhost:5100)**

*(No node, npm, or frontend build steps are needed! The visualizer runs directly via CDN-loaded Preact and Tailwind CSS).*

---

### 3. Run the CLI Storage Benchmark

To see how compact binary encoding compares against standard JSON over 10,000 real greenhouse telemetry records:

```bash
python3 demo.py
```

This benchmark:
- Generates 10,000 greenhouse sensor readings.
- Saves them as formatted JSON, minified JSON, and compact binary (`greenhouse_compact.bin`).
- Demonstrates **~85% storage space savings** and **lossless round-trip deserialization**.

---

## 🖥️ How to Use the Interactive Web Visualizer

When you open **`http://localhost:5100`**, you will see an interactive dashboard with live telemetry and visual memory maps.

### Step-by-Step Lab Walkthrough

```text
[ ➕ Push Next Reading ] -> [ Overwrite Oldest Slot ] -> [ 🧹 Force Mark-Compact GC ]
           │                                │                              │
       Fills Slot                 Orphans Heap String             Reclaims Lost Arena Space
```

#### Step 1: Push Telemetry Records
- Click **"➕ Push Next Reading"** to log an IoT reading (temperature, humidity, node identifier, GPS latitude).
- Watch **Slot #0** fill up in the Circular Ring Buffer table. Notice the **Write Head** moves forward to Slot #1.

#### Step 2: Observe Ring Buffer Overwriting & Garbage Creation
- Click **"➕ Push Next Reading"** until all slots are filled (by default, 5 slots).
- Click **"➕ Push Next Reading"** once more!
- **What happens?** The circular ring wraps around and overwrites Slot #0.
- Because Slot #0's previous string (`NODE-ALPHA`) is replaced by a new string, the old string on the heap arena becomes **Orphaned Dead Space (Garbage)**.
- Notice the **NOR Flash Memory Map** highlights the leaked memory in **striped red** (`⚠️ LEAKED`).

#### Step 3: Trigger Mark-Compact Garbage Collection
- Click the blinking red **"🧹 Force Mark-Compact GC"** button.
- The engine executes a three-phase compaction pass:
  1. **Mark:** Scans active slots to find live pointers.
  2. **Sort:** Sorts allocations by offset to preserve order.
  3. **Slide:** Relocates live strings back to offset 0, updates the dataframe cell pointers, and zeroes the freed memory.
- Notice how the heap watermark shrinks and all fragmented memory is defragmented into contiguous free space!

#### Step 4: Compare JSON vs. Binary Encoding
- Scroll to the **"Original JSON Data vs Packed Binary Representation"** card.
- Click between **"Formatted"** and **"Minified"** to compare text JSON sizes (~120 bytes) against the packed binary record (37 bytes).
- Hover over any JSON field (e.g. `"humidity": 62.0`) to see the exact corresponding Little-Endian binary cell `[ 00 00 78 42 ]` light up!

---

## 🧠 Memory Layout & File Structure

The binary file is partitioned into three contiguous regions:

```text
+-----------------------+-----------------------+-----------------------------+
|        Header         |       Dataframe       |            Heap             |
| (Schema & Metadata)   | (Fixed-width rows)    | (Variable-width allocations)|
+-----------------------+-----------------------+-----------------------------+
0x00               Header_End              Dataframe_End                  File_End
```

### 1. Header (Byte-by-Byte Architecture)

The header starts at offset `0x00` and contains:

1. **Fixed 32-Byte Superblock (`<8I`, 8 unsigned 32-bit integers):**
   - `0x00 .. 0x03` (4B): `total_file_size` (Pre-allocated boundary, e.g. 1024 bytes)
   - `0x04 .. 0x07` (4B): `header_size` (Base offset where dataframe rows begin)
   - `0x08 .. 0x0B` (4B): `dataframe_size` (`max_entries * col_count * 4` bytes)
   - `0x0C .. 0x0F` (4B): `max_entries` (Ring buffer capacity, e.g. 5 slots)
   - `0x10 .. 0x13` (4B): `entry_count` (Number of active populated records)
   - `0x14 .. 0x17` (4B): `head_index` (Write head ring pointer)
   - `0x18 .. 0x1B` (4B): `col_count` (Number of columns)
   - `0x1C .. 0x1F` (4B): `heap_watermark` (Current bump allocator offset in heap)
2. **Column Type Identifier Array (`uint8`, 1 byte each at `0x20`):**
   - `0x01` (`TYPE_INT32`): 4-byte signed integer (`<i`)
   - `0x02` (`TYPE_FLOAT`): 4-byte IEEE-754 float (`<f`)
   - `0x03` (`TYPE_STR`): 4-byte fat pointer (`<2H`: 2B offset + 2B length $\rightarrow$ UTF-8 heap string)
   - `0x04` (`TYPE_DOUBLE`): 4-byte fat pointer (`<2H`: 2B offset + 2B length $\rightarrow$ 8B float `<d` on heap)
3. **Column Names String (starts at `0x20 + col_count`):**
   - A single pipe-delimited string terminated with a null byte (`\0`):  
     `"timestamp|temp|humidity|node_id|precision_lat\0"`
4. **Derived Heap Boundary Formula:**
   $$\text{Heap Base} = \text{Header Size} + \text{Dataframe Size}$$
   $$\text{Max Heap Size} = \text{Total File Size} - \text{Heap Base}$$

---

### 2. Fixed Dataframe Rows ($\mathcal{O}(1)$ Random Access)

Every cell in every row is strictly **4 bytes** wide:
- Numeric types (`INT32`, `FLOAT`) are stored **directly in-place**.
- Variable-length types (`STR`, `DOUBLE`) store a **4-byte Fat Pointer** (`<2H`):
  - 2 bytes: Offset into the heap arena.
  - 2 bytes: Length of the payload in bytes.

Because every row has a fixed stride ($C \times 4\text{ bytes}$), computing cell offsets runs in $\mathcal{O}(1)$ time:
$$\text{Cell Offset} = \text{Header Size} + (\text{row\_index} \times \text{col\_count} \times 4) + (\text{col\_index} \times 4)$$

---

### 3. Heap Arena (Bump Allocator & Sliding Compactor)

- Appends use a fast $\mathcal{O}(1)$ bump allocator (`watermark += payload_length`).
- When circular buffer overwrites occur, abandoned strings leak space.
- Compacting slides live memory contiguously towards offset `0x00` and rewires cell pointers, restoring maximum contiguous free space without flash sector fragmentation.

---

## 📂 Project Structure

```text
iot-demo/
├── functions.py          # Core standalone binary engine (pure Python, struct & os)
├── app.py                # Flask REST backend (listening on http://localhost:5100)
├── demo.py               # CLI benchmark (10,000 greenhouse readings: JSON vs Binary)
├── requirements.txt      # Minimal dependencies (flask, flask-cors)
├── templates/
│   └── index.html        # Clean HTML5 visualizer container
├── static/
│   └── index.js          # Interactive Preact + HTM application logic
└── README.md             # Student lab manual and guide (this file)
```

---

## 💡 Key Takeaways for Students

1. **Why avoid JSON on microcontrollers?**  
   Text formats repeatedly store field names (like `"timestamp"`) with every single record. In our demo, text keys alone consume over **58%** of the payload. Binary encoding stores schema names **once in the file header**, cutting flash write wear by over **3x**.
2. **Why Little-Endian (`<`) everywhere?**  
   Modern microcontroller architectures like ESP32 (Xtensa LX6/LX7 and ESP32-C3 RISC-V) and ARM Cortex-M store numbers with the least significant byte first. Using Little-Endian allows direct memory copying without byte-swapping overhead.
3. **Why Fixed 4-Byte Cells with Indirect Pointers?**  
   Storing variable strings directly inside rows makes row lengths unpredictable ($\mathcal{O}(N)$ lookup). Storing fixed 4-byte fat pointers in the row table and payloads in an arena keeps row strides constant ($\mathcal{O}(1)$ lookup) while still allowing variable strings.
