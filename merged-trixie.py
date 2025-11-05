#!/usr/bin/python3

import os
import struct
import smbus2
import time
import gpiod
from subprocess import call

# User-configurable variables
SHUTDOWN_THRESHOLD = 3  # Number of consecutive failures required for shutdown
SLEEP_TIME = 60  # Time in seconds to wait between failure checks
MONITOR_INTERVAL = 3  # Seconds between monitoring checks

def readVoltage(bus):
    try:
        read = bus.read_word_data(address, 2)
        swapped = struct.unpack("<H", struct.pack(">H", read))[0]
        voltage = swapped * 1.25 / 1000 / 16
        return voltage
    except Exception:
        return 0

def readCapacity(bus):
    try:
        read = bus.read_word_data(address, 4)
        swapped = struct.unpack("<H", struct.pack(">H", read))[0]
        capacity = swapped / 256
        return capacity
    except Exception:
        return 0

def get_battery_status(voltage):
    if 3.87 <= voltage <= 4.23:
        return "Full"
    elif 3.7 <= voltage < 3.87:
        return "High"
    elif 3.55 <= voltage < 3.7:
        return "Medium"
    elif 3.4 <= voltage < 3.55:
        return "Low"
    elif voltage < 3.4:
        return "Critical"
    else:
        return "Unknown"

# Ensure only one instance of the script is running
pid = str(os.getpid())
pidfile = "/tmp/X1200.pid"
if os.path.isfile(pidfile):
    exit(1)
else:
    with open(pidfile, 'w') as f:
        f.write(pid)

try:
    # Initialize I2C bus
    bus = smbus2.SMBus(1)
    address = 0x36
    
    # Initialize GPIO
    PLD_PIN = 6
    request = gpiod.request_lines(
        '/dev/gpiochip0',
        consumer="PLD",
        config={
            PLD_PIN: gpiod.LineSettings(direction=gpiod.line.Direction.INPUT)
        }
    )

    failure_counter = 0

    while True:
        # Read GPIO value
        values = request.get_values()
        ac_power_state = values[PLD_PIN] if isinstance(values, dict) else values[0]
        
        # Read battery information
        voltage = readVoltage(bus)
        battery_status = get_battery_status(voltage)
        capacity = readCapacity(bus)
        
        # Display current status
        print(f"Battery: {capacity:.1f}% ({battery_status}), Voltage: {voltage:.2f}V, AC Power: {'Plugged in' if ac_power_state == gpiod.line.Value.ACTIVE else 'Unplugged'}")
        
        # Check conditions
        current_failures = 0
        
        if ac_power_state != gpiod.line.Value.ACTIVE:
            current_failures += 1
        
        if capacity < 20:
            current_failures += 1
        
        if voltage < 3.20:
            current_failures += 1
        
        # Update failure counter
        if current_failures > 0:
            failure_counter += 1
        else:
            failure_counter = 0
        
        # Check if shutdown threshold reached
        if failure_counter >= SHUTDOWN_THRESHOLD:
            shutdown_reason = []
            if capacity < 20:
                shutdown_reason.append("critical battery level")
            if voltage < 3.20:
                shutdown_reason.append("critical battery voltage")
            if ac_power_state != gpiod.line.Value.ACTIVE:
                shutdown_reason.append("AC power loss")
            
            reason_text = " and ".join(shutdown_reason)
            print(f"Critical condition met due to {reason_text}. Initiating shutdown.")
            
            # Uncomment to enable actual shutdown
            # call("sudo nohup shutdown -h now", shell=True)
            # break
        
        # Wait for next monitoring interval
        time.sleep(MONITOR_INTERVAL)

except KeyboardInterrupt:
    pass

except Exception:
    pass

finally:
    # Cleanup
    try:
        if 'request' in locals():
            request.release()
    except:
        pass
    
    try:
        if 'bus' in locals():
            bus.close()
    except:
        pass
    
    if os.path.isfile(pidfile):
        os.unlink(pidfile)
