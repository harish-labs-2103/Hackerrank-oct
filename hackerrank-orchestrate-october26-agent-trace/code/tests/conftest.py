import sys
from pathlib import Path

import pytest

CODE = Path(__file__).resolve().parents[1]
REPO = CODE.parent
sys.path.insert(0, str(CODE))


@pytest.fixture(scope="session")
def dataset_root():
    return REPO / "dataset"


@pytest.fixture(scope="session")
def det_session(dataset_root):
    """Deterministic (no LLM) session over the real participant dataset."""
    from data_investigator.app import open_session
    from data_investigator.config import Settings

    s = Settings()
    s.dataset.root = str(dataset_root)
    return open_session(s, use_llm=False)
