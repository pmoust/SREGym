"""setup_brain_go.sh's host-image gate: before `make up`, every image
scripts/benchmark/brain_go_images.py derives from the checked-out brain-go
ref must already be `docker image inspect`-able, or the script dies naming
every one still absent.

Runs the real script end to end with a fake `docker` on PATH: `docker
compose ... config --format json` returns a small, fixed compose shape (two
services, neither with a "build" key, so the real brain_go_images.py runs
unmodified without needing a real Dockerfile fixture); `docker image
inspect <ref>` fails only for a ref this test marks "missing" first. `aws`
is also a stub, since REGISTRY_CACHE_BUCKET being set is what turns this
gate on at all.
"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "benchmark" / "setup_brain_go.sh"

FAKE_DEV_ENV = """#!/bin/sh
mkdir -p deploy
[ -f deploy/.env ] || printf '%s\\n' \\
  LOG_LEVEL=info \\
  DEV_API_KEY=cli_fake \\
  DEV_SESSION=session_fake >deploy/.env
"""

FAKE_CURL = """#!/bin/sh
case "$*" in
  *169.254.169.254*) exit 1 ;;
esac
exit 0
"""

# IMDS answers the token request but has no role to offer: this is enough
# for setup_brain_go.sh's own "is this an EC2 instance" check (a non-empty
# IMDS_TOKEN), without needing a full fake credentials payload.
FAKE_CURL_IMDS_TOKEN_ONLY = """#!/bin/sh
case "$*" in
  *api/token*) echo "faketoken"; exit 0 ;;
  *169.254.169.254*) exit 1 ;;
esac
exit 0
"""

# Two services, both pullable "image:"-only (no "build" key), the exact
# shape `docker compose config --format json` returns for a service with no
# build. This is intentionally minimal: brain_go_images.py's own handling of
# a build-keyed service (reading a Dockerfile's ARG defaults) is covered by
# tests/benchmark/test_brain_go_images.py, not repeated here.
FAKE_COMPOSE_CONFIG_JSON = """{
  "services": {
    "postgres": {"image": "testpg/testpg:1"},
    "localstack": {"image": "testls/testls:1"}
  }
}"""

FAKE_AWS = """#!/bin/sh
# load_host_image's two reads: the digest pointer (stdout) and the tarball
# (a file `aws s3 cp <key> <dest>` must create for `docker load` to find).
case "$*" in
  *".digest"*)
    echo "sha256:$(printf '%040x' 1)"
    ;;
  *)
    for a in "$@"; do
      last="$a"
    done
    touch "$last" 2>/dev/null || true
    ;;
esac
exit 0
"""


def _fake_docker(compose_json_path: Path, missing_dir: Path) -> str:
    # docker compose ... config --format json -> the fixture JSON.
    # docker compose ... logs seed -> "Dev stack ready" (the seed-success
    # check later in the script).
    # docker image inspect <ref> -> fails iff a marker file for that ref
    # exists under missing_dir (this test's control knob).
    # docker inspect --format ... <ref> -> the same digest FAKE_AWS prints,
    # so load_host_image's own loaded-id check passes regardless.
    # docker load -i ... -> always succeeds.
    return f"""#!/bin/sh
if [ "$1" = "compose" ]; then
  shift
  is_config=0
  is_logs=0
  for a in "$@"; do
    case "$a" in
      config) is_config=1 ;;
      logs) is_logs=1 ;;
    esac
  done
  if [ "$is_config" = 1 ]; then
    cat "{compose_json_path}"
    exit 0
  fi
  if [ "$is_logs" = 1 ]; then
    echo "Dev stack ready"
    exit 0
  fi
  exit 0
fi
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
  shift 2
  # The ref is always the last positional arg, whether this is a plain
  # "docker image inspect <ref>" or "docker image inspect --format
  # '{{{{.Id}}}}' <ref>".
  for ref in "$@"; do :; done
  key=$(printf '%s' "$ref" | tr '/:' '__')
  if [ -f "{missing_dir}/$key" ]; then
    exit 1
  fi
  echo "sha256:$(printf '%040x' 1)"
  exit 0
fi
if [ "$1" = "inspect" ]; then
  echo "sha256:$(printf '%040x' 1)"
  exit 0
fi
if [ "$1" = "load" ]; then
  exit 0
fi
exit 0
"""


def _run(tmp_path, missing_refs=(), registry_cache_bucket="fake-bucket", curl_script=FAKE_CURL):
    brain_go = tmp_path / "brain-go"
    (brain_go / "scripts").mkdir(parents=True)
    dev_env = brain_go / "scripts" / "dev-env.sh"
    dev_env.write_text(FAKE_DEV_ENV)
    dev_env.chmod(0o755)
    subprocess.run(["git", "init", "-q"], cwd=str(brain_go), check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x"],
        cwd=str(brain_go),
        check=True,
    )

    compose_json_path = tmp_path / "compose-config.json"
    compose_json_path.write_text(FAKE_COMPOSE_CONFIG_JSON)

    missing_dir = tmp_path / "missing"
    missing_dir.mkdir()
    for ref in missing_refs:
        key = ref.replace("/", "_").replace(":", "_")
        (missing_dir / key).touch()

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "curl").write_text(curl_script)
    (bindir / "curl").chmod(0o755)
    (bindir / "make").write_text("#!/bin/sh\nexit 0\n")
    (bindir / "make").chmod(0o755)
    (bindir / "docker").write_text(_fake_docker(compose_json_path, missing_dir))
    (bindir / "docker").chmod(0o755)
    (bindir / "aws").write_text(FAKE_AWS)
    (bindir / "aws").chmod(0o755)

    host_image_manifest = tmp_path / "host-images.tsv"
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "BRAIN_GO_DIR": str(brain_go),
        "BRAIN_GO_ENV_OUT": str(tmp_path / "sregym-brain-go.sh"),
        "AGENT_MODEL_ID": "test-model",
        "SREGYM_HOST_IMAGE_MANIFEST": str(host_image_manifest),
    }
    if registry_cache_bucket is not None:
        env["REGISTRY_CACHE_BUCKET"] = registry_cache_bucket
    done = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env, cwd=str(ROOT))
    done.host_image_manifest = host_image_manifest
    return done


def _die_line(stderr: str) -> str:
    lines = [line for line in stderr.splitlines() if "still absent after the load step" in line]
    assert len(lines) == 1, f"expected exactly one gate failure line, got: {stderr!r}"
    return lines[0]


def test_the_gate_passes_when_every_derived_image_is_present(tmp_path):
    # log() writes to stdout; die() and load_host_image's own progress lines
    # write to stderr.
    done = _run(tmp_path, missing_refs=())
    assert done.returncode == 0, done.stdout + done.stderr
    assert "required host images are all present" in done.stdout


def test_the_gate_records_loaded_image_ids_for_the_run_manifest(tmp_path):
    """A run should state which bytes its brain-go host images actually
    were. The gate writes "<name>\\t<ref>\\t<loaded id>" per image to
    SREGYM_HOST_IMAGE_MANIFEST, which write_run_manifest.sh reads."""
    done = _run(tmp_path, missing_refs=())
    assert done.returncode == 0, done.stdout + done.stderr
    rows = sorted(line.split("\t") for line in done.host_image_manifest.read_text().splitlines() if line)
    assert [r[:2] for r in rows] == [
        ["testls-testls__1", "testls/testls:1"],
        ["testpg-testpg__1", "testpg/testpg:1"],
    ]
    assert all(r[2].startswith("sha256:") for r in rows)


def test_the_gate_dies_naming_every_image_still_missing(tmp_path):
    done = _run(tmp_path, missing_refs=("testpg/testpg:1", "testls/testls:1"))
    assert done.returncode != 0
    die_line = _die_line(done.stderr)
    assert "testpg/testpg:1" in die_line
    assert "testls/testls:1" in die_line
    # make is never reached once the gate fails.
    assert "Dev stack ready" not in done.stdout


def test_the_gate_names_only_the_image_that_is_actually_missing(tmp_path):
    done = _run(tmp_path, missing_refs=("testls/testls:1",))
    assert done.returncode != 0
    die_line = _die_line(done.stderr)
    assert "testls/testls:1" in die_line
    assert "testpg/testpg:1" not in die_line


def test_an_empty_bucket_on_a_developer_machine_skips_the_gate(tmp_path):
    """No IMDS token (curl to 169.254.169.254 fails outright) reads as a
    developer machine, not a benchmark box: an unset REGISTRY_CACHE_BUCKET
    is expected there, so the gate is skipped and `make up` still runs."""
    done = _run(tmp_path, registry_cache_bucket=None, curl_script=FAKE_CURL)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "skipping the host-image cache" in done.stdout


def test_an_empty_bucket_on_an_ec2_instance_fails_loud(tmp_path):
    """IMDS answering the token request means this is an EC2 instance: an
    unset REGISTRY_CACHE_BUCKET there means the bootstrap failed to export
    it, not that this is a developer machine, so the script must die rather
    than silently let `make up`'s compose pull reach Docker Hub."""
    done = _run(tmp_path, registry_cache_bucket=None, curl_script=FAKE_CURL_IMDS_TOKEN_ONLY)
    assert done.returncode != 0
    assert "REGISTRY_CACHE_BUCKET is unset on an EC2 instance" in done.stderr
    assert "Dev stack ready" not in done.stdout
