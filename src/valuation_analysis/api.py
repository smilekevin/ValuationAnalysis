from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
import threading
from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from valuation_analysis.config import settings
from valuation_analysis.providers.composite import CompositeMarketDataProvider
from valuation_analysis.repositories.universe import UniverseRepository
from valuation_analysis.services.valuation import ValuationService


app = FastAPI(title=settings.app_name)
web_dir = Path(__file__).resolve().parent / "web"

provider = CompositeMarketDataProvider()
universe_repository = UniverseRepository(settings.default_peer_universe_path)
valuation_service = ValuationService(provider, universe_repository)

app.mount("/static", StaticFiles(directory=web_dir), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(web_dir / "index.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.app_env}


def _sse_payload(event: str, payload: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(jsonable_encoder(payload), ensure_ascii=False)}\n\n"


@app.get("/analyze/{symbol}")
def analyze_company(
    symbol: str,
    peer_count: int = Query(default=5, ge=1, le=8),
) -> dict:
    try:
        analysis = valuation_service.analyze_company(symbol=symbol.upper(), peer_count=peer_count)
        return analysis.model_dump(mode="json")
    except Exception as exc:  # pragma: no cover - defensive API boundary
        raise HTTPException(status_code=500, detail=f"analysis failed for {symbol}: {exc}") from exc


@app.get("/analyze-stream/{symbol}")
async def analyze_company_stream(
    symbol: str,
    peer_count: int = Query(default=5, ge=1, le=8),
) -> StreamingResponse:
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def publish(event: str, payload: Any) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (event, payload))

    def progress_callback(message: str, level: str = "progress") -> None:
        publish(
            "log",
            {
                "message": message,
                "level": level,
                "timestamp": datetime.now().strftime("%H:%M:%S"),
            },
        )

    def worker() -> None:
        try:
            analysis = valuation_service.analyze_company(
                symbol=symbol.upper(),
                peer_count=peer_count,
                progress_callback=progress_callback,
            )
            publish("result", analysis.model_dump(mode="json"))
        except Exception as exc:  # pragma: no cover - defensive API boundary
            publish(
                "analysis-error",
                {"message": f"analysis failed for {symbol}: {exc}"},
            )
        finally:
            publish("done", {})

    threading.Thread(target=worker, daemon=True).start()

    async def event_generator():
        while True:
            event, payload = await queue.get()
            yield _sse_payload(event, payload)
            if event == "done":
                break

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
