import os

# ─────────────────────────────────────────────
#  REQUIRED
# ─────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")

# ─────────────────────────────────────────────
#  Alert engine settings
# ─────────────────────────────────────────────
# CHECK_INTERVAL removed — bot now uses real-time WebSocket stream
PUMP_THRESHOLD           = 1.0    # % move in one 60s window to alert
CUMULATIVE_THRESHOLD     = 5.0    # % move over CUMULATIVE_WINDOW_MINUTES to alert
CUMULATIVE_WINDOW_MINUTES = 30    # how many minutes to track cumulative move
VOLUME_SPIKE_MULTIPLIER  = 3.0    # alert if volume is 3x higher than last check
COOLDOWN_MINUTES         = 10     # same coin won't alert again for this many minutes
MIN_VOLUME_USDT          = 500    # minimum avg USDT volume per minute (filters dust coins)

# ─────────────────────────────────────────────
#  Sectors  (base asset symbols, no USDT suffix)
#  These are used for !summary, !sectors, and
#  filtering when you do !watch ai  etc.
#  Use  !watch all  to monitor every Binance pair
# ─────────────────────────────────────────────
SECTORS = {
    "ai": [
        "FET", "AGIX", "OCEAN", "RNDR", "NMR",
        "GRT", "AKT", "WLD", "TAO", "ARKM",
        "AIOZ", "PHB", "CTXC", "DBC",
    ],
    "defi": [
        "UNI", "AAVE", "COMP", "MKR", "SNX",
        "CRV", "SUSHI", "YFI", "BAL", "1INCH",
        "DYDX", "GMX", "CAKE", "RUNE", "LDO",
        "CVX", "FXS", "SPELL", "BOND",
    ],
    "gaming": [
        "AXS", "SAND", "MANA", "ENJ", "GALA",
        "ILV", "SLP", "ALICE", "TLM", "VOXEL",
        "MC", "PYR", "MAGIC", "YGG",
        "BEAM", "GHST", "MOBOX",
    ],
    "layer1": [
        "ETH", "BNB", "SOL", "ADA", "AVAX",
        "DOT", "ATOM", "NEAR", "FTM", "ONE",
        "ALGO", "XTZ", "EOS", "TRX", "HBAR",
        "EGLD", "KAVA", "CELO", "SUI", "APT",
    ],
    "layer2": [
        "MATIC", "OP", "ARB", "LRC", "IMX",
        "STRK", "SKL", "CELR", "BOBA", "METIS",
        "MANTA",
    ],
    "meme": [
        "DOGE", "SHIB", "PEPE", "FLOKI", "BONK",
        "WIF", "NEIRO", "TURBO", "MEME",
    ],
    "rwa": [
        "ONDO", "POLYX", "CFG", "RIO", "MPL",
        "TRU", "PAXG",
    ],
    "infra": [
        "LINK", "FIL", "AR", "STORJ", "HNT",
        "API3", "BAND", "TRB", "ANKR",
        "NKN", "FLUX", "ROSE",
    ],
    "exchange": [
        "BNB", "OKB", "CRO", "WOO", "DYDX", "BLUR",
    ],
    "privacy": [
        "XMR", "ZEC", "DASH", "SCRT",
        "ROSE", "NYM",
    ],
    "dusk": [
        "DUSK",
    ],
    "kmno": [
        "KMNO",
    ],
}

# ─────────────────────────────────────────────
#  Sector Contagion settings
# ─────────────────────────────────────────────
# A coin must pump at least this % in one check to trigger a contagion alert
CONTAGION_THRESHOLD      = 2.0   # % single-candle pump to fire contagion
# How far back to look when counting sector pump momentum
CONTAGION_WINDOW_MINUTES = 30    # minutes
CONTAGION_COOLDOWN_MINUTES = 15  # min gap between contagion alerts for same sector