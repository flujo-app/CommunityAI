#!/usr/bin/env sh
# DRIFT-LLM one-line installer for Linux and macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/flujo-app/CommunityAI/main/scripts/install.sh | sh
#
# Detects your OS and accelerator, installs a matching PyTorch build into a local
# .venv, and installs DRIFT-LLM (the `drift` package). Override the auto-detected
# accelerator with DRIFT_DEVICE=cpu|cuda|xpu|mps, e.g.:
#
#   DRIFT_DEVICE=xpu sh install.sh
#
# Windows users: use scripts/install.ps1 instead (it also builds the hivemind wheel).
set -eu

REPO_URL="${DRIFT_REPO_URL:-https://github.com/flujo-app/CommunityAI}"
DEVICE="${DRIFT_DEVICE:-auto}"
TORCH_SPEC="torch>=2.6,<2.7"

log() { printf '\033[1;36m[drift]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[drift] error:\033[0m %s\n' "$*" >&2; exit 1; }

# 1. Get the code: reuse the current checkout if we're in one, otherwise clone.
if [ -f pyproject.toml ] && grep -q '^name = "drift"' pyproject.toml 2>/dev/null; then
    log "using the checkout in $(pwd)"
else
    command -v git >/dev/null 2>&1 || die "git is required to fetch the code"
    log "cloning $REPO_URL"
    git clone --depth 1 "$REPO_URL" drift
    cd drift
fi

# 2. Detect the accelerator (best effort; Intel XPU is not reliably auto-detectable,
#    so set DRIFT_DEVICE=xpu explicitly for Intel Arc / integrated GPUs).
OS="$(uname -s)"
if [ "$DEVICE" = auto ]; then
    if [ "$OS" = Darwin ]; then
        DEVICE=mps
    elif command -v nvidia-smi >/dev/null 2>&1; then
        DEVICE=cuda
    else
        DEVICE=cpu
    fi
fi
log "target device: $DEVICE"

# 3. Create a Python environment, preferring uv (fast) and falling back to venv+pip.
if command -v uv >/dev/null 2>&1; then
    log "using uv"
    uv venv --python 3.12 .venv >/dev/null 2>&1 || uv venv .venv
    PIP="uv pip"
else
    command -v python3 >/dev/null 2>&1 || die "install uv (https://docs.astral.sh/uv/) or python3 first"
    log "uv not found; using python3 -m venv + pip"
    python3 -m venv .venv
    # shellcheck disable=SC1091
    . .venv/bin/activate
    PIP="python -m pip"
    $PIP install -U pip >/dev/null
fi
# Activate so `drift` lands on PATH for the closing hint (harmless if already active).
# shellcheck disable=SC1091
. .venv/bin/activate 2>/dev/null || true

# 4. Install a PyTorch build for the chosen device.
log "installing PyTorch ($DEVICE)"
case "$DEVICE" in
    cpu)  $PIP install --index-url https://download.pytorch.org/whl/cpu "$TORCH_SPEC" ;;
    cuda) $PIP install "$TORCH_SPEC" ;;  # default Linux wheels bundle CUDA
    xpu)  $PIP install --index-url https://download.pytorch.org/whl/xpu "torch==2.6.0+xpu" ;;
    mps)  $PIP install "$TORCH_SPEC" ;;  # default macOS wheels use Metal/MPS
    *)    die "unknown DRIFT_DEVICE=$DEVICE (expected cpu|cuda|xpu|mps)" ;;
esac

# 5. Install DRIFT-LLM itself.
log "installing DRIFT-LLM"
$PIP install -e .

log "done."
printf '\nStart a swarm on this machine:\n\n    drift up meta-llama/Llama-3.1-8B-Instruct\n\n'
printf 'It prints a `drift up ... --join drift://...` command; run that on your other\nmachines to add their compute to the same swarm.\n'
