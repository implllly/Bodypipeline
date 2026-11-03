#!/usr/bin/env bash
# Install the pinned Argus analysis toolchain for BodyPipeline.
#
# Run on the SERVER (the host that runs `pipeline.cli server`). Creates a
# separate Python 3.11 environment, because Argus pins 3.11 while letools pins
# 3.12; mixing them would break one of the two.
#
# The checkout is a real git repository at the pinned commit with the
# integration patch committed, because Argus refuses paid runs from a working
# tree with uncommitted changes.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARGUS_ROOT="${ARGUS_ROOT:-$ROOT/.runtime/argus}"
SRC="$ARGUS_ROOT/src"
REPO="https://github.com/Pantheon-Industries-Inc/argus"
REV="383ca11cf260e2db6da7ba901b8d60cfa8fc68fb"
PATCH="$ROOT/patches/argus/0001-pipeline-gateway.py"
TEMPLATE_PATCH="$ROOT/patches/argus/0002-task-template.py"
TEMPLATE_HELPER="$ROOT/pipeline/server/argus_template_contract.py"

command -v uv >/dev/null || { echo "Install uv before running this script" >&2; exit 1; }
command -v ffmpeg >/dev/null || {
  echo "ffmpeg 5.1+ is required; install it or put a static build on PATH" >&2
  exit 1
}
command -v ffprobe >/dev/null || {
  echo "ffprobe is required alongside ffmpeg" >&2
  exit 1
}
[[ -f "$PATCH" ]] || { echo "patch not found: $PATCH" >&2; exit 1; }

mkdir -p "$ARGUS_ROOT"
INTEGRATION_SUBJECT="integration: BodyPipeline gateway, mounting and pricing hooks"
if [[ ! -d "$SRC/.git" ]]; then
  git clone --filter=blob:none "$REPO" "$SRC"
else
  # The checkout is managed by this script. Refuse to replace one that carries
  # work of its own instead of silently orphaning a commit or losing edits.
  HEAD_REV="$(git -C "$SRC" rev-parse HEAD)"
  HEAD_SUBJECT="$(git -C "$SRC" log -1 --format=%s)"
  if [[ "$HEAD_REV" != "$REV" && "$HEAD_SUBJECT" != "$INTEGRATION_SUBJECT" ]]; then
    echo "Refusing to reset $SRC: HEAD is not the pinned commit or the BodyPipeline integration commit" >&2
    exit 1
  fi
  if [[ -n "$(git -C "$SRC" status --porcelain --untracked-files=no)" ]]; then
    echo "Refusing to reset $SRC: tracked files have local changes" >&2
    exit 1
  fi
fi
git -C "$SRC" fetch origin "$REV" --depth 1 || true
git -C "$SRC" checkout --detach "$REV"
git -C "$SRC" submodule update --init --depth 1 || true

# Apply the integration patch and commit it, so `git status` is clean and Argus
# will accept a paid run. An older patch version is replaced rather than stacked:
# the checkout is managed by this script, so resetting to the pinned commit is
# safe as long as nobody added work on top of it.
if ! grep -q "pipeline-argus-integration:v1" "$SRC/label/harness.py" \
   || ! grep -q "pipeline-argus-mounting:v2" "$SRC/prepare/formats.py" \
   || ! grep -q "pipeline-argus-gateway-key:v3" "$SRC/label/harness.py" \
   || ! grep -q "pipeline-argus-generic-models:v4" "$SRC/label/harness.py" \
   || ! grep -q "pipeline-argus-generic-route:v5" "$SRC/label/route.py" \
   || ! grep -q "pipeline-argus-generic-pricing:v5" "$SRC/label/pricing.py" \
   || ! grep -q "pipeline-argus-configured-prices:v6" "$SRC/label/harness.py" \
   || ! grep -q "pipeline-argus-generic-dry-run:v6" "$SRC/label/harness.py" \
   || ! grep -q "pipeline-argus-task-template:v1" "$SRC/label/episode.py" \
   || ! grep -q "pipeline-argus-task-template:v1" "$SRC/label/harness.py" \
   || ! grep -q "pipeline-argus-template-retry:v1" "$SRC/label/harness.py" \
   || ! cmp -s "$TEMPLATE_HELPER" "$SRC/label/pipeline_template.py"; then
  git -C "$SRC" reset --hard "$REV"
  python3 "$PATCH" "$SRC"
  python3 "$TEMPLATE_PATCH" "$SRC"
  git -C "$SRC" add label/pipeline_template.py
  git -C "$SRC" -c user.name=bodypipeline -c user.email=bodypipeline@local \
    commit -am "$INTEGRATION_SUBJECT"
fi

grep -q "pipeline-argus-mounting:v2" "$SRC/prepare/formats.py" || {
  echo "Argus mounting hook was not applied" >&2
  exit 1
}
grep -q "pipeline-argus-gateway-key:v3" "$SRC/label/harness.py" || {
  echo "Argus gateway key hook was not applied" >&2
  exit 1
}
grep -q "pipeline-argus-generic-models:v4" "$SRC/label/harness.py" || {
  echo "Argus generic gateway model hook was not applied" >&2
  exit 1
}
grep -q "pipeline-argus-generic-route:v5" "$SRC/label/route.py" || {
  echo "Argus generic gateway routing hook was not applied" >&2
  exit 1
}
grep -q "pipeline-argus-generic-pricing:v5" "$SRC/label/pricing.py" || {
  echo "Argus generic gateway pricing hook was not applied" >&2
  exit 1
}
grep -q "pipeline-argus-configured-prices:v6" "$SRC/label/harness.py" || {
  echo "Argus configured price hook was not applied" >&2
  exit 1
}
grep -q "pipeline-argus-generic-dry-run:v6" "$SRC/label/harness.py" || {
  echo "Argus generic dry-run pricing hook was not applied" >&2
  exit 1
}
grep -q "pipeline-argus-task-template:v1" "$SRC/label/episode.py" || {
  echo "Argus task template hook was not applied" >&2
  exit 1
}

UV_PROJECT_ENVIRONMENT="$ARGUS_ROOT/venv" uv sync \
  --project "$SRC" --frozen --python "${ARGUS_PYTHON:-3.11}"

(
  cd "$SRC"
  "$ARGUS_ROOT/venv/bin/python" - <<'PY'
import av, numpy, pandas, pyarrow, scipy  # noqa: F401
import board, checks, label, prepare, review  # noqa: F401
print("argus imports ok")
PY
)

# The automatic annotation page calls the official `python -m review` entry
# point.  Resolve it once here so a missing stage or option fails at install
# time rather than after a paid task is queued.
REVIEW_HELP="$(cd "$SRC" && "$ARGUS_ROOT/venv/bin/python" -m review --help)"
grep -q -- '--rig' <<<"$REVIEW_HELP" || {
  echo "Argus review does not expose --rig" >&2
  exit 1
}
grep -q -- '--inspection' <<<"$REVIEW_HELP" || {
  echo "Argus review does not expose --inspection" >&2
  exit 1
}
echo "argus review ok"

cat <<EOF

Argus installed.

  argus_src     = $SRC
  argus_python  = $ARGUS_ROOT/venv/bin/python

Add these to the Server config:

  "argus_src": "$SRC",
  "argus_python": "$ARGUS_ROOT/venv/bin/python",
  "argus_model": "openai/gpt-6-astra",
  "argus_models": ["openai/gpt-6-astra", "openai/gpt-6.1-sol"],
  "argus_per_episode_budget_usd": 1.0,
  "argus_max_tokens": 12000,
  "argus_annotation_concurrency": 8,
  "argus_annotation_timeout": 86400

Paid labelling needs a model key in the Server environment: either
PIPELINE_ARGUS_API_KEY, or OPENROUTER_API_KEYS / OPENAI_API_KEY as upstream
Argus reads them. With argus_base_url set, PIPELINE_ARGUS_API_KEY is sent to
that gateway. The automatic annotation page resolves the `review` module above;
it must stay on the pinned commit so its stage names and sidecar layout match
the parser.

A spend cap only dispatches a model whose rate is known. Argus ships rates for
openai/gpt-6-astra, openai/gpt-6-sol and openai/gpt-6.1-sol; price any other
gateway model in the Server config, in USD per token as
[input, cached input, output] per tier (one tier, or standard then long):

  "argus_prices": {
    "gpt-6.1-terra": [[1e-6, 5e-8, 5e-6], [2e-6, 1e-7, 7.5e-6]],
    "gpt-6-luna": [[1e-7, 1e-8, 5e-7], [2e-7, 2e-8, 7.5e-7]]
  }
EOF
