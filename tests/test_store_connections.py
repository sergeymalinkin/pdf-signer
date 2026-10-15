import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from max_workflow import Store


def test_store_does_not_leak_connections():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / 's.sqlite3')
        before = len(os.listdir('/proc/self/fd')) if os.path.isdir('/proc/self/fd') else 0
        for _ in range(200):
            with store.connect() as db:
                db.execute('SELECT 1').fetchone()
        after = len(os.listdir('/proc/self/fd')) if os.path.isdir('/proc/self/fd') else 0
        assert after - before < 5
