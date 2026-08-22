import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.core.runner import CommandResult, run_command_async

logger = logging.getLogger("ssd_intake.fio")

@dataclass
class FioJobMetrics:
    job_name: str
    read_bw_mb_s: float = 0.0
    read_iops: float = 0.0
    read_lat_mean_ms: float = 0.0
    read_lat_p99_ms: float = 0.0
    write_bw_mb_s: float = 0.0
    write_iops: float = 0.0
    write_lat_mean_ms: float = 0.0
    write_lat_p99_ms: float = 0.0
    total_errors: int = 0
    duration_seconds: float = 0.0
    passed: bool = True
    raw_output: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_name": self.job_name,
            "read_bw_mb_s": self.read_bw_mb_s,
            "read_iops": self.read_iops,
            "read_lat_mean_ms": self.read_lat_mean_ms,
            "read_lat_p99_ms": self.read_lat_p99_ms,
            "write_bw_mb_s": self.write_bw_mb_s,
            "write_iops": self.write_iops,
            "write_lat_mean_ms": self.write_lat_mean_ms,
            "write_lat_p99_ms": self.write_lat_p99_ms,
            "total_errors": self.total_errors,
            "duration_seconds": self.duration_seconds,
            "passed": self.passed,
        }


@dataclass
class BenchmarkSuiteResult:
    full_write: Optional[FioJobMetrics] = None
    full_verify: Optional[FioJobMetrics] = None
    seq_read: Optional[FioJobMetrics] = None
    seq_write: Optional[FioJobMetrics] = None
    rand_mixed: Optional[FioJobMetrics] = None
    all_passed: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "full_write": self.full_write.to_dict() if self.full_write else None,
            "full_verify": self.full_verify.to_dict() if self.full_verify else None,
            "seq_read": self.seq_read.to_dict() if self.seq_read else None,
            "seq_write": self.seq_write.to_dict() if self.seq_write else None,
            "rand_mixed": self.rand_mixed.to_dict() if self.rand_mixed else None,
            "all_passed": self.all_passed,
        }


class FioRunner:
    def __init__(self, cmd_runner: Optional[Callable] = None):
        self.run_async = cmd_runner or run_command_async

    def parse_fio_output(self, job_name: str, output_text: str, duration: float) -> FioJobMetrics:
        """Parses fio JSON or formatted text output into structured metrics."""
        metrics = FioJobMetrics(job_name=job_name, duration_seconds=duration, raw_output=output_text)

        # Try to find JSON in the output (if dual format was used or pure json)
        json_obj = None
        m_json = re.search(r"(\{.*\})", output_text, re.DOTALL)
        if m_json:
            try:
                json_obj = json.loads(m_json.group(1))
            except Exception:
                pass

        if json_obj and "jobs" in json_obj and len(json_obj["jobs"]) > 0:
            job_data = json_obj["jobs"][0]
            metrics.total_errors = job_data.get("error", 0)

            # Read metrics
            read_data = job_data.get("read", {})
            metrics.read_bw_mb_s = round(read_data.get("bw_bytes", 0) / (1024 * 1024), 2)
            metrics.read_iops = round(read_data.get("iops", 0), 1)
            # Latency in ms (fio json returns clat_ns or lat_ns in nanoseconds)
            clat_read = read_data.get("clat_ns", read_data.get("lat_ns", {}))
            metrics.read_lat_mean_ms = round(clat_read.get("mean", 0) / 1_000_000, 3)
            # 99th percentile
            pct_read = clat_read.get("percentile", {})
            if "99.000000" in pct_read:
                metrics.read_lat_p99_ms = round(pct_read["99.000000"] / 1_000_000, 3)

            # Write metrics
            write_data = job_data.get("write", {})
            metrics.write_bw_mb_s = round(write_data.get("bw_bytes", 0) / (1024 * 1024), 2)
            metrics.write_iops = round(write_data.get("iops", 0), 1)
            clat_write = write_data.get("clat_ns", write_data.get("lat_ns", {}))
            metrics.write_lat_mean_ms = round(clat_write.get("mean", 0) / 1_000_000, 3)
            pct_write = clat_write.get("percentile", {})
            if "99.000000" in pct_write:
                metrics.write_lat_p99_ms = round(pct_write["99.000000"] / 1_000_000, 3)

            metrics.passed = metrics.total_errors == 0
            return metrics

        # Fallback Text Parsing
        for line in output_text.splitlines():
            # e.g., read: IOPS=512, BW=512MiB/s (537MB/s)(30.0GiB/60001msec)
            # or:   READ: bw=480MiB/s (503MB/s)
            m_read_bw = re.search(r"read:\s*IOPS=([0-9.kKmMgG]+),\s*BW=([^\s\(,]+)", line, re.IGNORECASE)
            if not m_read_bw:
                m_read_bw = re.search(r"READ:\s*bw=([^\s\(,]+)", line)
            if m_read_bw:
                if len(m_read_bw.groups()) >= 2:
                    metrics.read_iops = self._parse_iops_str(m_read_bw.group(1))
                    bw_str = m_read_bw.group(2)
                else:
                    bw_str = m_read_bw.group(1)
                metrics.read_bw_mb_s = self._parse_bandwidth_str(bw_str)

            m_write_bw = re.search(r"write:\s*IOPS=([0-9.kKmMgG]+),\s*BW=([^\s\(,]+)", line, re.IGNORECASE)
            if not m_write_bw:
                m_write_bw = re.search(r"WRITE:\s*bw=([^\s\(,]+)", line)
            if m_write_bw:
                if len(m_write_bw.groups()) >= 2:
                    metrics.write_iops = self._parse_iops_str(m_write_bw.group(1))
                    bw_str = m_write_bw.group(2)
                else:
                    bw_str = m_write_bw.group(1)
                metrics.write_bw_mb_s = self._parse_bandwidth_str(bw_str)

            # Check errors
            if "err=" in line:
                m_err = re.search(r"err=\s*([0-9]+)", line)
                if m_err:
                    metrics.total_errors = int(m_err.group(1))

        if "crc32c: failed" in output_text.lower() or "verify failed" in output_text.lower():
            metrics.total_errors = max(1, metrics.total_errors)

        metrics.passed = metrics.total_errors == 0
        return metrics

    def _parse_iops_str(self, s: str) -> float:
        """Converts IOPS strings like '122k', '512', '1.5M' to float."""
        s = s.strip()
        m = re.match(r"^([0-9.]+)\s*([A-Za-z]*)", s)
        if not m:
            return 0.0
        val = float(m.group(1))
        unit = m.group(2).lower()
        if "k" in unit:
            return round(val * 1000, 1)
        if "m" in unit:
            return round(val * 1000000, 1)
        return round(val, 1)

    def _parse_bandwidth_str(self, s: str) -> float:
        """Converts bandwidth strings like '480Mi', '500M', '1.2Gi', '800Ki' to MB/s."""
        s = s.strip()
        m = re.match(r"^([0-9.]+)\s*([A-Za-z]*)", s)
        if not m:
            return 0.0
        val = float(m.group(1))
        unit = m.group(2).lower()
        if "g" in unit:
            return round(val * 1024, 2)
        if "k" in unit:
            return round(val / 1024, 2)
        return round(val, 2)

    async def run_full_verify(
        self,
        disk_path: str,
        output_dir: Path,
        log_callback: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> Dict[str, FioJobMetrics]:
        """
        Executes:
        Phase 1: Full-device write with CRC32C verification headers.
        Phase 2: Full-device read verifying all CRC32C headers.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        write_log_path = output_dir / "full-write.txt"
        verify_log_path = output_dir / "full-verify.txt"

        if log_callback:
            log_callback(f"[INFO] Phase 1/2: Starting full-device destructive write with CRC32C headers on {disk_path}...")

        write_args = [
            "fio",
            "--name=full-write",
            f"--filename={disk_path}",
            "--direct=1",
            "--rw=write",
            "--bs=1M",
            "--iodepth=16",
            "--verify=crc32c",
            "--do_verify=0",
            "--verify_state_save=1",
            "--group_reporting",
            "--status-interval=5",
        ]

        res_write = await self.run_async(
            write_args,
            log_callback=log_callback,
            cancel_event=cancel_event,
        )
        write_log_path.write_text(res_write.combined_output)
        write_metrics = self.parse_fio_output("full-write", res_write.combined_output, res_write.duration_seconds)
        if not res_write.success:
            write_metrics.passed = False

        if not write_metrics.passed or (cancel_event and cancel_event.is_set()):
            if log_callback:
                log_callback(f"[ERROR] Full-device write phase failed or was cancelled (exit code {res_write.exit_code})")
            return {"write": write_metrics, "verify": FioJobMetrics(job_name="full-verify", passed=False)}

        if log_callback:
            log_callback(f"[INFO] Phase 2/2: Starting full-device verification read on {disk_path}...")

        verify_args = [
            "fio",
            "--name=full-verify",
            f"--filename={disk_path}",
            "--direct=1",
            "--rw=read",
            "--bs=1M",
            "--iodepth=16",
            "--verify=crc32c",
            "--verify_only=1",
            "--group_reporting",
            "--status-interval=5",
        ]

        res_verify = await self.run_async(
            verify_args,
            log_callback=log_callback,
            cancel_event=cancel_event,
        )
        verify_log_path.write_text(res_verify.combined_output)
        verify_metrics = self.parse_fio_output("full-verify", res_verify.combined_output, res_verify.duration_seconds)
        if not res_verify.success:
            verify_metrics.passed = False

        if log_callback:
            if verify_metrics.passed:
                log_callback(f"[SUCCESS] Full-device CRC verification completed successfully with 0 errors on {disk_path}")
            else:
                log_callback(f"[ERROR] Full-device CRC verification FAILED on {disk_path} (Errors: {verify_metrics.total_errors})")

        return {"write": write_metrics, "verify": verify_metrics}

    async def run_benchmarks(
        self,
        disk_path: str,
        output_dir: Path,
        log_callback: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> BenchmarkSuiteResult:
        """
        Runs performance benchmarks:
        1. Sequential Read (1M, iodepth=32, 60s)
        2. Sequential Write (1M, iodepth=32, 60s)
        3. 4K Random Mixed (4k, iodepth=32, 70% read / 30% write, 120s)
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        suite = BenchmarkSuiteResult()

        # 1. Sequential Read
        if log_callback:
            log_callback("[INFO] Starting Sequential Read benchmark (1MB, iodepth=32, 60s)...")
        seq_read_args = [
            "fio",
            "--name=seq-read",
            f"--filename={disk_path}",
            "--direct=1",
            "--rw=read",
            "--bs=1M",
            "--iodepth=32",
            "--numjobs=1",
            "--runtime=60",
            "--time_based",
            "--group_reporting",
            "--status-interval=5",
        ]
        res_sr = await self.run_async(seq_read_args, log_callback=log_callback, cancel_event=cancel_event)
        (output_dir / "seq-read.txt").write_text(res_sr.combined_output)
        suite.seq_read = self.parse_fio_output("seq-read", res_sr.combined_output, res_sr.duration_seconds)
        if not res_sr.success:
            suite.seq_read.passed = False
            suite.all_passed = False

        if cancel_event and cancel_event.is_set():
            return suite

        # 2. Sequential Write
        if log_callback:
            log_callback("[INFO] Starting Sequential Write benchmark (1MB, iodepth=32, 60s)...")
        seq_write_args = [
            "fio",
            "--name=seq-write",
            f"--filename={disk_path}",
            "--direct=1",
            "--rw=write",
            "--bs=1M",
            "--iodepth=32",
            "--numjobs=1",
            "--runtime=60",
            "--time_based",
            "--group_reporting",
            "--status-interval=5",
        ]
        res_sw = await self.run_async(seq_write_args, log_callback=log_callback, cancel_event=cancel_event)
        (output_dir / "seq-write.txt").write_text(res_sw.combined_output)
        suite.seq_write = self.parse_fio_output("seq-write", res_sw.combined_output, res_sw.duration_seconds)
        if not res_sw.success:
            suite.seq_write.passed = False
            suite.all_passed = False

        if cancel_event and cancel_event.is_set():
            return suite

        # 3. 4K Random Mixed
        if log_callback:
            log_callback("[INFO] Starting 4K Random Mixed benchmark (4K, 70/30 read/write, iodepth=32, 120s)...")
        rand_mix_args = [
            "fio",
            "--name=rand-mixed",
            f"--filename={disk_path}",
            "--direct=1",
            "--rw=randrw",
            "--rwmixread=70",
            "--bs=4k",
            "--iodepth=32",
            "--numjobs=1",
            "--runtime=120",
            "--time_based",
            "--group_reporting",
            "--status-interval=5",
        ]
        res_rm = await self.run_async(rand_mix_args, log_callback=log_callback, cancel_event=cancel_event)
        (output_dir / "rand-mixed.txt").write_text(res_rm.combined_output)
        suite.rand_mixed = self.parse_fio_output("rand-mixed", res_rm.combined_output, res_rm.duration_seconds)
        if not res_rm.success:
            suite.rand_mixed.passed = False
            suite.all_passed = False

        if log_callback:
            log_callback("[INFO] Benchmarking suite completed.")
            if suite.seq_read:
                log_callback(f"[STATS] Seq Read: {suite.seq_read.read_bw_mb_s} MB/s ({suite.seq_read.read_iops} IOPS)")
            if suite.seq_write:
                log_callback(f"[STATS] Seq Write: {suite.seq_write.write_bw_mb_s} MB/s ({suite.seq_write.write_iops} IOPS)")
            if suite.rand_mixed:
                log_callback(f"[STATS] 4K Mixed: Read {suite.rand_mixed.read_iops} IOPS ({suite.rand_mixed.read_bw_mb_s} MB/s) | Write {suite.rand_mixed.write_iops} IOPS ({suite.rand_mixed.write_bw_mb_s} MB/s)")

        return suite


fio_runner = FioRunner()
