import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import settings

logger = logging.getLogger("ssd_intake.reporter")

def safe_id_string(val: str) -> str:
    """Replaces unsafe filesystem characters with underscores."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", val)


@dataclass
class IntakeRunRecord:
    run_id: str
    target_device: str
    model: str
    serial: str
    capacity: str
    transport: str
    created_at: str
    completed_at: Optional[str] = None
    status: str = "PENDING"  # PENDING, RUNNING, COMPLETED, FAILED, CANCELLED
    mode: str = "full"  # full, inventory, custom
    classification: Optional[str] = None  # PASS-A, PASS-B, LAB, REJECT
    technician_notes: str = ""
    firmware_before: str = ""
    firmware_after: str = ""
    firmware_status: str = "Not checked"
    firmware_updatable: bool = False
    firmware_update_available: bool = False
    protected_system_disk: str = ""
    stages: Dict[str, str] = field(default_factory=dict)
    smart_before: Optional[Dict[str, Any]] = None
    smart_after: Optional[Dict[str, Any]] = None
    smart_diff: Optional[Dict[str, Any]] = None
    benchmarks: Optional[Dict[str, Any]] = None
    full_verify: Optional[Dict[str, Any]] = None
    errors: List[str] = field(default_factory=list)
    raw_files: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ReportGenerator:
    def __init__(self, root_dir: Optional[Path] = None):
        self.root_dir = root_dir or settings.reports_dir
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def create_run_directory(self, serial: str) -> tuple[str, Path]:
        """Creates timestamped run directory /root/ssd-intake/YYYYMMDD-HHMMSS-SERIAL/."""
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_serial = safe_id_string(serial or "unknown-serial")
        run_id = f"{timestamp}-{safe_serial}"
        run_dir = self.root_dir / run_id
        
        for subdir in ["before", "after", "tests", "benchmarks", "firmware"]:
            (run_dir / subdir).mkdir(parents=True, exist_ok=True)

        # Update 'latest' symlink
        latest_link = self.root_dir / "latest"
        try:
            if latest_link.is_symlink() or latest_link.exists():
                latest_link.unlink()
            latest_link.symlink_to(run_dir, target_is_directory=True)
        except Exception as e:
            logger.warning(f"Could not update 'latest' symlink: {e}")

        return run_id, run_dir

    def save_run_metadata(self, run_dir: Path, record: IntakeRunRecord) -> None:
        """Persists structured metadata to run.json."""
        metadata_file = run_dir / "run.json"
        try:
            metadata_file.write_text(json.dumps(record.to_dict(), indent=2))
        except Exception as e:
            logger.error(f"Failed to save run.json to {run_dir}: {e}")

    def load_run_metadata(self, run_id: str) -> Optional[IntakeRunRecord]:
        """Loads structured run metadata from run.json."""
        run_dir = self.root_dir / run_id
        metadata_file = run_dir / "run.json"
        if not metadata_file.exists():
            return None
        try:
            data = json.loads(metadata_file.read_text())
            return IntakeRunRecord(**data)
        except Exception as e:
            logger.error(f"Error loading run record {run_id}: {e}")
            return None

    def list_all_runs(self) -> List[IntakeRunRecord]:
        """Discovers and loads all historical intake runs."""
        runs: List[IntakeRunRecord] = []
        if not self.root_dir.exists():
            return runs

        for entry in sorted(self.root_dir.iterdir(), reverse=True):
            if entry.is_dir() and entry.name != "latest":
                meta = self.load_run_metadata(entry.name)
                if meta:
                    runs.append(meta)
                elif (entry / "REPORT.md").exists():
                    # Fallback for runs without run.json
                    runs.append(
                        IntakeRunRecord(
                            run_id=entry.name,
                            target_device="Unknown",
                            model="Legacy run",
                            serial=entry.name.split("-", 2)[-1] if "-" in entry.name else entry.name,
                            capacity="Unknown",
                            transport="sata",
                            created_at=entry.name[:15] if len(entry.name) >= 15 else "Unknown",
                            status="COMPLETED",
                        )
                    )
        return runs

    def update_classification(self, run_id: str, classification: str, notes: str = "") -> bool:
        """Updates classification in both run.json and REPORT.md."""
        record = self.load_run_metadata(run_id)
        if not record:
            return False
        
        record.classification = classification
        if notes:
            record.technician_notes = notes
        
        run_dir = self.root_dir / run_id
        self.save_run_metadata(run_dir, record)
        self.generate_markdown_report(run_dir, record)
        return True

    def delete_run(self, run_id: str) -> bool:
        """Deletes a specific run directory and clears the 'latest' symlink if pointing to it."""
        safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", run_id)
        run_dir = self.root_dir / safe_id
        if not run_dir.exists() or not run_dir.is_dir():
            return False

        try:
            import shutil
            shutil.rmtree(run_dir)
            
            # Check latest symlink
            latest_link = self.root_dir / "latest"
            if latest_link.is_symlink():
                try:
                    target = os.path.realpath(str(latest_link))
                    if not os.path.exists(target):
                        latest_link.unlink()
                except Exception:
                    pass
            logger.info(f"Deleted report run: {safe_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete report run {safe_id}: {e}")
            return False

    def purge_all_runs(self, active_job_id: Optional[str] = None) -> int:
        """Deletes all historical run directories and logs from the reports root."""
        deleted_count = 0
        if not self.root_dir.exists():
            return 0

        import shutil
        for entry in list(self.root_dir.iterdir()):
            if entry.name == "latest":
                try:
                    entry.unlink()
                except Exception:
                    pass
                continue

            if active_job_id and entry.name == active_job_id:
                logger.info(f"Skipping active running job {entry.name} during purge")
                continue

            if entry.is_dir():
                try:
                    shutil.rmtree(entry)
                    deleted_count += 1
                except Exception as e:
                    logger.error(f"Failed to delete {entry}: {e}")

        logger.info(f"Purged {deleted_count} historical report folders.")
        return deleted_count

    def generate_markdown_report(self, run_dir: Path, record: IntakeRunRecord) -> str:
        """
        Renders the canonical REPORT.md file matching and enhancing the shell script report.
        """
        smart_after = record.smart_after or {}
        smart_before = record.smart_before or {}
        benchmarks = record.benchmarks or {}
        full_verify = record.full_verify or {}

        health = smart_after.get("health_status_str", "Unknown")
        poh = smart_after.get("power_on_hours", "Unknown")
        realloc = smart_after.get("reallocated_sectors", 0)
        uncorrect = smart_after.get("uncorrectable_errors", 0)
        crc = smart_after.get("crc_errors", 0)
        temp = f"{smart_after.get('temperature_celsius', 'Unknown')} °C"
        wear_remain = (
            f"{smart_after.get('wear_remaining_percentage')}%"
            if smart_after.get("wear_remaining_percentage") is not None
            else "Unknown"
        )
        tbw = f"{smart_after.get('tbw_terabytes', 'Unknown')} TB"

        # Extract self-test log extract if available
        selftest_log_path = run_dir / "after" / "smart.txt"
        selftests_text = "No self-test extract available."
        if selftest_log_path.exists():
            txt = selftest_log_path.read_text(errors="replace")
            m = re.search(r"(SMART Extended Self-test Log.*?)(?:SMART Selective|\Z)", txt, re.DOTALL)
            if m:
                selftests_text = "\n".join(m.group(1).splitlines()[:25])

        # Classification checkmarks
        cls = record.classification or ""
        pass_a_box = "[x]" if cls == "PASS-A" else "[ ]"
        pass_b_box = "[x]" if cls == "PASS-B" else "[ ]"
        lab_box = "[x]" if cls == "LAB" else "[ ]"
        reject_box = "[x]" if cls == "REJECT" else "[ ]"

        # Benchmarks extract
        bench_lines = []
        if full_verify:
            wr = full_verify.get("write", {})
            vf = full_verify.get("verify", {})
            v_status = "PASSED (0 errors)" if vf.get("passed") else f"FAILED ({vf.get('total_errors', 1)} errors)"
            bench_lines.append(f"- **Full Write + CRC Verification**: `{v_status}` (Write: {wr.get('write_bw_mb_s', 0)} MB/s, Verify: {vf.get('read_bw_mb_s', 0)} MB/s)")
        
        if benchmarks:
            sr = benchmarks.get("seq_read")
            if sr:
                bench_lines.append(f"- **Sequential Read**: `{sr.get('read_bw_mb_s')} MB/s` ({sr.get('read_iops')} IOPS, mean lat: {sr.get('read_lat_mean_ms')} ms)")
            sw = benchmarks.get("seq_write")
            if sw:
                bench_lines.append(f"- **Sequential Write**: `{sw.get('write_bw_mb_s')} MB/s` ({sw.get('write_iops')} IOPS, mean lat: {sw.get('write_lat_mean_ms')} ms)")
            rm = benchmarks.get("rand_mixed")
            if rm:
                bench_lines.append(f"- **4K Random Mixed (70/30)**: `Read {rm.get('read_iops')} IOPS ({rm.get('read_bw_mb_s')} MB/s) | Write {rm.get('write_iops')} IOPS ({rm.get('write_bw_mb_s')} MB/s)`")

        bench_summary = "\n".join(bench_lines) if bench_lines else "Benchmarks skipped or not executed."

        report_content = f"""# SSD Intake Report

- **Run ID**: `{record.run_id}`
- **Date**: {record.created_at}
- **Device during test**: `{record.target_device}`
- **Model**: `{record.model}`
- **Serial**: `{record.serial}`
- **Capacity**: `{record.capacity}`
- **Transport**: `{record.transport.upper()}`
- **Firmware before**: `{record.firmware_before}`
- **Firmware after**: `{record.firmware_after or record.firmware_before}`
- **Firmware status**: {record.firmware_status}
- **Protected system disk**: `{record.protected_system_disk}`
- **Execution Status**: `{record.status}`

---

## Final SMART Summary

- **Overall health**: `{health}`
- **Power-on hours**: `{poh}`
- **Wear remaining**: `{wear_remain}`
- **Total Data Written (TBW)**: `{tbw}`
- **Reallocated sectors**: `{realloc}`
- **Reported uncorrectable errors**: `{uncorrect}`
- **UDMA CRC errors**: `{crc}`
- **Temperature**: `{temp}`

---

## Performance & Verification Summary

{bench_summary}

---

## SMART Self-Test Log Extract

```text
{selftests_text}
```

---

## Output Artifacts

- `before/`: Initial SMART, hdparm, udev, and lsblk snapshots
- `after/`: Final SMART and post-test state
- `tests/`: SMART self-test logs and full-device write+verify logs
- `benchmarks/`: Sequential and 4K mixed fio benchmark outputs
- `firmware/`: `fwupdmgr` refresh, get-devices, and get-updates data

---

## Manual Classification

- {pass_a_box} **PASS-A** — Clean test results, 0 uncorrectable/reallocated defects, high health, low wear
- {pass_b_box} **PASS-B** — Fully functional, minor wear or older power-on hours, zero uncorrectable errors
- {lab_box} **LAB** — Usable for disposable testing, non-critical scratch workloads only
- {reject_box} **REJECT** — Defective, failed self-test, CRC/interface faults, or increasing defect counts

**Technician Notes**:
{record.technician_notes or "None recorded."}
"""
        report_file = run_dir / "REPORT.md"
        report_file.write_text(report_content)
        return report_content


report_generator = ReportGenerator()
