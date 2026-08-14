"""Shared contract for user-facing note lines in event messages.

Dependency-neutral on purpose: both the add-note writer (workflow layer) and
the summary parser (render layer) import the marker from here, so neither
layer depends on the other and the two definitions cannot drift.
"""

NOTE_MARKER = "Note to user:"
