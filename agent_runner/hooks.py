from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


@runtime_checkable
class CancellationSource(Protocol):
    @property
    def is_cancellation_requested(self) -> bool:
        ...


class LifecycleEventType(str, Enum):
    BACKEND_STARTED = "backend_started"
    INTERNAL_RETRY_STARTED = "internal_retry_started"
    CANCEL_REQUESTED = "cancel_requested"
    STALL_TIMEOUT = "stall_timeout"
    ATTEMPT_TIMEOUT = "attempt_timeout"
    CANCELED = "canceled"
    FAILED = "failed"
    SUCCEEDED = "succeeded"


@dataclass(frozen=True, slots=True, kw_only=True)
class LifecycleEvent:
    type: LifecycleEventType
    retry_index: int | None = None

    def __post_init__(self) -> None:
        if type(self.type) is not LifecycleEventType:
            raise TypeError("type must be LifecycleEventType")
        if self.type is LifecycleEventType.INTERNAL_RETRY_STARTED:
            if (
                isinstance(self.retry_index, bool)
                or not isinstance(self.retry_index, int)
                or self.retry_index <= 0
            ):
                raise ValueError(
                    "internal_retry_started requires a positive retry_index"
                )
        elif self.retry_index is not None:
            raise ValueError(
                "retry_index is only valid for internal_retry_started"
            )


@runtime_checkable
class LifecycleSink(Protocol):
    def on_lifecycle_event(self, event: LifecycleEvent) -> None:
        ...
