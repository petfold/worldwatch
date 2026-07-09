import json
import pathlib
import sqlite3

import pytest

from worldwatch.config.loader import load_sources
from worldwatch.db import open_db

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
CONFIG_DIR = pathlib.Path(__file__).parents[1] / "src" / "worldwatch" / "config" / "sources"


@pytest.fixture
def db(tmp_path) -> sqlite3.Connection:
    return open_db(tmp_path / "test.db")


@pytest.fixture
def sources():
    return load_sources(CONFIG_DIR)


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())
