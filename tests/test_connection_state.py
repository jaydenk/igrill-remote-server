"""Tests for BLE connection state machine."""

from service.ble.connection_state import ConnectionState, ConnectionStateMachine


def test_initial_state():
    sm = ConnectionStateMachine()
    assert sm.state == ConnectionState.DISCOVERED


def test_transition_discovered_to_connecting():
    sm = ConnectionStateMachine()
    sm.transition(ConnectionState.CONNECTING)
    assert sm.state == ConnectionState.CONNECTING


def test_full_happy_path():
    sm = ConnectionStateMachine()
    sm.transition(ConnectionState.CONNECTING)
    sm.transition(ConnectionState.AUTHENTICATING)
    sm.transition(ConnectionState.POLLING)
    assert sm.state == ConnectionState.POLLING


def test_disconnect_resets():
    sm = ConnectionStateMachine()
    sm.transition(ConnectionState.CONNECTING)
    sm.transition(ConnectionState.DISCONNECTED)
    assert sm.state == ConnectionState.DISCONNECTED


def test_backoff_calculation():
    sm = ConnectionStateMachine(max_backoff=60, jitter_factor=0)
    sm.transition(ConnectionState.DISCONNECTED)
    sm.transition(ConnectionState.BACKOFF)
    assert sm.backoff_seconds == 2  # initial backoff


def test_exponential_backoff_increases():
    sm = ConnectionStateMachine(max_backoff=60, jitter_factor=0)
    # First failure
    sm.transition(ConnectionState.DISCONNECTED)
    sm.transition(ConnectionState.BACKOFF)
    first = sm.backoff_seconds
    # Reset for second attempt
    sm.transition(ConnectionState.CONNECTING)
    sm.transition(ConnectionState.DISCONNECTED)
    sm.transition(ConnectionState.BACKOFF)
    second = sm.backoff_seconds
    assert second > first


def test_backoff_capped_at_max():
    sm = ConnectionStateMachine(max_backoff=10, jitter_factor=0)
    for _ in range(20):
        sm.transition(ConnectionState.DISCONNECTED)
        sm.transition(ConnectionState.BACKOFF)
        sm.transition(ConnectionState.CONNECTING)
    sm.transition(ConnectionState.DISCONNECTED)
    sm.transition(ConnectionState.BACKOFF)
    assert sm.backoff_seconds <= 10


def test_successful_connection_resets_backoff():
    sm = ConnectionStateMachine(jitter_factor=0)
    # Build up backoff
    for _ in range(5):
        sm.transition(ConnectionState.DISCONNECTED)
        sm.transition(ConnectionState.BACKOFF)
        sm.transition(ConnectionState.CONNECTING)
    # Successful connection
    sm.transition(ConnectionState.AUTHENTICATING)
    sm.transition(ConnectionState.POLLING)
    # Disconnect after success
    sm.transition(ConnectionState.DISCONNECTED)
    sm.transition(ConnectionState.BACKOFF)
    assert sm.backoff_seconds == 2  # reset to initial


def test_state_change_callback():
    changes = []
    sm = ConnectionStateMachine(on_change=lambda old, new: changes.append((old, new)))
    sm.transition(ConnectionState.CONNECTING)
    assert len(changes) == 1
    assert changes[0] == (ConnectionState.DISCOVERED, ConnectionState.CONNECTING)


def test_no_callback_on_same_state():
    changes = []
    sm = ConnectionStateMachine(on_change=lambda old, new: changes.append((old, new)))
    sm.transition(ConnectionState.CONNECTING)
    sm.transition(ConnectionState.CONNECTING)  # same state
    assert len(changes) == 1  # no duplicate callback
