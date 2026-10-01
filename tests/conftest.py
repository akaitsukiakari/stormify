import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from stormify.cli import example_rules  # noqa: E402
from stormify.config import Config  # noqa: E402
from stormify.db import Database  # noqa: E402


class FakeChannel:
    name = "fake"

    def __init__(self, sink):
        self.sink = sink

    def send(self, n):
        self.sink.append(n)


@pytest.fixture
def cfg(tmp_path):
    return Config(db_path=str(tmp_path / "t.db"), quiet_first_run=False, secret_key="test")


@pytest.fixture
def db(cfg):
    d = Database(cfg.db_path)
    uid = d.create_user("scott", None, {"timezone": "America/Denver",
                                        "channels": {"ntfy": {"topic": "x"}}})
    d.replace_rules(uid, example_rules())
    return d


@pytest.fixture
def sent():
    return []


@pytest.fixture
def channels(sent):
    return lambda settings: [FakeChannel(sent)]
