/**
 * ESP32 Flat-File Binary Buffer & Mark-Compact GC Simulator
 * Client Application Logic (Preact + HTM)
 * 
 * Features:
 * - Robust timeout timers, watchdog mutex locks, and latency telemetry on every operation.
 * - NEW: Whole Header Format Byte-by-Byte Visualizer (Fixed Superblock, Column Types, Schema String, Derived Heap).
 * - NOR Flash Memory Map (Linear Allocation).
 * - Interactive Side-by-Side Inspector: Original Uncompressed JSON vs Packed Binary Representation.
 * - Bidirectional field cross-highlighting between JSON keys and Little-Endian binary cells/heap.
 */

import { html, render, useState, useEffect, useRef } from 'https://unpkg.com/htm/preact/standalone.module.js';

// Constant sensor node identifiers for simulation
const PRESET_NODES = [
  'NODE-ALPHA',
  'NODE-BETA',
  'NODE-GAMMA',
  'NODE-DELTA',
  'NODE-EPSILON',
  'NODE-ZETA',
  'NODE-ETA',
  'NODE-THETA'
];

/**
 * Robust fetch wrapper with configurable watchdog timer.
 * Aborts cleanly if the network or backend takes longer than timeoutMs.
 */
async function fetchWithTimeout(url, options = {}, timeoutMs = 5000) {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => {
    controller.abort();
  }, timeoutMs);

  try {
    const res = await fetch(url, {
      ...options,
      signal: controller.signal
    });
    return res;
  } catch (err) {
    if (err.name === 'AbortError') {
      throw new Error(`Operation timed out after ${timeoutMs / 1000}s (watchdog timer triggered)`);
    }
    throw err;
  } finally {
    clearTimeout(timeoutId);
  }
}

function App() {
  const [state, setState] = useState(null);
  const [loading, setLoading] = useState(true);
  const [autoPlay, setAutoPlay] = useState(false);
  const [deduplicate, setDeduplicate] = useState(false);
  const [alertBanner, setAlertBanner] = useState(null);
  const [highlightOffset, setHighlightOffset] = useState(null);
  const [showCustomForm, setShowCustomForm] = useState(false);

  // Header Visualizer States
  const [hoveredHeaderField, setHoveredHeaderField] = useState(null);
  const [hoveredHeaderByte, setHoveredHeaderByte] = useState(null);

  // Inspector & Visualization States
  const [selectedSlotIdx, setSelectedSlotIdx] = useState(0);
  const [jsonViewMode, setJsonViewMode] = useState('formatted'); // 'formatted' | 'minified'
  const [inspectorViewMode, setInspectorViewMode] = useState('record'); // 'record' | 'batch'
  const [hoveredField, setHoveredField] = useState(null);
  const [copiedJson, setCopiedJson] = useState(false);

  // Operation Timer & Watchdog State
  const [isOperating, setIsOperating] = useState(false);
  const [currentAction, setCurrentAction] = useState(null);
  const [opElapsedMs, setOpElapsedMs] = useState(0);
  const [lastLatency, setLastLatency] = useState({
    serverMs: 0,
    roundtripMs: 0,
    action: 'None',
    timestamp: 'Initial'
  });

  const [customRow, setCustomRow] = useState({
    temp: 24.5,
    humidity: 65.0,
    node_id: 'NODE-CUSTOM',
    lat: -7.12345678
  });

  // Mutable refs to prevent stale closures inside timers
  const isOperatingRef = useRef(false);
  isOperatingRef.current = isOperating;

  const currentActionRef = useRef(currentAction);
  currentActionRef.current = currentAction;

  const autoPlayRef = useRef(autoPlay);
  autoPlayRef.current = autoPlay;

  const deduplicateRef = useRef(deduplicate);
  deduplicateRef.current = deduplicate;

  const stepCounterRef = useRef(1001);
  const consecutiveFailuresRef = useRef(0);

  /**
   * Universal Operation Runner with Watchdog Timer:
   * - Prevents race conditions / double-clicks (Mutex).
   * - Starts a live duration elapsed timer (ticks every 50ms).
   * - Sets a 6-second watchdog timer to unconditionally release lock if unhandled.
   * - Measures exact client round-trip latency.
   */
  const runWithTimer = async (actionName, fn, timeoutMs = 5000) => {
    if (isOperatingRef.current) {
      console.warn(`[Watchdog] Dropped "${actionName}": "${currentActionRef.current}" is currently executing.`);
      return;
    }

    setIsOperating(true);
    isOperatingRef.current = true;
    setCurrentAction(actionName);
    currentActionRef.current = actionName;
    setOpElapsedMs(0);

    const startTime = performance.now();

    // Elapsed timer ticker for live UI display
    const ticker = setInterval(() => {
      setOpElapsedMs(Math.round(performance.now() - startTime));
    }, 50);

    // Safety watchdog: Unconditionally resets lock after timeoutMs + 1000ms
    const safetyWatchdog = setTimeout(() => {
      if (isOperatingRef.current) {
        console.error(`[Watchdog] Safety timeout hit for "${actionName}". Forcing lock release.`);
        clearInterval(ticker);
        setIsOperating(false);
        isOperatingRef.current = false;
        setCurrentAction(null);
        currentActionRef.current = null;
        showAlert(`Watchdog Alert: "${actionName}" took longer than safety limit. UI unlocked.`, 'error');
      }
    }, timeoutMs + 1000);

    try {
      const serverResult = await fn();
      const roundtripMs = Math.round(performance.now() - startTime);
      const serverMs = (serverResult && serverResult.latency_ms) ? serverResult.latency_ms : 0;

      setLastLatency({
        serverMs,
        roundtripMs,
        action: actionName,
        timestamp: new Date().toLocaleTimeString()
      });

      // Reset failure counter on success
      consecutiveFailuresRef.current = 0;
    } catch (err) {
      console.error(`[Error] Action "${actionName}" failed:`, err);
      showAlert(err.message || `Action "${actionName}" failed`, 'error');

      consecutiveFailuresRef.current++;
      if (consecutiveFailuresRef.current >= 3 && autoPlayRef.current) {
        setAutoPlay(false);
        showAlert('Auto-stream paused due to 3 consecutive operation failures.', 'warning');
      }
    } finally {
      clearInterval(ticker);
      clearTimeout(safetyWatchdog);
      setIsOperating(false);
      isOperatingRef.current = false;
      setCurrentAction(null);
      currentActionRef.current = null;
    }
  };

  // 1. Initial State Fetch
  const fetchState = async () => {
    return runWithTimer('Fetch State', async () => {
      const res = await fetchWithTimeout('/api/state', {}, 5000);
      const data = await res.json();
      if (data.success) {
        setState(data.state);
        return data;
      } else {
        throw new Error(data.error || 'Failed to fetch simulator state');
      }
    });
  };

  useEffect(() => {
    (async () => {
      try {
        await fetchState();
      } catch (err) {
        console.error('Initial mount error:', err);
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  // 2. Auto-Play Streamer (Protected with Timer & In-Flight Guard)
  useEffect(() => {
    let interval = null;
    if (autoPlay) {
      interval = setInterval(() => {
        // Skip tick if an operation is still in-flight to prevent queue pile-ups
        if (isOperatingRef.current) {
          console.log('[Auto-Play] Skipping tick: previous operation still active');
          return;
        }
        handleQuickAppend();
      }, 1200);
    }
    return () => {
      if (interval) clearInterval(interval);
    };
  }, [autoPlay, deduplicate]);

  // 3. Append Row
  const handleQuickAppend = async () => {
    const step = stepCounterRef.current++;
    const nodeIndex = (step - 1001) % PRESET_NODES.length;
    const nodeName = PRESET_NODES[nodeIndex];
    const temp = parseFloat((20.0 + (step % 15) + (Math.sin(step) * 2)).toFixed(2));
    const hum = parseFloat((50.0 + (Math.cos(step) * 25)).toFixed(2));
    const lat = parseFloat((-7.12345678 + ((step % 10) * 0.0001)).toFixed(8));

    const row = [step, temp, hum, nodeName, lat];
    await sendAppend(row);
  };

  const handleCustomSubmit = async (e) => {
    if (e) e.preventDefault();
    const step = stepCounterRef.current++;
    const row = [
      step,
      parseFloat(customRow.temp),
      parseFloat(customRow.humidity),
      customRow.node_id.trim() || 'NODE-CUSTOM',
      parseFloat(customRow.lat)
    ];
    await sendAppend(row);
  };

  const sendAppend = async (row) => {
    return runWithTimer('Append Row', async () => {
      const res = await fetchWithTimeout('/api/append', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ row, deduplicate: deduplicateRef.current })
      }, 5000);

      const data = await res.json();
      if (data.success) {
        setState(data.state);
        // Select the newly written slot so students immediately see the new record in the inspector
        const writtenSlot = (data.state.metadata.head_index - 1 + data.state.metadata.max_entries) % data.state.metadata.max_entries;
        setSelectedSlotIdx(writtenSlot);

        // Check if ring wrapped
        if (data.state.metadata.entry_count === data.state.metadata.max_entries && data.state.heap_analysis.dead_bytes > 0) {
          showAlert('Slot overwritten! Old heap allocation became ORPHANED GARBAGE.', 'warning');
        }
        return data;
      } else {
        if (autoPlayRef.current) setAutoPlay(false);
        throw new Error(data.error || 'Append failed (Heap full! Try Compacting)');
      }
    });
  };

  // 4. Force Mark-Compact GC
  const handleCompact = async () => {
    return runWithTimer('Mark-Compact GC', async () => {
      const res = await fetchWithTimeout('/api/compact', { method: 'POST' }, 5000);
      const data = await res.json();
      if (data.success) {
        setState(data.state);
        showAlert(
          `Mark-Compact GC Complete! Reclaimed ${data.reclaimed_bytes} bytes. Watermark slid from 0x${data.pre_watermark.toString(16).toUpperCase()} to 0x${data.post_watermark.toString(16).toUpperCase()}.`,
          'success'
        );
        return data;
      } else {
        throw new Error(data.error || 'Compaction failed');
      }
    });
  };

  // 5. Reset Buffer
  const handleReset = async () => {
    setAutoPlay(false);
    return runWithTimer('Reset Buffer', async () => {
      const res = await fetchWithTimeout('/api/reset', { method: 'POST' }, 5000);
      const data = await res.json();
      if (data.success) {
        setState(data.state);
        stepCounterRef.current = 1001;
        setSelectedSlotIdx(0);
        showAlert('Binary buffer reset to clean initial state (5 slots, 1024 bytes).', 'info');
        return data;
      } else {
        throw new Error(data.error || 'Reset failed');
      }
    });
  };

  // 6. Capacity Initialization
  const handleInitCapacity = async (entries, size) => {
    setAutoPlay(false);
    return runWithTimer(`Init Capacity (${entries} slots)`, async () => {
      const res = await fetchWithTimeout('/api/init', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ max_entries: entries, total_size: size })
      }, 5000);

      const data = await res.json();
      if (data.success) {
        setState(data.state);
        stepCounterRef.current = 1001;
        setSelectedSlotIdx(0);
        showAlert(`Re-initialized flat-file: ${entries} slots capacity (${size} B boundary).`, 'info');
        return data;
      } else {
        throw new Error(data.error || 'Initialization failed');
      }
    });
  };

  const handleCopyJson = (text) => {
    navigator.clipboard.writeText(text);
    setCopiedJson(true);
    setTimeout(() => setCopiedJson(false), 2000);
  };

  const showAlert = (message, type = 'info') => {
    setAlertBanner({ message, type });
    setTimeout(() => {
      setAlertBanner(current => (current && current.message === message ? null : current));
    }, 5000);
  };

  // Loading Screen Fallback
  if (loading || !state) {
    return html`
      <div class="flex items-center justify-center min-h-screen">
        <div class="text-center space-y-4">
          <div class="w-12 h-12 border-4 border-purple-500 border-t-transparent rounded-full animate-spin mx-auto"></div>
          <p class="text-slate-400 font-mono text-sm">Mounting ESP32 Flat-File Simulator...</p>
        </div>
      </div>
    `;
  }

  const { metadata, physical_rows, chronological_rows, heap_analysis, savings, batch_json, header_layout, hex_preview } = state;

  // Active selected row for the JSON vs Binary Inspector
  const activeSelectedRow = physical_rows[selectedSlotIdx] || physical_rows.find(r => r.is_active) || physical_rows[0];

  // Memory breakdown percentages
  const totalSize = metadata.total_file_size;
  const headerPct = (metadata.header_size / totalSize) * 100;
  const dfPct = (metadata.dataframe_size / totalSize) * 100;
  const liveHeapPct = (heap_analysis.live_bytes / totalSize) * 100;
  const deadHeapPct = (heap_analysis.dead_bytes / totalSize) * 100;
  const freeHeapPct = (heap_analysis.free_bytes / totalSize) * 100;

  return html`
    <div class="min-h-screen pb-16">
      <!-- Top Navigation Bar -->
      <header class="border-b border-slate-800 bg-slate-900/60 backdrop-blur sticky top-0 z-40">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-3 flex flex-wrap items-center justify-between gap-4">
          <div class="flex items-center space-x-3">
            <div class="w-9 h-9 rounded-xl bg-gradient-to-br from-purple-600 via-indigo-600 to-blue-600 flex items-center justify-center font-bold text-white shadow-lg shadow-purple-500/20">
              ⚡
            </div>
            <div>
              <h1 class="text-base font-bold text-white flex items-center gap-2">
                ESP32 Flat-File Binary Engine
                <span class="text-xs px-2 py-0.5 rounded-full bg-purple-500/10 text-purple-400 border border-purple-500/20 font-mono font-medium">Interactive Simulator</span>
              </h1>
              <p class="text-xs text-slate-400">Little-Endian (&lt;) • O(1) Fixed Strides • Mark-Compact Heap Arena</p>
            </div>
          </div>

          <!-- Quick Status & Watchdog Timer Pill -->
          <div class="flex items-center gap-2 sm:gap-3 text-xs font-mono">
            <!-- Active Operation Timer Badge -->
            ${isOperating ? html`
              <div class="px-3 py-1.5 rounded-lg bg-amber-500/20 border border-amber-500/50 text-amber-300 flex items-center gap-2 animate-pulse">
                <span class="w-2 h-2 rounded-full bg-amber-400"></span>
                <span>⏱️ ${currentAction} (${opElapsedMs}ms)</span>
              </div>
            ` : html`
              <div class="px-2.5 py-1.5 rounded-lg bg-slate-800/80 border border-slate-700/60 flex items-center gap-2 text-slate-400">
                <span class="text-emerald-400">⚡</span>
                <span title="Roundtrip latency of last executed operation">RT: <strong class="text-slate-200">${lastLatency.roundtripMs}ms</strong></span>
                <span class="text-slate-600">|</span>
                <span title="Flask backend execution latency">API: <strong class="text-slate-200">${lastLatency.serverMs}ms</strong></span>
              </div>
            `}

            <div class="px-3 py-1.5 rounded-lg bg-slate-800/80 border border-slate-700/60 flex items-center gap-2">
              <span class="w-2 h-2 rounded-full ${metadata.entry_count === metadata.max_entries ? 'bg-amber-400 animate-pulse' : 'bg-emerald-400'}"></span>
              <span class="text-slate-400">Ring:</span>
              <span class="font-bold text-slate-200">${metadata.entry_count} / ${metadata.max_entries} slots</span>
            </div>

            <div class="px-3 py-1.5 rounded-lg bg-slate-800/80 border border-slate-700/60 flex items-center gap-2">
              <span class="text-slate-400">Head:</span>
              <span class="font-bold text-purple-400">#${metadata.head_index}</span>
            </div>

            <div class="px-3 py-1.5 rounded-lg bg-slate-800/80 border border-slate-700/60 flex items-center gap-2">
              <span class="text-slate-400">Garbage:</span>
              <span class="font-bold ${heap_analysis.dead_bytes > 0 ? 'text-rose-400' : 'text-slate-400'}">${heap_analysis.dead_bytes} B</span>
            </div>
          </div>
        </div>
      </header>

      <!-- Alert Notification Banner -->
      ${alertBanner && html`
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 mt-4 transition-all">
          <div class="p-3 rounded-xl border flex items-center justify-between text-sm shadow-lg ${
            alertBanner.type === 'warning' ? 'bg-amber-950/40 border-amber-800/80 text-amber-200' :
            alertBanner.type === 'success' ? 'bg-emerald-950/40 border-emerald-800/80 text-emerald-200' :
            alertBanner.type === 'error' ? 'bg-rose-950/40 border-rose-800/80 text-rose-200' :
            'bg-blue-950/40 border-blue-800/80 text-blue-200'
          }">
            <div class="flex items-center gap-2.5">
              <span class="text-base">${
                alertBanner.type === 'warning' ? '⚠️' :
                alertBanner.type === 'success' ? '✨' :
                alertBanner.type === 'error' ? '🚨' : 'ℹ️'
              }</span>
              <span>${alertBanner.message}</span>
            </div>
            <button onClick=${() => setAlertBanner(null)} class="text-xs px-2 py-1 rounded hover:bg-white/10 opacity-70 hover:opacity-100">Dismiss</button>
          </div>
        </div>
      `}

      <!-- Main Dashboard Content -->
      <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 mt-6 space-y-6">

        <!-- 1. Interactive Control Panel Bar -->
        <section class="bg-slate-900/70 border border-slate-800 rounded-2xl p-4 sm:p-5 shadow-xl backdrop-blur">
          <div class="flex flex-col lg:flex-row items-stretch lg:items-center justify-between gap-4">
            
            <!-- Action Buttons with Timer Protection -->
            <div class="flex flex-wrap items-center gap-2.5">
              <button 
                onClick=${handleQuickAppend} 
                disabled=${isOperating}
                class="px-4 py-2.5 rounded-xl bg-gradient-to-r from-purple-600 to-indigo-600 hover:from-purple-500 hover:to-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed text-white font-medium text-xs sm:text-sm shadow-lg shadow-purple-500/25 active:scale-95 transition-all flex items-center gap-2"
              >
                <span>➕</span> Push Next Reading
              </button>

              <button 
                onClick=${() => setAutoPlay(!autoPlay)} 
                class="px-4 py-2.5 rounded-xl border text-xs sm:text-sm font-medium transition-all flex items-center gap-2 ${
                  autoPlay
                    ? 'bg-amber-500/20 border-amber-500/60 text-amber-300 shadow-lg shadow-amber-500/20'
                    : 'bg-slate-800/80 hover:bg-slate-700/80 border-slate-700 text-slate-200'
                }"
              >
                <span>${autoPlay ? '⏸ Pause Stream' : '▶ Auto-Stream (1.2s)'}</span>
                ${autoPlay && html`<span class="w-2 h-2 rounded-full bg-amber-400 animate-ping"></span>`}
              </button>

              <button 
                onClick=${handleCompact} 
                disabled=${isOperating || heap_analysis.dead_bytes === 0}
                class="px-4 py-2.5 rounded-xl font-medium text-xs sm:text-sm transition-all flex items-center gap-2 border ${
                  heap_analysis.dead_bytes > 0 && !isOperating
                    ? 'bg-rose-600 hover:bg-rose-500 text-white shadow-lg shadow-rose-500/25 animate-bounce border-rose-500'
                    : 'bg-slate-800/40 border-slate-800 text-slate-500 cursor-not-allowed'
                }"
              >
                <span>🧹</span> Force Mark-Compact GC
                ${heap_analysis.dead_bytes > 0 && html`
                  <span class="px-1.5 py-0.5 rounded-full bg-rose-900 text-rose-200 text-xs font-mono font-bold">
                    ${heap_analysis.dead_bytes}B
                  </span>
                `}
              </button>

              <button 
                onClick=${() => setShowCustomForm(!showCustomForm)} 
                class="px-3.5 py-2.5 rounded-xl border text-xs sm:text-sm font-medium transition-all flex items-center gap-1.5 ${
                  showCustomForm
                    ? 'bg-purple-600/30 border-purple-500 text-purple-200'
                    : 'bg-slate-800/60 hover:bg-slate-700/60 border-slate-700/80 text-slate-300'
                }"
              >
                <span>✏️</span> Custom Row
              </button>

              <button 
                onClick=${handleReset} 
                disabled=${isOperating}
                class="px-3.5 py-2.5 rounded-xl bg-slate-800/60 hover:bg-slate-700/60 border border-slate-700/80 text-slate-300 hover:text-white text-xs sm:text-sm transition-all disabled:opacity-50"
                title="Reset binary buffer"
              >
                🔄 Reset
              </button>
            </div>

            <!-- Simulation Settings: Capacity Presets & Deduplication Toggle -->
            <div class="flex flex-wrap items-center gap-3 bg-slate-950/60 p-2 sm:px-3 rounded-xl border border-slate-800 text-xs">
              <!-- Capacity Presets -->
              <div class="flex items-center gap-1.5">
                <span class="text-slate-400">Capacity:</span>
                <button 
                  onClick=${() => handleInitCapacity(3, 512)} 
                  disabled=${isOperating}
                  class="px-2 py-0.5 rounded font-mono text-[11px] font-semibold border ${metadata.max_entries === 3 ? 'bg-purple-500/20 border-purple-500 text-purple-300' : 'bg-slate-800 border-slate-700 text-slate-400 hover:text-slate-200'}"
                  title="3 slots, 512B total (fast ring wrap & compaction)"
                >
                  3
                </button>
                <button 
                  onClick=${() => handleInitCapacity(5, 1024)} 
                  disabled=${isOperating}
                  class="px-2 py-0.5 rounded font-mono text-[11px] font-semibold border ${metadata.max_entries === 5 ? 'bg-purple-500/20 border-purple-500 text-purple-300' : 'bg-slate-800 border-slate-700 text-slate-400 hover:text-slate-200'}"
                  title="5 slots, 1024B total (default)"
                >
                  5
                </button>
                <button 
                  onClick=${() => handleInitCapacity(8, 2048)} 
                  disabled=${isOperating}
                  class="px-2 py-0.5 rounded font-mono text-[11px] font-semibold border ${metadata.max_entries === 8 ? 'bg-purple-500/20 border-purple-500 text-purple-300' : 'bg-slate-800 border-slate-700 text-slate-400 hover:text-slate-200'}"
                  title="8 slots, 2048B total"
                >
                  8
                </button>
              </div>

              <span class="text-slate-700">|</span>

              <div class="flex items-center gap-2">
                <span class="text-slate-400 font-medium">Interning / Pool:</span>
                <button 
                  onClick=${() => setDeduplicate(!deduplicate)}
                  class="px-2.5 py-1 rounded-lg font-mono font-semibold transition-all ${
                    deduplicate
                      ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/40'
                      : 'bg-slate-800 text-slate-400 border border-slate-700'
                  }"
                >
                  ${deduplicate ? 'ENABLED' : 'DISABLED'}
                </button>
              </div>
            </div>

          </div>

          <!-- Collapsible Custom Row Form -->
          ${showCustomForm && html`
            <form onSubmit=${handleCustomSubmit} class="mt-4 pt-4 border-t border-slate-800/80 grid grid-cols-2 sm:grid-cols-5 gap-3 text-xs">
              <div>
                <label class="block text-slate-400 mb-1 font-mono text-[11px]">Temperature (°C)</label>
                <input 
                  type="number" 
                  step="0.01" 
                  value=${customRow.temp} 
                  onInput=${(e) => setCustomRow({ ...customRow, temp: e.target.value })}
                  class="w-full px-2.5 py-1.5 rounded-lg bg-slate-950 border border-slate-800 text-slate-200 font-mono focus:border-purple-500 focus:outline-none"
                />
              </div>
              <div>
                <label class="block text-slate-400 mb-1 font-mono text-[11px]">Humidity (%)</label>
                <input 
                  type="number" 
                  step="0.01" 
                  value=${customRow.humidity} 
                  onInput=${(e) => setCustomRow({ ...customRow, humidity: e.target.value })}
                  class="w-full px-2.5 py-1.5 rounded-lg bg-slate-950 border border-slate-800 text-slate-200 font-mono focus:border-purple-500 focus:outline-none"
                />
              </div>
              <div>
                <label class="block text-slate-400 mb-1 font-mono text-[11px]">Node ID (String)</label>
                <input 
                  type="text" 
                  value=${customRow.node_id} 
                  onInput=${(e) => setCustomRow({ ...customRow, node_id: e.target.value })}
                  class="w-full px-2.5 py-1.5 rounded-lg bg-slate-950 border border-slate-800 text-slate-200 font-mono focus:border-purple-500 focus:outline-none"
                />
              </div>
              <div>
                <label class="block text-slate-400 mb-1 font-mono text-[11px]">Latitude (Double)</label>
                <input 
                  type="number" 
                  step="0.00000001" 
                  value=${customRow.lat} 
                  onInput=${(e) => setCustomRow({ ...customRow, lat: e.target.value })}
                  class="w-full px-2.5 py-1.5 rounded-lg bg-slate-950 border border-slate-800 text-slate-200 font-mono focus:border-purple-500 focus:outline-none"
                />
              </div>
              <div class="flex items-end">
                <button 
                  type="submit" 
                  disabled=${isOperating}
                  class="w-full py-1.5 rounded-lg bg-purple-600 hover:bg-purple-500 disabled:opacity-50 text-white font-medium text-xs shadow-md transition-all flex items-center justify-center gap-1.5"
                >
                  <span>Append Custom</span>
                </button>
              </div>
            </form>
          `}
        </section>

        <!-- ========================================================================= -->
        <!-- 2. NEW FEATURE: WHOLE HEADER FORMAT BYTE-BY-BYTE ARCHITECTURE VISUALIZER  -->
        <!-- (Rendered BEFORE the card "NOR Flash Memory Map (Linear Allocation)")      -->
        <!-- ========================================================================= -->
        ${header_layout && html`
          <section class="bg-slate-900/85 border border-blue-900/50 rounded-2xl p-5 shadow-2xl relative overflow-hidden">
            <!-- Subtle Blue Glow Accent -->
            <div class="absolute -top-24 -left-24 w-80 h-80 bg-blue-600/10 rounded-full blur-3xl pointer-events-none"></div>

            <!-- Card Header -->
            <div class="flex flex-col lg:flex-row items-start lg:items-center justify-between gap-4 pb-4 border-b border-slate-800">
              <div>
                <div class="flex items-center gap-2">
                  <span class="p-1.5 rounded-lg bg-blue-500/20 text-blue-400 font-bold">📋</span>
                  <h2 class="text-sm sm:text-base font-bold text-white tracking-wide">
                    Binary Flat-File Header Architecture (Byte-by-Byte Layout)
                  </h2>
                </div>
                <p class="text-xs text-slate-400 mt-1">
                  Fixed Superblock (<span class="font-mono text-blue-300">&lt;8I</span>, 32B) • Column Type Tags (<span class="font-mono text-emerald-300">uint8</span>) • Pipe-delimited Schema String (<span class="font-mono text-amber-300">UTF-8</span>) • Derived Heap
                </p>
              </div>

              <!-- Header Summary Metrics -->
              <div class="flex flex-wrap items-center gap-2 font-mono text-xs">
                <div class="px-2.5 py-1 rounded-lg bg-blue-500/10 border border-blue-500/30 text-blue-300">
                  Header Span: <strong>0x00 .. 0x${(header_layout.header_size - 1).toString(16).toUpperCase()}</strong> (${header_layout.header_size} Bytes)
                </div>
                <div class="px-2.5 py-1 rounded-lg bg-slate-800 border border-slate-700 text-slate-300">
                  Superblock: <strong>32 Bytes</strong>
                </div>
                <div class="px-2.5 py-1 rounded-lg bg-slate-800 border border-slate-700 text-slate-300">
                  Types: <strong>${header_layout.column_types.size} Bytes</strong>
                </div>
                <div class="px-2.5 py-1 rounded-lg bg-slate-800 border border-slate-700 text-slate-300">
                  Names: <strong>${header_layout.column_names.size} Bytes</strong>
                </div>
              </div>
            </div>

            <!-- Visual Contiguous Header Ribbon (Byte-by-Byte Timeline Strip) -->
            <div class="mt-4">
              <div class="flex items-center justify-between text-[11px] font-mono text-slate-400 mb-1.5">
                <span>Header Memory Ribbon: 0x00 .. 0x${(header_layout.header_size - 1).toString(16).toUpperCase()}</span>
                <span class="text-slate-500">Hover byte to inspect low-level metadata field</span>
              </div>

              <!-- Segmented Ribbon Bar -->
              <div class="w-full h-8 bg-slate-950 rounded-xl border border-slate-800 overflow-hidden flex shadow-inner">
                <!-- Zone 1: Fixed Superblock (32 Bytes) -->
                <div 
                  style=${{ width: `${(32 / header_layout.header_size) * 100}%` }}
                  onMouseEnter=${() => setHoveredHeaderField('superblock')}
                  onMouseLeave=${() => setHoveredHeaderField(null)}
                  class="h-full bg-blue-600/90 border-r border-blue-400/40 relative cursor-pointer transition-all hover:brightness-125 flex items-center justify-center ${
                    hoveredHeaderField === 'superblock' ? 'brightness-125 ring-2 ring-blue-400' : ''
                  }"
                  title="Fixed Superblock: 0x00 .. 0x1F (32 Bytes, 8x uint32)"
                >
                  <span class="text-[10px] font-mono font-bold text-blue-100 tracking-tight truncate px-1">
                    Superblock (0x00..0x1F, 32B)
                  </span>
                </div>

                <!-- Zone 2: Column Types (col_count Bytes) -->
                <div 
                  style=${{ width: `${(header_layout.column_types.size / header_layout.header_size) * 100}%` }}
                  onMouseEnter=${() => setHoveredHeaderField('types')}
                  onMouseLeave=${() => setHoveredHeaderField(null)}
                  class="h-full bg-emerald-600/90 border-r border-emerald-400/40 relative cursor-pointer transition-all hover:brightness-125 flex items-center justify-center ${
                    hoveredHeaderField === 'types' ? 'brightness-125 ring-2 ring-emerald-400' : ''
                  }"
                  title="Column Types: 0x20 .. 0x${(32 + header_layout.column_types.size - 1).toString(16).toUpperCase()} (${header_layout.column_types.size} Bytes, 1B per col)"
                >
                  <span class="text-[10px] font-mono font-bold text-emerald-100 tracking-tight truncate px-1">
                    Types (${header_layout.column_types.size}B)
                  </span>
                </div>

                <!-- Zone 3: Column Names String (variable Bytes) -->
                <div 
                  style=${{ width: `${(header_layout.column_names.size / header_layout.header_size) * 100}%` }}
                  onMouseEnter=${() => setHoveredHeaderField('names')}
                  onMouseLeave=${() => setHoveredHeaderField(null)}
                  class="h-full bg-purple-600/90 relative cursor-pointer transition-all hover:brightness-125 flex items-center justify-center ${
                    hoveredHeaderField === 'names' ? 'brightness-125 ring-2 ring-purple-400' : ''
                  }"
                  title="Column Names String: 0x${header_layout.column_names.offset_start.toString(16).toUpperCase()} .. 0x${header_layout.column_names.offset_end.toString(16).toUpperCase()} (${header_layout.column_names.size} Bytes)"
                >
                  <span class="text-[10px] font-mono font-bold text-purple-100 tracking-tight truncate px-1">
                    Schema Names String + \\0 (${header_layout.column_names.size}B)
                  </span>
                </div>
              </div>

              <!-- Offset Tick Markers -->
              <div class="relative w-full mt-1.5 font-mono text-[10px] text-slate-500 flex justify-between select-none px-1">
                <div>0x00 (Superblock)</div>
                <div style=${{ left: `${(32 / header_layout.header_size) * 100}%` }} class="absolute -translate-x-1/2 text-center text-emerald-400 font-semibold">
                  0x20 (Types)
                </div>
                <div style=${{ left: `${((32 + header_layout.column_types.size) / header_layout.header_size) * 100}%` }} class="absolute -translate-x-1/2 text-center text-purple-400 font-semibold">
                  0x${(32 + header_layout.column_types.size).toString(16).toUpperCase()} (Names)
                </div>
                <div class="text-blue-400 font-semibold">
                  0x${header_layout.header_size.toString(16).toUpperCase()} (Header End / DF Base)
                </div>
              </div>
            </div>

            <!-- ========================================================= -->
            <!-- 2.1 FIXED 32-BYTE SUPERBLOCK (8x 4-BYTE UINT32 FIELDS)    -->
            <!-- ========================================================= -->
            <div class="mt-5">
              <div class="flex items-center justify-between mb-2">
                <div class="flex items-center gap-2">
                  <span class="w-2.5 h-2.5 rounded bg-blue-500"></span>
                  <h3 class="text-xs font-bold text-blue-300 uppercase tracking-wider font-mono">
                    1. Fixed Superblock Header (32 Bytes, 0x00 .. 0x1F, Format: &lt;8I)
                  </h3>
                </div>
                <span class="text-[11px] font-mono text-slate-400">8 fields • 4 bytes each Little-Endian</span>
              </div>

              <!-- 8 Fields Grid -->
              <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-2.5">
                ${header_layout.fixed_superblock.fields.map(field => {
                  const isHovered = hoveredHeaderField === field.name;
                  return html`
                    <div 
                      onMouseEnter=${() => setHoveredHeaderField(field.name)}
                      onMouseLeave=${() => setHoveredHeaderField(null)}
                      class="p-2.5 rounded-xl border text-xs font-mono transition-all cursor-pointer ${
                        isHovered 
                          ? 'bg-blue-950/70 border-blue-400 ring-2 ring-blue-500/50 shadow-lg' 
                          : 'bg-slate-950/70 border-slate-800 hover:border-slate-700'
                      }"
                    >
                      <!-- Field Header Bar -->
                      <div class="flex items-center justify-between text-[11px] mb-1">
                        <span class="font-bold text-slate-200 truncate" title="${field.description}">
                          ${field.label}
                        </span>
                        <span class="px-1.5 py-0.2 rounded text-[10px] bg-blue-500/20 text-blue-300 font-semibold">
                          4B &lt;I
                        </span>
                      </div>

                      <!-- Exact Offset & Field Name -->
                      <div class="text-[10px] text-slate-400 flex items-center justify-between mb-1.5">
                        <span class="text-blue-400">${field.offset_hex}</span>
                        <span class="text-slate-500 font-sans truncate">${field.name}</span>
                      </div>

                      <!-- 4 Raw Hex Bytes -->
                      <div class="text-[11px] text-emerald-400 font-bold tracking-wider mb-1 flex items-center justify-between bg-slate-900/80 px-2 py-1 rounded">
                        <span>[ ${field.raw_hex} ]</span>
                        <span class="text-slate-200 text-xs">${field.value}</span>
                      </div>

                      <!-- Purpose Note -->
                      <div class="text-[10px] text-slate-400 font-sans truncate" title="${field.description}">
                        ${field.description}
                      </div>
                    </div>
                  `;
                })}
              </div>
            </div>

            <!-- ========================================================= -->
            <!-- 2.2 COLUMN TYPE IDENTIFIER ARRAY (1 BYTE EACH @ 0x20)     -->
            <!-- ========================================================= -->
            <div class="mt-5">
              <div class="flex items-center justify-between mb-2">
                <div class="flex items-center gap-2">
                  <span class="w-2.5 h-2.5 rounded bg-emerald-500"></span>
                  <h3 class="text-xs font-bold text-emerald-300 uppercase tracking-wider font-mono">
                    2. Column Data Type Identifiers (0x20 .. 0x${(32 + header_layout.column_types.size - 1).toString(16).toUpperCase()}, 1 Byte Each, Format: B)
                  </h3>
                </div>
                <span class="text-[11px] font-mono text-slate-400">${header_layout.column_types.size} columns defined</span>
              </div>

              <!-- Column Type Tags List -->
              <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2.5">
                ${header_layout.column_types.fields.map(ct => {
                  return html`
                    <div class="p-2.5 rounded-xl border border-slate-800 bg-slate-950/70 text-xs font-mono">
                      <div class="flex items-center justify-between text-[10px] mb-1">
                        <span class="text-slate-400">Col #${ct.col_idx}</span>
                        <span class="px-1.5 py-0.2 rounded text-[10px] bg-emerald-500/20 text-emerald-300 font-bold">
                          ${ct.type_name}
                        </span>
                      </div>

                      <div class="font-bold text-slate-200 mb-1 truncate">${ct.col_name}</div>

                      <div class="flex items-center justify-between text-[11px] bg-slate-900/80 px-2 py-1 rounded">
                        <span class="text-emerald-400 font-bold">0x${ct.hex}</span>
                        <span class="text-slate-500 text-[10px]">${ct.offset_hex} (1B)</span>
                      </div>
                    </div>
                  `;
                })}
              </div>
            </div>

            <!-- ========================================================= -->
            <!-- 2.3 COLUMN NAMES STRING (DELIMITED BY '|', NULL-TERMINATED)-->
            <!-- ========================================================= -->
            <div class="mt-5">
              <div class="flex items-center justify-between mb-2">
                <div class="flex items-center gap-2">
                  <span class="w-2.5 h-2.5 rounded bg-purple-500"></span>
                  <h3 class="text-xs font-bold text-purple-300 uppercase tracking-wider font-mono">
                    3. Column Names String (0x${header_layout.column_names.offset_start.toString(16).toUpperCase()} .. 0x${header_layout.column_names.offset_end.toString(16).toUpperCase()}, Delimited by '|', Ends with \\0)
                  </h3>
                </div>
                <span class="text-[11px] font-mono text-slate-400">${header_layout.column_names.size} Bytes total</span>
              </div>

              <!-- Byte-by-Byte String Character Ribbon -->
              <div class="p-3 rounded-xl bg-slate-950/90 border border-slate-800">
                <div class="text-[11px] font-mono text-slate-400 mb-2 flex items-center justify-between">
                  <span>Raw UTF-8 Byte Stream: <code class="text-purple-300 font-bold">"${header_layout.column_names.text}"</code></span>
                  <span class="text-slate-500">Hover individual character chip to see hex & offset</span>
                </div>

                <!-- Chips Stream -->
                <div class="flex flex-wrap gap-1 max-h-[140px] overflow-y-auto p-1 font-mono text-xs">
                  ${header_layout.column_names.bytes.map(b => html`
                    <div 
                      class="px-1.5 py-1 rounded border text-center transition-all cursor-default ${
                        b.is_null
                          ? 'bg-rose-950/60 border-rose-500 text-rose-300 font-bold'
                          : b.is_delimiter
                            ? 'bg-amber-950/60 border-amber-500 text-amber-300 font-bold'
                            : 'bg-slate-900 border-slate-800 hover:border-purple-500 text-slate-200'
                      }"
                      title="Offset: ${b.offset_hex} (Byte ${b.offset}) | Hex: 0x${b.hex} | ASCII: ${b.char}"
                    >
                      <div class="text-[11px] font-bold">${b.char}</div>
                      <div class="text-[9px] text-slate-500">${b.hex}</div>
                    </div>
                  `)}
                </div>
              </div>
            </div>

            <!-- ========================================================= -->
            <!-- 2.4 DERIVED HEAP SIZE & MEMORY BOUNDARIES EQUATION CARD    -->
            <!-- ========================================================= -->
            <div class="mt-5 p-4 rounded-xl bg-gradient-to-r from-blue-950/40 via-purple-950/30 to-emerald-950/40 border border-blue-800/40 text-xs font-mono">
              <div class="flex items-center justify-between mb-2">
                <span class="text-blue-300 font-bold uppercase tracking-wider flex items-center gap-1.5">
                  <span>📐</span> Derived Memory Boundaries & Heap Capacity
                </span>
                <span class="text-emerald-400 font-bold">Heap Base: 0x${header_layout.derived_heap.heap_base_hex} (${header_layout.derived_heap.heap_base} B)</span>
              </div>

              <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
                <div class="p-2.5 rounded-lg bg-slate-950/70 border border-slate-800/80">
                  <div class="text-slate-400 text-[10px] mb-0.5">1. Dataframe Base Offset</div>
                  <div class="text-slate-200 font-bold">Base (0x00) + Header Size (${header_layout.header_size}B)</div>
                  <div class="text-[10px] text-purple-400 mt-1">= 0x${header_layout.header_size.toString(16).toUpperCase()} (${header_layout.header_size} Bytes)</div>
                </div>

                <div class="p-2.5 rounded-lg bg-slate-950/70 border border-slate-800/80">
                  <div class="text-slate-400 text-[10px] mb-0.5">2. Heap Arena Base Offset</div>
                  <div class="text-slate-200 font-bold">Header (${header_layout.header_size}B) + Dataframe (${metadata.dataframe_size}B)</div>
                  <div class="text-[10px] text-emerald-400 mt-1">= 0x${header_layout.derived_heap.heap_base_hex} (${header_layout.derived_heap.heap_base} Bytes)</div>
                </div>

                <div class="p-2.5 rounded-lg bg-slate-950/70 border border-slate-800/80">
                  <div class="text-slate-400 text-[10px] mb-0.5">3. Derived Max Heap Size</div>
                  <div class="text-slate-200 font-bold">Total (${metadata.total_file_size}B) - (Header + DF)</div>
                  <div class="text-[10px] text-amber-400 font-bold mt-1">= ${header_layout.derived_heap.max_heap_size} Bytes available for Heap</div>
                </div>
              </div>
            </div>

          </section>
        `}

        <!-- 3. Memory Map Visualizer: Linear Byte Map & Sector View -->
        <section class="bg-slate-900/70 border border-slate-800 rounded-2xl p-5 shadow-xl">
          <div class="flex items-center justify-between mb-3">
            <div class="flex items-center gap-2">
              <h2 class="text-sm font-bold text-slate-200 uppercase tracking-wider">NOR Flash Memory Map (Linear Allocation)</h2>
              <span class="text-xs px-2 py-0.5 rounded bg-slate-800 font-mono text-slate-400">Total: ${metadata.total_file_size} Bytes</span>
            </div>
            <div class="text-xs font-mono text-slate-400 flex items-center gap-4">
              <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded bg-blue-500"></span> Header</span>
              <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded bg-purple-500"></span> Dataframe</span>
              <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded bg-emerald-500"></span> Live Heap</span>
              <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded bg-rose-500 striped-pattern"></span> Orphaned Garbage</span>
              <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded bg-slate-800"></span> Free</span>
            </div>
          </div>

          <!-- Visual Contiguous Memory Bar -->
          <div class="relative w-full h-11 bg-slate-950 rounded-xl border border-slate-800 overflow-hidden flex shadow-inner">
            <!-- Zone 1: Header -->
            <div 
              style=${{ width: `${headerPct}%` }} 
              class="h-full bg-blue-600/90 border-r border-blue-400/40 relative group cursor-pointer transition-all hover:brightness-110 flex items-center justify-center"
              title="Header: 0x00 .. 0x${metadata.header_size.toString(16).toUpperCase()} (${metadata.header_size} bytes)"
            >
              <span class="text-[10px] font-mono font-bold text-blue-100 tracking-tight truncate px-1">HDR (${metadata.header_size}B)</span>
            </div>

            <!-- Zone 2: Dataframe -->
            <div 
              style=${{ width: `${dfPct}%` }} 
              class="h-full bg-purple-600/90 border-r border-purple-400/40 relative group cursor-pointer transition-all hover:brightness-110 flex items-center justify-center"
              title="Dataframe: 0x${metadata.header_size.toString(16).toUpperCase()} .. 0x${metadata.heap_base.toString(16).toUpperCase()} (${metadata.dataframe_size} bytes)"
            >
              <span class="text-[10px] font-mono font-bold text-purple-100 tracking-tight truncate px-1">DATAFRAME (${metadata.dataframe_size}B)</span>
            </div>

            <!-- Zone 3: Live Heap Allocations -->
            ${liveHeapPct > 0 && html`
              <div 
                style=${{ width: `${liveHeapPct}%` }} 
                class="h-full bg-emerald-600/90 border-r border-emerald-400/40 relative group cursor-pointer transition-all hover:brightness-110 flex items-center justify-center glow-emerald"
                title="Live Heap: ${heap_analysis.live_bytes} bytes active"
              >
                <span class="text-[10px] font-mono font-bold text-emerald-100 tracking-tight truncate px-1">LIVE (${heap_analysis.live_bytes}B)</span>
              </div>
            `}

            <!-- Zone 4: Orphaned Dead Space (Garbage) -->
            ${deadHeapPct > 0 && html`
              <div 
                style=${{ width: `${deadHeapPct}%` }} 
                class="h-full bg-rose-600/90 striped-pattern border-r border-rose-400/50 relative group cursor-pointer transition-all hover:brightness-125 flex items-center justify-center glow-rose animate-pulse"
                title="Orphaned Garbage: ${heap_analysis.dead_bytes} bytes orphaned"
              >
                <span class="text-[10px] font-mono font-bold text-rose-100 tracking-tight truncate px-1">GARBAGE (${heap_analysis.dead_bytes}B)</span>
              </div>
            `}

            <!-- Zone 5: Free Heap Space -->
            ${freeHeapPct > 0 && html`
              <div 
                style=${{ width: `${freeHeapPct}%` }} 
                class="h-full bg-slate-900/60 relative group flex items-center justify-center"
                title="Available Free Heap: ${heap_analysis.free_bytes} bytes"
              >
                <span class="text-[10px] font-mono text-slate-500 tracking-tight truncate px-1">FREE (${heap_analysis.free_bytes}B)</span>
              </div>
            `}
          </div>

          <!-- Address Marker Scale -->
          <div class="relative w-full mt-2 font-mono text-[11px] text-slate-400 flex justify-between select-none px-1">
            <div>
              <span class="text-blue-400 font-bold">0x0000</span>
              <div class="text-[9px] text-slate-500">Superblock</div>
            </div>
            <div style=${{ left: `${headerPct}%` }} class="absolute -translate-x-1/2 text-center">
              <span class="text-purple-400 font-bold">0x${metadata.header_size.toString(16).toUpperCase()}</span>
              <div class="text-[9px] text-slate-500">DF Base</div>
            </div>
            <div style=${{ left: `${headerPct + dfPct}%` }} class="absolute -translate-x-1/2 text-center">
              <span class="text-emerald-400 font-bold">0x${metadata.heap_base.toString(16).toUpperCase()}</span>
              <div class="text-[9px] text-slate-500">Heap Base</div>
            </div>
            ${(liveHeapPct + deadHeapPct) > 0 && html`
              <div style=${{ left: `${headerPct + dfPct + liveHeapPct + deadHeapPct}%` }} class="absolute -translate-x-1/2 text-center">
                <span class="text-amber-400 font-bold">0x${(metadata.heap_base + metadata.heap_watermark).toString(16).toUpperCase()}</span>
                <div class="text-[9px] text-amber-300 font-bold">Watermark</div>
              </div>
            `}
            <div class="text-right">
              <span class="text-slate-300 font-bold">0x${metadata.total_file_size.toString(16).toUpperCase()}</span>
              <div class="text-[9px] text-slate-500">File End (${metadata.total_file_size}B)</div>
            </div>
          </div>
        </section>

        <!-- 4. FEATURE: UNCOMPRESSED JSON DATA VS PACKED BINARY REPRESENTATION INSPECTOR -->
        <section class="bg-slate-900/80 border border-purple-900/50 rounded-2xl p-5 shadow-2xl relative overflow-hidden">
          <!-- Background Glow Accent -->
          <div class="absolute -top-24 -right-24 w-80 h-80 bg-purple-600/10 rounded-full blur-3xl pointer-events-none"></div>

          <!-- Section Header & Controls -->
          <div class="flex flex-col md:flex-row items-start md:items-center justify-between gap-4 pb-4 border-b border-slate-800">
            <div>
              <div class="flex items-center gap-2">
                <span class="p-1.5 rounded-lg bg-purple-500/20 text-purple-400 font-bold">📊</span>
                <h2 class="text-sm sm:text-base font-bold text-white tracking-wide">
                  Original JSON Data vs Packed Binary Representation
                </h2>
              </div>
              <p class="text-xs text-slate-400 mt-1">
                Visualizing uncompressed text serialization overhead vs Little-Endian fixed dataframe strides and heap pointers
              </p>
            </div>

            <!-- Mode Selector & Controls -->
            <div class="flex flex-wrap items-center gap-2 font-mono text-xs">
              <!-- View Mode Toggle -->
              <div class="flex items-center bg-slate-950 p-1 rounded-xl border border-slate-800">
                <button 
                  onClick=${() => setInspectorViewMode('record')}
                  class="px-2.5 py-1 rounded-lg transition-all ${
                    inspectorViewMode === 'record'
                      ? 'bg-purple-600 text-white font-semibold shadow-md'
                      : 'text-slate-400 hover:text-slate-200'
                  }"
                >
                  Single Record
                </button>
                <button 
                  onClick=${() => setInspectorViewMode('batch')}
                  class="px-2.5 py-1 rounded-lg transition-all ${
                    inspectorViewMode === 'batch'
                      ? 'bg-purple-600 text-white font-semibold shadow-md'
                      : 'text-slate-400 hover:text-slate-200'
                  }"
                >
                  Full Batch (${metadata.entry_count} rows)
                </button>
              </div>

              <!-- JSON Formatting Toggle -->
              <div class="flex items-center bg-slate-950 p-1 rounded-xl border border-slate-800">
                <button 
                  onClick=${() => setJsonViewMode('formatted')}
                  class="px-2.5 py-1 rounded-lg transition-all ${
                    jsonViewMode === 'formatted'
                      ? 'bg-slate-800 text-purple-300 font-semibold'
                      : 'text-slate-400 hover:text-slate-200'
                  }"
                  title="Formatted JSON with indentation"
                >
                  Formatted
                </button>
                <button 
                  onClick=${() => setJsonViewMode('minified')}
                  class="px-2.5 py-1 rounded-lg transition-all ${
                    jsonViewMode === 'minified'
                      ? 'bg-slate-800 text-purple-300 font-semibold'
                      : 'text-slate-400 hover:text-slate-200'
                  }"
                  title="Minified JSON without whitespace (exact wire size)"
                >
                  Minified
                </button>
              </div>
            </div>
          </div>

          <!-- Slot Selector Pills (When in Single Record Mode) -->
          ${inspectorViewMode === 'record' && html`
            <div class="mt-4 flex flex-wrap items-center gap-2">
              <span class="text-xs text-slate-400 font-mono">Select Slot:</span>
              ${physical_rows.map(row => html`
                <button 
                  onClick=${() => setSelectedSlotIdx(row.row_idx)}
                  class="px-3 py-1.5 rounded-xl border text-xs font-mono transition-all flex items-center gap-1.5 ${
                    selectedSlotIdx === row.row_idx
                      ? 'bg-purple-600/30 border-purple-500 text-white font-bold ring-1 ring-purple-500/50'
                      : row.is_active
                        ? 'bg-slate-950/80 border-slate-800 hover:border-slate-700 text-slate-300'
                        : 'bg-slate-950/30 border-slate-900 text-slate-600 opacity-60'
                  }"
                >
                  <span>Slot #${row.row_idx}</span>
                  ${row.is_head && html`
                    <span class="w-1.5 h-1.5 rounded-full bg-purple-400 animate-pulse"></span>
                  `}
                  <span class="text-[10px] ${row.is_active ? 'text-emerald-400' : 'text-slate-500'}">
                    ${row.is_active ? `(${row.json_info ? row.json_info.dict.node_id : 'Active'})` : '(Empty)'}
                  </span>
                </button>
              `)}
            </div>
          `}

          <!-- Main Dual-Pane Comparison Grid -->
          <div class="mt-5 grid grid-cols-1 lg:grid-cols-12 gap-5">

            <!-- ========================================================= -->
            <!-- LEFT PANE: ORIGINAL JSON DATA (UNCOMPRESSED TEXT)         -->
            <!-- ========================================================= -->
            <div class="lg:col-span-6 flex flex-col bg-slate-950/90 border border-slate-800/90 rounded-xl p-4 shadow-inner">
              <div class="flex items-center justify-between pb-3 mb-3 border-b border-slate-800/80">
                <div class="flex items-center gap-2">
                  <span class="text-rose-400 font-bold font-mono">{ }</span>
                  <h3 class="text-xs sm:text-sm font-bold text-slate-200">
                    Original JSON Data (Uncompressed)
                  </h3>
                </div>

                <div class="flex items-center gap-2">
                  <!-- Size Pill -->
                  <span class="text-xs font-mono px-2 py-0.5 rounded-md bg-rose-500/20 text-rose-300 border border-rose-500/40 font-bold">
                    ${inspectorViewMode === 'record' 
                      ? (activeSelectedRow && activeSelectedRow.json_info 
                          ? `${jsonViewMode === 'formatted' ? activeSelectedRow.json_info.formatted_bytes : activeSelectedRow.json_info.minified_bytes} Bytes`
                          : '0 Bytes')
                      : `${jsonViewMode === 'formatted' ? (batch_json ? batch_json.formatted_bytes : 0) : (batch_json ? batch_json.minified_bytes : 0)} Bytes`
                    }
                  </span>

                  <!-- Copy Button -->
                  <button 
                    onClick=${() => {
                      const text = inspectorViewMode === 'record'
                        ? (activeSelectedRow && activeSelectedRow.json_info 
                            ? (jsonViewMode === 'formatted' ? activeSelectedRow.json_info.formatted : activeSelectedRow.json_info.minified)
                            : '{}')
                        : (batch_json ? (jsonViewMode === 'formatted' ? batch_json.formatted : batch_json.minified) : '[]');
                      handleCopyJson(text);
                    }}
                    class="px-2 py-1 rounded bg-slate-800 hover:bg-slate-700 text-[11px] font-mono text-slate-300 transition-all"
                  >
                    ${copiedJson ? '✓ Copied' : '📋 Copy'}
                  </button>
                </div>
              </div>

              <!-- Interactive JSON Viewer Content -->
              <div class="flex-1 min-h-[220px] max-h-[360px] overflow-auto bg-slate-900/60 p-3 rounded-lg border border-slate-800/60 font-mono text-xs">
                ${inspectorViewMode === 'record' ? html`
                  ${(!activeSelectedRow || !activeSelectedRow.is_active || !activeSelectedRow.json_info) ? html`
                    <div class="h-full flex items-center justify-center text-slate-500 text-center p-6 italic">
                      Slot #${selectedSlotIdx} is empty.<br />Click "Push Next Reading" above to populate records.
                    </div>
                  ` : jsonViewMode === 'formatted' ? html`
                    <div class="space-y-1 text-slate-300">
                      <div class="text-slate-500">{</div>
                      ${Object.entries(activeSelectedRow.json_info.dict).map(([key, val], idx, arr) => {
                        const isHovered = hoveredField === key;
                        const isLast = idx === arr.length - 1;
                        return html`
                          <div 
                            onMouseEnter=${() => setHoveredField(key)}
                            onMouseLeave=${() => setHoveredField(null)}
                            class="px-2 py-1 rounded transition-all cursor-pointer flex items-center justify-between ${
                              isHovered ? 'bg-purple-900/50 border border-purple-500 text-white glow-purple' : 'hover:bg-slate-800/60'
                            }"
                            title="Hover to highlight corresponding Little-Endian binary cell"
                          >
                            <div>
                              <span class="text-purple-300 font-semibold pl-4">"${key}"</span>: 
                              ${typeof val === 'string'
                                ? html`<span class="text-emerald-300">"${val}"</span>`
                                : html`<span class="text-amber-300">${val}</span>`
                              }${isLast ? '' : ','}
                            </div>
                            <span class="text-[10px] text-slate-500 font-sans tracking-tight">
                              ~${key.length + String(val).length + 4}B
                            </span>
                          </div>
                        `;
                      })}
                      <div class="text-slate-500">}</div>
                    </div>
                  ` : html`
                    <pre class="text-slate-300 whitespace-pre-wrap break-all leading-relaxed">${activeSelectedRow.json_info.minified}</pre>
                  `}
                ` : html`
                  <!-- Full Batch Mode -->
                  ${metadata.entry_count === 0 ? html`
                    <div class="h-full flex items-center justify-center text-slate-500 text-center p-6 italic">
                      Buffer empty. No JSON records to display.
                    </div>
                  ` : html`
                    <pre class="text-slate-300 whitespace-pre-wrap break-all leading-relaxed">${
                      jsonViewMode === 'formatted' ? (batch_json ? batch_json.formatted : '[]') : (batch_json ? batch_json.minified : '[]')
                    }</pre>
                  `}
                `}
              </div>

              <!-- Educational Overhead Metric Footer -->
              <div class="mt-3 pt-3 border-t border-slate-800/60 text-[11px] text-slate-400 space-y-1">
                <div class="flex items-center justify-between font-mono">
                  <span class="text-slate-400">Schema Key Repetition Overhead:</span>
                  <span class="text-rose-300 font-bold">~51 Bytes / record (~58%)</span>
                </div>
                <p class="text-[10px] text-slate-500">
                  Text JSON redundantly duplicates key strings (<code>"timestamp"</code>, <code>"precision_lat"</code>, etc.) on flash for each reading.
                </p>
              </div>
            </div>

            <!-- ========================================================= -->
            <!-- RIGHT PANE: PACKED BINARY REPRESENTATION (FLAT-FILE)      -->
            <!-- ========================================================= -->
            <div class="lg:col-span-6 flex flex-col bg-slate-950/90 border border-slate-800/90 rounded-xl p-4 shadow-inner">
              <div class="flex items-center justify-between pb-3 mb-3 border-b border-slate-800/80">
                <div class="flex items-center gap-2">
                  <span class="text-emerald-400 font-bold font-mono">0x</span>
                  <h3 class="text-xs sm:text-sm font-bold text-slate-200">
                    Packed Representation (ESP32 Binary Layout)
                  </h3>
                </div>

                <div class="flex items-center gap-2">
                  <!-- Packed Size Pill -->
                  <span class="text-xs font-mono px-2 py-0.5 rounded-md bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 font-bold">
                    ${inspectorViewMode === 'record'
                      ? (activeSelectedRow && activeSelectedRow.json_info
                          ? `${activeSelectedRow.json_info.total_packed_bytes} Bytes Total`
                          : '0 Bytes')
                      : `${heap_base + heap_analysis.live_bytes} Bytes Active`
                    }
                  </span>

                  <!-- Savings Pill -->
                  ${activeSelectedRow && activeSelectedRow.json_info && html`
                    <span class="text-xs font-mono px-2 py-0.5 rounded-md bg-purple-500/20 text-purple-300 border border-purple-500/40 font-bold">
                      -${activeSelectedRow.json_info.savings_pct}%
                    </span>
                  `}
                </div>
              </div>

              <!-- Packed Binary Viewer Content -->
              <div class="flex-1 min-h-[220px] max-h-[360px] overflow-auto space-y-3 bg-slate-900/60 p-3 rounded-lg border border-slate-800/60">
                ${(!activeSelectedRow || !activeSelectedRow.is_active) ? html`
                  <div class="h-full flex items-center justify-center text-slate-500 text-center p-6 italic font-mono text-xs">
                    Slot #${selectedSlotIdx} is unpopulated.<br />Push records to view packed Little-Endian cells and heap payloads.
                  </div>
                ` : html`
                  <!-- 1. Fixed Dataframe Cells (Row Stride: 20 Bytes) -->
                  <div>
                    <div class="flex items-center justify-between mb-1.5 text-[11px] font-mono">
                      <span class="text-slate-400 uppercase font-semibold">1. Fixed Dataframe Row (${activeSelectedRow.cells.length * 4}B @ 0x${activeSelectedRow.row_offset_hex})</span>
                      <span class="text-slate-500">Stride: ${activeSelectedRow.cells.length * 4} bytes</span>
                    </div>

                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
                      ${activeSelectedRow.cells.map(cell => {
                        const isHovered = hoveredField === cell.col_name;
                        return html`
                          <div 
                            onMouseEnter=${() => setHoveredField(cell.col_name)}
                            onMouseLeave=${() => setHoveredField(null)}
                            class="p-2 rounded-lg border text-xs font-mono transition-all cursor-pointer ${
                              isHovered
                                ? 'bg-purple-950/70 border-purple-500 ring-2 ring-purple-500/50 glow-purple'
                                : 'bg-slate-950/80 border-slate-800 hover:border-slate-700'
                            }"
                            title="Format: ${cell.format_tag} • Offset: ${cell.cell_offset_hex}"
                          >
                            <div class="flex items-center justify-between text-[10px] mb-1">
                              <span class="text-purple-300 font-semibold truncate">${cell.col_name}</span>
                              <span class="px-1 rounded text-[9px] ${
                                cell.type_name === 'INT32' ? 'bg-blue-500/20 text-blue-300' :
                                cell.type_name === 'FLOAT' ? 'bg-indigo-500/20 text-indigo-300' :
                                cell.type_name === 'STR' ? 'bg-emerald-500/20 text-emerald-300' :
                                'bg-amber-500/20 text-amber-300'
                              }">
                                ${cell.format_tag}
                              </span>
                            </div>

                            <!-- Raw Hex Bytes -->
                            <div class="text-[11px] text-emerald-400 font-bold tracking-wider mb-0.5">
                              [ ${cell.raw_hex} ]
                            </div>

                            <!-- Decoded Value / Pointer -->
                            <div class="text-[10px] text-slate-400 truncate flex items-center justify-between">
                              <span>${cell.pointer ? `🔗 off: ${cell.pointer.offset}B, len: ${cell.pointer.length}B` : `val: ${cell.decoded_val}`}</span>
                              <span class="text-slate-500">4B</span>
                            </div>
                          </div>
                        `;
                      })}
                    </div>
                  </div>

                  <!-- 2. Variable Heap Arena Payloads (Strings & Doubles) -->
                  <div>
                    <div class="flex items-center justify-between mb-1.5 text-[11px] font-mono">
                      <span class="text-slate-400 uppercase font-semibold">2. Dynamic Heap Payloads (${activeSelectedRow.json_info ? activeSelectedRow.json_info.heap_bytes : 0}B)</span>
                      <span class="text-slate-500">Base: 0x${metadata.heap_base.toString(16).toUpperCase()}</span>
                    </div>

                    <div class="space-y-1.5">
                      ${activeSelectedRow.cells.filter(c => c.pointer).map(cell => {
                        const isHovered = hoveredField === cell.col_name;
                        return html`
                          <div 
                            onMouseEnter=${() => setHoveredField(cell.col_name)}
                            onMouseLeave=${() => setHoveredField(null)}
                            class="p-2 rounded-lg border text-xs font-mono transition-all cursor-pointer ${
                              isHovered
                                ? 'bg-emerald-950/60 border-emerald-400 ring-2 ring-emerald-500/50 glow-emerald'
                                : 'bg-slate-950/80 border-slate-800/90'
                            }"
                          >
                            <div class="flex items-center justify-between text-[10px] mb-0.5">
                              <span class="text-emerald-300 font-bold">${cell.col_name} Heap Payload</span>
                              <span class="text-slate-400">0x${cell.pointer.heap_abs_addr.toString(16).toUpperCase()} (+${cell.pointer.offset}B)</span>
                            </div>

                            <div class="text-[11px] text-slate-200 tracking-wider font-mono">
                              Bytes: <span class="text-emerald-400 font-bold">${cell.pointer.heap_hex}</span>
                            </div>

                            <div class="text-[10px] text-slate-400 mt-0.5 flex items-center justify-between">
                              <span class="italic text-slate-300">Resolved: "${cell.decoded_val}"</span>
                              <span class="font-bold text-emerald-300">${cell.pointer.length} Bytes</span>
                            </div>
                          </div>
                        `;
                      })}
                    </div>
                  </div>
                `}
              </div>

              <!-- Educational Packed Footprint Footer -->
              <div class="mt-3 pt-3 border-t border-slate-800/60 text-[11px] text-slate-400 space-y-1">
                <div class="flex items-center justify-between font-mono">
                  <span class="text-slate-400">Packed Footprint Breakdown:</span>
                  <span class="text-emerald-300 font-bold">
                    ${activeSelectedRow && activeSelectedRow.json_info 
                      ? `${activeSelectedRow.json_info.df_bytes}B Dataframe + ${activeSelectedRow.json_info.heap_bytes}B Heap = ${activeSelectedRow.json_info.total_packed_bytes}B`
                      : '0 Bytes'
                    }
                  </span>
                </div>
                <p class="text-[10px] text-slate-500">
                  Schema names are stored once in the header. Field lookups execute in $\\mathcal{O}(1)$ time via pointer arithmetic without parsing.
                </p>
              </div>
            </div>

          </div>
        </section>

        <!-- 5. Main Two-Column Layout: Circular Ring Buffer & Heap Arena -->
        <div class="grid grid-cols-1 lg:grid-cols-12 gap-6">

          <!-- Left Column: Circular Ring Buffer Dataframe Table (7 cols) -->
          <div class="lg:col-span-7 space-y-4">
            <div class="bg-slate-900/70 border border-slate-800 rounded-2xl p-5 shadow-xl">
              
              <div class="flex items-center justify-between mb-4">
                <div>
                  <h2 class="text-sm font-bold text-slate-200 uppercase tracking-wider flex items-center gap-2">
                    <span>🔄</span> Circular Ring Buffer (Fixed 4B Cells)
                  </h2>
                  <p class="text-xs text-slate-400">Stride: ${metadata.col_count * 4} bytes/row • Formula: <code class="font-mono text-purple-300">0x${metadata.header_size.toString(16).toUpperCase()} + (row * ${metadata.col_count * 4})</code></p>
                </div>

                <div class="text-xs font-mono px-3 py-1 rounded-lg bg-purple-500/10 border border-purple-500/20 text-purple-300">
                  Write Head: Slot #${metadata.head_index}
                </div>
              </div>

              <!-- Ring Slots Container -->
              <div class="space-y-3">
                ${physical_rows.map(row => html`
                  <div 
                    onClick=${() => setSelectedSlotIdx(row.row_idx)}
                    class="rounded-xl border transition-all cursor-pointer ${
                      selectedSlotIdx === row.row_idx
                        ? 'ring-2 ring-purple-500/80 border-purple-400 bg-purple-950/20'
                        : row.is_head
                          ? 'border-purple-500/70 bg-purple-950/10'
                          : row.is_active
                            ? 'border-slate-700/80 bg-slate-950/50'
                            : 'border-slate-800/60 bg-slate-950/20 opacity-60'
                    }"
                  >
                    <!-- Slot Header Bar -->
                    <div class="px-3.5 py-2 border-b border-slate-800/80 flex items-center justify-between text-xs font-mono">
                      <div class="flex items-center gap-2">
                        <span class="font-bold ${row.is_active ? 'text-slate-200' : 'text-slate-500'}">
                          Slot #${row.row_idx}
                        </span>
                        <span class="text-slate-500">@ ${row.row_offset_hex}</span>
                        ${row.is_head && html`
                          <span class="px-2 py-0.5 rounded-full bg-purple-500 text-white font-sans text-[10px] font-bold tracking-wide uppercase animate-pulse">
                            Write Head (Next Target)
                          </span>
                        `}
                      </div>
                      <div class="flex items-center gap-2">
                        ${selectedSlotIdx === row.row_idx && html`
                          <span class="text-[10px] px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-300 border border-purple-500/40">Inspecting</span>
                        `}
                        <span class="text-[11px] ${row.is_active ? 'text-emerald-400' : 'text-slate-500'}">
                          ${row.is_active ? '● POPULATED' : '○ EMPTY'}
                        </span>
                      </div>
                    </div>

                    <!-- 4-Byte Cells Grid -->
                    <div class="p-3 grid grid-cols-2 sm:grid-cols-5 gap-2">
                      ${row.cells.map(cell => html`
                        <div class="p-2 rounded-lg bg-slate-900/90 border border-slate-800 text-[11px] font-mono flex flex-col justify-between ${
                          hoveredField === cell.col_name ? 'border-purple-500 ring-1 ring-purple-500/50 bg-purple-950/40' : ''
                        }">
                          <div class="flex items-center justify-between text-[10px] mb-1">
                            <span class="text-slate-400 font-sans font-medium truncate">${cell.col_name}</span>
                            <span class="px-1 rounded text-[9px] ${
                              cell.type_name === 'INT32' ? 'bg-blue-500/20 text-blue-300' :
                              cell.type_name === 'FLOAT' ? 'bg-indigo-500/20 text-indigo-300' :
                              cell.type_name === 'STR' ? 'bg-emerald-500/20 text-emerald-300' :
                              'bg-amber-500/20 text-amber-300'
                            }">${cell.type_name}</span>
                          </div>

                          <!-- Raw Hex Bytes -->
                          <div class="text-[10px] text-slate-500 mb-1 tracking-wider truncate" title="Raw 4 bytes in Little-Endian">
                            ${cell.raw_hex}
                          </div>

                          <!-- Resolved Value or Pointer Indicator -->
                          <div class="mt-auto">
                            ${cell.pointer ? html`
                              <div 
                                onMouseEnter=${() => setHighlightOffset(cell.pointer.offset)}
                                onMouseLeave=${() => setHighlightOffset(null)}
                                class="text-[10px] px-1.5 py-0.5 rounded bg-emerald-950/60 border border-emerald-600/40 text-emerald-300 font-bold truncate cursor-pointer hover:bg-emerald-800/40"
                                title="Heap Pointer: offset=${cell.pointer.offset}, len=${cell.pointer.length} -> Value: ${cell.decoded_val}"
                              >
                                🔗 off: ${cell.pointer.offset}B ("${cell.decoded_val}")
                              </div>
                            ` : html`
                              <div class="text-slate-200 font-semibold truncate">
                                ${cell.decoded_val !== null ? cell.decoded_val : '—'}
                              </div>
                            `}
                          </div>
                        </div>
                      `)}
                    </div>
                  </div>
                `)}
              </div>
            </div>

            <!-- Chronological Unwrapping Banner -->
            <div class="bg-slate-900/70 border border-slate-800 rounded-2xl p-4 text-xs font-mono shadow-xl">
              <div class="flex items-center justify-between mb-2">
                <span class="text-slate-300 font-bold uppercase">Chronological Unwrapping (Oldest -> Newest)</span>
                <span class="text-slate-400 font-sans">Formula: <code class="text-purple-300">(head_index + i) % max_entries</code></span>
              </div>
              <div class="flex flex-wrap gap-2">
                ${chronological_rows.length === 0 ? html`
                  <span class="text-slate-500">Buffer empty. Push readings to view.</span>
                ` : chronological_rows.map((r, i) => html`
                  <div class="px-2.5 py-1.5 rounded-lg bg-slate-950 border border-slate-800 flex items-center gap-2">
                    <span class="text-purple-400 font-bold">#${i + 1}</span>
                    <span class="text-slate-300">${r.node_id}</span>
                    <span class="text-slate-400">(${r.temp}°C, ${r.humidity}%)</span>
                  </div>
                `)}
              </div>
            </div>
          </div>

          <!-- Right Column: Heap Arena & Garbage Breakdown (5 cols) -->
          <div class="lg:col-span-5 space-y-4">
            <div class="bg-slate-900/70 border border-slate-800 rounded-2xl p-5 shadow-xl">
              
              <div class="flex items-center justify-between mb-3">
                <div>
                  <h2 class="text-sm font-bold text-slate-200 uppercase tracking-wider flex items-center gap-2">
                    <span>📦</span> Heap Arena Allocation Segments
                  </h2>
                  <p class="text-xs text-slate-400">Bump Base: <code class="font-mono text-emerald-300">0x${metadata.heap_base.toString(16).toUpperCase()}</code> • Watermark: <code class="font-mono text-amber-300">${metadata.heap_watermark} / ${metadata.max_heap_size} B</code></p>
                </div>

                ${heap_analysis.dead_bytes > 0 && html`
                  <span class="text-xs px-2.5 py-1 rounded-lg bg-rose-500/20 text-rose-300 border border-rose-500/40 font-mono font-bold animate-pulse">
                    ⚠️ ${heap_analysis.dead_bytes}B LEAKED
                  </span>
                `}
              </div>

              <!-- Heap Segments List -->
              <div class="space-y-2.5 max-h-[420px] overflow-y-auto pr-1">
                ${heap_analysis.segments.map((seg) => html`
                  <div class="p-3 rounded-xl border text-xs font-mono transition-all ${
                    seg.status === 'DEAD'
                      ? 'bg-rose-950/30 border-rose-700/60 text-rose-200 striped-pattern'
                      : seg.status === 'LIVE'
                        ? (highlightOffset === seg.offset ? 'bg-emerald-900/40 border-emerald-400 ring-2 ring-emerald-500/60' : 'bg-emerald-950/20 border-emerald-800/60 text-emerald-200')
                        : 'bg-slate-950/40 border-slate-800 text-slate-400'
                  }">
                    <div class="flex items-center justify-between mb-1">
                      <div class="flex items-center gap-2">
                        <span class="px-1.5 py-0.5 rounded text-[10px] font-bold ${
                          seg.status === 'DEAD' ? 'bg-rose-500 text-white' :
                          seg.status === 'LIVE' ? 'bg-emerald-500 text-white' : 'bg-slate-800 text-slate-400'
                        }">
                          ${seg.status}
                        </span>
                        <span class="font-semibold text-slate-200">${seg.label}</span>
                      </div>
                      <span class="font-bold ${seg.status === 'DEAD' ? 'text-rose-400' : 'text-slate-300'}">
                        ${seg.length} Bytes
                      </span>
                    </div>

                    <div class="text-[11px] text-slate-400 flex items-center justify-between">
                      <span>Offset: <strong>+${seg.offset}B</strong> (0x${seg.abs_addr.toString(16).toUpperCase()})</span>
                      <span class="truncate max-w-[150px] font-sans italic text-slate-300">"${seg.preview}"</span>
                    </div>
                  </div>
                `)}
              </div>

              <!-- Compactor Explanation Tooltip -->
              <div class="mt-4 p-3 rounded-xl bg-slate-950/60 border border-slate-800 text-[11px] text-slate-400 space-y-1">
                <div class="font-bold text-slate-300 flex items-center gap-1.5">
                  <span>💡</span> Why Mark-Compact Matters on Flash:
                </div>
                <p>
                  When the circular ring buffer wraps around, overwritten slots abandon their strings on the heap arena.
                  <strong>Mark-Compact</strong> scans active rows (Mark), sorts live offsets (Sort), slides them back to 0 (Slide), and zeroes dead space—reclaiming lost memory without sector fragmentation!
                </p>
              </div>
            </div>

            <!-- Savings & Flash Wear Efficiency Card -->
            <div class="bg-gradient-to-br from-purple-950/30 via-slate-900 to-indigo-950/30 border border-purple-900/40 rounded-2xl p-5 shadow-xl">
              <div class="flex items-center justify-between mb-3">
                <h3 class="text-xs font-bold uppercase tracking-wider text-purple-300 flex items-center gap-2">
                  <span>⚡</span> Flash Storage Efficiency
                </h3>
                <span class="text-xs font-mono px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 font-bold">
                  -${savings.savings_percent_formatted}% Space Saved
                </span>
              </div>

              <div class="grid grid-cols-2 gap-3 text-xs font-mono">
                <div class="p-2.5 rounded-xl bg-slate-950/70 border border-slate-800">
                  <div class="text-slate-400 text-[11px] mb-0.5">Plain JSON Size</div>
                  <div class="text-sm font-bold text-rose-300">${savings.json_bytes_formatted} Bytes</div>
                  <div class="text-[10px] text-slate-500 mt-1">~${Math.round(savings.json_bytes_formatted / Math.max(1, metadata.entry_count))} B / record</div>
                </div>

                <div class="p-2.5 rounded-xl bg-slate-950/70 border border-slate-800">
                  <div class="text-slate-400 text-[11px] mb-0.5">Binary Flat-File</div>
                  <div class="text-sm font-bold text-emerald-300">${savings.active_binary_bytes} Bytes</div>
                  <div class="text-[10px] text-slate-500 mt-1">~${savings.avg_bytes_per_record} B / record</div>
                </div>
              </div>

              <div class="mt-3 text-[11px] text-slate-400 font-sans">
                On an ESP32 SPI NOR Flash, saving <strong class="text-slate-200">~${savings.savings_percent_formatted}%</strong> storage reduces write wear cycles by over <strong class="text-slate-200">3x</strong>, multiplying offline logger battery life and storage longevity.
              </div>
            </div>

          </div>
        </div>

        <!-- 6. Low-Level Hex Dump Section -->
        <section class="bg-slate-900/70 border border-slate-800 rounded-2xl p-5 shadow-xl">
          <div class="flex items-center justify-between mb-3">
            <div class="flex items-center gap-2">
              <h3 class="text-xs font-bold uppercase tracking-wider text-slate-300 flex items-center gap-1.5">
                <span>🔬</span> Live Flash Page Hex Dump (Superblock + Dataframe First 128B)
              </h3>
              <span class="text-xs px-2 py-0.5 rounded bg-slate-800 text-slate-400 font-mono">Offset: 0x0000 .. 0x0080</span>
            </div>
            <button 
              onClick=${fetchState}
              disabled=${isOperating}
              class="text-xs px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 disabled:opacity-50 text-slate-300 font-mono flex items-center gap-1"
            >
              <span>Refresh Dump</span>
            </button>
          </div>

          <pre class="p-4 rounded-xl bg-slate-950 border border-slate-800/80 font-mono text-[11px] sm:text-xs text-purple-300/90 overflow-x-auto leading-relaxed shadow-inner">
${hex_preview}
          </pre>
        </section>

      </main>
    </div>
  `;
}

// Mount Preact app into root container
render(html`<${App} />`, document.getElementById('app'));
