import logging
import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Set

from app.config import settings
from app.core.runner import run_command_sync

logger = logging.getLogger("ssd_intake.safety")

DEVICE_REGEX = re.compile(r"^/dev/(sd[a-z]+|nvme[0-9]+n[0-9]+|vd[a-z]+|xvd[a-z]+|hd[a-z]+)$")

@dataclass
class SafetyCheckResult:
    is_safe: bool
    target_disk: str
    canonical_target: str
    reasons: List[str] = field(default_factory=list)
    protected_system_disks: List[str] = field(default_factory=list)
    is_system_disk: bool = False
    is_mounted: bool = False
    is_swap: bool = False
    is_lvm: bool = False
    is_zfs: bool = False
    is_whole_disk: bool = False
    is_in_allowlist: bool = False
    serial_matched: Optional[bool] = None

    @property
    def summary_message(self) -> str:
        if self.is_safe:
            return "All safety checks passed. Disk is safe for intake."
        return f"Safety check failed: {'; '.join(self.reasons)}"


class SafetyValidator:
    def __init__(self, cmd_runner: Optional[Callable[..., any]] = None):
        self.run_cmd = cmd_runner or run_command_sync

    def canonicalize_path(self, path: str) -> str:
        """Resolves symlinks to absolute canonical path."""
        if not path:
            return ""
        try:
            return os.path.realpath(path.strip())
        except Exception:
            return path.strip()

    def get_parent_disk_from_partition(self, part_dev: str) -> Optional[str]:
        """Given /dev/sda1 or /dev/nvme0n1p1 or sda1, returns /dev/sda or /dev/nvme0n1."""
        if not part_dev:
            return None
        res = self.run_cmd(["lsblk", "-no", "PKNAME", part_dev])
        if res.success and res.stdout.strip():
            pk = res.stdout.strip().splitlines()[0].strip()
            if pk:
                return f"/dev/{pk}" if not pk.startswith("/dev/") else pk
        return None

    def detect_system_disks(self) -> Set[str]:
        """
        Auto-detects the Proxmox / Host boot and system disks.
        Inspects:
        - Root mount '/'
        - '/boot' and '/boot/efi'
        - LVM physical volumes backing the root VG
        - ZFS pools backing root if applicable
        - Explicit system_disk_override from config/env
        """
        protected_disks: Set[str] = set()

        # Add explicit override if configured
        if settings.system_disk_override:
            override_canonical = self.canonicalize_path(settings.system_disk_override)
            if override_canonical:
                protected_disks.add(override_canonical)
                logger.info(f"Added configured system disk override: {override_canonical}")

        # Check critical mountpoints: /, /boot, /boot/efi
        for mountpoint in ["/", "/boot", "/boot/efi"]:
            res = self.run_cmd(["findmnt", "-rn", "-o", "SOURCE", mountpoint])
            if res.success and res.stdout.strip():
                src = res.stdout.strip().splitlines()[0].strip()
                canonical_src = self.canonicalize_path(src)

                # Check if it's a ZFS dataset (e.g. rpool/ROOT/pve-1)
                if "/" in src and not src.startswith("/dev/"):
                    pool_name = src.split("/")[0]
                    zfs_res = self.run_cmd(["zpool", "status", "-P", pool_name])
                    if zfs_res.success:
                        for line in zfs_res.stdout.splitlines():
                            line_str = line.strip()
                            if line_str.startswith("/dev/"):
                                dev_in_pool = line_str.split()[0]
                                parent = self.get_parent_disk_from_partition(dev_in_pool) or dev_in_pool
                                protected_disks.add(self.canonicalize_path(parent))
                    continue

                # Direct partition or disk
                parent = self.get_parent_disk_from_partition(canonical_src)
                if parent:
                    protected_disks.add(self.canonicalize_path(parent))
                elif canonical_src.startswith("/dev/"):
                    # Check if TYPE is disk
                    t_res = self.run_cmd(["lsblk", "-dn", "-o", "TYPE", canonical_src])
                    if t_res.success and t_res.stdout.strip() == "disk":
                        protected_disks.add(canonical_src)

                # Check if root is on LVM logical volume
                lv_res = self.run_cmd(["lvs", "--noheadings", "-o", "vg_name", canonical_src])
                if lv_res.success and lv_res.stdout.strip():
                    vg_name = lv_res.stdout.strip().splitlines()[0].strip()
                    pv_res = self.run_cmd(["pvs", "--noheadings", "-o", "pv_name,vg_name"])
                    if pv_res.success:
                        for line in pv_res.stdout.splitlines():
                            parts = line.strip().split()
                            if len(parts) >= 2 and parts[1] == vg_name:
                                pv_name = parts[0]
                                pv_parent = self.get_parent_disk_from_partition(pv_name) or pv_name
                                protected_disks.add(self.canonicalize_path(pv_parent))

        logger.debug(f"Detected protected system disks: {protected_disks}")
        return protected_disks

    def get_detected_whole_disks(self) -> List[str]:
        """Returns list of canonical paths of all whole physical block devices (TYPE=disk)."""
        disks = []
        res = self.run_cmd(["lsblk", "-dnpo", "NAME,TYPE"])
        if res.success:
            for line in res.stdout.splitlines():
                parts = line.strip().split()
                if len(parts) >= 2 and parts[1] == "disk":
                    disks.append(self.canonicalize_path(parts[0]))
        return disks

    def is_block_device(self, path: str) -> bool:
        """Verifies path exists and is a block device file."""
        if not path or not os.path.exists(path):
            return False
        try:
            mode = os.stat(path).st_mode
            return stat.S_ISBLK(mode)
        except Exception:
            return False

    def is_whole_disk(self, canonical_path: str) -> bool:
        """Verifies block device is a whole disk, not a partition, loop, or LVM volume."""
        if not canonical_path or not self.is_block_device(canonical_path):
            return False
        res = self.run_cmd(["lsblk", "-dn", "-o", "TYPE", canonical_path])
        return res.success and res.stdout.strip() == "disk"

    def check_mounts(self, canonical_path: str) -> List[str]:
        """Checks if disk or any child partition is currently mounted."""
        mounts = []
        # Check findmnt
        res = self.run_cmd(["findmnt", "-rn", "-S", f"{canonical_path}*"])
        if res.success and res.stdout.strip():
            for line in res.stdout.splitlines():
                if line.strip():
                    mounts.append(line.strip())

        # Also check lsblk mountpoints
        lsblk_res = self.run_cmd(["lsblk", "-lnpo", "MOUNTPOINT", canonical_path])
        if lsblk_res.success:
            for mp in lsblk_res.stdout.splitlines():
                if mp.strip() and mp.strip() not in mounts:
                    mounts.append(f"Mounted on {mp.strip()}")

        return mounts

    def check_swap(self, canonical_path: str) -> List[str]:
        """Checks if disk or any child partition is active swap space."""
        swap_devices = []
        res = self.run_cmd(["swapon", "--noheadings", "--show=NAME"])
        if res.success and res.stdout.strip():
            for line in res.stdout.splitlines():
                swap_dev = line.strip()
                if not swap_dev:
                    continue
                canonical_swap = self.canonicalize_path(swap_dev)
                # Match /dev/sdb or /dev/sdb1 or /dev/nvme0n1p1
                if canonical_swap == canonical_path or canonical_swap.startswith(f"{canonical_path}p") or (
                    canonical_path[-1].isalpha() and canonical_swap.startswith(canonical_path)
                ):
                    swap_devices.append(swap_dev)
        return swap_devices

    def check_lvm(self, canonical_path: str) -> List[str]:
        """Checks if disk or any child partition is an active LVM physical volume."""
        lvm_pvs = []
        res = self.run_cmd(["pvs", "--noheadings", "-o", "pv_name"])
        if res.success and res.stdout.strip():
            for line in res.stdout.splitlines():
                pv = line.strip()
                if not pv:
                    continue
                canonical_pv = self.canonicalize_path(pv)
                if canonical_pv == canonical_path or canonical_pv.startswith(f"{canonical_path}p") or (
                    canonical_path[-1].isalpha() and canonical_pv.startswith(canonical_path)
                ):
                    lvm_pvs.append(pv)
        return lvm_pvs

    def check_zfs(self, canonical_path: str) -> List[str]:
        """Checks if disk or any child partition is referenced by ZFS."""
        zfs_refs = []
        res = self.run_cmd(["zpool", "status", "-P"])
        if res.success and res.stdout.strip():
            for line in res.stdout.splitlines():
                line_str = line.strip()
                if canonical_path in line_str:
                    zfs_refs.append(line_str)
        return zfs_refs

    def check_holders(self, canonical_path: str) -> List[str]:
        """Checks sysfs holders (e.g. device mapper or mdadm RAID arrays)."""
        holders = []
        dev_name = os.path.basename(canonical_path)
        holders_dir = Path(f"/sys/class/block/{dev_name}/holders")
        if holders_dir.exists() and holders_dir.is_dir():
            try:
                for entry in holders_dir.iterdir():
                    holders.append(entry.name)
            except Exception as e:
                logger.warning(f"Error inspecting holders for {canonical_path}: {e}")
        return holders

    def validate_candidate(
        self,
        target_disk: str,
        is_destructive: bool = False,
        expected_serial: Optional[str] = None,
        entered_serial: Optional[str] = None,
        custom_system_disks: Optional[Set[str]] = None,
    ) -> SafetyCheckResult:
        """
        Executes comprehensive, uncompromising safety checks on the candidate target disk.
        """
        reasons = []
        if not target_disk:
            return SafetyCheckResult(
                is_safe=False,
                target_disk="",
                canonical_target="",
                reasons=["No target disk specified"],
            )

        canonical_target = self.canonicalize_path(target_disk)
        system_disks = custom_system_disks if custom_system_disks is not None else self.detect_system_disks()
        protected_list = sorted(list(system_disks))

        # 1. Syntax / Regex Validation
        if not DEVICE_REGEX.match(canonical_target) and not DEVICE_REGEX.match(target_disk):
            reasons.append(f"Invalid device path format '{target_disk}'. Must match standard block device path.")

        # 2. Block Device Existence
        if not self.is_block_device(canonical_target):
            reasons.append(f"'{target_disk}' is not a valid block device on the host system.")

        # 3. Whole Disk Check (Reject partitions like /dev/sda1 or /dev/nvme0n1p1)
        is_whole = self.is_whole_disk(canonical_target)
        if not is_whole:
            reasons.append(f"Target '{target_disk}' is not a whole disk (partitions or virtual devices are rejected).")

        # 4. Allowlist Check
        allowlist = self.get_detected_whole_disks()
        in_allowlist = canonical_target in allowlist
        if not in_allowlist and is_whole:
            reasons.append(f"Target '{target_disk}' is not in the detected physical disk allowlist.")

        # 5. Protected System Disk Check
        is_sys = canonical_target in system_disks
        if not is_sys:
            # Also verify parent/child relationship with system disks
            for sys_disk in system_disks:
                lsblk_sys = self.run_cmd(["lsblk", "-nrpo", "NAME", sys_disk])
                if lsblk_sys.success and canonical_target in lsblk_sys.stdout.splitlines():
                    is_sys = True
                    break

        if is_sys:
            reasons.append(f"Target '{target_disk}' is the protected Proxmox system/boot disk. Operation permanently forbidden.")

        # 6. Mounted Filesystems Check
        mounts = self.check_mounts(canonical_target)
        is_mounted = len(mounts) > 0
        if is_mounted:
            reasons.append(f"Disk or child partition is currently mounted: {', '.join(mounts)}")

        # 7. Active Swap Check
        swap_devs = self.check_swap(canonical_target)
        is_swap = len(swap_devs) > 0
        if is_swap:
            reasons.append(f"Disk or child partition has active swap space: {', '.join(swap_devs)}")

        # 8. Active LVM PV Check
        lvm_pvs = self.check_lvm(canonical_target)
        is_lvm = len(lvm_pvs) > 0
        if is_lvm:
            reasons.append(f"Disk or child partition is an active LVM Physical Volume: {', '.join(lvm_pvs)}")

        # 9. Active ZFS Pool Reference Check
        zfs_refs = self.check_zfs(canonical_target)
        is_zfs = len(zfs_refs) > 0
        if is_zfs:
            reasons.append("Disk or partition is currently referenced by a ZFS pool.")

        # 10. Device Holders / RAID Check
        holders = self.check_holders(canonical_target)
        if holders:
            reasons.append(f"Disk has active device holders (device-mapper/RAID): {', '.join(holders)}")

        # 11. Destructive Confirmation & Serial Match Check
        serial_matched = None
        if is_destructive:
            if not expected_serial:
                reasons.append("Destructive operation requires a valid drive serial number for verification.")
                serial_matched = False
            elif not entered_serial or entered_serial.strip() != expected_serial.strip():
                reasons.append(
                    f"Serial confirmation mismatch! Expected '{expected_serial}', but received '{entered_serial or ''}'."
                )
                serial_matched = False
            else:
                serial_matched = True

        is_safe = len(reasons) == 0

        return SafetyCheckResult(
            is_safe=is_safe,
            target_disk=target_disk,
            canonical_target=canonical_target,
            reasons=reasons,
            protected_system_disks=protected_list,
            is_system_disk=is_sys,
            is_mounted=is_mounted,
            is_swap=is_swap,
            is_lvm=is_lvm,
            is_zfs=is_zfs,
            is_whole_disk=is_whole,
            is_in_allowlist=in_allowlist,
            serial_matched=serial_matched,
        )


safety_validator = SafetyValidator()
