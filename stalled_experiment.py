import sys
import time

print("step=1/4", flush=True)
if len(sys.argv) > 1 and sys.argv[1] == "error":
    print("PicklingError: simulated archive failure", file=sys.stderr, flush=True)
time.sleep(30)
