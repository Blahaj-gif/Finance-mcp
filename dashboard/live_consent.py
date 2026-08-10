"""
A one-time acknowledgement before the first live order from this install.

Deliberately narrow. Reads are not gated and should not be: the reason to run
this tool at all is your real portfolio and real quotes, and simulated cash and
sandbox data answer a question nobody asked. Pointing the client at paper does
not give you "live data with simulated orders" either -- it repoints the whole
client, quotes included, at a separate deployment that production credentials
cannot authenticate against.

So the gate sits on the only action that spends money, and it fires once. The
failure mode it addresses is narrow but real: someone who has used the tool for
research for weeks, clicks Approve for the first time, and has not registered
that the account behind it is theirs. Naming the account number, its currency
and its buying power once is a speed bump exactly where the risk is, and never
again.

Consent is per account_id: a second account is a different pile of money.
"""
import datetime
import json
import os
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONSENT_FILE = os.getenv("FINMCP_LIVE_CONSENT_FILE",
                         os.path.join(BASE_DIR, "dashboard", "live_consent.json"))

_LOCK = threading.RLock()


def _load(path=None) -> dict:
    path = path or CONSENT_FILE
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read().strip()
        return json.loads(content) if content else {}
    except Exception:
        # A record of past consent. Unreadable means not granted, which fails
        # towards asking again rather than towards assuming yes.
        return {}


def has_consented(account_id: str, path=None) -> bool:
    if not account_id:
        return False
    with _LOCK:
        return bool(_load(path).get(str(account_id), {}).get("granted_at"))


def grant(account_id: str, detail: str = "", path=None) -> dict:
    """Record consent for one account. Returns the stored record."""
    if not account_id:
        raise ValueError("Consent needs an account id; it is granted per account.")
    path = path or CONSENT_FILE
    record = {"granted_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "detail": detail}
    with _LOCK:
        data = _load(path)
        data[str(account_id)] = record
        tmp = path + ".tmp"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    return record


def revoke(account_id: str, path=None) -> bool:
    """Withdraw consent, so the next live order asks again."""
    path = path or CONSENT_FILE
    with _LOCK:
        data = _load(path)
        if str(account_id) not in data:
            return False
        data.pop(str(account_id))
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
        return True


def granted_at(account_id: str, path=None):
    with _LOCK:
        return _load(path).get(str(account_id), {}).get("granted_at")
