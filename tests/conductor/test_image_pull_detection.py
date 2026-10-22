"""sregym.conductor.image_pull_detection: tell a genuine image-pull harness
failure apart from an intentional image fault, from observed pod state
(NOFireAI/sregym#150).

No pre-pull list to key off any more (the per-attempt pre-pull step is gone;
see NOFIRE.md for the registry-mirror replacement), so the rule is now an
exclude list of the handful of images a fault injector plants on purpose,
not an allow list of images the harness pre-warmed.
"""

import types

from sregym.conductor.image_pull_detection import (
    FAULT_INTRODUCED_IMAGES,
    detect_image_pull_failures,
)
from sregym.generators.fault.inject_app import INCORRECT_IMAGE_DEFAULT
from sregym.generators.fault.inject_operator import WRONG_OPERATOR_IMAGE
from sregym.generators.fault.inject_virtual import CUSTOM_SERVICE_HANG_INIT_IMAGE


def _pod(namespace, name, image, reason="ImagePullBackOff", container_name="main", init_images=(), init_reason=None):
    statuses = [
        types.SimpleNamespace(
            name=container_name,
            image=image,
            state=types.SimpleNamespace(waiting=types.SimpleNamespace(reason=reason)),
        )
    ]
    init_statuses = [
        types.SimpleNamespace(
            name=f"init-{i}",
            image=init_image,
            state=types.SimpleNamespace(waiting=types.SimpleNamespace(reason=init_reason or "ImagePullBackOff")),
        )
        for i, init_image in enumerate(init_images)
    ]
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(namespace=namespace, name=name),
        status=types.SimpleNamespace(container_statuses=statuses, init_container_statuses=init_statuses),
    )


def test_flags_a_harness_owned_image_stuck_pulling():
    pod = _pod("social-network", "wrk2-job-abc", "deathstarbench/wrk2-client:latest")

    detail = detect_image_pull_failures([pod])

    assert detail is not None
    assert "deathstarbench/wrk2-client:latest" in detail
    assert "ImagePullBackOff" in detail
    assert "social-network/wrk2-job-abc" in detail


def test_flags_errimagepull_not_only_imagepullbackoff():
    pod = _pod("ns", "p", "some/image:latest", reason="ErrImagePull")

    assert detect_image_pull_failures([pod]) is not None


def test_does_not_flag_the_incorrect_image_fault():
    pod = _pod("astronomy-shop", "product-catalog-abc", INCORRECT_IMAGE_DEFAULT)

    assert detect_image_pull_failures([pod]) is None


def test_does_not_flag_the_kube_proxy_replacement_fault():
    from sregym.conductor.problems.workload_imbalance import KUBE_PROXY_REPLACEMENT_IMAGE

    pod = _pod("kube-system", "kube-proxy-abc", KUBE_PROXY_REPLACEMENT_IMAGE)

    assert detect_image_pull_failures([pod]) is None


def test_flags_a_missing_busybox_probe_image():
    """busybox:1.36 is both the hang-init fault's image and the probe image
    many oracles use. A pull stuck on it is a cache or registry miss, so it
    is a harness failure, not a fault working as designed."""
    pod = _pod("hotel-reservation", "probe-abc", CUSTOM_SERVICE_HANG_INIT_IMAGE)

    detail = detect_image_pull_failures([pod])

    assert detail is not None
    assert CUSTOM_SERVICE_HANG_INIT_IMAGE in detail


def test_does_not_flag_the_running_hang_init_container():
    """The hang-init fault's container runs sleep infinity once its image is
    present, so it is never in an image-pull waiting reason."""
    pod = _pod(
        "social-network",
        "custom-service-abc",
        "harmless:latest",
        reason="Pending",
        init_images=[CUSTOM_SERVICE_HANG_INIT_IMAGE],
        # Any reason outside ErrImagePull and ImagePullBackOff; the helper
        # cannot express a running container, which reads the same way.
        init_reason="PodInitializing",
    )

    assert detect_image_pull_failures([pod]) is None


def test_checks_init_container_statuses_too():
    """A stuck init container blocks the pod exactly as a stuck main
    container does. The old pre-pull-era detector only read
    container_statuses and missed this (documented gap in NOFIRE.md)."""
    pod = _pod(
        "social-network",
        "p",
        "harmless:latest",
        reason="Pending",  # the main container is just waiting on its init container
        init_images=["some/real-image:v1"],
    )

    detail = detect_image_pull_failures([pod])

    assert detail is not None
    assert "some/real-image:v1" in detail


def test_an_excluded_image_in_an_init_container_is_still_not_flagged():
    pod = _pod(
        "social-network",
        "custom-service-abc",
        "harmless:latest",
        reason="Pending",
        init_images=[INCORRECT_IMAGE_DEFAULT],
    )

    assert detect_image_pull_failures([pod]) is None


def test_no_pods_is_not_a_failure():
    assert detect_image_pull_failures([]) is None
    assert detect_image_pull_failures(None) is None


def test_a_ready_container_is_not_flagged():
    pod = _pod("ns", "p", "some/image:latest", reason=None)

    assert detect_image_pull_failures([pod]) is None


def test_does_not_flag_the_wrong_operator_image_fault():
    pod = _pod("tidb-operator", "tidb-controller-manager-abc", WRONG_OPERATOR_IMAGE)

    assert detect_image_pull_failures([pod]) is None


def test_fault_introduced_images_has_exactly_the_three_known_faults():
    assert {
        INCORRECT_IMAGE_DEFAULT,
        "docker.io/jackcuii/kube-proxy:v1.31.12",
        WRONG_OPERATOR_IMAGE,
    } == FAULT_INTRODUCED_IMAGES


def test_excluded_images_param_overrides_the_default_set():
    pod = _pod("ns", "p", "deathstarbench/wrk2-client:latest")

    assert detect_image_pull_failures([pod], excluded_images=frozenset({"deathstarbench/wrk2-client:latest"})) is None


def test_known_images_none_flags_any_non_excluded_image():
    """The default: with no pre-agent snapshot, every image not in
    excluded_images is eligible, matching detect_image_pull_failures' prior
    behavior before known_images existed."""
    pod = _pod("social-network", "p", "some/harness-owned:v1")

    assert detect_image_pull_failures([pod], known_images=None) is not None


def test_known_images_excludes_an_agent_introduced_tag():
    """An image the agent patched in after the fault (not part of the
    pre-agent snapshot) is not a harness failure, even though it is stuck
    pulling and is not one of the fixed fault images: it is the agent's own
    mistake."""
    pod = _pod("social-network", "checkout-abc", "my-registry/checkout:agent-typo-tag")

    detail = detect_image_pull_failures(
        [pod],
        known_images=frozenset({"docker.io/library/checkout:v1"}),
    )

    assert detail is None


def test_known_images_still_flags_a_pre_agent_image():
    """An image that was already part of the deployed workload before the
    agent started is still flagged, even with known_images set."""
    pod = _pod("social-network", "checkout-abc", "docker.io/library/checkout:v1")

    detail = detect_image_pull_failures(
        [pod],
        known_images=frozenset({"docker.io/library/checkout:v1"}),
    )

    assert detail is not None
    assert "docker.io/library/checkout:v1" in detail


def test_known_images_does_not_override_the_fault_exclusion():
    """A fault-introduced image is excluded even if it happens to also be in
    known_images (for example because the fault ran before the snapshot)."""
    pod = _pod("astronomy-shop", "product-catalog-abc", INCORRECT_IMAGE_DEFAULT)

    detail = detect_image_pull_failures(
        [pod],
        known_images=frozenset({INCORRECT_IMAGE_DEFAULT}),
    )

    assert detail is None
