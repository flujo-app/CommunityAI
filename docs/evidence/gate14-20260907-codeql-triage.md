# September 7 CodeQL key-handling review

Source: `76b6d84fc52342af4fd2315926b187aaa36b1378`.
Python analysis: `1737656484`. The CodeQL summary initially failed with two
high-severity findings, despite passing functional, style and package jobs.
The exact SARIF source-to-sink paths were inspected; both are false positives
for the implemented credential types. The scan and its rules remain enabled.

## Alert 11: secret-resource name classified as private material

The logging rule starts at the literal assigned to `SECRET` in
`scripts/catalog_key_backup.py:13`, carries that value through the report's
`secret` field and reaches the final JSON print. That literal identifies a
Secret Manager resource; it is not its secret payload. The private PEM bytes
returned by the CLI are captured, parsed in memory and omitted from the report.
Subprocess output is also excluded from error messages. The printed signing
identity is the SHA-256 of the public key's DER encoding.

This review did not retrieve the online private key or execute the recovery
drill. It inspected source and the scanner's own data-flow record.

## Alert 6: bearer tokens classified as passwords

The hash store in `src/drift/node/keys.py` holds opaque API bearer tokens.
Created client keys, bootstrap keys and the Fly probe use
`secrets.token_urlsafe(32)`, with 256 random bits. Native credential storage's
`get_password` API returns the generated control token; its API name does not
make the stored value a human password. The control-token class is validated.
Domain-separated SHA-256 and constant-time comparison are appropriate for those
high-entropy token values; a slow password KDF is not needed for generated tokens.

The advanced headless `--api-key` import also accepts caller-provided bearer
tokens. Their entropy remains the operator's responsibility. Use a generated
token with at least 32 random bytes; do not substitute a human password or a
short memorable string. No claim is made that SHA-256 strengthens weak tokens
or that the importer can prove a caller's randomness. Ordinary desktop users
receive generated keys through the API-key creation flow.

All ten API-key regression tests passed, including key creation, persistence,
revocation and rotation behavior. No hashing/storage implementation, test,
security rule or branch protection was changed for this review. The two
specific alerts were dismissed with these explanations. GitHub's API confirmed
`state: dismissed` and `dismissed_reason: false positive` for alerts 11 and 6.
