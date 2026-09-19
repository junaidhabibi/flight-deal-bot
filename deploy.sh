#!/usr/bin/env bash
# One command to take the bot live on GitHub Actions.
#
#   ./deploy.sh
#
# It refuses to push if anything looks like a credential is about to be
# committed. If the `gh` CLI is installed and signed in, it does everything.
# If not, it does the local half and prints the exact clicks for the rest.

set -euo pipefail
cd "$(dirname "$0")"

B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[2m'; N=$'\033[0m'
ok(){ printf "${G}✓${N} %s\n" "$1"; }
no(){ printf "${R}✗${N} %s\n" "$1"; }
sec(){ printf "\n${B}%s${N}\n" "$1"; }
die(){ no "$1"; exit 1; }

REPO_NAME="flight-deal-bot"

# ----------------------------------------------------------------- 1. tests
sec "1/6  Tests"
python3 -m tests.test_bot >/tmp/fdb_tests 2>&1 \
  || { tail -20 /tmp/fdb_tests; die "Tests failed. Not deploying."; }
ok "$(grep -oE 'Ran [0-9]+ tests' /tmp/fdb_tests) passed"

# ----------------------------------------------------- 2. credentials present
sec "2/6  Credentials"
[ -f .env ] || die ".env is missing. Run ./setup.sh first."
missing=""
for v in SERPAPI_KEY SMTP_USER SMTP_PASS ALERT_EMAIL; do
  grep -qE "^$v=.+" .env || missing="$missing $v"
done
[ -n "$missing" ] && die "Missing from .env:$missing"
ok "All four present in .env"

# --------------------------------------------------- 3. nothing secret leaks
sec "3/6  Secret scan"
git add -A >/dev/null 2>&1 || true
leak=0
for f in $(git diff --cached --name-only 2>/dev/null); do
  case "$f" in
    .env|*.bak|.env.*) no "$f is staged and must not be"; leak=1 ;;
  esac
done
# Look for the actual values, not just filenames.
if [ -s .env ]; then
  while IFS='=' read -r k v; do
    case "$k" in ''|\#*) continue ;; esac
    [ ${#v} -lt 12 ] && continue
    if git grep -qF -- "$v" -- . ':!.env' 2>/dev/null; then
      no "The value of $k appears in a tracked file"; leak=1
    fi
  done < .env
fi
[ "$leak" -eq 1 ] && die "Refusing to push. Fix the above first."
ok "No credentials in anything tracked"

# The working tree being clean is not enough. Git keeps every old version,
# and `git push` publishes all of it.
hist=0
if [ -d .git ] && [ -s .env ]; then
  while IFS='=' read -r k v; do
    case "$k" in ''|\#*) continue ;; esac
    [ ${#v} -lt 12 ] && continue
    if [ -n "$(git log --all -S "$v" --oneline 2>/dev/null | head -1)" ]; then
      no "$k appears in an OLD COMMIT: $(git log --all -S "$v" --oneline 2>/dev/null | head -1)"
      hist=1
    fi
  done < .env
fi

if [ "$hist" -eq 1 ]; then
  printf "\n${Y}${B}  A credential is in this repo's git history.${N}\n"
  printf "  Removing it from the current files does nothing: git keeps every\n"
  printf "  old version, and pushing publishes all of them.\n\n"
  printf "  This history has never been pushed anywhere, so there is nothing\n"
  printf "  to preserve. Starting a fresh one is clean and loses no work.\n\n"
  BK=".git-backup-$(date +%Y%m%d-%H%M%S)"
  mv .git "$BK"
  ok "Old history moved to $BK (delete it once you're happy)"
  git init -q
  git add -A
  ok "Fresh history started -- the credential is not in it"
  printf "  ${D}Worth rotating that app password anyway:\n"
  printf "  myaccount.google.com/apppasswords${N}\n"
fi
ok "Price history (data/prices.db) will be committed — that is intentional"

# ------------------------------------------------------------- 4. commit
sec "4/6  Commit"
git config user.name  >/dev/null 2>&1 || git config user.name  "Junaid"
git config user.email >/dev/null 2>&1 || git config user.email "junaid_64@live.com"
if git diff --cached --quiet; then
  ok "Nothing new to commit"
else
  git commit -q -m "Switch to Google Travel Explore, and fix what an audit found

Travelpayouts' cache has no DFW-to-Europe fares, so the old scan
returned nothing. Explore answers 'what's cheap from DFW to anywhere
in Europe' in one request -- ~48 destinations, 20 on the list.

Also fixes, all caught by rehearsing the pipeline on real data:
  - a phantom \$90 carry-on fee on every route, from treating Explore's
    'multi' airline placeholder as an unknown airline that charges
  - Finnair and Icelandair marked as charging for a cabin bag, which is
    their intra-European rule, not their transatlantic one
  - baselines measured from Dallas being applied to Chicago fares, which
    scored a routine \$393 ORD-KEF fare as '40% off'
  - alerting off a comparison against the price ceiling, which
    manufactures a discount out of a preference
  - unittest.main() sitting mid-file, so the suite the Action runs
    silently skipped 11 tests

Then a full audit, which found nine more of the same kind:
  - RSS items marked seen on FETCH, so anything found by a non-digest
    run (6 of every 7) was recorded, never sent, and then filtered out
    of the digest that would have sent it
  - the daily email cap counting DEALS, so one email of 7 deals tripped
    a cap of 6 and gagged the bot for a day right after a sale
  - no way to tell 'no deals today' from 'the source is broken': both
    exit 0 and show a green check. Added a silence warning.
  - a missing stop count defaulting to 0, which short-circuits the whole
    layover rule as 'nonstop' and prints that in the email
  - a >120h layover estimate never rejected, and never even flagged
  - the email calling a connecting itinerary 'nonstop'
  - the workflow's push retry ending on sleep, so a lost price history
    exited 0
  - failed verifications not billed, under-reporting the API ledger
  - the dead-zone estimate discarding fares it cannot distinguish from
    a routing detour (Istanbul adds 5h of real flying to DFW-ARN)"
  ok "Committed"
fi

# ------------------------------------------------------------- 5. GitHub
sec "5/6  GitHub"

# The only step that genuinely needs you: GitHub has to know it's you.
# Everything after the sign-in is automatic, including the secrets --
# they're read straight from .env and handed to gh, so they never get
# typed, pasted, or shown on screen.
# Homebrew's installer does NOT put brew on your PATH -- it prints three
# "Next steps" commands and leaves them to you. So `command -v brew` means
# "brew is on this shell's PATH", not "brew is installed", and a fresh
# install looks identical to no install at all. Look where it actually
# lives: /opt/homebrew on Apple Silicon, /usr/local on Intel.
BREW=""
for candidate in "$(command -v brew 2>/dev/null)" /opt/homebrew/bin/brew /usr/local/bin/brew; do
  [ -n "$candidate" ] && [ -x "$candidate" ] && { BREW="$candidate"; break; }
done

if [ -n "$BREW" ]; then
  eval "$("$BREW" shellenv)"          # puts brew and anything it installs on PATH
  # Make it stick for future terminals, the way the installer intended.
  ZP="$HOME/.zprofile"
  if ! grep -q 'brew shellenv' "$ZP" 2>/dev/null; then
    printf '\neval "$(%s shellenv)"\n' "$BREW" >> "$ZP"
    ok "Added Homebrew to your PATH in ~/.zprofile (new terminals will have it)"
  fi
fi

if ! command -v gh >/dev/null 2>&1; then
  if [ -n "$BREW" ]; then
    printf "  Installing the GitHub CLI (one-off, ~30s)...\n"
    "$BREW" install gh >/dev/null 2>&1 || die "brew install gh failed."
    eval "$("$BREW" shellenv)"
    command -v gh >/dev/null 2>&1 || die "gh installed but isn't on PATH."
    ok "gh installed"
  else
    die "Homebrew isn't installed. Get it from https://brew.sh, then re-run this."
  fi
fi

if ! gh auth status >/dev/null 2>&1; then
  printf "\n  ${B}Sign in to GitHub.${N} A browser window will open; approve it\n"
  printf "  and come back here. This is the only manual step.\n\n"
  gh auth login -h github.com -p https -w || die "Sign-in didn't complete."
  ok "Signed in"
fi

USER=$(gh api user -q .login)
ok "GitHub user: $USER"

if gh repo view "$USER/$REPO_NAME" >/dev/null 2>&1; then
  ok "Repo already exists: $USER/$REPO_NAME"
  git remote get-url origin >/dev/null 2>&1 \
    || git remote add origin "https://github.com/$USER/$REPO_NAME.git"
else
  gh repo create "$REPO_NAME" --public --source=. --remote=origin \
    --description "Watches for heavily discounted DFW-to-Europe fares" >/dev/null \
    || die "Couldn't create the repo."
  ok "Created PUBLIC repo $USER/$REPO_NAME"
  printf "     ${D}public = unlimited free Actions minutes; nothing secret is in the code${N}\n"
fi

git push -u origin HEAD >/dev/null 2>&1 && ok "Pushed" || die "Push failed."

for v in SERPAPI_KEY SMTP_USER SMTP_PASS ALERT_EMAIL; do
  val=$(grep -E "^$v=" .env | cut -d= -f2-)
  printf '%s' "$val" | gh secret set "$v" --repo "$USER/$REPO_NAME" >/dev/null \
    && ok "Secret set: $v" || no "Couldn't set $v"
done

gh api -X PUT "repos/$USER/$REPO_NAME/actions/permissions/workflow" \
  -f default_workflow_permissions=write >/dev/null 2>&1 \
  && ok "Actions can write (needed to save price history)" \
  || no "Couldn't set workflow permissions -- Settings > Actions > General > Read and write"

sec "6/6  First run"
gh workflow run scan.yml --repo "$USER/$REPO_NAME" -f dry_run=true >/dev/null 2>&1 \
  && ok "Triggered a dry run (no email will be sent)" \
  || printf "  ${Y}Trigger it yourself: Actions tab -> Run workflow${N}\n"

printf "\n  Watch it:  ${B}gh run watch --repo %s/%s${N}\n" "$USER" "$REPO_NAME"
printf "  Or open:   https://github.com/%s/%s/actions\n\n" "$USER" "$REPO_NAME"
ok "Live. It runs every 4 hours from now on."
printf "  ${D}Expect silence for a week or two while it learns each route's\n"
printf "  normal price. If it stops seeing fares at all, it will email you.${N}\n\n"
