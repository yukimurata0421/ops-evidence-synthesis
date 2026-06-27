from __future__ import annotations

import argparse
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve a local Ops Evidence SQLite DB in the browser.")
    parser.add_argument("--db", required=True, help="SQLite DB path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    os.environ["OES_STORE"] = "sqlite"
    os.environ["OES_DB_PATH"] = str(db_path)
    os.environ.setdefault("OES_GEMINI_PROVIDER", "local")
    os.environ.setdefault("OES_CLAUDE_PROVIDER", "local")
    os.environ.setdefault("OES_GPT_OSS_PROVIDER", "local")
    os.environ.setdefault("OES_MISTRAL_PROVIDER", "local")

    import uvicorn

    url = f"http://{args.host}:{args.port}"
    print(f"serving_db={db_path}")
    print(f"url={url}")
    print(f"review_targets_url={url}/review-targets?limit=10&pending_only=false")
    uvicorn.run("ops_evidence_synthesis.api:app", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
