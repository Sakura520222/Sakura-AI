"""Export API contracts without starting workers or connecting deployment services."""

import json
from pathlib import Path

from fastapi import FastAPI

from backend.api.v1 import api_v1_router


def main():
    app = FastAPI(title="Sakura AI API", version="v1")
    app.include_router(api_v1_router, prefix="/api/v1")
    target = Path(__file__).resolve().parents[1] / "web" / "openapi.json"
    target.write_text(
        json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
