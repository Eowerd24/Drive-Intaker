import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.config import settings
from app.core.disk_detector import disk_detector
from app.core.intake_job import JobConfig, intake_job_manager
from app.core.reporter import report_generator
from app.core.runner import run_command_sync
from app.core.safety import safety_validator

# Setup logging
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger("ssd_intake.app")

settings.ensure_directories()

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Proxmox SSD Intake Station - Web Management GUI",
)

# Static and Templates
BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# --- Pydantic Request Models ---
class JobStartRequest(BaseModel):
    target_disk: str
    workflow_mode: str = "full"  # full, inventory, custom
    entered_serial: Optional[str] = None
    skip_firmware: bool = False
    skip_long_smart: bool = False
    skip_full_verify: bool = False
    skip_bench: bool = False


class ClassificationRequest(BaseModel):
    classification: str  # PASS-A, PASS-B, LAB, REJECT
    notes: Optional[str] = ""


class ManualSmartRequest(BaseModel):
    smart_text: str


# --- Web UI Routes ---

@app.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    hostname = disk_detector.get_hostname()
    drives = disk_detector.detect_all_drives()
    protected_disks = safety_validator.detect_system_disks()
    recent_runs = report_generator.list_all_runs()[:5]
    
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "hostname": hostname,
            "drives": drives,
            "protected_disks": list(protected_disks),
            "recent_runs": recent_runs,
            "active_job": intake_job_manager.current_status,
            "settings": settings,
        },
    )


@app.get("/drives/{drive_name}", response_class=HTMLResponse)
async def drive_detail_page(request: Request, drive_name: str):
    drive = disk_detector.get_drive(drive_name)
    if not drive:
        raise HTTPException(status_code=404, detail=f"Drive '{drive_name}' not found.")

    protected_disks = safety_validator.detect_system_disks()
    safety_check = safety_validator.validate_candidate(
        target_disk=drive.canonical_path,
        is_destructive=False,
        custom_system_disks=protected_disks,
    )

    # Collect raw outputs for collapsible inspection
    raw_smart = run_command_sync(["smartctl", "-x", drive.canonical_path]).combined_output
    raw_hdparm = run_command_sync(["hdparm", "-I", drive.canonical_path]).combined_output
    raw_udev = run_command_sync(["udevadm", "info", "--query=property", f"--name={drive.canonical_path}"]).combined_output
    raw_lsblk = run_command_sync(["lsblk", "-O", drive.canonical_path]).combined_output

    manual_smart = disk_detector.get_manual_smart(drive.canonical_path)
    manual_commands = {
        "full": f"sudo smartctl -x {drive.canonical_path}",
        "health": f"sudo smartctl -H {drive.canonical_path}",
        "short_test": f"sudo smartctl -t short {drive.canonical_path}",
        "long_test": f"sudo smartctl -t long {drive.canonical_path}",
        "selftest_log": f"sudo smartctl -l selftest {drive.canonical_path}",
        "identify": f"sudo hdparm -I {drive.canonical_path}",
    }

    return templates.TemplateResponse(
        request=request,
        name="drive_detail.html",
        context={
            "drive": drive,
            "safety": safety_check,
            "protected_disks": list(protected_disks),
            "raw_smart": raw_smart,
            "raw_hdparm": raw_hdparm,
            "raw_udev": raw_udev,
            "raw_lsblk": raw_lsblk,
            "manual_smart": manual_smart,
            "manual_commands": manual_commands,
            "settings": settings,
        },
    )


@app.get("/intake", response_class=HTMLResponse)
async def intake_setup_page(request: Request, disk: Optional[str] = None):
    drives = disk_detector.detect_all_drives()
    selected_drive = None
    if disk:
        selected_drive = disk_detector.get_drive(disk)

    protected_disks = safety_validator.detect_system_disks()
    
    return templates.TemplateResponse(
        request=request,
        name="intake.html",
        context={
            "drives": drives,
            "selected_drive": selected_drive,
            "protected_disks": list(protected_disks),
            "active_job": intake_job_manager.current_status,
            "settings": settings,
        },
    )


@app.get("/jobs/current", response_class=HTMLResponse)
async def job_progress_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="job_progress.html",
        context={
            "active_job": intake_job_manager.current_status,
            "log_history": intake_job_manager.log_history,
            "settings": settings,
        },
    )


@app.get("/reports", response_class=HTMLResponse)
async def reports_list_page(request: Request):
    runs = report_generator.list_all_runs()
    return templates.TemplateResponse(
        request=request,
        name="reports.html",
        context={
            "runs": runs,
            "settings": settings,
        },
    )


@app.get("/reports/{run_id}", response_class=HTMLResponse)
async def report_detail_page(request: Request, run_id: str):
    record = report_generator.load_run_metadata(run_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Report run '{run_id}' not found.")

    run_dir = settings.reports_dir / run_id
    report_md_path = run_dir / "REPORT.md"
    report_md_content = report_md_path.read_text(errors="replace") if report_md_path.exists() else "REPORT.md not found"

    # List artifact files available
    artifacts = {}
    for cat in ["before", "after", "tests", "benchmarks", "firmware"]:
        cat_dir = run_dir / cat
        if cat_dir.exists():
            artifacts[cat] = [f.name for f in cat_dir.iterdir() if f.is_file()]

    return templates.TemplateResponse(
        request=request,
        name="report_detail.html",
        context={
            "record": record,
            "report_md": report_md_content,
            "artifacts": artifacts,
            "settings": settings,
        },
    )


# --- REST API Endpoints ---

@app.get("/api/system")
async def api_system_info():
    hostname = disk_detector.get_hostname()
    protected_disks = list(safety_validator.detect_system_disks())
    return {
        "hostname": hostname,
        "app_name": settings.app_name,
        "version": settings.app_version,
        "protected_system_disks": protected_disks,
        "reports_dir": str(settings.reports_dir),
    }


@app.get("/api/drives")
async def api_list_drives():
    drives = disk_detector.detect_all_drives()
    return [d.to_dict() for d in drives]


@app.get("/api/drives/{drive_name}")
async def api_get_drive(drive_name: str):
    drive = disk_detector.get_drive(drive_name)
    if not drive:
        raise HTTPException(status_code=404, detail=f"Drive '{drive_name}' not found.")
    return drive.to_dict()


@app.post("/api/drives/{drive_name}/lock")
async def api_lock_drive(drive_name: str):
    drive = disk_detector.get_drive(drive_name)
    if not drive:
        raise HTTPException(status_code=404, detail=f"Drive '{drive_name}' not found.")
    
    locked = safety_validator.lock_disk(drive.canonical_path, note="Permanently locked via Web GUI")
    if not locked:
        raise HTTPException(status_code=500, detail="Failed to persist disk lock.")
    
    updated_drive = disk_detector.get_drive(drive_name)
    return {
        "success": True,
        "message": f"Drive '{drive.canonical_path}' is now permanently locked against data destruction.",
        "drive": updated_drive.to_dict() if updated_drive else None,
    }


@app.post("/api/drives/{drive_name}/smart/manual")
async def api_save_manual_smart(drive_name: str, req: ManualSmartRequest):
    drive = disk_detector.get_drive(drive_name)
    if not drive:
        raise HTTPException(status_code=404, detail=f"Drive '{drive_name}' not found.")
    
    if not req.smart_text.strip():
        raise HTTPException(status_code=400, detail="SMART text cannot be empty.")
    
    report = disk_detector.save_manual_smart(drive.canonical_path, req.smart_text)
    updated_drive = disk_detector.get_drive(drive_name)
    
    return {
        "success": True,
        "message": f"Manual SMART output parsed and saved for {drive.canonical_path}.",
        "smart_report": report.to_dict(),
        "drive": updated_drive.to_dict() if updated_drive else None,
    }


@app.delete("/api/drives/{drive_name}/smart/manual")
async def api_delete_manual_smart(drive_name: str):
    drive = disk_detector.get_drive(drive_name)
    if not drive:
        raise HTTPException(status_code=404, detail=f"Drive '{drive_name}' not found.")
    
    deleted = disk_detector.delete_manual_smart(drive.canonical_path)
    updated_drive = disk_detector.get_drive(drive_name)
    return {
        "success": True,
        "message": f"Manual SMART data cleared for {drive.canonical_path}.",
        "deleted": deleted,
        "drive": updated_drive.to_dict() if updated_drive else None,
    }



@app.post("/api/jobs")
async def api_start_job(req: JobStartRequest):
    if intake_job_manager.is_running:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Another intake job is already running ({intake_job_manager.current_status.job_id}).",
        )

    drive = disk_detector.get_drive(req.target_disk)
    if not drive:
        raise HTTPException(status_code=404, detail=f"Target disk '{req.target_disk}' not found on host.")

    # Strict Safety Validation Check
    is_destructive = req.workflow_mode not in ("inventory", "smart_long", "smart_short")
    safety_check = safety_validator.validate_candidate(
        target_disk=drive.canonical_path,
        is_destructive=is_destructive,
        expected_serial=drive.serial,
        entered_serial=req.entered_serial,
    )

    if not safety_check.is_safe:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Safety checks failed. Intake operation refused.",
                "reasons": safety_check.reasons,
                "safety_check": {
                    "is_system_disk": safety_check.is_system_disk,
                    "is_mounted": safety_check.is_mounted,
                    "is_swap": safety_check.is_swap,
                    "is_lvm": safety_check.is_lvm,
                    "is_zfs": safety_check.is_zfs,
                    "is_whole_disk": safety_check.is_whole_disk,
                    "serial_matched": safety_check.serial_matched,
                },
            },
        )

    config = JobConfig(
        target_disk=drive.canonical_path,
        workflow_mode=req.workflow_mode,
        entered_serial=req.entered_serial,
        skip_firmware=req.skip_firmware,
        skip_long_smart=req.skip_long_smart,
        skip_full_verify=req.skip_full_verify,
        skip_bench=req.skip_bench,
    )

    try:
        job_status = await intake_job_manager.start_job(config)
        return job_status.to_dict()
    except Exception as e:
        logger.exception(f"Failed to start intake job: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/jobs/current")
async def api_get_current_job():
    if not intake_job_manager.current_status:
        return {"status": "IDLE", "message": "No job currently active or recorded."}
    return {
        "job": intake_job_manager.current_status.to_dict(),
        "recent_logs": intake_job_manager.log_history[-100:],
    }


@app.post("/api/jobs/current/cancel")
async def api_cancel_current_job():
    if not intake_job_manager.is_running:
        raise HTTPException(status_code=400, detail="No active job to cancel.")
    success = await intake_job_manager.cancel_current_job()
    return {"success": success, "message": "Cancellation signal dispatched."}


@app.get("/api/jobs/{job_id}/stream")
async def api_stream_job_events(job_id: str):
    """Server-Sent Events (SSE) live streaming endpoint."""
    async def event_generator():
        q = intake_job_manager.subscribe_logs()
        try:
            # First send existing logs backlog
            for log_line in intake_job_manager.log_history:
                yield f"data: {json.dumps({'type': 'log', 'data': log_line})}\n\n"
            
            # Send current state
            if intake_job_manager.current_status:
                yield f"data: {json.dumps({'type': 'init', 'job': intake_job_manager.current_status.to_dict()})}\n\n"

            # Stream live updates
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    # Keep-alive ping
                    yield f": ping\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            intake_job_manager.unsubscribe_logs(q)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/reports")
async def api_list_reports():
    runs = report_generator.list_all_runs()
    return [r.to_dict() for r in runs]


@app.get("/api/reports/{run_id}")
async def api_get_report(run_id: str):
    record = report_generator.load_run_metadata(run_id)
    if not record:
        raise HTTPException(status_code=404, detail=f"Report '{run_id}' not found.")
    return record.to_dict()


@app.post("/api/reports/{run_id}/classify")
async def api_classify_report(run_id: str, req: ClassificationRequest):
    valid_classes = ["PASS-A", "PASS-B", "LAB", "REJECT"]
    if req.classification not in valid_classes:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid classification '{req.classification}'. Must be one of {valid_classes}.",
        )

    updated = report_generator.update_classification(run_id, req.classification, req.notes or "")
    if not updated:
        raise HTTPException(status_code=404, detail=f"Report '{run_id}' not found.")
    
    return {"success": True, "classification": req.classification, "notes": req.notes}


@app.get("/api/reports/{run_id}/raw/{category}/{filename}")
async def api_get_raw_artifact(run_id: str, category: str, filename: str):
    # Strict path traversal defense
    safe_cat = os.path.basename(category)
    safe_file = os.path.basename(filename)
    file_path = settings.reports_dir / run_id / safe_cat / safe_file
    
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"Artifact '{category}/{filename}' not found for run '{run_id}'.")

    return PlainTextResponse(file_path.read_text(errors="replace"))


@app.delete("/api/reports/{run_id}")
async def api_delete_report(run_id: str):
    if intake_job_manager.is_running and intake_job_manager.current_status and intake_job_manager.current_status.job_id == run_id:
        raise HTTPException(status_code=400, detail="Cannot delete a report while its intake job is actively running.")

    deleted = report_generator.delete_run(run_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Report run '{run_id}' not found or could not be deleted.")
    return {"success": True, "message": f"Report run '{run_id}' deleted."}


@app.post("/api/reports/purge")
async def api_purge_reports():
    active_job_id = intake_job_manager.current_status.job_id if intake_job_manager.is_running and intake_job_manager.current_status else None
    count = report_generator.purge_all_runs(active_job_id=active_job_id)
    return {"success": True, "deleted_count": count, "message": f"Purged {count} historical report folders."}

