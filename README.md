# 🧵 Producer-Consumer Synchronization Simulator

## 📌 Overview

This project is a **web-based simulation of the Producer-Consumer problem**, a classic synchronization problem in Operating Systems.

It demonstrates how multiple producer and consumer threads interact with a **shared bounded buffer**, while preventing issues such as:

* Race Conditions
* Deadlock
* Starvation

The system is built using **Flask, Multithreading, and WebSockets** to provide a **real-time interactive visualization** of thread behavior, buffer states, and system performance.

---

## 🌐 Live Demo

🔗 https://producer-consumer-simulator-l4gm.onrender.com/

---
## 📸 Screenshots

### 🖥️ Complete System Interface

![System](images/Complete%20System%20Interface%20of%20OS%20Simulator.png)

### 📦 Buffer Visualization

![Buffer](images/Buffer%20Visualization%20Interface.png)

### 📝 Event Logs

![Logs](images/Event%20Log%20of%20Producer%20and%20Consumer%20Activities.png)

### 🎮 Manual Controls

![Controls](images/Manual%20Control%20of%20Producers,%20Consumers,%20Buffers%20and%20Execution.png)

### 🧵 Thread Monitoring

![Threads](images/Thread%20Monitoring%20System.png)

### 📊 Throughput Graph

![Graph](images/Throughput%20Graph.png)

---
## 🎯 Objectives

* Simulate the Producer-Consumer problem in real time
* Understand thread synchronization concepts
* Visualize buffer conditions (Full / Empty)
* Demonstrate use of locks and condition variables
* Analyze system performance using graphs

---

## 🧠 Core Concepts

* Process Synchronization
* Critical Section Problem
* Mutual Exclusion
* Thread Communication
* Bounded Buffer Problem

---

## ⚙️ System Working

### 🔹 Producers

* Generate data items continuously
* Insert items into the shared buffer

### 🔹 Consumers

* Remove items from the buffer
* Process consumed items

### 🔹 Buffer Behavior

* Fixed-size (bounded buffer)
* If **buffer is FULL → producers wait**
* If **buffer is EMPTY → consumers wait**

---

## 🔐 Synchronization Mechanism

### 🧵 Mutex Locks

* Ensures only one thread accesses the buffer at a time
* Prevents race conditions

### 🔔 Condition Variables

* Used to make threads wait when buffer is full/empty
* Notify other threads when state changes

---

## 🖥️ Features & Functionalities

### 1️⃣ Complete System Interface

* Displays the full simulation dashboard
* Shows buffers, logs, controls, and graphs together

### 2️⃣ Buffer Visualization Interface

* Real-time view of buffer contents
* Shows insertion and removal of items

### 3️⃣ Event Log of Producer & Consumer Activities

* Displays real-time logs
* Tracks producer and consumer actions

### 4️⃣ Manual Control of System

* Control number of producers and consumers
* Adjust buffer size
* Modify execution speed

### 5️⃣ Thread Monitoring System

* Displays thread states:

  * RUNNING
  * WAITING
  * SLEEPING

### 6️⃣ Throughput Graph

* Shows production vs consumption rate
* Helps analyze system performance

---



## 🧰 Technologies Used

| Technology            | Purpose           |
| --------------------- | ----------------- |
| Python                | Backend logic     |
| Flask                 | Web framework     |
| Threading             | Concurrency       |
| Flask-SocketIO        | Real-time updates |
| HTML, CSS, JavaScript | Frontend          |

---

## 🛠️ Installation & Setup

### Clone the repository

```bash
git clone https://github.com/shravani492006/producer-consumer-simulator.git
cd producer-consumer-simulator
```

### Install dependencies

```bash
pip install -r requirements.txt
```

### Run the project

```bash
python app.py
```

### Open in browser

http://localhost:5000

---

## 📈 Results

* Proper synchronization achieved
* No race conditions observed
* Efficient coordination between threads
* Real-time visualization enhances understanding

---

## ⚠️ Challenges Faced

* Managing concurrent threads
* Avoiding deadlocks
* Real-time UI updates
* Debugging synchronization issues

---

## 💡 Applications

* Operating System learning
* Concurrency simulation
* Real-time system modeling

---

## 🔮 Future Enhancements

* Add semaphore-based implementation
* Implement other synchronization problems
* Improve UI with advanced analytics

---

## 📌 Conclusion

This project successfully demonstrates the Producer-Consumer problem using real-time simulation and synchronization techniques. It provides a practical understanding of how operating systems handle concurrent processes efficiently.

---
