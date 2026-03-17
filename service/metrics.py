"""Lightweight Prometheus-compatible metrics registry.

No external dependencies — renders text exposition format for /metrics.
"""


class MetricsRegistry:
    """In-memory counters and gauges with Prometheus text output."""

    def __init__(self) -> None:
        self._counters: dict[str, float] = {}
        self._gauges: dict[str, float] = {}
        self._labelled: dict[str, dict[str, float]] = {}

    def inc(self, name: str, value: float = 1, labels: dict[str, str] | None = None) -> None:
        if labels:
            key = self._label_key(labels)
            self._labelled.setdefault(name, {})[key] = (
                self._labelled.get(name, {}).get(key, 0) + value
            )
        else:
            self._counters[name] = self._counters.get(name, 0) + value

    def set(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        if labels:
            key = self._label_key(labels)
            self._labelled.setdefault(name, {})[key] = value
        else:
            self._gauges[name] = value

    def get(self, name: str, labels: dict[str, str] | None = None) -> float:
        if labels:
            key = self._label_key(labels)
            return self._labelled.get(name, {}).get(key, 0)
        return self._counters.get(name, self._gauges.get(name, 0))

    def render(self) -> str:
        lines = []
        for name, value in sorted(self._gauges.items()):
            lines.append(f"{name} {value}")
        for name, value in sorted(self._counters.items()):
            lines.append(f"{name} {value}")
        for name, labelled in sorted(self._labelled.items()):
            for label_str, value in sorted(labelled.items()):
                lines.append(f"{name}{{{label_str}}} {value}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _label_key(labels: dict[str, str]) -> str:
        return ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
