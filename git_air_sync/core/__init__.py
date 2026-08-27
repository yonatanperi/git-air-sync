"""Core logic. Imports nothing outside the stdlib and ``git_air_sync.vendor``.

This restriction is deliberate: it keeps the whole export/import pipeline testable
without click, rich, or questionary installed, and it is what lets the tool degrade
to plain text on an air-gapped machine where optional packages may be missing.
"""
