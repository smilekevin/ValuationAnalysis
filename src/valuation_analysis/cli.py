from __future__ import annotations

import argparse
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the valuation analysis service.")
    parser.add_argument(
        "--env-file",
        help="Optional env file to load. By default only process environment variables are used.",
    )
    parser.add_argument("--host", help="Host to bind. Defaults to HOST or settings default.")
    parser.add_argument("--port", type=int, help="Port to bind. Defaults to PORT or settings default.")
    parser.add_argument("--reload", action="store_true", help="Enable uvicorn auto-reload.")
    args = parser.parse_args()

    if args.env_file:
        os.environ["VALUATION_ANALYSIS_ENV_FILE"] = args.env_file

    from valuation_analysis.config import settings

    uvicorn.run(
        "valuation_analysis.api:app",
        host=args.host or settings.host,
        port=args.port or settings.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
