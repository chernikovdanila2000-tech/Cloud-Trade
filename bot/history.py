"""Загрузка исторических свечей фьючерсов из публичного архива data.binance.vision."""
from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
import requests

BASE = "https://data.binance.vision/data/futures/um/monthly/klines"
COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume",
        "trades", "taker_base", "taker_quote", "ignore"]
CACHE = Path("data/history")


def _months(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def _load_month(symbol: str, tf: str, y: int, m: int) -> pd.DataFrame | None:
    path = CACHE / symbol / tf / f"{y}-{m:02d}.csv.gz"
    if path.exists():
        return pd.read_csv(path, index_col=0, parse_dates=True)
    url = f"{BASE}/{symbol}/{tf}/{symbol}-{tf}-{y}-{m:02d}.zip"
    r = requests.get(url, timeout=60)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        raw = z.read(z.namelist()[0]).decode()
    first = raw.split("\n", 1)[0]
    df = pd.read_csv(io.StringIO(raw), header=0 if first.startswith("open_time") else None)
    df.columns = COLS
    df.index = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path)
    return df


def load(symbol: str, tf: str, start: date, end: date) -> pd.DataFrame:
    parts = [p for y, m in _months(start, end) if (p := _load_month(symbol, tf, y, m)) is not None]
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts).sort_index()
    df.index = pd.to_datetime(df.index, utc=True)
    return df[~df.index.duplicated()]
