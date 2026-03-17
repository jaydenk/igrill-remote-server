"""BLE connection state machine with exponential backoff."""

import enum
import random
from typing import Callable, Optional


class ConnectionState(enum.Enum):
    DISCOVERED = "discovered"
    CONNECTING = "connecting"
    AUTHENTICATING = "authenticating"
    POLLING = "polling"
    DISCONNECTED = "disconnected"
    BACKOFF = "backoff"


class ConnectionStateMachine:
    """Tracks connection state and manages backoff timing."""

    def __init__(
        self,
        initial_backoff: float = 2.0,
        max_backoff: float = 60.0,
        jitter_factor: float = 0.25,
        on_change: Optional[Callable[[ConnectionState, ConnectionState], None]] = None,
    ) -> None:
        self._state = ConnectionState.DISCOVERED
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._jitter_factor = jitter_factor
        self._on_change = on_change
        self._consecutive_failures = 0
        self._backoff_seconds = initial_backoff
        self._had_successful_connection = False

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def backoff_seconds(self) -> float:
        return self._backoff_seconds

    def transition(self, new_state: ConnectionState) -> None:
        old_state = self._state
        if new_state == old_state:
            return

        # Successful connection resets failure count
        if new_state == ConnectionState.POLLING:
            self._had_successful_connection = True
            self._consecutive_failures = 0

        # Calculate backoff on entering BACKOFF state
        if new_state == ConnectionState.BACKOFF:
            if self._had_successful_connection:
                self._consecutive_failures = 0
                self._had_successful_connection = False
            self._consecutive_failures += 1
            base = self._initial_backoff * (2 ** (self._consecutive_failures - 1))
            capped = min(base, self._max_backoff)
            jitter = random.uniform(0, capped * self._jitter_factor)
            self._backoff_seconds = capped + jitter

        self._state = new_state
        if self._on_change:
            self._on_change(old_state, new_state)
