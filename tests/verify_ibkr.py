"""
Read-only verification run against a real Interactive Brokers account.

    python -m tests.verify_ibkr

Sends NO orders. It reads the session, accounts, ledger, summary, positions and
contracts, and runs a *whatif* — IBKR's own non-binding pricing call — on an
order that is never submitted. Every request is printed before it is made, so
nothing happens that you did not watch happen.

Answers the questions in HELP-WANTED.md and prints a report to paste into an
issue. Amounts and account ids are redacted by default.

Setup, if you have not run the gateway before:

    1. Download the Client Portal Gateway from IBKR (a Java program).
    2. bin/run.sh root/conf.yaml     (or bin\\run.bat root\\conf.yaml)
    3. Open https://localhost:5000 in a browser and log in. The certificate
       warning is expected — the gateway is self-signed by design.
    4. export IBKR_TLS_INSECURE=1   (or point IBKR_CACERT at its certificate)
    5. python -m tests.verify_ibkr

A paper account works and is the better choice. Paper account ids start DU.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard.brokers.ibkr import IbkrBroker, IbkrError, IbkrNotVerified

SAFE_KEYS = ("currency", "assetClass", "secType", "orderType", "side", "tif",
             "symbol", "ticker", "description", "companyHeader", "conid",
             "status", "order_ref", "orderId", "warn", "error", "key",
             "authenticated", "connected", "competing", "outsideRTH")


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
    except IbkrNotVerified as e:
        print(f"    UNRESOLVED (expected): {str(e)[:180]}")
        report[n] = {"status": "unresolved", "detail": str(e)[:400]}
        return None
    except IbkrError as e:
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
    ap.add_argument("--show-amounts", action="store_true",
                    help="Do not redact numbers and long strings in the report")
    ap.add_argument("--base-url", default=None,
                    help="Override the gateway URL (default https://localhost:5000/v1/api)")
    args = ap.parse_args()

    print("=" * 70)
    print(" IBKR adapter verification — READ ONLY, no orders are submitted")
    print("=" * 70)

    broker = IbkrBroker(base_url=args.base_url)
    print(f"\n  Gateway: {broker.base}")
    print(f"  Environment as this adapter would label it: {broker.environment_label()}")
    if broker.environment_label() == "LIVE":
        print("\n  This looks like a LIVE account. The script still submits nothing:")
        print("  it reads, and it calls whatif, which IBKR documents as non-binding.")
        print("  A paper account (id starting DU) answers every question here just")
        print("  as well, if you would rather use one.\n")

    report, keep = {}, args.show_amounts

    session = step(0, "POST /iserver/auth/status — is this gateway logged in?",
                   broker.auth_status, report, keep)
    if session is None:
        print("\nCannot continue without an authenticated session.")
        print(json.dumps(report, indent=2, default=str))
        return 1

    account = step("0b", "GET /portfolio/accounts — can we identify one account?",
                   broker.primary_account_id, report, keep)
    if account is None:
        print("\nCannot continue without an account id.")
        print(json.dumps(report, indent=2, default=str))
        return 1

    base = step(1, "Q1 GET /portfolio/{id}/ledger — which key holds the base currency?",
                broker.base_currency, report, keep) or "USD"

    step(2, f"Q2 GET /portfolio/{{id}}/summary — which field holds buying power? ({base})",
         lambda: broker.buying_power(base), report, keep)

    step(3, "Q3 GET /portfolio/{id}/positions/0 — are the position fields right?",
         broker.positions, report, keep)

    conid = step(4, f"Q4 GET /iserver/secdef/search — does {args.symbol!r} resolve to one contract?",
                 lambda: broker.resolve_conid(args.symbol), report, keep)

    if conid is not None:
        # A limit far below the market, so that even a mistake could not fill.
        order = broker.build_order(args.symbol, "BUY", 1, "LMT", 0.01,
                                   client_order_id="VERIFY_probe", conid=conid)
        print("\n[5] Q5 order payload this adapter would send (NOT sent):")
        print("    " + json.dumps(order, default=str)[:400])
        report[5] = {"status": "constructed", "payload": redact(order, keep)}

        step(6, "Q6 POST /iserver/account/{id}/orders/whatif — what shape is the cost?",
             lambda: broker.preview_order(order), report, keep)

    working = step(7, "Q7 GET /iserver/account/orders — do working orders carry order_ref?",
                   broker.live_orders, report, keep)
    if working is not None:
        refs = [row for row in working if "order_ref" in row]
        verdict = ("no working orders to judge from — place one by hand in TWS and "
                   "re-run" if not working else
                   f"{len(refs)} of {len(working)} rows carry order_ref")
        print(f"    -> {verdict}")
        report["7_order_ref"] = verdict

    print("\n" + "=" * 70)
    print(" Report — paste this into an issue titled 'ibkr: verification report'")
    print("=" * 70)
    print(json.dumps({"broker": "ibkr", "gateway": broker.base,
                      "questions": report}, indent=2, default=str))
    if not keep:
        print("\n(Numbers and long strings redacted. Re-run with --show-amounts if "
              "you are comfortable sharing them; field NAMES are what matters.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
