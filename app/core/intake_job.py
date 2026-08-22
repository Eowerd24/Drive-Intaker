import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from app.config import settings
from app.core.disk_detector import DiskDetector, disk_detector
from app.core.fio_runner import BenchmarkSuiteResult, FioRunner, fio_runner
from app.core.firmware import FirmwareCheckResult, FirmwareManager, firmware_manager
from app.core.reporter import IntakeRunRecord, ReportGenerator, report_generator
from app.core.runner import CommandResult, run_command_async, run_command_sync
from app.core.safety import SafetyCheckResult, SafetyValidator, safety_validator
from app.core.smart_parser import SmartParser, SmartReport

logger = logging.getLogger("ssd_intake.job")

JOB_STAGES = [
    "INITIAL_INVENTORY",
    "SAFETY_CHECKS",
    "BEFORE_SNAPSHOT",
    "WIPE",
    "SMART_SHORT",
    "SMART_LONG",
    "FIRMWARE_CHECK",
    "FULL_VERIFY",
    "BENCHMARKS",
    "AFTER_SNAPSHOT",
    "GENERATE_REPORT",
]

@dataclass
class JobConfig:
    target_disk: str
    workflow_mode: str = "full"  # full, inventory, custom
    entered_serial: Optional[str] = None
    skip_firmware: bool = False
    skip_long_smart: bool = False
    skip_full_verify: bool = False
    skip_bench: bool = False


@dataclass
class JobStatus:
    job_id: str
    target_disk: str
    status: str  # IDLE, RUNNING, COMPLETED, FAILED, CANCELLED
    current_stage: str
    progress_percent: int
    stages_status: Dict[str, str] = field(default_factory=dict)  # stage -> PENDING, RUNNING, PASSED, FAILED, SKIPPED
    start_time: str = ""
    end_time: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    drive_model: str = ""
    drive_serial: str = ""
    drive_capacity: str = ""
    classification: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "target_disk": self.target_disk,
            "status": self.status,
            "current_stage": self.current_stage,
            "progress_percent": self.progress_percent,
            "stages_status": self.stages_status,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "errors": self.errors,
            "drive_model": self.drive_model,
            "drive_serial": self.drive_serial,
            "drive_capacity": self.drive_capacity,
            "classification": self.classification,
        }


class IntakeJobManager:
    def __init__(
        self,
        detector: Optional[DiskDetector] = None,
        validator: Optional[SafetyValidator] = None,
        reporter: Optional[ReportGenerator] = None,
        fw_mgr: Optional[FirmwareManager] = None,
        fio: Optional[FioRunner] = None,
    ):
        self.detector = detector or disk_detector
        self.validator = validator or safety_validator
        self.reporter = reporter or report_generator
        self.firmware_mgr = fw_mgr or firmware_manager
        self.fio = fio or fio_runner

        self._lock = asyncio.Lock()
        self._current_task: Optional[asyncio.Task] = None
        self._cancel_event = asyncio.Event()
        self.current_status: Optional[JobStatus] = None
        self.log_history: List[str] = []
        self._subscribers: Set[asyncio.Queue] = set()

    @property
    def is_running(self) -> bool:
        return self.current_status is not None and self.current_status.status == "RUNNING"

    def subscribe_logs(self) -> asyncio.Queue:
        """Returns a queue receiving log lines and stage events."""
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe_logs(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _broadcast_log(self, line: str) -> None:
        formatted = f"[{datetime.now().strftime('%H:%M:%S')}] {line}"
        self.log_history.append(formatted)
        for q in list(self._subscribers):
            try:
                q.put_nowait({"type": "log", "data": formatted})
            except Exception:
                pass

    def _broadcast_stage_change(self, stage: str, stage_status: str, progress: int) -> None:
        if self.current_status:
            self.current_status.current_stage = stage
            self.current_status.stages_status[stage] = stage_status
            self.current_status.progress_percent = progress
            event_data = {
                "type": "stage",
                "stage": stage,
                "status": stage_status,
                "progress": progress,
                "job": self.current_status.to_dict(),
            }
            for q in list(self._subscribers):
                try:
                    q.put_nowait(event_data)
                except Exception:
                    pass

    async def start_job(self, config: JobConfig) -> JobStatus:
        """Initiates a new intake job if no other job is active."""
        async with self._lock:
            if self.is_running:
                raise RuntimeError(
                    f"Another intake job ({self.current_status.job_id}) is currently in progress on {self.current_status.target_disk}."
                )

            self.log_history.clear()
            self._cancel_event.clear()

            # Pre-validate drive exists and collect basic info
            drive = self.detector.get_drive(config.target_disk)
            if not drive:
                raise ValueError(f"Drive {config.target_disk} not found on host.")

            is_destructive = config.workflow_mode != "inventory"
            
            # Initial stage list setup
            init_stages = {}
            for s in JOB_STAGES:
                if config.workflow_mode == "inventory" and s in ["WIPE", "SMART_SHORT", "SMART_LONG", "FULL_VERIFY", "BENCHMARKS"]:
                    init_stages[s] = "SKIPPED"
                elif config.workflow_mode == "smart_long" and s in ["WIPE", "SMART_SHORT", "FIRMWARE_CHECK", "FULL_VERIFY", "BENCHMARKS"]:
                    init_stages[s] = "SKIPPED"
                elif config.workflow_mode == "full" and s == "SMART_LONG":
                    init_stages[s] = "SKIPPED"  # Long SMART is separate from regular intake
                elif config.skip_long_smart and s == "SMART_LONG":
                    init_stages[s] = "SKIPPED"
                elif config.skip_firmware and s == "FIRMWARE_CHECK":
                    init_stages[s] = "SKIPPED"
                elif config.skip_full_verify and s == "FULL_VERIFY":
                    init_stages[s] = "SKIPPED"
                elif config.skip_bench and s == "BENCHMARKS":
                    init_stages[s] = "SKIPPED"
                else:
                    init_stages[s] = "PENDING"

            self.current_status = JobStatus(
                job_id="pending",
                target_disk=drive.canonical_path,
                status="RUNNING",
                current_stage="INITIAL_INVENTORY",
                progress_percent=0,
                stages_status=init_stages,
                start_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                drive_model=drive.model,
                drive_serial=drive.serial,
                drive_capacity=drive.size_str,
            )

            # Spawn background execution task
            self._current_task = asyncio.create_task(self._execute_workflow(config, drive))
            return self.current_status

    async def cancel_current_job(self) -> bool:
        """Requests graceful cancellation of the running intake job."""
        if not self.is_running:
            return False
        self._broadcast_log("[WARN] User requested job cancellation! Terminating active tasks...")
        self._cancel_event.set()
        if self.current_status:
            self.current_status.status = "CANCELLED"
        return True

    async def _execute_workflow(self, config: JobConfig, drive_info: Any) -> None:
        """Main workflow state machine."""
        target_disk = drive_info.canonical_path
        is_destructive = config.workflow_mode not in ("inventory", "smart_long")
        record: Optional[IntakeRunRecord] = None
        run_dir: Optional[Path] = None

        try:
            # 1. INITIAL_INVENTORY
            self._broadcast_stage_change("INITIAL_INVENTORY", "RUNNING", 5)
            self._broadcast_log(f"=== Starting SSD Intake Workflow for {target_disk} ({config.workflow_mode.upper()} mode) ===")
            self._broadcast_log(f"Drive Model:    {drive_info.model}")
            self._broadcast_log(f"Drive Serial:   {drive_info.serial}")
            self._broadcast_log(f"Drive Capacity: {drive_info.size_str}")
            self._broadcast_log(f"Transport:      {drive_info.transport}")
            self._broadcast_log(f"Firmware:       {drive_info.firmware}")

            run_id, run_dir = self.reporter.create_run_directory(drive_info.serial)
            if self.current_status:
                self.current_status.job_id = run_id

            sys_disks = self.validator.detect_system_disks()
            sys_disk_str = ", ".join(sys_disks) if sys_disks else "None detected"
            self._broadcast_log(f"Protected System Disks: {sys_disk_str}")
            self._broadcast_log(f"Reports Directory:      {run_dir}")

            record = IntakeRunRecord(
                run_id=run_id,
                target_device=target_disk,
                model=drive_info.model,
                serial=drive_info.serial,
                capacity=drive_info.size_str,
                transport=drive_info.transport,
                created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                status="RUNNING",
                mode=config.workflow_mode,
                firmware_before=drive_info.firmware,
                protected_system_disk=sys_disk_str,
                stages=self.current_status.stages_status if self.current_status else {},
            )
            self.reporter.save_run_metadata(run_dir, record)
            self._broadcast_stage_change("INITIAL_INVENTORY", "PASSED", 10)

            # 2. SAFETY CHECKS
            self._broadcast_stage_change("SAFETY_CHECKS", "RUNNING", 12)
            self._broadcast_log("[SAFETY] Performing rigorous pre-execution safety validation...")
            
            safety_res = self.validator.validate_candidate(
                target_disk=target_disk,
                is_destructive=is_destructive,
                expected_serial=drive_info.serial,
                entered_serial=config.entered_serial,
            )

            if not safety_res.is_safe:
                err_msg = f"SAFETY VALIDATION REJECTED: {'; '.join(safety_res.reasons)}"
                self._broadcast_log(f"[FATAL] {err_msg}")
                raise PermissionError(err_msg)

            self._broadcast_log("[SAFETY] All safety checks passed. Target disk verified eligible.")
            self._broadcast_stage_change("SAFETY_CHECKS", "PASSED", 15)

            # 3. BEFORE_SNAPSHOT
            self._broadcast_stage_change("BEFORE_SNAPSHOT", "RUNNING", 18)
            self._broadcast_log("[SNAPSHOT] Capturing baseline hardware state (before)...")
            smart_before = await self._capture_snapshot(target_disk, run_dir / "before")
            record.smart_before = smart_before.to_dict()
            self._broadcast_stage_change("BEFORE_SNAPSHOT", "PASSED", 20)

            # Handle Inventory-Only Mode
            if config.workflow_mode == "inventory":
                if not config.skip_firmware:
                    self._broadcast_stage_change("FIRMWARE_CHECK", "RUNNING", 50)
                    fw_res = await self.firmware_mgr.run_firmware_check(
                        device_path=target_disk,
                        serial=drive_info.serial,
                        current_fw=drive_info.firmware,
                        output_dir=run_dir / "firmware",
                        log_callback=self._broadcast_log,
                    )
                    record.firmware_status = fw_res.summary_status
                    record.firmware_updatable = fw_res.is_updatable
                    record.firmware_update_available = fw_res.update_available
                    self._broadcast_stage_change("FIRMWARE_CHECK", "PASSED", 80)

                # Clone snapshot
                shutil.copytree(run_dir / "before", run_dir / "after", dirs_exist_ok=True)
                record.smart_after = record.smart_before
                record.status = "COMPLETED"
                record.completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                self._broadcast_stage_change("GENERATE_REPORT", "RUNNING", 90)
                self.reporter.generate_markdown_report(run_dir, record)
                self.reporter.save_run_metadata(run_dir, record)
                self._broadcast_stage_change("GENERATE_REPORT", "PASSED", 100)

                self._broadcast_log(f"[SUCCESS] Inventory collection complete: {run_dir / 'REPORT.md'}")
                if self.current_status:
                    self.current_status.status = "COMPLETED"
                    self.current_status.end_time = record.completed_at
                return

            # Handle Standalone SMART Long Test Mode
            if config.workflow_mode == "smart_long":
                self._check_cancelled()
                self._broadcast_stage_change("SMART_LONG", "RUNNING", 30)
                await self._run_smart_selftest(target_disk, "long", run_dir / "tests", smart_before.long_test_duration_minutes)
                self._broadcast_stage_change("SMART_LONG", "PASSED", 80)

                self._check_cancelled()
                self._broadcast_stage_change("AFTER_SNAPSHOT", "RUNNING", 85)
                self._broadcast_log("[SNAPSHOT] Capturing final SMART status after extended test...")
                smart_after = await self._capture_snapshot(target_disk, run_dir / "after")
                record.smart_after = smart_after.to_dict()
                record.smart_diff = SmartParser.diff_smart(smart_before, smart_after)
                self._broadcast_stage_change("AFTER_SNAPSHOT", "PASSED", 90)

                record.status = "COMPLETED"
                record.completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self._broadcast_stage_change("GENERATE_REPORT", "RUNNING", 95)
                self.reporter.generate_markdown_report(run_dir, record)
                self.reporter.save_run_metadata(run_dir, record)
                self._broadcast_stage_change("GENERATE_REPORT", "PASSED", 100)

                self._broadcast_log(f"[SUCCESS] SMART Extended Self-Test complete: {run_dir / 'REPORT.md'}")
                if self.current_status:
                    self.current_status.status = "COMPLETED"
                    self.current_status.end_time = record.completed_at
                return

            # Regular / Custom Destructive Workflow Sequence

            # 4. WIPE
            self._check_cancelled()
            self._broadcast_stage_change("WIPE", "RUNNING", 25)
            self._broadcast_log("[WIPE] Unmounting partitions and destroying GPT/MBR/filesystem signatures...")
            await self._wipe_disk(target_disk)
            self._broadcast_stage_change("WIPE", "PASSED", 30)

            # 5. SMART_SHORT (Part of Regular Intake Pipeline)
            self._check_cancelled()
            self._broadcast_stage_change("SMART_SHORT", "RUNNING", 35)
            await self._run_smart_selftest(target_disk, "short", run_dir / "tests", smart_before.short_test_duration_minutes)
            self._broadcast_stage_change("SMART_SHORT", "PASSED", 45)

            # 6. SMART_LONG (Only if explicitly enabled via custom mode)
            if config.workflow_mode == "custom" and not config.skip_long_smart:
                self._check_cancelled()
                self._broadcast_stage_change("SMART_LONG", "RUNNING", 47)
                await self._run_smart_selftest(target_disk, "long", run_dir / "tests", smart_before.long_test_duration_minutes)
                self._broadcast_stage_change("SMART_LONG", "PASSED", 52)
            else:
                self._broadcast_stage_change("SMART_LONG", "SKIPPED", 52)

            # 7. FIRMWARE_CHECK
            if not config.skip_firmware:
                self._check_cancelled()
                self._broadcast_stage_change("FIRMWARE_CHECK", "RUNNING", 55)
                fw_res = await self.firmware_mgr.run_firmware_check(
                    device_path=target_disk,
                    serial=drive_info.serial,
                    current_fw=drive_info.firmware,
                    output_dir=run_dir / "firmware",
                    log_callback=self._broadcast_log,
                )
                record.firmware_status = fw_res.summary_status
                record.firmware_updatable = fw_res.is_updatable
                record.firmware_update_available = fw_res.update_available
                self._broadcast_stage_change("FIRMWARE_CHECK", "PASSED", 62)
            else:
                self._broadcast_stage_change("FIRMWARE_CHECK", "SKIPPED", 62)

            # 8. FULL_VERIFY (Destructive Write + CRC Verification)
            if not config.skip_full_verify:
                self._check_cancelled()
                self._broadcast_stage_change("FULL_VERIFY", "RUNNING", 62)
                verify_res = await self.fio.run_full_verify(
                    disk_path=target_disk,
                    output_dir=run_dir / "tests",
                    log_callback=self._broadcast_log,
                    cancel_event=self._cancel_event,
                )
                record.full_verify = {
                    "write": verify_res["write"].to_dict(),
                    "verify": verify_res["verify"].to_dict(),
                }
                if not verify_res["verify"].passed:
                    self._broadcast_stage_change("FULL_VERIFY", "FAILED", 75)
                    raise RuntimeError("Full-device CRC verification read detected data corruption errors!")
                self._broadcast_stage_change("FULL_VERIFY", "PASSED", 75)
            else:
                self._broadcast_stage_change("FULL_VERIFY", "SKIPPED", 75)

            # 9. BENCHMARKS
            if not config.skip_bench:
                self._check_cancelled()
                self._broadcast_stage_change("BENCHMARKS", "RUNNING", 78)
                bench_suite = await self.fio.run_benchmarks(
                    disk_path=target_disk,
                    output_dir=run_dir / "benchmarks",
                    log_callback=self._broadcast_log,
                    cancel_event=self._cancel_event,
                )
                record.benchmarks = bench_suite.to_dict()
                if not bench_suite.all_passed:
                    self._broadcast_stage_change("BENCHMARKS", "FAILED", 85)
                else:
                    self._broadcast_stage_change("BENCHMARKS", "PASSED", 85)
            else:
                self._broadcast_stage_change("BENCHMARKS", "SKIPPED", 85)

            # 10. AFTER_SNAPSHOT
            self._check_cancelled()
            self._broadcast_stage_change("AFTER_SNAPSHOT", "RUNNING", 88)
            self._broadcast_log("[SNAPSHOT] Capturing final hardware state (after)...")
            smart_after = await self._capture_snapshot(target_disk, run_dir / "after")
            record.smart_after = smart_after.to_dict()
            record.smart_diff = SmartParser.diff_smart(smart_before, smart_after)
            self._broadcast_stage_change("AFTER_SNAPSHOT", "PASSED", 92)

            # 11. GENERATE_REPORT
            self._broadcast_stage_change("GENERATE_REPORT", "RUNNING", 95)
            record.status = "COMPLETED"
            record.completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.reporter.generate_markdown_report(run_dir, record)
            self.reporter.save_run_metadata(run_dir, record)
            self._broadcast_stage_change("GENERATE_REPORT", "PASSED", 100)

            self._broadcast_log(f"[SUCCESS] SSD Intake Workflow completed successfully!")
            self._broadcast_log(f"Final Report: {run_dir / 'REPORT.md'}")

            if self.current_status:
                self.current_status.status = "COMPLETED"
                self.current_status.end_time = record.completed_at

        except asyncio.CancelledError:
            self._broadcast_log("[WARN] Job was explicitly cancelled by user.")
            if self.current_status:
                self.current_status.status = "CANCELLED"
                self.current_status.end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if record and run_dir:
                record.status = "CANCELLED"
                record.completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                record.errors.append("Job cancelled by user before completion.")
                self.reporter.generate_markdown_report(run_dir, record)
                self.reporter.save_run_metadata(run_dir, record)
        except Exception as e:
            err_str = str(e)
            logger.exception(f"Intake workflow failed on {target_disk}: {err_str}")
            self._broadcast_log(f"[FATAL ERROR] {err_str}")
            if self.current_status:
                self.current_status.status = "FAILED"
                self.current_status.end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.current_status.errors.append(err_str)
            if record and run_dir:
                record.status = "FAILED"
                record.completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                record.errors.append(err_str)
                self.reporter.generate_markdown_report(run_dir, record)
                self.reporter.save_run_metadata(run_dir, record)

    def _check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise asyncio.CancelledError("Execution cancelled by user.")

    async def _capture_snapshot(self, disk_path: str, target_dir: Path) -> SmartReport:
        """Captures smartctl, hdparm, udev, and lsblk snapshots to disk."""
        target_dir.mkdir(parents=True, exist_ok=True)
        
        # smartctl text & json
        res_txt = await run_command_async(["smartctl", "-x", disk_path], log_callback=None)
        (target_dir / "smart.txt").write_text(res_txt.combined_output)

        res_json = await run_command_async(["smartctl", "-x", "-j", disk_path], log_callback=None)
        (target_dir / "smart.json").write_text(res_json.combined_output)

        # hdparm
        res_hd = await run_command_async(["hdparm", "-I", disk_path], log_callback=None)
        (target_dir / "hdparm.txt").write_text(res_hd.combined_output)

        # udev
        res_udev = await run_command_async(["udevadm", "info", "--query=property", f"--name={disk_path}"], log_callback=None)
        (target_dir / "udev.txt").write_text(res_udev.combined_output)

        # lsblk
        res_lsblk = await run_command_async(["lsblk", "-O", disk_path], log_callback=None)
        (target_dir / "lsblk.txt").write_text(res_lsblk.combined_output)

        # lsscsi
        res_lsscsi = await run_command_async(["lsscsi", "-g"], log_callback=None)
        (target_dir / "lsscsi.txt").write_text(res_lsscsi.combined_output)

        return SmartParser.parse(smart_text=res_txt.combined_output, smart_json_str=res_json.stdout)

    async def _wipe_disk(self, disk_path: str) -> None:
        """Safely unmounts and wipes partitions and filesystem signatures."""
        # Unmount any partitions
        res_parts = await run_command_async(["lsblk", "-lnpo", "NAME,TYPE", disk_path])
        if res_parts.success:
            for line in res_parts.stdout.splitlines():
                parts = line.strip().split()
                if len(parts) >= 2 and parts[1] == "part":
                    part_dev = parts[0]
                    self._broadcast_log(f"[WIPE] Unmounting partition {part_dev}...")
                    await run_command_async(["umount", part_dev])

        self._broadcast_log(f"[WIPE] Executing sgdisk --zap-all {disk_path}...")
        zap_res = await run_command_async(["sgdisk", "--zap-all", disk_path], log_callback=self._broadcast_log)
        if not zap_res.success:
            self._broadcast_log(f"[WARN] sgdisk returned exit code {zap_res.exit_code}: {zap_res.stderr}")

        self._broadcast_log(f"[WIPE] Executing wipefs -a {disk_path}...")
        await run_command_async(["wipefs", "-a", disk_path], log_callback=self._broadcast_log)

        # Reread partition table
        self._broadcast_log("[WIPE] Requesting kernel partition table refresh...")
        pp_res = await run_command_async(["partprobe", disk_path])
        if not pp_res.success:
            await run_command_async(["blockdev", "--rereadpt", disk_path])

        await run_command_async(["udevadm", "settle"])
        await asyncio.sleep(2)

        # Second wipefs pass to guarantee clean state
        await run_command_async(["wipefs", "-a", disk_path], log_callback=self._broadcast_log)
        await run_command_async(["sync"])

        # Check for remaining signatures
        chk_res = await run_command_async(["wipefs", disk_path])
        if chk_res.success and chk_res.stdout.strip():
            self._broadcast_log(f"[WARN] Residual signatures detected on {disk_path}:\n{chk_res.stdout}")

    async def _run_smart_selftest(self, disk_path: str, test_type: str, tests_dir: Path, estimated_mins: int) -> None:
        """Initiates SMART self-test, polls status, and waits for completion."""
        tests_dir.mkdir(parents=True, exist_ok=True)
        start_file = tests_dir / f"smart-{test_type}-start.txt"
        res_file = tests_dir / f"smart-{test_type}-result.txt"

        self._broadcast_log(f"[SMART] Triggering SMART {test_type.upper()} self-test on {disk_path}...")
        res_start = await run_command_async(["smartctl", "-t", test_type, disk_path], log_callback=self._broadcast_log)
        start_file.write_text(res_start.combined_output)

        max_wait_seconds = max(30, estimated_mins * 60 + 60)
        self._broadcast_log(f"[SMART] Waiting for drive internal {test_type} test (Estimated: {estimated_mins} mins, Max wait: {max_wait_seconds // 60} mins)...")

        # Initial grace period
        await asyncio.sleep(15)
        elapsed = 15

        # Dynamic polling loop
        poll_interval = 15
        while elapsed < max_wait_seconds:
            self._check_cancelled()

            # Check if self test is still in progress
            res_c = await run_command_async(["smartctl", "-c", disk_path])
            res_l = await run_command_async(["smartctl", "-l", "selftest", disk_path])
            
            output_lower = f"{res_c.combined_output}\n{res_l.combined_output}".lower()
            is_in_progress = "in progress" in output_lower or "self-test routine in progress" in output_lower

            if not is_in_progress and elapsed >= 30:
                self._broadcast_log(f"[SMART] Internal {test_type} test reported finished after ~{elapsed // 60}m {elapsed % 60}s.")
                break

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            if elapsed % 60 == 0:
                self._broadcast_log(f"[SMART] {test_type.capitalize()} self-test in progress on drive... ({elapsed // 60}/{estimated_mins} mins)")

        # Collect final self-test log
        self._broadcast_log(f"[SMART] Collecting SMART self-test log for {disk_path}...")
        res_log = await run_command_async(["smartctl", "-l", "selftest", disk_path], log_callback=self._broadcast_log)
        res_file.write_text(res_log.combined_output)


intake_job_manager = IntakeJobManager()
