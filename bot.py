import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
import json
import os
from datetime import datetime
from config import DISCORD_TOKEN, SECTORS, CHECK_INTERVAL, PUMP_THRESHOLD

intents = discord.Intents.default()
intents.message_content = True   # requires "Message Content Intent" in Dev Portal → Bot tab

bot = commands.Bot(command_prefix="!", intents=intents)

# Store: { symbol: last_price }
price_cache = {}
# Store: { channel_id: [sector1, sector2] }
alert_channels = {}
# Store: binance symbols per sector
sector_symbols = {}

BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
BINANCE_24H_URL    = "https://api.binance.com/api/v3/ticker/24hr"

# ── helpers ──────────────────────────────────────────────────────────────────

async def fetch_binance_prices():
    """Return {symbol: float_price} for all USDT pairs."""
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_TICKER_URL) as r:
            data = await r.json()
    return {
        item["symbol"]: float(item["price"])
        for item in data
        if item["symbol"].endswith("USDT")
    }

async def fetch_24h_change(symbol: str):
    """Return 24-h price-change % for one symbol."""
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H_URL, params={"symbol": symbol}) as r:
            data = await r.json()
    return float(data.get("priceChangePercent", 0))

def build_sector_map(all_symbols: list[str]) -> dict[str, list[str]]:
    """Map each sector to the Binance USDT symbols that belong to it."""
    result = {}
    for sector, coins in SECTORS.items():
        matched = [
            f"{coin}USDT"
            for coin in coins
            if f"{coin}USDT" in all_symbols
        ]
        result[sector] = matched
    return result

def pump_embed(symbol: str, old_price: float, new_price: float,
               change_pct: float, sector: str, change_24h: float) -> discord.Embed:
    direction = "🚀 PUMP" if change_pct > 0 else "📉 DUMP"
    color     = 0x00FF88 if change_pct > 0 else 0xFF4444

    embed = discord.Embed(
        title=f"{direction} DETECTED — {symbol}",
        description=f"**Sector:** `{sector.upper()}`",
        color=color,
        timestamp=datetime.utcnow(),
    )
    embed.add_field(name="💰 Old Price",  value=f"`${old_price:.6f}`",          inline=True)
    embed.add_field(name="💰 New Price",  value=f"`${new_price:.6f}`",          inline=True)
    embed.add_field(name="📊 1-min Δ",    value=f"`{change_pct:+.2f}%`",        inline=True)
    embed.add_field(name="📈 24 h Δ",     value=f"`{change_24h:+.2f}%`",        inline=True)
    embed.add_field(name="🏦 Exchange",   value="`Binance`",                     inline=True)
    embed.add_field(name="🔗 Trade",
                    value=f"[Open on Binance](https://www.binance.com/en/trade/{symbol})",
                    inline=True)
    embed.set_footer(text="CryptoPump Bot • Binance USDT pairs")
    return embed

# ── background task ───────────────────────────────────────────────────────────

@tasks.loop(seconds=CHECK_INTERVAL)
async def check_pumps():
    if not alert_channels:
        return

    try:
        prices = await fetch_binance_prices()
    except Exception as e:
        print(f"[{datetime.utcnow()}] Price-fetch error: {e}")
        return

    # Rebuild sector map if symbols cache is empty
    global sector_symbols
    if not sector_symbols:
        sector_symbols = build_sector_map(list(prices.keys()))
        print(f"[{datetime.utcnow()}] Sector map built: "
              + ", ".join(f"{s}({len(v)})" for s, v in sector_symbols.items()))

    for channel_id, watched_sectors in alert_channels.items():
        channel = bot.get_channel(channel_id)
        if channel is None:
            continue

        for sector in watched_sectors:
            symbols = sector_symbols.get(sector, [])
            for symbol in symbols:
                new_price = prices.get(symbol)
                if new_price is None:
                    continue

                old_price = price_cache.get(symbol)
                price_cache[symbol] = new_price

                if old_price is None:
                    continue  # first run — no baseline yet

                change_pct = ((new_price - old_price) / old_price) * 100

                if abs(change_pct) >= PUMP_THRESHOLD:
                    try:
                        change_24h = await fetch_24h_change(symbol)
                        embed = pump_embed(symbol, old_price, new_price,
                                           change_pct, sector, change_24h)
                        await channel.send(embed=embed)
                        print(f"[{datetime.utcnow()}] Alert sent: {symbol} {change_pct:+.2f}%")
                    except Exception as e:
                        print(f"[{datetime.utcnow()}] Alert error for {symbol}: {e}")

@check_pumps.before_loop
async def before_check():
    await bot.wait_until_ready()

# ── commands ──────────────────────────────────────────────────────────────────

@bot.command(name="watch")
async def watch(ctx, *sectors):
    """Start watching one or more sectors in this channel.
    Usage: !watch ai defi gaming
    """
    if not sectors:
        await ctx.send("❌ Please provide sector(s). Example: `!watch ai defi`\n"
                       f"Available: `{', '.join(SECTORS.keys())}`")
        return

    valid   = [s.lower() for s in sectors if s.lower() in SECTORS]
    invalid = [s for s in sectors if s.lower() not in SECTORS]

    if invalid:
        await ctx.send(f"⚠️ Unknown sector(s): `{', '.join(invalid)}`\n"
                       f"Available: `{', '.join(SECTORS.keys())}`")

    if not valid:
        return

    cid = ctx.channel.id
    existing = set(alert_channels.get(cid, []))
    alert_channels[cid] = list(existing | set(valid))

    # Save to disk
    _save_channels()

    embed = discord.Embed(
        title="✅ Watching Sectors",
        description="\n".join(f"• `{s.upper()}`" for s in valid),
        color=0x00AAFF,
        timestamp=datetime.utcnow(),
    )
    embed.add_field(name="Threshold", value=f"`{PUMP_THRESHOLD}%` per `{CHECK_INTERVAL}s`")
    embed.set_footer(text="CryptoPump Bot • Binance USDT pairs")
    await ctx.send(embed=embed)

@bot.command(name="unwatch")
async def unwatch(ctx, *sectors):
    """Stop watching sectors. Usage: !unwatch ai  OR  !unwatch all"""
    cid = ctx.channel.id
    if not alert_channels.get(cid):
        await ctx.send("ℹ️ This channel has no active watches.")
        return

    if "all" in [s.lower() for s in sectors]:
        alert_channels.pop(cid, None)
        _save_channels()
        await ctx.send("🗑️ Removed all sector watches for this channel.")
        return

    to_remove = [s.lower() for s in sectors]
    alert_channels[cid] = [s for s in alert_channels[cid] if s not in to_remove]
    if not alert_channels[cid]:
        alert_channels.pop(cid)
    _save_channels()
    await ctx.send(f"🗑️ Removed: `{', '.join(to_remove)}`")

@bot.command(name="status")
async def status(ctx):
    """Show currently watched sectors in this channel."""
    cid = ctx.channel.id
    watched = alert_channels.get(cid, [])

    embed = discord.Embed(title="📡 Watch Status", color=0xFFAA00, timestamp=datetime.utcnow())
    if watched:
        embed.add_field(name="Sectors", value="\n".join(f"• `{s.upper()}`" for s in watched))
    else:
        embed.description = "No sectors being watched in this channel.\nUse `!watch <sector>` to start."

    embed.add_field(name="Check Interval", value=f"`{CHECK_INTERVAL}s`",    inline=True)
    embed.add_field(name="Pump Threshold",  value=f"`{PUMP_THRESHOLD}%`",   inline=True)
    embed.set_footer(text="CryptoPump Bot • Binance USDT pairs")
    await ctx.send(embed=embed)

@bot.command(name="sectors")
async def list_sectors(ctx):
    """List all available sectors and their coins."""
    embed = discord.Embed(title="📂 Available Sectors", color=0x9B59B6, timestamp=datetime.utcnow())
    for sector, coins in SECTORS.items():
        embed.add_field(
            name=f"`{sector.upper()}`",
            value=", ".join(f"`{c}`" for c in coins[:10]) + ("…" if len(coins) > 10 else ""),
            inline=False,
        )
    embed.set_footer(text="Use !watch <sector> to start monitoring")
    await ctx.send(embed=embed)

@bot.command(name="price")
async def price_cmd(ctx, coin: str):
    """Get the current Binance price for a coin. Usage: !price BTC"""
    symbol = coin.upper() + "USDT"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(BINANCE_24H_URL, params={"symbol": symbol}) as r:
                data = await r.json()

        if "code" in data:
            await ctx.send(f"❌ `{symbol}` not found on Binance.")
            return

        embed = discord.Embed(
            title=f"💱 {symbol} — Binance",
            color=0x00FF88,
            timestamp=datetime.utcnow(),
        )
        embed.add_field(name="Price",    value=f"`${float(data['lastPrice']):.6f}`", inline=True)
        embed.add_field(name="24h Δ",    value=f"`{float(data['priceChangePercent']):+.2f}%`", inline=True)
        embed.add_field(name="24h High", value=f"`${float(data['highPrice']):.6f}`", inline=True)
        embed.add_field(name="24h Low",  value=f"`${float(data['lowPrice']):.6f}`",  inline=True)
        embed.add_field(name="Volume",   value=f"`{float(data['volume']):,.2f}`",    inline=True)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"❌ Error fetching price: {e}")

@bot.command(name="addcoin")
@commands.has_permissions(administrator=True)
async def add_coin(ctx, sector: str, *coins):
    """Admin: add coins to a sector. Usage: !addcoin ai RNDR FET"""
    sector = sector.lower()
    if sector not in SECTORS:
        await ctx.send(f"❌ Unknown sector `{sector}`. Available: `{', '.join(SECTORS.keys())}`")
        return
    added = []
    for coin in coins:
        coin = coin.upper()
        if coin not in SECTORS[sector]:
            SECTORS[sector].append(coin)
            added.append(coin)
    # Invalidate cache so sector map is rebuilt
    sector_symbols.clear()
    _save_sectors()
    await ctx.send(f"✅ Added `{', '.join(added)}` to `{sector.upper()}`")

# ── persistence helpers ───────────────────────────────────────────────────────

def _save_channels():
    with open("alert_channels.json", "w") as f:
        json.dump({str(k): v for k, v in alert_channels.items()}, f)

def _load_channels():
    if os.path.exists("alert_channels.json"):
        with open("alert_channels.json") as f:
            data = json.load(f)
        for k, v in data.items():
            alert_channels[int(k)] = v
        print(f"Loaded {len(alert_channels)} alert channel(s) from disk.")

def _save_sectors():
    with open("sectors_custom.json", "w") as f:
        json.dump(SECTORS, f, indent=2)

# ── events ────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    _load_channels()
    check_pumps.start()
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    print(f"   Interval: {CHECK_INTERVAL}s | Threshold: {PUMP_THRESHOLD}%")
    print(f"   Sectors: {', '.join(SECTORS.keys())}")

bot.run(DISCORD_TOKEN)