"""Artist-image artwork module.

External-HTTP artwork resolution (Deezer artist portraits), strictly outside the
beets adapter (`app/beets/`). This package does NOT import beets/beetsplug/
mediafile — it only takes a plain artist-name ``str`` and returns image bytes.
"""
