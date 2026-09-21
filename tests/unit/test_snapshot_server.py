"""Unit tests for the image snapshot helper (no cloud needed).

The script imports openstacksdk, which is not a test requirement: stub the
one attribute it uses (the NotFound exception) before importing it.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "roles"
    / "slurm_compute_image"
    / "files"
    / "snapshot_server.py"
)


@pytest.fixture(scope="module")
def snapshot_server():
    if "openstack" not in sys.modules:
        fake = types.ModuleType("openstack")

        class NotFoundException(Exception):
            pass

        fake.exceptions = types.SimpleNamespace(NotFoundException=NotFoundException)
        sys.modules["openstack"] = fake
    spec = importlib.util.spec_from_file_location("snapshot_server", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def not_found(snapshot_server):
    """Whatever the script catches: the real sdk's exception, or the stub's."""
    return snapshot_server.openstack.exceptions.NotFoundException


class Image:
    def __init__(self, id, status):
        self.id = id
        self.status = status


class Conn:
    """conn.image with a scripted get_image and a fixed name lookup.

    Each entry of 'gets' is returned (or raised) by one get_image call; once
    they run out, get_image behaves like glance with no such image.
    """

    def __init__(self, not_found, gets=(), by_name=None):
        self._not_found = not_found
        self._gets = list(gets)
        self._by_name = by_name
        self.image = types.SimpleNamespace(
            get_image=self._get_image, find_image=self._find_image
        )

    def _get_image(self, image_id):
        if not self._gets:
            raise self._not_found("no image")
        result = self._gets.pop(0)
        if result is None:
            raise self._not_found("no image")
        return result

    def _find_image(self, name, ignore_missing=True):
        return self._by_name


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch, snapshot_server):
    monkeypatch.setattr(snapshot_server.time, "sleep", lambda _: None)


def test_missing_then_active(snapshot_server, not_found):
    """A 404 means 'not yet', not 'never': keep polling instead of raising."""
    conn = Conn(not_found, [None, Image("i-1", "saving"), Image("i-1", "active")])
    assert snapshot_server.wait_for_image(conn, "i-1", 60, "img").status == "active"


def test_falls_back_to_the_name(snapshot_server, not_found):
    """The id cinder returned never resolves, but the name does."""
    conn = Conn(not_found, by_name=Image("i-2", "active"))
    assert snapshot_server.wait_for_image(conn, "i-1", 60, "img").id == "i-2"


def test_gives_up_when_nothing_appears(snapshot_server, not_found, monkeypatch, capsys):
    """Nothing by id or by name: time out cleanly rather than raise."""
    ticks = iter([0, 1, 2, 3, 100, 200, 300])
    monkeypatch.setattr(snapshot_server.time, "time", lambda: next(ticks))
    assert snapshot_server.wait_for_image(Conn(not_found), "i-1", 10, "img") is None
    assert "missing" in capsys.readouterr().err


def test_killed_image_is_a_failure(snapshot_server, not_found):
    conn = Conn(not_found, [Image("i-1", "killed")])
    assert snapshot_server.wait_for_image(conn, "i-1", 60, "img") is None
