#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import asyncio
import time
import base64
import logging
from web3 import Web3
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Processed
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import transfer as system_transfer
import aiohttp
import ccxt.async_support as ccxt

# ==== КЛЮЧИ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ (GitHub Secrets) ====
OKX_API_KEY = os.getenv('OKX_API_KEY')
OKX_SECRET = os.getenv('OKX_SECRET')
OKX_PASSPHRASE = os.getenv('OKX_PASSPHRASE')
ARB_RPC = os.getenv('ARB_RPC')
ARB_PRIVATE_KEY = os.getenv('ARB_PRIVATE_KEY')
SOL_PRIVATE_B58 = os.getenv('SOL_PRIVATE_B58')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# ==== НАСТРОЙКИ (менять здесь, если нужно) ====
TRADE_ETH = 0.02          # $50
TRADE_SOL = 0.25          # $50
MAX_SLIPPAGE = 0.25       # 0.25%
JITO_TIP_LAMPORTS = 5_000_000   # 0.005 SOL
SAFETY_MARGIN = 0.10      # запас 0.10%
MAX_DAILY_LOSS = 5.0      # аварийная остановка при убытке > $5 за день
MAX_CONSECUTIVE_LOSSES = 5  # остановка после 5 убыточных сделок подряд

# Адреса контрактов (Arbitrum One) - ИСПРАВЛЕНЫ
UNISWAP_V3_ROUTER = "0xE592427A0AEce92De3Edee1F18E0157C05861564"
QUOTER_V2 = "0xb27308f9F90D607463bb33eA1BeBb41C27CE5AB6"   # правильный checksum
WETH = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
USDC_ARB = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"

# Solana
WSOL = Pubkey.from_string("So11111111111111111111111111111111111111112")
USDC_SOL = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
JITO_ENGINE = "https://frankfurt.mainnet.block-engine.jito.wtf/api/v1"
SOL_RPC = "https://api.mainnet-beta.solana.com"

QUOTER_ABI = [{"inputs":[{"internalType":"bytes","name":"path","type":"bytes"},{"internalType":"uint256","name":"amountIn","type":"uint256"}],"name":"quoteExactInput","outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"},{"internalType":"uint160[]","name":"sqrtPriceX96AfterList","type":"uint160[]"},{"internalType":"uint32[]","name":"initializedTicksCrossedList","type":"uint32[]"},{"internalType":"uint256","name":"gasEstimate","type":"uint256"}],"stateMutability":"view","type":"function"}]
ROUTER_ABI = [{"inputs":[{"components":[{"internalType":"bytes","name":"path","type":"bytes"},{"internalType":"address","name":"recipient","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"},{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint256","name":"amountOutMinimum","type":"uint256"}],"internalType":"struct ISwapRouter.ExactInputParams","name":"params","type":"tuple"}],"name":"exactInput","outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"}],"stateMutability":"payable","type":"function"}]

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

async def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
    except Exception as e:
        logger.error(f"Telegram error: {e}")

class FeeManager:
    def __init__(self, okx):
        self.okx = okx
        self.okx_taker_fee = 0.0010
        self.uniswap_fee = 0.0005
        self.jupiter_fee = 0.0010
        self.last_update = 0
    async def update_fees(self):
        try:
            markets = await self.okx.load_markets()
            if 'ETH/USDC' in markets:
                self.okx_taker_fee = markets['ETH/USDC']['taker'] / 100.0
                if self.okx_taker_fee > 1:
                    self.okx_taker_fee /= 100.0
        except Exception as e:
            logger.warning(f"Fee update error: {e}")
        self.last_update = time.time()
    def calc_min_spread_arb(self, gas_usd, eth_price):
        total = (self.uniswap_fee + self.okx_taker_fee)*100
        gas_pct = (gas_usd/(TRADE_ETH*eth_price))*100 if eth_price else 0
        return max(total + gas_pct + SAFETY_MARGIN, 0.20)
    def calc_min_spread_sol(self, gas_usd, sol_price):
        total = (self.jupiter_fee + self.okx_taker_fee)*100
        jito_pct = (JITO_TIP_LAMPORTS/1e9*sol_price)/(TRADE_SOL*sol_price)*100 if sol_price else 0
        gas_pct = (gas_usd/(TRADE_SOL*sol_price))*100 if sol_price else 0
        return max(total + jito_pct + gas_pct + SAFETY_MARGIN, 0.20)

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
        self.quoter = self.w3_pub.eth.contract(address=QUOTER_V2, abi=QUOTER_ABI)
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
        usdc_out = res[0] / 10**6
        return usdc_out / amount_eth, res[3]
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
            'from': self.wallet, 'value': wei, 'gas': 850000,
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

class SolanaEngine:
    def __init__(self, keypair):
        self.client = AsyncClient(SOL_RPC, commitment=Processed)
        self.keypair = keypair
        self.okx = None
    async def get_jupiter_quote(self, amount_sol):
        async with aiohttp.ClientSession() as session:
            url = f"https://quote-api.jup.ag/v6/quote?inputMint={WSOL}&outputMint={USDC_SOL}&amount={int(amount_sol*1e9)}&slippageBps={MAX_SLIPPAGE*100}"
            async with session.get(url) as resp:
                data = await resp.json()
                price = float(data['outAmount']) / 1e6 / amount_sol
                return price, data
    async def send_jito_bundle(self, swap_tx_bytes):
        tip_acc = Pubkey.from_string("Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY")
        tip_ix = system_transfer(self.keypair.pubkey(), tip_acc, JITO_TIP_LAMPORTS)
        swap_txn = Transaction.deserialize(swap_tx_bytes)
        combined = list(swap_txn.instructions) + [tip_ix]
        blockhash = await self.client.get_latest_blockhash()
        new_txn = Transaction.new(combined, blockhash.value.blockhash, self.keypair)
        new_txn.sign(self.keypair)
        bundle = [base64.b64encode(new_txn.serialize()).decode()]
        async with aiohttp.ClientSession() as session:
            payload = {"jsonrpc":"2.0","id":1,"method":"sendBundle","params":[bundle,{"encoding":"base64"}]}
            async with session.post(JITO_ENGINE, json=payload) as resp:
                res = await resp.json()
                if 'error' in res:
                    raise Exception(f"Jito error: {res['error']}")
        return True
    async def execute(self, amount_sol, dex_price):
        fresh_price, quote = await self.get_jupiter_quote(amount_sol)
        if fresh_price < dex_price * 0.98:
            raise Exception("price changed")
        async with aiohttp.ClientSession() as session:
            payload = {"quoteResponse": quote, "userPublicKey": str(self.keypair.pubkey()), "wrapAndUnwrapSol": True}
            async with session.post("https://quote-api.jup.ag/v6/swap", json=payload) as resp:
                swap_data = await resp.json()
                tx_bytes = base64.b64decode(swap_data['swapTransaction'])
        await self.send_jito_bundle(tx_bytes)
        usdc_received = amount_sol * dex_price
        buy = await self.okx.create_market_buy_order('SOL/USDC', usdc_received)
        profit_sol = buy['filled'] - amount_sol
        cex_price = await self.get_cex_price()
        return profit_sol * cex_price
    async def get_cex_price(self):
        ob = await self.okx.fetch_order_book('SOL/USDC', limit=5)
        return float(ob['bids'][0][0])
    async def get_gas_price_usd(self):
        return ((5000 + JITO_TIP_LAMPORTS) / 1e9) * (await self.get_cex_price())

async def main():
    global total_profit, trades_today
    total_profit = 0.0
    trades_today = 0
    consecutive_losses = 0
    await send_telegram("🚀 Orion-X Pro запущен на GitHub Actions (автопилот)")
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
    sol = SolanaEngine(Keypair.from_base58_string(SOL_PRIVATE_B58))
    sol.okx = okx
    adapt_arb = AdaptiveThreshold()
    adapt_sol = AdaptiveThreshold()

    while True:
        try:
            if time.time() - fee_mgr.last_update > 3600:
                await fee_mgr.update_fees()
            # === Аварийная остановка по дневному убытку ===
            if total_profit < -MAX_DAILY_LOSS:
                await send_telegram(f"⚠️ АВАРИЙНАЯ ОСТАНОВКА: дневной убыток превысил ${MAX_DAILY_LOSS}. Profit: ${total_profit:.2f}")
                break
            # === Аварийная остановка по череде убытков ===
            if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                await send_telegram(f"⚠️ АВАРИЙНАЯ ОСТАНОВКА: {MAX_CONSECUTIVE_LOSSES} убыточных сделок подряд")
                break

            # ----- ARBITRUM -----
            cex_eth = await arb.get_cex_price()
            dex_eth, _ = await arb.get_dex_price(TRADE_ETH)
            if dex_eth and cex_eth and dex_eth > cex_eth:
                gas_usd = await arb.get_gas_price_usd()
                base = fee_mgr.calc_min_spread_arb(gas_usd, cex_eth)
                min_spread = adapt_arb.get(base)
                cur_spread = (dex_eth - cex_eth)/cex_eth*100
                if cur_spread >= min_spread:
                    profit = await arb.execute(TRADE_ETH, dex_eth)
                    total_profit += profit
                    trades_today += 1
                    adapt_arb.trades_today = trades_today
                    adapt_arb.update(profit > 0)
                    if profit > 0:
                        consecutive_losses = 0
                    else:
                        consecutive_losses += 1
                    msg = f"✅ ARB +${profit:.2f} | Всего ${total_profit:.2f} | сделок {trades_today}"
                    logger.info(msg)
                    await send_telegram(msg)
                    await asyncio.sleep(2)

            # ----- SOLANA -----
            cex_sol = await sol.get_cex_price()
            dex_sol, _ = await sol.get_jupiter_quote(TRADE_SOL)
            if dex_sol and cex_sol and dex_sol > cex_sol:
                gas_usd = await sol.get_gas_price_usd()
                base = fee_mgr.calc_min_spread_sol(gas_usd, cex_sol)
                min_spread = adapt_sol.get(base)
                cur_spread = (dex_sol - cex_sol)/cex_sol*100
                if cur_spread >= min_spread:
                    profit = await sol.execute(TRADE_SOL, dex_sol)
                    total_profit += profit
                    trades_today += 1
                    adapt_sol.trades_today = trades_today
                    adapt_sol.update(profit > 0)
                    if profit > 0:
                        consecutive_losses = 0
                    else:
                        consecutive_losses += 1
                    msg = f"✅ SOL +${profit:.2f} | Всего ${total_profit:.2f} | сделок {trades_today}"
                    logger.info(msg)
                    await send_telegram(msg)
                    await asyncio.sleep(2)

            await asyncio.sleep(1.5)
        except Exception as e:
            err = f"⚠️ Ошибка: {e}"
            logger.error(err)
            await send_telegram(err)
            await asyncio.sleep(5)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
        asyncio.run(send_telegram("⏹️ Бот остановлен"))
