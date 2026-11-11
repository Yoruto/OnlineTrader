"""Typed, safe errors crossing internal and IPC boundaries."""


class TraderError(ValueError):
    def __init__(self, code: str, message: str = "Request refused") -> None:
        self.code = code
        super().__init__(message)
