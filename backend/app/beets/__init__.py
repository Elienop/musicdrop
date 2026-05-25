"""beets-adapter boundary.

ALL interaction with beets (the music library/tagger engine) goes through this
package. Do NOT `import beets` or `import beetsplug` anywhere else in the
codebase — beets' process-global config + plugin registry and version quirks
stay isolated here, behind typed adapter functions.
"""
