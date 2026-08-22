import asyncio
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

logger = logging.getLogger("ssd_intake.runner")

@dataclass
class CommandResult:
    command: List[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float

    @property
    def success(self) -> bool:
        return self.exit_code == 0

    @property
    def combined_output(self) -> str:
        if self.stdout and self.stderr:
            return f"{self.stdout}\n{self.stderr}"
        return self.stdout or self.stderr or ""


def run_command_sync(
    args: List[str],
    timeout: Optional[float] = None,
    check: bool = False,
    env: Optional[dict] = None,
) -> CommandResult:
    """
    Executes a system command synchronously using a list of arguments.
    Strictly forbids shell=True to avoid injection risks.
    """
    cmd_str = " ".join(args)
    logger.debug(f"Running sync command: {cmd_str}")
    start_time = time.time()
    
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)

    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env=merged_env,
            check=False,
        )
        duration = round(time.time() - start_time, 3)
        res = CommandResult(
            command=args,
            exit_code=proc.returncode,
            stdout=proc.stdout.strip(),
            stderr=proc.stderr.strip(),
            duration_seconds=duration,
        )
        if check and not res.success:
            raise RuntimeError(f"Command failed (exit {res.exit_code}): {cmd_str}\n{res.combined_output}")
        return res
    except subprocess.TimeoutExpired as e:
        duration = round(time.time() - start_time, 3)
        stdout = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        logger.error(f"Command timed out after {timeout}s: {cmd_str}")
        return CommandResult(
            command=args,
            exit_code=-1,
            stdout=stdout.strip(),
            stderr=f"Timeout after {timeout}s\n{stderr}".strip(),
            duration_seconds=duration,
        )
    except Exception as e:
        duration = round(time.time() - start_time, 3)
        if isinstance(e, FileNotFoundError):
            logger.debug(f"Command not found: {cmd_str}")
        else:
            logger.error(f"Error running command {cmd_str}: {e}")
        return CommandResult(
            command=args,
            exit_code=-1,
            stdout="",
            stderr=str(e),
            duration_seconds=duration,
        )


async def run_command_async(
    args: List[str],
    log_callback: Optional[Callable[[str], None]] = None,
    timeout: Optional[float] = None,
    cancel_event: Optional[asyncio.Event] = None,
    env: Optional[dict] = None,
) -> CommandResult:
    """
    Executes a system command asynchronously with real-time output streaming.
    Supports cancellation and non-blocking line streaming for live GUI feedback.
    """
    cmd_str = " ".join(args)
    logger.info(f"Running async command: {cmd_str}")
    start_time = time.time()

    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)

    stdout_lines: List[str] = []
    stderr_lines: List[str] = []

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
        )
    except Exception as e:
        duration = round(time.time() - start_time, 3)
        err_msg = f"Failed to spawn process '{cmd_str}': {e}"
        logger.error(err_msg)
        if log_callback:
            log_callback(f"[ERROR] {err_msg}")
        return CommandResult(
            command=args,
            exit_code=-1,
            stdout="",
            stderr=err_msg,
            duration_seconds=duration,
        )

    async def read_stream(stream: asyncio.StreamReader, is_stderr: bool):
        while True:
            line_bytes = await stream.readline()
            if not line_bytes:
                break
            line = line_bytes.decode(errors="replace").rstrip("\r\n")
            if is_stderr:
                stderr_lines.append(line)
            else:
                stdout_lines.append(line)
            
            if log_callback:
                try:
                    prefix = "[STDERR] " if is_stderr else ""
                    log_callback(f"{prefix}{line}")
                except Exception as cb_err:
                    logger.warning(f"Error in log_callback: {cb_err}")

    # Create background stream readers
    stdout_task = asyncio.create_task(read_stream(proc.stdout, False)) if proc.stdout else None
    stderr_task = asyncio.create_task(read_stream(proc.stderr, True)) if proc.stderr else None

    # Wait for completion, timeout, or cancellation
    timed_out = False
    cancelled = False

    async def wait_process():
        await proc.wait()
        if stdout_task:
            await stdout_task
        if stderr_task:
            await stderr_task

    process_task = asyncio.create_task(wait_process())

    try:
        if cancel_event:
            # Race process task with cancel event and timeout
            cancel_waiter = asyncio.create_task(cancel_event.wait())
            done, pending = await asyncio.wait(
                [process_task, cancel_waiter],
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if cancel_waiter in done and cancel_event.is_set():
                cancelled = True
                logger.warning(f"Cancellation requested for command: {cmd_str}")
                if log_callback:
                    log_callback(f"[WARN] Command cancelled by user: {cmd_str}")
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    logger.warning(f"Killing unresponsive process: {cmd_str}")
                    proc.kill()
            elif not done:
                timed_out = True
                logger.error(f"Command timed out after {timeout}s: {cmd_str}")
                proc.kill()
            
            for p in pending:
                p.cancel()
        else:
            if timeout:
                await asyncio.wait_for(process_task, timeout=timeout)
            else:
                await process_task

    except asyncio.TimeoutError:
        timed_out = True
        logger.error(f"Command timed out after {timeout}s: {cmd_str}")
        proc.kill()
    except asyncio.CancelledError:
        cancelled = True
        logger.warning(f"Task cancelled while running {cmd_str}")
        proc.kill()
        raise

    duration = round(time.time() - start_time, 3)
    exit_code = proc.returncode if proc.returncode is not None else (-9 if cancelled else -1)
    
    stdout_str = "\n".join(stdout_lines)
    stderr_str = "\n".join(stderr_lines)

    if timed_out:
        stderr_str = f"Command timed out after {timeout}s\n{stderr_str}".strip()

    return CommandResult(
        command=args,
        exit_code=exit_code,
        stdout=stdout_str,
        stderr=stderr_str,
        duration_seconds=duration,
    )
