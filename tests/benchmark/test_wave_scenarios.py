"""The Sonnet 5.5 wave scenarios carry run.tfvars's values, and the pin smoke
carries its refs.

run.tfvars is the Sonnet 4.6 wave. run-sonnet55-low.tfvars and
run-sonnet55-medium.tfvars are the other two waves of the same pin, and the
comparison between the three is only valid while every value except the
model and the effort is the same. The files are plain tfvars that launch.sh
runs one at a time, so the sameness is pinned here rather than by an overlay
mechanism launch.sh does not have.
"""

from pathlib import Path

import pytest

TF = Path(__file__).resolve().parents[2] / "terraform"

WAVES = {
    "run-sonnet55-low.tfvars": "low",
    "run-sonnet55-medium.tfvars": "medium",
}


def _values(path: Path) -> list[str]:
    """The assignment lines of a tfvars file: comments and blank lines dropped,
    whitespace around `=` normalised so an alignment change is not a drift."""
    out = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(" = ".join(part.strip() for part in stripped.split("=", 1)) if "=" in stripped else stripped)
    return out


def _without(lines: list[str], *keys: str) -> list[str]:
    return [line for line in lines if not any(line.startswith(k + " ") or line == k for k in keys)]


@pytest.mark.parametrize("name,effort", sorted(WAVES.items()))
def test_a_wave_carries_every_run_value_but_model_and_effort(name, effort):
    run = _values(TF / "run.tfvars")
    wave = _values(TF / name)

    assert _without(wave, "agent_model", "reasoning_effort") == _without(run, "agent_model", "reasoning_effort")
    assert 'agent_model = "us.anthropic.claude-sonnet-5-5"' in wave
    assert f'reasoning_effort = "{effort}"' in wave


def test_the_run_itself_sets_no_effort():
    """Sonnet 4.6 takes no effort level; an effort in run.tfvars would be a
    value the 4.6 wave ignores and the 5.5 waves override, which hides drift."""
    assert not any(line.startswith("reasoning_effort") for line in _values(TF / "run.tfvars"))


def test_the_pin_smoke_carries_the_run_refs():
    """smoke-pin.tfvars validates the pin, so it has to carry the pin: its
    own header says so, and a re-anchor that moves run.tfvars's two refs and
    forgets these two lines would smoke the previous anchor while reporting
    itself as the pin check."""
    run = _values(TF / "run.tfvars")
    smoke = _values(TF / "smoke-pin.tfvars")

    for key in ("brain_go_ref", "sregym_ref"):
        want = [line for line in run if line.startswith(key + " ")]
        got = [line for line in smoke if line.startswith(key + " ")]
        assert want and got, key
        assert got == want
