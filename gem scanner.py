import requests
import time
import datetime
from langchain_groq import ChatGroq
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser

# ── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
CHAT_ID = "YOUR_CHAT_ID_HERE"
GROQ_API_KEY = "YOUR_GROQ_API_KEY_HERE"

# ── FILTERS (tweak these to your taste) ──────────────────────────────────────
MIN_LIQUIDITY = 10000        # Minimum liquidity in USD
MIN_VOLUME_24H = 50000       # Minimum 24h volume in USD
MIN_PRICE_CHANGE = 20        # Minimum % price increase in last 24h
MAX_MARKET_CAP = 5000000     # Maximum market cap (avoid already pumped coins)
MIN_HOLDERS = 100            # Minimum number of holders (cuts out most rugs)
MIN_BUY_SELL_RATIO = 1.2     # Buys must be at least 1.2x more than sells
MAX_TOKEN_AGE_HOURS = 24     # Only alert on coins under 24 hours old
DAILY_SUMMARY_HOUR = 8       # Send daily summary at 8AM
SCAN_INTERVAL = 300          # Scan every 5 minutes (300 seconds)

# ── LLM SETUP ─────────────────────────────────────────────────────────────────
llm = ChatGroq(
    model_name="llama-3.3-70b-versatile",
    api_key=GROQ_API_KEY
)
parser = StrOutputParser()

prompt_template = """
You are a memecoin analyst. A new token just appeared on DexScreener with these metrics:

Token: {name} ({symbol}) on {chain}
Price: ${price}
Market Cap: ${market_cap}
24H Volume: ${volume}
Liquidity: ${liquidity}
Holders: {holders}
Price Change 5M: {change_5m}%
Price Change 1H: {change_1h}%
Price Change 24H: {change_24h}%
Buys (24H): {buys}
Sells (24H): {sells}
Buy/Sell Ratio: {buy_sell_ratio}
Age: {age}
Liquidity Locked: {liq_locked}
Contract Verified: {contract_verified}

In 5 bullet points, give a quick gem assessment:
- Overall signal (bullish/bearish/neutral)
- Key strength of this token
- Biggest red flag
- Buy pressure analysis
- Short verdict: PASS or SKIP and why

Be direct and concise. No fluff.
"""

my_chain = PromptTemplate(
    input_variables=["name", "symbol", "chain", "price", "market_cap",
                     "volume", "liquidity", "holders", "change_5m", "change_1h",
                     "change_24h", "buys", "sells", "buy_sell_ratio", "age",
                     "liq_locked", "contract_verified"],
    template=prompt_template
) | llm | parser


# ── TELEGRAM SENDER ───────────────────────────────────────────────────────────
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Telegram error: {e}")


# ── RUG PULL CHECK ────────────────────────────────────────────────────────────
def check_rug_safety(token_address: str) -> dict:
    """Check RugCheck API for basic safety signals."""
    try:
        url = f"https://api.rugcheck.xyz/v1/tokens/{token_address}/report/summary"
        response = requests.get(url, timeout=8)
        if response.status_code == 200:
            data = response.json()
            score = data.get("score", 0)
            risks = data.get("risks", [])
            risk_names = [r.get("name", "") for r in risks]

            # Score above 500 = good, below = risky on RugCheck
            is_safe = score >= 500
            liq_locked = "✅ Yes" if "Liquidity not locked" not in risk_names else "❌ No"
            contract_verified = "✅ Yes" if "Unverified contract" not in risk_names else "⚠️ No"

            return {
                "safe": is_safe,
                "score": score,
                "liq_locked": liq_locked,
                "contract_verified": contract_verified,
                "risks": risk_names
            }
    except Exception as e:
        print(f"RugCheck error: {e}")

    return {
        "safe": True,  # Default to true if API fails (don't block on API failure)
        "score": "N/A",
        "liq_locked": "⚠️ Unknown",
        "contract_verified": "⚠️ Unknown",
        "risks": []
    }


# ── TOKEN AGE CHECK ───────────────────────────────────────────────────────────
def get_token_age_hours(created_at) -> float:
    """Return token age in hours."""
    try:
        if not created_at:
            return 999  # Unknown age — skip it
        created_dt = datetime.datetime.fromtimestamp(created_at / 1000)
        age_hours = (datetime.datetime.now() - created_dt).total_seconds() / 3600
        return age_hours
    except:
        return 999


def format_age(created_at) -> str:
    """Return human readable age string."""
    try:
        created_dt = datetime.datetime.fromtimestamp(created_at / 1000)
        delta = datetime.datetime.now() - created_dt
        hours = int(delta.total_seconds() // 3600)
        minutes = int((delta.total_seconds() % 3600) // 60)
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    except:
        return "Unknown"


# ── DEXSCREENER FETCH ─────────────────────────────────────────────────────────
def get_new_solana_tokens():
    url = "https://api.dexscreener.com/token-profiles/latest/v1"
    try:
        response = requests.get(url, timeout=10)
        data = response.json()
        tokens = []
        for item in data:
            if item.get("chainId", "").lower() == "solana":
                tokens.append(item.get("tokenAddress"))
        return tokens
    except Exception as e:
        print(f"DexScreener fetch error: {e}")
        return []


def get_token_details(token_address: str):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        response = requests.get(url, timeout=10)
        data = response.json()
        pairs = data.get("pairs", [])
        if not pairs:
            return None
        pair = sorted(pairs, key=lambda x: x.get("volume", {}).get("h24", 0), reverse=True)[0]
        return pair
    except Exception as e:
        print(f"Token detail error: {e}")
        return None


# ── FILTER CHECK ──────────────────────────────────────────────────────────────
def passes_filter(pair: dict) -> bool:
    try:
        liquidity = pair.get("liquidity", {}).get("usd", 0) or 0
        volume = pair.get("volume", {}).get("h24", 0) or 0
        price_change = pair.get("priceChange", {}).get("h24", 0) or 0
        market_cap = pair.get("marketCap", 0) or 0
        buys = pair.get("txns", {}).get("h24", {}).get("buys", 0) or 0
        sells = pair.get("txns", {}).get("h24", {}).get("sells", 1) or 1
        holders = pair.get("info", {}).get("holders", 0) or 0
        created_at = pair.get("pairCreatedAt")

        # Age check
        age_hours = get_token_age_hours(created_at)
        if age_hours > MAX_TOKEN_AGE_HOURS:
            print(f"  ❌ Skipped — too old ({age_hours:.1f} hours)")
            return False

        if liquidity < MIN_LIQUIDITY:
            return False
        if volume < MIN_VOLUME_24H:
            return False
        if price_change < MIN_PRICE_CHANGE:
            return False
        if market_cap > MAX_MARKET_CAP:
            return False
        if holders < MIN_HOLDERS:
            print(f"  ❌ Skipped — only {holders} holders")
            return False
        if sells > 0 and (buys / sells) < MIN_BUY_SELL_RATIO:
            print(f"  ❌ Skipped — weak buy pressure ({buys} buys / {sells} sells)")
            return False
        return True
    except:
        return False


# ── FORMAT & ANALYZE ──────────────────────────────────────────────────────────
def analyze_and_alert(pair: dict, daily_log: list):
    try:
        base = pair.get("baseToken", {})
        name = base.get("name", "Unknown")
        symbol = base.get("symbol", "???")
        token_address = base.get("address", "")
        chain = pair.get("chainId", "unknown").upper()
        price = pair.get("priceUsd", "0")
        market_cap = pair.get("marketCap", 0)
        volume = pair.get("volume", {}).get("h24", 0)
        liquidity = pair.get("liquidity", {}).get("usd", 0)
        change_5m = pair.get("priceChange", {}).get("m5", 0)
        change_1h = pair.get("priceChange", {}).get("h1", 0)
        change_24h = pair.get("priceChange", {}).get("h24", 0)
        buys = pair.get("txns", {}).get("h24", {}).get("buys", 0)
        sells = pair.get("txns", {}).get("h24", {}).get("sells", 0)
        holders = pair.get("info", {}).get("holders", 0)
        buy_sell_ratio = round(buys / sells, 2) if sells > 0 else "∞"
        pair_url = pair.get("url", "")
        created_at = pair.get("pairCreatedAt")
        age = format_age(created_at)

        # Rug check
        print(f"🔍 Running rug check for {name}...")
        safety = check_rug_safety(token_address)

        # Skip if clearly unsafe
        if not safety["safe"] and safety["score"] != "N/A":
            print(f"  🚨 Skipped — RugCheck score too low ({safety['score']})")
            return

        liq_locked = safety["liq_locked"]
        contract_verified = safety["contract_verified"]
        rug_score = safety["score"]
        risks = safety["risks"]

        print(f"💎 Analyzing {name} ({symbol})...")

        ai_analysis = my_chain.invoke({
            "name": name,
            "symbol": symbol,
            "chain": chain,
            "price": price,
            "market_cap": f"{market_cap:,.0f}",
            "volume": f"{volume:,.0f}",
            "liquidity": f"{liquidity:,.0f}",
            "holders": holders,
            "change_5m": change_5m,
            "change_1h": change_1h,
            "change_24h": change_24h,
            "buys": buys,
            "sells": sells,
            "buy_sell_ratio": buy_sell_ratio,
            "age": age,
            "liq_locked": liq_locked,
            "contract_verified": contract_verified
        })

        # Format risk list
        risk_text = "\n".join([f"  ⚠️ {r}" for r in risks]) if risks else "  ✅ No major risks detected"

        message = f"""
💎 *GEM ALERT — {name} (${symbol})*

📊 *Metrics*
• Price: ${price}
• Market Cap: ${market_cap:,.0f}
• 24H Volume: ${volume:,.0f}
• Liquidity: ${liquidity:,.0f}
• Holders: {holders}
• Age: {age}

📈 *Price Changes*
• 5 Min: {change_5m}%
• 1 Hour: {change_1h}%
• 24 Hour: {change_24h}%

🔄 *Transactions (24H)*
• Buys: {buys} | Sells: {sells}
• Buy/Sell Ratio: {buy_sell_ratio}x

🛡️ *Safety Check (RugCheck)*
• Score: {rug_score}/1000
• Liquidity Locked: {liq_locked}
• Contract Verified: {contract_verified}
• Risks:
{risk_text}

🤖 *AI Analysis*
{ai_analysis}

🔗 [View on DexScreener]({pair_url})

⚠️ _DYOR — Not financial advice_
"""
        send_telegram(message)
        print(f"✅ Alert sent for {name} ({symbol})")

        # Log to daily summary
        daily_log.append({
            "name": name,
            "symbol": symbol,
            "price": price,
            "change_24h": change_24h,
            "market_cap": market_cap,
            "url": pair_url,
            "time": datetime.datetime.now().strftime("%H:%M"),
            "rug_score": rug_score
        })

    except Exception as e:
        print(f"Analysis error: {e}")


# ── DAILY SUMMARY ─────────────────────────────────────────────────────────────
def send_daily_summary(daily_log: list):
    if not daily_log:
        message = f"""
📋 *Daily Gem Summary — {datetime.date.today()}*

No gems were found today. Filters held strong 💪
Scanning again today...
"""
    else:
        lines = ""
        for i, coin in enumerate(daily_log, 1):
            lines += f"{i}. *{coin['name']}* (${coin['symbol']}) — alerted at {coin['time']}\n"
            lines += f"   Price: ${coin['price']} | 24H: {coin['change_24h']}% | Safety: {coin['rug_score']}/1000\n"
            lines += f"   🔗 {coin['url']}\n\n"

        message = f"""
📋 *Daily Gem Summary — {datetime.date.today()}*

Found *{len(daily_log)} potential gem(s)* in the last 24 hours:

{lines}
Keep watching these and DYOR 👀
⚠️ _Not financial advice_
"""

    send_telegram(message)
    print("📋 Daily summary sent!")


# ── MAIN SCANNER LOOP ─────────────────────────────────────────────────────────
def main():
    print("🚀 Gem Scanner started!")
    print(f"📡 Scanning Solana every {SCAN_INTERVAL // 60} minutes...")
    print(f"📋 Daily summary will be sent at {DAILY_SUMMARY_HOUR}:00 AM")
    send_telegram("🚀 *Gem Scanner is now active!*\nScanning Solana for fresh gems under 24h old 👀\nDaily summary drops at 8AM every day 📋")

    seen_tokens = set()
    daily_log = []
    last_summary_date = None

    while True:
        now = datetime.datetime.now()

        # Send daily summary at configured hour
        if now.hour == DAILY_SUMMARY_HOUR and now.date() != last_summary_date:
            send_daily_summary(daily_log)
            daily_log = []  # Reset log after summary
            last_summary_date = now.date()

        print(f"\n🔍 Scanning DexScreener... [{now.strftime('%H:%M:%S')}]")
        token_addresses = get_new_solana_tokens()

        for address in token_addresses:
            if address in seen_tokens:
                continue

            seen_tokens.add(address)
            pair = get_token_details(address)

            if pair and passes_filter(pair):
                analyze_and_alert(pair, daily_log)
                time.sleep(3)

        print(f"⏳ Next scan in {SCAN_INTERVAL // 60} minutes...")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
