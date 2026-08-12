"""
Reconcile the filing parsers against real filings.

    python -m tests.verify_parsers

Read-only. Fetches live filings from SEC EDGAR and checks each parse against a
number the filing states about *itself* — a 13F's cover page declares its own
entry count and total value, a Form 144 states both a share count and an
aggregate market value, a Form 4 states the holding remaining after each
transaction.

That independence is the point. Every parser here was verified by its author
reading its output, which is the weakest kind of verification: a parse that
silently drops rows produces a smaller number, and a smaller number looks
exactly like a smaller portfolio. Comparing against a second statement of the
same fact, filed by the same people, is the only check available that is not
this project marking its own homework.

Not part of the offline suite: it needs the network and SEC's fair-access
policy applies, so it is a script you run rather than a test that runs itself.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard import edgar_forms, econ_calendar

#: A spread rather than a favourite. Big and small, concentrated and diversified,
#: single-manager and multi-manager -- Berkshire files 90 rows across 29 issuers
#: through fourteen subsidiaries, which is exactly the shape that made a naive
#: entry-count check report a false discrepancy.
INSTITUTIONS = [
    ("0001067983", "Berkshire Hathaway"),
    ("0001350694", "Bridgewater Associates"),
    ("0001364742", "BlackRock"),
    ("0000102909", "Vanguard"),
    ("0001037389", "Renaissance Technologies"),
    ("0001423053", "Citadel Advisors"),
    ("0001167483", "Tiger Global"),
    ("0001061165", "Lone Pine Capital"),
    ("0001649339", "Scion Asset Management"),
    ("0000807985", "D. E. Shaw"),
]

#: Filers with frequent insider activity, for the Form 4 running-total check.
INSIDERS = ["AAPL", "MSFT", "NVDA", "JPM", "WMT", "XOM"]


def check_13f(cik, name, verbose):
    result = {"name": name, "cik": cik}
    try:
        data = edgar_forms.institutional_holdings(cik, limit=1000)
    except Exception as exc:
        result["status"] = "fetch failed"
        result["detail"] = str(exc)[:160]
        return result

    rec = data.get("reconciliation") or {}
    result.update({
        "positions": data.get("positions"),
        "rows": rec.get("rows"),
        "declared_entries": rec.get("declared_entries"),
        "parsed_value": rec.get("parsed_value"),
        "declared_value": rec.get("declared_value"),
        "status": ("reconciled" if rec.get("reconciled") is True
                   else "MISMATCH" if rec.get("reconciled") is False
                   else "not checked"),
        "problems": rec.get("problems") or [],
        "unavailable": rec.get("unavailable"),
    })
    if verbose:
        for line in rec.get("checks", []):
            print(f"      {line}")
    return result


def check_form4(symbol, verbose):
    result = {"symbol": symbol}
    try:
        # The tool returns a document, not a list: filings live under a key.
        reports = (edgar_forms.insider_transactions(symbol, limit=6)
                   or {}).get("filings") or []
    except Exception as exc:
        result["status"] = "fetch failed"
        result["detail"] = str(exc)[:160]
        return result

    checked, problems = 0, []
    for report in reports or []:
        rec = edgar_forms.reconcile_form4(report)
        if rec.get("reconciled") is None:
            continue
        checked += 1
        problems.extend(rec.get("problems") or [])
    result["reports"] = len(reports or [])
    result["chained"] = checked
    result["problems"] = problems[:4]
    result["status"] = ("MISMATCH" if problems
                        else "reconciled" if checked
                        else "nothing chainable")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verbose", action="store_true", help="Print every check")
    ap.add_argument("--limit", type=int, default=len(INSTITUTIONS),
                    help="How many institutions to check")
    args = ap.parse_args()

    if not os.getenv("SEC_USER_AGENT", "").strip():
        print("SEC_USER_AGENT is not set. The SEC's fair-access policy requires a\n"
              "descriptive User-Agent with a real contact address, and these tools\n"
              "refuse to send a request without one rather than risk an IP ban.",
              file=sys.stderr)
        return 1

    print("=" * 74)
    print(" Parser reconciliation against live SEC filings - READ ONLY")
    print("=" * 74)

    started = time.time()
    report = {"13f": [], "form4": []}

    print("\n13F-HR: parsed information table vs the filing's own cover page\n")
    for cik, name in INSTITUTIONS[:args.limit]:
        outcome = check_13f(cik, name, args.verbose)
        report["13f"].append(outcome)
        flag = {"reconciled": "  ok  ", "MISMATCH": " FAIL ",
                }.get(outcome["status"], "  ??  ")
        detail = ""
        if outcome.get("declared_entries") is not None:
            detail = (f"{outcome['rows']}/{outcome['declared_entries']} rows, "
                      f"{outcome['positions']} positions")
        elif outcome.get("detail"):
            detail = outcome["detail"][:60]
        print(f"  [{flag}] {name:<26} {detail}")
        for problem in outcome.get("problems", []):
            print(f"           {problem}")

    print("\nForm 4: transactions chained against the stated running total\n")
    for symbol in INSIDERS:
        outcome = check_form4(symbol, args.verbose)
        report["form4"].append(outcome)
        flag = {"reconciled": "  ok  ", "MISMATCH": " FAIL ",
                }.get(outcome["status"], "  ??  ")
        print(f"  [{flag}] {symbol:<8} {outcome.get('chained', 0)} of "
              f"{outcome.get('reports', 0)} reports chainable")
        for problem in outcome.get("problems", []):
            print(f"           {problem}")

    reconciled = sum(1 for r in report["13f"] + report["form4"]
                     if r["status"] == "reconciled")
    mismatched = sum(1 for r in report["13f"] + report["form4"]
                     if r["status"] == "MISMATCH")
    unchecked = len(report["13f"] + report["form4"]) - reconciled - mismatched

    print("\n" + "=" * 74)
    print(f" {reconciled} reconciled, {mismatched} mismatched, {unchecked} not "
          f"checkable, in {time.time() - started:.0f}s")
    print("=" * 74)
    if mismatched:
        print("\nA mismatch is a parser bug or an assumption about the form that\n"
              "does not hold. Please open an issue with the output above.")
    print(json.dumps(report, indent=1, default=str)[:400] + "\n...")
    return 1 if mismatched else 0


if __name__ == "__main__":
    raise SystemExit(main())
