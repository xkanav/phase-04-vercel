import sys
import os
from pathlib import Path
import threading
import time

ROOT = Path(__file__).resolve().parent.parent / "prototype"
sys.path.append(str(ROOT))

from server import Handler
from app import App

data_dir = Path("/tmp/data")
data_dir.mkdir(parents=True, exist_ok=True)

# Vercel environments are ephemeral, so we run the App pointing its data to /tmp.
# State will reset when the serverless function cold starts.
app_instance = App(data=data_dir, project=ROOT.parent, auto_worker=True)

class handler(Handler):
    def __init__(self, *args, **kwargs):
        class MockServer:
            app = app_instance
            server_port = 443
        self.server = MockServer()
        super().__init__(*args, **kwargs)

    def trusted(self):
        # Vercel handles routing, so we trust all incoming traffic to this handler
        return True
