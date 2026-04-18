# 🖥️ OS Simulator — Producer Consumer Problem

## 📌 Overview
This project is a **real-time Operating System simulation** that demonstrates the classic **Producer-Consumer problem** using Python.

It visually simulates how multiple producer and consumer threads interact with shared buffers, showcasing:
- Synchronization
- Thread management
- Deadlock detection
- Starvation handling
- Load balancing

Built using **Flask + Flask-SocketIO + Threading** with a dynamic web UI.

---

## ⚙️ Features

### 🔄 Core Simulation
- Multiple **Producer and Consumer threads**
- Shared **bounded buffers**
- Real-time item production and consumption
- Thread state tracking (RUNNING / WAITING / SLEEPING)

### 🧠 Smart System Logic
- Auto load-balancing between buffers
- Round-robin + optimization scheduling
- Deadlock detection with root cause analysis
- Starvation detection for consumers

### 📊 Live Dashboard
- Real-time statistics (items produced/consumed)
- Live throughput chart (Producer vs Consumer)
- Buffer utilization visualization
- Thread monitoring panel

### 🎛️ Controls
- Start / Stop simulation
- Manual & Auto mode switching
- Add / Remove producers and consumers
- Add / Resize buffers dynamically
- Inject items manually into buffers
- Pause / Resume individual threads
- Adjust thread speed dynamically

### 📤 Export Features
- Export logs as **CSV**
- Export statistics as **JSON**

---

## 🧰 Tech Stack

- Python 🐍
- Flask 🌐
- Flask-SocketIO ⚡
- HTML / CSS / JavaScript (Frontend UI)
- Threading (Concurrency model)

---

## 🚀 How to Run

### 1️⃣ Install dependencies
```bash
pip install flask flask-socketio