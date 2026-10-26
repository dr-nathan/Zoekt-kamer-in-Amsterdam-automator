#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/home/nathan/facebookrooms
STATE_DIR="$APP_DIR/.state/login-vnc"
DISPLAY_NUMBER=:99
NOVNC_PORT=6080
VNC_PORT=5900
SCREEN_NAME=facebook-login

mkdir -p "$STATE_DIR"

pid_is_running() {
    local file=$1
    [[ -f "$file" ]] && kill -0 "$(cat "$file")" 2>/dev/null
}

stop_pid() {
    local file=$1
    if pid_is_running "$file"; then
        kill "$(cat "$file")" 2>/dev/null || true
    fi
    rm -f "$file"
}

stop_all() {
    screen -S "$SCREEN_NAME" -X quit >/dev/null 2>&1 || true
    stop_pid "$STATE_DIR/websockify.pid"
    stop_pid "$STATE_DIR/x11vnc.pid"
    stop_pid "$STATE_DIR/xvfb.pid"
    rm -f "$STATE_DIR/login-input"
}

start_all() {
    stop_all

    nohup Xvfb "$DISPLAY_NUMBER" -screen 0 1440x1000x24 -nolisten tcp \
        > "$STATE_DIR/xvfb.log" 2>&1 &
    echo $! > "$STATE_DIR/xvfb.pid"

    for _ in {1..50}; do
        [[ -S /tmp/.X11-unix/X99 ]] && break
        sleep 0.1
    done
    if [[ ! -S /tmp/.X11-unix/X99 ]]; then
        echo "Xvfb did not start; see $STATE_DIR/xvfb.log" >&2
        stop_all
        exit 1
    fi

    nohup x11vnc -display "$DISPLAY_NUMBER" -localhost -forever -shared -nopw \
        -rfbport "$VNC_PORT" > "$STATE_DIR/x11vnc.log" 2>&1 &
    echo $! > "$STATE_DIR/x11vnc.pid"

    nohup websockify --web=/usr/share/novnc/ "127.0.0.1:$NOVNC_PORT" \
        "127.0.0.1:$VNC_PORT" > "$STATE_DIR/websockify.log" 2>&1 &
    echo $! > "$STATE_DIR/websockify.pid"

    : > "$STATE_DIR/login.log"
    screen -L -Logfile "$STATE_DIR/login.log" -dmS "$SCREEN_NAME" \
        bash -lc "cd '$APP_DIR' && DISPLAY='$DISPLAY_NUMBER' .venv/bin/fb-housing login"

    echo "Facebook login desktop started on localhost:$NOVNC_PORT."
    echo "After logging in, run: $0 finish"
}

finish_login() {
    if ! screen -list | grep -q "[.]$SCREEN_NAME"; then
        echo "No Facebook login session is running." >&2
        exit 1
    fi
    screen -S "$SCREEN_NAME" -X stuff $'\n'
    for _ in {1..100}; do
        if ! screen -list | grep -q "[.]$SCREEN_NAME"; then
            break
        fi
        sleep 0.1
    done
    if screen -list | grep -q "[.]$SCREEN_NAME"; then
        echo "The login command did not finish; inspect $STATE_DIR/login.log" >&2
        exit 1
    fi
    if ! grep -q "Facebook session saved" "$STATE_DIR/login.log"; then
        echo "Facebook did not confirm a saved session; inspect $STATE_DIR/login.log" >&2
        exit 1
    fi
    stop_all
    echo "Facebook session saved and temporary VNC processes stopped."
}

status() {
    printf 'xvfb=%s\n' "$(pid_is_running "$STATE_DIR/xvfb.pid" && echo active || echo inactive)"
    printf 'x11vnc=%s\n' "$(pid_is_running "$STATE_DIR/x11vnc.pid" && echo active || echo inactive)"
    printf 'websockify=%s\n' "$(pid_is_running "$STATE_DIR/websockify.pid" && echo active || echo inactive)"
    if screen -list | grep -q "[.]$SCREEN_NAME"; then
        echo 'login=waiting'
    elif [[ -f "$STATE_DIR/login.log" ]] && grep -q "Facebook session saved" "$STATE_DIR/login.log"; then
        echo 'login=saved'
    else
        echo 'login=inactive'
    fi
}

case "${1:-}" in
    start) start_all ;;
    finish) finish_login ;;
    stop) stop_all ;;
    status) status ;;
    *) echo "Usage: $0 {start|finish|stop|status}" >&2; exit 2 ;;
esac
