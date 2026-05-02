from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class MetricsRegistry:
    started_at: float = field(default_factory=time.time)
    api_requests: int = 0
    api_errors: int = 0
    instruction_steps: int = 0
    run_invocations: int = 0
    total_run_seconds: float = 0.0
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def record_api_request(self, *, error: bool = False) -> None:
        with self._lock:
            self.api_requests += 1
            if error:
                self.api_errors += 1

    def record_run(self, *, steps: int, elapsed_seconds: float) -> None:
        with self._lock:
            self.instruction_steps += max(0, int(steps))
            self.run_invocations += 1
            self.total_run_seconds += max(0.0, float(elapsed_seconds))

    def snapshot(self, *, active_sessions: int, estimated_bytes: int) -> dict[str, float | int]:
        with self._lock:
            steps_per_second = self.instruction_steps / self.total_run_seconds if self.total_run_seconds > 0 else 0.0
            return {
                "uptime_seconds": round(time.time() - self.started_at, 3),
                "api_requests": self.api_requests,
                "api_errors": self.api_errors,
                "instruction_steps": self.instruction_steps,
                "run_invocations": self.run_invocations,
                "steps_per_second": round(steps_per_second, 3),
                "active_sessions": active_sessions,
                "estimated_session_bytes": estimated_bytes,
            }
