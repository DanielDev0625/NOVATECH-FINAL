import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

app = FastAPI()

try:
    from backend.main import app  # noqa: F811
except Exception:
    _tb = traceback.format_exc()

    @app.get("/{path:path}")
    def _error(path: str):
        return PlainTextResponse(_tb, status_code=500)