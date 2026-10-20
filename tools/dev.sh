#!/usr/bin/env bash
# Dev loop for the CYD board.
#
#   tools/dev.sh flash   -- THE way to flash a bench board: stop the installed
#                           daemon, check the chip is not fused, build, flash,
#                           put the installed daemon back
#   tools/dev.sh up      -- start the daemon in the background
#   tools/dev.sh down    -- stop the daemon (frees the port)
#   tools/dev.sh log     -- tail the daemon log
#
# Only ONE process may own the serial port, so the daemon must be down while
# esptool talks to the board. `flash` sequences that for you, including the
# launchd job installed by `blink install` (the same stop and restore as
# burn.sh), so there is nothing to run by hand around it.
#
# The build is always the PILOT-panel one: every board here has the pilot panel
# (production firmware on it came out mirrored, 2026-10-15).
set -euo pipefail

PORT="${PORT:-$(ls /dev/cu.usbserial* 2>/dev/null | head -1 || true)}"
BOARD="esp32_devkitc/esp32/procpu"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="/tmp/claude-usage-bridge.log"
PIDFILE="/tmp/claude-usage-bridge.pid"

activate() {
	# shellcheck disable=SC1090
	source "$ROOT/tools/lib_zephyr.sh"
	blink_zephyr_activate || exit 1
}

down() {
	if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
		kill "$(cat "$PIDFILE")" 2>/dev/null || true
		sleep 1
	fi
	pkill -f claude_usage_bridge.py 2>/dev/null || true
	rm -f "$PIDFILE"
	sleep 0.5
	if lsof "$PORT" >/dev/null 2>&1; then
		echo "warning: $PORT still held by:"
		lsof "$PORT"
	fi
}

AGENT="gui/$(id -u)/com.blink.bridge"
DAEMON_WAS_UP=0

# Stops the installed daemon: the launchd job, and then whatever is still on the
# port. bootout RETURNS before the process is gone, and on 2026-10-15 the process
# outlived an unloaded job and kept the port, which read as "cannot tell whether
# this chip is fused -- is the port free?" because lib_efuse.sh discards
# espefuse's own error. Only the Blink daemon is stopped; anything else on the
# port is somebody else's and is reported, not killed.
stop_installed() {
	local pid cmd i
	if launchctl print "$AGENT" >/dev/null 2>&1; then
		DAEMON_WAS_UP=1
		launchctl bootout "$AGENT" >/dev/null 2>&1 || true
		for i in $(seq 1 20); do
			launchctl print "$AGENT" >/dev/null 2>&1 || break
			sleep 0.25
		done
	fi
	for pid in $(lsof -t "$PORT" 2>/dev/null || true); do
		cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
		case "$cmd" in
		*/.blink/bin/blink*)
			DAEMON_WAS_UP=1
			echo "stopping the installed daemon (pid $pid)"
			kill "$pid" 2>/dev/null || true
			;;
		*)
			echo "FATAL: $PORT is held by pid $pid ($cmd), which is not the Blink daemon." >&2
			echo "       Stop it yourself and run this again." >&2
			exit 1
			;;
		esac
	done
	for i in $(seq 1 40); do
		lsof "$PORT" >/dev/null 2>&1 || return 0
		sleep 0.25
	done
	echo "FATAL: $PORT is still held after 10 s:" >&2
	lsof "$PORT" >&2 || true
	exit 1
}

# bootstrap REGISTERS the job; it does not start it. kickstart -k does.
restore_installed() {
	[ "$DAEMON_WAS_UP" = 1 ] || return 0
	launchctl bootstrap "gui/$(id -u)" \
		"$HOME/Library/LaunchAgents/com.blink.bridge.plist" >/dev/null 2>&1 || true
	launchctl kickstart -k "$AGENT" >/dev/null 2>&1 || true
	echo "installed daemon put back"
}

up() {
	activate
	cd "$ROOT"
	nohup python3 -u claude_usage_bridge.py --port "$PORT" >"$LOG" 2>&1 &
	echo $! >"$PIDFILE"
	sleep 2
	echo "daemon up (pid $(cat "$PIDFILE")), logging to $LOG"
	tail -n 5 "$LOG" || true
}

case "${1:-flash}" in
flash)
	down >/dev/null
	# Registered BEFORE stop_installed: bootout can succeed and then the port
	# check can exit 1 (a stranger holds it), and without this trap the
	# installed daemon would stay unloaded. restore_installed does nothing
	# unless the daemon was actually up, so registering it early is safe.
	trap restore_installed EXIT
	trap 'exit 130' INT TERM
	stop_installed
	# dev.sh writes PLAINTEXT. A fused chip's ROM cannot read that, and the
	# board goes dark until tools/flash_encrypted.sh restores it -- the exact
	# mirror of the mistake that script refuses. Ask the chip, before the build.
	if [ "${BLINK_SKIP_EFUSE_CHECK:-0}" != "1" ]; then
		# shellcheck source=lib_efuse.sh
		. "$ROOT/tools/lib_efuse.sh"
		efuse_probe "$PORT"
		case "$EFUSE_STATE" in
		plaintext)
			;;	# what this script is for
		encrypted)
			{
				echo "FATAL: this chip is FUSED (FLASH_CRYPT_CNT = 0b$EFUSE_BITS${EFUSE_MAC:+, MAC $EFUSE_MAC})."
				echo "       dev.sh writes plaintext, which a fused ROM cannot read -- the board"
				echo "       would go dark until re-flashed. Use the encrypted path instead:"
				echo "         cd $ROOT/firmware && west build --sysbuild -d build-sb -b $BOARD . \\"
				echo "           -- -DSB_CONFIG_BOOTLOADER_MCUBOOT=y -DUSE_CCACHE=0 \\"
				# Single-quoted so the key path reaches the user's shell unexpanded,
				# and the nested quotes survive: sysbuild needs the value quoted, and
				# a build that omits this silently signs with MCUboot's bundled dev
				# key -- the board boots, but every later OTA is rejected and reverts.
				echo '             -DSB_CONFIG_BOOT_SIGNATURE_KEY_FILE="\"$HOME/.blink/ota_signing_key_p256.pem\"" \'
				echo "           && $ROOT/tools/flash_encrypted.sh $PORT"
				echo "       BLINK_SKIP_EFUSE_CHECK=1 overrides."
			} >&2
			exit 1
			;;
		*)
			echo "FATAL: cannot tell whether this chip is fused -- $EFUSE_REASON" >&2
			echo "       Refusing to flash blind. BLINK_SKIP_EFUSE_CHECK=1 overrides." >&2
			exit 1
			;;
		esac
	fi
	if [ "${BLINK_PLAIN_FLASH:-0}" != "1" ]; then
			# A plain `west flash` writes only the app at 0x1000 and takes the MCUboot
			# bootloader with it, so the board can never take an OTA again. Refuse it
			# unless the caller asks for that loss by name. burn.sh keeps MCUboot.
			# Refusing before the build leaves the installed daemon alone: the EXIT
			# trap restores it.
			{
				echo "FATAL: dev.sh flashes WITHOUT MCUboot. The board loses OTA for good."
				echo "       Use tools/burn.sh, which keeps MCUboot:"
				echo "         tools/burn.sh --edition <claude|codex> --port $PORT"
				echo "       To accept the loss on purpose: BLINK_PLAIN_FLASH=1 tools/dev.sh flash"
			} >&2
			exit 1
		fi
		activate   # sources zephyr-env.sh too
	cd "$ROOT/firmware"
	# Always the pilot-panel build (firmware/pilot.conf). See BLINK_PANEL_PILOT.
	west build -b "$BOARD" . -- -DUSE_CCACHE=0 \
		-DEXTRA_CONF_FILE=pilot.conf | tail -3
	# 921600 is beyond what this board's CH340 tolerates; it fails mid-write.
	west flash --esp-device "$PORT" --esp-baud-rate 115200 | tail -1
	# The installed daemon comes back on EXIT. Only a machine that had none
	# gets the throwaway one from `up`, so two never share a port.
	if [ "$DAEMON_WAS_UP" = 0 ]; then
		up
	fi
	# A reminder, never a gate: nothing here can see the screen, and a flash that
	# "worked" has come out mirrored before (2026-10-15, production build on a
	# pilot panel). It names the build so the answer points at the fix.
	cat <<EOF

Flashed a PILOT-panel build. Now look at the board:
  - mirrored, or red/blue swapped, or dim?  Do not change 'rotation'; check the build.
  - touch lands where you press?
  - still alive after 90 s?  (unconfirmed images revert at about 90 s)
EOF
	;;
up) down; up ;;
down) down; echo "daemon down, $PORT free" ;;
log) tail -f "$LOG" ;;
*) echo "usage: $0 {flash|up|down|log}"; exit 1 ;;
esac
