# DRIFT-LLM

**D**ist**R**ibuted **I**nference and **F**ine-**T**uning of **L**arge **L**anguage **M**odels

Run large language models across a cluster of your own machines. Each machine serves a slice of the model's layers; a client stitches them together and runs inference or fine-tuning as if the whole model were local.

This is a fork of [Petals](https://github.com/bigscience-workshop/petals), which is no longer maintained. It is modernized (newer `transformers`, PyTorch, and `hivemind`) and refocused: instead of one large public swarm, it targets private clusters. There is no "main" public network to join and no central coordinator; you run the whole thing yourself.

The inference-first community revival plan is tracked in
[`docs/REVIVAL.md`](docs/REVIVAL.md). Its selected PySide product client is developed as
an isolated package in [`desktop/`](desktop/README.md); it controls the standalone local
node without importing model or networking runtimes into the GUI process.

## How it works

- The model is split into contiguous blocks of transformer layers.
- Each **server** loads a few blocks (as many as its GPU or CPU can hold) and announces them to a private DHT.
- A **client** loads only the input/output embeddings, finds a set of servers that together cover every block, and runs a forward or backward pass through them.

You get the ergonomics of a local `transformers` model (full PyTorch access to logits and hidden states, custom sampling, prompt-tuning) while the weights live across the cluster. The client holds almost nothing, so it runs comfortably on a laptop even for very large models.

## Quickstart

**1. Install.** On Linux or macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/flujo-app/CommunityAI/main/scripts/install.sh | sh
```

On Windows, run this from the revival checkout. It needs
[uv](https://docs.astral.sh/uv/); the installer downloads a checksum-verified
portable Go toolchain if Go is absent:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

The installer detects your accelerator and installs a matching PyTorch build. Override it with `DRIFT_DEVICE=cpu|cuda|xpu|mps`.

**2. Start a swarm** on your first machine:

```bash
drift up meta-llama/Llama-3.1-8B-Instruct
```

It serves as many of the model's layers as fit, then prints a join command:

```
drift up meta-llama/Llama-3.1-8B-Instruct \
    --join drift://12D3KooW...@203.0.113.10:31337
```

**3. Add more machines.** Run that printed command on each one. Between them the servers must cover every layer; `drift up` reports any that are missing. The first node keeps a stable address, so the same join token works across restarts.

**4. Connect a client** from anywhere that can reach the swarm:

```python
from transformers import AutoTokenizer
from drift import AutoDistributedModelForCausalLM

model_name = "meta-llama/Llama-3.1-8B-Instruct"
# The multiaddr form of the drift:// join token, i.e. drift://<peer>@<host>:<port>
# is the same as /ip4/<host>/tcp/<port>/p2p/<peer>.
initial_peers = ["/ip4/203.0.113.10/tcp/31337/p2p/12D3KooW..."]

tokenizer = AutoTokenizer.from_pretrained(model_name)
model = AutoDistributedModelForCausalLM.from_pretrained(model_name, initial_peers=initial_peers)

outputs = model.generate(tokenizer("A cat sat", return_tensors="pt")["input_ids"], max_new_tokens=5)
print(tokenizer.decode(outputs[0]))
```

**5. Stop a machine's servers.** `drift up` runs in the foreground, so `Ctrl+C` is the usual way to stop it. When you started it in the background (or it wedged), `drift down` stops the DRIFT-LLM servers running on that machine:

```bash
drift down --list   # preview what's running, stop nothing
drift down          # stop them (gracefully, escalating to a kill if needed)
```

`drift down` only affects the machine it runs on; a server kept alive by an external supervisor (a Scheduled Task, systemd, a launchd `KeepAlive` agent) should be stopped there instead.

## Manual setup

`drift up` wraps two lower-level commands, `drift dht` and `drift server`. Use them directly when you want full control — a dedicated always-on bootstrap peer, specific block ranges, custom ports, and so on.

Every machine in a cluster must be able to reach the others over the network: a LAN, a VPN such as Tailscale or WireGuard, or public IPs with the chosen ports open.

### 1. Start a bootstrap node

Pick one machine to run a DHT bootstrap peer. Servers and clients use it to discover each other.

```bash
drift dht --identity_path bootstrap.id \
    --host_maddrs /ip4/0.0.0.0/tcp/31337
```

It logs its reachable address; note the full multiaddr, for example:

```
/ip4/203.0.113.10/tcp/31337/p2p/12D3KooW...
```

Use that value as the initial peer below. `--identity_path` keeps the peer ID stable across restarts, so the address does not change.

### 2. Start servers

On each machine with spare compute, host part of the model:

```bash
drift server meta-llama/Llama-3.1-8B-Instruct \
    --initial_peers /ip4/203.0.113.10/tcp/31337/p2p/12D3KooW... \
    --num_blocks 8
```

Run this on as many machines as you like. Between them, the servers must cover all of the model's blocks — the client reports if any are missing. Use `--block_indices 0:16` to pin specific blocks instead of `--num_blocks`, and `--device cpu --torch_dtype float32` to serve on CPU. By default a server picks the best available accelerator — NVIDIA CUDA, Intel XPU, or Apple MPS — falling back to CPU; pass `--device` to choose explicitly (e.g. `--device xpu`).

### 3. Connect a client

```python
from transformers import AutoTokenizer
from drift import AutoDistributedModelForCausalLM

model_name = "meta-llama/Llama-3.1-8B-Instruct"
initial_peers = ["/ip4/203.0.113.10/tcp/31337/p2p/12D3KooW..."]

tokenizer = AutoTokenizer.from_pretrained(model_name)
model = AutoDistributedModelForCausalLM.from_pretrained(model_name, initial_peers=initial_peers)

inputs = tokenizer("A cat sat", return_tensors="pt")["input_ids"]
outputs = model.generate(inputs, max_new_tokens=5)
print(tokenizer.decode(outputs[0]))
```

Larger models simply need more machines (or bigger GPUs) among the servers; the client code does not change.

**Gated models.** For Llama and other gated weights, request access on the Hugging Face Hub and run `huggingface-cli login` on the servers and client before starting them.

### 4. Optional: an OpenAI-compatible HTTP API

`drift api` runs a swarm client behind an OpenAI-compatible HTTP server (`/v1/models`, `/v1/chat/completions`, `/v1/completions`, including streaming), so any OpenAI SDK or tool can talk to the swarm. It needs the `api` extra (`uv sync --extra api` or `pip install drift[api]`):

```bash
drift api meta-llama/Llama-3.1-8B-Instruct \
    --initial_peers /ip4/203.0.113.10/tcp/31337/p2p/QmXXX... \
    --host 0.0.0.0 --port 8080 --api_key my-secret-key \
    --request_timeout 30 --max_retries 3
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://203.0.113.10:8080/v1", api_key="my-secret-key")
print(client.chat.completions.create(
    model="meta-llama/Llama-3.1-8B-Instruct",
    messages=[{"role": "user", "content": "Hello!"}],
).choices[0].message.content)
```

Generation is serialized (`--max_concurrent`, default 1) since each in-flight request holds a server-side attention cache. API requests use finite failover by default: each swarm RPC waits at most 30 seconds and each step gets at most three route attempts. Operators can tune `--request_timeout` and `--max_retries` for slower hardware.

### 5. Persistent local node (milestone 4 preview)

`drift node` keeps a stable authenticated API on loopback, registers exact
`ModelManifest v1` identities, and lazily loads tokenizer and client-side weights
on first use. A manifest name, declared API alias, or full `sha256:<digest>` selects
that exact swarm; an unknown `model` is rejected instead of being silently
substituted.

```bash
drift node ./tinyllama-manifest.json \
    --initial_peers /ip4/203.0.113.10/tcp/31337/p2p/QmXXX...
```

For multiple models, use the strict, secret-free
[`NodeConfig v1`](docs/NODE_CONFIG_V1.md). It registers every model without eager
artifact downloads and enforces a hard `max_loaded_models` residency limit:

```bash
drift node --config ./community-node.json
```

The default OpenAI URL is `http://127.0.0.1:8080/v1`. If `--api_key` is omitted,
the command creates a persistent client key in `~/.drift/node/local-api.key` and
logs only that path. It separately creates the privileged local control credential
in `~/.drift/node/control-api.key`:

```bash
curl -H "Authorization: Bearer $(cat ~/.drift/node/control-api.key)" \
    http://127.0.0.1:8080/control/v1/status
```

OpenAI client keys authorize only `/v1/*`; the control credential authorizes only
`/control/v1/*`. An existing installation keeps its `local-api.key` for AI clients
and receives the separate control key on its first upgraded start. A headless
operator may select another private control file with `--control_key_path`.

Binding beyond loopback requires both key classes and the explicit
`--allow_network` acknowledgement. Authenticated status includes model lifecycle,
runtime-budget, active-request, and loaded-route coverage data; idle models can be
unloaded through the control API. The boundary and deferred work are recorded in
[`docs/adr/0001-unified-local-node.md`](docs/adr/0001-unified-local-node.md).

## Installation

The [Quickstart](#quickstart) install scripts (`scripts/install.sh` / `scripts/install.ps1`) are the easiest path — they detect your accelerator, install a matching PyTorch build, and provide the `drift` command. The rest of this section covers installing manually.

Requires **Python 3.10+**. The project is managed with [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/flujo-app/CommunityAI
cd CommunityAI
uv sync --extra dev
```

This installs the `drift` command (`drift up`, `drift server`, `drift dht`, `drift api`, `drift node`); each is also runnable as `python -m drift.cli <command>`.

### Windows native setup

PyPI does not publish Windows wheels for `hivemind`, and upstream `hivemind` depends on POSIX-only process and socket behavior. On Windows, build and install the patched wheel from this repository before running DRIFT-LLM:

```powershell
uv run python scripts/build_hivemind_windows.py --out-dir dist
uv pip install (Get-ChildItem .\dist\hivemind-1.1.12-*-win_amd64.whl | Select-Object -Last 1).FullName
uv pip install -e .
```

The manual build requires Go on `PATH`; `scripts/install.ps1` instead provisions
a checksum-verified portable Go toolchain automatically. The build script compiles
`p2pd.exe` and packages it into the wheel. The DRIFT-LLM dependency on PyPI
`hivemind` is disabled on Windows, so install the local wheel explicitly after
creating or syncing the environment.

Or install into an existing environment with pip:

```bash
pip install git+https://github.com/flujo-app/CommunityAI
```

For NVIDIA GPUs, install a CUDA build of PyTorch (for example `conda install pytorch pytorch-cuda=12.4 -c pytorch -c nvidia`) before installing. A `Dockerfile` is included for running servers in a container.

For **Intel GPUs** (Arc, or the integrated graphics on recent Core chips), install the PyTorch XPU build that matches this fork's torch pin:

```bash
pip install --force-reinstall --index-url https://download.pytorch.org/whl/xpu "torch==2.6.0+xpu"
```

Use the Intel GPU driver and Level-Zero runtime. Servers then run with `--device xpu`. Quantization (`--quant_type int8/nf4`) is CUDA-only; on XPU, MPS, or CPU run with `--quant_type none` (the default off CUDA).

To smoke-test the Windows native stack with a one-machine private swarm, use the TinyLlamaV0-compatible checkpoint `Maykeye/TinyLLama-v0`:

```powershell
uv pip install --force-reinstall --index-url https://download.pytorch.org/whl/xpu "torch==2.6.0+xpu"
.\.venv\Scripts\python.exe -u scripts\smoke_tinyllama_local_swarm.py --device xpu --timeout 300 --block-indices 0:8
```

The smoke script starts a local DHT peer, serves all eight tiny Llama blocks,
connects a distributed client through the local peer address, generates a few
tokens, and verifies exact token parity with the stock Transformers model. Pass
`--skip-reference` only when a faster connectivity-only check is sufficient.

To exercise in-generation worker replacement, start two complete local worker
replicas, stop the selected worker inside the active session, replay its cached
activation prefix on the survivor, and verify exact parity:

```powershell
.\.venv\Scripts\python.exe -u scripts\smoke_tinyllama_local_swarm.py `
    --device cpu --torch-dtype float32 --block-indices 0:8 `
    --test-failover --failover-tokens 8
```

To qualify any exact `ModelManifest v1` instead of the legacy TinyLlama default,
use the model-agnostic runner. It derives the repository, immutable revision, block
count, DHT namespace, dtype, and attention profile from the manifest, serves every
declared transformer block, compares generated token IDs with the stock model, and
writes bounded machine-readable evidence:

```powershell
.\.venv\Scripts\python.exe scripts\qualify_model_manifest.py `
    manifests\candidates\qwen3-1.7b-bfloat16-eager.json `
    --device cpu --with-failover `
    --output qualification-qwen3-1.7b-windows-cpu.json
```

Pass `--artifact-root` with a complete publisher snapshot to re-hash every declared
artifact before inference. A standard immutable Hugging Face snapshot also lets the
runner infer and reuse its Hub cache root; pass `--cache-dir` for other cache layouts.
`--manifest-only` performs the schema/runtime/artifact gate without loading the model.
The report always marks
`complete_release_qualification` false because multi-machine interruption recovery,
the cross-platform device matrix, the cold-client resource envelope, and redundant
public-worker soak require separate evidence.

## Supported models

Dense GQA models:

- Llama 3.x
- Qwen 2.5/3
- Gemma 2/3
- Mistral

Plus **Gemma 4** (per-layer embeddings + cross-server KV sharing; DRIFT-LLM serves the text tower of the multimodal checkpoints, e.g. `google/gemma-4-E2B-it`, including the MoE variant `google/gemma-4-26B-A4B-it`), **Gemma 4 Unified** (the dense mid-size branch with k=v full-attention layers, e.g. `google/gemma-4-12B-it`), **Mixtral** (mixture of experts), **DeepSeek-V3** (multi-head latent attention + MoE), **Falcon**, and **BLOOM**. Any checkpoint in one of these architectures on the Hugging Face Hub should work.

## Security

Running a server **does not** let others execute arbitrary code on your machine. A server only runs the model's forward and backward pass on the tensors it receives. Still, run a cluster only among machines and people you trust, and keep the DHT port off the public internet unless you intend it to be reachable.

## Contributing

Issues and pull requests are welcome on this repository. For advanced topics that still apply from the upstream project, like using multiple GPUs, running custom architectures, or AMD GPU setup, the original [Petals wiki](https://github.com/bigscience-workshop/petals/wiki) remains a useful reference.

## Attribution

This project is a hard fork of **Petals**, created by the [BigScience](https://bigscience.huggingface.co/) research workshop and collaborators. All credit for the original design and research belongs to its authors. If you build on this work, please cite the original papers:

Alexander Borzunov, Dmitry Baranchuk, Tim Dettmers, Max Ryabinin, Younes Belkada, Artem Chumachenko, Pavel Samygin, and Colin Raffel.
[Petals: Collaborative Inference and Fine-tuning of Large Models.](https://arxiv.org/abs/2209.01188)
_Proceedings of the 61st Annual Meeting of the Association for Computational Linguistics (Volume 3: System Demonstrations)._ 2023.

```bibtex
@inproceedings{borzunov2023petals,
  title = {Petals: Collaborative Inference and Fine-tuning of Large Models},
  author = {Borzunov, Alexander and Baranchuk, Dmitry and Dettmers, Tim and Riabinin, Maksim and Belkada, Younes and Chumachenko, Artem and Samygin, Pavel and Raffel, Colin},
  booktitle = {Proceedings of the 61st Annual Meeting of the Association for Computational Linguistics (Volume 3: System Demonstrations)},
  pages = {558--568},
  year = {2023},
  url = {https://arxiv.org/abs/2209.01188}
}
```

Alexander Borzunov, Max Ryabinin, Artem Chumachenko, Dmitry Baranchuk, Tim Dettmers, Younes Belkada, Pavel Samygin, and Colin Raffel.
[Distributed inference and fine-tuning of large language models over the Internet.](https://arxiv.org/abs/2312.08361)
_Advances in Neural Information Processing Systems_ 36 (2023).

```bibtex
@inproceedings{borzunov2023distributed,
  title = {Distributed inference and fine-tuning of large language models over the {I}nternet},
  author = {Borzunov, Alexander and Ryabinin, Max and Chumachenko, Artem and Baranchuk, Dmitry and Dettmers, Tim and Belkada, Younes and Samygin, Pavel and Raffel, Colin},
  booktitle = {Advances in Neural Information Processing Systems},
  volume = {36},
  pages = {12312--12331},
  year = {2023},
  url = {https://arxiv.org/abs/2312.08361}
}
```
