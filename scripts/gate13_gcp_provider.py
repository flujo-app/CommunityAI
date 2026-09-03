"""GCP and GitHub adapters for the one-click Gate 13 cloud replay."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from gate13_cloud_orchestrator import Gate13CloudError, PackageArtifact

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_RUN_RE = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")


class CommandError(Gate13CloudError):
    """A bounded local or provider command failed."""


class LoggedRunner:
    """Run argv-only commands and persist a privacy-safe action journal."""

    def __init__(self, journal_path: Path, progress: Callable[[str], None] = print) -> None:
        self.journal_path = journal_path
        self.progress = progress
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)

    def _record(self, value: Mapping[str, Any]) -> None:
        with self.journal_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def run(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        action: str,
        timeout: float = 300,
        stdin: str | None = None,
        check: bool = True,
        sensitive_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = [os.fspath(item) for item in argv]
        executable = shutil.which(command[0])
        if executable is not None:
            command[0] = executable
        started = time.time()
        self.progress(action)
        environment = dict(os.environ)
        environment.update(
            {
                "CLOUDSDK_CORE_DISABLE_PROMPTS": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "PYTHONUTF8": "1",
            }
        )
        try:
            result = subprocess.run(
                command,
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout,
                env=environment,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._record(
                {
                    "action": action,
                    "started_at_unix": int(started),
                    "finished_at_unix": int(time.time()),
                    "result": "failed",
                    "failure_code": type(exc).__name__,
                }
            )
            raise CommandError(f"{action} could not run") from exc
        self._record(
            {
                "action": action,
                "started_at_unix": int(started),
                "finished_at_unix": int(time.time()),
                "duration_seconds": round(time.time() - started, 3),
                "exit_code": result.returncode,
                "result": "passed" if result.returncode == 0 else "failed",
                "output_retained": False,
                "sensitive_output": sensitive_output,
            }
        )
        if check and result.returncode != 0:
            raise CommandError(f"{action} failed with exit code {result.returncode}")
        return result

    def json(self, argv: Sequence[str | os.PathLike[str]], *, action: str, timeout: float = 300) -> Any:
        result = self.run(argv, action=action, timeout=timeout)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CommandError(f"{action} returned invalid JSON") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _strict_object(payload: str, label: str) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise Gate13CloudError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise Gate13CloudError(f"{label} is not an object")
    return value


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ARG002
        return None


class GitHubPackageSource:
    """Resolve or build two exact workflow artifacts from the pushed HEAD."""

    def __init__(
        self,
        *,
        repository_root: Path,
        output_root: Path,
        repository: str,
        workflow: str,
        runner: LoggedRunner,
        progress: Callable[[str], None] = print,
        sleeper: Callable[[float], None] = time.sleep,
        workflow_timeout_seconds: int = 7_200,
    ) -> None:
        self.repository_root = repository_root
        self.output_root = output_root
        self.repository = repository
        self.workflow = workflow
        self.runner = runner
        self.progress = progress
        self.sleeper = sleeper
        self.workflow_timeout_seconds = workflow_timeout_seconds
        self._artifacts_by_name: dict[str, Mapping[str, Any]] = {}

    def _git(self, *arguments: str, action: str) -> str:
        return self.runner.run(
            ["git", "-C", self.repository_root, *arguments], action=action, timeout=120
        ).stdout.strip()

    def _head_and_branch(self) -> tuple[str, str]:
        head = self._git("rev-parse", "HEAD", action="Checking package source commit")
        branch = self._git("symbolic-ref", "--short", "HEAD", action="Checking package source branch")
        if not _COMMIT_RE.fullmatch(head) or not branch:
            raise Gate13CloudError("package source is not a named Git branch")
        remote_url = self._git("remote", "get-url", "origin", action="Checking canonical package source remote").rstrip(
            "/"
        )
        repository = self.repository.removesuffix(".git")
        if remote_url not in {
            f"https://github.com/{repository}",
            f"https://github.com/{repository}.git",
            f"git@github.com:{repository}",
            f"git@github.com:{repository}.git",
            f"ssh://git@github.com/{repository}",
            f"ssh://git@github.com/{repository}.git",
        }:
            raise Gate13CloudError("origin is not the configured canonical GitHub repository")
        remote = self._git(
            "ls-remote", "origin", f"refs/heads/{branch}", action="Checking pushed package source"
        ).split()
        if not remote or remote[0] != head:
            raise Gate13CloudError("current HEAD must be pushed to origin before the one-click run")
        return head, branch

    def _workflow_runs(self, branch: str) -> list[Mapping[str, Any]]:
        value = self.runner.json(
            [
                "gh",
                "api",
                "--method",
                "GET",
                f"repos/{self.repository}/actions/workflows/{self.workflow}/runs",
                "-f",
                f"branch={branch}",
                "-f",
                "event=workflow_dispatch",
                "-f",
                "per_page=20",
            ],
            action="Inspecting production package workflow runs",
        )
        runs = value.get("workflow_runs") if isinstance(value, dict) else None
        if not isinstance(runs, list):
            raise Gate13CloudError("GitHub workflow run listing is invalid")
        return [item for item in runs if isinstance(item, dict)]

    def _artifacts(self, run_id: int) -> dict[str, Mapping[str, Any]]:
        value = self.runner.json(
            ["gh", "api", f"repos/{self.repository}/actions/runs/{run_id}/artifacts", "--paginate"],
            action="Inspecting production package artifacts",
        )
        items = value.get("artifacts") if isinstance(value, dict) else None
        if not isinstance(items, list):
            raise Gate13CloudError("GitHub artifact listing is invalid")
        result = {
            item["name"]: item
            for item in items
            if isinstance(item, dict) and isinstance(item.get("name"), str) and item.get("expired") is False
        }
        return result

    @staticmethod
    def _has_required_artifacts(artifacts: Mapping[str, Any]) -> bool:
        return all(
            f"communityai-desktop-{kind}-{platform}" in artifacts
            for kind in ("install", "audit")
            for platform in ("windows", "linux")
        )

    def _select_or_build_run(self, head: str, branch: str) -> tuple[int, dict[str, Mapping[str, Any]]]:
        prior_runs = self._workflow_runs(branch)
        for run in prior_runs:
            if run.get("head_sha") == head and run.get("status") == "completed" and run.get("conclusion") == "success":
                run_id = run.get("id")
                if isinstance(run_id, int):
                    artifacts = self._artifacts(run_id)
                    if self._has_required_artifacts(artifacts):
                        self.progress(f"Using existing production package run {run_id}")
                        return run_id, artifacts

        prior_run_ids = {item.get("id") for item in prior_runs if isinstance(item.get("id"), int)}
        self.runner.run(
            ["gh", "workflow", "run", self.workflow, "--repo", self.repository, "--ref", branch],
            action="Starting production package workflow",
        )
        deadline = time.monotonic() + self.workflow_timeout_seconds
        matching_run_id: int | None = None
        while time.monotonic() < deadline:
            for run in self._workflow_runs(branch):
                run_id = run.get("id")
                if run.get("head_sha") != head or not isinstance(run_id, int) or run_id in prior_run_ids:
                    continue
                matching_run_id = run_id
                if run.get("status") == "completed":
                    if run.get("conclusion") != "success":
                        raise Gate13CloudError(f"production package workflow {run_id} failed")
                    artifacts = self._artifacts(run_id)
                    if not self._has_required_artifacts(artifacts):
                        raise Gate13CloudError("production package workflow omitted required artifacts")
                    return run_id, artifacts
                self.progress(f"Production package run {run_id} is {run.get('status')}; waiting")
                break
            else:
                self.progress("Waiting for GitHub to create the production package run")
            self.sleeper(30)
        suffix = "" if matching_run_id is None else f" {matching_run_id}"
        raise Gate13CloudError(f"production package workflow{suffix} exceeded its time bound")

    def _download_audit(self, run_id: int, platform: str) -> Path:
        destination = self.output_root / "package-audit" / platform
        destination.mkdir(parents=True, exist_ok=False)
        self.runner.run(
            [
                "gh",
                "run",
                "download",
                str(run_id),
                "--repo",
                self.repository,
                "--name",
                f"communityai-desktop-audit-{platform}",
                "--dir",
                destination,
            ],
            action=f"Downloading {platform} package audit",
            timeout=600,
        )
        return destination

    def prepare(self) -> Mapping[str, PackageArtifact]:
        head, branch = self._head_and_branch()
        self.runner.run(["gh", "auth", "status"], action="Checking GitHub authentication", timeout=60)
        run_id, artifacts = self._select_or_build_run(head, branch)
        self._artifacts_by_name = dict(artifacts)
        result: dict[str, PackageArtifact] = {}
        for platform in ("windows", "linux"):
            audit_root = self._download_audit(run_id, platform)
            provenance_path = audit_root / "provenance.json"
            provenance = _strict_object(provenance_path.read_text(encoding="utf-8"), "package provenance")
            install = provenance.get("install_archive")
            if provenance.get("source_commit") != head or not isinstance(install, dict):
                raise Gate13CloudError("package provenance does not bind the pushed HEAD")
            expected_platform = "Windows" if platform == "windows" else "Linux"
            expected_archive = (
                "communityai-desktop-windows.zip" if platform == "windows" else "communityai-desktop-linux.tar.gz"
            )
            digest = install.get("sha256")
            byte_count = install.get("size_bytes")
            if (
                install.get("platform") != expected_platform
                or install.get("path") != expected_archive
                or not isinstance(digest, str)
                or not _DIGEST_RE.fullmatch(digest)
                or not isinstance(byte_count, int)
                or isinstance(byte_count, bool)
                or byte_count <= 0
            ):
                raise Gate13CloudError("package provenance install archive is invalid")
            artifact_name = f"communityai-desktop-install-{platform}"
            artifact = artifacts[artifact_name]
            artifact_id = artifact.get("id")
            wrapper_digest = artifact.get("digest")
            wrapper_bytes = artifact.get("size_in_bytes")
            if (
                not isinstance(artifact_id, int)
                or not isinstance(wrapper_digest, str)
                or not wrapper_digest.startswith("sha256:")
                or not _DIGEST_RE.fullmatch(wrapper_digest.removeprefix("sha256:"))
                or not isinstance(wrapper_bytes, int)
                or isinstance(wrapper_bytes, bool)
                or wrapper_bytes <= 0
            ):
                raise Gate13CloudError("package artifact wrapper identity is invalid")
            result[platform] = PackageArtifact(
                platform=platform,
                source_commit=head,
                workflow_run_id=run_id,
                artifact_id=artifact_id,
                artifact_name=artifact_name,
                wrapper_sha256=wrapper_digest.removeprefix("sha256:"),
                wrapper_bytes=wrapper_bytes,
                archive_name=expected_archive,
                archive_sha256=digest,
                archive_bytes=byte_count,
            )
        return result

    def signed_download_url(self, artifact: PackageArtifact) -> str:
        expected = self._artifacts_by_name.get(artifact.artifact_name)
        if not isinstance(expected, Mapping) or expected.get("id") != artifact.artifact_id:
            raise Gate13CloudError("package artifact was not prepared by this run")
        token = self.runner.run(
            ["gh", "auth", "token"],
            action=f"Authorizing the {artifact.platform} route relay download",
            timeout=60,
            sensitive_output=True,
        ).stdout.strip()
        if not token or any(character.isspace() for character in token):
            raise Gate13CloudError("GitHub token is unavailable")
        request = urllib.request.Request(
            f"https://api.github.com/repos/{self.repository}/actions/artifacts/{artifact.artifact_id}/zip",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "CommunityAI-Gate13/1",
            },
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RejectRedirects())
        try:
            opener.open(request, timeout=30)
        except urllib.error.HTTPError as exc:
            if exc.code not in {301, 302, 303, 307, 308}:
                raise Gate13CloudError("GitHub artifact authorization failed") from exc
            location = exc.headers.get("Location")
        except (OSError, TimeoutError) as exc:
            raise Gate13CloudError("GitHub artifact authorization failed") from exc
        else:
            raise Gate13CloudError("GitHub artifact endpoint did not return a redirect")
        finally:
            token = ""
        if not isinstance(location, str):
            raise Gate13CloudError("GitHub artifact redirect is absent")
        parsed = urllib.parse.urlsplit(location)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or not host.startswith("productionresults")
            or not host.endswith(".blob.core.windows.net")
        ):
            raise Gate13CloudError("GitHub artifact redirect host is not allowlisted")
        return location


@dataclass(frozen=True)
class GcpConfig:
    project: str
    region: str
    zone: str
    network: str
    subnet: str
    protected_instance: str
    protected_zone: str
    route_machine_type: str
    route_image: str
    route_image_project: str
    windows_machine_type: str
    windows_image: str
    windows_image_project: str
    linux_machine_type: str
    linux_image: str
    linux_image_project: str
    route_source_commit: str
    catalog_source_commit: str
    route_setup_commit: str
    configure_helper_commit: str
    acceptance_helper_commit: str
    windows_startup_commit: str
    linux_startup_commit: str
    route_wheel_path: str
    route_wheel_sha256: str
    route_wheel_bytes: int

    @classmethod
    def load(cls, path: Path) -> "GcpConfig":
        value = _strict_object(path.read_text(encoding="utf-8"), "GCP one-click configuration")
        expected = set(cls.__dataclass_fields__)
        string_fields = expected - {"route_wheel_bytes"}
        if (
            set(value) != expected
            or not all(isinstance(value[field], str) and value[field] for field in string_fields)
            or not isinstance(value["route_wheel_bytes"], int)
            or isinstance(value["route_wheel_bytes"], bool)
            or value["route_wheel_bytes"] <= 0
        ):
            raise Gate13CloudError("GCP one-click configuration fields are invalid")
        result = cls(**value)
        for commit in (
            result.route_source_commit,
            result.catalog_source_commit,
            result.route_setup_commit,
            result.configure_helper_commit,
            result.acceptance_helper_commit,
            result.windows_startup_commit,
            result.linux_startup_commit,
        ):
            if not _COMMIT_RE.fullmatch(commit):
                raise Gate13CloudError("GCP one-click configuration commit is invalid")
        if not _DIGEST_RE.fullmatch(result.route_wheel_sha256):
            raise Gate13CloudError("GCP route wheel digest is invalid")
        return result


class GcpProvider:
    """Exact GCP resource adapter; no Gate 13 sequencing lives here."""

    name = "gcp"

    def __init__(
        self,
        *,
        run_id: str,
        repository_root: Path,
        output_root: Path,
        config: GcpConfig,
        runner: LoggedRunner,
        signed_url: Callable[[PackageArtifact], str],
        progress: Callable[[str], None] = print,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not _RUN_RE.fullmatch(run_id):
            raise Gate13CloudError("GCP run ID is invalid")
        self.run_id = run_id
        self.repository_root = repository_root
        self.output_root = output_root
        self.config = config
        self.runner = runner
        self.signed_url = signed_url
        self.progress = progress
        self.sleeper = sleeper
        self.route = f"{run_id}-route"
        self.clients = {
            platform: f"{run_id}-{'win' if platform == 'windows' else 'linux'}" for platform in ("windows", "linux")
        }
        self.dht_firewall = f"{run_id}-dht"
        self.iap_firewall = f"{run_id}-iap"
        self.relay_firewall = f"{run_id}-relay"
        self.client_tag = f"{run_id}-client"
        for name in (
            self.route,
            *self.clients.values(),
            self.dht_firewall,
            self.iap_firewall,
            self.relay_firewall,
            self.client_tag,
        ):
            if not _RUN_RE.fullmatch(name):
                raise Gate13CloudError("derived GCP resource name is invalid")

    def _gcloud(
        self, *arguments: str, action: str, timeout: float = 300, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return self.runner.run(["gcloud", *arguments], action=action, timeout=timeout, check=check, stdin="n\n")

    def _gcloud_json(self, *arguments: str, action: str, timeout: float = 300) -> Any:
        result = self._gcloud(*arguments, "--format=json", action=action, timeout=timeout)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise Gate13CloudError(f"{action} returned invalid JSON") from exc

    def _ssh(
        self,
        instance: str,
        command: str,
        *,
        action: str,
        user: str | None = None,
        timeout: float = 300,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        target = instance if user is None else f"{user}@{instance}"
        return self._gcloud(
            "compute",
            "ssh",
            target,
            "--project",
            self.config.project,
            "--zone",
            self.config.zone,
            "--tunnel-through-iap",
            "--quiet",
            "--command",
            command,
            action=action,
            timeout=timeout,
            check=check,
        )

    def _scp(
        self,
        sources: Sequence[str | Path],
        destination: str,
        *,
        action: str,
        timeout: float = 600,
    ) -> None:
        self._gcloud(
            "compute",
            "scp",
            *[os.fspath(item) for item in sources],
            destination,
            "--project",
            self.config.project,
            "--zone",
            self.config.zone,
            "--tunnel-through-iap",
            "--quiet",
            action=action,
            timeout=timeout,
        )

    def _describe_instance(self, name: str, *, check: bool = True) -> Mapping[str, Any] | None:
        result = self._gcloud(
            "compute",
            "instances",
            "describe",
            name,
            "--project",
            self.config.project,
            "--zone",
            self.config.zone,
            "--format=json",
            action=f"Inspecting instance {name}",
            timeout=60,
            check=check,
        )
        if result.returncode != 0:
            return None
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise Gate13CloudError("GCP instance description is invalid") from exc
        if not isinstance(value, dict):
            raise Gate13CloudError("GCP instance description is invalid")
        return value

    @staticmethod
    def _basename(value: Any) -> str:
        return value.rsplit("/", 1)[-1] if isinstance(value, str) else ""

    def _assert_owned_instance(self, name: str, value: Mapping[str, Any]) -> None:
        labels = value.get("labels")
        disks = value.get("disks")
        if (
            value.get("name") != name
            or not isinstance(labels, dict)
            or labels.get("communityai_run") != self.run_id
            or value.get("deletionProtection") is True
            or not isinstance(disks, list)
            or len(disks) != 1
            or not isinstance(disks[0], dict)
            or disks[0].get("autoDelete") is not True
            or self._basename(disks[0].get("source")) != name
        ):
            raise Gate13CloudError(f"refusing to mutate unbound instance {name}")

    def _wait_ssh(
        self,
        instance: str,
        command: str,
        *,
        action: str,
        user: str | None = None,
        timeout_seconds: int = 1_800,
    ) -> str:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            result = self._ssh(
                instance,
                command,
                action=action,
                user=user,
                timeout=90,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            self.sleeper(15)
        raise Gate13CloudError(f"{action} exceeded its time bound")

    def _ensure_ssh_key(self) -> Path:
        root = Path.home() / ".ssh"
        private = root / "google_compute_engine"
        public = private.with_suffix(".pub")
        root.mkdir(mode=0o700, exist_ok=True)
        if public.is_file() and 32 <= public.stat().st_size <= 16_384:
            return public
        if private.is_file():
            result = self.runner.run(
                ["ssh-keygen", "-y", "-f", private], action="Deriving the GCP SSH public key", timeout=60
            )
            public.write_text(result.stdout.strip() + "\n", encoding="ascii", newline="\n")
        else:
            self.runner.run(
                ["ssh-keygen", "-t", "rsa", "-b", "3072", "-N", "", "-f", private],
                action="Creating the GCP SSH key",
                timeout=120,
            )
        if not public.is_file():
            raise Gate13CloudError("GCP SSH public key is unavailable")
        return public

    def _resource_absence(self) -> tuple[list[str], list[str], list[str]]:
        targets = {self.route, *self.clients.values()}
        firewall_targets = {
            self.dht_firewall,
            self.iap_firewall,
            self.relay_firewall,
        }
        instance_inventory = self._gcloud_json(
            "compute",
            "instances",
            "list",
            "--project",
            self.config.project,
            action="Inventorying run-scoped GCP instances",
            timeout=120,
        )
        disk_inventory = self._gcloud_json(
            "compute",
            "disks",
            "list",
            "--project",
            self.config.project,
            action="Inventorying run-scoped GCP disks",
            timeout=120,
        )
        firewall_inventory = self._gcloud_json(
            "compute",
            "firewall-rules",
            "list",
            "--project",
            self.config.project,
            action="Inventorying run-scoped GCP firewalls",
            timeout=120,
        )
        if not all(isinstance(value, list) for value in (instance_inventory, disk_inventory, firewall_inventory)):
            raise Gate13CloudError("GCP resource inventory is invalid")
        instances = sorted(
            item["name"] for item in instance_inventory if isinstance(item, dict) and item.get("name") in targets
        )
        disks = sorted(
            item["name"] for item in disk_inventory if isinstance(item, dict) and item.get("name") in targets
        )
        firewalls = sorted(
            item["name"]
            for item in firewall_inventory
            if isinstance(item, dict) and item.get("name") in firewall_targets
        )
        return instances, disks, firewalls

    def _route_wheel(self) -> Path:
        wheel = (self.repository_root / self.config.route_wheel_path).resolve()
        if (
            not wheel.is_file()
            or wheel.stat().st_size != self.config.route_wheel_bytes
            or _sha256(wheel) != self.config.route_wheel_sha256
        ):
            raise Gate13CloudError("the exact successful-run route wheel is absent or changed")
        return wheel

    def _validate_immutable_sources(self) -> None:
        objects = (
            (self.config.route_source_commit, None, "route runtime commit"),
            (
                self.config.catalog_source_commit,
                "public-alpha/catalog-v1",
                "signed route catalog",
            ),
            (
                self.config.route_setup_commit,
                "scripts/gate13_route_setup.sh",
                "route setup",
            ),
            (
                self.config.configure_helper_commit,
                "scripts/configure_product_route_node.py",
                "route configuration helper",
            ),
            (
                self.config.acceptance_helper_commit,
                "scripts/gate11_product_node_acceptance.py",
                "route acceptance helper",
            ),
            (
                self.config.windows_startup_commit,
                "scripts/gate13_windows_client_startup.ps1",
                "Windows startup",
            ),
            (
                self.config.linux_startup_commit,
                "scripts/gate13_linux_client_startup.sh",
                "Linux startup",
            ),
        )
        for commit, path, label in objects:
            object_name = f"{commit}:" + path if path is not None else f"{commit}^{{commit}}"
            self.runner.run(
                ["git", "-C", self.repository_root, "cat-file", "-e", object_name],
                action=f"Checking immutable {label}",
                timeout=120,
            )
        self._route_wheel()

    def preflight(self) -> Mapping[str, Any]:
        self._validate_immutable_sources()
        self.runner.run(["gcloud", "--version"], action="Checking the gcloud CLI", timeout=60)
        accounts = self._gcloud_json(
            "auth", "list", "--filter=status:ACTIVE", action="Checking GCP authentication", timeout=60
        )
        if not isinstance(accounts, list) or len(accounts) != 1:
            raise Gate13CloudError("exactly one active gcloud account is required")
        credential = self.runner.run(
            ["gcloud", "auth", "print-access-token"],
            action="Checking reusable GCP credentials",
            timeout=60,
            sensitive_output=True,
        ).stdout.strip()
        if not credential or any(character.isspace() for character in credential):
            raise Gate13CloudError("GCP access token is unavailable")
        credential = ""
        self._gcloud_json(
            "compute",
            "networks",
            "describe",
            self.config.network,
            "--project",
            self.config.project,
            action="Checking the GCP network",
            timeout=60,
        )
        self._gcloud_json(
            "compute",
            "networks",
            "subnets",
            "describe",
            self.config.subnet,
            "--project",
            self.config.project,
            "--region",
            self.config.region,
            action="Checking the GCP subnet",
            timeout=60,
        )
        for machine_type in {
            self.config.route_machine_type,
            self.config.windows_machine_type,
            self.config.linux_machine_type,
        }:
            self._gcloud_json(
                "compute",
                "machine-types",
                "describe",
                machine_type,
                "--project",
                self.config.project,
                "--zone",
                self.config.zone,
                action=f"Checking GCP machine type {machine_type}",
                timeout=60,
            )
        for image, project in (
            (self.config.route_image, self.config.route_image_project),
            (self.config.windows_image, self.config.windows_image_project),
            (self.config.linux_image, self.config.linux_image_project),
        ):
            self._gcloud_json(
                "compute",
                "images",
                "describe",
                image,
                "--project",
                project,
                action=f"Checking GCP image {image}",
                timeout=60,
            )
        bootstrap = self._gcloud_json(
            "compute",
            "instances",
            "describe",
            self.config.protected_instance,
            "--project",
            self.config.project,
            "--zone",
            self.config.protected_zone,
            action="Checking the protected bootstrap",
            timeout=60,
        )
        if not isinstance(bootstrap, dict) or bootstrap.get("status") != "RUNNING":
            raise Gate13CloudError("protected bootstrap is not running")
        region = self._gcloud_json(
            "compute",
            "regions",
            "describe",
            self.config.region,
            "--project",
            self.config.project,
            action="Checking GCP L4 quota",
            timeout=60,
        )
        quota = region.get("quotas") if isinstance(region, dict) else None
        l4 = next(
            (item for item in quota or [] if isinstance(item, dict) and item.get("metric") == "NVIDIA_L4_GPUS"), None
        )
        if not isinstance(l4, dict) or float(l4.get("limit", 0)) - float(l4.get("usage", 0)) < 1:
            raise Gate13CloudError("one free regional NVIDIA L4 is required")
        self._ensure_ssh_key()
        instances, disks, firewalls = self._resource_absence()
        if instances or disks or firewalls:
            raise Gate13CloudError("run-scoped GCP targets already exist")
        return {
            "result": "passed",
            "project": self.config.project,
            "zone": self.config.zone,
            "one_l4_free": True,
            "targets_absent": True,
            "protected_bootstrap_running": True,
        }

    def create_route(self) -> None:
        labels = f"communityai_run={self.run_id},communityai_scope=gate13_one_click"
        self._gcloud(
            "compute",
            "firewall-rules",
            "create",
            self.dht_firewall,
            "--project",
            self.config.project,
            "--network",
            self.config.network,
            "--direction",
            "INGRESS",
            "--action",
            "ALLOW",
            "--rules",
            "tcp:31337-31338",
            "--source-ranges",
            "0.0.0.0/0",
            "--target-tags",
            self.route,
            action="Creating the run-scoped route firewall",
            timeout=180,
        )
        self._gcloud(
            "compute",
            "firewall-rules",
            "create",
            self.iap_firewall,
            "--project",
            self.config.project,
            "--network",
            self.config.network,
            "--direction",
            "INGRESS",
            "--action",
            "ALLOW",
            "--rules",
            "tcp:22",
            "--source-ranges",
            "35.235.240.0/20",
            "--target-tags",
            f"{self.route},{self.client_tag}",
            action="Creating the run-scoped IAP firewall",
            timeout=180,
        )
        self._gcloud(
            "compute",
            "firewall-rules",
            "create",
            self.relay_firewall,
            "--project",
            self.config.project,
            "--network",
            self.config.network,
            "--direction",
            "INGRESS",
            "--priority",
            "1000",
            "--action",
            "ALLOW",
            "--rules",
            "tcp:38081",
            "--source-tags",
            self.client_tag,
            "--target-tags",
            self.route,
            action="Creating the proven private package-relay firewall",
            timeout=180,
        )
        self._gcloud(
            "compute",
            "instances",
            "create",
            self.route,
            "--project",
            self.config.project,
            "--zone",
            self.config.zone,
            "--machine-type",
            self.config.route_machine_type,
            "--network",
            self.config.network,
            "--subnet",
            self.config.subnet,
            "--maintenance-policy",
            "TERMINATE",
            "--provisioning-model",
            "STANDARD",
            "--no-service-account",
            "--no-scopes",
            "--image",
            self.config.route_image,
            "--image-project",
            self.config.route_image_project,
            "--boot-disk-type",
            "pd-balanced",
            "--boot-disk-size",
            "200GB",
            "--boot-disk-device-name",
            self.route,
            "--boot-disk-auto-delete",
            "--tags",
            self.route,
            "--labels",
            labels,
            "--max-run-duration",
            "57600s",
            "--instance-termination-action",
            "DELETE",
            action="Creating the GCP route VM",
            timeout=900,
        )
        value = self._describe_instance(self.route)
        if value is None:
            raise Gate13CloudError("route VM disappeared after creation")
        self._assert_owned_instance(self.route, value)

    def _git_blob(self, commit: str, path: str, destination: Path) -> None:
        result = self.runner.run(
            ["git", "-C", self.repository_root, "show", f"{commit}:{path}"],
            action=f"Extracting immutable route helper {Path(path).name}",
            timeout=120,
        )
        destination.write_text(result.stdout, encoding="utf-8", newline="\n")

    def _client_startup_script(self, platform: str) -> Path:
        if platform == "windows":
            name = "gate13_windows_client_startup.ps1"
            commit = self.config.windows_startup_commit
        elif platform == "linux":
            name = "gate13_linux_client_startup.sh"
            commit = self.config.linux_startup_commit
        else:
            raise Gate13CloudError("client platform is invalid")
        root = self.output_root / "client-startup"
        root.mkdir(exist_ok=True)
        destination = root / name
        self._git_blob(commit, f"scripts/{name}", destination)
        return destination

    def _build_route_bundle(self) -> Path:
        bundle = self.output_root / "route-bundle"
        bundle.mkdir(parents=True, exist_ok=False)
        shutil.copy2(
            self._route_wheel(),
            bundle / "drift-2.3.0.dev2-py3-none-any.whl",
        )
        catalog = bundle / "catalog-v1.tar"
        self.runner.run(
            [
                "git",
                "-C",
                self.repository_root,
                "archive",
                "--format=tar",
                f"--output={catalog}",
                self.config.catalog_source_commit,
                "public-alpha/catalog-v1",
            ],
            action="Archiving the exact successful signed catalog",
            timeout=180,
        )
        self._git_blob(
            self.config.configure_helper_commit,
            "scripts/configure_product_route_node.py",
            bundle / "configure_product_route_node.py",
        )
        self._git_blob(
            self.config.acceptance_helper_commit,
            "scripts/gate11_product_node_acceptance.py",
            bundle / "gate11_product_node_acceptance.py",
        )
        self._git_blob(
            self.config.route_setup_commit,
            "scripts/gate13_route_setup.sh",
            bundle / "gate13_route_setup.sh",
        )
        return bundle

    def prepare_route(self) -> Mapping[str, Any]:
        value = self._describe_instance(self.route)
        if value is None:
            raise Gate13CloudError("route VM is absent")
        interfaces = value.get("networkInterfaces")
        access = interfaces[0].get("accessConfigs") if isinstance(interfaces, list) and interfaces else None
        public_ip = access[0].get("natIP") if isinstance(access, list) and access else None
        if not isinstance(public_ip, str) or not public_ip:
            raise Gate13CloudError("route VM has no public address")
        bundle = self._build_route_bundle()
        self._wait_ssh(
            self.route,
            "test -f /etc/os-release && "
            "test \"$(cut -d. -f1 /proc/uptime)\" -ge 300 && "
            "(! command -v fuser >/dev/null || "
            "(! sudo fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 && "
            "! sudo fuser /var/lib/dpkg/lock >/dev/null 2>&1 && "
            "! sudo fuser /var/lib/apt/lists/lock >/dev/null 2>&1))",
            action="Waiting for the new route machine to finish starting",
            timeout_seconds=1_800,
        )
        files = [path for path in bundle.iterdir() if path.is_file()]
        self._ssh(
            self.route,
            "rm -rf -- /tmp/gate13-route && install -d -m 0700 /tmp/gate13-route",
            action="Preparing the route staging directory",
        )
        self._scp(
            files,
            f"{self.route}:/tmp/gate13-route/",
            action="Staging the immutable route bundle",
            timeout=1_800,
        )
        setup = self._ssh(
            self.route,
            "install -d -m 0700 /tmp/gate13-route/catalog-v1 && "
            "tar -xf /tmp/gate13-route/catalog-v1.tar -C /tmp/gate13-route/catalog-v1 "
            "--strip-components=2 && sudo bash /tmp/gate13-route/gate13_route_setup.sh",
            action="Installing and starting the route services",
            timeout=7_200,
            check=False,
        )
        if setup.returncode != 0:
            combined = "\n".join(part for part in (setup.stdout, setup.stderr) if part)
            tail = "\n".join(combined.splitlines()[-30:])[-4_000:]
            tail = re.sub(r"https?://\S+", "<redacted-url>", tail)
            if tail:
                self.progress("Route setup failed; bounded redacted tail:\n" + tail)
            raise CommandError(
                "Installing and starting the route services failed with exit code "
                f"{setup.returncode}"
            )
        fence = self.repository_root / "scripts" / "gate13_route_fence.py"
        self._scp(
            [fence],
            f"{self.route}:/tmp/gate13_route_fence.py",
            action="Staging the exact final route fence",
            timeout=300,
        )
        fence_digest = _sha256(fence)
        self._ssh(
            self.route,
            "test \"$(sha256sum /tmp/gate13_route_fence.py | cut -d' ' -f1)\" " f'= "{fence_digest}"',
            action="Verifying the exact final route fence",
            timeout=120,
        )
        return {
            "result": "passed",
            "route_source_commit": self.config.route_source_commit,
            "public_address_present": True,
            "immutable_bundle_staged": True,
        }

    def fence_route(self, platform: str) -> Mapping[str, Any]:
        result = self._ssh(
            self.route,
            f"sudo /opt/communityai/venv/bin/python "
            f"/tmp/gate13_route_fence.py --target {platform} "
            "--timeout-seconds 900 --settle-seconds 30",
            action=f"Fencing the route for {platform}",
            timeout=1_200,
        )
        value = _strict_object(result.stdout, f"{platform} route fence")
        if value.get("result") != "passed" or value.get("target") != platform:
            raise Gate13CloudError(f"{platform} route fence rejected")
        return value

    def _route_private_ip(self) -> str:
        value = self._describe_instance(self.route)
        interfaces = value.get("networkInterfaces") if isinstance(value, Mapping) else None
        private_ip = (
            interfaces[0].get("networkIP")
            if isinstance(interfaces, list) and len(interfaces) == 1 and isinstance(interfaces[0], dict)
            else None
        )
        try:
            parsed = ipaddress.IPv4Address(private_ip)
        except (ipaddress.AddressValueError, TypeError) as exc:
            raise Gate13CloudError("route VM has no valid private IPv4 address") from exc
        return str(parsed)

    def _relay_root(self, platform: str) -> str:
        if platform not in self.clients:
            raise Gate13CloudError("client platform is invalid")
        return f"/tmp/{self.run_id}-{platform}-relay"

    def _relay_unit(self, platform: str) -> str:
        if platform not in self.clients:
            raise Gate13CloudError("client platform is invalid")
        return f"{self.run_id}-{platform}-relay"

    def _relay_download_script(self, platform: str, package: PackageArtifact) -> Path:
        stage = self.output_root / "route-relay" / platform
        stage.mkdir(parents=True, exist_ok=False)
        path = stage / f"route-download-{platform}.sh"
        root = self._relay_root(platform)
        content = f"""#!/usr/bin/env bash
set -euo pipefail
umask 077

root={root}
wrapper="$root/artifact-wrapper.zip"
archive="$root/{package.archive_name}"
install -d -m 0700 "$root"

url="$(curl -fsS -H 'Metadata-Flavor: Google' \\
  http://metadata.google.internal/computeMetadata/v1/instance/attributes/artifact-probe-url)"
curl -fL --retry 4 --retry-delay 3 --silent --show-error "$url" -o "$wrapper"
url=
test "$(stat -c %s "$wrapper")" = {package.wrapper_bytes}
test "$(sha256sum "$wrapper" | cut -d' ' -f1)" = {package.wrapper_sha256}

/opt/communityai/venv/bin/python - "$wrapper" "$archive" <<'PY'
import pathlib
import shutil
import sys
import zipfile

wrapper = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
expected = {package.archive_name!r}
with zipfile.ZipFile(wrapper) as bundle:
    members = bundle.namelist()
    if members != [expected]:
        raise SystemExit("artifact wrapper inventory changed")
    with bundle.open(members[0]) as source, target.open("xb") as output:
        shutil.copyfileobj(source, output, length=1024 * 1024)
PY

test "$(stat -c %s "$archive")" = {package.archive_bytes}
test "$(sha256sum "$archive" | cut -d' ' -f1)" = {package.archive_sha256}
printf '%s\\n' '{{"result":"passed","scope":"gate13-{platform}-artifact-relay","sha256":"{package.archive_sha256}","bytes":{package.archive_bytes}}}'
"""
        path.write_text(content, encoding="utf-8", newline="\n")
        return path

    def _prepare_route_relay(self, platform: str, package: PackageArtifact) -> str:
        script = self._relay_download_script(platform, package)
        remote_script = f"/tmp/{self.run_id}-route-download-{platform}.sh"
        private_ip = self._route_private_ip()
        url_path = self.output_root / f".{platform}-artifact-url"
        with url_path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(self.signed_url(package) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        metadata_added = False
        try:
            self._gcloud(
                "compute",
                "instances",
                "add-metadata",
                self.route,
                "--project",
                self.config.project,
                "--zone",
                self.config.zone,
                "--metadata-from-file",
                f"artifact-probe-url={url_path}",
                "--quiet",
                action=f"Authorizing the proven {platform} route-relay download",
                timeout=180,
            )
            metadata_added = True
            self._scp(
                [script],
                f"{self.route}:{remote_script}",
                action=f"Staging the proven {platform} route-relay download",
                timeout=300,
            )
            downloaded = self._ssh(
                self.route,
                f"bash {remote_script}",
                action=f"Downloading and verifying {platform} on the route relay",
                timeout=1_800,
            )
        finally:
            url_path.unlink(missing_ok=True)
            if metadata_added:
                self._gcloud(
                    "compute",
                    "instances",
                    "remove-metadata",
                    self.route,
                    "--project",
                    self.config.project,
                    "--zone",
                    self.config.zone,
                    "--keys",
                    "artifact-probe-url",
                    "--quiet",
                    action=f"Removing the {platform} signed URL from route metadata",
                    timeout=180,
                )
        result = _strict_object(downloaded.stdout, f"{platform} route-relay download")
        if (
            result.get("result") != "passed"
            or result.get("sha256") != package.archive_sha256
            or result.get("bytes") != package.archive_bytes
        ):
            raise Gate13CloudError(f"{platform} route-relay verification rejected")
        unit = self._relay_unit(platform)
        root = self._relay_root(platform)
        self._ssh(
            self.route,
            f"sudo systemd-run --unit={unit} --property=RuntimeMaxSec=3600 "
            f"/opt/communityai/venv/bin/python -m http.server 38081 "
            f"--bind {private_ip} --directory {root} && sleep 1 && "
            f"systemctl is-active {unit}",
            action=f"Starting the proven private {platform} package relay",
            timeout=180,
        )
        return f"http://{private_ip}:38081/artifact-wrapper.zip"

    def _cleanup_route_relay(self, platform: str) -> None:
        root = self._relay_root(platform)
        unit = self._relay_unit(platform)
        remote_script = f"/tmp/{self.run_id}-route-download-{platform}.sh"
        self._ssh(
            self.route,
            "set -euo pipefail; "
            f'root={root}; test "$(realpath -e "$root")" = "$root"; '
            'test ! -L "$root"; '
            f"sudo systemctl stop {unit}; "
            'rm -rf -- "$root"; '
            f"rm -f -- {remote_script}; "
            'test ! -e "$root"',
            action=f"Removing the proven private {platform} package relay",
            timeout=300,
        )

    def create_client(self, platform: str, package: PackageArtifact) -> None:
        if platform not in self.clients:
            raise Gate13CloudError("client platform is invalid")
        name = self.clients[platform]
        startup = self._client_startup_script(platform)
        package_url = self._prepare_route_relay(platform, package)
        public_key = self._ensure_ssh_key()
        labels = f"communityai_run={self.run_id},communityai_scope=gate13_one_click"
        metadata = (
            f"package-url={package_url},"
            f"package-sha256={package.archive_sha256},"
            f"package-bytes={package.archive_bytes}"
        )
        metadata_files = [f"gate13-ssh-public-key={public_key}"]
        if platform == "windows":
            machine_type = self.config.windows_machine_type
            image = self.config.windows_image
            image_project = self.config.windows_image_project
            metadata_files.append(f"windows-startup-script-ps1={startup}")
            disk_size = "120GB"
        else:
            machine_type = self.config.linux_machine_type
            image = self.config.linux_image
            image_project = self.config.linux_image_project
            metadata_files.append(f"startup-script={startup}")
            disk_size = "120GB"
        self._gcloud(
            "compute",
            "instances",
            "create",
            name,
            "--project",
            self.config.project,
            "--zone",
            self.config.zone,
            "--machine-type",
            machine_type,
            "--network",
            self.config.network,
            "--subnet",
            self.config.subnet,
            "--network-tier",
            "PREMIUM",
            "--maintenance-policy",
            "MIGRATE",
            "--provisioning-model",
            "STANDARD",
            "--no-service-account",
            "--no-scopes",
            "--image",
            image,
            "--image-project",
            image_project,
            "--boot-disk-type",
            "pd-balanced",
            "--boot-disk-size",
            disk_size,
            "--boot-disk-device-name",
            name,
            "--boot-disk-auto-delete",
            "--tags",
            self.client_tag,
            "--labels",
            labels,
            "--metadata",
            metadata,
            "--metadata-from-file",
            ",".join(metadata_files),
            "--max-run-duration",
            "21600s",
            "--instance-termination-action",
            "DELETE",
            *(("--enable-display-device",) if platform == "windows" else ()),
            action=f"Creating the clean {platform} client VM",
            timeout=1_200,
        )
        value = self._describe_instance(name)
        if value is None:
            raise Gate13CloudError(f"{platform} client disappeared after creation")
        self._assert_owned_instance(name, value)

    @staticmethod
    def _policy(model_id: str) -> Mapping[str, Any]:
        return {
            "sharing_enabled": True,
            "allowed_models": [model_id],
            "preferred_models": [model_id],
            "denied_models": [],
            "max_disk_space": "32GB",
            "max_vram": "20GB",
            "max_bandwidth_mbps": 100.0,
            "max_power_watts": None,
            "pause_timeout": 120.0,
            "schedule": {
                "timezone": "UTC",
                "windows": [
                    {
                        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                        "start": "00:00",
                        "end": "23:59",
                    }
                ],
            },
        }

    def _lifecycle_config(self, platform: str, package: PackageArtifact) -> dict[str, Any]:
        lifecycle_run_id = f"{self.run_id}-{platform}"
        if platform == "windows":
            archive = r"C:\Gate13Run\package\communityai-desktop-windows.zip"
            executable = r"C:\Gate13Run\install\CommunityAI\CommunityAI.exe"
            work_root = rf"C:\Gate13Run\.gate13-playthrough-{lifecycle_run_id}"
            model_id = "Qwen3.5 2B"
            manifest = "sha256:3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33"
            blocks = 24
        else:
            archive = "/qualification/package/communityai-desktop-linux.tar.gz"
            executable = "/qualification/install/CommunityAI/CommunityAI"
            work_root = f"/qualification/.gate13-playthrough-{lifecycle_run_id}"
            model_id = "Gemma 4 E2B IT"
            manifest = "sha256:2f8debbe0fcdf5af8d4c56c982210fa50aa584314968ae2617e2ccc2de9eafdd"
            blocks = 35
        return {
            "schema_version": 2,
            "run_id": lifecycle_run_id,
            "platform": platform,
            "source_commit": package.source_commit,
            "package_archive": archive,
            "package_sha256": "sha256:" + package.archive_sha256,
            "package_bytes": package.archive_bytes,
            "desktop_executable": executable,
            "work_root": work_root,
            "model_id": model_id,
            "manifest_digest": manifest,
            "total_blocks": blocks,
            "policy": self._policy(model_id),
            "session_timeout_seconds": 3_600,
            "inference_timeout_seconds": 600,
        }

    def _host_config(self, platform: str, package: PackageArtifact, lifecycle_sha256: str) -> dict[str, Any]:
        if platform == "windows":
            root = r"C:\Gate13Run"
            separator = "\\"
            host_user = "M"
            python = r"C:\Gate13Python\python.exe"
        else:
            root = "/qualification"
            separator = "/"
            host_user = "gate13"
            python = "/usr/bin/python3"

        def remote(name: str) -> str:
            return root + separator + name

        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "lifecycle_run_id": f"{self.run_id}-{platform}",
            "platform": platform,
            "attempt_ordinal": 1,
            "source_commit": package.source_commit,
            "job_name": f"communityai-gate13-{self.run_id}-{platform}",
            "host_user": host_user,
            "adapter_path": remote("gate13_host_job.py"),
            "adapter_sha256": "sha256:" + _sha256(self.repository_root / "scripts" / "gate13_host_job.py"),
            "config_path": remote("host-job.json"),
            "entrypoint_path": remote("gate13_automated_playthrough.py"),
            "entrypoint_sha256": "sha256:"
            + _sha256(self.repository_root / "scripts" / "gate13_automated_playthrough.py"),
            "lifecycle_config_path": remote(f"gate13-{platform}-run.json"),
            "lifecycle_config_sha256": "sha256:" + lifecycle_sha256,
            "evidence_path": remote("evidence.json"),
            "stderr_path": remote("stderr.log"),
            "status_path": remote("status.json"),
            "terminal_path": remote("terminal.json"),
            "working_directory": root,
            "python_executable": python,
            "max_run_seconds": 14_400,
        }

    @staticmethod
    def _write_json(path: Path, value: Mapping[str, Any]) -> None:
        payload = json.dumps(value, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        path.write_text(payload, encoding="utf-8", newline="\n")

    def _build_client_stage(self, platform: str, package: PackageArtifact) -> tuple[Path, Path]:
        stage = self.output_root / f"{platform}-stage"
        stage.mkdir(parents=True, exist_ok=False)
        lifecycle_path = stage / f"gate13-{platform}-run.json"
        self._write_json(lifecycle_path, self._lifecycle_config(platform, package))
        host_path = stage / "host-job.json"
        self._write_json(host_path, self._host_config(platform, package, _sha256(lifecycle_path)))
        scripts = (
            "gate13_host_job.py",
            "gate13_automated_playthrough.py",
            "gate13_packaged_lifecycle.py",
        )
        for name in scripts:
            shutil.copy2(self.repository_root / "scripts" / name, stage / name)
        if platform == "windows":
            stage_script = self._windows_stage_script(stage, scripts, lifecycle_path, host_path)
        else:
            stage_script = self._linux_stage_script(stage, scripts, lifecycle_path, host_path, package)
        return stage, stage_script

    def _windows_stage_script(
        self,
        stage: Path,
        scripts: Sequence[str],
        lifecycle_path: Path,
        host_path: Path,
    ) -> Path:
        expected = {name: _sha256(stage / name) for name in (*scripts, lifecycle_path.name, host_path.name)}
        entries = "\n".join(f'    "{name}" = "{digest}"' for name, digest in expected.items())
        content = f"""$ErrorActionPreference = "Stop"
$root = "C:\\Gate13Run"
$expected = @{{
{entries}
}}
$actual = @{{}}
foreach ($name in $expected.Keys) {{
    $digest = (Get-FileHash -LiteralPath "$root\\$name" -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($digest -cne $expected[$name]) {{ throw "digest mismatch for $name" }}
    $actual[$name] = $digest
}}
$hostConfig = Get-Content -LiteralPath "$root\\host-job.json" -Raw | ConvertFrom-Json
if ($hostConfig.lifecycle_config_sha256 -cne "sha256:$($expected['{lifecycle_path.name}'])") {{
    throw "host config does not bind the staged lifecycle config"
}}
$explorer = @(Get-Process explorer -IncludeUserName -ErrorAction SilentlyContinue |
    Where-Object {{ $_.UserName -like "*\\M" }})
if ($explorer.Count -ne 1 -or $explorer[0].SessionId -lt 1) {{
    throw "ordinary M interactive session is not ready"
}}
[pscustomobject]@{{
    result = "passed"
    ready = $true
    hashes = $actual
    host_user = $explorer[0].UserName
    session_id = $explorer[0].SessionId
}} | ConvertTo-Json -Depth 5 -Compress
"""
        path = stage / "stage.ps1"
        path.write_text(content, encoding="utf-8-sig", newline="\r\n")
        return path

    def _linux_stage_script(
        self,
        stage: Path,
        scripts: Sequence[str],
        lifecycle_path: Path,
        host_path: Path,
        package: PackageArtifact,
    ) -> Path:
        expected = {name: _sha256(stage / name) for name in (*scripts, lifecycle_path.name, host_path.name)}
        installs = "\n".join(
            f"install -o gate13 -g gate13 -m 0700 /tmp/{name} /qualification/{name}" for name in scripts
        )
        checks = "\n".join(
            f'test "$(sha256sum /qualification/{name} | cut -d\' \' -f1)" = "{digest}"'
            for name, digest in expected.items()
        )
        content = f"""#!/usr/bin/env bash
set -euo pipefail
umask 077
{installs}
install -o gate13 -g gate13 -m 0600 /tmp/{lifecycle_path.name} /qualification/{lifecycle_path.name}
install -o gate13 -g gate13 -m 0600 /tmp/{host_path.name} /qualification/{host_path.name}
{checks}
test "$(sha256sum /qualification/package/{package.archive_name} | cut -d' ' -f1)" = "{package.archive_sha256}"
test "$(stat -c %s /qualification/package/{package.archive_name})" = "{package.archive_bytes}"
test -x /qualification/install/CommunityAI/CommunityAI
sudo -u gate13 env DISPLAY=:99 xdpyinfo >/dev/null
printf '%s\\n' '{{"result":"passed","ready":true,"host_user":"gate13","display":":99,"hashes_verified":true}}'
"""
        path = stage / "stage.sh"
        path.write_text(content, encoding="utf-8", newline="\n")
        return path

    def prepare_client(self, platform: str, package: PackageArtifact) -> Mapping[str, Any]:
        name = self.clients[platform]
        if platform == "windows":
            user = "Gate13Admin"
            ready_command = (
                "powershell.exe -NoLogo -NoProfile -NonInteractive -Command "
                "\"if (!(Test-Path -LiteralPath 'C:\\Gate13Bootstrap\\ready.txt' "
                "-PathType Leaf)) { exit 1 }; "
                "$p=@(Get-Process explorer -IncludeUserName -ErrorAction SilentlyContinue | "
                "Where-Object {$_.UserName -like '*\\M'}); "
                'if ($p.Count -ne 1 -or $p[0].SessionId -lt 1) { exit 1 }"'
            )
        else:
            user = None
            ready_command = (
                "test -f /var/lib/gate13-bootstrap-ready && " "sudo -u gate13 env DISPLAY=:99 xdpyinfo >/dev/null"
            )
        self._wait_ssh(
            name,
            ready_command,
            user=user,
            action=f"Waiting for the clean {platform} client",
            timeout_seconds=5_400,
        )
        self._cleanup_route_relay(platform)
        stage, stage_script = self._build_client_stage(platform, package)
        files = [path for path in stage.iterdir() if path.is_file()]
        if platform == "windows":
            destination = f"Gate13Admin@{name}:C:/Gate13Run/"
        else:
            destination = f"{name}:/tmp/"
        self._scp(
            files,
            destination,
            action=f"Staging the {platform} qualification job",
            timeout=900,
        )
        if platform == "windows":
            command = (
                "powershell.exe -NoLogo -NoProfile -NonInteractive "
                "-ExecutionPolicy Bypass -File C:\\Gate13Run\\stage.ps1"
            )
        else:
            command = "sudo bash /tmp/stage.sh"
        result = self._ssh(
            name,
            command,
            user=user,
            action=f"Validating the {platform} qualification stage",
            timeout=300,
        )
        value = _strict_object(result.stdout, f"{platform} qualification stage")
        if value.get("result") != "passed" or value.get("ready") is not True:
            raise Gate13CloudError(f"{platform} qualification stage rejected")
        return {
            "result": "passed",
            "package_relay_verified": True,
            "hashes_verified": True,
            "ordinary_desktop_user_ready": True,
        }

    def _host_command(self, platform: str, action: str) -> tuple[str, str | None]:
        if platform == "windows":
            return (
                f"C:\\Gate13Python\\python.exe C:\\Gate13Run\\gate13_host_job.py "
                f"{action} --config C:\\Gate13Run\\host-job.json",
                "Gate13Admin",
            )
        return (
            f"sudo /usr/bin/python3 /qualification/gate13_host_job.py "
            f"{action} --config /qualification/host-job.json",
            None,
        )

    def run_client(self, platform: str, package: PackageArtifact) -> bytes:
        name = self.clients[platform]
        start_command, user = self._host_command(platform, "start")
        started = self._ssh(
            name,
            start_command,
            user=user,
            action=f"Starting the durable {platform} qualification job",
            timeout=180,
        )
        start_value = _strict_object(started.stdout, f"{platform} host-job start")
        if start_value.get("job_state") not in {"starting", "running", "passed"}:
            raise Gate13CloudError(f"{platform} host job did not start")

        status_command, _ = self._host_command(platform, "status")
        deadline = time.monotonic() + 14_700
        while time.monotonic() < deadline:
            observed = self._ssh(
                name,
                status_command,
                user=user,
                action=f"Checking the {platform} qualification job",
                timeout=180,
            )
            status = _strict_object(observed.stdout, f"{platform} host-job status")
            state = status.get("job_state")
            if state == "passed":
                break
            if state in {"failed", "ambiguous", "absent"}:
                raise Gate13CloudError(f"{platform} host job ended in state {state}")
            if state not in {"starting", "running"}:
                raise Gate13CloudError(f"{platform} host job returned an invalid state")
            self.progress(f"{platform.capitalize()} qualification is {state}; waiting")
            self.sleeper(30)
        else:
            raise Gate13CloudError(f"{platform} host job exceeded its time bound")

        collect_command, _ = self._host_command(platform, "collect")
        collected = self._ssh(
            name,
            collect_command,
            user=user,
            action=f"Collecting the {platform} qualification evidence",
            timeout=300,
        )
        payload = collected.stdout.encode("utf-8")
        cleanup_command, _ = self._host_command(platform, "cleanup")
        self._ssh(
            name,
            cleanup_command,
            user=user,
            action=f"Removing the {platform} native host job",
            timeout=180,
        )
        if not payload:
            raise Gate13CloudError(f"{platform} evidence is empty")
        return payload

    def _expected_image(self, name: str) -> str:
        if name == self.route:
            return self.config.route_image
        if name == self.clients["windows"]:
            return self.config.windows_image
        if name == self.clients["linux"]:
            return self.config.linux_image
        raise Gate13CloudError("instance target is outside this run")

    def _delete_orphan_disk(self, name: str) -> None:
        inventory = self._gcloud_json(
            "compute",
            "disks",
            "list",
            "--project",
            self.config.project,
            action=f"Checking orphan disk {name}",
            timeout=120,
        )
        present = (
            [item for item in inventory if isinstance(item, dict) and item.get("name") == name]
            if isinstance(inventory, list)
            else []
        )
        if not present:
            return
        if len(present) != 1:
            raise Gate13CloudError(f"disk inventory for {name} is ambiguous")
        disk = self._gcloud_json(
            "compute",
            "disks",
            "describe",
            name,
            "--project",
            self.config.project,
            "--zone",
            self.config.zone,
            action=f"Binding orphan disk {name}",
            timeout=120,
        )
        users = disk.get("users") if isinstance(disk, dict) else None
        source_image = self._basename(disk.get("sourceImage")) if isinstance(disk, dict) else ""
        if (
            not isinstance(disk, dict)
            or disk.get("name") != name
            or users not in (None, [])
            or source_image != self._expected_image(name)
        ):
            raise Gate13CloudError(f"refusing to delete unbound disk {name}")
        self._gcloud(
            "compute",
            "disks",
            "delete",
            name,
            "--project",
            self.config.project,
            "--zone",
            self.config.zone,
            "--quiet",
            action=f"Deleting orphan disk {name}",
            timeout=600,
        )

    def _delete_instance(self, name: str, *, label: str) -> None:
        value = self._describe_instance(name, check=False)
        if value is not None:
            self._assert_owned_instance(name, value)
            self._gcloud(
                "compute",
                "instances",
                "delete",
                name,
                "--project",
                self.config.project,
                "--zone",
                self.config.zone,
                "--delete-disks",
                "all",
                "--quiet",
                action=f"Deleting {label}",
                timeout=900,
            )
        self._delete_orphan_disk(name)

    def _delete_firewall(self, name: str) -> None:
        bindings = {
            self.dht_firewall: {
                "source_ranges": ["0.0.0.0/0"],
                "source_tags": [],
                "target_tags": [self.route],
                "ports": ["31337-31338"],
            },
            self.iap_firewall: {
                "source_ranges": ["35.235.240.0/20"],
                "source_tags": [],
                "target_tags": [self.client_tag, self.route],
                "ports": ["22"],
            },
            self.relay_firewall: {
                "source_ranges": [],
                "source_tags": [self.client_tag],
                "target_tags": [self.route],
                "ports": ["38081"],
            },
        }
        if name not in bindings:
            raise Gate13CloudError(f"refusing to inspect unknown firewall {name}")
        inventory = self._gcloud_json(
            "compute",
            "firewall-rules",
            "list",
            "--project",
            self.config.project,
            action=f"Checking firewall {name}",
            timeout=120,
        )
        present = (
            [item for item in inventory if isinstance(item, dict) and item.get("name") == name]
            if isinstance(inventory, list)
            else []
        )
        if not present:
            return
        if len(present) != 1:
            raise Gate13CloudError(f"firewall inventory for {name} is ambiguous")
        firewall = self._gcloud_json(
            "compute",
            "firewall-rules",
            "describe",
            name,
            "--project",
            self.config.project,
            action=f"Binding firewall {name}",
            timeout=120,
        )
        expected = bindings[name]
        allowed = firewall.get("allowed") if isinstance(firewall, dict) else None
        first_allow = allowed[0] if isinstance(allowed, list) and len(allowed) == 1 else None
        if (
            not isinstance(firewall, dict)
            or firewall.get("name") != name
            or self._basename(firewall.get("network")) != self.config.network
            or firewall.get("direction") != "INGRESS"
            or sorted(firewall.get("sourceRanges") or []) != sorted(expected["source_ranges"])
            or sorted(firewall.get("sourceTags") or []) != sorted(expected["source_tags"])
            or sorted(firewall.get("targetTags") or []) != sorted(expected["target_tags"])
            or not isinstance(first_allow, dict)
            or first_allow.get("IPProtocol") != "tcp"
            or first_allow.get("ports") != expected["ports"]
        ):
            raise Gate13CloudError(f"refusing to delete unbound firewall {name}")
        self._gcloud(
            "compute",
            "firewall-rules",
            "delete",
            name,
            "--project",
            self.config.project,
            "--quiet",
            action=f"Deleting firewall {name}",
            timeout=300,
        )

    def delete_client(self, platform: str) -> None:
        if platform not in self.clients:
            raise Gate13CloudError("client platform is invalid")
        self._delete_instance(self.clients[platform], label=f"{platform} client")

    def delete_route(self) -> None:
        self._delete_instance(self.route, label="route VM")
        self._delete_firewall(self.dht_firewall)
        self._delete_firewall(self.iap_firewall)
        self._delete_firewall(self.relay_firewall)

    def cleanup_all(self) -> Mapping[str, Any]:
        errors: list[str] = []
        for platform in ("windows", "linux"):
            try:
                self.delete_client(platform)
            except BaseException as exc:
                errors.append(f"{platform}:{type(exc).__name__}")
        try:
            self.delete_route()
        except BaseException as exc:
            errors.append(f"route:{type(exc).__name__}")
        return {
            "result": "failed" if errors else "passed",
            "errors": errors,
            "exact_targets_only": True,
        }

    def verify_cleanup(self) -> Mapping[str, Any]:
        instances, disks, firewalls = self._resource_absence()
        bootstrap = self._gcloud_json(
            "compute",
            "instances",
            "describe",
            self.config.protected_instance,
            "--project",
            self.config.project,
            "--zone",
            self.config.protected_zone,
            action="Rechecking the protected bootstrap",
            timeout=120,
        )
        protected_running = isinstance(bootstrap, dict) and bootstrap.get("status") == "RUNNING"
        passed = not instances and not disks and not firewalls and protected_running
        return {
            "result": "passed" if passed else "failed",
            "instances_absent": not instances,
            "disks_absent": not disks,
            "firewalls_absent": not firewalls,
            "remaining_instances": instances,
            "remaining_disks": disks,
            "remaining_firewalls": firewalls,
            "protected_bootstrap_running": protected_running,
        }
