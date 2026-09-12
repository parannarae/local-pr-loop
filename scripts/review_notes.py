"""Shared contract for the tags a user-facing note may carry.

Dependency-neutral on purpose: the schema that validates a published
`note.attach` and the workflow command that composes one both import the tag
set from here, so neither layer depends on the other and the two definitions
cannot drift.
"""

NOTE_TAGS = ("action-required", "follow-up", "decision")
