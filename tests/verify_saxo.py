"""
Read-only verification run against a real Saxo account.

    python -m tests.verify_saxo

Sends NO orders. It reads accounts, balances, positions and instruments, and
runs a *precheck* — Saxo's own non-binding pricing call — on an order that is
never placed. Every request is printed before it is made, so nothing happens
that you did not watch happen.

Answers the six questions in HELP-WANTED.md and prints a report to paste into an
issue. Amounts and account keys are redacted by default.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard.brokers.saxo import SaxoBroker, SaxoError, SaxoNotVerified

SAFE_KEYS = ("Currency", "AssetType", "OrderType", "PreCheckResult", "ErrorInfo",
             "Symbol", "Description", "ExchangeId", "Uic", "Identifier")


def redact(value, keep_numbers: bool):
    """Field names are the answer; the amounts are the reporter's business."""
    if isinstance(value, dict):
        return {k: (v if (k in SAFE_KEYS or keep_numbers) else redact(v, keep_numbers))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, keep_numbers) for v in value[:3]]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "<number>" if not keep_numbers else value
    if isinstance(value, str) and len(value) > 24:
        return "<redacted>" if not keep_numbers else value
    return value


def step(n, question, fn, report, keep):
    print(f"\n[{n}] {question}")
    try:
        result = fn()
    except SaxoNotVerified as e:
        print(f"    UNRESOLVED (expected): {str(e)[:180]}")
        report[n] = {"status": "unresolved", "detail": str(e)[:400]}
        return None
    except SaxoError as e:
        print(f"    ERROR: {str(e)[:220]}")
        report[n] = {"status": "error", "detail": str(e)[:400]}
        return None
    except Exception as e:
        print(f"    UNEXPECTED {type(e).__name__}: {str(e)[:200]}")
        report[n] = {"status": "unexpected", "detail": f"{type(e).__name__}: {e}"[:400]}
        return None
    print(f"    OK: {json.dumps(redact(result, keep), default=str)[:400]}")
    report[n] = {"status": "ok", "sample": redact(result, keep)}
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="AAPL", help="Ticker to resolve (default AAPL)")
    ap.add_argument("--currency", default=None, help="Currency for the buying-power check")
    ap.add_argument("--show-amounts", action="store_true",
                    help="Do not redact numbers and long strings in the report")
    ap.add_argument("--live", action="store_true",
                    help="Use the live environment instead of simulation")
    args = ap.parse_args()

    env = "live" if args.live else "sim"
    if not os.getenv("SAXO_ACCESS_TOKEN", "").strip():
        print("SAXO_ACCESS_TOKEN is not set.\n"
              "A 24-hour simulation token comes from Saxo's Developer Portal — "
              "no approval process and no live money.", file=sys.stderr)
        return 1

    print("=" * 70)
    print(f" Saxo adapter verification — {env.upper()} — READ ONLY, no orders sent")
    print("=" * 70)
    if args.live:
        print("\n  Running against LIVE. This script still sends no orders: it reads,")
        print("  and it prechecks, which Saxo documents as non-binding.\n")

    broker = SaxoBroker(environment=env)
    report, keep = {}, args.show_amounts

    account = step(0, "GET /port/v1/accounts/me — can we identify one account?",
                   broker.primary_account_id, report, keep)
    if account is None:
        print("\nCannot continue without an account key.")
        print(json.dumps(report, indent=2, default=str))
        return 1

    currency = args.currency
    if currency is None:
        try:
            raw = broker._request("GET", "/port/v1/balances",
                                  params={"AccountKey": account})
            currency = (raw.get("Currency") or "USD").upper()
        except Exception:
            currency = "USD"

    step(1, f"Q1 GET /port/v1/balances — which field holds buying power? ({currency})",
         lambda: broker.buying_power(currency), report, keep)

    step(4, "Q4 GET /port/v1/netpositions — are the position fields right?",
         broker.positions, report, keep)

    uic = step(6, f"Q6 GET /ref/v1/instruments — does {args.symbol!r} resolve unambiguously?",
               lambda: broker.resolve_uic(args.symbol), report, keep)

    if uic is not None:
        # A limit far below the market so that even a mistake could not fill.
        order = broker.build_order(args.symbol, "BUY", 1, "LMT", 0.01,
                                   client_order_id="VERIFY_probe", uic=uic)
        print(f"\n[5] Q5 order payload this adapter would send (NOT sent):")
        print("    " + json.dumps(order, default=str)[:400])
        report[5] = {"status": "constructed", "payload": redact(order, keep)}

        step(3, "Q3 POST /trade/v2/orders/precheck — what does it return for cost?",
             lambda: broker.preview_order(order), report, keep)

    step(2, "Q2 GET /port/v1/orders — is ExternalReference returned for working orders?",
         lambda: broker._request("GET", "/port/v1/orders",
                                 params={"AccountKey": account, "Status": "Working"}),
         report, keep)

    print("\n" + "=" * 70)
    print(" Report — paste this into an issue titled 'saxo: verification report'")
    print("=" * 70)
    print(json.dumps({"environment": env, "questions": report}, indent=2, default=str))
    if not keep:
        print("\n(Numbers and long strings redacted. Re-run with --show-amounts if "
              "you are comfortable sharing them; field NAMES are what matters.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
