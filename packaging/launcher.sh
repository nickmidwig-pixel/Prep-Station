#!/bin/bash
# @APP_NAME@ launcher. First launch sets up a private Python (nothing is installed system-wide);
# later launches start straight away. Everything is logged to ~/Library/Logs/@APP_NAME@/launch.log.
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
APP="@APP_NAME@"
SUPPORT="$HOME/Library/Application Support/$APP"
LOGDIR="$HOME/Library/Logs/$APP"
VENV="$SUPPORT/venv"
# earlier builds had another name: reuse their setup instead of downloading everything again
OLD="$HOME/Library/Application Support/Midwig Sync"
if [ ! -d "$SUPPORT" ] && [ -d "$OLD/venv" ]; then mv "$OLD" "$SUPPORT" 2>/dev/null; fi
mkdir -p "$SUPPORT" "$LOGDIR"
LOG="$LOGDIR/launch.log"
# keep the log from growing forever
if [ -f "$LOG" ] && [ "$(stat -f %z "$LOG" 2>/dev/null || echo 0)" -gt 2000000 ]; then
  tail -c 500000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
exec 3>>"$LOG"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >&3; }
log "---- launch: macOS $(sw_vers -productVersion 2>/dev/null) $(uname -m), app at $RES"

alert() {  # title, message
  local btn
  btn=$(osascript - "$1" "$2" <<'OSA' 2>/dev/null
on run argv
  set r to display alert (item 1 of argv) message (item 2 of argv) as critical buttons {"Show Log", "OK"} default button "OK"
  return button returned of r
end run
OSA
)
  if [ "$btn" = "Show Log" ]; then open -R "$LOG"; fi
}

REQ_HASH="$(cat "$RES/requirements.txt" "$RES/requirements-extra.txt" 2>/dev/null | shasum | cut -d' ' -f1)"
if [ ! -x "$VENV/bin/python" ] || [ "$(cat "$SUPPORT/.req" 2>/dev/null)" != "$REQ_HASH" ]; then
  # only one setup at a time (a second double-click while setting up just says so)
  LOCK="$SUPPORT/.setup.lock"
  if ! mkdir "$LOCK" 2>/dev/null; then
    age=$(( $(date +%s) - $(stat -f %m "$LOCK" 2>/dev/null || echo 0) ))
    if [ "$age" -lt 900 ]; then
      log "setup already running (${age}s), not starting a second one"
      osascript -e 'display dialog "@APP_NAME@ is still setting up from when you first opened it. The window appears by itself when it’s ready." buttons {"OK"} default button 1 with title "@APP_NAME@" giving up after 30' >/dev/null 2>&1
      exit 0
    fi
    log "stale setup lock (${age}s), taking over"
    rm -rf "$LOCK"; mkdir "$LOCK"
  fi
  trap 'rmdir "$LOCK" 2>/dev/null; [ -n "$DLG" ] && kill "$DLG" 2>/dev/null' EXIT
  # a visible window while setting up, so it never looks like nothing happened
  osascript -e 'display dialog "Setting up @APP_NAME@. This only happens the first time.

It downloads its own copy of Python and the audio tools (about 1–2 minutes, needs internet). The app opens by itself when it’s ready." buttons {"OK"} default button 1 with title "@APP_NAME@" giving up after 900' >/dev/null 2>&1 &
  DLG=$!
  log "setting up (requirements $REQ_HASH)"
  UV=""
  for c in "$SUPPORT/uv/uv" "$SUPPORT/uv/bin/uv" "$(command -v uv 2>/dev/null)"; do
    [ -n "$c" ] && [ -x "$c" ] && UV="$c" && break
  done
  if [ -z "$UV" ]; then
    log "downloading uv"
    curl -LsSf https://astral.sh/uv/install.sh -o "$SUPPORT/uv-install.sh" >&3 2>&3 && \
      env UV_INSTALL_DIR="$SUPPORT/uv" UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 sh "$SUPPORT/uv-install.sh" >&3 2>&3
    for c in "$SUPPORT/uv/uv" "$SUPPORT/uv/bin/uv"; do [ -x "$c" ] && UV="$c" && break; done
  fi
  ok=0
  if [ -n "$UV" ]; then
    log "uv: $UV ($("$UV" --version 2>&1))"
    rm -rf "$VENV"
    "$UV" venv --python 3.11 "$VENV" >&3 2>&3 && \
    VIRTUAL_ENV="$VENV" "$UV" pip install --python "$VENV/bin/python" -r "$RES/requirements.txt" >&3 2>&3 && \
    "$VENV/bin/python" -c "import numpy, av, webview, objc, imageio_ffmpeg; print('self-test ok')" >&3 2>&3 && ok=1
    if [ $ok -eq 1 ] && [ -f "$RES/requirements-extra.txt" ]; then
      VIRTUAL_ENV="$VENV" "$UV" pip install --python "$VENV/bin/python" -r "$RES/requirements-extra.txt" >&3 2>&3 \
        && log "extras installed (OTIO, AAF)" || log "extras failed to install: OTIO/AAF exports will be skipped"
    fi
  else
    log "could not get uv"
  fi
  kill "$DLG" 2>/dev/null; DLG=""
  if [ $ok -ne 1 ]; then
    log "SETUP FAILED"
    alert "@APP_NAME@ couldn’t finish setting up" "It needs the internet the first time it opens. Check the connection and open it again.

If it keeps happening, send Claude the log (Show Log)."
    exit 1
  fi
  echo "$REQ_HASH" > "$SUPPORT/.req"
  log "setup done"
fi
export APP_ICON="$RES/AppIcon.icns"
export APP_LOG="$LOG"
cd "$RES/app" || exit 1
log "starting window"
exec "$VENV/bin/python" -m midwigsync.launch "$@" >&3 2>&3
