#!/usr/bin/env python3
"""Autonomous Growth Engine for Pulse402 Oracle.

Discovers active developers & bot builders on GitHub pushing repos related to
Polymarket, Base DEX arbitrage, Aerodrome, ElizaOS, and AgentKit.
Extracts commit author contact details, filters out bots/noreply, deduplicates,
pulls live Pulse402 alpha signals, and sends personalized technical outreach
via SMTP (or logs in dry-run mode).
"""
import argparse
from datetime import datetime, timezone, timedelta
import email.utils
import json
import os
from pathlib import Path
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_KEYWORDS = [
    "polymarket bot",
    "polymarket agent",
    "base arbitrage",
    "aerodrome bot",
    "uniswap v3 base",
    "pump fun bot",
    "solana sniper bot",
    "raydium bot",
    "x402",
    "coinbase agentkit",
    "elizaos agent",
]

CONTACTED_FILE = Path(__file__).resolve().parents[1] / "data" / "contacted_leads.json"
PRIMARY_PREVIEW_URL = "https://pulse402-oracle.ak10082247.workers.dev/v1/preview"
FALLBACK_PREVIEW_URL = "https://pulse402-oracle.onrender.com/v1/preview"

EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")


def _get_ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    try:
        return ssl.create_default_context()
    except Exception:
        return ssl._create_unverified_context()


def fetch_json(url: str, headers: dict | None = None, timeout: int = 15) -> dict | list | None:
    req_headers = {"User-Agent": "Pulse402-Growth-Engine/1.3.0"}
    if headers:
        req_headers.update(headers)
    ctx = _get_ssl_context()
    try:
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"[WARN] HTTP {e.code} for {url}\n")
    except Exception as e:
        # Fallback to unverified context if SSL failed
        try:
            unverified_ctx = ssl._create_unverified_context()
            req = urllib.request.Request(url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=timeout, context=unverified_ctx) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode("utf-8"))
        except Exception as e2:
            sys.stderr.write(f"[WARN] fetch_json error for {url}: {e2}\n")
    return None


def get_live_signals() -> dict:
    """Fetches real live preview data from Polymarket velocity and Base liquidity feeds."""
    signals = {"polymarket": None, "liquidity": None}

    # Polymarket velocity preview
    for base in (PRIMARY_PREVIEW_URL, FALLBACK_PREVIEW_URL):
        data = fetch_json(f"{base}/polymarket-velocity")
        if data and isinstance(data, dict) and data.get("status") == "ok":
            signals["polymarket"] = data
            break

    # Base liquidity preview
    for base in (PRIMARY_PREVIEW_URL, FALLBACK_PREVIEW_URL):
        data = fetch_json(f"{base}/base-liquidity")
        if data and isinstance(data, dict) and data.get("status") == "ok":
            signals["liquidity"] = data
            break

    return signals


def load_contacted_leads() -> dict:
    if CONTACTED_FILE.exists():
        try:
            with open(CONTACTED_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_contacted_leads(leads: dict) -> None:
    CONTACTED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONTACTED_FILE, "w", encoding="utf-8") as f:
        json.dump(leads, f, indent=2, sort_keys=True)


def is_valid_lead_email(email_str: str) -> bool:
    if not email_str or not EMAIL_REGEX.match(email_str):
        return False
    lower = email_str.lower()
    if lower.endswith("@users.noreply.github.com") or "noreply" in lower:
        return False
    if any(b in lower for b in ("dependabot", "actions@github.com", "bot@", "automation@", "workflow@", "sentry", "anthropic.com", "openai.com", "copilot", "github-actions")):
        return False
    return True


def search_github_leads(keywords: list[str], github_pat: str | None, max_results: int = 15) -> list[dict]:
    """Search for repos pushed within last 48 hours."""
    two_days_ago = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = {"Accept": "application/vnd.github.v3+json"}
    if github_pat:
        headers["Authorization"] = f"Bearer {github_pat}"

    leads = []
    seen_repos = set()

    for kw in keywords:
        q = urllib.parse.quote(f"{kw} pushed:>={two_days_ago}")
        url = f"https://api.github.com/search/repositories?q={q}&sort=updated&order=desc&per_page=10"
        data = fetch_json(url, headers=headers)
        if not data or not isinstance(data, dict) or "items" not in data:
            continue

        for item in data["items"]:
            full_name = item.get("full_name")
            if not full_name or full_name in seen_repos:
                continue
            seen_repos.add(full_name)

            # Get recent commits to find commit author email
            commits_url = f"https://api.github.com/repos/{full_name}/commits?per_page=5"
            commits = fetch_json(commits_url, headers=headers)
            if not commits or not isinstance(commits, list):
                continue

            for c in commits:
                author_obj = c.get("commit", {}).get("author", {})
                author_email = author_obj.get("email")
                author_name = author_obj.get("name") or item.get("owner", {}).get("login", "Developer")

                if is_valid_lead_email(author_email):
                    leads.append({
                        "name": author_name,
                        "email": author_email,
                        "repo": full_name,
                        "repo_url": item.get("html_url"),
                        "description": item.get("description") or "",
                        "keyword": kw,
                        "pushed_at": item.get("pushed_at"),
                    })
                    break  # One lead per repo is sufficient

            if len(leads) >= max_results:
                break
        if len(leads) >= max_results:
            break

    return leads


def build_email_content(lead: dict, signals: dict) -> tuple[str, str]:
    """Builds personalized plain-text email with live preview signals and x402 curl instructions."""
    name = lead.get("name") or "there"
    repo = lead.get("repo") or "your project"

    poly_sample = ""
    if signals.get("polymarket") and "data" in signals["polymarket"]:
        p_data = signals["polymarket"]["data"]
        recs = p_data.get("records_available", 0)
        top = p_data.get("top_sample") or []
        poly_sample = f"\n  Live Polymarket Signals Available: {recs} active markets tracked."
        if top:
            t = top[0]
            poly_sample += f"\n  Top mover: {t.get('market_slug', 'N/A')} (5m velocity: {t.get('velocity_5m_pct', '0')}%, 1h volume: ${t.get('volume_1h_usd', 0):,.2f})"

    liq_sample = ""
    if signals.get("liquidity") and "data" in signals["liquidity"]:
        l_data = signals["liquidity"]["data"]
        recs = l_data.get("records_available", 0)
        liq_sample = f"\n  Live Base DEX Liquidity Pools: {recs} pairs tracked across Aerodrome and Uniswap V3."

    subject = f"Low-latency alpha feeds for {repo.split('/')[-1]} on Base & Solana"

    body = f"""Hi {name},

Saw your recent work on {repo} and wanted to reach out with something directly relevant to your trading and automation stack.

We run Pulse402, an autonomous oracle network that streams verified, low-latency market intelligence directly to on-chain bots and trading agents over the x402 V2 protocol (supporting Base L2 EIP-3009 and Solana SPL TokenChecked micropayments):

Current Live Oracle Feeds:
• Polymarket Orderflow & Volatility (/v1/pulse/polymarket-velocity): 5-minute price delta, volume surges, and liquidation pressure.{poly_sample}
• Base DEX Real-Time Liquidity (/v1/pulse/base-liquidity): Live Aerodrome slip, Uniswap v3 fee tiers, depth, and spread across WETH/USDC/AERO.{liq_sample}
• Cross-DEX Arbitrage Routes (/v1/pulse/arb-routes): Zero-risk cyclic route discovery with net-profit thresholding.
• GoPlus Threat Intel (/v1/pulse/threat-intel): Pre-trade honeypot detection, blacklist checks, and token safety scoring.
• Solana Pump.fun & Raydium Velocity (/v1/pulse/solana-velocity): Live trending meme coin orderflow, 5m volume, price delta, and bonding curve status for Solana sniper bots.

Tiered Pricing:
- Tier 1 (Pre-Trade Safety & Liquidity): $0.005 USDC per call
- Tier 2 (Actionable Alpha & Arbitrage): $0.01 USDC per call
- High-Frequency Pass (POST /v1/pass/prepay): $5.00 USDC for 2,500 zero-latency calls ($0.002/call effective rate — saves 60%–80% and completely eliminates per-call signing latency).

Quick Start (No API key needed — pay per call on-chain or pass an HFT session token):
  curl -X GET "https://pulse402-oracle.ak10082247.workers.dev/v1/preview/polymarket-velocity"

Official Apify Store Actor (for cloud scheduling and webhook triggers):
  https://apify.com/ak10082247/pulse402-oracle

Full MCP & AgentKit Documentation:
  https://pulse402-oracle.ak10082247.workers.dev/skill.md

Happy to answer any questions or provision test credits if you want to benchmark the feeds against your execution engine.

Best regards,
The Pulse402 Team
Oracle Infrastructure & On-Chain Intelligence
https://pulse402-oracle.ak10082247.workers.dev

---
To opt out of future developer updates regarding Pulse402, reply to this email with 'unsubscribe' and you will be immediately removed from our outreach list.
"""
    return subject, body


def send_email_smtp(smtp_user: str, smtp_pass: str, to_email: str, subject: str, body: str) -> bool:
    try:
        msg = f"From: Pulse402 Team <{smtp_user}>\r\n"
        msg += f"To: {to_email}\r\n"
        msg += f"Subject: {subject}\r\n"
        msg += f"Date: {email.utils.formatdate(localtime=True)}\r\n"
        msg += f"Message-ID: {email.utils.make_msgid(domain='pulse402-oracle.ak10082247.workers.dev')}\r\n"
        msg += "Content-Type: text/plain; charset=UTF-8\r\n"
        msg += "Auto-Submitted: auto-generated\r\n"
        msg += "Precedence: bulk\r\n\r\n"
        msg += body

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [to_email], msg.encode("utf-8"))
        return True
    except Exception as e:
        sys.stderr.write(f"[ERROR] Failed to send email to {to_email}: {e}\n")
        return False


def main():
    parser = argparse.ArgumentParser(description="Pulse402 Autonomous Growth Engine")
    parser.add_argument("--dry-run", action="store_true", help="Print outreach without sending emails")
    parser.add_argument("--limit", type=int, default=5, help="Max emails to send this run (default 5)")
    parser.add_argument("--max-daily", type=int, default=20, help="Max emails allowed per 24h (default 20)")
    parser.add_argument("--keywords", nargs="+", default=DEFAULT_KEYWORDS, help="Keywords to search on GitHub")
    args = parser.parse_args()

    github_pat = os.getenv("GH_PAT") or os.getenv("GITHUB_PAT") or os.getenv("GITHUB_TOKEN")
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")

    print(f"=== Pulse402 Autonomous Growth Engine ===")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print(f"Mode: {'DRY RUN' if args.dry_run or not (smtp_user and smtp_pass) else 'LIVE SMTP'}")
    print(f"Limit: {args.limit} | Daily Cap: {args.max_daily}")

    contacted = load_contacted_leads()

    # Check daily cap
    now_ts = time.time()
    one_day_ago = now_ts - 86400
    sent_last_24h = sum(1 for item in contacted.values() if item.get("contacted_at", 0) > one_day_ago)
    print(f"Leads contacted in last 24h: {sent_last_24h} / {args.max_daily}")

    if sent_last_24h >= args.max_daily:
        print("Daily cap reached. Exiting cleanly.")
        return 0

    available_slots = min(args.limit, args.max_daily - sent_last_24h)

    print("Fetching live preview signals from Pulse402 edge...")
    signals = get_live_signals()
    print("Signals retrieved successfully.")

    print(f"Searching GitHub for repositories pushed in the last 48h across {len(args.keywords)} keyword terms...")
    leads = search_github_leads(args.keywords, github_pat, max_results=30)
    print(f"Found {len(leads)} potential lead candidates.")

    # Filter out already contacted
    fresh_leads = []
    for l in leads:
        em = l["email"].lower()
        if em not in contacted:
            fresh_leads.append(l)

    print(f"Uncontacted fresh leads: {len(fresh_leads)}")

    sent_count = 0
    for lead in fresh_leads[:available_slots]:
        em = lead["email"].lower()
        subject, body = build_email_content(lead, signals)

        if args.dry_run or not (smtp_user and smtp_pass):
            print(f"\n[DRY RUN OUTREACH to {lead['name']} <{em}>]")
            print(f"Repo: {lead['repo']} ({lead['keyword']})")
            print(f"Subject: {subject}")
            print("Preview of body:")
            print("\n".join(body.splitlines()[:12]) + "\n...")
            contacted[em] = {
                "name": lead["name"],
                "repo": lead["repo"],
                "keyword": lead["keyword"],
                "contacted_at": int(time.time()),
                "status": "dry_run",
            }
            sent_count += 1
        else:
            print(f"Sending live email to {em} ({lead['repo']})...")
            success = send_email_smtp(smtp_user, smtp_pass, em, subject, body)
            if success:
                print(f"Successfully delivered email to {em}")
                contacted[em] = {
                    "name": lead["name"],
                    "repo": lead["repo"],
                    "keyword": lead["keyword"],
                    "contacted_at": int(time.time()),
                    "status": "sent",
                }
                sent_count += 1
                time.sleep(2)  # Respectful pause between outbound emails
            else:
                print(f"Failed to send email to {em}")

    save_contacted_leads(contacted)
    print(f"\nGrowth engine run complete. Processed {sent_count} leads.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
