"""Launch the live scanner with local runtime dependencies.

This is a small Windows-friendly wrapper for running the bot via pythonw.exe.
It redirects stdout/stderr before importing main.py because pythonw.exe has no
console streams.
"""

from pathlib import Path
import os
import socket
import sys


BASE = Path(__file__).resolve().parent
os.chdir(BASE)

# Single-instance guard: hold a localhost port for the process lifetime.
# The Task Scheduler keepalive trigger re-runs this script every 15 minutes;
# when the bot is already alive the bind fails and this launch exits silently
# (no log spam). The OS releases the port when the process dies, so a crashed
# bot never leaves a stale lock.
_instance_lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    _instance_lock.bind(("127.0.0.1", 47831))
except OSError:
    sys.exit(0)

stdout = open(BASE / "live_stdout.log", "a", encoding="utf-8", buffering=1)
stderr = open(BASE / "live_stderr.log", "a", encoding="utf-8", buffering=1)
sys.stdout = stdout
sys.stderr = stderr

import main


main.main()
