"""
Scopenos background worker — processes indexing jobs from the Redis queue.

Run with:
    python -m src.worker

Or in Docker:
    docker compose run scopenos-worker
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure src/ is importable when run directly
sys.path.insert(0, str(Path(__file__).parent.parent))

from rq import Worker
from .queue import get_redis, QUEUE_NAME


def main() -> None:
    redis = get_redis()
    queues = [QUEUE_NAME]
    print(f"[worker] Starting RQ worker — queues: {queues}")
    print(f"[worker] Redis: {os.getenv('REDIS_URL', 'redis://localhost:6379')}")
    print(f"[worker] Database: {os.getenv('DATABASE_URL', 'postgresql://scopenos:scopenos@localhost/scopenos')}")
    worker = Worker(queues, connection=redis)
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
