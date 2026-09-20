"""Unit tests for the OpenStack resume/suspend program (no cloud needed).

The script is a Jinja template (only its shebang and the ansible_managed
comment are templated) deployed to the controller, so it is not importable as
part of the collection: the tests render those two lines and import the
result, which also proves the template stays valid Python.
"""

import importlib.util
import logging
from pathlib import Path

import pytest

TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "roles"
    / "slurm_install"
    / "templates"
    / "slurm_openstack_nodes.py.j2"
)


def load_script(tmp_path):
    source = TEMPLATE.read_text()
    assert source.startswith("#!{{ slurm_install_cloud_python_interpreter }}\n")
    rendered = (
        source.replace("#!{{ slurm_install_cloud_python_interpreter }}", "#!/usr/bin/python3", 1)
        .replace("# {{ ansible_managed }}", "# Ansible managed", 1)
        .replace("{{ slurm_install_cloud_log_file }}", "/var/log/slurm/dynamic_nodes.log", 1)
        .replace("{{ slurm_install_cloud_name }}", "openstack", 1)
    )
    assert "{{" not in rendered and "{%" not in rendered, "unrendered Jinja left in the script"

    path = tmp_path / "slurm_openstack_nodes.py"
    path.write_text(rendered)
    spec = importlib.util.spec_from_file_location("slurm_openstack_nodes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod(tmp_path_factory):
    return load_script(tmp_path_factory.mktemp("script"))


# --- parsing scontrol output ------------------------------------------------


def test_parse_features_reads_vm_features(mod):
    out = """NodeName=compute-01 CoresPerSocket=1
   CPUAlloc=0 CPUEfctv=4 CPUTot=4
   AvailableFeatures=image=compute-image,flavor=c002r004,keypair=mykey,network=mynet,security_groups=default|slurm
   ActiveFeatures=image=compute-image
"""
    features = mod.parse_features(out)
    assert features["compute-01"] == {
        "image": "compute-image",
        "flavor": "c002r004",
        "keypair": "mykey",
        "network": "mynet",
        "security_groups": "default|slurm",
    }


def test_parse_features_several_nodes(mod):
    out = """NodeName=compute-01 CoresPerSocket=1
   AvailableFeatures=image=img-a,flavor=small
NodeName=compute-02 CoresPerSocket=1
   AvailableFeatures=image=img-b,flavor=large
"""
    features = mod.parse_features(out)
    assert features["compute-01"]["image"] == "img-a"
    assert features["compute-02"]["flavor"] == "large"


def test_parse_features_ignores_plain_constraint_features(mod):
    out = """NodeName=compute-01 CoresPerSocket=1
   AvailableFeatures=image=img-a,haswell,flavor=small
"""
    features = mod.parse_features(out)
    assert features["compute-01"] == {"image": "img-a", "flavor": "small"}


def test_parse_features_handles_no_features(mod):
    out = """NodeName=compute-01 CoresPerSocket=1
   AvailableFeatures=(null)
"""
    assert mod.parse_features(out) == {"compute-01": {}}


# --- the instance-id files --------------------------------------------------


def test_instance_id_roundtrip(mod, tmp_path):
    statedir = str(tmp_path)
    assert mod.read_instance_id(statedir, "compute-01") is None
    mod.record_instance_id(statedir, "compute-01", "server-uuid")
    assert mod.read_instance_id(statedir, "compute-01") == "server-uuid"
    mod.forget_instance_id(statedir, "compute-01")
    assert mod.read_instance_id(statedir, "compute-01") is None
    # removing a file that is already gone is not an error
    mod.forget_instance_id(statedir, "compute-01")


def test_instance_id_without_statedir_is_harmless(mod):
    assert mod.instance_id_path(None, "compute-01") is None
    assert mod.read_instance_id(None, "compute-01") is None
    mod.record_instance_id(None, "compute-01", "id")  # must not raise
    mod.forget_instance_id(None, "compute-01")


def test_record_instance_id_survives_unwritable_dir(mod, tmp_path, caplog):
    statedir = tmp_path / "nope"  # does not exist
    with caplog.at_level(logging.WARNING):
        mod.record_instance_id(str(statedir), "compute-01", "id")
    assert "cannot record instance id" in caplog.text


# --- resolving the OpenStack objects named by the features ------------------


class FakeFound:
    def __init__(self, name, ident):
        self.name = name
        self.id = ident


class FakeConn:
    """Minimal stand-in for an openstacksdk connection."""

    def __init__(self, missing=(), raising=()):
        self.missing = set(missing)
        self.raising = set(raising)
        self.deleted = []
        self.created = []
        conn = self

        class Compute:
            def find_image(self, name, ignore_missing=True):
                return conn._find("image", name)

            def find_flavor(self, name, ignore_missing=True):
                return conn._find("flavor", name)

            def find_keypair(self, name, ignore_missing=True):
                return conn._find("keypair", name)

            def find_server(self, name, ignore_missing=True):
                return conn._find("server", name)

            def create_server(self, **kwargs):
                conn.created.append(kwargs)
                return FakeFound(kwargs["name"], "new-id")

            def wait_for_server(self, server, wait=None):
                return server

            def delete_server(self, server_id):
                conn.deleted.append(server_id)

            def wait_for_delete(self, server, wait=None):
                return server

        class Network:
            def find_network(self, name, ignore_missing=True):
                return conn._find("network", name)

            def find_security_group(self, name, ignore_missing=True):
                return conn._find("security_group", name)

        self.compute = Compute()
        self.network = Network()

    def _find(self, kind, name):
        if kind in self.raising:
            raise RuntimeError(f"{kind} lookup exploded")
        if kind in self.missing:
            return None
        return FakeFound(name, f"{kind}-id")


FEATURES = {
    "image": "img",
    "flavor": "flv",
    "network": "net",
    "keypair": "key",
    "security_groups": "default|slurm",
}


def test_resolve_vm_parameters_builds_create_kwargs(mod):
    params = mod.resolve_vm_parameters(FakeConn(missing=["server"]), "compute-01", FEATURES)
    assert params["name"] == "compute-01"
    assert params["image_id"] == "image-id"
    assert params["flavor_id"] == "flavor-id"
    assert params["networks"] == [{"uuid": "network-id"}]
    assert params["key_name"] == "key"
    assert params["security_groups"] == [{"name": "default"}, {"name": "slurm"}]


def test_resolve_vm_parameters_reports_missing_features(mod, caplog):
    with caplog.at_level(logging.ERROR):
        assert mod.resolve_vm_parameters(FakeConn(), "compute-01", {"image": "img"}) is None
    assert "Features lack" in caplog.text


def test_resolve_vm_parameters_reports_unknown_openstack_object(mod, caplog):
    conn = FakeConn(missing=["flavor", "server"])
    with caplog.at_level(logging.ERROR):
        assert mod.resolve_vm_parameters(conn, "compute-01", FEATURES) is None
    assert "flavor 'flv' does not exist" in caplog.text


def test_resolve_vm_parameters_reports_api_error(mod, caplog):
    conn = FakeConn(raising=["network"])
    with caplog.at_level(logging.ERROR):
        assert mod.resolve_vm_parameters(conn, "compute-01", FEATURES) is None
    assert "looking up network" in caplog.text


# --- resume / suspend behaviour ---------------------------------------------


def test_resume_node_creates_vm_and_records_id(mod, tmp_path):
    conn = FakeConn(missing=["server"])
    assert mod.resume_node(conn, "compute-01", FEATURES, str(tmp_path)) is True
    assert conn.created[0]["name"] == "compute-01"
    assert mod.read_instance_id(str(tmp_path), "compute-01") == "new-id"


def test_resume_node_deletes_leftover_vm_first(mod, tmp_path, caplog):
    conn = FakeConn()  # find_server returns an existing VM
    with caplog.at_level(logging.WARNING):
        assert mod.resume_node(conn, "compute-01", FEATURES, str(tmp_path)) is True
    assert conn.deleted == ["server-id"]
    assert "leftover VM" in caplog.text


def test_resume_node_fails_without_creating_when_features_are_wrong(mod, tmp_path):
    conn = FakeConn(missing=["image", "server"])
    assert mod.resume_node(conn, "compute-01", FEATURES, str(tmp_path)) is False
    assert conn.created == []


def test_suspend_node_deletes_recorded_vm(mod, tmp_path):
    statedir = str(tmp_path)
    mod.record_instance_id(statedir, "compute-01", "server-uuid")
    conn = FakeConn()
    assert mod.suspend_node(conn, "compute-01", statedir) is True
    assert conn.deleted == ["server-id"]
    assert mod.read_instance_id(statedir, "compute-01") is None


def test_suspend_node_falls_back_to_name(mod, tmp_path, caplog):
    conn = FakeConn()
    with caplog.at_level(logging.WARNING):
        assert mod.suspend_node(conn, "compute-01", str(tmp_path)) is True
    assert "no recorded instance id" in caplog.text
    assert conn.deleted == ["server-id"]


def test_suspend_node_succeeds_when_vm_is_already_gone(mod, tmp_path, caplog):
    conn = FakeConn(missing=["server"])
    with caplog.at_level(logging.WARNING):
        assert mod.suspend_node(conn, "compute-01", str(tmp_path)) is True
    assert "no VM to delete" in caplog.text
    assert conn.deleted == []


def test_suspend_node_reports_delete_failure(mod, tmp_path, caplog):
    conn = FakeConn(raising=["server"])
    with caplog.at_level(logging.ERROR):
        assert mod.suspend_node(conn, "compute-01", str(tmp_path)) is False
    assert "cannot look up the VM" in caplog.text


# --- main(): mode selection and exit status ---------------------------------


def test_main_resumes_every_node_and_reports_failures(mod, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "expand_nodes", lambda expr: ["compute-01", "compute-02"])
    monkeypatch.setattr(mod, "get_state_save_location", lambda: str(tmp_path))
    monkeypatch.setattr(mod, "get_features", lambda expr: {"compute-01": FEATURES, "compute-02": FEATURES})
    monkeypatch.setattr(mod, "connect", lambda: FakeConn(missing=["server"]))

    seen = []

    def fake_resume(conn, node, features, statedir):
        seen.append(node)
        return node != "compute-02"  # second node fails

    monkeypatch.setattr(mod, "resume_node", fake_resume)
    rc = mod.main(["/usr/local/sbin/slurm_openstack_resume", "compute-[01-02]"])
    assert seen == ["compute-01", "compute-02"]  # the failure did not stop the batch
    assert rc == 1


def test_main_suspend_mode_from_program_name(mod, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "expand_nodes", lambda expr: ["compute-01"])
    monkeypatch.setattr(mod, "get_state_save_location", lambda: str(tmp_path))
    monkeypatch.setattr(mod, "connect", lambda: FakeConn())
    called = []
    monkeypatch.setattr(mod, "suspend_node", lambda conn, node, statedir: called.append(node) or True)
    monkeypatch.setattr(mod, "resume_node", lambda *a: pytest.fail("resume must not run"))
    assert mod.main(["/usr/local/sbin/slurm_openstack_suspend", "compute-01"]) == 0
    assert called == ["compute-01"]


def test_main_fails_without_openstack_connection(mod, monkeypatch):
    monkeypatch.setattr(mod, "expand_nodes", lambda expr: ["compute-01"])
    monkeypatch.setattr(mod, "connect", lambda: None)
    assert mod.main(["slurm_openstack_resume", "compute-01"]) == 1


def test_main_fails_on_unexpandable_hostlist(mod, monkeypatch):
    monkeypatch.setattr(mod, "expand_nodes", lambda expr: [])
    assert mod.main(["slurm_openstack_resume", "compute-[01-02]"]) == 1


def test_main_needs_an_argument(mod):
    assert mod.main(["slurm_openstack_resume"]) == 2


def test_main_survives_an_unexpected_error_in_one_node(mod, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "expand_nodes", lambda expr: ["compute-01", "compute-02"])
    monkeypatch.setattr(mod, "get_state_save_location", lambda: str(tmp_path))
    monkeypatch.setattr(mod, "get_features", lambda expr: {})
    monkeypatch.setattr(mod, "connect", lambda: FakeConn())
    done = []

    def boom(conn, node, features, statedir):
        if node == "compute-01":
            raise RuntimeError("kaboom")
        done.append(node)
        return True

    monkeypatch.setattr(mod, "resume_node", boom)
    assert mod.main(["slurm_openstack_resume", "compute-[01-02]"]) == 1
    assert done == ["compute-02"]
