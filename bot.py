import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
import json
import os
from datetime import datetime, timezone
from collections import defaultdict
from config import (
    DISCORD_TOKEN, SECTORS, CHECK_INTERVAL,
    PUMP_THRESHOLD, VOLUME_SPIKE_MULTIPLIER,
    COOLDOWN_MINUTES, MIN_VOLUME_USDT,
    CUMULATIVE_WINDOW_MINUTES, CUMULATIVE_THRESHOLD
)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ── state ─────────────────────────────────────────────────────────────────────
price_cache   = {}          # symbol -> last price
volume_cache  = {}          # symbol -> last quoteVolume (from 24h ticker)
alert_cooldown = {}         # symbol -> datetime of last alert
price_history  = defaultdict(list)  # symbol -> [(timestamp, price), ...]
sector_map     = {}         # symbol -> sector name  (built at startup)
alert_channels = {}         # channel_id -> list of sectors (or ["all"])

BINANCE_24H_URL = "https://api.binance.com/api/v3/ticker/24hr"

# ── build sector map ──────────────────────────────────────────────────────────

def build_sector_map(all_symbols: set) -> dict:
    """symbol -> sector, built from config + 'all' catch-all."""
    result = {}
    for sector, coins in SECTORS.items():
        for coin in coins:
            sym = f"{coin}USDT"
            if sym in all_symbols:
                result[sym] = sector
    # Every symbol not in a named sector gets tagged "other"
    for sym in all_symbols:
        if sym not in result:
            result[sym] = "other"
    return result

# ── fetch all tickers in one call ─────────────────────────────────────────────

async def fetch_all_tickers() -> list[dict]:
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H_URL) as r:
            data = await r.json()
    return [t for t in data if t["symbol"].endswith("USDT")]

# ── embed builders ────────────────────────────────────────────────────────────

def pump_embed(symbol, old_price, new_price, change_pct,
               sector, change_24h, volume_usdt, volume_spike, trigger) -> discord.Embed:

    is_pump = change_pct > 0
    direction = "🚀 PUMP" if is_pump else "📉 DUMP"
    color     = 0x00FF88  if is_pump else 0xFF4444
    coin      = symbol.replace("USDT", "")

    embed = discord.Embed(
        title=f"{direction} — {symbol}",
        description=(
            f"**Sector:** `{sector.upper()}`\n"
            f"**Trigger:** `{trigger}`"
        ),
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="💰 Price",      value=f"`${new_price:.6f}`",             inline=True)
    embed.add_field(name="📊 1-min Δ",    value=f"`{change_pct:+.2f}%`",           inline=True)
    embed.add_field(name="📈 24h Δ",      value=f"`{change_24h:+.2f}%`",           inline=True)
    embed.add_field(name="💵 Vol (quote)", value=f"`${volume_usdt:,.0f}`",          inline=True)
    if volume_spike:
        embed.add_field(name="⚡ Vol Spike", value=f"`{volume_spike:.1f}x normal`", inline=True)
    embed.add_field(name="🔗 Trade",
        value=f"[Binance](https://www.binance.com/en/trade/{coin}_USDT)",           inline=True)
    embed.set_footer(text="CryptoPump Bot • Binance USDT")
    return embed

# ── cooldown helper ───────────────────────────────────────────────────────────

def is_on_cooldown(symbol: str) -> bool:
    last = alert_cooldown.get(symbol)
    if last is None:
        return False
    diff = (datetime.now(timezone.utc) - last).total_seconds() / 60
    return diff < COOLDOWN_MINUTES

def set_cooldown(symbol: str):
    alert_cooldown[symbol] = datetime.now(timezone.utc)

# ── cumulative change helper ───────────────────────────────────────────────────

def get_cumulative_change(symbol: str, current_price: float) -> float | None:
    """Return % change over last CUMULATIVE_WINDOW_MINUTES, or None if not enough data."""
    history = price_history[symbol]
    now = datetime.now(timezone.utc)
    cutoff = (now.timestamp()) - (CUMULATIVE_WINDOW_MINUTES * 60)
    # Keep only recent entries
    price_history[symbol] = [(t, p) for t, p in history if t >= cutoff]
    history = price_history[symbol]
    if not history:
        return None
    oldest_price = history[0][1]
    if oldest_price == 0:
        return None
    return ((current_price - oldest_price) / oldest_price) * 100

# ── main polling loop ─────────────────────────────────────────────────────────

@tasks.loop(seconds=CHECK_INTERVAL)
async def check_pumps():
    if not alert_channels:
        return

    try:
        tickers = await fetch_all_tickers()
    except Exception as e:
        print(f"[{datetime.now(timezone.utc)}] Fetch error: {e}")
        return

    global sector_map
    if not sector_map:
        all_syms = {t["symbol"] for t in tickers}
        sector_map = build_sector_map(all_syms)
        print(f"[{datetime.now(timezone.utc)}] Sector map: "
              f"{len(sector_map)} symbols, "
              f"{len([s for s in sector_map.values() if s != 'other'])} in named sectors")

    now_ts = datetime.now(timezone.utc).timestamp()

    # Collect alerts: list of (embed, sector, symbol)
    alerts_to_send = []

    for ticker in tickers:
        symbol     = ticker["symbol"]
        new_price  = float(ticker["lastPrice"])
        change_24h = float(ticker["priceChangePercent"])
        vol_quote  = float(ticker["quoteVolume"])  # total USDT volume in 24h

        if new_price == 0:
            continue

        # Skip very low volume coins (likely illiquid / scam)
        # Use count-based volume from ticker: quoteVolume / 24h ≈ per-minute
        per_min_vol = vol_quote / (24 * 60)
        if per_min_vol < MIN_VOLUME_USDT:
            price_cache[symbol] = new_price
            continue

        # Track price history for cumulative alerts
        price_history[symbol].append((now_ts, new_price))
        # Trim old entries (keep last 2h max)
        price_history[symbol] = [
            (t, p) for t, p in price_history[symbol]
            if t >= now_ts - 7200
        ]

        old_price = price_cache.get(symbol)
        price_cache[symbol] = new_price

        # Volume spike detection
        old_vol     = volume_cache.get(symbol)
        volume_cache[symbol] = vol_quote
        vol_spike_ratio = None
        if old_vol and old_vol > 0:
            # Compare current quoteVolume to last stored — rough spike signal
            vol_spike_ratio = vol_quote / old_vol if vol_quote > old_vol else None
            if vol_spike_ratio and vol_spike_ratio < VOLUME_SPIKE_MULTIPLIER:
                vol_spike_ratio = None

        if old_price is None:
            continue  # first run

        change_pct = ((new_price - old_price) / old_price) * 100
        cumulative  = get_cumulative_change(symbol, new_price)

        # Determine trigger type
        trigger = None
        if abs(change_pct) >= PUMP_THRESHOLD:
            trigger = f"{'pump' if change_pct > 0 else 'dump'} {change_pct:+.2f}% in {CHECK_INTERVAL}s"
        elif vol_spike_ratio:
            trigger = f"volume spike {vol_spike_ratio:.1f}x"
        elif cumulative is not None and abs(cumulative) >= CUMULATIVE_THRESHOLD:
            trigger = f"cumulative {cumulative:+.2f}% in {CUMULATIVE_WINDOW_MINUTES}min"

        if trigger is None:
            continue

        if is_on_cooldown(symbol):
            continue

        sector = sector_map.get(symbol, "other")
        embed  = pump_embed(symbol, old_price, new_price, change_pct,
                            sector, change_24h, per_min_vol * CHECK_INTERVAL,
                            vol_spike_ratio, trigger)
        alerts_to_send.append((embed, sector, symbol))

    # Send to subscribed channels
    for channel_id, watched in alert_channels.items():
        channel = bot.get_channel(channel_id)
        if channel is None:
            continue

        watch_all = "all" in watched

        for embed, sector, symbol in alerts_to_send:
            if watch_all or sector in watched or "other" in watched:
                try:
                    await channel.send(embed=embed)
                    set_cooldown(symbol)
                    print(f"[{datetime.now(timezone.utc)}] ✅ {symbol} → #{channel.name}")
                except Exception as e:
                    print(f"[{datetime.now(timezone.utc)}] Send error: {e}")
                await asyncio.sleep(0.3)

@check_pumps.before_loop
async def before_loop():
    await bot.wait_until_ready()

# ── commands ──────────────────────────────────────────────────────────────────

@bot.command(name="watch")
async def watch(ctx, *sectors):
    """Watch sectors. Use 'all' to watch every Binance coin.
    Usage: !watch ai defi    OR    !watch all
    """
    valid_sectors = list(SECTORS.keys()) + ["all", "other"]

    if not sectors:
        await ctx.send(
            f"❌ Provide sector(s) or `all`.\n"
            f"Named sectors: `{', '.join(SECTORS.keys())}`\n"
            f"Use `!watch all` to monitor every Binance USDT pair."
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
    embed.add_field(name="Price threshold",      value=f"`{PUMP_THRESHOLD}%` per `{CHECK_INTERVAL}s`")
    embed.add_field(name="Cumulative threshold", value=f"`{CUMULATIVE_THRESHOLD}%` over `{CUMULATIVE_WINDOW_MINUTES}min`")
    embed.add_field(name="Cooldown",             value=f"`{COOLDOWN_MINUTES} min` per coin")
    embed.set_footer(text="CryptoPump Bot • Binance USDT")
    await ctx.send(embed=embed)

@bot.command(name="unwatch")
async def unwatch(ctx, *sectors):
    """Stop watching. Usage: !unwatch ai  OR  !unwatch all"""
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
    """Show what's being watched in this channel."""
    cid     = ctx.channel.id
    watched = alert_channels.get(cid, [])
    embed   = discord.Embed(title="📡 Watch Status", color=0xFFAA00,
                             timestamp=datetime.now(timezone.utc))
    if watched:
        embed.add_field(name="Watching",
                        value="\n".join(f"• `{s.upper()}`" for s in watched),
                        inline=False)
    else:
        embed.description = "Nothing being watched. Use `!watch <sector>` or `!watch all`."

    embed.add_field(name="Price threshold",      value=f"`{PUMP_THRESHOLD}%/{CHECK_INTERVAL}s`", inline=True)
    embed.add_field(name="Cumulative threshold", value=f"`{CUMULATIVE_THRESHOLD}%/{CUMULATIVE_WINDOW_MINUTES}min`", inline=True)
    embed.add_field(name="Vol spike trigger",    value=f"`{VOLUME_SPIKE_MULTIPLIER}x`",           inline=True)
    embed.add_field(name="Cooldown",             value=f"`{COOLDOWN_MINUTES} min`",               inline=True)
    embed.set_footer(text="CryptoPump Bot • Binance USDT")
    await ctx.send(embed=embed)

@bot.command(name="sectors")
async def list_sectors(ctx):
    """List all named sectors and their coins."""
    embed = discord.Embed(title="📂 Sectors", color=0x9B59B6,
                          timestamp=datetime.now(timezone.utc))
    for sector, coins in SECTORS.items():
        embed.add_field(
            name=f"`{sector.upper()}`",
            value=", ".join(f"`{c}`" for c in coins[:12]) + ("…" if len(coins) > 12 else ""),
            inline=False,
        )
    embed.set_footer(text="!watch all  →  monitors every Binance USDT pair")
    await ctx.send(embed=embed)

@bot.command(name="price")
async def price_cmd(ctx, coin: str):
    """Live Binance price. Usage: !price DOT"""
    symbol = coin.upper() + "USDT"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(BINANCE_24H_URL, params={"symbol": symbol}) as r:
                data = await r.json()
        if "code" in data:
            await ctx.send(f"❌ `{symbol}` not found on Binance.")
            return
        embed = discord.Embed(title=f"💱 {symbol}", color=0x00FF88,
                              timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Price",    value=f"`${float(data['lastPrice']):.6f}`",           inline=True)
        embed.add_field(name="24h Δ",    value=f"`{float(data['priceChangePercent']):+.2f}%`", inline=True)
        embed.add_field(name="24h High", value=f"`${float(data['highPrice']):.6f}`",           inline=True)
        embed.add_field(name="24h Low",  value=f"`${float(data['lowPrice']):.6f}`",            inline=True)
        embed.add_field(name="24h Vol",  value=f"`${float(data['quoteVolume']):,.0f}`",        inline=True)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"❌ Error: {e}")

@bot.command(name="top")
async def top_movers(ctx, hours: int = 8):
    """Top movers across all Binance USDT pairs. Usage: !top  OR  !top 4"""
    await ctx.send(f"⏳ Scanning ALL Binance USDT pairs for last `{hours}h`...")
    try:
        tickers = await fetch_all_tickers()
        results = []
        for t in tickers:
            chg24  = float(t["priceChangePercent"])
            vol    = float(t["quoteVolume"])
            price  = float(t["lastPrice"])
            sym    = t["symbol"]
            if vol < MIN_VOLUME_USDT * 60 * 24:   # filter dust
                continue
            approx = chg24 * (hours / 24)
            sector = sector_map.get(sym, "other")
            results.append((abs(approx), approx, sym, sector, price, chg24))

        results.sort(reverse=True)
        top    = results[:10]
        bottom = sorted(results, key=lambda x: x[1])[:5]

        lines = ["```"]
        lines.append(f"🚀 TOP PUMPS — Last ~{hours}h")
        lines.append(f"{'COIN':<10} {'SECTOR':<10} {'PRICE':>10} {f'~{hours}h':>8} {'24h':>8}")
        lines.append("─" * 50)
        for _, chg_h, sym, sec, price, chg24 in top:
            coin = sym.replace("USDT","")
            ps   = f"${price:.4f}" if price < 1 else f"${price:.2f}"
            lines.append(f"{coin:<10} {sec:<10} {ps:>10} {chg_h:>+7.2f}% {chg24:>+7.2f}%")
        lines.append("")
        lines.append(f"📉 TOP DUMPS — Last ~{hours}h")
        lines.append(f"{'COIN':<10} {'SECTOR':<10} {'PRICE':>10} {f'~{hours}h':>8} {'24h':>8}")
        lines.append("─" * 50)
        for _, chg_h, sym, sec, price, chg24 in bottom:
            coin = sym.replace("USDT","")
            ps   = f"${price:.4f}" if price < 1 else f"${price:.2f}"
            lines.append(f"{coin:<10} {sec:<10} {ps:>10} {chg_h:>+7.2f}% {chg24:>+7.2f}%")
        lines.append("```")

        embed = discord.Embed(
            title="📊 Top Movers — All Binance USDT Pairs",
            description="\n".join(lines),
            color=0xFFAA00,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text="CryptoPump Bot • ~estimate from 24h data")
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"❌ Error: {e}")

@bot.command(name="summary")
async def summary(ctx, hours: int = 8):
    """Sector-by-sector snapshot. Usage: !summary  OR  !summary 4"""
    await ctx.send(f"⏳ Building sector snapshot for last `{hours}h`...")
    try:
        tickers    = await fetch_all_tickers()
        ticker_map = {t["symbol"]: t for t in tickers}

        for sector, coins in SECTORS.items():
            rows = []
            for coin in coins:
                sym = f"{coin}USDT"
                t   = ticker_map.get(sym)
                if not t:
                    continue
                price  = float(t["lastPrice"])
                chg24  = float(t["priceChangePercent"])
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
    except Exception as e:
        await ctx.send(f"❌ Error: {e}")

@bot.command(name="addcoin")
@commands.has_permissions(administrator=True)
async def add_coin(ctx, sector: str, *coins):
    """Admin: add coins to a sector. Usage: !addcoin ai RNDR"""
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
    sector_map.clear()   # force rebuild
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

# ── startup ───────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    _load_channels()
    check_pumps.start()
    print(f"✅ {bot.user} ready")
    print(f"   Interval={CHECK_INTERVAL}s | Threshold={PUMP_THRESHOLD}% | "
          f"Cumulative={CUMULATIVE_THRESHOLD}%/{CUMULATIVE_WINDOW_MINUTES}min | "
          f"VolSpike={VOLUME_SPIKE_MULTIPLIER}x | Cooldown={COOLDOWN_MINUTES}min")

bot.run(DISCORD_TOKEN)