"""用 asyncio 子进程执行 CLI，并解析 stream-json 输出。"""

import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_JSON_BLOCK = re.compile(r"```json\s*\n([\s\S]*?)\n```")


@dataclass
class CliResult:
    """一次 CLI 调用的退出码、正文和抽出的 JSON。"""

    exit_code: int | None = None
    error: str | None = None
    full_text: str | None = None
    extracted_json: str | None = None
    cli_session_id: str | None = None


@dataclass
class StreamParseResult:
    """从 stdout 行里拼出的文本。"""

    text: str = ""
    extracted_json: str | None = None
    cli_session_id: str | None = None


class CliExecutor:
    """对齐 ``CliExecutor`` 的命令行、环境和超时。"""

    def __init__(
        self,
        cli_binary: str = "claude",
        timeout_seconds: int = 300,
        launch_mode: str = "direct",
        shell_binary: str = "/bin/bash",
        anthropic_api_key: str = "",
        anthropic_auth_token: str = "",
        anthropic_base_url: str = "",
        anthropic_model: str = "",
    ) -> None:
        self.cli_binary = cli_binary
        self.timeout_seconds = timeout_seconds
        self.launch_mode = launch_mode
        self.shell_binary = shell_binary
        self.anthropic_api_key = anthropic_api_key
        self.anthropic_auth_token = anthropic_auth_token
        self.anthropic_base_url = anthropic_base_url
        self.anthropic_model = anthropic_model

    async def execute(
        self,
        prompt: str,
        cli_session_ref: str | None,
        work_dir: str,
        allowed_tools: str | None,
        system_prompt: str | None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> CliResult:
        """启动 CLI。超时或启动失败时退出码为 -1，并带上错误文本。"""
        command = self.build_command(
            prompt, cli_session_ref, work_dir, allowed_tools, system_prompt
        )
        result = CliResult()
        logger.info(
            "cli exec start workDir=%s promptLen=%s hasResume=%s",
            work_dir,
            len(prompt),
            cli_session_ref is not None,
        )
        try:
            env = os.environ.copy()
            _set_env(env, "ANTHROPIC_API_KEY", self.anthropic_api_key)
            _set_env(env, "ANTHROPIC_AUTH_TOKEN", self.anthropic_auth_token)
            _set_env(env, "ANTHROPIC_BASE_URL", self.anthropic_base_url)
            _set_env(env, "ANTHROPIC_MODEL", self.anthropic_model)
            env["HOME"] = work_dir
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=work_dir,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_task = asyncio.create_task(_read_stdout(process.stdout, on_delta))
            stderr_task = asyncio.create_task(_read_stderr(process.stderr))
            try:
                await asyncio.wait_for(process.wait(), self.timeout_seconds)
            except TimeoutError:
                process.kill()
                await process.wait()
                stdout_task.cancel()
                stderr_task.cancel()
                result.exit_code = -1
                result.error = "CLI timeout after " + str(self.timeout_seconds) + "s"
                return result
            parsed = await stdout_task
            stderr = await stderr_task
            result.exit_code = process.returncode
            result.full_text = parsed.text
            result.extracted_json = parsed.extracted_json
            result.cli_session_id = parsed.cli_session_id
            if process.returncode != 0:
                result.error = stderr if len(stderr) <= 500 else stderr[:500]
        except Exception as error:
            logger.error("CLI execution failed", exc_info=True)
            result.exit_code = -1
            result.error = str(error)
        return result

    def build_command(
        self,
        prompt: str,
        cli_session_ref: str | None,
        work_dir: str,
        allowed_tools: str | None,
        system_prompt: str | None,
    ) -> list[str]:
        """拼 CLI 参数。非 direct 模式交给 shell，并重定向 stdin。"""
        del work_dir
        args = [
            self.cli_binary,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if cli_session_ref is not None and cli_session_ref.strip() != "":
            args.extend(["--resume", cli_session_ref])
        if allowed_tools is not None:
            args.extend(["--allowedTools", allowed_tools])
        if system_prompt is not None and system_prompt.strip() != "":
            args.extend(["--append-system-prompt", system_prompt])
        if self.launch_mode.lower() == "direct":
            return args
        return [self.shell_binary, "-c", _shell_command(args) + " < /dev/null"]


def parse_stream_output(lines: list[str]) -> StreamParseResult:
    """解析 stream-json 行。空行跳过，非 JSON 行忽略。"""
    result = StreamParseResult()
    full_text: list[str] = []
    for line in lines:
        if line.strip() == "":
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type == "assistant":
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text")
                        if isinstance(text, str):
                            full_text.append(text)
        elif event_type == "result":
            result_text = event.get("result")
            if isinstance(result_text, str) and len(full_text) == 0:
                full_text.append(result_text)
            session_id = event.get("session_id")
            if isinstance(session_id, str):
                result.cli_session_id = session_id
    result.text = "".join(full_text)
    result.extracted_json = extract_json_block(result.text)
    return result


def extract_json_block(text: str | None) -> str | None:
    """优先取 markdown json 代码块，否则接受整段或第一段对象。"""
    if text is None or text.strip() == "":
        return None
    matched = _JSON_BLOCK.search(text)
    if matched is not None:
        return matched.group(1).strip()
    trimmed = text.strip()
    if trimmed.startswith("{") or trimmed.startswith("["):
        try:
            json.loads(trimmed)
        except json.JSONDecodeError:
            embedded = _first_object(trimmed)
            return embedded
        return trimmed
    return _first_object(trimmed)


def _first_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(text[start:], start):
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : index + 1]
                try:
                    json.loads(candidate)
                except json.JSONDecodeError:
                    return None
                return candidate
    return None


def _shell_command(args: list[str]) -> str:
    command = "exec"
    for arg in args:
        command += " " + _shell_quote(arg)
    return command


def _shell_quote(value: str | None) -> str:
    if value is None or value == "":
        return "''"
    return "'" + value.replace("'", "'\\''") + "'"


def _set_env(env: dict[str, str], key: str, value: str) -> None:
    if value != "":
        env[key] = value


async def _read_stdout(
    stream: asyncio.StreamReader | None,
    on_delta: Callable[[str], Awaitable[None]] | None,
) -> StreamParseResult:
    if stream is None:
        return StreamParseResult()
    lines: list[str] = []
    while True:
        raw = await stream.readline()
        if raw == b"":
            break
        line = raw.decode()
        if line.endswith("\n"):
            line = line[:-1]
        lines.append(line)
        if on_delta is not None and line.strip() != "":
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                event = None
            if isinstance(event, dict) and event.get("type") == "assistant":
                message = event.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text")
                            if isinstance(text, str) and text != "":
                                await on_delta(text)
    return parse_stream_output(lines)


async def _read_stderr(stream: asyncio.StreamReader | None) -> str:
    if stream is None:
        return ""
    data = await stream.read()
    return data.decode()
