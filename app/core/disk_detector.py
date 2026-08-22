import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.config import settings
from app.core.runner import run_command_sync
from app.core.safety import SafetyValidator, safety_validator
from app.core.smart_parser import SmartParser, SmartReport

logger = logging.getLogger("ssd_intake.detector")

@dataclass
class PartitionInfo:
    name: str
    path: str
    size: str
    fstype: Optional[str]
    mountpoint: Optional[str]
    label: Optional[str]


@dataclass
class DriveInfo:
    name: str  # e.g. sdb
    path: str  # e.g. /dev/sdb
    canonical_path: str  # /dev/sdb
    model: str
    serial: str
    size_str: str
    size_bytes: Optional[int]
    transport: str  # sata, nvme, sas, usb
    vendor: str
    firmware: str
    is_ssd: bool
    smart_health: str  # PASSED, FAILED, UNKNOWN
    power_on_hours: Optional[int]
    temperature_celsius: Optional[int]
    wear_remaining_percentage: Optional[int]
    is_system_disk: bool
    is_locked: bool
    is_eligible: bool
    status_badge: str  # Ready, Protected, Locked, Mounted, LVM, ZFS, Swap, In Use, Unsupported
    blockers: List[str] = field(default_factory=list)
    partitions: List[PartitionInfo] = field(default_factory=list)
    smart_report: Optional[SmartReport] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "canonical_path": self.canonical_path,
            "model": self.model,
            "serial": self.serial,
            "size_str": self.size_str,
            "size_bytes": self.size_bytes,
            "transport": self.transport,
            "vendor": self.vendor,
            "firmware": self.firmware,
            "is_ssd": self.is_ssd,
            "smart_health": self.smart_health,
            "power_on_hours": self.power_on_hours,
            "temperature_celsius": self.temperature_celsius,
            "wear_remaining_percentage": self.wear_remaining_percentage,
            "is_system_disk": self.is_system_disk,
            "is_locked": self.is_locked,
            "is_eligible": self.is_eligible,
            "status_badge": self.status_badge,
            "blockers": self.blockers,
            "partitions": [
                {
                    "name": p.name,
                    "path": p.path,
                    "size": p.size,
                    "fstype": p.fstype,
                    "mountpoint": p.mountpoint,
                    "label": p.label,
                }
                for p in self.partitions
            ],
            "smart_summary": self.smart_report.to_dict() if self.smart_report else None,
        }


class DiskDetector:
    def __init__(self, cmd_runner: Optional[Callable] = None, validator: Optional[SafetyValidator] = None):
        self.run_sync = cmd_runner or run_command_sync
        self.validator = validator or safety_validator

    def get_hostname(self) -> str:
        res = self.run_sync(["hostname"])
        return res.stdout.strip() if res.success else "proxmox-host"

    def detect_all_drives(self) -> List[DriveInfo]:
        """Discovers and inspects all block storage devices on the system."""
        protected_disks = self.validator.detect_system_disks()
        drives: List[DriveInfo] = []

        # Query lsblk in JSON format with comprehensive columns
        lsblk_res = self.run_sync([
            "lsblk",
            "-J",
            "-b",
            "-o",
            "NAME,PATH,TYPE,SIZE,ROTA,TRAN,MODEL,SERIAL,REV,VENDOR,MOUNTPOINT,FSTYPE,LABEL,PKNAME",
        ])

        devices_tree = []
        if lsblk_res.success and lsblk_res.stdout.strip():
            try:
                data = json.loads(lsblk_res.stdout)
                devices_tree = data.get("blockdevices", [])
            except Exception as e:
                logger.error(f"Failed to parse lsblk JSON: {e}")

        # Fallback to standard line parsing if json is empty
        if not devices_tree:
            return self._detect_fallback(protected_disks)

        for dev in devices_tree:
            dev_type = dev.get("type", "")
            if dev_type != "disk":
                continue

            dev_name = dev.get("name", "")
            # Skip loop, ram, zram devices
            if dev_name.startswith("loop") or dev_name.startswith("ram") or dev_name.startswith("zram"):
                continue

            dev_path = dev.get("path") or f"/dev/{dev_name}"
            canonical_path = self.validator.canonicalize_path(dev_path)

            model = (dev.get("model") or "").strip()
            serial = (dev.get("serial") or "").strip()
            firmware = (dev.get("rev") or "").strip()
            vendor = (dev.get("vendor") or "").strip()
            transport = (dev.get("tran") or "").lower().strip()
            rota = dev.get("rota", True)
            is_ssd = not bool(rota)
            size_bytes = dev.get("size")

            # Parse partitions
            partitions: List[PartitionInfo] = []
            for child in dev.get("children", []):
                p_name = child.get("name", "")
                p_path = child.get("path") or f"/dev/{p_name}"
                p_size = self._human_size(child.get("size"))
                partitions.append(
                    PartitionInfo(
                        name=p_name,
                        path=p_path,
                        size=p_size,
                        fstype=child.get("fstype"),
                        mountpoint=child.get("mountpoint"),
                        label=child.get("label"),
                    )
                )

            # Query SMART for more accurate Model/Serial/FW/Health if missing
            smart_report = self._inspect_smart(canonical_path)
            
            # Enrich missing metadata from smartctl or udev
            if not model or not serial or not firmware:
                smart_i = self.run_sync(["smartctl", "-i", canonical_path])
                if smart_i.success:
                    for line in smart_i.stdout.splitlines():
                        if re.search(r"Device Model|Product:", line):
                            model = model or line.split(":", 1)[1].strip()
                        if re.search(r"Serial Number:", line):
                            serial = serial or line.split(":", 1)[1].strip()
                        if re.search(r"Firmware Version:", line):
                            firmware = firmware or line.split(":", 1)[1].strip()

            if not serial:
                udev_res = self.run_sync(["udevadm", "info", "--query=property", f"--name={canonical_path}"])
                if udev_res.success:
                    for line in udev_res.stdout.splitlines():
                        if line.startswith("ID_SERIAL_SHORT="):
                            serial = line.split("=", 1)[1].strip()
                        elif line.startswith("ID_MODEL=") and not model:
                            model = line.split("=", 1)[1].strip()
                        elif line.startswith("ID_REVISION=") and not firmware:
                            firmware = line.split("=", 1)[1].strip()

            if not serial:
                serial = f"{dev_name}-unknown-serial"
            if not model:
                model = f"Unknown Disk ({dev_name})"
            if not transport:
                if "nvme" in dev_name:
                    transport = "nvme"
                else:
                    transport = "sata"

            # Check Safety and Eligibility
            safety_res = self.validator.validate_candidate(
                target_disk=canonical_path,
                is_destructive=False,
                custom_system_disks=protected_disks,
            )

            is_system_disk = safety_res.is_system_disk or (canonical_path in protected_disks)
            is_locked = safety_res.is_locked or self.validator.is_disk_locked(canonical_path)
            blockers = list(safety_res.reasons)

            # Determine badge status
            if is_system_disk:
                status_badge = "Protected"
            elif is_locked:
                status_badge = "Locked"
            elif safety_res.is_mounted:
                status_badge = "Mounted"
            elif safety_res.is_swap:
                status_badge = "Swap"
            elif safety_res.is_lvm:
                status_badge = "LVM"
            elif safety_res.is_zfs:
                status_badge = "ZFS"
            elif not safety_res.is_safe:
                status_badge = "In Use"
            else:
                status_badge = "Ready"

            size_str = self._human_size(size_bytes)

            drives.append(
                DriveInfo(
                    name=dev_name,
                    path=dev_path,
                    canonical_path=canonical_path,
                    model=model,
                    serial=serial,
                    size_str=size_str,
                    size_bytes=size_bytes,
                    transport=transport,
                    vendor=vendor,
                    firmware=firmware,
                    is_ssd=is_ssd,
                    smart_health=smart_report.health_status_str if smart_report else "UNKNOWN",
                    power_on_hours=smart_report.power_on_hours if smart_report else None,
                    temperature_celsius=smart_report.temperature_celsius if smart_report else None,
                    wear_remaining_percentage=smart_report.wear_remaining_percentage if smart_report else None,
                    is_system_disk=is_system_disk,
                    is_locked=is_locked,
                    is_eligible=safety_res.is_safe and not is_system_disk and not is_locked,
                    status_badge=status_badge,
                    blockers=blockers,
                    partitions=partitions,
                    smart_report=smart_report,
                )
            )

        return drives

    def get_drive(self, dev_name_or_path: str) -> Optional[DriveInfo]:
        """Finds a specific drive by name ('sdb') or full path ('/dev/sdb')."""
        clean_name = os.path.basename(dev_name_or_path)
        all_drives = self.detect_all_drives()
        for d in all_drives:
            if d.name == clean_name or d.path == dev_name_or_path or d.canonical_path == dev_name_or_path:
                return d
        return None

    def _inspect_smart(self, canonical_path: str) -> Optional[SmartReport]:
        """Collects SMART data quickly via smartctl."""
        res_json = self.run_sync(["smartctl", "-x", "-j", canonical_path])
        if res_json.success or res_json.stdout.strip():
            return SmartParser.parse(smart_text="", smart_json_str=res_json.stdout)
        
        # Fallback to text
        res_txt = self.run_sync(["smartctl", "-x", canonical_path])
        if res_txt.stdout.strip():
            return SmartParser.parse(smart_text=res_txt.stdout)
        return None

    def _human_size(self, bytes_val: Optional[int]) -> str:
        if not bytes_val or bytes_val <= 0:
            return "0 B"
        units = ["B", "KB", "MB", "GB", "TB", "PB"]
        i = 0
        size = float(bytes_val)
        while size >= 1000.0 and i < len(units) - 1:
            size /= 1000.0
            i += 1
        return f"{size:.1f} {units[i]}"

    def _detect_fallback(self, protected_disks: set) -> List[DriveInfo]:
        """Fallback disk detection using lsblk text output."""
        drives = []
        res = self.run_sync(["lsblk", "-dnpo", "NAME,TYPE,SIZE,TRAN,MODEL,SERIAL,REV"])
        if res.success:
            for line in res.stdout.splitlines():
                parts = line.strip().split()
                if len(parts) >= 2 and parts[1] == "disk":
                    path = parts[0]
                    name = os.path.basename(path)
                    canonical = self.validator.canonicalize_path(path)
                    is_sys = canonical in protected_disks
                    drives.append(
                        DriveInfo(
                            name=name,
                            path=path,
                            canonical_path=canonical,
                            model=parts[4] if len(parts) > 4 else f"Disk {name}",
                            serial=parts[5] if len(parts) > 5 else f"{name}-serial",
                            size_str=parts[2] if len(parts) > 2 else "Unknown",
                            size_bytes=None,
                            transport=parts[3] if len(parts) > 3 else "sata",
                            vendor="",
                            firmware=parts[6] if len(parts) > 6 else "",
                            is_ssd=True,
                            smart_health="UNKNOWN",
                            power_on_hours=None,
                            temperature_celsius=None,
                            wear_remaining_percentage=None,
                            is_system_disk=is_sys,
                            is_eligible=not is_sys,
                            status_badge="Protected" if is_sys else "Ready",
                            blockers=["Protected system disk"] if is_sys else [],
                        )
                    )
        return drives


disk_detector = DiskDetector()
