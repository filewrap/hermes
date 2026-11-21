#!/usr/bin/env bash
# Install Hermes — clones OUR repo and installs it with its own PM setup.
# Does NOT use the official installer. This file lives outside the repo,
# so your local repo stays clean.
#
# Run:
#   bash install-hermes.sh               # install / update — terminal chat only
#   bash install-hermes.sh --messaging   # same + Telegram / Discord / Slack
#
# Change where / what it clones:
#   HERMES_DIR=~/my-hermes bash install-hermes.sh
#   REPO_URL=https://github.com/filewrap/hermes.git bash install-hermes.sh
#
# Linux, macOS, or WSL2 only. Native Windows: run this inside WSL2.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/filewrap/hermes-agent.git}"
DIR="${HERMES_DIR:-$HOME/hermes-agent}"
WANT_MESSAGING=0

for arg in "$@"; do
  case "$arg" in
    --messaging) WANT_MESSAGING=1 ;;
    -h|--help)
      echo "Usage: bash install-hermes.sh [--messaging]"
      echo "  (no flag)    install / update Hermes for terminal chat"
      echo "  --messaging  also install Telegram / Discord / Slack support"
      exit 0 ;;
    *) echo "Unknown option: $arg — use --messaging or --help" >&2; exit 2 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

# --- 1. OS -----------------------------------------------------------------
case "$(uname -s)" in
  Linux|Darwin) ;;
  *) die "Linux, macOS, or WSL2 only." ;;
esac

# --- 2. git + curl ----------------------------------------------------------
missing=""
command -v git  >/dev/null 2>&1 || missing="$missing git"
command -v curl >/dev/null 2>&1 || missing="$missing curl"
if [ -n "$missing" ]; then
  say "Installing:$missing"
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update && sudo apt-get install -y $missing
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y $missing
  elif command -v brew >/dev/null 2>&1; then
    brew install $missing
  else
    die "Install these first, then re-run:$missing"
  fi
fi

# --- 3. Clone or update ------------------------------------------------------
if [ -d "$DIR/.git" ]; then
  say "Updating $DIR"
  git -C "$DIR" pull --ff-only
elif [ -e "$DIR" ]; then
  die "$DIR exists and is not this repo. Move it away, then re-run."
else
  say "Cloning $REPO_URL -> $DIR"
  git clone "$REPO_URL" "$DIR"
fi

# --- 4. Install (PM environment: pinned uv + Python + tools + deps) ---------
# setup-hermes.sh is the repo's own installer. It also creates .env,
# puts `hermes` in ~/.local/bin, adds it to your shell PATH, and syncs skills.
say "Installing Hermes (first run: 1-5 minutes)"
cd "$DIR"
./setup-hermes.sh

export PATH="$HOME/.local/bin:$PATH"
command -v hermes >/dev/null 2>&1 || die "Install finished but 'hermes' is not on PATH. Open a new terminal and try: hermes doctor"

# --- 5. Optional: messaging ---------------------------------------------------
if [ "$WANT_MESSAGING" = 1 ]; then
  say "Installing messaging support (Telegram / Discord / Slack)"
  hermes pm install --extra messaging
fi

# --- 6. What to run next --------------------------------------------------------
cat <<EOF

Done. Hermes is in: $DIR

Run this first:
  hermes setup     # add your provider / API key, pick a model
  hermes           # chat in the terminal

Want Telegram / Discord / Slack?
EOF
if [ "$WANT_MESSAGING" = 1 ]; then
  echo "  Already installed. Now run:"
else
  echo "  Not installed. Install it, then run:"
  echo "    hermes pm install --extra messaging"
fi
cat <<'EOF'
    hermes gateway setup    # pick a platform, paste its token
    hermes gateway start    # start it — then message your bot
  Telegram needs 3 things in setup: API ID + API hash from
  my.telegram.org/apps, and a bot token from @BotFather in Telegram.

Want to check / fix / update?
  hermes doctor    # diagnose problems
  hermes status    # show config
  hermes update    # update to the latest version

Want the dev environment in this shell (to work on the code)?
  cd ~/hermes-agent && source ./activate
  deactivate       # leave it
EOF
