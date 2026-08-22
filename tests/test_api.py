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


