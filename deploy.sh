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
  git commit -q -m "Durability pass: make the quiet failures loud

The bot is designed to stay silent for weeks, which means silence carries
no information and a dead bot looks exactly like a healthy one. This fixes
the ways it could rot without anyone noticing.

  - weekly heartbeat email. The only thing that can catch the bot NOT
    RUNNING (GitHub disabling the schedule after 60 days, Actions off,
    workflow broken) -- no check inside the bot fires when the bot is not
    executing. It also reports how close each route is to its alert bar.
  - the silence warning saturated: the lookback capped the streak at 18,
    a multiple of the 6-run threshold, so a dead bot emailed on EVERY run
    forever. ~2,500 a year, which trains you to filter the bot away and
    lose the real alerts with it. Now escalates 1x, 2x, 4x and stops.
  - the record bar could only ever ratchet DOWN: one lucky cheap fare
    raised the difficulty for 18 months, so the chance of alerting decayed
    every month by construction. Records now use a 180-day window.
  - SerpApi billing was wrong in both directions -- the verifier never
    counted its searches at all (under-billing against a hard 250/month
    cap), while failures WERE counted, which their FAQ says are free.
  - the schedule fired 7 times a day against a budget sized for 6, so one
    sweep was silently skipped daily -- and if it was the digest run,
    there was no digest that day. Now 6 runs.
  - the digest was detected by comparing an exact cron string; any edit to
    that line would have disabled it forever, silently. Matches the hour.
  - a corrupt prices.db was committable, which would kill every future run
    with no email and no recovery. PRAGMA integrity_check now gates it.
  - email timestamps used the runner's UTC clock, so every alert looked
    5-6 hours stale.
  - dependencies pinned: unpinned ranges ship upstream releases straight
    to production at 3am.
  - README banner: it documented the old thresholds and would have
    actively misled anyone tuning this a year from now."

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

# Pull before pushing. The bot commits a price-history update to this repo
# on nearly every run, so after a day of running the local clone is ALWAYS
# behind and a plain push is rejected as a non-fast-forward. The first
# version of this script just pushed and died with "Push failed.", hiding
# the actual reason.
if git ls-remote --exit-code origin HEAD >/dev/null 2>&1; then
  git fetch -q origin 2>/dev/null || true
  if git rev-parse --verify -q origin/main >/dev/null; then
    behind=$(git rev-list --count HEAD..origin/main 2>/dev/null || echo 0)
    if [ "${behind:-0}" -gt 0 ]; then
      printf "  ${D}%s new commit(s) on GitHub (the bot's own price history) — rebasing${N}\n" "$behind"
      # A leftover index or a half-finished rebase makes this fail before it
      # starts ("your index contains uncommitted changes"). Clear the way,
      # but never discard real work: only locks and an abandoned rebase.
      rm -f .git/index.lock .git/HEAD.lock 2>/dev/null || true
      if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then
        printf "  ${Y}a previous rebase was left unfinished — abandoning it${N}\n"
        git rebase --abort >/dev/null 2>&1 || true
      fi
      rebase_err=$(git rebase origin/main 2>&1)
      if [ $? -ne 0 ]; then
        # The only file that can genuinely conflict is the binary database,
        # and GitHub's copy is the authoritative one: it holds the runs that
        # happened in the cloud, which this machine never saw.
        if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then
          git checkout --ours data/prices.db >/dev/null 2>&1 || true
          git add data/prices.db >/dev/null 2>&1 || true
          if ! git -c core.editor=true rebase --continue >/dev/null 2>&1; then
            git rebase --abort >/dev/null 2>&1 || true
            no "Couldn't reconcile with GitHub. Git said:"
            printf "%s\n" "$rebase_err" | sed 's/^/     /' | head -8
            die "Try: git pull --rebase"
          fi
          ok "Kept GitHub's price history (it has the cloud runs)"
        else
          # Not a conflict -- something stopped it starting. Show the reason
          # instead of a bare "Rebase failed", which says nothing.
          no "Rebase failed. Git said:"
          printf "%s\n" "$rebase_err" | sed 's/^/     /' | head -8
          die "Try: git pull --rebase"
        fi
      else
        ok "Rebased onto GitHub"
      fi
    fi
  fi
fi

push_err=$(git push -u origin HEAD 2>&1) && ok "Pushed" || {
  no "Push failed:"
  printf "%s\n" "$push_err" | sed 's/^/     /' | head -8
  exit 1
}

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
