"""Secret-free generation binding shared by anchor, admitted API and desktop."""

from __future__ import annotations

import hashlib
import json
import re

from communityai_anchor.resource_recovery import RecoverableStateError

HEADER = "X-CommunityAI-Node-Generation"
PROFILE = "multigpu-volunteer"


def digest(value):
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        ).hexdigest()
    )


def validate_identity(value):
    try:
        if type(value) is not dict or set(value) != {
            "version",
            "profile",
            "generation",
            "pid",
            "start_ticks",
            "anchor_binding",
            "cgroup_binding",
        }:
            raise ValueError()
        if type(value["version"]) is not int or value["version"] != 1 or value["profile"] != PROFILE:
            raise ValueError()
        if type(value["generation"]) is not str or re.fullmatch("[0-9a-f]{32}", value["generation"]) is None:
            raise ValueError()
        if type(value["pid"]) is not int or not 1 < value["pid"] < 2**31:
            raise ValueError()
        if type(value["start_ticks"]) is not int or not 0 < value["start_ticks"] < 2**63:
            raise ValueError()
        for key in ("anchor_binding", "cgroup_binding"):
            if type(value[key]) is not str or re.fullmatch("sha256:[0-9a-f]{64}", value[key]) is None:
                raise ValueError()
        return dict(value)
    except Exception:
        raise RecoverableStateError() from None


def make_identity(binding, generation):
    if generation is None or generation["pid"] is None:
        return None
    return validate_identity(
        dict(
            version=1,
            profile=PROFILE,
            generation=generation["id"],
            pid=generation["pid"],
            start_ticks=generation["start_ticks"],
            anchor_binding=digest(binding),
            cgroup_binding=digest(generation["cgroup"]),
        )
    )


def identity_header(identity):
    return digest(validate_identity(identity))
