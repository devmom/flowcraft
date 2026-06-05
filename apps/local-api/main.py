"""FlowCraft Local API — 主入口.

启动方式:
    python apps/local-api/main.py
    # 或
    uvicorn apps.local-api.main:app --host 127.0.0.1 --port 8765
"""

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from flowcraft_core.api.server import app

# ── Web UI ───────────────────────────────────────────────────
WEB_DIR = Path(__file__).parent.parent.parent / "core" / "flowcraft_core" / "web"


@app.get("/", response_class=HTMLResponse)
async def index():
    index_path = WEB_DIR / "index.html"
    if index_path.exists():
        return HTMLResponse(index_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>FlowCraft</h1><p>Web UI not found.</p>")


# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")

