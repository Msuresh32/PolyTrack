import os
import threading

workers = 1
bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
timeout = 120

def post_fork(server, worker):
    from dashboard import refresh_loop
    t = threading.Thread(target=refresh_loop, daemon=True)
    t.start()
