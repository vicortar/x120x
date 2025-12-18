#!/usr/bin/python3

import os
import struct
import smbus2
import time
import logging
import subprocess
import gpiod
from subprocess import call
import signal
import sys

# --- 用户可配置变量 ---
MIN_CAPACITY_SHUTDOWN = 30  # 如果电量低于此百分比则关机
MIN_VOLTAGE_SHUTDOWN = 3.20 # 如果电压低于此值则关机
SLEEP_TIME = 60  # 检查间隔时间（秒）
Loop = True

# --- 全局变量与信号处理 ---
pidfile = "/var/run/X1200.pid"
bus = None

def cleanup_and_exit(signum, frame):
    """信号处理程序，用于优雅地关闭。"""
    print("UPS Monitor: Received termination signal. Cleaning up...", flush=True)
    if bus:
        try: bus.close()
        except Exception as e: print(f"UPS Monitor: Error closing SMBus: {e}", flush=True)
    if os.path.isfile(pidfile):
        try:
            os.unlink(pidfile)
            print(f"UPS Monitor: PID file {pidfile} removed.", flush=True)
        except Exception as e: print(f"UPS Monitor: Error removing PID file {pidfile}: {e}", flush=True)
    print("UPS Monitor: Exiting.", flush=True)
    sys.exit(0)

signal.signal(signal.SIGINT, cleanup_and_exit)
signal.signal(signal.SIGTERM, cleanup_and_exit)

def readVoltage(current_bus):
    read = current_bus.read_word_data(address, 2)
    swapped = struct.unpack("<H", struct.pack(">H", read))[0]
    return swapped * 1.25 / 1000 / 16

def readCapacity(current_bus):
    read = current_bus.read_word_data(address, 4)
    swapped = struct.unpack("<H", struct.pack(">H", read))[0]
    return swapped / 256

def get_battery_status(voltage):
    if 3.87 <= voltage <= 4.25: return "Full" # 稍微调高上限以适应充电电压
    elif 3.7 <= voltage < 3.87: return "High"
    elif 3.55 <= voltage < 3.7: return "Medium"
    elif 3.4 <= voltage < 3.55: return "Low"
    elif MIN_VOLTAGE_SHUTDOWN <= voltage < 3.4: return "Very Low"
    elif voltage < MIN_VOLTAGE_SHUTDOWN: return "Critical"
    else: return "Unknown"

# --- 主程序 ---
print("UPS Monitor: Script starting...", flush=True)

if os.path.isfile(pidfile):
    try:
        with open(pidfile, 'r') as pf: old_pid = int(pf.read().strip())
        os.kill(old_pid, 0)
        print(f"UPS Monitor: Script already running with PID {old_pid}. Exiting.", flush=True)
        sys.exit(1)
    except (IOError, ValueError, ProcessLookupError):
        print(f"UPS Monitor: Stale PID file {pidfile} found. Removing.", flush=True)
        try: os.unlink(pidfile)
        except OSError as e: print(f"UPS Monitor: Error removing stale PID file: {e}. Continuing...", flush=True)

try:
    with open(pidfile, 'w') as f: f.write(str(os.getpid()))
    print(f"UPS Monitor: PID file {pidfile} created with PID {os.getpid()}.", flush=True)
except IOError as e:
    print(f"UPS Monitor: Could not create PID file {pidfile}: {e}. Exiting.", flush=True)
    sys.exit(1)

try:
    print("UPS Monitor: Initializing SMBus...", flush=True)
    bus = smbus2.SMBus(1)
    address = 0x36
    print("UPS Monitor: SMBus initialized.", flush=True)

    PLD_PIN = 6
    print(f"UPS Monitor: Initializing GPIO for PLD (pin {PLD_PIN})...", flush=True)
    chip = gpiod.Chip('gpiochip0')
    pld_line = chip.get_line(PLD_PIN)
    pld_line.request(consumer="PLD", type=gpiod.LINE_REQ_DIR_IN)
    print("UPS Monitor: GPIO for PLD initialized.", flush=True)
    print("UPS Monitor: Entering main monitoring loop...", flush=True)
    print(f"UPS Monitor: Shutdown will occur if capacity < {MIN_CAPACITY_SHUTDOWN}% OR voltage < {MIN_VOLTAGE_SHUTDOWN}V while on battery.", flush=True)

    ac_power_lost_consecutive_checks = 0

    while True:
        ac_power_state = pld_line.get_value()
        voltage = readVoltage(bus)
        battery_status_str = get_battery_status(voltage)
        capacity = readCapacity(bus)

        ac_power_state_str = 'Plugged in' if ac_power_state == 1 else 'Unplugged'
        print(f"UPS Monitor: Capacity: {capacity:.2f}% ({battery_status_str}), AC Power State: {ac_power_state_str} (Raw PLD: {ac_power_state}), Voltage: {voltage:.2f}V", flush=True)

        shutdown_now = False
        shutdown_reason = ""

        if ac_power_state == 0:
            ac_power_lost_consecutive_checks += 1
            print(f"UPS Monitor: UPS is unplugged or AC power loss detected. (Consecutive check: {ac_power_lost_consecutive_checks})", flush=True)
            if capacity < MIN_CAPACITY_SHUTDOWN:
                shutdown_reason = f"due to critical battery level ({capacity:.2f}% < {MIN_CAPACITY_SHUTDOWN}%)."
                shutdown_now = True
            elif voltage < MIN_VOLTAGE_SHUTDOWN:
                shutdown_reason = f"due to critical battery voltage ({voltage:.2f}V < {MIN_VOLTAGE_SHUTDOWN}V)."
                shutdown_now = True
        else:
            if ac_power_lost_consecutive_checks > 0:
                print(f"UPS Monitor: AC Power Restored. Resetting AC loss counter.", flush=True)
            ac_power_lost_consecutive_checks = 0

        if shutdown_now:
            shutdown_message = f"UPS Monitor: Critical condition met {shutdown_reason} Initiating shutdown."
            print(shutdown_message, flush=True)
            call("sudo nohup shutdown -h now", shell=True)
            time.sleep(30)
        
        if Loop:
            time.sleep(SLEEP_TIME)
        else:
            cleanup_and_exit(None, None)

except Exception as e:
    print(f"UPS Monitor: An unhandled exception occurred: {e}", flush=True)
    cleanup_and_exit(None, None)

print("UPS Monitor: Script unexpectedly reached end of execution.", flush=True)
cleanup_and_exit(None, None)
