"""Bounded crash-safe storage for Klipper Revive.

Uses only Python's standard library. No Klipper dependency. All persistent I/O is
performed by the single background writer, never from the Klipper reactor loop.
"""
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import threading

SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 16384


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


class CheckpointStore:
    """Two independent alternating slots; latest valid sequence wins."""

    def __init__(self, directory):
        self.directory = Path(directory)

    def _slot(self, sequence):
        return self.directory / ('checkpoint.%d.json' % (sequence % 2))

    def load(self):
        """Return (sequence, payload), or (0, None). Ignore torn/corrupt slots."""
        best = (0, None)
        for number in range(2):
            path = self.directory / ('checkpoint.%d.json' % number)
            try:
                with path.open('rb') as stream:
                    content = stream.read(MAX_RECORD_BYTES + 1)
                if len(content) > MAX_RECORD_BYTES:
                    continue
                wrapper = json.loads(content)
                sequence = wrapper['sequence']
                payload = wrapper['payload']
                if type(sequence) is not int or sequence < 1:
                    continue
                if sequence % 2 != number:
                    continue
                if payload.get('schema') != SCHEMA_VERSION:
                    continue
                check = hashlib.sha256(_canonical({'sequence': sequence,
                                                    'payload': payload})).hexdigest()
                if check != wrapper.get('sha256'):
                    continue
                if sequence > best[0]:
                    best = (sequence, payload)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
        return best

    def write(self, sequence, payload):
        """Atomically replace one slot, fsync file and directory (Linux)."""
        if type(sequence) is not int or sequence < 1:
            raise ValueError('Invalid checkpoint sequence')
        if payload.get('schema') != SCHEMA_VERSION:
            raise ValueError('Unsupported checkpoint schema')
        signed = {'sequence': sequence, 'payload': payload}
        wrapper = dict(signed, sha256=hashlib.sha256(_canonical(signed)).hexdigest())
        data = _canonical(wrapper) + b'\n'
        if len(data) > MAX_RECORD_BYTES:
            raise ValueError('Checkpoint exceeds %d bytes' % MAX_RECORD_BYTES)
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.checkpoint-', dir=str(self.directory))
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, str(self._slot(sequence)))
            dfd = os.open(str(self.directory), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class CheckpointWriter:
    """At most one pending snapshot; disk I/O never blocks submit().

    On a write error, recording is disabled rather than retrying aggressively.
    """

    def __init__(self, store):
        self.store = store
        self.sequence, self.last_saved = store.load()
        self.last_error = None
        self._pending = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread = None
        self._closed = False

    def submit(self, payload):
        """Offer immutable, already-built payload; newer pending state replaces old."""
        with self._lock:
            if self._closed or self.last_error is not None:
                return False
            self._pending = payload
            if self._thread is None:
                self._thread = threading.Thread(target=self._run,
                                                name='revive-checkpoint', daemon=True)
                self._thread.start()
        self._wake.set()
        return True

    def _run(self):
        while True:
            self._wake.wait()
            self._wake.clear()
            with self._lock:
                payload = self._pending
                self._pending = None
                closed = self._closed
            if payload is None:
                if closed:
                    return
                continue
            try:
                next_seq = self.sequence + 1
                self.store.write(next_seq, payload)
                self.sequence = next_seq
                self.last_saved = payload
                with self._lock:
                    if self._closed and self._pending is None:
                        return
            except Exception as exc:
                logging.exception('Klipper Revive checkpoint write failed; recording disabled')
                with self._lock:
                    self.last_error = str(exc)
                    self._pending = None
                return

    def close(self):
        """Do not join a disk-bound worker from the Klipper event loop."""
        with self._lock:
            self._closed = True
        self._wake.set()
