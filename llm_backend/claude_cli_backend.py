"""Claude Code CLI backend for the LLM-as-a-judge (local, subscription-billed).

Shells out to ``claude -p --output-format json`` for each judge inference so the
oracle scores diagnoses against a Claude Code subscription instead of a metered
API key. Enabled by setting the judge model to ``claude_cli/<model>`` (e.g.
``--judge-model claude_cli/claude-sonnet-4-5``) or ``JUDGE_PROVIDER=claude_cli``.

Exposes the same ``inference(...)`` surface as ``LiteLLMBackend`` so it is a
drop-in for ``get_llm_backend_for_judge()``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

logger = logging.getLogger(__name__)

# Credentials stripped from the subprocess environment so the CLI authenticates
# against the interactive subscription login, not a metered API key.
_STRIPPED_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

_MODEL_PREFIX = "claude_cli/"


class ClaudeCliBackend:
    def __init__(
        self,
        model_name: str,
        cli_path: str | None = None,
        timeout: int = 180,
        max_retries: int = 3,
    ):
        self.model_name = model_name[len(_MODEL_PREFIX) :] if model_name.startswith(_MODEL_PREFIX) else model_name
        if not self.model_name:
            self.model_name = "claude-sonnet-4-5"
        self.cli_path = cli_path or os.environ.get("CLAUDE_CLI_PATH", "claude")
        self.timeout = timeout
        self.max_retries = max_retries

    def inference(
        self,
        messages: str | list[SystemMessage | HumanMessage | AIMessage],
        system_prompt: str | None = None,
        tools: list | None = None,
    ) -> AIMessage:
        rendered_system, rendered_user = self._render(messages, system_prompt)

        command = [
            self.cli_path,
            "-p",
            rendered_user,
            "--output-format",
            "json",
            "--model",
            self.model_name,
            "--tools",
            "",
            "--strict-mcp-config",
            "--setting-sources",
            "",
            "--no-session-persistence",
        ]
        if rendered_system:
            command.extend(["--system-prompt", rendered_system])

        env = {key: value for key, value in os.environ.items() if key not in _STRIPPED_ENV_VARS}

        last_error = ""
        for attempt in range(self.max_retries):
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    env=env,
                )
            except subprocess.TimeoutExpired:
                last_error = f"claude CLI timed out after {self.timeout}s"
                logger.warning(f"{last_error} (attempt {attempt + 1}/{self.max_retries})")
                continue

            content = self._extract_result(completed.stdout)
            if content:
                return AIMessage(content=content)

            last_error = (completed.stderr or completed.stdout or "empty CLI response").strip()[:300]
            logger.warning(
                f"claude CLI returned no result (attempt {attempt + 1}/{self.max_retries}): {last_error}"
            )
            time.sleep(2 * (attempt + 1))

        raise RuntimeError(f"claude CLI judge inference failed after {self.max_retries} attempts: {last_error}")

    def _render(
        self,
        messages: str | list[SystemMessage | HumanMessage | AIMessage],
        system_prompt: str | None,
    ) -> tuple[str, str]:
        if isinstance(messages, str):
            return system_prompt or "", messages

        system_parts: list[str] = [system_prompt] if system_prompt else []
        user_parts: list[str] = []
        for message in messages:
            text = message.content if isinstance(message.content, str) else str(message.content)
            if isinstance(message, SystemMessage):
                system_parts.append(text)
            else:
                user_parts.append(text)
        return "\n\n".join(p for p in system_parts if p), "\n\n".join(user_parts)

    @staticmethod
    def _extract_result(stdout: str) -> str:
        if not stdout or not stdout.strip():
            return ""
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            return ""
        if payload.get("is_error"):
            return ""
        result = payload.get("result", "")
        return re.sub(r"```json\s*|\s*```", "", result).strip() if isinstance(result, str) else ""


def is_claude_cli_judge(
    *,
    provider: str | None = None,
    model_name: str | None = None,
) -> bool:
    """True when the configured judge should route through the Claude Code CLI.

    Explicit constructor configuration wins over the environment. Upstream #963
    made DiagnosisJudge pass provider/model_name through, so a judge configured in
    code with provider="claude_cli" must route here even when JUDGE_PROVIDER and
    JUDGE_MODEL_ID are unset. Falling back to the env vars keeps the original
    behaviour for callers that pass nothing.
    """
    if provider is not None and provider.strip().lower() == "claude_cli":
        return True
    if model_name is not None and model_name.startswith(_MODEL_PREFIX):
        return True
    if provider is None and os.environ.get("JUDGE_PROVIDER", "").strip().lower() == "claude_cli":
        return True
    if model_name is None and os.environ.get("JUDGE_MODEL_ID", "").startswith(_MODEL_PREFIX):
        return True
    return False
