#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import asyncio
import time
import logging
from web3 import Web3
import ccxt.async_support as ccxt

# ==== КЛЮЧИ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ====
OKX_API_KEY = os.getenv('OKX_API_KEY')
OKX_SECRET = os.getenv('OKX_SECRET')
OKX_PASSPHRASE = os.getenv('OKX_PASSPHRASE')
ARB_RPC = os.getenv('ARB_RPC')
ARB_PRIVATE_KEY = os.getenv('ARB_PRIVATE_KEY')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# ==== НАСТРОЙКИ ====
TRADE_ETH = 0.02          # $50
MAX_SLIPPAGE = 0.25       # 0.25%
SAFETY_MARGIN = 0.10
MAX_DAILY_LOSS = 5.0
MAX_CONSECUTIVE_LOSSES = 5
MAX_RUNTIME_SECONDS = 600  # 10 минут работы

# Адреса контрактов (Arbitrum One)
UNISWAP_V3_ROUTER = "0xE592427A0AEce92De3Edee1F18E0157C05861564"
QUOTER = "0xb27308f9F90D607463bb33eA1BeBb41C27CE5AB6"
WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
USDC_ARB = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"

# ABI
QUOTER_ABI = [{
    "inputs": [
        {"internalType": "bytes", "name": "path", "type": "bytes"},
        {"internalType": "uint256", "name": "amountIn", "type": "uint256"}
    ],
    "name": "quoteExactInput",
    "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}],
    "stateMutability": "view",
    "type": "function"
}]

ROUTER_ABI = [{
    "inputs": [{
        "components": [
            {"internalType": "bytes", "name": "path", "type": "bytes"},
            {"internalType": "address", "name": "recipient", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"},
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint256", "name": "amountOutMinimum", "type": "uint256"}
        ],
        "internalType": "struct ISwapRouter.ExactInputParams",
        "name": "params",
        "type": "tuple"
    }],
    "name": "exactInput",
    "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}],
    "stateMutability": "payable",
    "type": "function"
}]

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

async def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
    except:
        pass

class FeeManager:
    def __init__(self, okx):
        self.okx = okx
        self.okx_taker_fee = 0.0010
        self.uniswap_fee = 0.0005
        self.last_update = 0
    async def update_fees(self):
        try:
            markets = await self.okx.load_markets()
            if 'ETH/USDC' in markets:
                self.okx_taker_fee = markets['ETH/USDC']['taker'] / 100.0
                if self.okx_taker_fee > 1:
                    self.okx_taker_fee /= 100.0
        except: pass
        self.last_update = time.time()
    def calc_min_spread(self, gas_usd, eth_price):
        total = (self.uniswap_fee + self.okx_taker_fee) * 100
        gas_pct = (gas_usd / (TRADE_ETH * eth_price)) * 100 if eth_price else 0
        return max(total + gas_pct + SAFETY_MARGIN, 0.20)

class AdaptiveThreshold:
    def __init__(self):
        self.multiplier = 1.0
        self.consecutive_losses = 0
        self.consecutive_wins = 0
        self.trades_today = 0
        self.last_adjust = time.time()
    def update(self, was_profitable):
        if was_profitable:
            self.consecutive_wins += 1
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            self.consecutive_wins = 0
        if (self.trades_today % 10 == 0 or time.time() - self.last_adjust > 3600) and self.trades_today > 5:
            if self.consecutive_losses > 3:
                self.multiplier = min(1.5, self.multiplier * 1.05)
                logger.info(f"Адаптация: повышаем порог до {self.multiplier:.2f}")
            elif self.consecutive_wins > 5:
                self.multiplier = max(0.7, self.multiplier * 0.98)
                logger.info(f"Адаптация: снижаем порог до {self.multiplier:.2f}")
            self.last_adjust = time.time()
    def get(self, base):
        return base * self.multiplier

class ArbitrumEngine:
    def __init__(self, rpc, priv_key):
        self.w3_pub = Web3(Web3.HTTPProvider(rpc))
        self.w3_priv = Web3(Web3.HTTPProvider(rpc))
        self.account = self.w3_priv.eth.account.from_key(priv_key)
        self.wallet = self.account.address
        self.router = self.w3_priv.eth.contract(address=UNISWAP_V3_ROUTER, abi=ROUTER_ABI)
        self.quoter = self.w3_pub.eth.contract(address=QUOTER, abi=QUOTER_ABI)
        self.okx = None
    def encode_path(self, tokens, fees):
        packed = b''
        for i in range(len(tokens)-1):
            packed += bytes.fromhex(tokens[i][2:])
            packed += fees[i].to_bytes(3, 'big')
        packed += bytes.fromhex(tokens[-1][2:])
        return packed
    async def get_dex_price(self, amount_eth):
        wei = self.w3_pub.to_wei(amount_eth, 'ether')
        path = self.encode_path([WETH, USDC_ARB], [500])
        res = self.quoter.functions.quoteExactInput(path, wei).call()
        usdc_out = res / 10**6
        return usdc_out / amount_eth, 0
    async def get_gas_price_usd(self):
        gas_price_gwei = self.w3_pub.eth.gas_price / 1e9
        eth_price = await self.get_cex_price()
        return gas_price_gwei * 400_000 / 1e9 * eth_price
    async def execute(self, amount_eth, dex_price):
        wei = self.w3_priv.to_wei(amount_eth, 'ether')
        expected_usdc = amount_eth * dex_price
        min_usdc = int(expected_usdc * (1 - MAX_SLIPPAGE/100) * 10**6)
        path = self.encode_path([WETH, USDC_ARB], [500])
        tx = self.router.functions.exactInput((path, self.wallet, int(time.time())+60, wei, min_usdc)).build_transaction({
            'from': self.wallet,
            'value': wei,
            'gas': 850000,
            'gasPrice': int(self.w3_priv.eth.gas_price * 1.05),
            'nonce': self.w3_priv.eth.get_transaction_count(self.wallet)
        })
        signed = self.w3_priv.eth.account.sign_transaction(tx, ARB_PRIVATE_KEY)
        tx_hash = self.w3_priv.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3_pub.eth.wait_for_transaction_receipt(tx_hash, timeout=45)
        if receipt['status'] != 1:
            raise Exception("Arb DEX revert")
        buy = await self.okx.create_market_buy_order('ETH/USDC', expected_usdc)
        profit_eth = buy['filled'] - amount_eth
        cex_price = await self.get_cex_price()
        return profit_eth * cex_price
    async def get_cex_price(self):
        ob = await self.okx.fetch_order_book('ETH/USDC', limit=5)
        return float(ob['bids'][0][0])

async def main():
    total_profit = 0.0
    trades_today = 0
    consecutive_losses = 0
    start_time = time.time()
    await send_telegram("🚀 Orion-X Pro (только Arbitrum) запущен на GitHub Actions")
    okx = ccxt.okx({
        'apiKey': OKX_API_KEY,
        'secret': OKX_SECRET,
        'password': OKX_PASSPHRASE,
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    })
    fee_mgr = FeeManager(okx)
    await fee_mgr.update_fees()
    arb = ArbitrumEngine(ARB_RPC, ARB_PRIVATE_KEY)
    arb.okx = okx
    adapt = AdaptiveThreshold()

    while True:
        try:
            if time.time() - start_time > MAX_RUNTIME_SECONDS:
                await send_telegram("⏱️ Лимит времени 10 мин, завершение")
                logger.info("Timeout reached, exiting")
                break

            if time.time() - fee_mgr.last_update > 3600:
                await fee_mgr.update_fees()
            if total_profit < -MAX_DAILY_LOSS:
                await send_telegram(f"⚠️ АВАРИЙНАЯ ОСТАНОВКА: убыток ${total_profit:.2f}")
                break
            if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                await send_telegram(f"⚠️ АВАРИЙНАЯ ОСТАНОВКА: {MAX_CONSECUTIVE_LOSSES} убыточных сделок")
                break

            cex = await arb.get_cex_price()
            dex, _ = await arb.get_dex_price(TRADE_ETH)
            if dex and cex and dex > cex:
                gas_usd = await arb.get_gas_price_usd()
                base = fee_mgr.calc_min_spread(gas_usd, cex)
                min_spread = adapt.get(base)
                cur_spread = (dex - cex) / cex * 100
                if cur_spread >= min_spread:
                    profit = await arb.execute(TRADE_ETH, dex)
                    total_profit += profit
                    trades_today += 1
                    adapt.trades_today = trades_today
                    adapt.update(profit > 0)
                    if profit > 0:
                        consecutive_losses = 0
                    else:
                        consecutive_losses += 1
                    msg = f"✅ ARB +${profit:.2f} | Всего ${total_profit:.2f} | сделок {trades_today}"
                    logger.info(msg)
                    await send_telegram(msg)
                    await asyncio.sleep(2)

            await asyncio.sleep(1.5)
        except Exception as e:
            err = f"⚠️ Ошибка: {e}"
            logger.error(err)
            await send_telegram(err)
            await asyncio.sleep(5)

    await okx.close()
    logger.info("Бот завершил работу")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
