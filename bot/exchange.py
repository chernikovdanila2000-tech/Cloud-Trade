"""Работа с биржей.

LiveBroker   — реальные ордера через ccxt (Binance USDⓈ-M Futures; режимы testnet и live).
PaperBroker  — бумажная торговля: реальные цены, но сделки только в памяти/файле.

У обоих одинаковый интерфейс, поэтому главный цикл бота не знает, с каким брокером работает.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import ccxt
import pandas as pd

from .config import EXCLUDED_BASES, BotConfig

log = logging.getLogger("exchange")


@dataclass
class Position:
    symbol: str
    side: str        # long | short
    qty: float
    entry: float


def _new_exchange(opts: dict) -> ccxt.binanceusdm:
    ex = ccxt.binanceusdm({"enableRateLimit": True, **opts})
    proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    if proxy:                      # ccxt не читает системный прокси сам
        ex.https_proxy = proxy
    return ex


def _to_df(rows: list) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.drop(columns="ts").astype(float)


class MarketData:
    """Публичные данные: свечи, тикеры, список пар. Ключи не нужны."""

    def __init__(self, ex: ccxt.binanceusdm):
        self.ex = ex
        self.ex.load_markets()

    def closed_candles(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        """Только закрытые свечи (последняя, ещё формирующаяся, отбрасывается)."""
        rows = self.ex.fetch_ohlcv(symbol, timeframe, limit=min(limit + 1, 1500))
        df = _to_df(rows)
        tf_ms = self.ex.parse_timeframe(timeframe) * 1000
        now_ms = self.ex.milliseconds()
        if len(df) and df.index[-1].value // 10**6 + tf_ms > now_ms:
            df = df.iloc[:-1]
        return df

    def top_symbols(self, n: int, min_quote_volume: float) -> list[str]:
        """Самые ликвидные бессрочные USDT-фьючерсы по объёму за 24 часа."""
        tickers = self.ex.fetch_tickers()
        rows = []
        for sym, t in tickers.items():
            m = self.ex.markets.get(sym)
            if not m or not m.get("swap") or not m.get("linear") or m.get("quote") != "USDT":
                continue
            if not m.get("active", True) or m.get("base") in EXCLUDED_BASES:
                continue
            qv = t.get("quoteVolume") or 0
            if qv >= min_quote_volume:
                rows.append((qv, sym))
        return [s for _, s in sorted(rows, reverse=True)[:n]]

    def last_price(self, symbol: str) -> float:
        return float(self.ex.fetch_ticker(symbol)["last"])


class LiveBroker(MarketData):
    def __init__(self, cfg: BotConfig):
        if not cfg.api_key or not cfg.api_secret:
            raise SystemExit("Для режимов testnet/live нужны BINANCE_API_KEY и BINANCE_API_SECRET в .env")
        ex = _new_exchange({
            "apiKey": cfg.api_key, "secret": cfg.api_secret,
            "options": {"adjustForTimeDifference": True},
        })
        if cfg.mode == "testnet":
            ex.set_sandbox_mode(True)
        super().__init__(ex)
        self.cfg = cfg
        try:
            ex.set_position_mode(False)          # one-way режим (не hedge)
        except Exception as e:                    # уже установлен или есть открытые позиции
            log.debug("set_position_mode: %s", e)

    # --- счёт ---
    def equity(self) -> float:
        bal = self.ex.fetch_balance()
        info = bal.get("info", {})
        val = info.get("totalMarginBalance")
        return float(val) if val is not None else float(bal["total"].get("USDT", 0))

    def positions(self) -> dict[str, Position]:
        out = {}
        for p in self.ex.fetch_positions():
            qty = float(p.get("contracts") or 0)
            if qty > 0:
                out[p["symbol"]] = Position(p["symbol"], p["side"], qty, float(p["entryPrice"]))
        return out

    # --- ордера ---
    def prepare_symbol(self, symbol: str, leverage: int):
        for call in (lambda: self.ex.set_margin_mode("isolated", symbol),
                     lambda: self.ex.set_leverage(leverage, symbol)):
            try:
                call()
            except Exception as e:  # "No need to change margin type" и т.п.
                log.debug("prepare %s: %s", symbol, e)

    def normalize_qty(self, symbol: str, qty: float, price: float) -> float:
        m = self.ex.market(symbol)
        q = float(self.ex.amount_to_precision(symbol, qty))
        min_qty = (m.get("limits", {}).get("amount", {}) or {}).get("min") or 0
        min_cost = (m.get("limits", {}).get("cost", {}) or {}).get("min") or 5
        if q < min_qty or q * price < min_cost:
            return 0.0
        return q

    def market_order(self, symbol: str, side: str, qty: float, reduce_only: bool = False) -> float:
        """side: buy | sell. Возвращает среднюю цену исполнения."""
        o = self.ex.create_order(symbol, "market", side, qty, None,
                                 {"reduceOnly": True} if reduce_only else {})
        price = o.get("average") or o.get("price")
        if not price:
            time.sleep(1)
            price = self.ex.fetch_order(o["id"], symbol).get("average")
        return float(price or self.last_price(symbol))

    def place_stop(self, symbol: str, pos_side: str, qty: float, price: float) -> str:
        side = "sell" if pos_side == "long" else "buy"
        o = self.ex.create_order(symbol, "market", side, qty, None, {
            "stopLossPrice": float(self.ex.price_to_precision(symbol, price)),
            "reduceOnly": True, "workingType": "MARK_PRICE"})
        return str(o["id"])

    def place_take(self, symbol: str, pos_side: str, qty: float, price: float) -> str:
        side = "sell" if pos_side == "long" else "buy"
        o = self.ex.create_order(symbol, "market", side, qty, None, {
            "takeProfitPrice": float(self.ex.price_to_precision(symbol, price)),
            "reduceOnly": True, "workingType": "MARK_PRICE"})
        return str(o["id"])

    def open_trigger_ids(self, symbol: str) -> set[str]:
        return {str(o["id"]) for o in self.ex.fetch_open_orders(symbol, params={"trigger": True})}

    def cancel_trigger(self, symbol: str, order_id: str):
        try:
            self.ex.cancel_order(order_id, symbol, params={"trigger": True})
        except ccxt.OrderNotFound:
            pass

    def cancel_all(self, symbol: str):
        for params in ({}, {"trigger": True}):
            try:
                self.ex.cancel_all_orders(symbol, params=params)
            except Exception as e:
                log.debug("cancel_all %s %s: %s", symbol, params, e)


class PaperBroker(MarketData):
    """Бумажная торговля на реальных котировках Binance.

    SL/TP проверяются по минутным свечам: если цена задела уровень, позиция закрывается
    по цене уровня (с проскальзыванием). Состояние хранится в data/paper.json.
    """

    def __init__(self, cfg: BotConfig, data_source: str = "mainnet"):
        ex = _new_exchange({})
        if data_source == "testnet":
            ex.set_sandbox_mode(True)
        super().__init__(ex)
        self.cfg = cfg
        self.path = Path(cfg.state_file).with_name("paper.json")
        self.fee = cfg.risk.fee_rate
        self.slip = cfg.risk.slippage
        self._load()

    def _load(self):
        if self.path.exists():
            s = json.loads(self.path.read_text())
            self.balance = s["balance"]
            self.pos = {k: Position(**v) for k, v in s["positions"].items()}
            self.orders = s["orders"]
            self.checked = s["checked"]
            self.next_id = s["next_id"]
        else:
            self.balance, self.pos, self.orders, self.checked, self.next_id = self.cfg.paper_balance, {}, {}, {}, 1

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "balance": self.balance, "positions": {k: asdict(v) for k, v in self.pos.items()},
            "orders": self.orders, "checked": self.checked, "next_id": self.next_id}, indent=1))

    # --- симуляция срабатывания SL/TP ---
    def _simulate(self):
        for sym in list(self.pos):
            p = self.pos[sym]
            since = self.checked.get(sym, self.ex.milliseconds() - 60_000)
            df = _to_df(self.ex.fetch_ohlcv(sym, "1m", since=since, limit=1000))
            for ts, bar in df.iterrows():
                hit = None
                for oid, o in list(self.orders.items()):
                    if o["symbol"] != sym:
                        continue
                    lvl = o["price"]
                    if o["kind"] == "stop" and ((p.side == "long" and bar.low <= lvl) or (p.side == "short" and bar.high >= lvl)):
                        hit = (oid, min(bar.open, lvl) if p.side == "long" else max(bar.open, lvl))
                        break  # стоп проверяется первым (пессимистично)
                    if o["kind"] == "take" and ((p.side == "long" and bar.high >= lvl) or (p.side == "short" and bar.low <= lvl)):
                        hit = (oid, lvl)
                if hit:
                    self._close(sym, hit[1])
                    break
            if len(df):
                self.checked[sym] = int(df.index[-1].value // 10**6)
        self._save()

    def _close(self, sym: str, price: float):
        p = self.pos.pop(sym)
        d = 1 if p.side == "long" else -1
        px = price * (1 - d * self.slip)
        self.balance += d * (px - p.entry) * p.qty - px * p.qty * self.fee
        self.orders = {k: v for k, v in self.orders.items() if v["symbol"] != sym}
        self.checked.pop(sym, None)

    # --- интерфейс как у LiveBroker ---
    def equity(self) -> float:
        self._simulate()
        unreal = 0.0
        for p in self.pos.values():
            d = 1 if p.side == "long" else -1
            unreal += d * (self.last_price(p.symbol) - p.entry) * p.qty
        return self.balance + unreal

    def positions(self) -> dict[str, Position]:
        self._simulate()
        return dict(self.pos)

    def prepare_symbol(self, symbol: str, leverage: int):
        pass

    def normalize_qty(self, symbol: str, qty: float, price: float) -> float:
        q = float(self.ex.amount_to_precision(symbol, qty))
        return q if q * price >= 5 else 0.0

    def market_order(self, symbol: str, side: str, qty: float, reduce_only: bool = False) -> float:
        price = self.last_price(symbol)
        if reduce_only:
            self._close(symbol, price)
            self._save()
            return price
        d = 1 if side == "buy" else -1
        px = price * (1 + d * self.slip)
        self.balance -= px * qty * self.fee
        self.pos[symbol] = Position(symbol, "long" if side == "buy" else "short", qty, px)
        self.checked[symbol] = self.ex.milliseconds()
        self._save()
        return px

    def _add_order(self, symbol: str, kind: str, price: float) -> str:
        oid = f"paper-{self.next_id}"
        self.next_id += 1
        self.orders[oid] = {"symbol": symbol, "kind": kind, "price": price}
        self._save()
        return oid

    def place_stop(self, symbol, pos_side, qty, price) -> str:
        return self._add_order(symbol, "stop", price)

    def place_take(self, symbol, pos_side, qty, price) -> str:
        return self._add_order(symbol, "take", price)

    def open_trigger_ids(self, symbol: str) -> set[str]:
        return {k for k, v in self.orders.items() if v["symbol"] == symbol}

    def cancel_trigger(self, symbol: str, order_id: str):
        self.orders.pop(order_id, None)
        self._save()

    def cancel_all(self, symbol: str):
        self.orders = {k: v for k, v in self.orders.items() if v["symbol"] != symbol}
        self._save()


def make_broker(cfg: BotConfig, paper_data: str = "mainnet"):
    if cfg.mode == "paper":
        return PaperBroker(cfg, paper_data)
    if cfg.mode in ("testnet", "live"):
        return LiveBroker(cfg)
    raise SystemExit(f"Неизвестный MODE={cfg.mode}: используйте paper, testnet или live")
