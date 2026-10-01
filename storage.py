"""
Minimal thread-safe JSON-file storage. This will be swapped with a real database before actually deploying anywhere.
This is here just so the hackathon build has zero external dependencies to set up.
"""


import json
import os
import threading
from typing import Any


class JSONStore:
    def __init__(self, path: str, default: Any):
        self.path = path
        self._lock = threading.Lock()
        if not os.path.exists(self.path):
            self._write_locked(default)
 
    def _write_locked(self, data: Any) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp_path, self.path)
 
    def read(self) -> Any:
        with self._lock:
            if not os.path.exists(self.path):
                return []
            with open(self.path) as f:
                return json.load(f)
 
    def write(self, data: Any) -> None:
        with self._lock:
            self._write_locked(data)