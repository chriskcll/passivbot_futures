from __future__ import annotations
import asyncio
import json
import websockets
import os
import sys
import numpy as np
import pandas as pd
import pprint
from datetime import datetime
from math import ceil
from math import floor
from time import time, sleep
from typing import Callable, Iterator
from passivbot import init_ccxt, load_key_secret, load_settings, make_get_filepath, print_, \
    ts_to_date, flatten, filter_orders, Bot, start_bot, round_up, round_dn, \
    calc_long_entry_price, calc_shrt_entry_price, calc_entry_qty
from binance import fetch_trades as fetch_trades_binance


def calc_long_liq_price(leverage, price, maintenance_margin_rate=0.005):
    return (price * leverage) / (leverage + 1 - (maintenance_margin_rate * leverage))

def calc_long_bankruptcy_price(qty, order_value, equity, order_margin, fee_to_open):
    return (1.00075 * qty) / (order_value + (equity - order_margin - fee_to_open))

def calc_long_liq_price_cross(leverage, equity, qty, order_margin, entry_price, maintenance_margin_rate=0.005):
    bankruptcy_price = 0.0
    return (4000 * bankruptcy_price * entry_price * qty) / \
        (4000 * equity * bankruptcy_price * entry_price - 4000 * bankruptcy_price * ((maintenance_margin_rate - 1) * qty + entry_price * order_margin))

'''

qty * (1 / price - 1 / liq_price) == -(equity - order_margin - (qty / price) * maintenance_margin_rate - (qty * 0.00075 / bankruptcy_price))
'''

def calc_shrt_liq_price(leverage, price, maintenance_margin_rate=0.005):
    return (price * leverage) / (leverage - 1 + (maintenance_margin_rate * leverage))

def calc_shrt_bankruptcy_price():
    pass

def calc_shrt_liq_price_cross(leverage, price, maintenance_margin_rate=0.005):
    pass


async def fetch_trades(cc, symbol: str, from_id: int = None) -> [dict]:

    params = {'symbol': symbol, 'limit': 1000}
    if from_id:
        params['from'] = from_id
    fetched_trades = await cc.public_get_trading_records(params=params)
    trades = [{'trade_id': int(t['id']),
               'side': t['side'],
               'price': t['price'],
               'qty': t['qty'],
               'timestamp': date_to_ts(t['time'][:-1])} for t in fetched_trades['result']]
    print_(['fetched trades', symbol, trades[0]['trade_id'],
            ts_to_date(trades[0]['timestamp'] / 1000)])
    return trades

def date_to_ts(date: str):
    date = date[:23].replace('Z', '')
    try:
        return datetime.strptime(date, "%Y-%m-%dT%H:%M:%S.%f").timestamp() * 1000
    except ValueError:
        formats = ["%Y-%m-%dT%H:%M:%S"]
        for f in formats:
            try:
                return datetime.strptime(date, f).timestamp() * 1000
            except ValueError:
                continue
    raise Exception(f'unable to convert date {date} to timestamp')

async def create_bot(user: str, settings: str):
    bot = BybitBot(user, settings)
    await bot._init()
    return bot


class BybitBot(Bot):
    def __init__(self, user: str, settings: dict):
        super().__init__(user, settings)
        self.cc = init_ccxt('bybit', user)
        self.binance_cc = init_ccxt('binance', 'example_user')
        self.max_leverage = 100

    async def _init(self):
        info = await self.cc.public_get_symbols()
        for e in info['result']:
            if e['name'] == self.symbol:
                break
        else:
            raise Exception('symbol missing')
        self.coin = e['base_currency']
        self.quot = e['quote_currency']
        self.price_step = float(e['price_filter']['tick_size'])
        self.qty_step = float(e['lot_size_filter']['qty_step'])
        self.min_qty = float(e['lot_size_filter']['min_trading_qty'])
        self.prdn = lambda n: round_dn(n, self.price_step)
        self.prup = lambda n: round_up(n, self.price_step)
        self.ardn = lambda n: round_dn(n, self.qty_step)
        self.arup = lambda n: round_up(n, self.qty_step)
        await self.update_position()
        await self.init_order_book()

    async def init_order_book(self):
        ticker = await self.cc.public_get_tickers(params={'symbol': self.symbol})
        self.ob = [float(ticker['result'][0]['bid_price']), float(ticker['result'][0]['ask_price'])]
        self.price = float(ticker['result'][0]['last_price'])

    def calc_entry_qty(self, equity_, pos_size_, pos_price_):
        return calc_entry_qty(self.qty_step,
                              self.min_qty,
                              self.ddown_factor,
                              self.leverage,
                              equity_,
                              pos_size_,
                              pos_price_)

    def calc_long_entry_price(self, equity_, pos_size_, pos_price_):
        return calc_long_entry_price(self.price_step,
                                     self.leverage,
                                     self.grid_spacing,
                                     self.grid_spacing_coefficient,
                                     equity_,
                                     pos_size_,
                                     pos_price_)

    def calc_shrt_entry_price(self, equity_, pos_size_, pos_price_):
        return calc_shrt_entry_price(self.price_step,
                                     self.leverage,
                                     self.grid_spacing,
                                     self.grid_spacing_coefficient,
                                     equity_,
                                     pos_size_,
                                     pos_price_)

    async def fetch_open_orders(self) -> [dict]:
        fetched = await self.cc.private_get_order(params={'symbol': self.symbol})
        return [
            {'order_id': e['order_id'],
             'symbol': e['symbol'],
             'price': float(e['price']),
             'qty': float(e['qty']),
             'side': e['side'].lower(),
             'timestamp': date_to_ts(e['created_at'])}
            for e in fetched['result']
        ]

    async def fetch_position(self) -> None:

        position, balance = await asyncio.gather(
            self.cc.private_get_position_list(params={'symbol': self.symbol}),
            self.cc.private_get_wallet_balance()
        )
        pos = position['result']
        result = {'size': pos['size'] * (-1 if pos['side'] == 'Sell' else 1),
                  'entry_price': float(pos['entry_price']),
                  'leverage': float(pos['leverage']),
                  'liquidation_price': float(pos['liq_price']),
                  'equity': balance['result'][self.coin]['equity']}
        return result

    async def execute_bid(self, qty: float, price: float) -> dict:
        o = await self.cc.private_post_order_create(
            params={'symbol': self.symbol, 'side': 'Buy', 'order_type': 'Limit',
                    'time_in_force': 'PostOnly', 'qty': qty, 'price': price}
        )
        return {'symbol': o['result']['symbol'],
                'side': 'buy',
                'type': 'limit',
                'qty': o['result']['qty'],
                'price': o['result']['price']}

    async def execute_ask(self, qty: float, price: float) -> dict:
        o = await self.cc.private_post_order_create(
            params={'symbol': self.symbol, 'side': 'Sell', 'order_type': 'Limit',
                    'time_in_force': 'PostOnly', 'qty': qty, 'price': price}
        )
        return {'symbol': o['result']['symbol'],
                'side': 'sell',
                'type': 'limit',
                'qty': o['result']['qty'],
                'price': o['result']['price']}

    async def execute_cancellation(self, id_: [dict]) -> [dict]:
        o = await self.cc.private_post_order_cancel(
            params={'symbol': self.symbol, 'order_id': id_}
        )
        return {'symbol': o['result']['symbol'], 'side': o['result']['side'].lower(),
                'qty': o['result']['qty'], 'price': o['result']['price']}

    async def fetch_trades(self, from_id: int = None):
        #### QUICK FIX
        #### bybit returns empty list when attempting to fetch btcusd trade history,
        #### works for other symbols.
        #### use binance BTCUSDT data instead until bybit works again
        ####
        #### update:
        #### BTCUSD works again
        #if self.symbol == 'BTCUSD':
        if False:
            return await fetch_trades_binance(self.binance_cc, self.symbol.replace('USD', 'USDT'),
                                              from_id)
        return await fetch_trades(self.cc, self.symbol, from_id)

    def calc_margin_cost(self, qty: float, price: float) -> float:
        return qty / price / self.max_leverage

    async def start_websocket(self) -> None:
        self.stop_websocket = False
        uri = f"wss://stream.bybit.com/realtime"
        print_([uri])
        await self.update_position()
        if self.position['leverage'] != self.leverage:
            try:
                print(await self.cc.user_post_leverage_save(
                    params={'symbol': self.symbol, 'leverage': 0}
                ))
            except Exception as e:
                print('error starting websocket', e)
        param = {'op': 'subscribe', 'args': ['trade.' + self.symbol]}
        k = 1
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps(param))
            async for msg in ws:
                if msg is None:
                    continue
                data = json.loads(msg)
                price_changed = False
                try:
                    for e in data['data']:
                        if e['price'] != self.price:
                            if e['side'] == 'Buy':
                                self.ob[1] = e['price']
                            elif e['side'] == 'Sell':
                                self.ob[0] = e['price']
                            self.price = e['price']
                            price_changed = True
                except Exception as e:
                    if 'success' not in data:
                        print('error in websocket streamed data', e)
                if price_changed:
                    if self.ts_locked['decide'] < self.ts_released['decide']:
                        asyncio.create_task(self.decide())
                    elif k % 10 == 0:
                        self.flush_stuck_locks()
                        k = 1
                    k += 1


async def main() -> None:
    bot = await create_bot(sys.argv[1], load_settings('bybit', sys.argv[1]))
    await start_bot(bot)


if __name__ == '__main__':
    asyncio.run(main())


