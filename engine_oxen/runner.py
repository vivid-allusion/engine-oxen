"""Bounded parallel batches: at most ``concurrency`` jobs in flight.

Split out of ``engine.py`` so the sequential path stays readable. The contract
the Generator relies on is unchanged: results come back in input order, one
entry per saved file or per failure, exactly as a one-at-a-time run.

Two rules that are not obvious:

* a queued item is checked against the stop flag **before** it calls the
  provider, so a fatal error stops the batch instead of finishing it;
* the stop flag is set under a lock — the workers are threads, and "check then
  set" on a plain attribute is a race that loses work.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from .datatypes import InputFile, OutputFile


def run_batch(engine, inputs: list[InputFile]) -> list[OutputFile]:
    """Run every input through ``engine`` with the profile's concurrency."""
    workers = max(1, int(engine._concurrency))
    if workers == 1 or len(inputs) <= 1:
        return engine._run_serial(inputs)

    stop = threading.Event()
    lock = threading.Lock()
    results: list[list[OutputFile]] = [[] for _ in inputs]

    def work(index: int, item: InputFile) -> None:
        if stop.is_set():
            results[index] = [
                OutputFile(
                    source_path=item.path,
                    status="error",
                    error_msg="Skipped: the batch stopped after an unrecoverable error",
                    media_type=engine._media_type(),
                )
            ]
            return
        try:
            results[index] = engine._run_one(index, len(inputs), item, stop)
        except Exception as exc:  # noqa: BLE001 - a worker must never vanish
            results[index] = [
                OutputFile(
                    source_path=item.path,
                    status="error",
                    error_msg=str(exc),
                    media_type=engine._media_type(),
                )
            ]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, index, item) for index, item in enumerate(inputs)]
        for future in futures:
            future.result()

    flat: list[OutputFile] = []
    for group in results:
        flat.extend(group)
    return flat


__all__ = ["run_batch"]
