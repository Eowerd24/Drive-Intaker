import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from app.core.runner import run_command_sync, run_command_async

logger = logging.getLogger("ssd_intake.firmware")

@dataclass
class FirmwareRelease:
    version: str
    summary: str = ""
    description: str = ""
    checksum: str = ""
    uri: str = ""


@dataclass
class FirmwareCheckResult:
    is_updatable: bool = False
    update_available: bool = False
    current_version: str = "Unknown"
    latest_version: Optional[str] = None
    summary_status: str = "Not checked"
    reboot_required: bool = False
    power_cycle_required: bool = False
    device_guid: Optional[str] = None
    plugin: Optional[str] = None
    available_releases: List[FirmwareRelease] = field(default_factory=list)
    raw_devices_output: str = ""
    raw_updates_output: str = ""
    raw_refresh_output: str = ""

    def to_dict(self) -> Dict[str, any]:
        return {
            "is_updatable": self.is_updatable,
            "update_available": self.update_available,
            "current_version": self.current_version,
            "latest_version": self.latest_version,
            "summary_status": self.summary_status,
            "reboot_required": self.reboot_required,
            "power_cycle_required": self.power_cycle_required,
            "device_guid": self.device_guid,
            "plugin": self.plugin,
            "available_releases": [
                {"version": r.version, "summary": r.summary} for r in self.available_releases
            ],
        }


class FirmwareManager:
    def __init__(self, cmd_runner_sync: Optional[Callable] = None, cmd_runner_async: Optional[Callable] = None):
        self.run_sync = cmd_runner_sync or run_command_sync
        self.run_async = cmd_runner_async or run_command_async

    def parse_fwupd_results(
        self,
        device_path: str,
        serial: str,
        current_fw: str,
        devices_text: str,
        updates_text: str,
        refresh_text: str = "",
    ) -> FirmwareCheckResult:
        """
        Parses fwupd outputs to distinguish:
        - Updatable (hardware supports fwupd update mechanism)
        - Update available (a newer version is offered by enabled remotes)
        """
        res = FirmwareCheckResult(
            current_version=current_fw or "Unknown",
            raw_devices_output=devices_text,
            raw_updates_output=updates_text,
            raw_refresh_output=refresh_text,
        )

        dev_name = Path(device_path).name if device_path else ""
        
        # 1. Inspect Devices Output
        # Look for device matching serial, path, or model in fwupd get-devices
        is_device_found = False
        device_section = ""

        # Attempt to split devices_text by device blocks
        if devices_text:
            # Check for JSON output
            if devices_text.strip().startswith("{") or devices_text.strip().startswith("["):
                try:
                    dev_json = json.loads(devices_text)
                    devices_list = dev_json.get("Devices", []) if isinstance(dev_json, dict) else dev_json
                    for d in devices_list:
                        d_serial = d.get("Serial", "")
                        d_guid = d.get("Guid", [""])[0] if isinstance(d.get("Guid"), list) else str(d.get("Guid", ""))
                        d_plugin = d.get("Plugin", "")
                        d_flags = d.get("Flags", [])
                        
                        if (serial and serial.lower() in d_serial.lower()) or (dev_name and dev_name in str(d)):
                            is_device_found = True
                            res.device_guid = d_guid
                            res.plugin = d_plugin
                            res.is_updatable = "updatable" in [f.lower() for f in d_flags] or bool(d_plugin)
                            if "reboot-required" in [f.lower() for f in d_flags]:
                                res.reboot_required = True
                            if "power-cycle-required" in [f.lower() for f in d_flags]:
                                res.power_cycle_required = True
                            break
                except Exception as e:
                    logger.warning(f"Error parsing fwupd JSON devices: {e}")

            # Fallback / Text parsing for devices
            if not is_device_found:
                if (serial and serial.lower() in devices_text.lower()) or (dev_name and dev_name.lower() in devices_text.lower()):
                    is_device_found = True

            combined_fw_text = f"{devices_text}\n{updates_text}".lower()
            if "updatable" in combined_fw_text:
                res.is_updatable = True
            if "reboot-required" in combined_fw_text or "reboot required" in combined_fw_text:
                res.reboot_required = True
            if "power-cycle-required" in combined_fw_text or "power cycle required" in combined_fw_text:
                res.power_cycle_required = True

        # 2. Inspect Updates Output
        # CRITICAL: Distinguish 'Updatable' vs 'Update available'
        if updates_text:
            # Check for standard "No updatable devices" or "no available firmware updates"
            no_updates_patterns = [
                r"no updatable devices",
                r"no available firmware updates",
                r"no updates available",
                r"device.*is up to date",
                r"all devices are up to date",
            ]
            has_no_updates = any(re.search(pat, updates_text, re.IGNORECASE) for pat in no_updates_patterns)

            # Check for actual update available indicators
            has_update_patterns = [
                r"update available",
                r"new version:",
                r"upgrade available",
                r"update to [0-9]",
                r"releases:",
            ]
            has_updates = any(re.search(pat, updates_text, re.IGNORECASE) for pat in has_update_patterns)

            if has_no_updates:
                res.update_available = False
            elif has_updates:
                res.update_available = True
                res.is_updatable = True  # If an update is found, it is definitely updatable

                # Try to extract new version
                m_ver = re.search(r"New version:\s*([A-Za-z0-9_.-]+)", updates_text, re.IGNORECASE)
                if m_ver:
                    res.latest_version = m_ver.group(1).strip()
            
            if re.search(r"reboot[- ]required", updates_text, re.IGNORECASE):
                res.reboot_required = True
            if re.search(r"power[- ]cycle[- ]required", updates_text, re.IGNORECASE):
                res.power_cycle_required = True

        # 3. Determine Summary Status Message
        if res.update_available:
            res.summary_status = "Firmware update available — manual review required"
        elif res.is_updatable:
            res.summary_status = "Updatable device; no update offered by enabled fwupd remotes"
        elif is_device_found:
            res.summary_status = "Device recognized by fwupd (no vendor firmware stream available)"
        else:
            res.summary_status = "No fwupd device profile found / unmanaged"

        return res

    async def run_firmware_check(
        self,
        device_path: str,
        serial: str,
        current_fw: str,
        output_dir: Optional[Path] = None,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> FirmwareCheckResult:
        """
        Executes fwupdmgr refresh, get-devices, get-updates and records outputs.
        NEVER automatically flashes firmware in v1.
        """
        if log_callback:
            log_callback("[INFO] Starting fwupd firmware discovery and availability check...")

        # 1. Refresh metadata
        if log_callback:
            log_callback("[INFO] Refreshing fwupd remotes metadata...")
        refresh_res = await self.run_async(["fwupdmgr", "refresh", "--force"], log_callback=log_callback)
        
        # 2. Get devices
        if log_callback:
            log_callback("[INFO] Querying fwupd devices...")
        dev_res = await self.run_async(["fwupdmgr", "get-devices"], log_callback=log_callback)

        # 3. Get updates
        if log_callback:
            log_callback("[INFO] Querying fwupd update availability...")
        upd_res = await self.run_async(["fwupdmgr", "get-updates"], log_callback=log_callback)

        # Save files if directory provided
        if output_dir:
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "refresh.txt").write_text(refresh_res.combined_output or "None\n")
            (output_dir / "devices.txt").write_text(dev_res.combined_output or "None\n")
            (output_dir / "updates.txt").write_text(upd_res.combined_output or "None\n")

        result = self.parse_fwupd_results(
            device_path=device_path,
            serial=serial,
            current_fw=current_fw,
            devices_text=dev_res.combined_output,
            updates_text=upd_res.combined_output,
            refresh_text=refresh_res.combined_output,
        )

        if log_callback:
            log_callback(f"[INFO] Firmware check status: {result.summary_status}")
            if result.update_available:
                log_callback(f"[WARN] *** {result.summary_status} *** (Current: {result.current_version}, New: {result.latest_version or 'N/A'})")
                log_callback("[WARN] Automatic firmware flashing is strictly disabled in v1.")
            if result.reboot_required or result.power_cycle_required:
                log_callback("[WARN] Host reboot/power cycle required for firmware alterations.")

        return result


firmware_manager = FirmwareManager()
