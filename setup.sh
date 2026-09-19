#!/usr/bin/env bash
#
# One-command setup. Run this, answer the prompts, done:
#
#     ./setup.sh
#
# It installs dependencies, asks for your credentials, saves them to a
# .env file (which is gitignored and never leaves your machine), and sends
# a test email to prove the whole chain works.
#
# Why a .env file instead of `export`: shell exports vanish when you close
# Terminal, and a single stray comma sets a variable to the wrong value
# without saying so. The file is read automatically every run.

set -uo pipefail
cd "$(dirname "$0")"

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'
GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'

# NOTE: these are prefixed with _ on purpose. An earlier version defined a
# function called `head`, which shadowed the `head` COMMAND used further
# down to read saved values -- so `grep ... | head -1` called the function
# with the argument "-1", and "-1" silently became every saved credential.
# Never name a shell helper after a real command.
_say()     { printf "%s\n" "$*"; }
_ok()      { printf "${GREEN}✓${RESET} %s\n" "$*"; }
_warn()    { printf "${YELLOW}!${RESET} %s\n" "$*"; }
_err()     { printf "${RED}✗${RESET} %s\n" "$*"; }
_section() { printf "\n${BOLD}%s${RESET}\n" "$*"; }

trap 'echo; _err "Setup interrupted. Re-run ./setup.sh to pick up where you left off."; exit 130' INT

# --------------------------------------------------------------------
_section "1/5  Checking Python"

PY=""
for c in python3.12 python3.11 python3 python; do
  if command -v "$c" >/dev/null 2>&1; then
    v=$("$c" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null) || continue
    major=${v%%.*}; minor=${v##*.}
    if [ "$major" -eq 3 ] && [ "$minor" -ge 10 ]; then PY="$c"; break; fi
  fi
done

if [ -z "$PY" ]; then
  _err "Need Python 3.10 or newer. Install from https://www.python.org/downloads/"
  exit 1
fi
_ok "Using $PY ($("$PY" --version 2>&1))"

# --------------------------------------------------------------------
_section "2/5  Installing dependencies"

if "$PY" -m pip install -q -r requirements.txt 2>&1 | tail -3; then
  _ok "Dependencies installed"
else
  _err "pip install failed. Try: $PY -m pip install -r requirements.txt"
  exit 1
fi

# macOS python.org builds ship an empty trust store until this is run.
# The bot falls back to certifi anyway, but fixing it properly helps every
# other Python tool on the machine too.
if [ "$(uname)" = "Darwin" ]; then
  certcmd=$(ls -d /Applications/Python*/Install\ Certificates.command 2>/dev/null | tail -1)
  if [ -n "$certcmd" ]; then
    if "$PY" -c 'import ssl,sys; sys.exit(0 if ssl.create_default_context().cert_store_stats()["x509_ca"]>0 else 1)' 2>/dev/null; then
      _ok "TLS trust store looks healthy"
    else
      _warn "Empty TLS trust store (normal for python.org installs) — fixing"
      open "$certcmd" >/dev/null 2>&1 && _ok "Ran Install Certificates.command" \
        || _warn "Couldn't run it; the bot will use certifi instead (fine)"
    fi
  fi
fi

# --------------------------------------------------------------------
_section "3/5  Running tests"

if "$PY" -m tests.test_bot >/tmp/fdb_tests.log 2>&1; then
  _ok "$(grep -o 'Ran [0-9]* tests' /tmp/fdb_tests.log | tail -1) passed"
else
  _err "Tests failed. Last lines:"
  tail -15 /tmp/fdb_tests.log
  exit 1
fi

# --------------------------------------------------------------------
_section "4/5  Credentials"

ENV_FILE=".env"
# Read one value from .env using only shell builtins -- nothing external
# to shadow, and it keeps spaces and '=' inside the value intact.
existing() {
  local want="$1" line key val
  [ -f "$ENV_FILE" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      \#*|"") continue ;;
    esac
    key="${line%%=*}"
    val="${line#*=}"
    if [ "$key" = "$want" ]; then printf '%s' "$val"; return 0; fi
  done < "$ENV_FILE"
  return 0
}

# ask VAR "Prompt" "help text" secret?
ask() {
  local var="$1" prompt="$2" help="$3" secret="${4:-no}"
  local current; current=$(existing "$var")
  local shown="" input=""

  if [ -n "$current" ]; then
    if [ "$secret" = "yes" ]; then shown=" ${DIM}[saved — Enter to keep]${RESET}"
    else shown=" ${DIM}[$current — Enter to keep]${RESET}"; fi
  fi

  printf "\n  ${DIM}%s${RESET}\n" "$help"
  if [ "$secret" = "yes" ]; then
    printf "  %s:%s " "$prompt" "$shown"; read -rs input; echo
  else
    printf "  %s:%s " "$prompt" "$shown"; read -r input
  fi

  # Clean up the classic paste mistakes: wrapping quotes, trailing comma,
  # a pasted "export VAR=" prefix, and the literal placeholder from docs.
  input="${input#export }"
  input="${input#$var=}"
  input="${input%\"}"; input="${input#\"}"
  input="${input%\'}"; input="${input#\'}"
  input="${input%,}"
  input="$(printf '%s' "$input" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"

  if [ "$input" = "..." ] || [ "$input" = "your_token_here" ]; then
    _err "That's the placeholder from the docs, not a real value."
    ask "$var" "$prompt" "$help" "$secret"; return
  fi

  [ -z "$input" ] && input="$current"

  # Never write a multi-line or dash-leading value: a real token, address or
  # password is a single line, so anything else means something upstream
  # mangled it and we would be silently destroying a good credential.
  case "$input" in
    *"$(printf '\n')"*|-*)
      _err "Refusing to save a malformed value for $var."
      _say "  Delete .env and re-run, or set it by hand."
      input="" ;;
  esac
  printf '%s' "$input" > "/tmp/fdb_$var"
}

ask SERPAPI_KEY "SerpApi key" \
  "REQUIRED — this is where every fare comes from. One request returns
  ~48 European destinations from your airport. Free tier is 250/month,
  which the bot paces itself against. Get it at serpapi.com/manage-api-key" no

ask SMTP_USER "Gmail address" \
  "The account that SENDS the alerts." no

ask SMTP_PASS "Gmail app password" \
  "NOT your normal password — Google rejects those from scripts.
  Create one at myaccount.google.com/apppasswords (needs 2FA on).
  16 characters; spaces are fine. Input is hidden." yes

ask ALERT_EMAIL "Send alerts to" \
  "Where deals land. Press Enter to use the Gmail address above." no

ask TRAVELPAYOUTS_TOKEN "Travelpayouts token (optional)" \
  "Not needed. Its cache has no Dallas-to-Europe fares in it, so the
  source is disabled in config.yml. Press Enter to skip." no

if [ -f "$ENV_FILE" ]; then
  cp "$ENV_FILE" "$ENV_FILE.bak" && chmod 600 "$ENV_FILE.bak"
  _say "  ${DIM}(previous .env backed up to .env.bak)${RESET}"
fi

{
  echo "# Flight deal bot credentials."
  echo "# Gitignored — never committed. Delete this file to reset."
  echo "# Generated $(date '+%Y-%m-%d %H:%M')"
  for v in TRAVELPAYOUTS_TOKEN SMTP_USER SMTP_PASS ALERT_EMAIL SERPAPI_KEY; do
    val=$(cat "/tmp/fdb_$v" 2>/dev/null || true)
    [ -n "$val" ] && printf '%s=%s\n' "$v" "$val"
  done
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"
rm -f /tmp/fdb_* 2>/dev/null
_ok "Saved to .env (readable only by you, and gitignored)"

missing=""
# SERPAPI_KEY is required now, not Travelpayouts: it is the only source
# that actually returns DFW->Europe fares. See config.yml for why.
for v in SERPAPI_KEY SMTP_USER SMTP_PASS; do
  grep -qE "^$v=.+" "$ENV_FILE" || missing="$missing $v"
done
if [ -n "$missing" ]; then
  _err "Still missing:$missing"
  if [ -f "$ENV_FILE.bak" ]; then
    _say "Your previous values are in .env.bak. To restore them:"
    _say "    cp .env.bak .env"
  fi
  _say "Then re-run ./setup.sh."
  exit 1
fi

# --------------------------------------------------------------------
_section "5/5  Sending a test email"

if "$PY" -m bot.main --test-email 2>&1 | tail -12; then
  echo
  _ok "Check your inbox."
else
  echo
  _err "Test email failed — see the message above."
  exit 1
fi

# --------------------------------------------------------------------
cat <<EOF

${BOLD}Done. What you can run now:${RESET}

  ${BOLD}$PY -m bot.main --dry-run -v${RESET}
      A real scan that prints what it found instead of emailing it.
      Start here — it takes about 4 minutes.

  ${BOLD}$PY -m bot.main${RESET}
      A real run that emails you if anything clears the bar.

  ${BOLD}$PY -m bot.main --stats${RESET}
      What the bot has learned about prices so far.

  ${BOLD}$PY -m bot.main --bag${RESET}
      Your carry-on vs. every airline's limits.

${BOLD}To make it run without your laptop${RESET}, push to GitHub and add the same
values as repository secrets — see the "Put it on GitHub Actions" section
of README.md. Your .env file is never committed.
EOF
