#!/usr/bin/python3

import argparse
import os
import signal
import struct
import sys
import time
from subprocess import call

import gpiod
import smbus2

# --- Defaults (can be overridden via CLI flags) ---
DEFAULT_MIN_CAPACITY_SHUTDOWN = 30.0  # Shutdown if capacity below this % while on battery
DEFAULT_MIN_VOLTAGE_SHUTDOWN = 3.20  # Shutdown if voltage below this V while on battery
DEFAULT_SLEEP_TIME = 60  # Seconds between checks
DEFAULT_PIDFILE = "/var/run/X1200_v2.pid"
DEFAULT_AC_LOSS_CONFIRMATIONS = 1
DEFAULT_SHUTDOWN_CONFIRMATIONS = 1

I2C_BUS_NUMBER = 1
I2C_ADDRESS = 0x36
REG_VOLTAGE = 2
REG_CAPACITY = 4

GPIO_CHIP = "gpiochip0"
PLD_PIN = 6

bus = None
pld_line = None
pidfile = DEFAULT_PIDFILE


def log(message):
    print(f"UPS Monitor v2: {message}", flush=True)


def cleanup_and_exit(signum=None, frame=None):
    log("Received termination signal. Cleaning up...")

    global pld_line
    if pld_line is not None:
        try:
            pld_line.release()
        except Exception as e:
            log(f"Error releasing PLD line: {e}")
        pld_line = None

    global bus
    if bus is not None:
        try:
            bus.close()
        except Exception as e:
            log(f"Error closing SMBus: {e}")
        bus = None

    if os.path.isfile(pidfile):
        try:
            os.unlink(pidfile)
            log(f"PID file {pidfile} removed.")
        except Exception as e:
            log(f"Error removing PID file {pidfile}: {e}")

    log("Exiting.")
    sys.exit(0)


def read_voltage(current_bus):
    read = current_bus.read_word_data(I2C_ADDRESS, REG_VOLTAGE)
    swapped = struct.unpack("<H", struct.pack(">H", read))[0]
    return swapped * 1.25 / 1000 / 16


def read_capacity(current_bus):
    read = current_bus.read_word_data(I2C_ADDRESS, REG_CAPACITY)
    swapped = struct.unpack("<H", struct.pack(">H", read))[0]
    return swapped / 256


def get_battery_status(voltage, min_voltage_shutdown):
    if 3.87 <= voltage <= 4.25:
        return "Full"
    if 3.7 <= voltage < 3.87:
        return "High"
    if 3.55 <= voltage < 3.7:
        return "Medium"
    if 3.4 <= voltage < 3.55:
        return "Low"
    if min_voltage_shutdown <= voltage < 3.4:
        return "Very Low"
    if voltage < min_voltage_shutdown:
        return "Critical"
    return "Unknown"


def ensure_single_instance(target_pidfile):
    if os.path.isfile(target_pidfile):
        try:
            with open(target_pidfile, "r") as pid_file:
                old_pid = int(pid_file.read().strip())
            os.kill(old_pid, 0)
            log(f"Script already running with PID {old_pid}. Exiting.")
            sys.exit(1)
        except (IOError, ValueError, ProcessLookupError):
            log(f"Stale PID file {target_pidfile} found. Removing.")
            try:
                os.unlink(target_pidfile)
            except OSError as e:
                log(f"Error removing stale PID file: {e}. Continuing...")

    try:
        with open(target_pidfile, "w") as pid_file:
            pid_file.write(str(os.getpid()))
        log(f"PID file {target_pidfile} created with PID {os.getpid()}.")
    except IOError as e:
        log(f"Could not create PID file {target_pidfile}: {e}. Exiting.")
        sys.exit(1)


def parse_args():
    parser = argparse.ArgumentParser(
        description="X120x UPS monitor (v2 canary). Use --dry-run for no shutdown.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not execute shutdown command.")
    parser.add_argument(
        "--no-pld",
        action="store_true",
        help="Do not read PLD GPIO (allows running alongside ups-monitor.service).",
    )
    parser.add_argument("--pidfile", default=DEFAULT_PIDFILE, help=f"PID file path (default: {DEFAULT_PIDFILE}).")
    parser.add_argument(
        "--iterations",
        type=int,
        default=0,
        help="Number of loop iterations to run (0 = run forever). Useful for testing.",
    )
    parser.add_argument(
        "--ac-loss-confirmations",
        type=int,
        default=DEFAULT_AC_LOSS_CONFIRMATIONS,
        help=f"Consecutive PLD=0 reads required to consider AC lost (default: {DEFAULT_AC_LOSS_CONFIRMATIONS}).",
    )
    parser.add_argument(
        "--shutdown-confirmations",
        type=int,
        default=DEFAULT_SHUTDOWN_CONFIRMATIONS,
        help=f"Consecutive critical readings required before shutdown (default: {DEFAULT_SHUTDOWN_CONFIRMATIONS}).",
    )
    parser.add_argument(
        "--min-capacity-shutdown",
        type=float,
        default=DEFAULT_MIN_CAPACITY_SHUTDOWN,
        help=f"Shutdown if capacity below this %% while on battery (default: {DEFAULT_MIN_CAPACITY_SHUTDOWN}).",
    )
    parser.add_argument(
        "--min-voltage-shutdown",
        type=float,
        default=DEFAULT_MIN_VOLTAGE_SHUTDOWN,
        help=f"Shutdown if voltage below this V while on battery (default: {DEFAULT_MIN_VOLTAGE_SHUTDOWN}).",
    )
    parser.add_argument(
        "--sleep",
        type=int,
        default=DEFAULT_SLEEP_TIME,
        help=f"Seconds between checks (default: {DEFAULT_SLEEP_TIME}).",
    )
    return parser.parse_args()


def shutdown_now(dry_run):
    if dry_run:
        log("DRY-RUN: shutdown suppressed (would run: shutdown -h now)")
        return

    cmd = ["shutdown", "-h", "now"]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n"] + cmd

    try:
        rc = call(cmd)
        log(f"Shutdown command exited with code {rc}.")
    except FileNotFoundError as e:
        log(f"Shutdown command not found: {e}")
    except Exception as e:
        log(f"Shutdown command failed: {e}")


def main():
    args = parse_args()

    global pidfile
    pidfile = args.pidfile

    signal.signal(signal.SIGINT, cleanup_and_exit)
    signal.signal(signal.SIGTERM, cleanup_and_exit)

    log("Script starting...")
    if args.dry_run:
        log("DRY-RUN enabled: shutdown will NOT be executed.")
    if args.no_pld:
        log("NO-PLD enabled: will not read AC/adapter state from GPIO.")
    if args.iterations < 0:
        log("Invalid --iterations (must be >= 0). Exiting.")
        sys.exit(2)
    if args.ac_loss_confirmations < 1:
        log("Invalid --ac-loss-confirmations (must be >= 1). Exiting.")
        sys.exit(2)
    if args.shutdown_confirmations < 1:
        log("Invalid --shutdown-confirmations (must be >= 1). Exiting.")
        sys.exit(2)

    ensure_single_instance(pidfile)

    global bus
    global pld_line

    try:
        log("Initializing SMBus...")
        bus = smbus2.SMBus(I2C_BUS_NUMBER)
        log("SMBus initialized.")

        if not args.no_pld:
            log(f"Initializing GPIO for PLD (pin {PLD_PIN})...")
            chip = gpiod.Chip(GPIO_CHIP)
            pld_line = chip.get_line(PLD_PIN)
            pld_line.request(consumer="PLD-v2", type=gpiod.LINE_REQ_DIR_IN)
            log("GPIO for PLD initialized.")

        log("Entering main monitoring loop...")
        log(
            f"Shutdown policy (only when on battery): capacity < {args.min_capacity_shutdown}% OR voltage < {args.min_voltage_shutdown}V.",
        )
        log(
            f"Confirmations: AC loss={args.ac_loss_confirmations}, shutdown={args.shutdown_confirmations}.",
        )

        ac_power_lost_consecutive_checks = 0
        critical_consecutive_checks = 0
        iterations_remaining = args.iterations

        while True:
            if args.no_pld:
                ac_power_state = None
                ac_power_state_str = "Unknown (PLD disabled)"
            else:
                ac_power_state = pld_line.get_value()
                ac_power_state_str = "Plugged in" if ac_power_state == 1 else "Unplugged"

            raw_pld = "N/A" if ac_power_state is None else str(ac_power_state)
            voltage = None
            capacity = None
            try:
                voltage = read_voltage(bus)
                capacity = read_capacity(bus)
            except OSError as e:
                log(f"I2C read error: {e}")
            except Exception as e:
                log(f"Unexpected sensor read error: {e}")

            if voltage is None or capacity is None:
                log(
                    f"Capacity: N/A, AC Power State: {ac_power_state_str} (Raw PLD: {raw_pld}), Voltage: N/A",
                )
                critical_consecutive_checks = 0
            else:
                if not (0.0 <= capacity <= 100.0):
                    log(
                        f"Out-of-range capacity reading: {capacity:.2f}%. Ignoring this sample.",
                    )
                    critical_consecutive_checks = 0
                    capacity = None
                if not (0.0 < voltage < 6.0):
                    log(
                        f"Out-of-range voltage reading: {voltage:.2f}V. Ignoring this sample.",
                    )
                    critical_consecutive_checks = 0
                    voltage = None

            if voltage is not None and capacity is not None:
                battery_status_str = get_battery_status(voltage, args.min_voltage_shutdown)
                log(
                    f"Capacity: {capacity:.2f}% ({battery_status_str}), AC Power State: {ac_power_state_str} (Raw PLD: {raw_pld}), Voltage: {voltage:.2f}V",
                )

            shutdown_reason = ""
            should_shutdown = False

            if ac_power_state == 0:
                ac_power_lost_consecutive_checks += 1
                log(
                    f"UPS is unplugged or AC power loss detected. (Consecutive check: {ac_power_lost_consecutive_checks})",
                )
                if ac_power_lost_consecutive_checks >= args.ac_loss_confirmations and voltage is not None and capacity is not None:
                    if capacity < args.min_capacity_shutdown:
                        shutdown_reason = (
                            f"due to critical battery level ({capacity:.2f}% < {args.min_capacity_shutdown}%)."
                        )
                        should_shutdown = True
                    elif voltage < args.min_voltage_shutdown:
                        shutdown_reason = (
                            f"due to critical battery voltage ({voltage:.2f}V < {args.min_voltage_shutdown}V)."
                        )
                        should_shutdown = True
            elif ac_power_state == 1:
                if ac_power_lost_consecutive_checks > 0:
                    log("AC Power restored. Resetting AC loss counter.")
                ac_power_lost_consecutive_checks = 0
                critical_consecutive_checks = 0
            else:
                ac_power_lost_consecutive_checks = 0
                critical_consecutive_checks = 0

            if should_shutdown:
                critical_consecutive_checks += 1
                log(
                    f"Critical condition detected ({critical_consecutive_checks}/{args.shutdown_confirmations}).",
                )
            else:
                critical_consecutive_checks = 0

            if critical_consecutive_checks >= args.shutdown_confirmations:
                shutdown_message = f"Critical condition met {shutdown_reason} Initiating shutdown."
                log(shutdown_message)
                shutdown_now(args.dry_run)
                time.sleep(30)

            if iterations_remaining > 0:
                iterations_remaining -= 1
                if iterations_remaining == 0:
                    log("Test iterations complete. Exiting.")
                    break

            time.sleep(args.sleep)

    except Exception as e:
        log(f"An unhandled exception occurred: {e}")
        cleanup_and_exit()

    cleanup_and_exit()


if __name__ == "__main__":
    main()
