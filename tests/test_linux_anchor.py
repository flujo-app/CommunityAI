"""Pure service-contract tests. OS and manager facts here are fixtures."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from drift.node import linux_anchor as anchor
from drift.node.resource_recovery import RecoverableStateError


def properties():
    return dict(
        Id=anchor.UNIT,
        LoadState="loaded",
        ActiveState="active",
        SubState="running",
        MainPID="100",
        ControlGroup="/unusual.slice/actual-service",
        InvocationID="a" * 32,
        Delegate="yes",
        Type="exec",
        KillMode="control-group",
        Restart="no",
        Transient="no",
    )


@pytest.fixture
def service(monkeypatch):
    values = properties()
    monkeypatch.setattr(anchor.cg, "_platform", lambda: None)
    monkeypatch.setattr(anchor.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(anchor.os, "getpid", lambda: 100)
    monkeypatch.setattr(anchor, "_query_properties", lambda: values)
    monkeypatch.setattr(anchor, "_process", lambda pid: (555, values["ControlGroup"] + "/anchor-control"))
    return values


def test_fixed_service_uses_actual_control_group_and_hides_private_repr(service):
    value = anchor.inspect_service()
    assert value.control_group == "/unusual.slice/actual-service" and value.pid == 100
    assert value.invocation not in repr(value) and value.control_group not in repr(value)


@pytest.mark.parametrize(
    "key,value",
    [
        ("Id", "other.service"),
        ("LoadState", "not-found"),
        ("ActiveState", "failed"),
        ("SubState", "dead"),
        ("Delegate", "no"),
        ("Type", "simple"),
        ("KillMode", "process"),
        ("Restart", "always"),
        ("Transient", "yes"),
        ("MainPID", "0"),
        ("MainPID", "01"),
        ("MainPID", "-1"),
        ("MainPID", "injected"),
        ("InvocationID", "A" * 32),
        ("InvocationID", "0" * 32),
        ("ControlGroup", "/"),
        ("ControlGroup", "/../other"),
        ("ControlGroup", "/actual\nother"),
    ],
)
def test_unqualified_service_refused(service, key, value):
    service[key] = value
    with pytest.raises(RecoverableStateError):
        anchor.inspect_service()


def test_start_must_be_exact_main_process_at_boundary(service, monkeypatch):
    monkeypatch.setattr(anchor, "_process", lambda pid: (555, service["ControlGroup"]))
    assert anchor.inspect_service(starting=True).pid == 100
    service.update(ActiveState="activating", SubState="start")
    assert anchor.inspect_service(starting=True).pid == 100
    monkeypatch.setattr(anchor.os, "getpid", lambda: 101)
    with pytest.raises(RecoverableStateError):
        anchor.inspect_service(starting=True)
    with pytest.raises(RecoverableStateError):
        anchor.inspect_service()


@pytest.mark.parametrize("path", ["/other", "/unusual.slice/actual-service", "/unusual.slice/actual-service/workers"])
def test_connected_service_must_live_in_its_control_leaf(service, monkeypatch, path):
    monkeypatch.setattr(anchor, "_process", lambda pid: (555, path))
    with pytest.raises(RecoverableStateError):
        anchor.inspect_service()


def test_manager_failures_are_fixed_private_errors(service, monkeypatch):
    def denied():
        raise OSError("private bus address and secret path")

    monkeypatch.setattr(anchor, "_query_properties", denied)
    with pytest.raises(RecoverableStateError) as error:
        anchor.inspect_service()
    assert "private" not in str(error.value)


@pytest.mark.parametrize("text", ["A=one\nA=two\n", "A=one\nB=two\n", "A", "", "A=x" * 10000])
def test_property_parser_rejects_ambiguous_or_unbounded_output(text):
    with pytest.raises(RecoverableStateError):
        anchor._properties(text, ("A",))


def test_property_parser_accepts_only_requested_keys():
    assert anchor._properties("A=one\nB=two\n", ("B", "A")) == {"A": "one", "B": "two"}


def test_query_cannot_inherit_manager_redirects(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(anchor, "_runtime_directory", lambda: tmp_path)
    monkeypatch.setattr(anchor.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "tcp:attacker")
    monkeypatch.setenv("SYSTEMD_HOST", "attacker")

    def run(command, **kwargs):
        calls.append((command, kwargs))
        output = (
            "Linger=yes\n"
            if command[0].endswith("loginctl")
            else "".join(f"{k}={v}\n" for k, v in properties().items())
        )
        return SimpleNamespace(stdout=output.encode("ascii"))

    monkeypatch.setattr(anchor.subprocess, "run", run)
    assert anchor._query_properties() == properties()
    assert len(calls) == 2
    assert calls[0][0] == ["/usr/bin/loginctl", "show-user", "1000", "--property=Linger"]
    assert calls[1][0][0:4] == ["/usr/bin/systemctl", "--user", "show", anchor.UNIT]
    for _, kwargs in calls:
        assert kwargs["env"]["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=" + str(tmp_path / "bus")
        assert "SYSTEMD_HOST" not in kwargs["env"] and kwargs["timeout"] == 5


@pytest.mark.parametrize("linger", ["no", "", "yes\nExtra=yes"])
def test_missing_persistent_user_manager_is_not_enabled(monkeypatch, tmp_path, linger):
    calls = []
    monkeypatch.setattr(anchor, "_runtime_directory", lambda: tmp_path)
    monkeypatch.setattr(anchor.os, "geteuid", lambda: 1000, raising=False)

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout=("Linger=" + linger + "\n").encode("ascii"))

    monkeypatch.setattr(anchor.subprocess, "run", run)
    with pytest.raises(RecoverableStateError):
        anchor._query_properties()
    assert len(calls) == 1 and "enable-linger" not in calls[0]


def mount(root="/", point="/actual/mount", options="rw", kind="cgroup2", number=20):
    return f"{number} 1 0:42 {root} {point} {options} - {kind} cgroup rw\n"


def test_mount_discovery_uses_actual_unique_writable_mount(service, monkeypatch):
    monkeypatch.setattr(
        anchor.cg, "_read_path", lambda *a, **kw: mount(options="ro") + mount(point="/different", number=21)
    )
    assert anchor._delegated_path(anchor.inspect_service()) == "/different" + service["ControlGroup"]


@pytest.mark.parametrize(
    "text",
    [
        mount(options="ro"),
        mount(root="/subroot"),
        mount() + mount(number=21),
        mount(kind="cgroup"),
        "bad",
        mount(point="/../other"),
    ],
)
def test_mount_discovery_refuses_guessing(service, monkeypatch, text):
    monkeypatch.setattr(anchor.cg, "_read_path", lambda *a, **kw: text)
    with pytest.raises(RecoverableStateError):
        anchor._delegated_path(anchor.inspect_service())


def test_process_start_identity_parses_parentheses(monkeypatch):
    monkeypatch.setattr(anchor.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(anchor.os, "stat", lambda path: SimpleNamespace(st_uid=1000))

    def read(path, maximum):
        if path.endswith("/status"):
            return "Uid:\t1000\t1000\t1000\t1000\n"
        return (
            "100 (name with ) parentheses) " + "S " + "0 " * 18 + "555 0\n"
            if path.endswith("stat")
            else "0::/actual/anchor-control\n"
        )

    monkeypatch.setattr(anchor.cg, "_read_path", read)
    assert anchor._process(100) == (555, "/actual/anchor-control")


@pytest.mark.parametrize("group", ["0::/actual\n1:cpu:/other\n", "0::/actual", "0::/\n", "0::/../other\n"])
def test_process_identity_rejects_namespace_or_hybrid_ambiguity(monkeypatch, group):
    monkeypatch.setattr(anchor.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(anchor.os, "stat", lambda path: SimpleNamespace(st_uid=1000))
    monkeypatch.setattr(
        anchor.cg,
        "_read_path",
        lambda path, maximum: "Uid:\t1000\t1000\t1000\t1000\n"
        if path.endswith("/status")
        else "100 (name) S " + "0 " * 18 + "555 0\n"
        if path.endswith("stat")
        else group,
    )
    with pytest.raises(RecoverableStateError):
        anchor._process(100)


@pytest.mark.parametrize("nonce", [None, True, "", "a" * 63, "A" * 64, "../path"])
def test_request_contract_never_accepts_paths_or_arguments(nonce):
    with pytest.raises(RecoverableStateError):
        anchor._request(nonce)


def test_receipt_explicitly_grants_no_node_or_maintenance_authority(service):
    result = anchor._receipt(anchor.inspect_service(), (), "a" * 64)
    assert result["node_generation"] is None and result["admission"] is False and result["maintenance"] is False
    assert result["profile"] == "multigpu-volunteer"
    changed = anchor._receipt(replace(anchor.inspect_service(), start_ticks=556), (), "a" * 64)
    assert result != changed
