"""Edge detection for PICO face-button combinations.

This module is copied into the G1 GR00T runtime and imported by the PICO
manager. Keeping the state transition pure makes read failures and partial
combo releases testable without loading XRT, torch, or robot hardware.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ButtonEdges:
    ax: bool
    by: bool
    start: bool


class AbxyDecoder:
    def __init__(self):
        self.prev_ax = False
        self.prev_by = False
        self.prev_start = False
        self.latched = False
        self.last: tuple[bool, bool, bool, bool] = (False, False, False, False)

    def decode(self, buttons: tuple[bool, bool, bool, bool] | None) -> ButtonEdges:
        """Return rising edges while preserving state across source failures.

        Once any ABXY combination activates, all four buttons must be released
        before another face-button combination can activate. A complete
        A+B+X+Y press therefore cannot trigger A+X or B+Y during partial
        release.
        """

        if buttons is None:
            return ButtonEdges(False, False, False)

        a_pressed, b_pressed, x_pressed, y_pressed = buttons
        self.last = buttons
        if self.latched:
            if not any(buttons):
                self.latched = False
                self.prev_ax = False
                self.prev_by = False
                self.prev_start = False
            return ButtonEdges(False, False, False)

        start = all(buttons)
        ax = a_pressed and x_pressed and not start
        by = b_pressed and y_pressed and not start
        if start or ax or by:
            self.latched = True
        self.prev_ax = ax
        self.prev_by = by
        self.prev_start = start
        return ButtonEdges(ax, by, start)
