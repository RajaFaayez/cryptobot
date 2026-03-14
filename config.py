import os

# ─────────────────────────────────────────────
#  REQUIRED — fill these in before running
# ─────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")

# ─────────────────────────────────────────────
#  Alert settings
# ─────────────────────────────────────────────
CHECK_INTERVAL = 60      # seconds between each price check
PUMP_THRESHOLD = 1.0     # minimum % change to trigger an alert

# ─────────────────────────────────────────────
#  Sectors → Binance base-asset symbols (no USDT suffix)
#  Add / remove coins freely.
# ─────────────────────────────────────────────
SECTORS = {
    "ai": [
        "FET", "AGIX", "OCEAN", "RNDR", "NMR",
        "GRT", "AKT", "WLD", "TAO", "ARKM",
        "AIOZ", "PHB", "CTXC", "DBC", "VELO",
    ],
    "defi": [
        "UNI", "AAVE", "COMP", "MKR", "SNX",
        "CRV", "SUSHI", "YFI", "BAL", "1INCH",
        "DYDX", "GMX", "CAKE", "RUNE", "LDO",
        "CVX", "FXS", "SPELL", "ALCX", "BOND",
    ],
    "gaming": [
        "AXS", "SAND", "MANA", "ENJ", "GALA",
        "ILV", "SLP", "ALICE", "TLM", "VOXEL",
        "MC", "PYR", "HERO", "MAGIC", "YGG",
        "BEAM", "GHST", "UFO", "NAKA", "MOBOX",
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
        "ZKJ", "MANTA", "SCROLL", "BASE", "ZKEVM",
    ],
    "meme": [
        "DOGE", "SHIB", "PEPE", "FLOKI", "BONK",
        "WIF", "NEIRO", "MOG", "TURBO", "MEME",
        "BABYDOGE", "ELON", "SAMO", "CHEEMS", "VOLT",
    ],
    "rwa": [
        "ONDO", "POLYX", "CFG", "RIO", "MPL",
        "TRU", "CRED", "GOLDX", "PAXG", "XAUT",
    ],
    "infra": [
        "LINK", "FIL", "AR", "STORJ", "HNT",
        "API3", "BAND", "DIA", "TRB", "ANKR",
        "NKN", "FLUX", "STRAX", "XDC", "ROSE",
    ],
    "exchange": [
        "BNB", "OKB", "CRO", "HT", "KCS",
        "GT", "MX", "WOO", "DYDX", "BLUR",
    ],
    "privacy": [
        "XMR", "ZEC", "DASH", "SCRT", "KEEP",
        "ROSE", "RAIL", "TORN", "NYM", "DERO",
    ],
}
