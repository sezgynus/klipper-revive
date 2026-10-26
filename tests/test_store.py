import sys
from pathlib import Path
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'klipper_extras'))
from klipper_revive_store import CheckpointStore, CheckpointWriter, SCHEMA_VERSION


def record(number, status='active'):
    return {'schema': SCHEMA_VERSION, 'status': status,
            'job': {'path': 'test.gcode', 'size': 1000},
            'file_position': number}


class CheckpointStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = CheckpointStore(Path(self.tmp.name) / 'checkpoints')

    def test_roundtrip_two_slots_and_rollback_on_corrupt(self):
        self.assertEqual(self.store.load(), (0, None))
        self.store.write(1, record(12))
        self.store.write(2, record(25))
        self.assertEqual(self.store.load(), (2, record(25)))
        self.store._slot(2).write_text('{torn file', encoding='utf-8')
        self.assertEqual(self.store.load(), (1, record(12)))

    def test_checksum_tamper_rejected(self):
        self.store.write(1, record(10))
        path = self.store._slot(1)
        path.write_text(path.read_text().replace('test.gcode', 'other.gcode'), encoding='utf-8')
        self.assertEqual(self.store.load(), (0, None))

    def test_reject_nan_and_unbounded_payload(self):
        with self.assertRaises(ValueError):
            self.store.write(1, {'schema': 1, 'bad': float('nan')})
        with self.assertRaises(ValueError):
            self.store.write(1, {'schema': 1, 'huge': 'a' * 20000})
        self.assertEqual(self.store.load(), (0, None))

    def test_writer_persists_latest_and_exits(self):
        writer = CheckpointWriter(self.store)
        self.assertTrue(writer.submit(record(12)))
        self.assertTrue(writer.submit(record(25)))
        writer.close()
        writer._thread.join(3)
        self.assertFalse(writer._thread.is_alive())
        seq, payload = self.store.load()
        self.assertGreater(seq, 0)
        self.assertIn(payload['file_position'], (12, 25))
        self.assertFalse(writer.submit(record(30)))

    def test_writer_error_disables_recording(self):
        class FailStore(CheckpointStore):
            def write(self, sequence, payload):
                raise OSError('disk unavailable')
        writer = CheckpointWriter(FailStore(self.store.directory))
        self.assertTrue(writer.submit(record(42)))
        writer._thread.join(3)
        self.assertFalse(writer._thread.is_alive())
        self.assertEqual(writer.last_error, 'disk unavailable')
        self.assertFalse(writer.submit(record(44)))


if __name__ == '__main__':
    unittest.main()
