"""
Single instance lock - prevents multiple copies of the app from running.
If already running, signals the existing instance to show itself, then exits.
"""
import os
import socket
import sys
import threading

LOCK_DIR = os.path.join(os.path.expanduser("~"), ".quickfind")


def _lock_file(app_name: str) -> str:
    os.makedirs(LOCK_DIR, exist_ok=True)
    return os.path.join(LOCK_DIR, f"{app_name}.lock")


def ensure_single_instance(app_name: str, on_show_callback=None) -> bool:
    """
    Ensure only one instance of the app runs.

    Returns True if this is the first instance (proceed normally).
    Returns False and exits if another instance is already running.
    """
    lock_path = _lock_file(app_name)

    # Check if another instance is running
    if os.path.exists(lock_path):
        try:
            with open(lock_path, "r") as f:
                port = int(f.read().strip())
            # Try to connect and send "show"
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            sock.connect(("127.0.0.1", port))
            sock.send(b"show")
            sock.close()
            # Other instance is alive — exit this one
            sys.exit(0)
        except (ConnectionRefusedError, OSError, ValueError):
            # Lock file is stale — remove it and continue
            os.remove(lock_path)

    # This is the first instance — create lock
    port = _start_listener(app_name, on_show_callback)
    with open(lock_path, "w") as f:
        f.write(str(port))

    return True


def _start_listener(app_name: str, on_show_callback) -> int:
    """Start a background listener that receives 'show' commands."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))  # Random available port
    port = server.getsockname()[1]
    server.listen(1)

    def listen():
        while True:
            try:
                conn, _ = server.accept()
                data = conn.recv(64)
                conn.close()
                if data == b"show" and on_show_callback:
                    on_show_callback()
            except OSError:
                break

    t = threading.Thread(target=listen, daemon=True)
    t.start()
    return port


def cleanup_lock(app_name: str):
    """Remove lock file on exit."""
    lock_path = _lock_file(app_name)
    try:
        os.remove(lock_path)
    except OSError:
        pass
