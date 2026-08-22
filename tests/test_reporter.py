import pytest
import os
import shutil
from pathlib import Path
from app.core.reporter import IntakeRunRecord, ReportGenerator

def test_report_generation(tmp_path):
    reporter = ReportGenerator(root_dir=tmp_path)
    run_id, run_dir = reporter.create_run_directory("TEST123456")

    assert run_dir.exists()
    assert (tmp_path / "latest").is_symlink()

    record = IntakeRunRecord(
        run_id=run_id,
        target_device="/dev/sdb",
        model="SAMSUNG MZ7KM960HMJP-00005",
        serial="TEST123456",
        capacity="960 GB",
        transport="sata",
        created_at="2026-08-22 12:00:00",
        status="COMPLETED",
        firmware_before="GXM5104Q",
        firmware_after="GXM5104Q",
        firmware_status="Updatable device; no update offered by enabled fwupd remotes",
        protected_system_disk="/dev/sda",
        smart_after={
            "health_status_str": "PASSED",
            "power_on_hours": 12000,
            "wear_remaining_percentage": 97,
            "reallocated_sectors": 0,
            "uncorrectable_errors": 0,
            "crc_errors": 0,
            "temperature_celsius": 29,
        },
    )

    reporter.save_run_metadata(run_dir, record)
    md_content = reporter.generate_markdown_report(run_dir, record)

    assert "# SSD Intake Report" in md_content
    assert "SAMSUNG MZ7KM960HMJP-00005" in md_content
    assert "TEST123456" in md_content
    assert "97%" in md_content

    # Test classification update
    success = reporter.update_classification(run_id, "PASS-A", "Passed enterprise certification tests")
    assert success is True

    updated_record = reporter.load_run_metadata(run_id)
    assert updated_record.classification == "PASS-A"
    assert updated_record.technician_notes == "Passed enterprise certification tests"

    # Check updated markdown has [x] PASS-A
    updated_md = (run_dir / "REPORT.md").read_text()
    assert "- [x] **PASS-A**" in updated_md


def test_delete_run(tmp_path):
    reporter = ReportGenerator(root_dir=tmp_path)
    run_id, run_dir = reporter.create_run_directory("SERIAL_DEL")
    assert run_dir.exists()

    deleted = reporter.delete_run(run_id)
    assert deleted is True
    assert not run_dir.exists()


def test_purge_all_runs(tmp_path):
    reporter = ReportGenerator(root_dir=tmp_path)
    run_id1, _ = reporter.create_run_directory("SERIAL_1")
    run_id2, _ = reporter.create_run_directory("SERIAL_2")
    run_id3, _ = reporter.create_run_directory("SERIAL_ACTIVE")

    # Purge all except SERIAL_ACTIVE
    count = reporter.purge_all_runs(active_job_id=run_id3)
    assert count == 2

    # Verify SERIAL_ACTIVE still exists
    assert (tmp_path / run_id3).exists()
    assert not (tmp_path / run_id1).exists()
    assert not (tmp_path / run_id2).exists()

