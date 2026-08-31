# CommunityAI

[![Version](https://img.shields.io/badge/version-2.3.0.dev2-6d4aff)](https://github.com/flujo-app/CommunityAI)
[![Tests](https://github.com/flujo-app/CommunityAI/actions/workflows/run-tests.yaml/badge.svg?branch=main)](https://github.com/flujo-app/CommunityAI/actions/workflows/run-tests.yaml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-22c55e.svg)](LICENSE)

**AI powered by people.**


![CommunityAI sharing screen](desktop/dist/communityai-sharing-final.png)

## How it works

Community-AI is a shared Large-Language-Model, by the people, for the people.

1. Start the app.
2. Connect an OpenAI-compatible client to the local endpoint.
3. Use public community inference and optionally share compute within your limits.

Community-AI takes care of everything else.

The application ships one model-agnostic runtime. Its signed catalog approves exact model
manifests; when a model is selected, CommunityAI downloads only the upstream Hugging Face
checkpoint files needed by the local client components or contributed block range, verifies
their declared size and SHA-256, and keeps them in a persistent shared cache. It does not need
one installer or container image per model. Download minimization is currently limited to
whole upstream checkpoint shards. See
[`ADR 0003`](docs/adr/0003-direct-manifested-artifact-delivery.md).

CommunityAI is still working toward its first public inference alpha. Credits,
earnings, payments, and payouts are planned later and are not currently available.
