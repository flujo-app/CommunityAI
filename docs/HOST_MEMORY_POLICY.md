# Shared host RAM allowance

`contribution_policy.max_host_memory` records the operator's explicit, node-wide
host RAM allowance for contribution workers. It is separate from `max_vram` and
is shared across workers, including workers assigned to different GPUs. There is
no per-worker or per-GPU host RAM field.

The value is positive byte-size text, for example `"16GiB"` or `"1.5 GiB"`.
Parsing derives `ContributionPolicyConfig.max_host_memory_bytes` for admission
accounting. Zero, negative values, percentages, numbers supplied without text,
booleans, and invalid sizes are rejected. The derived bytes are internal and are
never added to the policy API document.

```json
{
  "sharing_enabled": false,
  "max_disk_space": "100GiB",
  "max_host_memory": "16GiB",
  "max_vram": "50%"
}
```

This is a config policy fragment; a control API replacement sends the complete
policy returned by GET plus the current `expected_config_revision`.

## Existing configurations and consent

Missing or null `max_host_memory` parses as unset. There is no automatic default,
hardware-derived permission, or migration that grants RAM. Serialized policies
omit the field while unset, preserving the existing strict-client response shape.
Once configured, the response includes the text value; clients that understand
only the old strict schema need updating before using that configured node.

The desktop accepts both old snapshots without the field and snapshots with the
new optional field. In **Memory, storage and other limits…**, **Shared host RAM
allowance** begins blank when unset and explains that all contribution workers
share it. A placeholder example is not a default. Saving another setting while
this previously absent field remains blank does not add it. Clearing an existing
allowance sends null; the saved config and response subsequently omit it.

The current runtime supports this allowance for managed, automatic CUDA workers:
NVIDIA GPUs selected through CommunityAI's GPU controls. When it is set, legacy
or manually configured contribution workers are blocked rather than allowed to
bypass shared accounting. The settings dialog explains this restriction. Clearing
the allowance restores the legacy unset policy shape, but also blocks managed
automatic sharing until explicit RAM consent is supplied again.

Pause contribution workers before editing limits. Saving uses the existing
revision-checked policy transaction; a stale revision rejects the entire save
without changing the previous allowance. This setting does not select GPUs,
start workers, or alter the one-automatic-worker configuration guard.

## Admission and verification boundaries

This field expresses a resource admission allowance, not an OS-enforced process
memory cap or a guarantee that a model's actual peak fits its estimate. Parsing
an older config without a value remains supported. The coordinated runtime change
requires explicit consent for managed automatic admission and compares retained,
additional persistent, and loading claims against both this allowance and fresh
available host memory. Loading estimates remain reserved for the entire worker
generation and are summed; there is no cross-worker serial-loading gate or hard
bandwidth rate guarantee. Parsing or displaying the policy alone is not evidence
that the complete resource lifecycle has been qualified.

The desktop distinguishes these situations:

- An unset allowance directs the operator to **Memory, storage and other limits…**.
- Insufficient shared RAM or storage suggests closing other apps, freeing storage,
  or adjusting explicit sharing limits; it never increases consent automatically.
- Incomplete stopping directs the operator to **Pause** to retry cleanup.
- Uncertain earlier work directs the operator to keep sharing paused until cleanup
  can be verified. Repeated Start or an app restart does not prove that earlier
  work stopped. A persisted uncertain generation is not cleared merely because
  its original process ID is absent.

Generic unavailable checks remain generic: the desktop does not infer that RAM
is exhausted from an arbitrary filesystem or reservation error. Messages on the
sharing panels, model cards, and sharing-error tooltips use fixed operator copy;
private paths and internal reservation tokens are not recovery instructions.

Cache verification is warmed outside policy and supervisor transition locks.
Final admission still performs a short cooperative scan while holding the
supervisor lock, with a two-second scan budget. Filesystem calls themselves have
no hard timeout, so slow storage can delay Pause/status beyond that budget. This
change does not claim an unconditional responsiveness guarantee. There is no
general desktop repair action for uncertain journal state; verified cleanup and
state recovery remain an operational limitation.

Focused tests in `tests/test_host_memory_policy.py` cover legacy omission,
positive-size conversion, invalid input, real API persistence and clearing,
stale-update rejection, and unchanged worker configuration. Tests in
`desktop/tests/test_host_memory_policy_settings.py` exercise the real Qt dialog's
Save button and production client codec with a local recording fixture, including
blank defaults, clearing, distinct visible status instructions, and private-error
redaction. These fixtures do not qualify physical GPU loading,
native host RAM peaks, or all-card runtime admission. The historical gate 13 proof
replay and its fixed policy schema are unchanged.
