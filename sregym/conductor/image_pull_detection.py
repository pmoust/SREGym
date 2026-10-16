"""Tell a genuine image-pull harness failure apart from an intentional
image fault, from observed pod state alone (NOFireAI/sregym#150).

The benchmark no longer pre-pulls a problem's images before its fault runs
(sregym/service/image_prepull.py and the per-attempt pre-pull step are gone;
see NOFIRE.md for the registry-mirror replacement, which cuts cold-pull time
at the cluster level instead of the per-attempt level). Without a pre-pull
list to key off, "this attempt's own image" can no longer mean "an image we
pre-pulled for it": it now means "any image a container in the app's own
namespace(s) is waiting on", with two things subtracted out:

- A small, fixed set of images a fault injector plants on purpose. These are
  never expected to resolve (or, for a real-but-degraded image, are not what
  the attempt is being measured on) and must not be mistaken for a harness
  problem:

  - ``inject_app.INCORRECT_IMAGE_DEFAULT`` ("app-image:latest"): a tag that
    exists on no registry, the fault under test for the incorrect_image
    family of problems.
  - ``workload_imbalance.KUBE_PROXY_REPLACEMENT_IMAGE``: a real but degraded
    kube-proxy build, the fault under test for WorkloadImbalance. It is
    injected into kube-system, outside any app's own namespace, so today's
    namespace-scoped scan would not see it anyway; excluded for the same
    reason the others are, in case that scope ever widens.
  - ``inject_virtual.CUSTOM_SERVICE_HANG_INIT_IMAGE`` ("busybox:1.36"): the
    rolling_update_misconfigured fault's init container, which sleeps
    forever by design (``sleep infinity``) and is not an image-pull problem
    even though it keeps the pod pending.
  - ``inject_operator.WRONG_OPERATOR_IMAGE`` ("pingcap/tidb-operatorr:v1.6.3",
    typo in "operatorr"): a tag that exists on no registry, the fault under
    test for operator_wrong_operator_image.

  Four images today; see FAULT_INTRODUCED_IMAGES for the current set rather
  than trusting a number in this docstring to stay in sync.

- Any image not present in the caller's ``known_images`` (the set of images
  already deployed before the agent's first stage got control, recorded by
  Conductor._snapshot_namespace_images right after fault injection). An
  image outside that set was introduced after the fault, which in practice
  means the agent itself patched it in, for example a bad mitigation tag. A
  pull stuck on an image the agent chose is the agent's own mistake, not a
  harness failure, and scoring it as harness_failure would both mislabel the
  mitigation and drop the attempt from graded (score_run.py's
  GRADED_EXCLUDING_HARNESS_FAILURES), inflating the pass rate by removing a
  bad outcome instead of counting it.

Every other image a container in the app's own namespace(s) is waiting on is
harness-owned: it is the application, its workload generator, or something
else the deploy itself needed to come up, not a fault the problem planted and
not something the agent introduced.
"""

from __future__ import annotations

from sregym.conductor.problems.workload_imbalance import KUBE_PROXY_REPLACEMENT_IMAGE
from sregym.generators.fault.inject_app import INCORRECT_IMAGE_DEFAULT
from sregym.generators.fault.inject_operator import WRONG_OPERATOR_IMAGE
from sregym.generators.fault.inject_virtual import CUSTOM_SERVICE_HANG_INIT_IMAGE

# A container waiting on one of these is a fault working as designed, never
# a harness failure. See the module docstring for what each one is and why.
FAULT_INTRODUCED_IMAGES = frozenset(
    {
        INCORRECT_IMAGE_DEFAULT,
        KUBE_PROXY_REPLACEMENT_IMAGE,
        CUSTOM_SERVICE_HANG_INIT_IMAGE,
        WRONG_OPERATOR_IMAGE,
    }
)

# A container stuck in either of these waiting reasons is a pull that never
# finished, as opposed to a still-in-progress one.
_IMAGE_PULL_WAITING_REASONS = {"ErrImagePull", "ImagePullBackOff"}


def detect_image_pull_failures(
    pods,
    excluded_images: frozenset[str] = FAULT_INTRODUCED_IMAGES,
    known_images: frozenset[str] | None = None,
) -> str | None:
    """Whether observed pod state shows a container stuck pulling an image
    the harness did not plant as a fault, and if so, which one.

    Scans both ``container_statuses`` and ``init_container_statuses``: a
    container and an init container's waiting-reason shape are the same
    ``ContainerStateWaiting``, and a stuck init container blocks the pod
    exactly as a stuck main container does (an init container must finish
    before any of its pod's main containers can start).

    Returns the first matching failure as a one-line detail, or None when
    nothing matches, including an empty pod list, which reads as "nothing
    to flag", not an error.

    ``excluded_images`` defaults to this module's own FAULT_INTRODUCED_IMAGES
    and is a parameter (not a module-level-only constant) so a test can pass
    a smaller or different set without monkeypatching the module.

    ``known_images``, when given, is the set of images already deployed
    before the agent started (see the module docstring): an image stuck
    pulling that is not excluded but also not in ``known_images`` was
    introduced after that point, almost always by the agent's own
    mitigation, and is not flagged. Left as None (the default), every image
    not in ``excluded_images`` is eligible to be flagged, which is also the
    fallback a caller should use when it could not take a pre-agent
    snapshot.
    """
    if not pods:
        return None

    for pod in pods:
        status = getattr(pod, "status", None)
        statuses = list(getattr(status, "init_container_statuses", None) or []) + list(
            getattr(status, "container_statuses", None) or []
        )
        for container_status in statuses:
            image = getattr(container_status, "image", None)
            if not image or image in excluded_images:
                continue
            if known_images is not None and image not in known_images:
                continue

            state = getattr(container_status, "state", None)
            waiting = getattr(state, "waiting", None) if state else None
            reason = getattr(waiting, "reason", None) if waiting else None
            if reason in _IMAGE_PULL_WAITING_REASONS:
                pod_name = getattr(getattr(pod, "metadata", None), "name", "?")
                namespace = getattr(getattr(pod, "metadata", None), "namespace", "?")
                container = getattr(container_status, "name", "?")
                return f"{namespace}/{pod_name} container {container}: {reason} for {image}"

    return None
