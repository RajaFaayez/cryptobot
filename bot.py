import discord
from discord.ext import commands, tasks
import aiohttp
from aiohttp import web
import asyncio
import json
import os
from datetime import datetime, timezone
from collections import defaultdict

from config import (
    DISCORD_TOKEN, SECTORS, CHECK_INTERVAL,
    PUMP_THRESHOLD, VOLUME_SPIKE_MULTIPLIER,
    COOLDOWN_MINUTES, MIN_VOLUME_USDT,
    CUMULATIVE_WINDOW_MINUTES, CUMULATIVE_THRESHOLD,
    CONTAGION_THRESHOLD, CONTAGION_WINDOW_MINUTES, CONTAGION_COOLDOWN_MINUTES,
)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ── state ─────────────────────────────────────────────────────────────────────
price_cache    = {}                  # symbol -> last price
volume_cache   = {}                  # symbol -> last quoteVolume
alert_cooldown = {}                  # symbol -> datetime of last alert
price_history  = defaultdict(list)   # symbol -> [(ts, price), ...]
sector_map     = {}                  # symbol -> sector name
alert_channels = {}                  # channel_id -> [sectors]

# Contagion tracking
# sector -> [(ts, symbol, change_pct)]  — log of pumps per sector
sector_pump_log = defaultdict(list)
# symbol -> ts  — when we last sent a contagion suggestion for it
contagion_sent  = {}
# sector -> ts  — when we last sent a contagion alert for this sector
sector_contagion_sent = {}

BINANCE_24H_URL = "https://api.binance.com/api/v3/ticker/24hr"

# ── reverse lookup: sector -> [symbols] ───────────────────────────────────────
def get_sector_symbols(sector: str) -> list[str]:
    return [sym for sym, sec in sector_map.items() if sec == sector]

# ── build sector map ──────────────────────────────────────────────────────────
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

# ── fetch ─────────────────────────────────────────────────────────────────────
async def fetch_all_tickers() -> list[dict]:
    async with aiohttp.ClientSession() as session:
        async with session.get(BINANCE_24H_URL) as r:
            data = await r.json()
    return [t for t in data if t["symbol"].endswith("USDT")]

# ── cooldown helpers ──────────────────────────────────────────────────────────
def is_on_cooldown(symbol: str) -> bool:
    last = alert_cooldown.get(symbol)
    if last is None:
        return False
    return (datetime.now(timezone.utc) - last).total_seconds() / 60 < COOLDOWN_MINUTES

def set_cooldown(symbol: str):
    alert_cooldown[symbol] = datetime.now(timezone.utc)

# ── cumulative change ─────────────────────────────────────────────────────────
def get_cumulative_change(symbol: str, current_price: float) -> float | None:
    now_ts  = datetime.now(timezone.utc).timestamp()
    cutoff  = now_ts - (CUMULATIVE_WINDOW_MINUTES * 60)
    price_history[symbol] = [(t, p) for t, p in price_history[symbol] if t >= cutoff]
    history = price_history[symbol]
    if not history:
        return None
    oldest = history[0][1]
    return ((current_price - oldest) / oldest) * 100 if oldest else None

# ── contagion engine ──────────────────────────────────────────────────────────
def record_sector_pump(sector: str, symbol: str, change_pct: float):
    """Log a pump event for the sector."""
    now_ts = datetime.now(timezone.utc).timestamp()
    sector_pump_log[sector].append((now_ts, symbol, change_pct))
    # Trim old events outside contagion window
    cutoff = now_ts - (CONTAGION_WINDOW_MINUTES * 60)
    sector_pump_log[sector] = [
        (t, s, c) for t, s, c in sector_pump_log[sector] if t >= cutoff
    ]

def get_contagion_suggestions(
    pumped_symbol: str,
    sector: str,
    ticker_map: dict,
) -> list[dict] | None:
    """
    After a strong pump in a sector, analyse all OTHER coins in that sector
    and return ranked suggestions based on:
      - Not yet pumped (still below recent high)
      - Has decent volume
      - Correlated historically via 24h direction
      - Hasn't already been suggested recently
    Returns list of dicts or None if sector is unknown / too small.
    """
    if sector == "other":
        return None

    sector_syms = [s for s in get_sector_symbols(sector) if s != pumped_symbol]
    if len(sector_syms) < 2:
        return None

    now_ts  = datetime.now(timezone.utc).timestamp()
    cutoff  = now_ts - (CONTAGION_WINDOW_MINUTES * 60)

    # Recent pumps in this sector (excluding the triggering coin)
    recent_pumps = [
        (s, c) for t, s, c in sector_pump_log[sector]
        if t >= cutoff and s != pumped_symbol
    ]
    already_pumped = {s for s, _ in recent_pumps}

    candidates = []
    for sym in sector_syms:
        t = ticker_map.get(sym)
        if not t:
            continue

        price     = float(t["lastPrice"])
        chg24     = float(t["priceChangePercent"])
        vol_quote = float(t["quoteVolume"])
        per_min   = vol_quote / (24 * 60)

        if per_min < MIN_VOLUME_USDT:
            continue  # dust

        # Skip coins that already pumped hard in this window
        if sym in already_pumped:
            continue

        # Skip if we already sent a contagion alert for this coin recently
        last_cong = contagion_sent.get(sym)
        if last_cong and (now_ts - last_cong) < (CONTAGION_WINDOW_MINUTES * 60):
            continue

        # Score: prefer coins that are lagging (low 24h change) but have good volume
        # and same directional bias as the sector pump
        lag_score    = -chg24          # more negative = more room to pump
        volume_score = min(per_min / 1000, 10)  # cap at 10
        score        = lag_score + volume_score

        candidates.append({
            "symbol":    sym,
            "coin":      sym.replace("USDT", ""),
            "price":     price,
            "chg24":     chg24,
            "vol_per_min": per_min,
            "score":     score,
        })

    if not candidates:
        return None

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:5]   # top 5


def contagion_embed(
    pumped_symbol: str,
    pumped_pct: float,
    sector: str,
    suggestions: list[dict],
    recent_pumps_in_sector: int,
) -> discord.Embed:
    pumped_coin = pumped_symbol.replace("USDT", "")

    embed = discord.Embed(
        title=f"🔥 SECTOR CONTAGION — {sector.upper()}",
        description=(
            f"**{pumped_coin}** just pumped **{pumped_pct:+.2f}%**\n"
            f"`{recent_pumps_in_sector}` coin(s) already moved in this sector recently.\n\n"
            f"⚡ **These coins could follow — watch closely:**"
        ),
        color=0xFFAA00,
        timestamp=datetime.now(timezone.utc),
    )

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    for i, s in enumerate(suggestions):
        coin = s["coin"]
        ps   = f"${s['price']:.6f}" if s["price"] < 1 else f"${s['price']:.4f}"
        vol  = f"${s['vol_per_min']:,.0f}/min"
        chg  = f"{s['chg24']:+.2f}%"
        embed.add_field(
            name=f"{medals[i]}  {coin}",
            value=(
                f"Price: `{ps}`\n"
                f"24h: `{chg}`\n"
                f"Vol: `{vol}`\n"
                f"[Trade](https://www.binance.com/en/trade/{coin}_USDT)"
            ),
            inline=True,
        )

    embed.set_footer(
        text=(
            "CryptoPump Bot • Sector Contagion Alert • "
            "Suggestions based on lag + volume — DYOR, not financial advice"
        )
    )
    return embed


# ── pump alert embed ──────────────────────────────────────────────────────────
def pump_embed(symbol, old_price, new_price, change_pct,
               sector, change_24h, volume_usdt, volume_spike, trigger) -> discord.Embed:
    is_pump   = change_pct > 0
    direction = "🚀 PUMP" if is_pump else "📉 DUMP"
    color     = 0x00FF88 if is_pump else 0xFF4444
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
    embed.add_field(name="💰 Price",       value=f"`${new_price:.6f}`",             inline=True)
    embed.add_field(name="📊 Move",        value=f"`{change_pct:+.2f}%`",           inline=True)
    embed.add_field(name="📈 24h Δ",       value=f"`{change_24h:+.2f}%`",           inline=True)
    embed.add_field(name="💵 Vol/min",     value=f"`${volume_usdt:,.0f}`",           inline=True)
    if volume_spike:
        embed.add_field(name="⚡ Vol Spike", value=f"`{volume_spike:.1f}x`",        inline=True)
    embed.add_field(name="🔗 Trade",
        value=f"[Binance](https://www.binance.com/en/trade/{coin}_USDT)",            inline=True)
    embed.set_footer(text="CryptoPump Bot • Binance USDT")
    return embed


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
        all_syms   = {t["symbol"] for t in tickers}
        sector_map = build_sector_map(all_syms)
        print(f"[{datetime.now(timezone.utc)}] Sector map built: {len(sector_map)} symbols")

    now_ts     = datetime.now(timezone.utc).timestamp()
    ticker_map = {t["symbol"]: t for t in tickers}  # for contagion lookups

    alerts_to_send     = []   # (embed, sector, symbol, change_pct, is_pump)
    contagion_to_send  = []   # (embed, sector)

    for ticker in tickers:
        symbol     = ticker["symbol"]
        new_price  = float(ticker["lastPrice"])
        change_24h = float(ticker["priceChangePercent"])
        vol_quote  = float(ticker["quoteVolume"])

        if new_price == 0:
            continue

        # Filter out known broken/glitchy coins
        BLACKLIST = {"BTTCUSDT", "LUNCUSDT"}
        if symbol in BLACKLIST:
            continue

        per_min_vol = vol_quote / (24 * 60)
        if per_min_vol < MIN_VOLUME_USDT:
            price_cache[symbol] = new_price
            continue
        
        # Filter coins whose price is so small it causes floating point noise
        if new_price < 0.000001:
            continue

        # Price history
        price_history[symbol].append((now_ts, new_price))
        price_history[symbol] = [
            (t, p) for t, p in price_history[symbol] if t >= now_ts - 7200
        ]

        old_price = price_cache.get(symbol)
        price_cache[symbol] = new_price

        # Volume spike
        old_vol = volume_cache.get(symbol)
        volume_cache[symbol] = vol_quote
        vol_spike_ratio = None
        if old_vol and old_vol > 0 and vol_quote > old_vol:
            ratio = vol_quote / old_vol
            if ratio >= VOLUME_SPIKE_MULTIPLIER:
                vol_spike_ratio = ratio

        if old_price is None:
            continue

        change_pct = ((new_price - old_price) / old_price) * 100
        cumulative  = get_cumulative_change(symbol, new_price)

        # Trigger detection
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

        # Build pump alert embed
        embed = pump_embed(
            symbol, old_price, new_price, change_pct,
            sector, change_24h, per_min_vol * CHECK_INTERVAL,
            vol_spike_ratio, trigger,
        )
        alerts_to_send.append((embed, sector, symbol, change_pct, change_pct > 0))

        # Record for contagion tracking (only pumps, not dumps)
        # Works for ALL named sectors (not other)
        if change_pct > 0 and sector != "other":
            record_sector_pump(sector, symbol, change_pct)

        # ── contagion check ───────────────────────────────────────────────────
        # Fire if: single candle >= threshold OR cumulative >= threshold
        # Only for named sectors (needs peer coins to suggest)
        effective_move = max(abs(change_pct), abs(cumulative) if cumulative else 0)
        if effective_move >= CONTAGION_THRESHOLD and sector != "other" and change_pct > 0:
            recent_in_sector = len([
                1 for t, s, c in sector_pump_log[sector]
                if s != symbol
            ])
            # Only send contagion if not sent one for this sector recently
            last_sector_cong = sector_contagion_sent.get(sector, 0)
            now_check = datetime.now(timezone.utc).timestamp()
            if now_check - last_sector_cong >= CONTAGION_COOLDOWN_MINUTES * 60:
                suggestions = get_contagion_suggestions(symbol, sector, ticker_map)
                if suggestions:
                    c_embed = contagion_embed(
                        symbol, change_pct, sector, suggestions, recent_in_sector
                    )
                    contagion_to_send.append((c_embed, sector, symbol, suggestions))
                    sector_contagion_sent[sector] = now_check

    # ── send alerts to subscribed channels ────────────────────────────────────
    for channel_id, watched in alert_channels.items():
        channel   = bot.get_channel(channel_id)
        if channel is None:
            continue
        watch_all = "all" in watched

        # Regular pump/dump alerts
        for embed, sector, symbol, change_pct, is_pump in alerts_to_send:
            if watch_all or sector in watched or "other" in watched:
                try:
                    await channel.send(embed=embed)
                    set_cooldown(symbol)
                    print(f"[{datetime.now(timezone.utc)}] ✅ {symbol} {change_pct:+.2f}% → #{channel.name}")
                except Exception as e:
                    print(f"Send error: {e}")
                await asyncio.sleep(0.3)

        # Contagion alerts (sent right after the triggering pump alert)
        for c_embed, sector, pumped_sym, suggestions in contagion_to_send:
            if watch_all or sector in watched or "other" in watched:
                try:
                    await channel.send(embed=c_embed)
                    # Mark all suggested coins so we don't re-suggest too soon
                    now_ts2 = datetime.now(timezone.utc).timestamp()
                    for s in suggestions:
                        contagion_sent[s["symbol"]] = now_ts2
                    print(f"[{datetime.now(timezone.utc)}] 🔥 Contagion alert: {pumped_sym} → {sector}")
                except Exception as e:
                    print(f"Contagion send error: {e}")
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
    embed.add_field(name="Price threshold",      value=f"`{PUMP_THRESHOLD}%` per `{CHECK_INTERVAL}s`")
    embed.add_field(name="Cumulative threshold", value=f"`{CUMULATIVE_THRESHOLD}%` over `{CUMULATIVE_WINDOW_MINUTES}min`")
    embed.add_field(name="Contagion trigger",    value=f"`{CONTAGION_THRESHOLD}%` single-candle pump")
    embed.add_field(name="Cooldown",             value=f"`{COOLDOWN_MINUTES} min` per coin")
    embed.set_footer(text="CryptoPump Bot • Binance USDT")
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
    embed   = discord.Embed(title="📡 Watch Status", color=0xFFAA00,
                             timestamp=datetime.now(timezone.utc))
    if watched:
        embed.add_field(name="Watching",
                        value="\n".join(f"• `{s.upper()}`" for s in watched), inline=False)
    else:
        embed.description = "Nothing being watched. Use `!watch <sector>` or `!watch all`."

    embed.add_field(name="Price threshold",    value=f"`{PUMP_THRESHOLD}%/{CHECK_INTERVAL}s`",                    inline=True)
    embed.add_field(name="Cumulative",         value=f"`{CUMULATIVE_THRESHOLD}%/{CUMULATIVE_WINDOW_MINUTES}min`", inline=True)
    embed.add_field(name="Vol spike",          value=f"`{VOLUME_SPIKE_MULTIPLIER}x`",                             inline=True)
    embed.add_field(name="Contagion trigger",  value=f"`{CONTAGION_THRESHOLD}%` pump",                            inline=True)
    embed.add_field(name="Contagion window",   value=f"`{CONTAGION_WINDOW_MINUTES} min`",                         inline=True)
    embed.add_field(name="Cooldown",           value=f"`{COOLDOWN_MINUTES} min`",                                 inline=True)
    embed.set_footer(text="CryptoPump Bot • Binance USDT")
    await ctx.send(embed=embed)

@bot.command(name="sectors")
async def list_sectors(ctx):
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
    await ctx.send(f"⏳ Scanning ALL Binance USDT pairs for last `{hours}h`...")
    try:
        tickers = await fetch_all_tickers()
        results = []
        for t in tickers:
            chg24  = float(t["priceChangePercent"])
            vol    = float(t["quoteVolume"])
            price  = float(t["lastPrice"])
            sym    = t["symbol"]
            if vol < MIN_VOLUME_USDT * 60 * 24:
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
            coin = sym.replace("USDT", "")
            ps   = f"${price:.4f}" if price < 1 else f"${price:.2f}"
            lines.append(f"{coin:<10} {sec:<10} {ps:>10} {chg_h:>+7.2f}% {chg24:>+7.2f}%")
        lines.append("")
        lines.append(f"📉 TOP DUMPS — Last ~{hours}h")
        lines.append(f"{'COIN':<10} {'SECTOR':<10} {'PRICE':>10} {f'~{hours}h':>8} {'24h':>8}")
        lines.append("─" * 50)
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
        embed.set_footer(text="CryptoPump Bot • ~estimate from 24h data")
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"❌ Error: {e}")

@bot.command(name="summary")
async def summary(ctx, hours: int = 8):
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

@bot.command(name="watchlist")
async def watchlist(ctx, sector: str = None):
    """Show current contagion candidates for a sector. Usage: !watchlist ai"""
    if not sector:
        await ctx.send("Usage: `!watchlist <sector>`  e.g. `!watchlist ai`")
        return
    sector = sector.lower()
    if sector not in SECTORS:
        await ctx.send(f"❌ Unknown sector `{sector}`")
        return

    try:
        tickers    = await fetch_all_tickers()
        ticker_map = {t["symbol"]: t for t in tickers}

        # Build a dummy contagion suggestion without a specific trigger
        sector_syms = [f"{c}USDT" for c in SECTORS[sector]]
        candidates  = []
        for sym in sector_syms:
            t = ticker_map.get(sym)
            if not t:
                continue
            price     = float(t["lastPrice"])
            chg24     = float(t["priceChangePercent"])
            vol_quote = float(t["quoteVolume"])
            per_min   = vol_quote / (24 * 60)
            if per_min < MIN_VOLUME_USDT:
                continue
            lag_score    = -chg24
            volume_score = min(per_min / 1000, 10)
            candidates.append({
                "symbol": sym, "coin": sym.replace("USDT",""),
                "price": price, "chg24": chg24,
                "vol_per_min": per_min,
                "score": lag_score + volume_score,
            })

        candidates.sort(key=lambda x: x["score"], reverse=True)
        top = candidates[:5]

        embed = discord.Embed(
            title=f"👀 {sector.upper()} — Coins to Watch",
            description="Ranked by lag (low 24h change) + volume. These are most likely to pump next if sector heats up.",
            color=0x9B59B6,
            timestamp=datetime.now(timezone.utc),
        )
        medals = ["🥇","🥈","🥉","4️⃣","5️⃣"]
        for i, s in enumerate(top):
            ps  = f"${s['price']:.6f}" if s["price"] < 1 else f"${s['price']:.4f}"
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
    except Exception as e:
        await ctx.send(f"❌ Error: {e}")

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
    return web.Response(text="CryptoPump Bot alive", status=200)

async def run_web_server():
    port    = int(os.getenv("PORT", 8080))
    app     = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    runner  = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 Web server running on port {port}")

# ── startup ───────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    _load_channels()
    check_pumps.start()
    print(f"✅ {bot.user} ready")
    print(f"   Threshold={PUMP_THRESHOLD}% | Cumulative={CUMULATIVE_THRESHOLD}%/{CUMULATIVE_WINDOW_MINUTES}min")
    print(f"   Contagion trigger={CONTAGION_THRESHOLD}% | Window={CONTAGION_WINDOW_MINUTES}min")

async def main():
    await run_web_server()
    await bot.start(DISCORD_TOKEN)

asyncio.run(main())