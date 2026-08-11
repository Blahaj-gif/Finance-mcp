"""
What a broker can actually do here, as opposed to what its SDK offers.

Two facts forced this to exist rather than be a table keyed on broker name:

  * **Eight tools worked only with Webull.** They called its SDK directly, so
    configuring Saxo or IBKR left them failing with a missing-Webull-key error
    about a broker the user had not configured. Advertising a tool that cannot
    run is the thing to stop.

  * **"Webull" is twelve brokers, and an account is not its broker.** Webull
    runs independent regional entities on separate hosts (`api.webull.com`,
    `api.webull.co.th`, `api.webull.hk`, …) which is a fact about the endpoint
    table. What any one account may *call* is a third thing again, below the
    SDK and below the entity: market-data entitlements are bought, so an order
    book capped at one level means an L1 subscription, not a missing endpoint.

    That distinction was learned by getting it wrong here. A probe refused
    options with `UNSUPPORTED_CATEGORY` and this module claimed the region did
    not serve them; the categories had in fact been passed as strings the SDK
    could not parse into its `Category` enum, and a later attempt to redo it
    properly failed on token state instead. **The question is open.** What
    survives is the rule, not the anecdote:

        capability = what the SDK implements
                   ∩ what the entity serves
                   ∩ what this account is entitled to

    A probe reports **what** a call did, never **why**. Reading a cause out of
    an error message is how the wrong conclusion got written down in the first
    place, so nothing here records one.

So capability is keyed on **broker × entity × account** — the account being
where entitlement lives — and the honest way to fill it in is to call the API
rather than assert from a name.

Two sources, in order of authority:

  1. **Declared** — the adapter says which capabilities it implements. Cheap,
     available at import, and never wrong about the code. It can be wrong about
     the account, which is the whole point above.
  2. **Probed** — read-only calls made once against the real account and cached
     to disk. Only ever *removes* capability: a probe that refuses is evidence,
     a probe that was never run is not evidence of anything.

Registration reads the cache; it never makes a network call at import, because a
server that needs the internet to list its tools is a server that fails to start
on a train.
"""
import hashlib
import json
import os
import tempfile
import time

try:
    from dashboard.envfile import user_config_dir
except ImportError:  # imported as a top-level module from dashboard/
    from envfile import user_config_dir

# The vocabulary. A capability is named for the question a user asks, not for
# the endpoint that answers it, so the same name survives a broker changing its
# API generation underneath.
ACCOUNTS = "accounts"
POSITIONS = "positions"
BUYING_POWER = "buying_power"
OPEN_ORDERS = "open_orders"
PREVIEW_ORDER = "preview_order"
PLACE_ORDER = "place_order"
CANCEL_ORDER = "cancel_order"
OPTIONS_CHAIN = "options_chain"
HISTORY_BARS = "history_bars"
CONTRACT_RULES = "contract_rules"
MARKET_SCANNER = "market_scanner"
CORPORATE_ACTIONS = "corporate_actions"

ALL = (ACCOUNTS, POSITIONS, BUYING_POWER, OPEN_ORDERS, PREVIEW_ORDER,
       PLACE_ORDER, CANCEL_ORDER, OPTIONS_CHAIN, HISTORY_BARS,
       CONTRACT_RULES, MARKET_SCANNER, CORPORATE_ACTIONS)

#: Capabilities whose absence should hide a tool rather than let it fail. The
#: rest are informational until something is built on them.
#: Deliberately excludes the broker-specific ones (MARKET_SCANNER,
#: CORPORATE_ACTIONS). This set is the fail-open default, and failing open into
#: `ibkr_market_scanner` on a Webull account would advertise a tool that cannot
#: exist rather than one that might.
GATING = frozenset((ACCOUNTS, POSITIONS, BUYING_POWER, OPEN_ORDERS,
                    PREVIEW_ORDER, PLACE_ORDER, CANCEL_ORDER))


def _cache_path() -> str:
    return os.path.join(user_config_dir(), "broker_capabilities.json")


def fingerprint(broker) -> str:
    """
    Which account this record is about.

    Broker name alone is not enough -- the TH and US Webull entities are
    different brokers wearing one name -- and the raw account id does not belong
    in a config file that might get pasted into an issue, so it is hashed.
    """
    name = getattr(broker, "name", "?")
    entity = ""
    for attr in ("region", "environment", "base"):
        value = getattr(broker, attr, "")
        if value:
            entity = str(value)
            break
    if not entity and name == "webull":
        try:
            from dashboard import webull_client
            entity = webull_client.WEBULL_REGION_ID
        except Exception:
            entity = ""
    account = getattr(broker, "_account_id", "") or ""
    digest = hashlib.sha256(str(account).encode("utf-8")).hexdigest()[:12] if account else "unpinned"
    return f"{name}:{entity or 'default'}:{digest}"


def declared(broker) -> frozenset:
    """
    What the adapter says it implements.

    An adapter without a CAPABILITIES attribute is assumed to do everything the
    protocol defines -- the conservative reading, since the alternative is
    hiding tools from an adapter that merely predates this module.
    """
    stated = getattr(broker, "CAPABILITIES", None)
    if stated is None:
        return frozenset(GATING)
    return frozenset(stated)


def _load() -> dict:
    try:
        with open(_cache_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict):
    path = _cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Atomic, like every other write in this project: a half-written capability
    # file would hide tools at the next start with no way to tell why.
    handle, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def probed(broker) -> dict:
    """The cached probe for this account, or {} if it has never been run."""
    return _load().get(fingerprint(broker), {}).get("results", {})


def record(broker, results: dict):
    """Store a probe result. Only `probe_broker` should call this."""
    data = _load()
    data[fingerprint(broker)] = {
        "broker": getattr(broker, "name", "?"),
        "probed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "results": results,
    }
    _save(data)


def effective(broker) -> frozenset:
    """
    What to actually offer: declared, minus anything a probe caught refusing.

    A probe never *adds* a capability. If the adapter does not implement it,
    the API supporting it changes nothing.
    """
    out = set(declared(broker))
    for name, result in probed(broker).items():
        if isinstance(result, dict) and result.get("status") == "refused":
            out.discard(name)
    return frozenset(out)


def unprobed(broker) -> frozenset:
    """Declared but never called. Honest to show, since it is a claim, not a fact."""
    return frozenset(declared(broker)) - frozenset(probed(broker))


def summary(broker) -> str:
    """One block a person or a model can read, for get_data_sources."""
    have = sorted(effective(broker))
    missing = sorted(set(declared(broker)) - set(have))
    never = sorted(unprobed(broker))
    lines = [f"* Capabilities ({fingerprint(broker)}): {', '.join(have) or 'none'}"]
    if missing:
        lines.append(f"* Withdrawn by a live probe: {', '.join(missing)}")
    if never:
        lines.append(f"* Declared but never probed: {', '.join(never)} — run "
                     "`probe_broker_capabilities` to replace the claim with a fact.")
    return "\n".join(lines) + "\n"
