# 🚀 CryptoPump Discord Bot

A Discord bot that monitors **Binance USDT pairs** by sector and sends alerts when any coin pumps or dumps by a configurable threshold (default: **2% per 60 seconds**).

---

## ✅ Features

- ✅ Monitors only **Binance** spot market (USDT pairs)
- ✅ Checks prices every **60 seconds**
- ✅ Alerts on **≥ 2% move** (configurable)
- ✅ Organized by **sector** (AI, DeFi, Gaming, L1, L2, Meme, RWA, etc.)
- ✅ Rich Discord embeds with price, % change, 24 h stats, trade link
- ✅ **Persists** your watched channels across bot restarts
- ✅ Admin command to add coins to a sector on the fly

---

## 🛠️ Setup

### 1. Create a Discord Bot

1. Go to https://discord.com/developers/applications
2. Click **New Application** → name it (e.g. `CryptoPump`)
3. Go to **Bot** tab → click **Add Bot**
4. Under **Privileged Gateway Intents** enable **Message Content Intent**
5. Copy the **Bot Token**
6. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`
   - Bot Permissions: `Send Messages`, `Embed Links`, `Read Message History`, `View Channels`
7. Open the generated URL in your browser and invite the bot to your server

### 2. Install Dependencies

```bash
# Python 3.10+ required
pip install -r requirements.txt
```

### 3. Configure the Bot

Open `config.py` and set your token **or** use an environment variable:

```bash
# Option A — edit config.py directly
DISCORD_TOKEN = "your-token-here"

# Option B — environment variable (recommended)
export DISCORD_TOKEN="your-token-here"
```

Optionally tweak:
```python
CHECK_INTERVAL = 60    # seconds between checks
PUMP_THRESHOLD = 2.0   # minimum % move to alert
```

### 4. Run the Bot

```bash
python bot.py
```

---

## 💬 Bot Commands

| Command | Description | Example |
|---|---|---|
| `!watch <sector...>` | Start watching sector(s) in this channel | `!watch ai defi` |
| `!unwatch <sector...>` | Stop watching sector(s) | `!unwatch ai` |
| `!unwatch all` | Remove all watches for this channel | `!unwatch all` |
| `!status` | Show currently watched sectors | `!status` |
| `!sectors` | List all available sectors & coins | `!sectors` |
| `!price <COIN>` | Get live Binance price | `!price BTC` |
| `!addcoin <sector> <COIN...>` | *(Admin)* Add coin(s) to a sector | `!addcoin ai RENDER` |

---

## 📂 Available Sectors

| Sector | Key Coins |
|---|---|
| `ai` | FET, AGIX, OCEAN, RNDR, WLD, TAO, ARKM… |
| `defi` | UNI, AAVE, COMP, MKR, CRV, SUSHI, GMX… |
| `gaming` | AXS, SAND, MANA, ENJ, GALA, ILV, BEAM… |
| `layer1` | ETH, SOL, ADA, AVAX, DOT, NEAR, SUI… |
| `layer2` | MATIC, OP, ARB, IMX, STRK, MANTA… |
| `meme` | DOGE, SHIB, PEPE, FLOKI, BONK, WIF… |
| `rwa` | ONDO, POLYX, CFG, PAXG, XAUT… |
| `infra` | LINK, FIL, AR, HNT, API3, BAND, FLUX… |
| `exchange` | BNB, OKB, CRO, WOO, DYDX… |
| `privacy` | XMR, ZEC, DASH, SCRT, ROSE… |

---

## 📬 Example Alert

```
🚀 PUMP DETECTED — FETUSDT
Sector: AI
💰 Old Price   $0.423100
💰 New Price   $0.434200
📊 1-min Δ    +2.63%
📈 24 h Δ     +8.41%
🏦 Exchange   Binance
🔗 Trade      [Open on Binance]
```

---

## ⚙️ Customization

**Add a brand-new sector** — edit `SECTORS` in `config.py`:
```python
"nft": ["APE", "BLUR", "X2Y2", "LOOKS"],
```

**Change the pump threshold** — in `config.py`:
```python
PUMP_THRESHOLD = 3.0  # only alert on 3%+ moves
```

**Change check frequency**:
```python
CHECK_INTERVAL = 30  # check every 30 seconds
```

---

## 📁 File Structure

```
crypto-pump-bot/
├── bot.py              # Main bot logic
├── config.py           # Token, thresholds, sector definitions
├── requirements.txt    # Python dependencies
├── .env.example        # Environment variable template
├── alert_channels.json # Auto-created — persists channel watches
└── README.md
```

---

## ❓ Troubleshooting

| Problem | Fix |
|---|---|
| Bot doesn't respond to commands | Ensure **Message Content Intent** is enabled in Discord Developer Portal |
| `!watch` says coin not found | The coin may not be listed on Binance as a USDT pair; use `!price COIN` to verify |
| No alerts firing | Make sure you ran `!watch <sector>` in the channel; check `!status` |
| Rate limit errors | Increase `CHECK_INTERVAL` to 120+ if running many sectors |
