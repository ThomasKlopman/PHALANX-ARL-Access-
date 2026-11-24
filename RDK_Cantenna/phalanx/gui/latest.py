"""A single display slot: replacing a preview never changes acquired/recorded data."""
from threading import Lock


class LatestPreview:
    def __init__(self):
        self._lock = Lock()
        self._item = None
        self.published = 0
        self.consumed = 0
        self.superseded = 0

    def publish(self, result, frame):
        with self._lock:
            if self._item is not None:
                self.superseded += 1
            self._item = (result, frame)
            self.published += 1

    def take(self):
        with self._lock:
            item, self._item = self._item, None
            if item is not None:
                self.consumed += 1
            return item

    def clear(self):
        with self._lock:
            self._item = None
