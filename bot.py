"""
CryptoPump Discord Bot  —  WebSocket edition
Real-time price alerts via Binance !miniTicker stream.
Latency: ~100ms–500ms vs the old 60-second polling delay.
"""
import discord
from discord.ext import commands, tasks
import aiohttp
from aiohttp import web
import asyncio
import json
import os
import time
from datetime import datetime, timezone
from collections import defaultdict

from config import (
    DISCORD_TOKEN, SECTORS,
    PUMP_THRESHOLD, VOLUME_SPIKE_MULTIPLIER,
    COOLDOWN_MINUTES, MIN_VOLUME_USDT,
    CUMULATIVE_WINDOW_MINUTES, CUMULATIVE_THRESHOLD,
    CONTAGION_THRESHOLD, CONTAGION_WINDOW_MINUTES, CONTAGION_COOLDOWN_MINUTES,
)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ── state ─────────────────────────────────────────────────────────────────────
price_cache           = {}               # symbol -> last price
volume_cache          = {}               # symbol -> last 24h quoteVolume
alert_cooldown        = {}               # symbol -> float ts of last alert
price_history         = defaultdict(list)# symbol -> [(ts, price)]
sector_map            = {}               # symbol -> sector name
alert_channels        = {}               # channel_id -> [sectors]
sector_pump_log       = defaultdict(list)# sector -> [(ts, symbol, pct)]
contagion_sent        = {}               # symbol -> ts
sector_contagion_sent = {}               # sector -> ts
ticker_snapshot       = {}               # symbol -> latest full ticker dict (for contagion)

BLACKLIST = {"BTTCUSDT", "LUNCUSDT"}

# Binance WebSocket URL — streams ALL mini-tickers in one connection
WS_URL      = "wss://stream.binance.com:9443/ws/!miniTicker@arr"
# Fallback REST for full 24h data (used for contagion suggestions and commands)
BINANCE_24H = "https://api.binance.com/api/v3/ticker/24hr"

# Alert queue: websocket task -> discord sender task
alert_queue: asyncio.Queue = None

# ── helpers ───────────────────────────────────────────────────────────────────

def get_sector_symbols(sector: str) -> list[str]:
    return [sym for sym, sec in sector_map.items() if sec == sector]

def build_sector_map(all_symbols: set) -> dict:
    result = {}
    for sector, coins in SECTORS.items():
        for coin in coins:
            sym = f"{coin}USDT"
            if sym in all_symbols:
                result[sym] = sector
    for sym in all_symbols:
        if sym not in result:
            result[sym] = "other"
    return result

def is_on_cooldown(symbol: str) -> bool:
    last = alert_cooldown.get(symbol)
    return last is not None and (time.time() - last) / 60 < COOLDOWN_MINUTES

def set_cooldown(symbol: str):
    alert_cooldown[symbol] = time.time()

def get_cumulative_change(symbol: str, current_price: float) -> float | None:
    now_ts = time.time()
    cutoff = now_ts - (CUMULATIVE_WINDOW_MINUTES * 60)
    price_history[symbol] = [(t, p) for t, p in price_history[symbol] if t >= cutoff]
    history = price_history[symbol]
    if len(history) < 2:
        return None
    oldest = history[0][1]
    return ((current_price - oldest) / oldest) * 100 if oldest else None

async def fetch_all_tickers() -> list[dict]:
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H) as r:
            data = await r.json()
    return [t for t in data if t["symbol"].endswith("USDT")]

# ── contagion ─────────────────────────────────────────────────────────────────

def record_sector_pump(sector: str, symbol: str, change_pct: float):
    now_ts = time.time()
    sector_pump_log[sector].append((now_ts, symbol, change_pct))
    cutoff = now_ts - (CONTAGION_WINDOW_MINUTES * 60)
    sector_pump_log[sector] = [e for e in sector_pump_log[sector] if e[0] >= cutoff]

def get_contagion_suggestions(pumped_symbol: str, sector: str) -> list[dict] | None:
    if sector == "other":
        return None
    sector_syms = [s for s in get_sector_symbols(sector) if s != pumped_symbol]
    if len(sector_syms) < 2:
        return None

    now_ts  = time.time()
    cutoff  = now_ts - (CONTAGION_WINDOW_MINUTES * 60)
    already_pumped = {s for t, s, c in sector_pump_log[sector] if t >= cutoff and s != pumped_symbol}

    candidates = []
    for sym in sector_syms:
        snap = ticker_snapshot.get(sym)
        if not snap:
            continue
        price     = snap["price"]
        chg24     = snap.get("chg24", 0.0)
        per_min   = snap.get("per_min_vol", 0.0)

        if per_min < MIN_VOLUME_USDT:
            continue
        if sym in already_pumped:
            continue
        last_cong = contagion_sent.get(sym)
        if last_cong and (now_ts - last_cong) < (CONTAGION_WINDOW_MINUTES * 60):
            continue

        lag_score    = -chg24
        volume_score = min(per_min / 1000, 10)
        candidates.append({
            "symbol": sym, "coin": sym.replace("USDT", ""),
            "price": price, "chg24": chg24,
            "vol_per_min": per_min,
            "score": lag_score + volume_score,
        })

    if not candidates:
        return None
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:5]

# ── embed builders ────────────────────────────────────────────────────────────

def pump_embed(symbol, old_price, new_price, change_pct,
               sector, chg24, per_min_vol, vol_spike, trigger, elapsed_ms) -> discord.Embed:
    is_pump   = change_pct > 0
    color     = 0x00FF88 if is_pump else 0xFF4444
    direction = "🚀 PUMP" if is_pump else "📉 DUMP"
    coin      = symbol.replace("USDT", "")

    embed = discord.Embed(
        title=f"{direction} — {symbol}",
        description=f"**Sector:** `{sector.upper()}`  |  **Trigger:** `{trigger}`",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="💰 Price",    value=f"`${new_price:.6f}`",           inline=True)
    embed.add_field(name="📊 Move",     value=f"`{change_pct:+.2f}%`",         inline=True)
    embed.add_field(name="📈 24h Δ",    value=f"`{chg24:+.2f}%`",              inline=True)
    embed.add_field(name="💵 Vol/min",  value=f"`${per_min_vol:,.0f}`",         inline=True)
    if vol_spike:
        embed.add_field(name="⚡ Spike", value=f"`{vol_spike:.1f}x`",          inline=True)
    embed.add_field(name="⏱ Latency",  value=f"`{elapsed_ms:.0f}ms`",         inline=True)
    embed.add_field(name="🔗 Trade",
        value=f"[Binance](https://www.binance.com/en/trade/{coin}_USDT)",       inline=True)
    embed.set_footer(text="CryptoPump Bot • Binance WebSocket • Real-time")
    return embed

def contagion_embed(pumped_symbol, pumped_pct, sector, suggestions, n_recent) -> discord.Embed:
    coin = pumped_symbol.replace("USDT", "")
    embed = discord.Embed(
        title=f"🔥 SECTOR CONTAGION — {sector.upper()}",
        description=(
            f"**{coin}** pumped **{pumped_pct:+.2f}%**\n"
            f"`{n_recent}` other coin(s) already moved in this sector.\n\n"
            f"⚡ **These coins could follow — watch closely:**"
        ),
        color=0xFFAA00,
        timestamp=datetime.now(timezone.utc),
    )
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    for i, s in enumerate(suggestions):
        c  = s["coin"]
        ps = f"${s['price']:.6f}" if s["price"] < 1 else f"${s['price']:.4f}"
        embed.add_field(
            name=f"{medals[i]}  {c}",
            value=(
                f"Price: `{ps}`\n"
                f"24h: `{s['chg24']:+.2f}%`\n"
                f"Vol: `${s['vol_per_min']:,.0f}/min`\n"
                f"[Trade](https://www.binance.com/en/trade/{c}_USDT)"
            ),
            inline=True,
        )
    embed.set_footer(text="CryptoPump Bot • Contagion Alert • DYOR — not financial advice")
    return embed

# ── process a single ticker update ───────────────────────────────────────────

def process_ticker(t: dict) -> list:
    """
    Called for every real-time ticker update from WebSocket.
    Returns list of (embed_type, embed, sector, symbol, extra) tuples to send.
    """
    symbol    = t.get("s", "")
    new_price = float(t.get("c", 0))   # close / last price
    chg24_pct = float(t.get("P", 0))   # 24h price change %
    vol_quote = float(t.get("q", 0))   # 24h quote asset volume
    receive_ts = t.get("_recv_ts", time.time())

    if not symbol.endswith("USDT"):
        return []
    if symbol in BLACKLIST:
        return []
    if new_price < 0.000001:
        return []

    per_min_vol = vol_quote / (24 * 60)
    if per_min_vol < MIN_VOLUME_USDT:
        price_cache[symbol] = new_price
        return []

    # Update snapshot for contagion lookups
    ticker_snapshot[symbol] = {
        "price": new_price,
        "chg24": chg24_pct,
        "per_min_vol": per_min_vol,
    }

    # Price history
    now_ts = time.time()
    price_history[symbol].append((now_ts, new_price))
    price_history[symbol] = [
        (t2, p) for t2, p in price_history[symbol] if t2 >= now_ts - 7200
    ]

    old_price = price_cache.get(symbol)
    price_cache[symbol] = new_price

    # Volume spike
    old_vol = volume_cache.get(symbol)
    volume_cache[symbol] = vol_quote
    vol_spike = None
    if old_vol and old_vol > 0 and vol_quote > old_vol:
        ratio = vol_quote / old_vol
        if ratio >= VOLUME_SPIKE_MULTIPLIER:
            vol_spike = ratio

    if old_price is None:
        return []   # no baseline yet

    change_pct = ((new_price - old_price) / old_price) * 100
    cumulative  = get_cumulative_change(symbol, new_price)

    # Trigger
    trigger = None
    if abs(change_pct) >= PUMP_THRESHOLD:
        trigger = f"{'pump' if change_pct > 0 else 'dump'} {change_pct:+.2f}%"
    elif vol_spike:
        trigger = f"vol spike {vol_spike:.1f}x"
    elif cumulative is not None and abs(cumulative) >= CUMULATIVE_THRESHOLD:
        trigger = f"cumul. {cumulative:+.2f}% / {CUMULATIVE_WINDOW_MINUTES}min"

    if trigger is None:
        return []
    if is_on_cooldown(symbol):
        return []

    sector     = sector_map.get(symbol, "other")
    elapsed_ms = (time.time() - receive_ts) * 1000
    results    = []

    # Pump/dump alert
    embed = pump_embed(symbol, old_price, new_price, change_pct,
                       sector, chg24_pct, per_min_vol, vol_spike, trigger, elapsed_ms)
    results.append(("pump", embed, sector, symbol, change_pct))

    # Contagion
    if change_pct > 0 and sector != "other":
        record_sector_pump(sector, symbol, change_pct)

    effective_move = max(abs(change_pct), abs(cumulative) if cumulative else 0)
    if effective_move >= CONTAGION_THRESHOLD and sector != "other" and change_pct > 0:
        last_cong = sector_contagion_sent.get(sector, 0)
        if time.time() - last_cong >= CONTAGION_COOLDOWN_MINUTES * 60:
            n_recent    = len([1 for t2, s, c in sector_pump_log[sector] if s != symbol])
            suggestions = get_contagion_suggestions(symbol, sector)
            if suggestions:
                c_embed = contagion_embed(symbol, change_pct, sector, suggestions, n_recent)
                results.append(("contagion", c_embed, sector, symbol, suggestions))
                sector_contagion_sent[sector] = time.time()
                now_ts2 = time.time()
                for sg in suggestions:
                    contagion_sent[sg["symbol"]] = now_ts2

    return results

# ── WebSocket listener ────────────────────────────────────────────────────────

async def ws_listener():
    """
    Connects to Binance !miniTicker@arr WebSocket.
    Pushes all ticker updates (every ~1s) into alert_queue.
    Auto-reconnects on disconnect.
    """
    global sector_map

    # Build sector map on first connect via REST
    if not sector_map:
        print("📡 Fetching initial ticker list to build sector map...")
        try:
            tickers    = await fetch_all_tickers()
            all_syms   = {t["symbol"] for t in tickers}
            sector_map = build_sector_map(all_syms)
            # Seed ticker_snapshot from REST data
            for t in tickers:
                sym = t["symbol"]
                if sym.endswith("USDT") and sym not in BLACKLIST:
                    vol_quote = float(t["quoteVolume"])
                    ticker_snapshot[sym] = {
                        "price":       float(t["lastPrice"]),
                        "chg24":       float(t["priceChangePercent"]),
                        "per_min_vol": vol_quote / (24 * 60),
                    }
                    price_cache[sym]  = float(t["lastPrice"])
                    volume_cache[sym] = vol_quote
            print(f"✅ Sector map: {len(sector_map)} symbols")
        except Exception as e:
            print(f"❌ Failed to build sector map: {e}")

    retry_delay = 5
    while True:
        try:
            print(f"🔌 Connecting to Binance WebSocket...")
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    WS_URL,
                    heartbeat=30,
                    receive_timeout=60,
                ) as ws:
                    print("✅ WebSocket connected — real-time stream active")
                    retry_delay = 5  # reset on successful connect
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            recv_ts = time.time()
                            tickers = json.loads(msg.data)
                            if isinstance(tickers, list):
                                for t in tickers:
                                    t["_recv_ts"] = recv_ts
                                    results = process_ticker(t)
                                    for r in results:
                                        await alert_queue.put(r)
                        elif msg.type in (aiohttp.WSMsgType.ERROR,
                                          aiohttp.WSMsgType.CLOSED):
                            print(f"⚠️ WebSocket closed: {msg}")
                            break
        except Exception as e:
            print(f"❌ WebSocket error: {e}  — reconnecting in {retry_delay}s")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)

# ── Alert sender (reads from queue, sends to Discord) ─────────────────────────

async def alert_sender():
    """
    Reads alerts from alert_queue and dispatches them to Discord channels.
    Runs as a separate task so WebSocket is never blocked by Discord I/O.
    """
    while True:
        try:
            item = await alert_queue.get()
            kind, embed, sector, symbol, extra = item

            for channel_id, watched in list(alert_channels.items()):
                channel   = bot.get_channel(channel_id)
                if channel is None:
                    continue
                watch_all = "all" in watched
                if not (watch_all or sector in watched or "other" in watched):
                    continue
                try:
                    await channel.send(embed=embed)
                    if kind == "pump":
                        set_cooldown(symbol)
                        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                              f"{'🚀' if extra > 0 else '📉'} {symbol} {extra:+.2f}% → #{channel.name}")
                    else:
                        print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                              f"🔥 Contagion {symbol} → #{channel.name}")
                except Exception as e:
                    print(f"Send error: {e}")
            alert_queue.task_done()
        except Exception as e:
            print(f"Alert sender error: {e}")
            await asyncio.sleep(1)

# ── Periodic 24h snapshot refresh (keeps chg24 + volume accurate) ────────────

@tasks.loop(minutes=5)
async def refresh_snapshots():
    """Refresh 24h stats from REST every 5 minutes to keep contagion data fresh."""
    try:
        tickers = await fetch_all_tickers()
        for t in tickers:
            sym = t["symbol"]
            if sym.endswith("USDT") and sym not in BLACKLIST:
                vol_quote = float(t["quoteVolume"])
                if sym in ticker_snapshot:
                    ticker_snapshot[sym]["chg24"]       = float(t["priceChangePercent"])
                    ticker_snapshot[sym]["per_min_vol"] = vol_quote / (24 * 60)
                volume_cache[sym] = vol_quote
    except Exception as e:
        print(f"Snapshot refresh error: {e}")

@refresh_snapshots.before_loop
async def before_refresh():
    await bot.wait_until_ready()

# ── commands ──────────────────────────────────────────────────────────────────

@bot.command(name="watch")
async def watch(ctx, *sectors):
    valid_sectors = list(SECTORS.keys()) + ["all", "other"]
    if not sectors:
        await ctx.send(
            f"❌ Provide sector(s) or `all`.\n"
            f"Named sectors: `{', '.join(SECTORS.keys())}`"
        )
        return
    chosen  = [s.lower() for s in sectors]
    invalid = [s for s in chosen if s not in valid_sectors]
    valid   = [s for s in chosen if s in valid_sectors]
    if invalid:
        await ctx.send(f"⚠️ Unknown: `{', '.join(invalid)}`")
    if not valid:
        return
    cid = ctx.channel.id
    existing = set(alert_channels.get(cid, []))
    alert_channels[cid] = list(existing | set(valid))
    _save_channels()
    embed = discord.Embed(
        title="✅ Now Watching",
        description="\n".join(f"• `{s.upper()}`" for s in valid),
        color=0x00AAFF,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Stream",             value="`Binance WebSocket (real-time)`", inline=False)
    embed.add_field(name="Price threshold",    value=f"`{PUMP_THRESHOLD}%`")
    embed.add_field(name="Cumulative",         value=f"`{CUMULATIVE_THRESHOLD}%/{CUMULATIVE_WINDOW_MINUTES}min`")
    embed.add_field(name="Contagion trigger",  value=f"`{CONTAGION_THRESHOLD}%`")
    embed.add_field(name="Cooldown",           value=f"`{COOLDOWN_MINUTES} min`")
    embed.set_footer(text="CryptoPump Bot • Binance WebSocket")
    await ctx.send(embed=embed)

@bot.command(name="unwatch")
async def unwatch(ctx, *sectors):
    cid = ctx.channel.id
    if not alert_channels.get(cid):
        await ctx.send("ℹ️ No active watches in this channel.")
        return
    if not sectors or "all" in [s.lower() for s in sectors]:
        alert_channels.pop(cid, None)
        _save_channels()
        await ctx.send("🗑️ Cleared all watches for this channel.")
        return
    to_remove = [s.lower() for s in sectors]
    alert_channels[cid] = [s for s in alert_channels[cid] if s not in to_remove]
    if not alert_channels[cid]:
        alert_channels.pop(cid)
    _save_channels()
    await ctx.send(f"🗑️ Removed: `{', '.join(to_remove)}`")

@bot.command(name="status")
async def status(ctx):
    cid     = ctx.channel.id
    watched = alert_channels.get(cid, [])
    ws_live = len(ticker_snapshot) > 0
    embed   = discord.Embed(
        title="📡 Watch Status",
        color=0x00FF88 if ws_live else 0xFF4444,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="WebSocket",
        value=f"{'🟢 Live' if ws_live else '🔴 Connecting...'} — `{len(ticker_snapshot):,}` symbols tracked",
        inline=False,
    )
    if watched:
        embed.add_field(name="Watching",
                        value="\n".join(f"• `{s.upper()}`" for s in watched), inline=False)
    else:
        embed.add_field(name="Watching", value="Nothing — use `!watch all` to start", inline=False)
    embed.add_field(name="Price threshold",   value=f"`{PUMP_THRESHOLD}%`",                              inline=True)
    embed.add_field(name="Cumulative",        value=f"`{CUMULATIVE_THRESHOLD}%/{CUMULATIVE_WINDOW_MINUTES}min`", inline=True)
    embed.add_field(name="Contagion trigger", value=f"`{CONTAGION_THRESHOLD}%`",                         inline=True)
    embed.add_field(name="Cooldown",          value=f"`{COOLDOWN_MINUTES} min`",                         inline=True)
    embed.set_footer(text="CryptoPump Bot • Binance WebSocket • Real-time")
    await ctx.send(embed=embed)

@bot.command(name="sectors")
async def list_sectors(ctx):
    embed = discord.Embed(title="📂 Sectors", color=0x9B59B6, timestamp=datetime.now(timezone.utc))
    for sector, coins in SECTORS.items():
        embed.add_field(
            name=f"`{sector.upper()}`",
            value=", ".join(f"`{c}`" for c in coins[:12]) + ("…" if len(coins) > 12 else ""),
            inline=False,
        )
    embed.set_footer(text="!watch all  →  monitors every Binance USDT pair in real-time")
    await ctx.send(embed=embed)

@bot.command(name="price")
async def price_cmd(ctx, coin: str):
    symbol = coin.upper() + "USDT"
    snap   = ticker_snapshot.get(symbol)
    if snap:
        embed = discord.Embed(title=f"💱 {symbol}", color=0x00FF88, timestamp=datetime.now(timezone.utc))
        ps    = f"${snap['price']:.6f}" if snap["price"] < 1 else f"${snap['price']:.4f}"
        embed.add_field(name="Price",   value=f"`{ps}`",                        inline=True)
        embed.add_field(name="24h Δ",   value=f"`{snap['chg24']:+.2f}%`",       inline=True)
        embed.add_field(name="Vol/min", value=f"`${snap['per_min_vol']:,.0f}`",  inline=True)
        embed.set_footer(text="CryptoPump Bot • from live WebSocket cache")
        await ctx.send(embed=embed)
    else:
        # Fallback to REST
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(BINANCE_24H, params={"symbol": symbol}) as r:
                    data = await r.json()
            if "code" in data:
                await ctx.send(f"❌ `{symbol}` not found on Binance.")
                return
            embed = discord.Embed(title=f"💱 {symbol}", color=0x00FF88, timestamp=datetime.now(timezone.utc))
            embed.add_field(name="Price",    value=f"`${float(data['lastPrice']):.6f}`",           inline=True)
            embed.add_field(name="24h Δ",    value=f"`{float(data['priceChangePercent']):+.2f}%`", inline=True)
            embed.add_field(name="24h Vol",  value=f"`${float(data['quoteVolume']):,.0f}`",        inline=True)
            await ctx.send(embed=embed)
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")

@bot.command(name="top")
async def top_movers(ctx, hours: int = 8):
    await ctx.send(f"⏳ Scanning `{len(ticker_snapshot):,}` live symbols...")
    results = []
    for sym, snap in ticker_snapshot.items():
        vol = snap["per_min_vol"] * 24 * 60
        if vol < MIN_VOLUME_USDT * 60 * 24:
            continue
        chg24  = snap["chg24"]
        approx = chg24 * (hours / 24)
        sector = sector_map.get(sym, "other")
        results.append((abs(approx), approx, sym, sector, snap["price"], chg24))
    results.sort(reverse=True)
    top    = results[:10]
    bottom = sorted(results, key=lambda x: x[1])[:5]
    lines  = ["```",
              f"🚀 TOP PUMPS — Last ~{hours}h",
              f"{'COIN':<10} {'SECTOR':<10} {'PRICE':>10} {f'~{hours}h':>8} {'24h':>8}",
              "─" * 52]
    for _, chg_h, sym, sec, price, chg24 in top:
        coin = sym.replace("USDT", "")
        ps   = f"${price:.4f}" if price < 1 else f"${price:.2f}"
        lines.append(f"{coin:<10} {sec:<10} {ps:>10} {chg_h:>+7.2f}% {chg24:>+7.2f}%")
    lines += ["", f"📉 TOP DUMPS — Last ~{hours}h",
              f"{'COIN':<10} {'SECTOR':<10} {'PRICE':>10} {f'~{hours}h':>8} {'24h':>8}",
              "─" * 52]
    for _, chg_h, sym, sec, price, chg24 in bottom:
        coin = sym.replace("USDT", "")
        ps   = f"${price:.4f}" if price < 1 else f"${price:.2f}"
        lines.append(f"{coin:<10} {sec:<10} {ps:>10} {chg_h:>+7.2f}% {chg24:>+7.2f}%")
    lines.append("```")
    embed = discord.Embed(
        title="📊 Top Movers — All Binance USDT Pairs",
        description="\n".join(lines),
        color=0xFFAA00,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text="CryptoPump Bot • from live WebSocket cache")
    await ctx.send(embed=embed)

@bot.command(name="summary")
async def summary(ctx, hours: int = 8):
    await ctx.send(f"⏳ Building sector snapshot...")
    for sector, coins in SECTORS.items():
        rows = []
        for coin in coins:
            sym  = f"{coin}USDT"
            snap = ticker_snapshot.get(sym)
            if not snap:
                continue
            price  = snap["price"]
            chg24  = snap["chg24"]
            approx = chg24 * (hours / 24)
            rows.append((abs(approx), coin, price, approx, chg24))
        if not rows:
            continue
        rows.sort(reverse=True)
        lines = ["```",
                 f"{'COIN':<8} {'PRICE':>12} {f'~{hours}h':>8} {'24h':>8}",
                 f"{'─'*8} {'─'*12} {'─'*8} {'─'*8}"]
        for _, coin, price, approx, chg24 in rows:
            ps = f"${price:.4f}" if price < 1 else f"${price:.2f}"
            lines.append(f"{coin:<8} {ps:>12} {approx:>+7.2f}% {chg24:>+7.2f}%")
        lines.append("```")
        embed = discord.Embed(
            title=f"{sector.upper()} — Last ~{hours}h",
            description="\n".join(lines),
            color=0x00AAFF,
            timestamp=datetime.now(timezone.utc),
        )
        await ctx.send(embed=embed)
        await asyncio.sleep(0.4)

@bot.command(name="watchlist")
async def watchlist(ctx, sector: str = None):
    if not sector:
        await ctx.send("Usage: `!watchlist <sector>`")
        return
    sector = sector.lower()
    if sector not in SECTORS:
        await ctx.send(f"❌ Unknown sector `{sector}`")
        return
    candidates = []
    for coin in SECTORS[sector]:
        sym  = f"{coin}USDT"
        snap = ticker_snapshot.get(sym)
        if not snap or snap["per_min_vol"] < MIN_VOLUME_USDT:
            continue
        lag_score    = -snap["chg24"]
        volume_score = min(snap["per_min_vol"] / 1000, 10)
        candidates.append({
            "symbol": sym, "coin": coin,
            "price": snap["price"], "chg24": snap["chg24"],
            "vol_per_min": snap["per_min_vol"],
            "score": lag_score + volume_score,
        })
    candidates.sort(key=lambda x: x["score"], reverse=True)
    top   = candidates[:5]
    embed = discord.Embed(
        title=f"👀 {sector.upper()} — Coins to Watch",
        description="Ranked by lag + volume. Most likely to pump next if sector heats up.",
        color=0x9B59B6,
        timestamp=datetime.now(timezone.utc),
    )
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣"]
    for i, s in enumerate(top):
        ps = f"${s['price']:.6f}" if s["price"] < 1 else f"${s['price']:.4f}"
        embed.add_field(
            name=f"{medals[i]}  {s['coin']}",
            value=(
                f"Price: `{ps}`\n"
                f"24h: `{s['chg24']:+.2f}%`\n"
                f"Vol: `${s['vol_per_min']:,.0f}/min`\n"
                f"[Trade](https://www.binance.com/en/trade/{s['coin']}_USDT)"
            ),
            inline=True,
        )
    embed.set_footer(text="DYOR — not financial advice")
    await ctx.send(embed=embed)

@bot.command(name="addcoin")
@commands.has_permissions(administrator=True)
async def add_coin(ctx, sector: str, *coins):
    sector = sector.lower()
    if sector not in SECTORS:
        await ctx.send(f"❌ Unknown sector `{sector}`")
        return
    added = []
    for coin in coins:
        coin = coin.upper()
        if coin not in SECTORS[sector]:
            SECTORS[sector].append(coin)
            added.append(coin)
    sector_map.clear()
    _save_sectors()
    await ctx.send(f"✅ Added `{', '.join(added)}` to `{sector.upper()}`")

# ── persistence ───────────────────────────────────────────────────────────────
def _save_channels():
    with open("alert_channels.json", "w") as f:
        json.dump({str(k): v for k, v in alert_channels.items()}, f)

def _load_channels():
    if os.path.exists("alert_channels.json"):
        with open("alert_channels.json") as f:
            data = json.load(f)
        for k, v in data.items():
            alert_channels[int(k)] = v
        print(f"Loaded {len(alert_channels)} channel(s).")

def _save_sectors():
    with open("sectors_custom.json", "w") as f:
        json.dump(SECTORS, f, indent=2)

# ── keep-alive web server ─────────────────────────────────────────────────────
async def health_handler(request):
    syms = len(ticker_snapshot)
    return web.Response(
        text=f"CryptoPump Bot alive | WS symbols: {syms} | channels: {len(alert_channels)}",
        status=200,
    )

async def run_web_server():
    port   = int(os.getenv("PORT", 8080))
    app    = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"🌐 Keep-alive server on port {port}")

# ── startup ───────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    global alert_queue
    alert_queue = asyncio.Queue(maxsize=1000)
    _load_channels()
    refresh_snapshots.start()
    # Launch WebSocket listener and alert sender as background tasks
    asyncio.create_task(ws_listener())
    asyncio.create_task(alert_sender())
    print(f"✅ {bot.user} ready — WebSocket stream starting...")
    print(f"   Threshold={PUMP_THRESHOLD}% | Cumul={CUMULATIVE_THRESHOLD}%/{CUMULATIVE_WINDOW_MINUTES}min")
    print(f"   Contagion={CONTAGION_THRESHOLD}% | Cooldown={COOLDOWN_MINUTES}min")

async def main():
    await run_web_server()
    await bot.start(DISCORD_TOKEN)

asyncio.run(main())