"""Shared error types."""

from __future__ import annotations


class ProviderError(Exception):
    def __init__(self, message: str, status: int | None = None, provider: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.provider = provider
        self.retryable = retryable


class HttpError(Exception):
    def __init__(self, status_code: int, message: str, code: str = "bad_request") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
