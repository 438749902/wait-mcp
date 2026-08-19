import sys
import time

seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 120
for i in range(seconds):
    print(f"step={i + 1}/{seconds}", flush=True)
    time.sleep(1)
print("experiment complete", flush=True)
