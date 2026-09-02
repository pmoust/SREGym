"""The incident start the driver reads off /get_app.

sregym#52: with no bounds the agent reasons over a flat hour ending now, of
which almost all is cluster build and app deploy. The conductor is the only
thing that knows when the fault actually landed, so it has to say.
"""

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace

from sregym.conductor import conductor_api

ROOT = Path(__file__).resolve().parents[2]


def _get_app(stub):
    conductor_api.set_conductor(stub)
    try:
        return asyncio.run(conductor_api.get_app())
    finally:
        conductor_api.set_conductor(None)


def _stub(fault_injected_at):
    app = SimpleNamespace(
        app_name="astronomy-shop",
        namespace="astronomy-shop",
        namespaces=["astronomy-shop"],
        description="demo",
    )

    return SimpleNamespace(app=app, fault_injected_at=fault_injected_at)


def test_get_app_reports_when_the_fault_landed():
    assert _get_app(_stub(1756795628.0))["incident_start"] == 1756795628.0


def test_get_app_reports_none_before_injection():
    """None and not 0, so a caller can tell "not yet" from the epoch.

    The driver treats a falsy value as unknown and sends no window at all,
    which leaves brain-go on its own default rather than pointing it at 1970.
    """
    assert _get_app(_stub(None))["incident_start"] is None


def test_the_incident_start_is_stamped_at_injection_not_from_the_budget_clock():
    """execution_start_time is reset *after* injection and measures the agent.

    Reusing it here would tie the incident window to a value that moves for an
    unrelated reason, and would leave a window in which /status already says
    "diagnosis" while the timestamp still holds the previous problem's value.
    So the stamp goes next to inject_fault, and TTL/TTM keep their own clock.
    """
    src = inspect.getsource(__import__("sregym.conductor.conductor", fromlist=["Conductor"]).Conductor._inject_fault)

    assert "self.fault_injected_at = time.time()" in src, "the incident start must be stamped inside _inject_fault"
    assert "execution_start_time" not in src, "the incident start must not be derived from the agent's budget clock"
