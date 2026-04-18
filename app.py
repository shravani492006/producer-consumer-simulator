from flask import Flask, render_template_string, request, redirect, Response, jsonify
from flask_socketio import SocketIO, emit
import threading
import time
import random
import json
import csv
import io
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'ossimulator2025'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ===============================
# CONFIG FILE
# ===============================
CONFIG_FILE = "sim_config.json"

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {"producer_delay": 0.5, "consumer_delay": 0.5, "num_producers": 3, "num_consumers": 3}

def save_config():
    with open(CONFIG_FILE, "w") as f:
        json.dump({
            "producer_delay": producer_delay,
            "consumer_delay": consumer_delay,
            "num_producers": num_producers,
            "num_consumers": num_consumers
        }, f)

cfg = load_config()

# ===============================
# GLOBAL Variables 
# ===============================
running = False
manual_mode = False

producer_delay   = cfg.get("producer_delay", 0.5)
consumer_delay   = cfg.get("consumer_delay", 0.5)
num_producers    = cfg.get("num_producers", 3)
num_consumers    = cfg.get("num_consumers", 3)

buffers      = []
log_data     = []           # list of dicts
stats_history = []          # [{ts, prod_rate, cons_rate}]

produce_count = 0
consume_count = 0
start_time    = time.time()

deadlock_detected  = False
deadlock_info      = {}     # holds root cause details
starvation_alerts  = {}     # consumer_id -> last_consume_time
toast_queue        = []     # list of {type, msg}

# Thread state tracking
producer_states  = {}   # pid -> "RUNNING" | "WAITING" | "SLEEPING"
consumer_states  = {}   # cid -> "RUNNING" | "WAITING" | "SLEEPING"
paused_producers = set()
paused_consumers = set()

# Per-thread speed overrides
producer_speed_override = {}  # pid -> delay
consumer_speed_override = {}  # cid -> delay

# Round-robin counters for AUTO mode (ensures all buffers get utilized)
producer_rr_counter = {}   # pid -> last buffer index used
consumer_rr_counter = {}   # cid -> last buffer index used

lock_global = threading.Lock()

# ===============================
# BUFFER CLASS
# ===============================
class Buffer:
    def __init__(self, capacity, name):
        self.buffer   = []
        self.capacity = capacity
        self.name     = name
        self.lock      = threading.Lock()
        self.condition = threading.Condition(self.lock)
        self.total_in  = 0
        self.total_out = 0
        self.history   = []  # fill level snapshots
        self.wait_count = 0  # how many threads currently waiting on this buffer

    def fill_pct(self):
        return round(len(self.buffer) / self.capacity * 100)

for i in range(3):
    buffers.append(Buffer(5, f"Buffer {i+1}"))

# ===============================
# HELPERS
# ===============================
def push_toast(kind, msg):
    toast_queue.append({"type": kind, "msg": msg, "ts": time.time()})
    while len(toast_queue) > 10:
        toast_queue.pop(0)

def check_starvation():
    now = time.time()
    for cid, last in starvation_alerts.items():
        if now - last > 5.0:
            push_toast("warn", f"Consumer C{cid} starving — no items consumed for 5s")
            starvation_alerts[cid] = now

def check_deadlock():
    """
    Enhanced deadlock detection with root cause analysis.
    Deadlock scenarios:
    1. All producers WAITING (buffers full) + All consumers WAITING (buffers empty) — impossible in theory but can occur with buffer count mismatch
    2. All producers waiting on full buffers + all consumers waiting on empty buffers (different subsets)
    3. All threads paused externally
    """
    global deadlock_detected, deadlock_info
    if not running:
        return

    active_pids = [pid for pid in range(num_producers) if pid not in paused_producers]
    active_cids = [cid for cid in range(num_consumers) if cid not in paused_consumers]

    if not active_pids or not active_cids:
        return

    all_waiting_prod = all(producer_states.get(pid, "SLEEPING") == "WAITING" for pid in active_pids)
    all_waiting_cons = all(consumer_states.get(cid, "SLEEPING") == "WAITING" for cid in active_cids)

    # Detailed root cause analysis
    full_buffers  = [b for b in buffers if len(b.buffer) == b.capacity]
    empty_buffers = [b for b in buffers if len(b.buffer) == 0]
    all_full      = len(full_buffers) == len(buffers)
    all_empty     = len(empty_buffers) == len(buffers)

    if all_waiting_prod and all_waiting_cons:
        if not deadlock_detected:
            deadlock_detected = True
            # Build root cause explanation
            cause = "UNKNOWN"
            explanation = ""
            recommendation = ""

            if all_full and all_empty:
                
                cause = "BUFFER STATE CORRUPTION"
                explanation = "All buffers are simultaneously reported as full and empty — state inconsistency."
                recommendation = "Reset the simulation."
            elif all_full:
                cause = "ALL BUFFERS FULL"
                explanation = (
                    f"All {len(buffers)} buffer(s) are at full capacity. "
                    f"Producers cannot insert items and are blocked on condition.wait(). "
                    f"Meanwhile, consumers are also waiting — likely targeting empty sub-regions "
                    f"or all consumer threads are paused/slow."
                )
                recommendation = "Add more consumers, increase consumer speed, remove a buffer, or inject a manual consume."
            elif all_empty:
                cause = "ALL BUFFERS EMPTY"
                explanation = (
                    f"All {len(buffers)} buffer(s) are empty. "
                    f"Consumers cannot retrieve items and are blocked on condition.wait(). "
                    f"Producers are also waiting — likely all producer threads are paused or too slow."
                )
                recommendation = "Add more producers, increase producer speed, or manually inject items."
            else:
                # Mixed state — circular wait condition
                filled = [b.name for b in buffers if len(b.buffer) > 0]
                empty_names = [b.name for b in empty_buffers]
                cause = "CIRCULAR WAIT / THREAD CONTENTION"
                explanation = (
                    f"Producers are all waiting despite {len(filled)} non-full buffer(s) ({', '.join(filled)}). "
                    f"Consumers are all waiting despite non-empty buffers. "
                    f"This indicates threads are contending on the same buffer locks simultaneously — "
                    f"a circular dependency or starvation loop."
                )
                recommendation = "Increase number of threads, add buffers, or adjust thread delays to break contention."

            deadlock_info = {
                "cause":          cause,
                "explanation":    explanation,
                "recommendation": recommendation,
                "full_buffers":   [b.name for b in full_buffers],
                "empty_buffers":  [b.name for b in empty_buffers],
                "waiting_producers": [f"P{p}" for p in active_pids if producer_states.get(p) == "WAITING"],
                "waiting_consumers": [f"C{c}" for c in active_cids if consumer_states.get(c) == "WAITING"],
                "detected_at":    time.strftime("%H:%M:%S"),
            }
            push_toast("error", f"⚠ DEADLOCK: {cause}")
    else:
        deadlock_detected = False
        deadlock_info = {}

def get_balanced_buffer_for_producer(pid):
    """
    In AUTO mode: pick the buffer with the MOST empty space (least fill %).
    This ensures all buffers get utilized evenly.
    Falls back to round-robin if all are equal.
    """
    if not buffers:
        return None
    # Sort by fill ascending (most empty first), break ties by round-robin
    rr = producer_rr_counter.get(pid, -1)
    available = [(i, b) for i, b in enumerate(buffers) if len(b.buffer) < b.capacity]
    if not available:
        # All full — pick least full for waiting
        available = list(enumerate(buffers))
    # Prefer least-filled buffer; among equal, advance round-robin
    available.sort(key=lambda x: (x[1].fill_pct(), (x[0] <= rr)))
    chosen_idx, chosen_buf = available[0]
    producer_rr_counter[pid] = chosen_idx
    return chosen_buf

def get_balanced_buffer_for_consumer(cid):
    """
    In AUTO mode: pick the buffer with the MOST items (highest fill %).
    This ensures all filled buffers get drained and utilization stays balanced.
    """
    if not buffers:
        return None
    rr = consumer_rr_counter.get(cid, -1)
    available = [(i, b) for i, b in enumerate(buffers) if len(b.buffer) > 0]
    if not available:
        # All empty — pick any (will wait)
        available = list(enumerate(buffers))
    available.sort(key=lambda x: (-x[1].fill_pct(), (x[0] <= rr)))
    chosen_idx, chosen_buf = available[0]
    consumer_rr_counter[cid] = chosen_idx
    return chosen_buf

def emit_state():
    buf_data = []
    for b in buffers:
        pct = b.fill_pct()
        buf_data.append({
            "name":     b.name,
            "items":    list(b.buffer),
            "capacity": b.capacity,
            "fill_pct": pct,
            "fill":     len(b.buffer),
            "total_in": b.total_in,
            "total_out":b.total_out,
        })

    elapsed   = time.time() - start_time
    prod_rate = round(produce_count / elapsed, 2) if elapsed else 0
    cons_rate = round(consume_count / elapsed, 2) if elapsed else 0

    stats_history.append({"ts": round(elapsed, 1), "prod": prod_rate, "cons": cons_rate})
    if len(stats_history) > 60:
        stats_history.pop(0)

    # Buffer utilization: avg fill % across all buffers
    avg_util = round(sum(b.fill_pct() for b in buffers) / max(len(buffers), 1), 1)

    payload = {
        "running":           running,
        "manual_mode":       manual_mode,
        "deadlock":          deadlock_detected,
        "deadlock_info":     deadlock_info,
        "prod_rate":         prod_rate,
        "cons_rate":         cons_rate,
        "produce_count":     produce_count,
        "consume_count":     consume_count,
        "buffers":           buf_data,
        "logs":              log_data[-40:],
        "stats_history":     stats_history[-60:],
        "producer_states":   {str(k): v for k, v in producer_states.items()},
        "consumer_states":   {str(k): v for k, v in consumer_states.items()},
        "paused_producers":  list(paused_producers),
        "paused_consumers":  list(paused_consumers),
        "toasts":            list(toast_queue),
        "num_producers":     num_producers,
        "num_consumers":     num_consumers,
        "producer_delay":    producer_delay,
        "consumer_delay":    consumer_delay,
        "avg_buffer_util":   avg_util,
    }
    socketio.emit("state", payload)

# ===============================
# PRODUCER THREAD
# ===============================
def producer(pid):
    global produce_count
    producer_states[pid] = "SLEEPING"
    while True:
        if not running or pid in paused_producers:
            producer_states[pid] = "SLEEPING"
            time.sleep(0.3)
            continue

        item = random.randint(1, 100)

        # AUTO mode: smart buffer selection (most empty space)
        # MANUAL mode: random
        if manual_mode:
            buffer = random.choice(buffers)
        else:
            buffer = get_balanced_buffer_for_producer(pid)
            if buffer is None:
                time.sleep(0.1)
                continue

        producer_states[pid] = "WAITING"
        check_deadlock()

        with buffer.condition:
            while len(buffer.buffer) == buffer.capacity:
                producer_states[pid] = "WAITING"
                buffer.condition.wait(timeout=0.5)
                if not running:
                    break
                # Re-evaluate best buffer in auto mode after waking
                if not manual_mode and pid not in paused_producers:
                    # Check if another buffer has space for better optimization 
                    better = get_balanced_buffer_for_producer(pid)
                    if better is not None and better is not buffer and len(better.buffer) < better.capacity:
                        break  # exit inner loop to re-select buffer

            if not running:
                continue

            if len(buffer.buffer) < buffer.capacity:
                producer_states[pid] = "RUNNING"
                ts  = time.strftime("%H:%M:%S")
                buffer.buffer.append(item)
                buffer.total_in += 1
                produce_count   += 1
                log_data.append({
                    "kind":   "produce",
                    "ts":     ts,
                    "thread": f"P{pid}",
                    "item":   item,
                    "buffer": buffer.name,
                    "msg":    f"[{ts}] P{pid} → {item} into {buffer.name}"
                })
                buffer.condition.notify_all()

        delay = producer_speed_override.get(pid, producer_delay)
        producer_states[pid] = "SLEEPING"
        time.sleep(delay if manual_mode else random.uniform(0.2, delay + 0.5))

# ===============================
# CONSUMER THREAD
# ===============================
def consumer(cid):
    global consume_count
    consumer_states[cid] = "SLEEPING"
    starvation_alerts[cid] = time.time()
    while True:
        if not running or cid in paused_consumers:
            consumer_states[cid] = "SLEEPING"
            time.sleep(0.3)
            continue

        
        # MANUAL mode: random
        if manual_mode:
            buffer = random.choice(buffers)
        else:
        # AUTO mode: smart buffer selection (consume From most item buffer)
            buffer = get_balanced_buffer_for_consumer(cid)
            if buffer is None:
                time.sleep(0.1)
                continue

        consumer_states[cid] = "WAITING"
        check_deadlock()
        check_starvation()

        with buffer.condition:
            while len(buffer.buffer) == 0:
                consumer_states[cid] = "WAITING"
                buffer.condition.wait(timeout=0.5)
                if not running:
                    break
                # Re-check for better buffer in auto mode after waking
                if not manual_mode and cid not in paused_consumers:
                    better = get_balanced_buffer_for_consumer(cid)
                    if better is not None and better is not buffer and len(better.buffer) > 0:
                        break

            if not running:
                continue

            if len(buffer.buffer) > 0:
                consumer_states[cid] = "RUNNING"
                item = buffer.buffer.pop(0)
                buffer.total_out     += 1
                consume_count        += 1
                starvation_alerts[cid] = time.time()

                ts = time.strftime("%H:%M:%S")
                log_data.append({
                    "kind":   "consume",
                    "ts":     ts,
                    "thread": f"C{cid}",
                    "item":   item,
                    "buffer": buffer.name,
                    "msg":    f"[{ts}] C{cid} ← {item} from {buffer.name}"
                })
                buffer.condition.notify_all()

        delay = consumer_speed_override.get(cid, consumer_delay)
        consumer_states[cid] = "SLEEPING"
        time.sleep(delay if manual_mode else random.uniform(0.2, delay + 0.5))

# ===============================
# THREAD MANAGER
# ===============================
active_producer_threads = {}
active_consumer_threads = {}

def ensure_threads():
    for i in range(num_producers):
        if i not in active_producer_threads or not active_producer_threads[i].is_alive():
            t = threading.Thread(target=producer, args=(i,), daemon=True)
            t.start()
            active_producer_threads[i] = t
    for i in range(num_consumers):
        if i not in active_consumer_threads or not active_consumer_threads[i].is_alive():
            t = threading.Thread(target=consumer, args=(i,), daemon=True)
            t.start()
            active_consumer_threads[i] = t

ensure_threads()

# ===============================
# STATE BROADCAST LOOP
# ===============================
def broadcast_loop():
    while True:
        try:
            emit_state()
        except Exception:
            pass
        time.sleep(0.8)

threading.Thread(target=broadcast_loop, daemon=True).start()

# ===============================
# ROUTES
# ===============================
@app.route('/')
def home():
    return render_template_string(HTML)

@app.route('/api/state')
def api_state():
    elapsed   = time.time() - start_time
    prod_rate = round(produce_count / elapsed, 2) if elapsed else 0
    cons_rate = round(consume_count / elapsed, 2) if elapsed else 0
    return jsonify({
        "running": running,
        "manual_mode": manual_mode,
        "produce_count": produce_count,
        "consume_count": consume_count,
        "prod_rate": prod_rate,
        "cons_rate": cons_rate,
        "deadlock": deadlock_detected,
        "deadlock_info": deadlock_info,
        "buffers": [{"name": b.name, "fill": len(b.buffer), "capacity": b.capacity} for b in buffers],
    })

@app.route('/toggle')
def toggle():
    global running
    running = not running
    push_toast("info", f"Simulation {'started' if running else 'stopped'}")
    return redirect('/')

@app.route('/set_mode')
def set_mode():
    global manual_mode
    manual_mode = request.args.get('mode') == 'manual'
    push_toast("info", f"Mode set to {'MANUAL' if manual_mode else 'AUTO'}")
    save_config()
    return redirect('/')

@app.route('/set_speed')
def set_speed():
    global producer_delay, consumer_delay
    try:
        producer_delay = float(request.args.get('p', producer_delay))
        consumer_delay = float(request.args.get('c', consumer_delay))
        save_config()
        push_toast("info", f"Delays updated — P:{producer_delay}s  C:{consumer_delay}s")
    except:
        push_toast("error", "Invalid delay values")
    return redirect('/')

@app.route('/add_buffer')
def add_buffer():
    cap  = int(request.args.get('cap', 5))
    name = f"Buffer {len(buffers)+1}"
    buffers.append(Buffer(cap, name))
    push_toast("info", f"Added {name} (cap {cap})")
    return redirect('/')

@app.route('/remove_buffer')
def remove_buffer():
    if len(buffers) > 1:
        b = buffers.pop()
        push_toast("warn", f"Removed {b.name}")
    return redirect('/')

@app.route('/resize_buffer')
def resize_buffer():
    idx = int(request.args.get('idx', 0))
    cap = int(request.args.get('cap', 5))
    if 0 <= idx < len(buffers):
        buffers[idx].capacity = cap
        push_toast("info", f"{buffers[idx].name} resized to {cap}")
    return redirect('/')

@app.route('/inject_item')
def inject_item():
    idx  = int(request.args.get('idx', 0))
    val  = int(request.args.get('val', random.randint(1,100)))
    if 0 <= idx < len(buffers):
        b = buffers[idx]
        with b.condition:
            if len(b.buffer) < b.capacity:
                b.buffer.append(val)
                b.total_in += 1
                push_toast("info", f"Injected {val} into {b.name}")
                b.condition.notify_all()
            else:
                push_toast("warn", f"{b.name} is full — injection failed")
    return redirect('/')

@app.route('/add_producer')
def add_producer():
    global num_producers
    num_producers += 1
    save_config()
    ensure_threads()
    push_toast("info", f"Producer P{num_producers-1} added")
    return redirect('/')

@app.route('/remove_producer')
def remove_producer():
    global num_producers
    if num_producers > 1:
        pid = num_producers - 1
        paused_producers.add(pid)
        num_producers -= 1
        save_config()
        push_toast("warn", f"Producer P{pid} removed")
    return redirect('/')

@app.route('/add_consumer')
def add_consumer():
    global num_consumers
    num_consumers += 1
    save_config()
    ensure_threads()
    push_toast("info", f"Consumer C{num_consumers-1} added")
    return redirect('/')

@app.route('/remove_consumer')
def remove_consumer():
    global num_consumers
    if num_consumers > 1:
        cid = num_consumers - 1
        paused_consumers.add(cid)
        num_consumers -= 1
        save_config()
        push_toast("warn", f"Consumer C{cid} removed")
    return redirect('/')

@app.route('/pause_thread')
def pause_thread():
    kind = request.args.get('kind')
    tid  = int(request.args.get('id', 0))
    if kind == 'producer':
        if tid in paused_producers:
            paused_producers.discard(tid)
            push_toast("info", f"P{tid} resumed")
        else:
            paused_producers.add(tid)
            push_toast("warn", f"P{tid} paused")
    elif kind == 'consumer':
        if tid in paused_consumers:
            paused_consumers.discard(tid)
            push_toast("info", f"C{tid} resumed")
        else:
            paused_consumers.add(tid)
            push_toast("warn", f"C{tid} paused")
    return ('', 204)

@app.route('/set_thread_speed')
def set_thread_speed():
    kind  = request.args.get('kind')
    tid   = int(request.args.get('id', 0))
    delay = float(request.args.get('delay', 0.5))
    if kind == 'producer':
        producer_speed_override[tid] = delay
        push_toast("info", f"P{tid} delay set to {delay}s")
    elif kind == 'consumer':
        consumer_speed_override[tid] = delay
        push_toast("info", f"C{tid} delay set to {delay}s")
    return ('', 204)

@app.route('/export_logs')
def export_logs():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Timestamp", "Type", "Thread", "Item", "Buffer"])
    for entry in log_data:
        writer.writerow([entry["ts"], entry["kind"].upper(), entry["thread"], entry["item"], entry["buffer"]])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=sim_logs.csv"}
    )

@app.route('/export_stats')
def export_stats():
    summary = {
        "total_produced":    produce_count,
        "total_consumed":    consume_count,
        "avg_prod_rate":     round(sum(s["prod"] for s in stats_history) / max(len(stats_history),1), 2),
        "avg_cons_rate":     round(sum(s["cons"] for s in stats_history) / max(len(stats_history),1), 2),
        "peak_prod_rate":    max((s["prod"] for s in stats_history), default=0),
        "peak_cons_rate":    max((s["cons"] for s in stats_history), default=0),
        "deadlock_detected": deadlock_detected,
        "deadlock_info":     deadlock_info,
        "num_buffers":       len(buffers),
        "buffer_stats":      [{"name": b.name, "total_in": b.total_in, "total_out": b.total_out} for b in buffers],
    }
    return Response(
        json.dumps(summary, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment;filename=sim_stats.json"}
    )

@socketio.on('connect')
def on_connect():
    emit_state()

# ===============================
# HTML TEMPLATE
# ===============================
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OS Simulator — Producer/Consumer</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&family=Syne:wght@700;800;900&display=swap" rel="stylesheet">
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root {
  --bg:       #070b0f;
  --s1:       #0b1118;
  --s2:       #0f1822;
  --border:   #1a2535;
  --border2:  #243040;
  --cp:       #00e5ff;
  --cc:       #ff4f7b;
  --cy:       #f0c040;
  --cg:       #3ddc84;
  --text:     #c8d8e8;
  --muted:    #3d5468;
  --muted2:   #5a7a92;
  --gp:       0 0 20px #00e5ff40;
  --gc:       0 0 20px #ff4f7b40;
  --gy:       0 0 14px #f0c04040;
  --gg:       0 0 14px #3ddc8440;
  --radius:   10px;
  --font:     'JetBrains Mono', monospace;
  --display:  'Syne', sans-serif;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  font-family: var(--font);
  background: var(--bg);
  color: var(--text);
  overflow: hidden;
  height: 100vh;
  display: flex;
  flex-direction: column;
}
body::before {
  content: '';
  position: fixed; inset: 0;
  background-image:
    linear-gradient(rgba(0,229,255,.018) 1px, transparent 1px),
    linear-gradient(90deg, rgba(0,229,255,.018) 1px, transparent 1px);
  background-size: 36px 36px;
  pointer-events: none; z-index: 0;
}

/* ===== HEADER ===== */
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  height: 56px;
  border-bottom: 1px solid var(--border);
  background: var(--s1);
  flex-shrink: 0;
  z-index: 10;
  position: relative;
}
.logo { display: flex; align-items: center; gap: 14px; }
.logo h1 {
  font-family: var(--display);
  font-size: 1.15rem; font-weight: 900; color: #fff; letter-spacing: -0.5px;
}
.logo .badge {
  font-size: .58rem; letter-spacing: 2.5px; color: var(--cp);
  border: 1px solid var(--cp); padding: 2px 7px; border-radius: 4px;
  text-transform: uppercase; opacity: .85;
}
.header-right { display: flex; align-items: center; gap: 10px; }
.status-pill {
  display: flex; align-items: center; gap: 6px;
  padding: 5px 12px; border-radius: 20px;
  border: 1px solid var(--border2); background: var(--s2);
  font-size: .65rem; letter-spacing: .8px; text-transform: uppercase;
}
.dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
.dot.run    { background: var(--cg); box-shadow: var(--gg); animation: blink 1.2s infinite; }
.dot.stop   { background: var(--cc); }
.dot.auto   { background: var(--cp); }
.dot.manual { background: var(--cy); box-shadow: var(--gy); }
.dot.dead   { background: var(--cc); box-shadow: var(--gc); animation: blink .5s infinite; }

#deadlock-banner {
  display: none;
  background: rgba(255,79,123,.15);
  border: 1px solid rgba(255,79,123,.5);
  color: var(--cc);
  padding: 5px 14px;
  border-radius: 6px;
  font-size: .65rem;
  letter-spacing: 1px;
  animation: blink .8s infinite;
  cursor: pointer;
}
#deadlock-banner:hover { background: rgba(255,79,123,.25); }

@keyframes blink { 0%,100%{opacity:1} 50%{opacity:.4} }

/* ===== LAYOUT ===== */
.workspace {
  display: grid;
  grid-template-columns: 260px 1fr 280px;
  flex: 1;
  overflow: hidden;
  position: relative;
  z-index: 1;
}

/* ===== SIDEBARS ===== */
.sidebar, .sidebar-right {
  border-right: 1px solid var(--border);
  background: var(--s1);
  display: flex; flex-direction: column;
  overflow-y: auto;
  padding: 16px;
  gap: 20px;
  scrollbar-width: thin;
  scrollbar-color: var(--border) transparent;
}
.sidebar-right { border-right: none; border-left: 1px solid var(--border); }

.sec-label {
  font-size: .57rem; letter-spacing: 3px; text-transform: uppercase;
  color: var(--muted); margin-bottom: 10px; padding-bottom: 6px;
  border-bottom: 1px solid var(--border);
}

/* stat cards */
.stats-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 7px; }
.stat-card {
  background: var(--s2); border: 1px solid var(--border);
  border-radius: 8px; padding: 10px 8px; text-align: center;
}
.stat-card .val { font-size: 1.3rem; font-weight: 700; line-height: 1.1; margin-bottom: 3px; }
.stat-card .lbl { font-size: .58rem; color: var(--muted2); letter-spacing: 1px; }
.stat-card.p .val { color: var(--cp); text-shadow: var(--gp); }
.stat-card.c .val { color: var(--cc); text-shadow: var(--gc); }
.stat-card.g .val { color: var(--cg); text-shadow: var(--gg); }

/* buttons */
.btn {
  display: flex; align-items: center; justify-content: center; gap: 7px;
  padding: 9px 12px; border-radius: 7px; border: 1px solid var(--border2);
  background: var(--s2); color: var(--text); font-family: var(--font);
  font-size: .72rem; cursor: pointer; transition: all .18s;
  text-decoration: none; letter-spacing: .4px; width: 100%; margin-bottom: 5px;
}
.btn:hover { border-color: var(--cp); color: #fff; background: #0e1e2e; }
.btn.primary { background: var(--cp); color: #000; border-color: var(--cp); font-weight: 700; box-shadow: var(--gp); }
.btn.primary:hover { background: #00cce0; }
.btn.danger  { background: var(--cc); color: #fff; border-color: var(--cc); font-weight: 700; }
.btn.danger:hover  { background: #e03060; }
.btn.warn    { border-color: var(--cy); color: var(--cy); }
.btn.warn:hover    { background: rgba(240,192,64,.1); }
.btn.sm      { padding: 6px 10px; font-size: .65rem; margin-bottom: 0; width: auto; }
.btn.active  { border-color: var(--cp); color: var(--cp); background: rgba(0,229,255,.07); }
.btn-row     { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-bottom: 5px; }

/* form fields */
.field { display: flex; align-items: center; gap: 8px; margin-bottom: 7px; }
.field label { font-size: .6rem; color: var(--muted2); width: 18px; text-transform: uppercase; flex-shrink: 0; }
.field input, .field select {
  flex: 1; background: var(--bg); border: 1px solid var(--border);
  border-radius: 5px; color: var(--text); font-family: var(--font);
  font-size: .72rem; padding: 6px 9px; outline: none; transition: border-color .2s;
}
.field input:focus, .field select:focus { border-color: var(--cp); }

/* ===== CENTER ===== */
.center { display: flex; flex-direction: column; overflow: hidden; }

.buffers-section {
  padding: 16px 18px; border-bottom: 1px solid var(--border); flex-shrink: 0;
}
.buffers-grid { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px; }

.buffer-card {
  background: var(--s2); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 12px 14px;
  min-width: 150px; flex: 1; max-width: 200px;
  position: relative; overflow: hidden; transition: border-color .2s;
}
.buffer-card::after {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
  background: linear-gradient(90deg, var(--cp), transparent); opacity: .5;
}
.buffer-card:hover { border-color: var(--border2); }
.buffer-card.active-prod { border-color: rgba(0,229,255,.4); box-shadow: 0 0 12px rgba(0,229,255,.1); }
.buffer-card.active-cons { border-color: rgba(255,79,123,.4); box-shadow: 0 0 12px rgba(255,79,123,.1); }

.buffer-card .buf-name {
  font-size: .58rem; letter-spacing: 2px; text-transform: uppercase;
  color: var(--muted2); margin-bottom: 8px;
  display: flex; justify-content: space-between; align-items: center;
}
.items-row { display: flex; flex-wrap: wrap; gap: 3px; min-height: 26px; margin-bottom: 8px; }
.item-chip {
  padding: 2px 7px; background: rgba(0,229,255,.1); border: 1px solid rgba(0,229,255,.25);
  border-radius: 4px; font-size: .65rem; color: var(--cp);
  animation: chipIn .15s ease;
}
@keyframes chipIn { from{transform:scale(.6);opacity:0} to{transform:scale(1);opacity:1} }

.bar-track { height: 4px; background: var(--border); border-radius: 2px; overflow: hidden; margin-bottom: 4px; }
.bar-fill { height: 100%; border-radius: 2px; transition: width .4s ease; }
.bar-fill.low  { background: var(--cg); }
.bar-fill.mid  { background: var(--cy); }
.bar-fill.high { background: var(--cc); box-shadow: var(--gc); }

.buf-meta {
  display: flex; justify-content: space-between; align-items: center;
  font-size: .6rem; color: var(--muted2);
}
.buf-inject-btn {
  font-size: .55rem; padding: 2px 6px; cursor: pointer;
  background: transparent; border: 1px solid var(--border2);
  border-radius: 3px; color: var(--muted2); font-family: var(--font);
  transition: all .15s;
}
.buf-inject-btn:hover { border-color: var(--cp); color: var(--cp); }

/* utilization badge */
.util-badge {
  font-size: .55rem; padding: 1px 6px; border-radius: 3px;
  background: rgba(61,220,132,.1); color: var(--cg);
  border: 1px solid rgba(61,220,132,.2);
}

/* chart */
.chart-section { padding: 12px 18px; border-bottom: 1px solid var(--border); flex-shrink: 0; }
.chart-wrap { position: relative; height: 110px; margin-top: 8px; }

/* logs */
.logs-section {
  flex: 1; padding: 12px 18px 16px; display: flex;
  flex-direction: column; overflow: hidden; min-height: 0;
}
.log-toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.log-filter-btn {
  font-size: .58rem; padding: 3px 10px; cursor: pointer;
  background: transparent; border: 1px solid var(--border2);
  border-radius: 12px; color: var(--muted2); font-family: var(--font);
  letter-spacing: .5px; transition: all .15s;
}
.log-filter-btn:hover, .log-filter-btn.active { border-color: var(--cp); color: var(--cp); background: rgba(0,229,255,.07); }
.log-filter-btn.active-c { border-color: var(--cc); color: var(--cc); background: rgba(255,79,123,.07); }
.log-search {
  margin-left: auto; background: var(--bg); border: 1px solid var(--border);
  border-radius: 5px; color: var(--text); font-family: var(--font);
  font-size: .65rem; padding: 4px 9px; outline: none; width: 140px; transition: border-color .2s;
}
.log-search:focus { border-color: var(--cp); }
.log-box {
  flex: 1; overflow-y: auto; scrollbar-width: thin; scrollbar-color: var(--border) transparent;
  background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
  padding: 8px 12px; font-size: .67rem; line-height: 1.9; min-height: 0;
}
.log-line {
  display: flex; gap: 8px; align-items: flex-start;
  padding: 1px 0; border-bottom: 1px solid rgba(26,37,53,.6);
}
.log-line:last-child { border-bottom: none; }
.log-line.hidden { display: none; }
.log-badge {
  font-size: .52rem; letter-spacing: .8px; padding: 2px 6px; border-radius: 3px;
  flex-shrink: 0; margin-top: 2px; font-weight: 700;
}
.log-badge.produce { background: rgba(0,229,255,.12); color: var(--cp); border: 1px solid rgba(0,229,255,.25); }
.log-badge.consume { background: rgba(255,79,123,.12); color: var(--cc); border: 1px solid rgba(255,79,123,.25); }
.log-text { color: var(--text); opacity: .8; }
.log-text mark { background: rgba(240,192,64,.25); color: var(--cy); border-radius: 2px; padding: 0 2px; }

/* ===== THREAD MONITOR ===== */
.thread-list { display: flex; flex-direction: column; gap: 5px; }
.thread-row {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 10px; background: var(--s2); border: 1px solid var(--border);
  border-radius: 7px; font-size: .65rem; transition: border-color .2s;
}
.thread-row:hover { border-color: var(--border2); }
.thread-id { font-weight: 700; width: 28px; flex-shrink: 0; }
.thread-id.p-id { color: var(--cp); }
.thread-id.c-id { color: var(--cc); }
.thread-state {
  font-size: .55rem; letter-spacing: 1px; padding: 2px 7px; border-radius: 10px; flex-shrink: 0;
}
.thread-state.RUNNING  { background: rgba(61,220,132,.15); color: var(--cg); border: 1px solid rgba(61,220,132,.3); }
.thread-state.WAITING  { background: rgba(240,192,64,.12); color: var(--cy); border: 1px solid rgba(240,192,64,.3); animation: blink 1s infinite; }
.thread-state.SLEEPING { background: rgba(58,90,110,.2); color: var(--muted2); border: 1px solid var(--border); }
.thread-state.PAUSED   { background: rgba(255,79,123,.12); color: var(--cc); border: 1px solid rgba(255,79,123,.3); }
.thread-actions { margin-left: auto; display: flex; gap: 4px; }
.thr-btn {
  font-size: .55rem; padding: 2px 7px; cursor: pointer;
  background: transparent; border: 1px solid var(--border2);
  border-radius: 4px; color: var(--muted2); font-family: var(--font); transition: all .15s;
}
.thr-btn:hover { border-color: var(--cy); color: var(--cy); }
.thr-btn.paused { border-color: var(--cc); color: var(--cc); }

/* buffer detail panel */
.buffer-detail-list { display: flex; flex-direction: column; gap: 6px; }
.buf-detail-row {
  background: var(--s2); border: 1px solid var(--border);
  border-radius: 7px; padding: 8px 10px; font-size: .63rem;
}
.buf-detail-row .bd-name { color: var(--cp); font-weight: 600; margin-bottom: 4px; }
.buf-detail-row .bd-stats { display: flex; gap: 10px; color: var(--muted2); }
.buf-detail-row .bd-stats span { display: flex; flex-direction: column; gap: 1px; }
.buf-detail-row .bd-stats strong { color: var(--text); font-size: .7rem; }

/* ===== DEADLOCK MODAL ===== */
.modal-overlay {
  display: none;
  position: fixed; inset: 0;
  background: rgba(0,0,0,.75);
  backdrop-filter: blur(6px);
  z-index: 9000;
  align-items: center; justify-content: center;
}
.modal-overlay.open { display: flex; }
.modal {
  background: var(--s1);
  border: 1px solid rgba(255,79,123,.4);
  border-radius: 14px;
  padding: 28px 32px;
  max-width: 560px;
  width: 90%;
  box-shadow: 0 0 60px rgba(255,79,123,.15), 0 20px 60px rgba(0,0,0,.6);
  animation: modalIn .25s ease;
}
@keyframes modalIn { from{transform:scale(.9) translateY(20px);opacity:0} to{transform:none;opacity:1} }

.modal-header {
  display: flex; align-items: center; gap: 14px; margin-bottom: 20px;
}
.modal-icon {
  width: 44px; height: 44px; border-radius: 10px;
  background: rgba(255,79,123,.15); border: 1px solid rgba(255,79,123,.4);
  display: flex; align-items: center; justify-content: center;
  font-size: 1.3rem; flex-shrink: 0;
  animation: blink .8s infinite;
}
.modal-title { font-family: var(--display); font-size: 1rem; font-weight: 900; color: var(--cc); }
.modal-subtitle { font-size: .62rem; color: var(--muted2); margin-top: 2px; }

.modal-cause {
  background: rgba(255,79,123,.08); border: 1px solid rgba(255,79,123,.2);
  border-radius: 8px; padding: 12px 14px; margin-bottom: 14px;
}
.modal-cause .cause-label {
  font-size: .55rem; letter-spacing: 2px; text-transform: uppercase;
  color: var(--cc); margin-bottom: 6px; opacity: .8;
}
.modal-cause .cause-title { font-size: .85rem; font-weight: 700; color: #fff; margin-bottom: 8px; }
.modal-cause .cause-desc { font-size: .7rem; color: var(--muted2); line-height: 1.7; }

.modal-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 14px; }
.modal-box {
  background: var(--s2); border: 1px solid var(--border);
  border-radius: 8px; padding: 10px 12px;
}
.modal-box .mb-label { font-size: .55rem; letter-spacing: 1.5px; text-transform: uppercase; color: var(--muted); margin-bottom: 6px; }
.modal-box .mb-items { display: flex; flex-wrap: wrap; gap: 4px; }
.modal-box .mb-tag {
  font-size: .62rem; padding: 2px 8px; border-radius: 4px;
  background: rgba(0,229,255,.08); color: var(--cp); border: 1px solid rgba(0,229,255,.2);
}
.modal-box .mb-tag.cons { background: rgba(255,79,123,.08); color: var(--cc); border-color: rgba(255,79,123,.2); }
.modal-box .mb-tag.warn { background: rgba(240,192,64,.08); color: var(--cy); border-color: rgba(240,192,64,.2); }
.modal-box .mb-tag.empty-tag { color: var(--muted2); border-color: var(--border); background: none; font-style: italic; }

.modal-rec {
  background: rgba(61,220,132,.07); border: 1px solid rgba(61,220,132,.2);
  border-radius: 8px; padding: 10px 14px; margin-bottom: 18px;
}
.modal-rec .rec-label { font-size: .55rem; letter-spacing: 2px; text-transform: uppercase; color: var(--cg); margin-bottom: 5px; }
.modal-rec .rec-text  { font-size: .7rem; color: var(--text); line-height: 1.6; }

.modal-ts { font-size: .6rem; color: var(--muted); margin-bottom: 14px; }
.modal-close {
  width: 100%; padding: 10px; border-radius: 7px; border: 1px solid rgba(255,79,123,.4);
  background: rgba(255,79,123,.1); color: var(--cc); font-family: var(--font);
  font-size: .72rem; cursor: pointer; font-weight: 700; transition: all .18s;
}
.modal-close:hover { background: rgba(255,79,123,.2); }

/* ===== TOASTS ===== */
#toast-container {
  position: fixed; bottom: 20px; right: 20px; z-index: 9999;
  display: flex; flex-direction: column-reverse; gap: 8px; pointer-events: none;
}
.toast {
  padding: 10px 16px; border-radius: 8px; font-size: .68rem; font-family: var(--font);
  letter-spacing: .3px; max-width: 320px; pointer-events: auto; border: 1px solid;
  animation: toastIn .25s ease, toastOut .3s ease 3.5s forwards;
}
.toast.info  { background: rgba(0,229,255,.1);  border-color: rgba(0,229,255,.3);  color: var(--cp); }
.toast.warn  { background: rgba(240,192,64,.1); border-color: rgba(240,192,64,.3); color: var(--cy); }
.toast.error { background: rgba(255,79,123,.1); border-color: rgba(255,79,123,.3); color: var(--cc); }
@keyframes toastIn  { from{transform:translateX(30px);opacity:0} to{transform:none;opacity:1} }
@keyframes toastOut { from{opacity:1} to{opacity:0; transform:translateX(30px)} }

::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 3px; }
</style>
</head>
<body>

<!-- HEADER -->
<header>
  <div class="logo">
    <h1>OS Simulator</h1>
    <span class="badge">Producer / Consumer</span>
  </div>
  <div class="header-right">
    <div id="deadlock-banner" onclick="openDeadlockModal()">⚠ DEADLOCK — click for details</div>
    <div class="status-pill">
      <span class="dot" id="run-dot"></span>
      <span id="run-label">STOPPED</span>
    </div>
    <div class="status-pill">
      <span class="dot" id="mode-dot"></span>
      <span id="mode-label">AUTO</span>
    </div>
    <a href="/export_logs"  class="btn sm">📥 CSV</a>
    <a href="/export_stats" class="btn sm">📊 Stats</a>
  </div>
</header>

<div class="workspace">

  <!-- LEFT SIDEBAR -->
  <aside class="sidebar">
    <div>
      <div class="sec-label">Throughput</div>
      <div class="stats-grid">
        <div class="stat-card p"><div class="val" id="prod-rate">0</div><div class="lbl">P/sec</div></div>
        <div class="stat-card c"><div class="val" id="cons-rate">0</div><div class="lbl">C/sec</div></div>
        <div class="stat-card p"><div class="val" id="prod-count">0</div><div class="lbl">Produced</div></div>
        <div class="stat-card c"><div class="val" id="cons-count">0</div><div class="lbl">Consumed</div></div>
        <div class="stat-card g" style="grid-column:span 2;">
          <div class="val" id="avg-util">0%</div>
          <div class="lbl">Avg Buffer Utilization</div>
        </div>
      </div>
    </div>

    <div>
      <div class="sec-label">Simulation</div>
      <a href="/toggle" class="btn primary" id="toggle-btn">▶ Start</a>
    </div>

    <div>
      <div class="sec-label">Mode</div>
      <div class="btn-row">
        <a href="/set_mode?mode=auto"   class="btn" id="btn-auto">⚡ Auto</a>
        <a href="/set_mode?mode=manual" class="btn" id="btn-manual">🎛 Manual</a>
      </div>
      <div id="mode-hint" style="font-size:.6rem;color:var(--muted2);line-height:1.5;padding:6px 4px;"></div>
    </div>

    <div>
      <div class="sec-label">Global Delays (s)</div>
      <form action="/set_speed" method="get">
        <div class="field"><label>P</label><input name="p" type="number" step="0.1" min="0.1" placeholder="0.5"></div>
        <div class="field"><label>C</label><input name="c" type="number" step="0.1" min="0.1" placeholder="0.5"></div>
        <button type="submit" class="btn">Apply Delays</button>
      </form>
    </div>

    <div>
      <div class="sec-label">Producers (<span id="np">3</span>)</div>
      <div class="btn-row">
        <a href="/add_producer"    class="btn">＋ Add</a>
        <a href="/remove_producer" class="btn">－ Remove</a>
      </div>
    </div>

    <div>
      <div class="sec-label">Consumers (<span id="nc">3</span>)</div>
      <div class="btn-row">
        <a href="/add_consumer"    class="btn">＋ Add</a>
        <a href="/remove_consumer" class="btn">－ Remove</a>
      </div>
    </div>

    <div>
      <div class="sec-label">Buffers</div>
      <div class="btn-row">
        <a href="/add_buffer"    class="btn">＋ Add</a>
        <a href="/remove_buffer" class="btn warn">－ Remove</a>
      </div>
      <div class="field" style="margin-top:6px;">
        <label style="width:60px;font-size:.58rem;">New Cap</label>
        <input id="new-buf-cap" type="number" min="1" max="20" value="5" placeholder="capacity">
      </div>
      <button class="btn" onclick="addBufWithCap()">Add with Capacity</button>
    </div>

    <div>
      <div class="sec-label">Inject Item</div>
      <div class="field">
        <label style="width:50px;font-size:.58rem;">Buffer</label>
        <select id="inj-buf"></select>
      </div>
      <div class="field">
        <label style="width:50px;font-size:.58rem;">Value</label>
        <input id="inj-val" type="number" min="1" max="100" placeholder="random">
      </div>
      <button class="btn" onclick="injectItem()">Inject</button>
    </div>
  </aside>

  <!-- CENTER -->
  <div class="center">

    <!-- BUFFERS -->
    <div class="buffers-section">
      <div style="display:flex;align-items:center;justify-content:space-between;">
        <div class="sec-label" style="margin-bottom:0;">Shared Buffers</div>
        <div style="font-size:.6rem;color:var(--muted2);">
          Auto mode: producers → least-full · consumers → most-full
        </div>
      </div>
      <div class="buffers-grid" id="buffers-grid"></div>
    </div>

    <!-- CHART -->
    <div class="chart-section">
      <div class="sec-label" style="margin-bottom:0;">Live Throughput — Producer vs Consumer</div>
      <div class="chart-wrap">
        <canvas id="rateChart"></canvas>
      </div>
    </div>

    <!-- LOGS -->
    <div class="logs-section">
      <div class="log-toolbar">
        <span class="sec-label" style="margin-bottom:0;border:none;padding:0;">Event Log</span>
        <button class="log-filter-btn active" id="flt-all"     onclick="setFilter('all')">ALL</button>
        <button class="log-filter-btn"        id="flt-produce" onclick="setFilter('produce')">PRODUCE</button>
        <button class="log-filter-btn"        id="flt-consume" onclick="setFilter('consume')">CONSUME</button>
        <input  class="log-search" id="log-search" placeholder="Search logs…" oninput="applySearch(this.value)">
      </div>
      <div class="log-box" id="log-box"></div>
    </div>

  </div>

  <!-- RIGHT SIDEBAR -->
  <aside class="sidebar-right">
    <div>
      <div class="sec-label">Thread Monitor</div>
      <div class="thread-list" id="thread-list"></div>
    </div>
    <div>
      <div class="sec-label">Buffer Stats</div>
      <div class="buffer-detail-list" id="buf-detail-list"></div>
    </div>
  </aside>

</div>

<!-- DEADLOCK MODAL -->
<div class="modal-overlay" id="deadlock-modal">
  <div class="modal">
    <div class="modal-header">
      <div class="modal-icon">🔒</div>
      <div>
        <div class="modal-title">Deadlock Detected</div>
        <div class="modal-subtitle">All active threads are in a waiting state</div>
      </div>
    </div>

    <div class="modal-cause">
      <div class="cause-label">Root Cause</div>
      <div class="cause-title" id="dl-cause">—</div>
      <div class="cause-desc"  id="dl-explanation">—</div>
    </div>

    <div class="modal-grid">
      <div class="modal-box">
        <div class="mb-label">Waiting Producers</div>
        <div class="mb-items" id="dl-waiting-prod"></div>
      </div>
      <div class="modal-box">
        <div class="mb-label">Waiting Consumers</div>
        <div class="mb-items" id="dl-waiting-cons"></div>
      </div>
      <div class="modal-box">
        <div class="mb-label">Full Buffers</div>
        <div class="mb-items" id="dl-full-bufs"></div>
      </div>
      <div class="modal-box">
        <div class="mb-label">Empty Buffers</div>
        <div class="mb-items" id="dl-empty-bufs"></div>
      </div>
    </div>

    <div class="modal-rec">
      <div class="rec-label">💡 Recommendation</div>
      <div class="rec-text" id="dl-recommendation">—</div>
    </div>

    <div class="modal-ts" id="dl-ts"></div>
    <button class="modal-close" onclick="closeDeadlockModal()">✕ Dismiss</button>
  </div>
</div>

<!-- TOASTS -->
<div id="toast-container"></div>

<script>
const socket = io();

// ===== CHART =====
const ctx = document.getElementById('rateChart').getContext('2d');
const rateChart = new Chart(ctx, {
  type: 'line',
  data: {
    labels: [],
    datasets: [
      { label: 'Producer', data: [], borderColor: '#00e5ff', backgroundColor: 'rgba(0,229,255,.08)', borderWidth: 2, tension: .45, fill: true, pointRadius: 0 },
      { label: 'Consumer', data: [], borderColor: '#ff4f7b', backgroundColor: 'rgba(255,79,123,.06)', borderWidth: 2, tension: .45, fill: true, pointRadius: 0 }
    ]
  },
  options: {
    responsive: true, maintainAspectRatio: false, animation: false,
    plugins: {
      legend: { labels: { color: '#5a7a92', font: { family: 'JetBrains Mono', size: 10 }, boxWidth: 12 } },
      tooltip: { enabled: false }
    },
    scales: {
      x: { ticks: { color: '#3d5468', font: { family: 'JetBrains Mono', size: 9 }, maxTicksLimit: 8 }, grid: { color: 'rgba(26,37,53,.6)' } },
      y: { ticks: { color: '#3d5468', font: { family: 'JetBrains Mono', size: 9 } }, grid: { color: 'rgba(26,37,53,.6)' }, min: 0 }
    }
  }
});

// ===== STATE =====
let logFilter  = 'all';
let searchTerm = '';
let toastSeen  = new Set();
let pausedP    = new Set();
let pausedC    = new Set();
let lastDeadlockInfo = null;
let deadlockModalOpenedFor = null;

function setFilter(f) {
  logFilter = f;
  ['all','produce','consume'].forEach(k => {
    document.getElementById('flt-'+k).className = 'log-filter-btn' + (k === f ? ' active' : '');
  });
  applyFilters();
}

function applySearch(v) {
  searchTerm = v.toLowerCase().trim();
  applyFilters();
}

function applyFilters() {
  document.querySelectorAll('.log-line').forEach(el => {
    const kind = el.dataset.kind, text = el.dataset.text;
    const matchFilter = logFilter === 'all' || kind === logFilter;
    const matchSearch = !searchTerm || text.includes(searchTerm);
    el.classList.toggle('hidden', !(matchFilter && matchSearch));
    const textEl = el.querySelector('.log-text');
    if (textEl) {
      if (searchTerm && matchFilter && matchSearch)
        textEl.innerHTML = text.replace(new RegExp(`(${searchTerm})`, 'gi'), '<mark>$1</mark>');
      else if (matchFilter)
        textEl.textContent = text;
    }
  });
}

// ===== DEADLOCK MODAL =====
function openDeadlockModal() {
  if (!lastDeadlockInfo) return;
  const di = lastDeadlockInfo;

  document.getElementById('dl-cause').textContent       = di.cause        || '—';
  document.getElementById('dl-explanation').textContent = di.explanation   || '—';
  document.getElementById('dl-recommendation').textContent = di.recommendation || '—';
  document.getElementById('dl-ts').textContent = di.detected_at ? `Detected at ${di.detected_at}` : '';

  const makeItems = (arr, cls='') =>
    arr && arr.length
      ? arr.map(x => `<span class="mb-tag ${cls}">${x}</span>`).join('')
      : '<span class="mb-tag empty-tag">none</span>';

  document.getElementById('dl-waiting-prod').innerHTML = makeItems(di.waiting_producers, '');
  document.getElementById('dl-waiting-cons').innerHTML = makeItems(di.waiting_consumers, 'cons');
  document.getElementById('dl-full-bufs').innerHTML    = makeItems(di.full_buffers,      'warn');
  document.getElementById('dl-empty-bufs').innerHTML   = makeItems(di.empty_buffers,     'cons');

  document.getElementById('deadlock-modal').classList.add('open');
}

function closeDeadlockModal() {
  document.getElementById('deadlock-modal').classList.remove('open');
}

// Close on overlay click
document.getElementById('deadlock-modal').addEventListener('click', function(e) {
  if (e.target === this) closeDeadlockModal();
});

// ===== SOCKET =====
socket.on('state', (d) => {
  // Header
  const runDot = document.getElementById('run-dot');
  runDot.className = 'dot ' + (d.running ? 'run' : 'stop');
  document.getElementById('run-label').textContent = d.running ? 'RUNNING' : 'STOPPED';

  const modeDot = document.getElementById('mode-dot');
  modeDot.className = 'dot ' + (d.manual_mode ? 'manual' : 'auto');
  document.getElementById('mode-label').textContent = d.manual_mode ? 'MANUAL' : 'AUTO';

  // Mode hint
  document.getElementById('mode-hint').textContent = d.manual_mode
    ? 'MANUAL: threads pick buffers randomly — good for testing edge cases.'
    : 'AUTO: threads use smart load-balancing — all buffers stay utilized.';

  // Deadlock banner
  const dlBanner = document.getElementById('deadlock-banner');
  dlBanner.style.display = d.deadlock ? 'block' : 'none';

  // Save deadlock info and auto-open modal on first detection
  if (d.deadlock && d.deadlock_info && Object.keys(d.deadlock_info).length) {
    lastDeadlockInfo = d.deadlock_info;
    const key = d.deadlock_info.detected_at;
    if (key && key !== deadlockModalOpenedFor) {
      deadlockModalOpenedFor = key;
      openDeadlockModal();
    }
  }
  if (!d.deadlock) { lastDeadlockInfo = null; deadlockModalOpenedFor = null; }

  // Toggle btn
  const tb = document.getElementById('toggle-btn');
  tb.textContent = d.running ? '⏹ Stop Simulation' : '▶ Start Simulation';
  tb.className = 'btn ' + (d.running ? 'danger' : 'primary');

  // Mode btns
  document.getElementById('btn-auto').className   = 'btn' + (!d.manual_mode ? ' active' : '');
  document.getElementById('btn-manual').className = 'btn' + ( d.manual_mode ? ' active' : '');

  // Stats
  document.getElementById('prod-rate').textContent  = d.prod_rate;
  document.getElementById('cons-rate').textContent  = d.cons_rate;
  document.getElementById('prod-count').textContent = d.produce_count;
  document.getElementById('cons-count').textContent = d.consume_count;
  document.getElementById('avg-util').textContent   = (d.avg_buffer_util || 0) + '%';
  document.getElementById('np').textContent = d.num_producers;
  document.getElementById('nc').textContent = d.num_consumers;

  // Chart
  rateChart.data.labels = d.stats_history.map(s => s.ts + 's');
  rateChart.data.datasets[0].data = d.stats_history.map(s => s.prod);
  rateChart.data.datasets[1].data = d.stats_history.map(s => s.cons);
  rateChart.update('none');

  // Buffers grid
  const grid = document.getElementById('buffers-grid');
  if (grid.children.length !== d.buffers.length) {
    grid.innerHTML = '';
    d.buffers.forEach((b, i) => {
      const card = document.createElement('div');
      card.className = 'buffer-card';
      card.id = `buf-card-${i}`;
      grid.appendChild(card);
    });
    const sel = document.getElementById('inj-buf');
    sel.innerHTML = d.buffers.map((b,i) => `<option value="${i}">${b.name}</option>`).join('');
  }
  d.buffers.forEach((b, i) => {
    const card = document.getElementById(`buf-card-${i}`);
    if (!card) return;
    const pct = b.fill_pct;
    const cls = pct > 79 ? 'high' : pct > 49 ? 'mid' : 'low';
    card.innerHTML = `
      <div class="buf-name">
        <span>${b.name}</span>
        <button class="buf-inject-btn" onclick="quickInject(${i})">＋</button>
      </div>
      <div class="items-row">${b.items.map(v => `<span class="item-chip">${v}</span>`).join('')}</div>
      <div class="bar-track"><div class="bar-fill ${cls}" style="width:${pct}%"></div></div>
      <div class="buf-meta">
        <span>${b.fill} / ${b.capacity}</span>
        <span class="util-badge">${pct}%</span>
      </div>`;
  });

  // Buffer detail list
  document.getElementById('buf-detail-list').innerHTML = d.buffers.map(b => `
    <div class="buf-detail-row">
      <div class="bd-name">${b.name}</div>
      <div class="bd-stats">
        <span><strong>${b.fill}/${b.capacity}</strong>filled</span>
        <span><strong>${b.total_in}</strong>in</span>
        <span><strong>${b.total_out}</strong>out</span>
      </div>
    </div>`).join('');

  // Thread monitor
  pausedP = new Set(d.paused_producers.map(String));
  pausedC = new Set(d.paused_consumers.map(String));
  let html = '';
  for (let i = 0; i < d.num_producers; i++) {
    const paused = pausedP.has(String(i));
    const state  = paused ? 'PAUSED' : (d.producer_states[String(i)] || 'SLEEPING');
    html += `<div class="thread-row">
      <span class="thread-id p-id">P${i}</span>
      <span class="thread-state ${state}">${state}</span>
      <div class="thread-actions">
        <button class="thr-btn ${paused?'paused':''}" onclick="pauseThread('producer',${i})">${paused?'▶':'⏸'}</button>
        <button class="thr-btn" onclick="promptThreadSpeed('producer',${i})">⚙</button>
      </div></div>`;
  }
  for (let i = 0; i < d.num_consumers; i++) {
    const paused = pausedC.has(String(i));
    const state  = paused ? 'PAUSED' : (d.consumer_states[String(i)] || 'SLEEPING');
    html += `<div class="thread-row">
      <span class="thread-id c-id">C${i}</span>
      <span class="thread-state ${state}">${state}</span>
      <div class="thread-actions">
        <button class="thr-btn ${paused?'paused':''}" onclick="pauseThread('consumer',${i})">${paused?'▶':'⏸'}</button>
        <button class="thr-btn" onclick="promptThreadSpeed('consumer',${i})">⚙</button>
      </div></div>`;
  }
  document.getElementById('thread-list').innerHTML = html;

  // Logs
  const lb = document.getElementById('log-box');
  const wasAtBottom = lb.scrollHeight - lb.scrollTop - lb.clientHeight < 40;
  lb.innerHTML = d.logs.slice().reverse().map(l => `
    <div class="log-line" data-kind="${l.kind}" data-text="${l.msg}">
      <span class="log-badge ${l.kind}">${l.kind === 'produce' ? 'PROD' : 'CONS'}</span>
      <span class="log-text">${l.msg}</span>
    </div>`).join('');
  applyFilters();
  if (wasAtBottom) lb.scrollTop = 0;

  // Toasts
  d.toasts.forEach(t => {
    const key = t.ts + t.msg;
    if (!toastSeen.has(key)) { toastSeen.add(key); showToast(t.type, t.msg); }
  });
});

// ===== ACTIONS =====
function pauseThread(kind, id) { fetch(`/pause_thread?kind=${kind}&id=${id}`); }
function promptThreadSpeed(kind, id) {
  const d = prompt(`Set delay for ${kind[0].toUpperCase()}${id} (seconds):`, '0.5');
  if (d !== null) fetch(`/set_thread_speed?kind=${kind}&id=${id}&delay=${parseFloat(d)}`);
}
function quickInject(idx) { fetch(`/inject_item?idx=${idx}&val=${Math.floor(Math.random()*100)+1}`); }
function injectItem() {
  const idx = document.getElementById('inj-buf').value;
  const val = document.getElementById('inj-val').value || Math.floor(Math.random()*100)+1;
  fetch(`/inject_item?idx=${idx}&val=${val}`);
}
function addBufWithCap() {
  const cap = document.getElementById('new-buf-cap').value || 5;
  window.location.href = `/add_buffer?cap=${cap}`;
}

// ===== TOASTS =====
function showToast(type, msg) {
  const tc  = document.getElementById('toast-container');
  const div = document.createElement('div');
  div.className = `toast ${type}`;
  div.textContent = msg;
  tc.appendChild(div);
  setTimeout(() => div.remove(), 4000);
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)