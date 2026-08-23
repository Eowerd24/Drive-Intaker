import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.core.disk_detector import DriveInfo, disk_detector
from app.core.intake_job import intake_job_manager

@pytest.mark.asyncio
async def test_api_system_info():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/api/system")
        assert res.status_code == 200
        data = res.json()
        assert "hostname" in data
        assert "protected_system_disks" in data


@pytest.mark.asyncio
async def test_api_list_drives(monkeypatch):
    mock_drives = [
        DriveInfo(
            name="sdb",
            path="/dev/sdb",
            canonical_path="/dev/sdb",
            model="SAMSUNG MZ7KM960HMJP-00005",
            serial="S2HCNX0H123456",
            size_str="960 GB",
            size_bytes=960000000000,
            transport="sata",
            vendor="SAMSUNG",
            firmware="GXM5104Q",
            is_ssd=True,
            smart_health="PASSED",
            power_on_hours=5000,
            temperature_celsius=27,
            wear_remaining_percentage=98,
            is_system_disk=False,
            is_locked=False,
            is_eligible=True,
            status_badge="Ready",
        )
    ]
    monkeypatch.setattr(disk_detector, "detect_all_drives", lambda: mock_drives)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get("/api/drives")
        assert res.status_code == 200
        data = res.json()
        assert len(data) == 1
        assert data[0]["serial"] == "S2HCNX0H123456"
        assert data[0]["is_eligible"] is True


@pytest.mark.asyncio
async def test_api_reject_destructive_without_serial(monkeypatch):
    mock_drives = [
        DriveInfo(
            name="sdb",
            path="/dev/sdb",
            canonical_path="/dev/sdb",
            model="SAMSUNG MZ7KM960HMJP-00005",
            serial="S2HCNX0H123456",
            size_str="960 GB",
            size_bytes=960000000000,
            transport="sata",
            vendor="SAMSUNG",
            firmware="GXM5104Q",
            is_ssd=True,
            smart_health="PASSED",
            power_on_hours=5000,
            temperature_celsius=27,
            wear_remaining_percentage=98,
            is_system_disk=False,
            is_locked=False,
            is_eligible=True,
            status_badge="Ready",
        )
    ]
    monkeypatch.setattr(disk_detector, "detect_all_drives", lambda: mock_drives)
    monkeypatch.setattr(disk_detector, "get_drive", lambda p: mock_drives[0] if "sdb" in p else None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # Destructive job without correct serial
        res = await ac.post("/api/jobs", json={
            "target_disk": "/dev/sdb",
            "workflow_mode": "full",
            "entered_serial": "WRONG_SERIAL"
        })
        assert res.status_code == 400
        data = res.json()
        assert "Safety checks failed" in data["detail"]["message"]


@pytest.mark.asyncio
async def test_api_concurrency_lock():
    """Test that starting a second job when one is running returns 409 Conflict."""
    from app.core.intake_job import JobStatus
    intake_job_manager.current_status = JobStatus(
        job_id="test-run-123",
        target_disk="/dev/sdb",
        status="RUNNING",
        current_stage="WIPE",
        progress_percent=25
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.post("/api/jobs", json={
            "target_disk": "/dev/sdc",
            "workflow_mode": "inventory"
        })
        assert res.status_code == 409
        data = res.json()
        assert "already running" in data["detail"]

    # Reset
    intake_job_manager.current_status = None


@pytest.mark.asyncio
async def test_api_report_classification(tmp_path, monkeypatch):
    """Test updating classification via REST endpoint."""
    from app.core.reporter import ReportGenerator, IntakeRunRecord, report_generator
    custom_reporter = ReportGenerator(root_dir=tmp_path)
    monkeypatch.setattr("app.main.report_generator", custom_reporter)
    
    run_id, run_dir = custom_reporter.create_run_directory("SERIAL999")
    rec = IntakeRunRecord(
        run_id=run_id,
        target_device="/dev/sdb",
        model="Samsung 960",
        serial="SERIAL999",
        capacity="960G",
        transport="sata",
        created_at="2026-08-22 12:00:00",
        status="COMPLETED"
    )
    custom_reporter.save_run_metadata(run_dir, rec)
    custom_reporter.generate_markdown_report(run_dir, rec)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.post(f"/api/reports/{run_id}/classify", json={
            "classification": "PASS-A",
            "notes": "Excellent performance"
        })
        assert res.status_code == 200
        assert res.json()["success"] is True

        # Verify invalid classification is rejected
        res_bad = await ac.post(f"/api/reports/{run_id}/classify", json={
            "classification": "INVALID_GRADE",
            "notes": ""
        })
        assert res_bad.status_code == 400


@pytest.mark.asyncio
async def test_api_delete_and_purge_reports(tmp_path, monkeypatch):
    """Test DELETE /api/reports/{run_id} and POST /api/reports/purge."""
    from app.core.reporter import ReportGenerator, IntakeRunRecord
    custom_reporter = ReportGenerator(root_dir=tmp_path)
    monkeypatch.setattr("app.main.report_generator", custom_reporter)

    run_id1, _ = custom_reporter.create_run_directory("DEL1")
    run_id2, _ = custom_reporter.create_run_directory("DEL2")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # Delete run_id1
        res_del = await ac.delete(f"/api/reports/{run_id1}")
        assert res_del.status_code == 200
        assert res_del.json()["success"] is True
        assert not (tmp_path / run_id1).exists()

        # Purge remaining
        res_purge = await ac.post("/api/reports/purge")
        assert res_purge.status_code == 200
        assert res_purge.json()["deleted_count"] >= 1
        assert not (tmp_path / run_id2).exists()


@pytest.mark.asyncio
async def test_api_lock_drive(tmp_path, monkeypatch):
    """Test POST /api/drives/{drive_name}/lock permanently locks the drive."""
    from app.core.safety import SafetyValidator, safety_validator
    from app.core.disk_detector import DriveInfo, disk_detector

    lock_file = tmp_path / "locked_disks.json"
    monkeypatch.setattr(SafetyValidator, "locked_disks_file", property(lambda self: lock_file))

    dummy_drive = DriveInfo(
        name="sdb",
        path="/dev/sdb",
        canonical_path="/dev/sdb",
        model="Test Model",
        serial="SER123",
        size_str="500G",
        size_bytes=500000000000,
        transport="sata",
        vendor="TestVendor",
        firmware="1.0",
        is_ssd=True,
        smart_health="PASSED",
        power_on_hours=100,
        temperature_celsius=30,
        wear_remaining_percentage=99,
        is_system_disk=False,
        is_locked=False,
        is_eligible=True,
        status_badge="Ready",
    )
    monkeypatch.setattr(disk_detector, "get_drive", lambda name: dummy_drive)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.post("/api/drives/sdb/lock")
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert "permanently locked" in data["message"]
        assert safety_validator.is_disk_locked("/dev/sdb") is True


def test_runner_sudo_resolution(monkeypatch):
    """Test that privileged commands get prefixed with sudo -n when use_sudo is true and non-root."""
    from app.config import settings
    from app.core.runner import resolve_command_args
    import os

    # When use_sudo is False
    monkeypatch.setattr(settings, "use_sudo", False)
    assert resolve_command_args(["smartctl", "-x", "/dev/sdb"]) == ["smartctl", "-x", "/dev/sdb"]

    # When use_sudo is True and euid != 0
    monkeypatch.setattr(settings, "use_sudo", True)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert resolve_command_args(["smartctl", "-x", "/dev/sdb"]) == ["sudo", "-n", "smartctl", "-x", "/dev/sdb"]
    assert resolve_command_args(["wipefs", "-a", "/dev/sdb"]) == ["sudo", "-n", "wipefs", "-a", "/dev/sdb"]
    # Non-privileged command shouldn't be prefixed
    assert resolve_command_args(["lsblk", "-J"]) == ["lsblk", "-J"]
    # Already sudo shouldn't be double prefixed
    assert resolve_command_args(["sudo", "smartctl"]) == ["sudo", "smartctl"]


@pytest.mark.asyncio
async def test_api_manual_smart_save_and_clear(tmp_path, monkeypatch):
    """Test POST /api/drives/{drive_name}/smart/manual and DELETE endpoint."""
    from app.config import settings
    from app.core.disk_detector import DriveInfo, disk_detector

    monkeypatch.setattr(settings, "reports_dir", tmp_path)

    dummy_drive = DriveInfo(
        name="sdb",
        path="/dev/sdb",
        canonical_path="/dev/sdb",
        model="Test Model",
        serial="SER123",
        size_str="500G",
        size_bytes=500000000000,
        transport="sata",
        vendor="TestVendor",
        firmware="1.0",
        is_ssd=True,
        smart_health="UNKNOWN",
        power_on_hours=None,
        temperature_celsius=None,
        wear_remaining_percentage=None,
        is_system_disk=False,
        is_locked=False,
        is_eligible=True,
        status_badge="Ready",
    )
    monkeypatch.setattr(disk_detector, "get_drive", lambda name: dummy_drive)

    sample_smart_output = """=== START OF READ SMART DATA SECTION ===
SMART overall-health self-assessment test result: PASSED

ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE
  5 Reallocated_Sector_Ct   0x0033   100   100   010    Pre-fail  Always       -       0
  9 Power_On_Hours          0x0032   095   095   000    Old_age   Always       -       4321
194 Temperature_Celsius     0x0022   068   050   000    Old_age   Always       -       32
231 SSD_Life_Left           0x0013   096   096   010    Pre-fail  Always       -       96
"""

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # 1. Save manual SMART
        res = await ac.post("/api/drives/sdb/smart/manual", json={
            "smart_text": sample_smart_output
        })
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert data["smart_report"]["health_status_str"] == "PASSED"
        assert data["smart_report"]["power_on_hours"] == 4321
        assert data["smart_report"]["temperature_celsius"] == 32
        assert data["smart_report"]["wear_remaining_percentage"] == 96

        # Check that manual SMART is saved
        saved = disk_detector.get_manual_smart("/dev/sdb")
        assert saved is not None
        assert "PASSED" in saved

        # 2. Clear manual SMART
        res_del = await ac.delete("/api/drives/sdb/smart/manual")
        assert res_del.status_code == 200
        assert res_del.json()["success"] is True
        assert disk_detector.get_manual_smart("/dev/sdb") is None




