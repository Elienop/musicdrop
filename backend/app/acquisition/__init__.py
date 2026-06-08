"""The acquisition seam: completion signals -> the existing import pipeline.

This package owns the producer-agnostic plumbing that turns a finished download
folder into an unattended beets import: inbox-dir resolution + a path
containment guard (``inbox``), an atomic import ledger (``ledger``), and a serial
FIFO queue that drains folders through the existing ``ImportJobRegistry``
(``queue``). It does NOT import beets/beetsplug — all beets access stays behind
``app/beets/``; this package drives imports through the public registry seam.
"""
