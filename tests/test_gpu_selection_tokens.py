import json

import pytest

from drift.node.gpu_selection_tokens import GpuSelectionChangedError, GpuSelectionTokens

REVISION = "sha256:" + "a" * 64
IDENTITIES = {f"cuda:{index}": f"GPU-00000000-0000-0000-0000-{index + 1:012d}" for index in range(8)}


def tokens(identities=None, live=lambda value: True):
    identities = dict(IDENTITIES) if identities is None else identities
    return GpuSelectionTokens(devices=lambda: tuple(identities), identity=identities.get, live=live)


def test_eight_identical_cards_have_opaque_distinct_process_local_tokens():
    provider = tokens()
    snapshot = provider.snapshot(REVISION)
    assert len(snapshot["devices"]) == 8
    assert len({row["selection_token"] for row in snapshot["devices"]}) == 8
    assert all(identity not in json.dumps(snapshot) for identity in IDENTITIES.values())
    for row in snapshot["devices"]:
        provider.verify(row["device"], REVISION, row["selection_token"])
        with pytest.raises(GpuSelectionChangedError):
            tokens().verify(row["device"], REVISION, row["selection_token"])


def test_equal_name_capacity_ordinal_swap_invalidates_both_choices():
    identities = dict(IDENTITIES)
    provider = tokens(identities)
    before = provider.snapshot(REVISION)
    identities["cuda:0"], identities["cuda:1"] = identities["cuda:1"], identities["cuda:0"]
    for row in before["devices"][:2]:
        with pytest.raises(GpuSelectionChangedError):
            provider.verify(row["device"], REVISION, row["selection_token"])
    row = before["devices"][2]
    provider.verify(row["device"], REVISION, row["selection_token"])


@pytest.mark.parametrize("mutation", ["revision", "lost", "identity", "tamper"])
def test_changed_state_or_token_rejects_without_private_details(mutation):
    identities = dict(IDENTITIES)
    live = [True]
    provider = tokens(identities, live=lambda value: live[0])
    row = provider.snapshot(REVISION)["devices"][0]
    revision = "sha256:" + "b" * 64 if mutation == "revision" else REVISION
    if mutation == "lost":
        live[0] = False
    elif mutation == "identity":
        identities["cuda:0"] = identities["cuda:1"]
    elif mutation == "tamper":
        row["selection_token"] = "sha256:" + "0" * 64
    with pytest.raises(GpuSelectionChangedError) as failure:
        provider.verify(row["device"], revision, row["selection_token"])
    assert all(identity not in str(failure.value) for identity in IDENTITIES.values())


def test_enrolled_identity_must_match_displayed_choice_even_if_mapping_flaps():
    provider = tokens()
    row = provider.snapshot(REVISION)["devices"][0]
    provider.verify_enrolled("cuda:0", REVISION, row["selection_token"], IDENTITIES["cuda:0"])
    with pytest.raises(GpuSelectionChangedError):
        provider.verify_enrolled("cuda:0", REVISION, row["selection_token"], IDENTITIES["cuda:1"])


def test_unavailable_cards_are_absent_and_probes_bounded():
    probes = []

    def identity(device):
        probes.append(device)
        return IDENTITIES["cuda:0"]

    provider = GpuSelectionTokens(
        devices=lambda: tuple(f"cuda:{i}" for i in range(1000)), identity=identity, live=lambda value: False
    )
    assert provider.snapshot(REVISION)["devices"] == []
    assert len(probes) == 16


def test_private_probe_error_is_sanitized():
    def broken(device):
        raise RuntimeError("secret/path/" + IDENTITIES["cuda:0"])

    provider = GpuSelectionTokens(devices=lambda: ("cuda:0",), identity=broken)
    assert provider.snapshot(REVISION)["devices"] == []
    with pytest.raises(GpuSelectionChangedError) as failure:
        provider.verify("cuda:0", REVISION, "sha256:" + "a" * 64)
    assert "secret" not in str(failure.value) and "GPU-" not in str(failure.value)
