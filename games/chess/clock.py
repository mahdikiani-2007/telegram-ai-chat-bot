import time
from typing import Optional, Tuple


class ChessClock:
    """Server-authoritative chess clock using monotonic time for in-process accuracy
    and epoch timestamps for persistence recovery.
    """

    def __init__(self, white_ms: int, black_ms: int, increment_ms: int = 0):
        self.white_remaining_ms = int(white_ms)
        self.black_remaining_ms = int(black_ms)
        self.increment_ms = int(increment_ms)
        self._active_color: Optional[str] = None  # "white" | "black"
        self._turn_start: Optional[float] = None  # monotonic time

    @property
    def active_color(self) -> Optional[str]:
        return self._active_color

    def start(self, color: str) -> None:
        """Start the clock for the specified player."""
        self._active_color = color.lower()
        self._turn_start = time.monotonic()

    def stop_and_switch(self) -> int:
        """Stop active player's clock, deduct elapsed time, add increment.
        Returns elapsed ms on this turn.
        """
        if self._active_color is None or self._turn_start is None:
            return 0

        elapsed_ms = int((time.monotonic() - self._turn_start) * 1000)
        if self._active_color == "white":
            self.white_remaining_ms = max(0, self.white_remaining_ms - elapsed_ms)
            self.white_remaining_ms += self.increment_ms
        else:
            self.black_remaining_ms = max(0, self.black_remaining_ms - elapsed_ms)
            self.black_remaining_ms += self.increment_ms

        self._active_color = None
        self._turn_start = None
        return elapsed_ms

    def pause(self) -> int:
        """Pause the clock without adding increment (e.g. on game finish or timeout)."""
        if self._active_color is None or self._turn_start is None:
            return 0
        elapsed_ms = int((time.monotonic() - self._turn_start) * 1000)
        if self._active_color == "white":
            self.white_remaining_ms = max(0, self.white_remaining_ms - elapsed_ms)
        else:
            self.black_remaining_ms = max(0, self.black_remaining_ms - elapsed_ms)
        self._active_color = None
        self._turn_start = None
        return elapsed_ms

    def get_remaining(self) -> Tuple[int, int]:
        """Current remaining milliseconds for (white, black)."""
        w = self.white_remaining_ms
        b = self.black_remaining_ms
        if self._active_color and self._turn_start:
            elapsed = int((time.monotonic() - self._turn_start) * 1000)
            if self._active_color == "white":
                w = max(0, w - elapsed)
            else:
                b = max(0, b - elapsed)
        return w, b

    def is_flag_fallen(self) -> Optional[str]:
        """Returns color ('white' or 'black') whose time has expired, or None."""
        w, b = self.get_remaining()
        if w <= 0:
            return "white"
        if b <= 0:
            return "black"
        return None

    def restore(
        self,
        white_ms: int,
        black_ms: int,
        active_color: Optional[str],
        turn_started_at: Optional[float],
    ) -> None:
        """Restore clock state from database snapshot after a server restart."""
        self.white_remaining_ms = max(0, int(white_ms))
        self.black_remaining_ms = max(0, int(black_ms))

        if active_color and turn_started_at:
            # Wall clock delta elapsed since turn started before restart
            elapsed_ms = max(0, int((time.time() - turn_started_at) * 1000))
            if active_color == "white":
                self.white_remaining_ms = max(0, self.white_remaining_ms - elapsed_ms)
            else:
                self.black_remaining_ms = max(0, self.black_remaining_ms - elapsed_ms)

            # If still alive, continue tracking with monotonic clock
            if (active_color == "white" and self.white_remaining_ms > 0) or (
                active_color == "black" and self.black_remaining_ms > 0
            ):
                self._active_color = active_color
                self._turn_start = time.monotonic()
            else:
                self._active_color = None
                self._turn_start = None
        else:
            self._active_color = None
            self._turn_start = None
