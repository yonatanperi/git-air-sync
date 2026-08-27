"""Terminal interface.

These are the only modules allowed to import rich or questionary, and all capability
detection happens once in ``theme.py``. Everything else calls the six primitives in
``displays.py``, so the number of places that branch on "is rich available" stays at
six rather than growing with every new screen.
"""
