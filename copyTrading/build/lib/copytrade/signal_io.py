"""Append-only daily JSONL transport with byte-offset reading."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .protocol import Signal, SignalValidationError, encode_signal, parse_signal

logger = logging.getLogger(__name__)


def signal_filename(day: date | datetime | None = None) -> str:
    day = day or datetime.now().astimezone()
    return f"sinyal_{day:%Y%m%d}.jsonl"


def signal_path(folder: str | Path, day: date | datetime | None = None) -> Path:
    return Path(folder) / signal_filename(day)


def append_signal(folder: str | Path, signal: Signal) -> Path:
    path = signal_path(folder, datetime.now().astimezone())
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (encode_signal(signal) + "\n").encode("utf-8")
    with path.open("ab", buffering=0) as stream:
        stream.write(payload)
    return path


@dataclass(frozen=True, slots=True)
class SignalRecord:
    signal: Signal
    end_offset: int


@dataclass(frozen=True, slots=True)
class ReadResult:
    records: tuple[SignalRecord, ...]
    offset: int
    has_partial_line: bool


def read_new(path: str | Path, offset: int = 0) -> ReadResult:
    """Read complete lines after offset; leave a trailing partial line unread."""
    path = Path(path)
    if not path.exists():
        return ReadResult((), offset, False)
    size = path.stat().st_size
    start = max(0, offset)
    if start > size:
        logger.warning("File sinyal %s lebih pendek dari offset tersimpan; membaca ulang dari awal", path)
        start = 0
    with path.open("rb") as stream:
        stream.seek(start)
        data = stream.read()
    records: list[SignalRecord] = []
    cursor = 0
    partial = bool(data and not data.endswith((b"\n", b"\r")))
    for raw_line in data.splitlines(keepends=True):
        if not raw_line.endswith((b"\n", b"\r")):
            break
        cursor += len(raw_line)
        line = raw_line.strip()
        if not line:
            continue
        try:
            signal = parse_signal(line)
        except SignalValidationError as exc:
            logger.error("Baris sinyal rusak di %s byte %d: %s", path, start + cursor - len(raw_line), exc)
            continue
        records.append(SignalRecord(signal, start + cursor))
    return ReadResult(tuple(records), start + cursor, partial)


def next_day(day: date) -> date:
    return day + timedelta(days=1)
