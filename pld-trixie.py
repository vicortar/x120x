#!/usr/bin/env python3
# This python script is only suitable for UPS Shield X12xx series

import gpiod
import time
from subprocess import call

PLD_PIN = 6

try:
    with gpiod.request_lines(
        '/dev/gpiochip0',
        consumer="PLD",
        config={
            PLD_PIN: gpiod.LineSettings(direction=gpiod.line.Direction.INPUT)
        }
    ) as request:
        
        print("Power monitoring started. Press Ctrl+C to stop.")
        
        while True:
            # If it returns a list, use index 0
            values = request.get_values()
            pld_state = values[0]  # Use the first element
            
            if pld_state == gpiod.line.Value.ACTIVE:
                print("✓ AC Power OK, Power Adapter OK")
            else:
                print("✗ AC Power Loss OR Power Adapter Failure")
                # Uncomment to enable automatic shutdown
                # call("sudo shutdown -h now", shell=True)
            
            time.sleep(1)

except KeyboardInterrupt:
    print("\nMonitoring stopped")
except Exception as e:
    print(f"Error: {e}")