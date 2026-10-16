"""Backward-compatible import for the canonical T05 reader.

The production implementation lives under ``yoyoflot.readers``. Keeping this
module as a thin adapter prevents the legacy PyCharm import from drifting into
a second, non-streaming implementation.
"""

from yoyoflot.readers.boarding_csv import EXPECTED_COLUMNS, read

__all__ = ["EXPECTED_COLUMNS", "read"]
