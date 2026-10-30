import os
import tempfile

# Isolated storage for every test session: never touch the real uploads/results.
os.environ.setdefault("STELE_STORAGE", tempfile.mkdtemp(prefix="stele_test_"))
