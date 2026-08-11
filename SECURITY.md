# Security

This project holds brokerage API credentials and can place real orders. Treat
findings here as you would in any tool that touches money.

## Reporting

Use **[GitHub private vulnerability reporting](https://github.com/Blahaj-gif/Finance-mcp/security/advisories/new)**
rather than a public issue, for anything in the classes below. Everything else
— a wrong number, a bad refusal, a crash — belongs in a normal issue, and is
just as welcome.

Expect a slow reply. This is one person's project.

## What counts

- **A path from the tool surface to a live order.** The assistant can draft and
  preview; submission exists only in the dashboard behind a human click. A code
  path that reaches the market without that click is the most serious bug this
  project can have, whatever it is for.
- **A credential reaching disk, a log, stdout or a subprocess.** The SDK once
  wrote app keys, request signatures and full response bodies to an 11 MB log
  at DEBUG, and a `.pypirc` block pasted into `.env` exported a PyPI token as
  `os.environ["PASSWORD"]` for every child process to inherit. Both are fixed;
  both are the shape to look for.
- **A pre-trade guard that can be bypassed.** The naked-short and buying-power
  checks once ran only when an unrelated `try` did not raise, so a price lookup
  failing disabled the notional check entirely.
- **Anything that makes an unverified adapter look verified**, or a stale price
  look live.

## Known and accepted

- **`IBKR_TLS_INSECURE=1` disables certificate verification** for the local
  Client Portal Gateway, which is self-signed by design. It is opt-in, never a
  default, and the error message says what you are choosing. Over localhost
  this is a considered trade; over anything else it is not.
- **The Saxo and IBKR adapters are unverified.** They have never been run
  against their APIs. That is a correctness risk rather than a vulnerability,
  and it is stated on the class, in tool output, and in the README.
- **`.env` holds plaintext credentials.** It is gitignored, and the loader
  stops at the first `[section]` so INI pasted below one cannot become
  environment variables. There is no keyring integration; if you want one, that
  is a welcome pull request.
- **Order drafts are a local JSON file.** Anyone who can write it can queue a
  draft. They cannot send it — that still needs a human click and a broker
  preview in the dashboard.

## If you rotate a credential

`conf/token.txt` caches a session token; delete it after rotating an app key.
Logs under `conf/` are redacted going forward but not retroactively — redaction
only protects what is written after it was added.
