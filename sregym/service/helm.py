"""Interface for helm operations"""

import logging
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import yaml

from sregym.service.kubectl import KubeCtl

logger = logging.getLogger("all.infra.helm")
logger.propagate = True
logger.setLevel(logging.DEBUG)

# Packaged dependency charts this repo carries for a local chart that needs
# them, one subdirectory per chart name (the `name` field in that chart's own
# Chart.yaml). A lite benchmark run must not fetch dependency charts over the
# network mid-run, so Helm.install never runs `helm dependency update`; a
# chart whose own `charts/` directory does not already have every declared
# dependency gets the missing ones copied in from here instead.
VENDORED_DEPENDENCY_CHARTS_DIR = Path(__file__).resolve().parents[1] / "resources" / "vendored" / "charts"


def _version_pattern(version: str) -> str:
    """Turn a Chart.yaml dependency version (exact, or a wildcard like
    "5.15.*") into a regex matching the versions it allows."""
    return re.escape(version).replace(r"\*", ".*")


def _find_tgz(directory: Path, name: str, version: str) -> Path | None:
    if not directory.is_dir():
        return None
    pattern = re.compile(rf"{re.escape(name)}-{_version_pattern(version)}\.tgz$")
    for entry in sorted(directory.glob(f"{name}-*.tgz")):
        if pattern.fullmatch(entry.name):
            return entry
    return None


def _any_tgz_for(directory: Path, name: str) -> Path | None:
    if not directory.is_dir():
        return None
    for entry in sorted(directory.glob(f"{name}-*.tgz")):
        return entry
    return None


def _subchart_dir_exists(directory: Path, name: str) -> bool:
    return (directory / name / "Chart.yaml").exists()


def _resolve_local_chart(chart_path: str) -> tuple[str, tempfile.TemporaryDirectory | None]:
    """Return a chart path safe to `helm install` with no network access.

    Reads the chart's own declared dependencies and checks each one is
    already present, by name, in its `charts/` directory, either as a
    `<name>-<anything>.tgz` or an unpacked `<name>/` subdirectory; a
    dependency missing there is copied in from
    VENDORED_DEPENDENCY_CHARTS_DIR/<chart name>/ instead, matched on the
    exact version the chart declares. A dependency missing from both raises
    before any `helm` subprocess runs, naming the chart and the dependency.

    Returns (path_to_pass_to_helm, tmp_dir). tmp_dir is None when
    chart_path can be used unmodified; otherwise the caller must keep it
    alive until the helm command has run and then call tmp_dir.cleanup().
    """
    chart_dir = Path(chart_path)
    with open(chart_dir / "Chart.yaml") as f:
        chart_yaml = yaml.safe_load(f) or {}

    dependencies = chart_yaml.get("dependencies") or []
    if not dependencies:
        return chart_path, None

    chart_name = chart_yaml.get("name", chart_dir.name)
    own_charts_dir = chart_dir / "charts"
    vendored_dir = VENDORED_DEPENDENCY_CHARTS_DIR / chart_name

    missing = []
    to_copy = []
    for dependency in dependencies:
        name = dependency["name"]
        version = str(dependency.get("version", ""))
        # Presence in the chart's own charts/ directory is checked by name
        # only, matching what `helm install`/`helm template` itself does:
        # neither one re-validates an already-packaged or already-unpacked
        # subchart against the parent's declared version, so a chart
        # already committed here with a dependency one patch version ahead
        # of its own Chart.lock (drift, not a missing dependency) still
        # installs. Picking a vendored tgz when nothing is present
        # locally DOES match on version: that choice is ours to get right,
        # not something already on disk to merely trust.
        if _any_tgz_for(own_charts_dir, name) is not None:
            continue
        if _subchart_dir_exists(own_charts_dir, name):
            continue
        vendored_tgz = _find_tgz(vendored_dir, name, version)
        if vendored_tgz is None:
            missing.append(f"{name} {version}")
            continue
        to_copy.append(vendored_tgz)

    if missing:
        raise RuntimeError(
            f"Helm chart '{chart_name}' at {chart_path} declares dependencies that are "
            f"neither in its own charts/ directory nor vendored under "
            f"{VENDORED_DEPENDENCY_CHARTS_DIR}/{chart_name}/: {', '.join(missing)}. "
            "Vendor the missing dependency tgz there (see sregym/resources/vendored/"
            "README.md) before installing this chart."
        )

    if not to_copy:
        return chart_path, None

    tmp_dir = tempfile.TemporaryDirectory(prefix="sregym-helm-chart-")
    tmp_chart_dir = Path(tmp_dir.name) / chart_dir.name
    shutil.copytree(chart_dir, tmp_chart_dir)
    (tmp_chart_dir / "charts").mkdir(exist_ok=True)
    for tgz in to_copy:
        shutil.copy2(tgz, tmp_chart_dir / "charts" / tgz.name)
    return str(tmp_chart_dir), tmp_dir


class Helm:
    @staticmethod
    def install(**args):
        """Install a helm chart

        Args:
            release_name (str): Name of the release
            chart_path (str): Path to the helm chart
            namespace (str): Namespace to install the chart
            version (str): Version of the chart
            extra_args (List[str)]: Extra arguments for the helm install command
            remote_chart (bool): Whether the chart is remote (from a Helm repo)
        """

        release_name = args.get("release_name")
        chart_path = args.get("chart_path")
        namespace = args.get("namespace")
        version = args.get("version")
        extra_args = args.get("extra_args")
        remote_chart = args.get("remote_chart", False)

        logger.info(f"Helm Install: {release_name} in namespace {namespace}")

        tmp_dir = None
        if not remote_chart:
            chart_path, tmp_dir = _resolve_local_chart(chart_path)

        try:
            command = f"helm install {release_name} {chart_path} -n {namespace} --create-namespace"

            # --version only does anything when chart_path is a repo
            # coordinate (e.g. "grafana/loki"): Helm resolves that name
            # against the version this flag names. When chart_path is a
            # local path instead (a directory, or a packaged .tgz such as
            # the vendored charts under sregym/resources/vendored/), Helm
            # ignores --version entirely and installs whatever that path
            # actually contains; the file's own name is what pins the
            # version in that case. Still passed here when a caller sets
            # it, because a repo-coordinate caller needs it and a
            # local-path caller passing it too is harmless, just a no-op,
            # not wrong.
            if version:
                command += f" --version {version}"

            if extra_args:
                command += " " + " ".join(extra_args)

            process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            output, error = process.communicate()

            stderr = error.decode("utf-8").strip() if error else ""
            stdout = output.decode("utf-8").strip() if output else ""

            if stderr:
                logger.warning(f"Helm install stderr output:\n{stderr}")

            if process.returncode != 0:
                raise RuntimeError(
                    f"Helm install failed for release '{release_name}' in namespace '{namespace}'. "
                    f"Chart: {chart_path}. Error output:\n{stderr}\n"
                    f"Stdout (for context):\n{stdout}"
                )
            else:
                logger.debug(stdout)
        finally:
            if tmp_dir is not None:
                tmp_dir.cleanup()

    @staticmethod
    def uninstall(**args):
        """Uninstall a helm chart

        Args:
            release_name (str): Name of the release
            namespace (str): Namespace to uninstall the chart
        """
        release_name = args.get("release_name")
        namespace = args.get("namespace")

        logger.info(f"Helm Uninstall: {release_name} in namespace {namespace}")

        if not Helm.exists_release(release_name, namespace):
            logger.warning(f"Release {release_name} does not exist. Skipping uninstall.")
            return

        command = f"helm uninstall {release_name} -n {namespace}"
        process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output, error = process.communicate()

        stderr = error.decode("utf-8").strip() if error else ""
        stdout = output.decode("utf-8").strip() if output else ""

        if stderr:
            logger.warning(f"Helm uninstall stderr output:\n{stderr}")

        if process.returncode != 0:
            raise RuntimeError(
                f"Helm uninstall failed for release '{release_name}' in namespace '{namespace}'. "
                f"Release: {release_name}. Error output:\n{stderr}\n"
                f"Stdout (for context):\n{stdout}"
            )
        else:
            logger.debug(stdout)

    @staticmethod
    def exists_release(release_name: str, namespace: str) -> bool:
        """Check if a Helm release exists

        Args:
            release_name (str): Name of the release
            namespace (str): Namespace to check

        Returns:
            bool: True if release exists
        """
        command = f"helm list -n {namespace}"
        process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE)
        output, error = process.communicate()

        if error:
            logger.error(error.decode("utf-8"))
            return False
        else:
            return release_name in output.decode("utf-8")

    @staticmethod
    def assert_if_deployed(namespace: str):
        """Assert if all services in the application are deployed

        Args:
            namespace (str): Namespace to check

        Returns:
            bool: True if deployed

        Raises:
            Exception: If not deployed
        """
        kubectl = KubeCtl()
        try:
            kubectl.wait_for_ready(namespace)
        except Exception as e:
            raise e

        return True

    @staticmethod
    def upgrade(**args):
        """Upgrade a helm chart

        Args:
            release_name (str): Name of the release
            chart_path (str): Path to the helm chart
            namespace (str): Namespace to upgrade the chart
            values_file (str): Path to the values.yaml file
            set_values (dict): Key-value pairs for --set options
        """

        release_name = args.get("release_name")
        chart_path = args.get("chart_path")
        namespace = args.get("namespace")
        values_file = args.get("values_file")
        set_values = args.get("set_values", {})

        logger.info(f"Helm Upgrade: {release_name} in namespace {namespace}")

        command = [
            "helm",
            "upgrade",
            release_name,
            chart_path,
            "-n",
            namespace,
            "-f",
            values_file,
            "--server-side=true",
            "--force-conflicts",
        ]

        # Add --set options if provided
        for key, value in set_values.items():
            command.append("--set")
            command.append(f"{key}={value}")

        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output, error = process.communicate()

        stderr = error.decode("utf-8").strip() if error else ""
        stdout = output.decode("utf-8").strip() if output else ""

        if stderr:
            logger.warning(f"Helm upgrade stderr output:\n{stderr}")

        if process.returncode != 0:
            logger.error("Error during helm upgrade:")
            raise RuntimeError(
                f"Helm upgrade failed for release '{release_name}' in namespace '{namespace}'. "
                f"Chart: {chart_path}. Error output:\n{stderr}\n"
                f"Stdout (for context):\n{stdout}"
            )
        else:
            logger.info("Helm upgrade successful!")
            logger.debug(stdout)

    @staticmethod
    def add_repo(name: str, url: str, max_retries: int = 3, backoff_factor: float = 2.0):
        """Add a Helm repository with retry logic

        Args:
            name (str): Name of the repository
            url (str): URL of the repository
            max_retries (int): Maximum number of retry attempts
            backoff_factor (float): Multiplier for exponential backoff (seconds)

        Raises:
            RuntimeError: If all retry attempts fail
        """
        logger.info(f"Helm Repo Add: {name} with url {url}")
        command = f"helm repo add {name} {url}"

        for attempt in range(max_retries):
            process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            output, error = process.communicate()

            # Check if the repo add was successful (return code 0) or repo already exists
            stdout = output.decode("utf-8").strip() if output else ""
            stderr = error.decode("utf-8").strip() if error else ""

            # Success if returncode is 0 or if "already exists" in output
            if process.returncode == 0 or "already exists" in stderr or "already exists" in stdout:
                logger.info(f"Helm repo {name} added successfully: {stdout}")
                return

            if attempt < max_retries - 1:
                wait_time = backoff_factor**attempt
                logger.warning(
                    f"Helm repo add failed on attempt {attempt + 1}/{max_retries}. "
                    f"Retrying in {wait_time}s... Error: {stderr}"
                )
                time.sleep(wait_time)
            else:
                # Final attempt failed
                logger.error(
                    f"Helm repo add failed after {max_retries} attempts. "
                    f"Error output:\n{stderr}\n"
                    f"Stdout (for context):\n{stdout}"
                )
                raise RuntimeError(
                    f"Helm repo add failed for '{name}' at {url} after {max_retries} attempts. "
                    f"Error output:\n{stderr}\n"
                    f"Stdout (for context):\n{stdout}"
                )

    @staticmethod
    def repo_update(max_retries: int = 3, backoff_factor: float = 2.0):
        """Update Helm repositories with retry logic

        Args:
            max_retries (int): Maximum number of retry attempts
            backoff_factor (float): Multiplier for exponential backoff (seconds)

        Raises:
            RuntimeError: If all retry attempts fail
        """
        logger.info("Helm Repo Update with retry logic")
        command = "helm repo update"

        for attempt in range(max_retries):
            process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            output, error = process.communicate()

            if process.returncode == 0:
                logger.info(f"Helm repo update successful on attempt {attempt + 1}")
                logger.debug(output.decode("utf-8"))
                return

            # Log the error but continue retrying
            stderr = error.decode("utf-8").strip() if error else ""
            stdout = output.decode("utf-8").strip() if output else ""

            if attempt < max_retries - 1:
                wait_time = backoff_factor**attempt
                logger.warning(
                    f"Helm repo update failed on attempt {attempt + 1}/{max_retries}. "
                    f"Retrying in {wait_time}s... Error: {stderr}"
                )
                time.sleep(wait_time)
            else:
                # Final attempt failed
                logger.error(
                    f"Helm repo update failed after {max_retries} attempts. "
                    f"Error output:\n{stderr}\n"
                    f"Stdout (for context):\n{stdout}"
                )
                raise RuntimeError(
                    f"Helm repo update failed after {max_retries} attempts. "
                    f"Error output:\n{stderr}\n"
                    f"Stdout (for context):\n{stdout}"
                )


# Example usage
if __name__ == "__main__":
    sn_configs = {
        "release_name": "social-network",
        "chart_path": "~/DeathStarBench/socialNetwork/helm-chart/socialnetwork",
        "namespace": "social-network",
    }
    Helm.install(**sn_configs)
    Helm.uninstall(**sn_configs)
