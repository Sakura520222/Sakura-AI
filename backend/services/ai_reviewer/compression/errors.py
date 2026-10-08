"""Infrastructure failures that must not become optional compression fallback."""


class UsagePersistenceError(RuntimeError):
    """A required usage receipt could not be persisted; stop further AI work."""

    def __init__(self):
        super().__init__("AI usage persistence failed")
