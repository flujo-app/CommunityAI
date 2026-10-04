# Factory receiver v2 candidate (offline)

`create_factory_receiver_app_v2(receiver_profile=...)` is an opt-in route to the
existing ordinary distributed text path. It does not change the v1
`create_factory_app(factory_admission=...)` callback. The two adapters cannot be
configured together. A v2 adapter must return `FactoryAdmissionV2`; a bare v1
admission is denied.

An outer ASGI gate caps every POST to the v2 app before FastAPI parses JSON.
It retains at most 32,768 body bytes, stops at the first excess frame with 413,
rejects a declared oversized `Content-Length` before reading any frame, and
replays the accepted bytes unchanged. The receiver then supplies a
`FactoryIngressObservationV2` to the host. It contains those de-chunked bytes,
their SHA-256, the independently computed digest of the Pydantic normalized
body, and the receiver-observed `POST` method, exact route, and
allowlisted header values. Query strings, encoded route variants, duplicate
projected headers, malformed headers, and size overflow are denied before the
host's transport or Original-admission callback. Authorization, host, forwarded,
and cookie headers are excluded from the projection. The projected names are
the ten OpenAI SDK headers in the real Flow capture plus the four routing names
`openai-organization`, `openai-project`, `http-referer`, and `x-title` if present.

These are **receiver-observed ASGI values**, not an authenticated copy of the
SDK-final wire. A proxy can change the path, body, headers, scheme, and host;
sorting the receiver's selected headers cannot prove the sender's ordered
ten-header projection. The host must authenticate the separate Original issuer
and sender commitment, verify the actual sender principal and credential
generation, bind the call and slot, and compare the two projections under a
declared and verified proxy transformation policy. It must also authenticate
the deployed receiver identity, build and generation. The v2 profile's opaque
in-process marker and transport verifier receive only host-populated ASGI scope
extensions; caller headers and the API bearer cannot create that marker. The
host must issue it only after checking the transport channel and generation.
The source checks marker identity and generation before calling the Original
adapter. The returned admission binds the observed projection digest and
transport fields and continues to use the existing fresh authority and
one-attempt dispatch claim. These checks cannot turn an unverified host
callback into a trust root. The admission's fresh authority callback must also
recheck Original and transport validity immediately before dispatch.

The real Flow fixture has a 299-byte SDK body with SHA-256
`b86e125b6a031a226acc03147b5d865402ad149a621626fcdbf7629f5541fd03`.
PR35's Python-normalized body has 307 bytes and SHA-256
`ab288a143d34de92156f01d0d4c4f5d05bbdc51eda4a335e16c8816205cd08b7`.
The separate FACTORY held v3 candidate now commits that same 307-byte Python
normalization. Its `HELD_BEFORE_POST` expected observation includes the
authenticated sender and credential fields and the ten SDK-final headers; it
cannot be filled by this post-ingress ASGI observation. FACTORY has a separate
synthetic post-POST receipt shape, but a real authenticated receipt and host
trust root are still required for the join.

ASGI delivers whole frames, so an oversized single frame can exist transiently
before the gate rejects it. The deployment ingress still needs a global edge
limit on request/frame size and duration, including non-POST paths. This
candidate has no Original issuer, authenticated transport,
deployed receiver receipt, or physical send. The offline test host supplies
synthetic values and holds the dispatch claim.
