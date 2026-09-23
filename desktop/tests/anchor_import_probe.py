"""Fresh isolated interpreter probe; optional actual native fixture connection."""

import importlib.abc
import json
import sys
from pathlib import Path

FORBIDDEN = {"drift", "torch", "transformers", "hivemind", "accelerate"}
attempted = []


class DenyRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in FORBIDDEN:
            attempted.append(fullname)
            raise ImportError("Forbidden desktop dependency: " + fullname)


sys.meta_path.insert(0, DenyRuntime())
sys.path[:0] = json.loads(sys.argv[1])
from communityai_desktop import anchor_lifecycle
from communityai_desktop.client import NodeClient
from communityai_desktop.profiles import VolunteerProfile

from communityai_anchor import linux_anchor as anchor
from communityai_anchor.linux_anchor_control import control_anchor
from communityai_anchor.linux_node_channel import NodeControlTransport

if len(sys.argv) > 2:
    fixture = json.loads(sys.argv[2])
    anchor._query_properties = lambda: fixture["properties"]
    anchor._runtime_directory = lambda: Path(fixture["directory"])
    profile = VolunteerProfile(Path(fixture["directory"]) / "profile")
    receipt, proof = anchor_lifecycle.prepare_anchored_profile(profile)
    client = NodeClient(profile.node_url, "fixture-control", transport=NodeControlTransport(receipt))
    result = client.status()["node_identity"]
    if result != control_anchor()["node"]["api_identity"]:
        raise RuntimeError("generation changed")
else:
    result = "import-only"

if attempted or any(name.split(".")[0] in FORBIDDEN for name in sys.modules):
    raise RuntimeError("model/runtime dependency escaped the desktop boundary: " + repr(attempted))
print(json.dumps(dict(result=result, forbidden_attempts=attempted)))
