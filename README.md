# MT5 Candle Fetcher

A combined **PyQt5 desktop application** and **FastAPI backend service** for fetching and serving market candles from **MetaTrader 5 (MT5)**.

This tool allows you to:
- Configure MT5 credentials via a desktop GUI.
- Fetch candle data (by date range or offset) through a REST API.
- Adjust time offsets (delta) automatically or manually.
- Reset MT5 session and delta from the GUI.
- Run a **local FastAPI server** and GUI in parallel.

---

## 🚀 Features

- **PyQt5 GUI**  
  Set MT5 path, login, password, server, symbol, timeframe, and delta offset interactively.

- **REST API (FastAPI)**  
  - `POST /get_candles/` → fetch candles between `time_from` and `time_to`.  
  - `POST /get_candles_by_offset/` → fetch candles by `offset` (bars ago) and `count`.  
  - `GET /status` → check system health, MT5 readiness, delta, and UTC time sync.

- **Delta Offset Handling**  
  - Manual input (`+03:30`, `-02:00`, or minutes).  
  - Auto-detection based on XAUUSD ticks (rounded to 30 minutes).

- **Thread-Safe MT5 Integration**  
  MT5 is initialized and logged in once, shared across API calls.

- **Background UTC Time Tracking**  
  Keeps MT5 UTC reference updated in the background.

---

## 📦 Installation

### Requirements

- Python **3.9+**
- Installed **MetaTrader5 terminal** (with valid account)
- MT5 Python package

Install dependencies:

```bash
pip install MetaTrader5 fastapi uvicorn pandas pytz requests pyqt5
```
▶️ Usage
Run the script:

```bash

python main.py
```

This will:

Open the PyQt5 GUI for entering MT5 credentials and parameters.

Start a FastAPI server on http://127.0.0.1:8000 (by default).

🔗 API Endpoints
Health Check

```http
GET /status
```
Response example:

```json

{
  "gui_ready": true,
  "mt5_ready": true,
  "credentials_set": true,
  "delta_seconds": 12600,
  "utc_time_tracking": "2025-10-02T09:23:00+00:00"
}
```
Fetch Candles by Range
```http
POST /get_candles/
```
Body:

```json

{
  "symbol": "XAUUSD",
  "time_frame": "M5",
  "time_from": "2025-09-30 00:00:00",
  "time_to": "2025-09-30 12:00:00"
}
```
Fetch Candles by Offset
```http
POST /get_candles_by_offset/
```
Body:

```json
{
  "symbol": "XAUUSD",
  "time_frame": "M1",
  "offset": 0,
  "count": 100
}
```
🖥️ GUI Overview
Inputs: MT5 path, login, password, server, symbol, timeframe, delta, time range, offset/count.

Buttons:

Fetch Candles (Range) → Calls /get_candles/

Fetch Candles (Offset/Count) → Calls /get_candles_by_offset/

Reset MT5 → Reinitializes MT5 connection.

Reset Delta → Clears delta so it will be auto-detected or re-entered.

Result is shown directly in the GUI label.

⚠️ Notes
By default, FastAPI runs on 0.0.0.0:8000 (accessible on LAN).
If exposing remotely, add authentication / IP allowlist.

Tested with MetaTrader5 (build 3900+).

GUI and FastAPI share state via globals; do not run multiple processes in parallel.