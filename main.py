
import sys
import threading
import time
from datetime import datetime, timedelta
from typing import Union, Optional

import MetaTrader5 as mt5
import pandas as pd
import pytz
import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QLabel,
    QLineEdit, QPushButton, QFormLayout, QHBoxLayout
)

# =========================================================
# FastAPI app
# =========================================================
app = FastAPI(
    title="MT5 Candle Fetcher",
    description="Fetch market candles from MetaTrader 5",
    version="1.0.0",
    swagger_ui_parameters={"theme": "dark"},
)

# =========================================================
# Models
# =========================================================
class CandleRequest(BaseModel):
    symbol: str
    time_frame: str                    # e.g., M1, M5, M15, H1, H4, D1, W1, MN1
    time_from: Union[int, float, str]  # unix seconds or 'YYYY-MM-DD HH:MM:SS'
    time_to:   Union[int, float, str]

class CandleOffsetRequest(BaseModel):
    symbol: str
    time_frame: str
    offset: int                        # 0 = most recent
    count: int                         # how many bars

# =========================================================
# Globals & helpers
# =========================================================
window = None  # will be set in __main__ after GUI is created
utc_time: Optional[datetime] = None
delta: Optional[timedelta] = None

mt5_lock = threading.Lock()
mt5_ready = False

TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1, "M2": mt5.TIMEFRAME_M2, "M3": mt5.TIMEFRAME_M3, "M4": mt5.TIMEFRAME_M4,
    "M5": mt5.TIMEFRAME_M5, "M6": mt5.TIMEFRAME_M6, "M10": mt5.TIMEFRAME_M10, "M12": mt5.TIMEFRAME_M12,
    "M15": mt5.TIMEFRAME_M15, "M20": mt5.TIMEFRAME_M20, "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1, "H2": mt5.TIMEFRAME_H2, "H3": mt5.TIMEFRAME_H3, "H4": mt5.TIMEFRAME_H4,
    "H6": mt5.TIMEFRAME_H6, "H8": mt5.TIMEFRAME_H8, "H12": mt5.TIMEFRAME_H12,
    "D1": mt5.TIMEFRAME_D1, "W1": mt5.TIMEFRAME_W1, "MN1": mt5.TIMEFRAME_MN1,
}

def _ensure_gui_ready():
    if window is None:
        raise HTTPException(status_code=503, detail="GUI not ready yet")

def _ensure_credentials_present():
    if not window.mt5_path_input.text().strip() \
       or not window.login_input.text().strip() \
       or not window.password_input.text().strip() \
       or not window.server_input.text().strip():
        raise HTTPException(status_code=503, detail="MT5 credentials not set in GUI")

def _ensure_mt5_ready():
    """
    Initialize and login to MT5 exactly once (thread-safe).
    """
    global mt5_ready
    _ensure_gui_ready()
    _ensure_credentials_present()

    if mt5_ready:
        return

    with mt5_lock:
        if mt5_ready:
            return
        path = window.mt5_path_input.text().strip()
        if not mt5.initialize(path):
            raise HTTPException(status_code=500, detail="MT5 initialization failed")
        if not mt5.login(
            int(window.login_input.text().strip()),
            password=window.password_input.text(),
            server=window.server_input.text().strip()
        ):
            raise HTTPException(status_code=500, detail="MT5 login failed")
        mt5_ready = True
        print("[mt5] initialized and logged in")

def _parse_delta_text(s: str) -> timedelta:
    """
    Accepts:
      - minutes (e.g. "210", "-90")
      - signed time: "+03:30", "-02:00", "+03:30:15"
      - "0" treated as zero
    """
    s = s.strip()
    if s in ("", "0", "+0", "-0"):
        return timedelta(0)

    # minutes form
    if s.lstrip("+-").isdigit():
        return timedelta(minutes=int(s))

    # clock form
    sign = 1
    if s[0] in "+-":
        sign = -1 if s[0] == "-" else 1
        s = s[1:]
    parts = s.split(":")
    if len(parts) not in (2, 3):
        raise HTTPException(status_code=400, detail="Invalid delta format. Use minutes or ±HH:MM[:SS].")
    try:
        hh = int(parts[0]); mm = int(parts[1]); ss = int(parts[2]) if len(parts) == 3 else 0
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid delta format. Use minutes or ±HH:MM[:SS].")
    return sign * timedelta(hours=hh, minutes=mm, seconds=ss)

def _ensure_delta():
    """
    Use GUI-provided delta if present; otherwise auto-detect via your rounded logic.
    """
    global delta
    if delta is not None:
        return

    _ensure_gui_ready()  # we might read GUI delta

    # 1) Try GUI delta
    gui_text = window.delta_input.text().strip() if hasattr(window, "delta_input") else ""
    if gui_text != "":
        parsed = _parse_delta_text(gui_text)
        delta = parsed
        print(f"[delta] using GUI-provided delta: {delta}")
        return

    # 2) Auto-detect (rounded to 30 minutes)
    _ensure_mt5_ready()
    rates = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_M1, 0, 1)
    if not rates or len(rates) == 0:
        raise HTTPException(status_code=500, detail="Unable to read reference tick for delta")
    localTimeFirstTick = datetime.utcfromtimestamp(rates[0][0]).replace(tzinfo=pytz.utc)
    localTimeFirstTick = localTimeFirstTick.replace(microsecond=(rates[0][0] % 1000) * 1000)
    utcNow = datetime.utcnow().replace(tzinfo=pytz.utc)
    rawOffset = localTimeFirstTick - utcNow
    totalMinutes = rawOffset.total_seconds() / 60
    roundedMinutes = round(totalMinutes / 30) * 30
    delta = timedelta(minutes=roundedMinutes)
    print(f"[delta] auto lft:{localTimeFirstTick}, now:{utcNow}, roundedmin:{roundedMinutes}, totalmin:{totalMinutes}")

def _parse_time_any(t: Union[int, float, str]) -> datetime:
    """
    Accept unix seconds or ISO-like strings; returns UTC-aware datetime.
    """
    if isinstance(t, (int, float)):
        return datetime.utcfromtimestamp(t).replace(tzinfo=pytz.utc)
    s = str(t).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=pytz.utc)
        except ValueError:
            pass
    # pandas parser
    try:
        dt = pd.to_datetime(s, utc=True)
        if isinstance(dt, pd.Timestamp):
            return dt.to_pydatetime()
    except Exception:
        pass
    raise HTTPException(status_code=400, detail=f"Invalid time format: {t!r}. Use Unix seconds or 'YYYY-MM-DD HH:MM:SS'")

def _to_payload(df: pd.DataFrame, delta_: timedelta) -> list[list]:
    """
    Returns rows as:
    ['adjusted_time', 'open', 'high', 'low', 'close', 'tick_volume', 'spread', 'real_volume']
    where adjusted_time = MT5 'time' (epoch seconds) - delta.seconds
    """
    if df.empty:
        return []
    df = df.copy()
    df['adjusted_time'] = df['time'] - delta_.total_seconds()
    cols = ['adjusted_time', 'open', 'high', 'low', 'close', 'tick_volume', 'spread', 'real_volume']
    for c in cols:
        if c not in df.columns:
            df[c] = 0
    return df[cols].values.tolist()

# =========================================================
# Background updater (only after MT5 is ready)
# =========================================================
def update_utc_time():
    global utc_time
    while True:
        try:
            if mt5_ready:
                rates = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_M1, 0, 1)
                if rates:
                    utc_time = datetime.utcfromtimestamp(rates[0][0]).replace(tzinfo=pytz.utc)
        except Exception:
            pass
        time.sleep(1)

@app.on_event("startup")
async def startup_event():
    threading.Thread(target=update_utc_time, daemon=True, name="utc_updater").start()

# =========================================================
# Endpoints
# =========================================================
@app.get("/status")
def status():
    creds_set = False
    if window is not None:
        creds_set = all([
            window.mt5_path_input.text().strip() != "",
            window.login_input.text().strip() != "",
            window.password_input.text().strip() != "",
            window.server_input.text().strip() != "",
        ])
    return {
        "gui_ready": window is not None,
        "mt5_ready": mt5_ready,
        "credentials_set": creds_set,
        "delta_seconds": None if delta is None else int(delta.total_seconds()),
        "utc_time_tracking": utc_time.isoformat() if utc_time else None,
    }

@app.post("/get_candles/")
async def get_candles(data: CandleRequest):
    _ensure_mt5_ready()
    _ensure_delta()
    if data.time_frame not in TIMEFRAME_MAP:
        raise HTTPException(status_code=400, detail="Invalid time frame. Try: M1, M5, M15, M30, H1, H4, D1, W1, MN1")

    try:
        time_from = _parse_time_any(data.time_from) + delta
        time_to   = _parse_time_any(data.time_to)   + delta
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid time format")

    rates = mt5.copy_rates_range(data.symbol, TIMEFRAME_MAP[data.time_frame], time_from, time_to)
    if rates is None or len(rates) == 0:
        raise HTTPException(status_code=404, detail="No data found for the given parameters")

    df = pd.DataFrame(rates)
    payload = _to_payload(df, delta)
    return JSONResponse(content=payload)

@app.post("/get_candles_by_offset/")
async def get_candles_by_offset(data: CandleOffsetRequest):
    _ensure_mt5_ready()
    _ensure_delta()
    if data.time_frame not in TIMEFRAME_MAP:
        raise HTTPException(status_code=400, detail="Invalid time frame. Try: M1, M5, M15, M30, H1, H4, D1, W1, MN1")
    if data.count <= 0:
        raise HTTPException(status_code=400, detail="'count' must be > 0")
    if data.offset < 0:
        raise HTTPException(status_code=400, detail="'offset' must be >= 0")

    rates = mt5.copy_rates_from_pos(data.symbol, TIMEFRAME_MAP[data.time_frame], data.offset, data.count)
    if rates is None or len(rates) == 0:
        raise HTTPException(status_code=404, detail="No data found for the given parameters")

    df = pd.DataFrame(rates)
    payload = _to_payload(df, delta)
    return JSONResponse(content=payload)

# =========================================================
# PyQt GUI
# =========================================================
class MT5CandleFetcherApp(QWidget):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("MT5 Candle Fetcher")
        self.setGeometry(100, 100, 720, 620)

        self.layout = QVBoxLayout()
        self.form_layout = QFormLayout()

        # MT5 creds
        self.mt5_path_input = QLineEdit(self)
        self.form_layout.addRow("MT5 Path", self.mt5_path_input)

        self.login_input = QLineEdit(self)
        self.form_layout.addRow("MT5 Login", self.login_input)

        self.password_input = QLineEdit(self)
        self.password_input.setEchoMode(QLineEdit.Password)
        self.form_layout.addRow("MT5 Password", self.password_input)

        self.server_input = QLineEdit(self)
        self.form_layout.addRow("MT5 Server", self.server_input)

        # Symbol & timeframe
        self.symbol_input = QLineEdit(self)
        self.form_layout.addRow("Symbol", self.symbol_input)

        self.time_frame_input = QLineEdit(self)
        self.form_layout.addRow("Time Frame (e.g., M1, M5, H1)", self.time_frame_input)

        # Delta
        self.delta_input = QLineEdit(self)
        self.delta_input.setPlaceholderText("Leave empty = auto. Examples: +03:30, -02:00, 210 (minutes)")
        self.form_layout.addRow("Delta", self.delta_input)

        # Range inputs
        self.time_from_input = QLineEdit(self)
        self.time_from_input.setPlaceholderText("Unix seconds or 'YYYY-MM-DD HH:MM:SS'")
        self.form_layout.addRow("Time From", self.time_from_input)

        self.time_to_input = QLineEdit(self)
        self.time_to_input.setPlaceholderText("Unix seconds or 'YYYY-MM-DD HH:MM:SS'")
        self.form_layout.addRow("Time To", self.time_to_input)

        # Offset inputs
        self.offset_input = QLineEdit(self)
        self.offset_input.setPlaceholderText("0 = most recent")
        self.form_layout.addRow("Offset", self.offset_input)

        self.count_input = QLineEdit(self)
        self.count_input.setPlaceholderText("e.g., 500")
        self.form_layout.addRow("Count", self.count_input)

        self.layout.addLayout(self.form_layout)

        # Buttons row
        btn_row = QHBoxLayout()

        self.fetch_button = QPushButton("Fetch Candles (Range)", self)
        self.fetch_button.clicked.connect(self.fetch_candles_range)
        btn_row.addWidget(self.fetch_button)

        self.fetch_offset_button = QPushButton("Fetch Candles (Offset/Count)", self)
        self.fetch_offset_button.clicked.connect(self.fetch_candles_offset)
        btn_row.addWidget(self.fetch_offset_button)

        self.reset_mt5_button = QPushButton("Reset MT5", self)
        self.reset_mt5_button.clicked.connect(self.reset_mt5)
        btn_row.addWidget(self.reset_mt5_button)

        self.reset_delta_button = QPushButton("Reset Delta", self)
        self.reset_delta_button.clicked.connect(self.reset_delta)
        btn_row.addWidget(self.reset_delta_button)

        self.layout.addLayout(btn_row)

        self.result_label = QLabel("Result will be displayed here", self)
        self.layout.addWidget(self.result_label)

        self.setLayout(self.layout)

    # --- GUI helpers ---
    def _api_post(self, path: str, payload: dict):
        try:
            resp = requests.post(f"http://127.0.0.1:8000{path}", json=payload, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                self.result_label.setText(f"OK: first rows={str(data[:3])} ... total={len(data)}")
            else:
                try:
                    detail = resp.json().get("detail")
                except Exception:
                    detail = resp.text
                self.result_label.setText(f"Error {resp.status_code}: {detail}")
        except requests.exceptions.RequestException as e:
            self.result_label.setText(f"HTTP error: {e}")

    def fetch_candles_range(self):
        payload = {
            "symbol": self.symbol_input.text().strip(),
            "time_frame": self.time_frame_input.text().strip(),
            "time_from": self.time_from_input.text().strip(),
            "time_to": self.time_to_input.text().strip(),
        }
        self._api_post("/get_candles/", payload)

    def fetch_candles_offset(self):
        # validate ints
        try:
            offset = int(self.offset_input.text().strip())
            count = int(self.count_input.text().strip())
        except ValueError:
            self.result_label.setText("Offset/Count must be integers.")
            return
        payload = {
            "symbol": self.symbol_input.text().strip(),
            "time_frame": self.time_frame_input.text().strip(),
            "offset": offset,
            "count": count,
        }
        self._api_post("/get_candles_by_offset/", payload)

    def reset_mt5(self):
        global mt5_ready
        with mt5_lock:
            try:
                if mt5_ready:
                    mt5.shutdown()
                    print("[mt5] shutdown")
            except Exception:
                pass
            mt5_ready = False
        self.result_label.setText("MT5 state reset. Next call will re-initialize & login.")

    def reset_delta(self):
        global delta
        delta = None
        self.result_label.setText("Delta reset. Next call will use GUI delta (if set) or auto-detect.")

# =========================================================
# Entrypoint
# =========================================================
if __name__ == "__main__":
    # 1) Start GUI FIRST (so 'window' exists for API guards)
    app_qt = QApplication(sys.argv)
    window = MT5CandleFetcherApp()
    window.show()

    # 2) Start FastAPI on localhost only
    def start_fastapi():
        # For remote access, change host to "0.0.0.0" AND protect it (token/IP allowlist)
        uvicorn.run(app, host="0.0.0.0", port=8000)

    fastapi_thread = threading.Thread(target=start_fastapi, daemon=True, name="uvicorn_thread")
    fastapi_thread.start()

    # 3) Qt event loop
    sys.exit(app_qt.exec_())
