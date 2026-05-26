"""In-memory import-job lifecycle (chunk 2).

Owns the single active ImportJob, drains chunk-1's ImportBridge (per-album
outcomes + the at-most-one parked album) into a live feed, and delivers the
user's choice for the parked album back to the worker. Pure-typed: imports the
ImportBridge + our models, never beets directly (the beets contact lives in
app/import_jobs/runner.py and app/beets/).
"""
