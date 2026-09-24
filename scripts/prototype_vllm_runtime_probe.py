"""Standalone local-server admission experiment; no vLLM or GPU required."""

import asyncio

import httpx


async def prototype_probe(client, *, model, context, version):
    async def get(path):
        response = await client.get(path, headers={"Authorization": "Bearer test-key"})
        if response.status_code != 200 or len(response.content) > 8192:
            raise ValueError("backend probe failed")
        return response

    health = await get("/health")
    if health.content not in (b"", b"null"):
        raise ValueError("unexpected health body")
    if (await get("/version")).json() != {"version": version}:
        raise ValueError("wrong backend version")
    models = (await get("/v1/models")).json()
    if models.get("object") != "list" or len(models.get("data", [])) != 1:
        raise ValueError("wrong model set")
    card = models["data"][0]
    if card.get("id") != model or card.get("max_model_len") != context:
        raise ValueError("wrong model identity or context")


def fixture(request):
    if request.url.path == "/health":
        return httpx.Response(200)
    if request.url.path == "/version":
        return httpx.Response(200, json={"version": "0.30.0"})
    if request.url.path == "/v1/models":
        return httpx.Response(200, json={"object": "list", "data": [{"id": "test/model", "max_model_len": 2048}]})
    raise AssertionError(request.url.path)


async def main():
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture), base_url="http://127.0.0.1:8000") as client:
        await asyncio.wait_for(prototype_probe(client, model="test/model", context=2048, version="0.30.0"), 1)
        for model, context, version in (
            ("wrong", 2048, "0.30.0"),
            ("test/model", 4096, "0.30.0"),
            ("test/model", 2048, "0.29.0"),
        ):
            try:
                await prototype_probe(client, model=model, context=context, version=version)
            except ValueError:
                continue
            raise AssertionError("mismatched backend admitted")
    print("standalone vLLM runtime probe PASS")


if __name__ == "__main__":
    asyncio.run(main())
