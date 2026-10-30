"""Hysteresis logic for adding/removing the standby server (pure Python)."""


class AutoScaler(object):
    """Scale up when average load stays above `up_load` for `up_duration` seconds,
    scale down when it stays below `down_load` for `down_duration` seconds."""

    def __init__(self, up_load=75.0, up_duration=15.0, down_load=30.0, down_duration=30.0):
        self.up_load = up_load
        self.up_duration = up_duration
        self.down_load = down_load
        self.down_duration = down_duration
        self.high_since = None
        self.low_since = None

    def update(self, avg_load, now, standby_active):
        """Feed one sample. Returns "scale_up", "scale_down" or None."""
        if not standby_active:
            self.low_since = None
            if avg_load > self.up_load:
                if self.high_since is None:
                    self.high_since = now
                if now - self.high_since >= self.up_duration:
                    self.high_since = None
                    return "scale_up"
            else:
                self.high_since = None
        else:
            self.high_since = None
            if avg_load < self.down_load:
                if self.low_since is None:
                    self.low_since = now
                if now - self.low_since >= self.down_duration:
                    self.low_since = None
                    return "scale_down"
            else:
                self.low_since = None
        return None
