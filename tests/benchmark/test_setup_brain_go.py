"""setup_brain_go.sh writes the keys it forces into the box's deploy/.env.

The script is run end to end against a fake brain-go checkout. dev-env.sh,
make, docker and curl are stubs on PATH, so nothing starts and nothing reaches
IMDS. What is asserted is the deploy/.env the script leaves behind, because
that file is what docker compose hands every brain-go container.
"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "benchmark" / "setup_brain_go.sh"

# dev-env.sh ensure creates deploy/.env the way brain-go's does: with the two
# credentials the script reads back after `make up`, and brain-go's own
# LOG_LEVEL=info from deploy/.env.sample, so the test shows the forced value
# replacing an existing key rather than only appending a missing one.
FAKE_DEV_ENV = """#!/bin/sh
mkdir -p deploy
[ -f deploy/.env ] || printf '%s\\n' \\
  LOG_LEVEL=info \\
  DEV_API_KEY=cli_fake \\
  DEV_SESSION=session_fake >deploy/.env
"""

# IMDS is unreachable, so the script takes its developer-box branch and skips
# the credential write. Every other URL is brain-go's /health, which answers.
FAKE_CURL = """#!/bin/sh
case "$*" in
  *169.254.169.254*) exit 1 ;;
esac
exit 0
"""

FAKE_DOCKER = """#!/bin/sh
echo "Dev stack ready"
"""


def _run(tmp_path, env_extra=None):
    brain_go = tmp_path / "brain-go"
    (brain_go / "scripts").mkdir(parents=True)
    dev_env = brain_go / "scripts" / "dev-env.sh"
    dev_env.write_text(FAKE_DEV_ENV)
    dev_env.chmod(0o755)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("curl", FAKE_CURL), ("docker", FAKE_DOCKER), ("make", "#!/bin/sh\nexit 0\n")):
        stub = bindir / name
        stub.write_text(body)
        stub.chmod(0o755)

    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "BRAIN_GO_DIR": str(brain_go),
        "BRAIN_GO_ENV_OUT": str(tmp_path / "sregym-brain-go.sh"),
        "AGENT_MODEL_ID": "test-model",
    }
    env.update(env_extra or {})
    done = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env, cwd=str(ROOT))
    assert done.returncode == 0, done.stdout + done.stderr

    lines = (brain_go / "deploy" / ".env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line), lines


def test_the_box_runs_brain_go_at_debug(tmp_path):
    """At info the archived brain-go log carries no edge ingest line, so a run
    cannot tell a publish that never arrived from one that was dropped. The
    ingest success line is written at DEBUG, so the box forces debug over the
    info that brain-go's own sample .env starts with."""
    env, lines = _run(tmp_path)

    assert env["LOG_LEVEL"] == "debug"
    assert sum(1 for line in lines if line.startswith("LOG_LEVEL=")) == 1


def test_the_other_forced_keys_still_land(tmp_path):
    env, _ = _run(tmp_path)

    assert env["DEV_CUSTOMER"] == "sregym-bench"
    assert env["LLM_PROVIDER"] == "bedrock"
    assert env["LLM_MODEL"] == "test-model"
    assert env["FEATURE_FORCE_RCI_FOR_CLI_ENABLED"] == "true"


def test_a_run_configured_level_overrides_the_forced_one(tmp_path):
    """BRAIN_ENV_EXTRA is applied after the forced keys, so a run that wants a
    different level can still set one without editing the script."""
    env, _ = _run(tmp_path, {"BRAIN_ENV_EXTRA": "LOG_LEVEL=warn"})

    assert env["LOG_LEVEL"] == "warn"


def test_the_native_arm_enables_brain_gos_kubernetes_tools(tmp_path):
    """brain-go ships its k8s_* tool family off. The native arm investigates
    through that family, so the box must turn it on or every k8s_* call fails
    closed while the seeded kubernetes connection reads as healthy."""
    env, lines = _run(tmp_path, {"SREGYM_ARM": "native"})

    assert env["FEATURE_KUBERNETES_TOOLS_ENABLED"] == "true"
    assert sum(1 for line in lines if line.startswith("FEATURE_KUBERNETES_TOOLS_ENABLED=")) == 1


def test_the_mcp_arm_leaves_brain_gos_kubernetes_tools_alone(tmp_path):
    """The MCP arm reaches the cluster through its sregym-kubectl connection
    and is the frozen anchor, so the flag is not written for it."""
    env, _ = _run(tmp_path, {"SREGYM_ARM": "mcp"})

    assert "FEATURE_KUBERNETES_TOOLS_ENABLED" not in env


def test_the_edge_arm_leaves_brain_gos_kubernetes_tools_alone(tmp_path):
    """Edge seeds no kubernetes connection at all, so there is nothing for the
    k8s_* tool family to serve and the flag stays off, the product default."""
    env, _ = _run(tmp_path, {"SREGYM_ARM": "edge"})

    assert "FEATURE_KUBERNETES_TOOLS_ENABLED" not in env


def test_the_edge_arm_accepts_an_override_that_leaves_kubernetes_tools_off(tmp_path):
    """An edge run's BRAIN_ENV_EXTRA is free to set other keys; only turning
    the k8s_* tool family on is refused."""
    env, _ = _run(tmp_path, {"SREGYM_ARM": "edge", "BRAIN_ENV_EXTRA": "FEATURE_KUBERNETES_TOOLS_ENABLED=false"})

    assert env["FEATURE_KUBERNETES_TOOLS_ENABLED"] == "false"


def test_the_edge_arm_refuses_an_override_that_turns_kubernetes_tools_on(tmp_path):
    """Edge measures brain-go with no Kubernetes connection. An override that
    enables the k8s_* tool family anyway would give it tools with nothing to
    resolve against, which is not what this arm is for, so the box must not
    start rather than silently run a different experiment."""
    brain_go = tmp_path / "brain-go"
    (brain_go / "scripts").mkdir(parents=True)
    dev_env = brain_go / "scripts" / "dev-env.sh"
    dev_env.write_text(FAKE_DEV_ENV)
    dev_env.chmod(0o755)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("curl", FAKE_CURL), ("docker", FAKE_DOCKER), ("make", "#!/bin/sh\nexit 0\n")):
        stub = bindir / name
        stub.write_text(body)
        stub.chmod(0o755)

    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "BRAIN_GO_DIR": str(brain_go),
        "BRAIN_GO_ENV_OUT": str(tmp_path / "sregym-brain-go.sh"),
        "AGENT_MODEL_ID": "test-model",
        "SREGYM_ARM": "edge",
        "BRAIN_ENV_EXTRA": "FEATURE_KUBERNETES_TOOLS_ENABLED=true",
    }
    done = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env, cwd=str(ROOT))

    assert done.returncode != 0
    assert "FEATURE_KUBERNETES_TOOLS_ENABLED" in done.stderr
    assert "edge" in done.stderr


def test_the_native_arm_tolerates_the_flag_brain_env_extra_would_also_set(tmp_path):
    """The edge-only refusal must not misfire on native, where the flag is
    expected to be true regardless of what BRAIN_ENV_EXTRA says."""
    env, _ = _run(tmp_path, {"SREGYM_ARM": "native", "BRAIN_ENV_EXTRA": "FEATURE_KUBERNETES_TOOLS_ENABLED=true"})

    assert env["FEATURE_KUBERNETES_TOOLS_ENABLED"] == "true"


def test_the_box_raises_the_inline_evidence_cap_for_every_arm(tmp_path):
    """The box has no evidence object store, so a tool result over the cap
    fails its call closed. Every arm gets the raised cap."""
    for arm in ("native", "mcp", "edge"):
        env, lines = _run(tmp_path / arm, {"SREGYM_ARM": arm})

        assert env["FEDERATION_EVIDENCE_INLINE_MAX_BYTES"] == "4194304", arm
        assert sum(1 for line in lines if line.startswith("FEDERATION_EVIDENCE_INLINE_MAX_BYTES=")) == 1, arm
