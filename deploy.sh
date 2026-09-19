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
  git commit -q -m "Switch to Google Travel Explore as the fare source

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
    silently skipped 11 tests"
  ok "Committed"
fi

# ------------------------------------------------------------- 5. GitHub
sec "5/6  GitHub"
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  USER=$(gh api user -q .login)
  if gh repo view "$USER/$REPO_NAME" >/dev/null 2>&1; then
    ok "Repo exists: $USER/$REPO_NAME"
    git remote get-url origin >/dev/null 2>&1 \
      || git remote add origin "https://github.com/$USER/$REPO_NAME.git"
  else
    gh repo create "$REPO_NAME" --public --source=. --remote=origin \
      --description "Watches for heavily discounted DFW-to-Europe fares" >/dev/null
    ok "Created public repo $USER/$REPO_NAME"
    printf "  ${D}public = unlimited free Actions minutes${N}\n"
  fi

  git push -u origin HEAD >/dev/null 2>&1 && ok "Pushed"

  for v in SERPAPI_KEY SMTP_USER SMTP_PASS ALERT_EMAIL; do
    val=$(grep -E "^$v=" .env | cut -d= -f2-)
    printf '%s' "$val" | gh secret set "$v" --repo "$USER/$REPO_NAME" >/dev/null
    ok "Secret set: $v"
  done

  gh api -X PUT "repos/$USER/$REPO_NAME/actions/permissions/workflow" \
    -f default_workflow_permissions=write >/dev/null 2>&1 \
    && ok "Actions can write (needed to save price history)"

  sec "6/6  First run"
  gh workflow run scan.yml --repo "$USER/$REPO_NAME" -f dry_run=true >/dev/null 2>&1 \
    && ok "Triggered a dry run" \
    || printf "  ${Y}Trigger it yourself: Actions tab -> Run workflow${N}\n"
  printf "\n  Watch it:  ${B}gh run watch --repo %s/%s${N}\n" "$USER" "$REPO_NAME"
  printf "  Or open:   https://github.com/%s/%s/actions\n\n" "$USER" "$REPO_NAME"
  ok "Live. It runs every 4 hours from now on."
else
  printf "${Y}!${N} The gh CLI isn't installed or isn't signed in.\n"
  printf "  ${B}Easiest fix (then re-run this script):${N}\n"
  printf "    brew install gh && gh auth login\n\n"
  printf "  ${B}Or do it by hand:${N}\n"
  printf "   1. Make a PUBLIC repo called %s at https://github.com/new\n" "$REPO_NAME"
  printf "      ${D}Public matters: private repos get 2,000 Actions minutes/month,\n"
  printf "      public repos get unlimited. Nothing secret is in the code.${N}\n"
  printf "   2. Back here:\n"
  printf "        git remote add origin https://github.com/YOURNAME/%s.git\n" "$REPO_NAME"
  printf "        git push -u origin HEAD\n"
  printf "   3. Settings -> Secrets and variables -> Actions -> New secret.\n"
  printf "      Add these four, names exactly as written:\n"
  for v in SERPAPI_KEY SMTP_USER SMTP_PASS ALERT_EMAIL; do
    val=$(grep -E "^$v=" .env | cut -d= -f2-)
    printf "        %-14s %s…%s ${D}(%d chars — copy from .env)${N}\n" \
      "$v" "${val:0:4}" "${val: -3}" "${#val}"
  done
  printf "   4. Settings -> Actions -> General -> Workflow permissions\n"
  printf "      -> ${B}Read and write${N}. Without this it can't save price history.\n"
  printf "   5. Actions tab -> 'Flight deal scan' -> Run workflow -> dry_run: true\n\n"
fi
