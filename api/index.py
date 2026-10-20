import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from backend.main import app  # noqa: F401
except Exception:
    from fastapi import FastAPI
    from fastapi.responses import PlainTextResponse

    _tb = traceback.format_exc()
    app = FastAPI()

    @app.get("/{path:path}")
    def _error(path: str):
        return PlainTextResponse(_tb, status_code=500)