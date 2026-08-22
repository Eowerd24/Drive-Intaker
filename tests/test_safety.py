import pytest
from app.config import settings
from app.core.runner import CommandResult
from app.core.safety import SafetyValidator

def mock_runner_builder(commands_map):
    """Helper to return mock CommandResult for specified command prefixes."""
    def _mock_run(args, **kwargs):
        cmd_str = " ".join(args)
        for prefix, (code, stdout, stderr) in commands_map.items():
            if cmd_str.startswith(prefix) or prefix in cmd_str:
                return CommandResult(
                    command=args,
                    exit_code=code,
                    stdout=stdout,
                    stderr=stderr,
                    duration_seconds=0.01,
                )
        # Default fallback
        return CommandResult(
            command=args,
            exit_code=0,
            stdout="",
            stderr="",
            duration_seconds=0.01,
        )
    return _mock_run


def test_system_disk_auto_detection_root_partition():
    """Test auto-detection when root / is on a direct partition like /dev/sda2."""
    cmd_map = {
        "findmnt -rn -o SOURCE /": (0, "/dev/sda2", ""),
        "lsblk -no PKNAME /dev/sda2": (0, "sda", ""),
        "findmnt -rn -o SOURCE /boot": (0, "/dev/sda1", ""),
        "lsblk -no PKNAME /dev/sda1": (0, "sda", ""),
        "findmnt -rn -o SOURCE /boot/efi": (0, "", ""),
        "lvs --noheadings -o vg_name /dev/sda2": (1, "", "Not an LVM device"),
    }
    validator = SafetyValidator(cmd_runner=mock_runner_builder(cmd_map))
    sys_disks = validator.detect_system_disks()
    
    assert "/dev/sda" in sys_disks


def test_system_disk_auto_detection_lvm():
    """Test auto-detection when root / is on an LVM LV backed by /dev/nvme0n1p3."""
    cmd_map = {
        "findmnt -rn -o SOURCE /": (0, "/dev/mapper/pve-root", ""),
        "lsblk -no PKNAME /dev/mapper/pve-root": (0, "", ""),
        "lvs --noheadings -o vg_name /dev/mapper/pve-root": (0, "pve", ""),
        "pvs --noheadings -o pv_name,vg_name": (0, "/dev/nvme0n1p3 pve\n/dev/sdb1 data_vg", ""),
        "lsblk -no PKNAME /dev/nvme0n1p3": (0, "nvme0n1", ""),
        "findmnt -rn -o SOURCE /boot": (0, "/dev/nvme0n1p2", ""),
        "lsblk -no PKNAME /dev/nvme0n1p2": (0, "nvme0n1", ""),
        "findmnt -rn -o SOURCE /boot/efi": (0, "/dev/nvme0n1p1", ""),
        "lsblk -no PKNAME /dev/nvme0n1p1": (0, "nvme0n1", ""),
    }
    validator = SafetyValidator(cmd_runner=mock_runner_builder(cmd_map))
    sys_disks = validator.detect_system_disks()

    assert "/dev/nvme0n1" in sys_disks


def test_system_disk_auto_detection_zfs():
    """Test auto-detection when root / is on a ZFS dataset rpool/ROOT/pve-1."""
    cmd_map = {
        "findmnt -rn -o SOURCE /": (0, "rpool/ROOT/pve-1", ""),
        "zpool status -P rpool": (0, "rpool ONLINE\n  /dev/sda3 ONLINE\n", ""),
        "lsblk -no PKNAME /dev/sda3": (0, "sda", ""),
        "findmnt -rn -o SOURCE /boot": (0, "", ""),
        "findmnt -rn -o SOURCE /boot/efi": (0, "", ""),
    }
    validator = SafetyValidator(cmd_runner=mock_runner_builder(cmd_map))
    sys_disks = validator.detect_system_disks()

    assert "/dev/sda" in sys_disks


def test_explicit_system_disk_override(monkeypatch):
    """Test explicit system disk override is added to protected list."""
    monkeypatch.setattr(settings, "system_disk_override", "/dev/sdz")
    validator = SafetyValidator(cmd_runner=mock_runner_builder({}))
    sys_disks = validator.detect_system_disks()

    assert "/dev/sdz" in sys_disks


def test_cannot_select_system_disk(monkeypatch):
    """Test candidate disk that is the system disk is strictly rejected."""
    monkeypatch.setattr(settings, "system_disk_override", "/dev/sda")
    cmd_map = {
        "lsblk -dnpo NAME,TYPE": (0, "/dev/sda disk\n/dev/sdb disk", ""),
        "lsblk -dn -o TYPE /dev/sda": (0, "disk", ""),
    }
    validator = SafetyValidator(cmd_runner=mock_runner_builder(cmd_map))
    
    # Mock is_block_device to return True
    monkeypatch.setattr(validator, "is_block_device", lambda p: True)

    res = validator.validate_candidate(target_disk="/dev/sda", is_destructive=True, expected_serial="S123", entered_serial="S123")
    
    assert res.is_safe is False
    assert res.is_system_disk is True
    assert any("protected Proxmox system" in r for r in res.reasons)


def test_cannot_select_partition(monkeypatch):
    """Test partitions like /dev/sdb1 or /dev/nvme0n1p1 are rejected as whole-disk targets."""
    cmd_map = {
        "lsblk -dnpo NAME,TYPE": (0, "/dev/sdb disk", ""),
        "lsblk -dn -o TYPE /dev/sdb1": (0, "part", ""),
    }
    validator = SafetyValidator(cmd_runner=mock_runner_builder(cmd_map))
    monkeypatch.setattr(validator, "is_block_device", lambda p: True)

    res = validator.validate_candidate(target_disk="/dev/sdb1", custom_system_disks={"/dev/sda"})
    
    assert res.is_safe is False
    assert res.is_whole_disk is False
    assert any("not a whole disk" in r for r in res.reasons)


def test_mounted_disk_rejected(monkeypatch):
    """Test candidate drive with an active mountpoint is rejected."""
    cmd_map = {
        "lsblk -dnpo NAME,TYPE": (0, "/dev/sdb disk", ""),
        "lsblk -dn -o TYPE /dev/sdb": (0, "disk", ""),
        "findmnt -rn -S /dev/sdb*": (0, "/dev/sdb1 /mnt/data ext4 rw", ""),
        "lsblk -lnpo MOUNTPOINT /dev/sdb": (0, "/mnt/data", ""),
    }
    validator = SafetyValidator(cmd_runner=mock_runner_builder(cmd_map))
    monkeypatch.setattr(validator, "is_block_device", lambda p: True)

    res = validator.validate_candidate(target_disk="/dev/sdb", custom_system_disks={"/dev/sda"})
    
    assert res.is_safe is False
    assert res.is_mounted is True
    assert any("currently mounted" in r for r in res.reasons)


def test_lvm_pv_disk_rejected(monkeypatch):
    """Test candidate drive holding an active LVM PV is rejected."""
    cmd_map = {
        "lsblk -dnpo NAME,TYPE": (0, "/dev/sdb disk", ""),
        "lsblk -dn -o TYPE /dev/sdb": (0, "disk", ""),
        "pvs --noheadings -o pv_name": (0, "/dev/sdb1\n/dev/sda2", ""),
    }
    validator = SafetyValidator(cmd_runner=mock_runner_builder(cmd_map))
    monkeypatch.setattr(validator, "is_block_device", lambda p: True)

    res = validator.validate_candidate(target_disk="/dev/sdb", custom_system_disks={"/dev/sda"})
    
    assert res.is_safe is False
    assert res.is_lvm is True
    assert any("LVM Physical Volume" in r for r in res.reasons)


def test_swap_disk_rejected(monkeypatch):
    """Test candidate drive containing active swap is rejected."""
    cmd_map = {
        "lsblk -dnpo NAME,TYPE": (0, "/dev/sdb disk", ""),
        "lsblk -dn -o TYPE /dev/sdb": (0, "disk", ""),
        "swapon --noheadings --show=NAME": (0, "/dev/sdb2", ""),
    }
    validator = SafetyValidator(cmd_runner=mock_runner_builder(cmd_map))
    monkeypatch.setattr(validator, "is_block_device", lambda p: True)

    res = validator.validate_candidate(target_disk="/dev/sdb", custom_system_disks={"/dev/sda"})
    
    assert res.is_safe is False
    assert res.is_swap is True
    assert any("active swap" in r for r in res.reasons)


def test_zfs_disk_rejected(monkeypatch):
    """Test candidate drive in a ZFS pool is rejected."""
    cmd_map = {
        "lsblk -dnpo NAME,TYPE": (0, "/dev/sdb disk", ""),
        "lsblk -dn -o TYPE /dev/sdb": (0, "disk", ""),
        "zpool status -P": (0, "pool: datapool\n state: ONLINE\n  /dev/sdb ONLINE\n", ""),
    }
    validator = SafetyValidator(cmd_runner=mock_runner_builder(cmd_map))
    monkeypatch.setattr(validator, "is_block_device", lambda p: True)

    res = validator.validate_candidate(target_disk="/dev/sdb", custom_system_disks={"/dev/sda"})
    
    assert res.is_safe is False
    assert res.is_zfs is True
    assert any("referenced by a ZFS pool" in r for r in res.reasons)


def test_arbitrary_device_path_injection_rejected(monkeypatch):
    """Test arbitrary non-disk paths, path traversals, or character devices are rejected."""
    validator = SafetyValidator(cmd_runner=mock_runner_builder({}))
    
    # Path traversal
    res1 = validator.validate_candidate(target_disk="../../../etc/shadow")
    assert res1.is_safe is False

    # Arbitrary file
    res2 = validator.validate_candidate(target_disk="/dev/null")
    assert res2.is_safe is False

    # Raw memory
    res3 = validator.validate_candidate(target_disk="/dev/mem")
    assert res3.is_safe is False


def test_destructive_serial_confirmation_required(monkeypatch):
    """Test destructive operations require exact drive serial matching."""
    cmd_map = {
        "lsblk -dnpo NAME,TYPE": (0, "/dev/sdb disk", ""),
        "lsblk -dn -o TYPE /dev/sdb": (0, "disk", ""),
    }
    validator = SafetyValidator(cmd_runner=mock_runner_builder(cmd_map))
    monkeypatch.setattr(validator, "is_block_device", lambda p: True)

    # 1. Serial mismatch
    res_bad = validator.validate_candidate(
        target_disk="/dev/sdb",
        is_destructive=True,
        expected_serial="S2HCNX0H123456",
        entered_serial="WRONG_SERIAL",
        custom_system_disks={"/dev/sda"},
    )
    assert res_bad.is_safe is False
    assert res_bad.serial_matched is False
    assert any("Serial confirmation mismatch" in r for r in res_bad.reasons)

    # 2. Exact match
    res_good = validator.validate_candidate(
        target_disk="/dev/sdb",
        is_destructive=True,
        expected_serial="S2HCNX0H123456",
        entered_serial="S2HCNX0H123456",
        custom_system_disks={"/dev/sda"},
    )
    assert res_good.is_safe is True
    assert res_good.serial_matched is True


def test_inventory_mode_bypasses_serial_check(monkeypatch):
    """Test non-destructive inventory mode does not require serial confirmation."""
    cmd_map = {
        "lsblk -dnpo NAME,TYPE": (0, "/dev/sdb disk", ""),
        "lsblk -dn -o TYPE /dev/sdb": (0, "disk", ""),
    }
    validator = SafetyValidator(cmd_runner=mock_runner_builder(cmd_map))
    monkeypatch.setattr(validator, "is_block_device", lambda p: True)

    res = validator.validate_candidate(
        target_disk="/dev/sdb",
        is_destructive=False,
        custom_system_disks={"/dev/sda"},
    )
    assert res.is_safe is True


def test_smart_long_mode_bypasses_serial_check(monkeypatch):
    """Test non-destructive SMART long test does not require serial typing."""
    cmd_map = {
        "lsblk -dnpo NAME,TYPE": (0, "/dev/sdb disk", ""),
        "lsblk -dn -o TYPE /dev/sdb": (0, "disk", ""),
    }
    validator = SafetyValidator(cmd_runner=mock_runner_builder(cmd_map))
    monkeypatch.setattr(validator, "is_block_device", lambda p: True)

    res = validator.validate_candidate(
        target_disk="/dev/sdb",
        is_destructive=False,
        custom_system_disks={"/dev/sda"},
    )
    assert res.is_safe is True
    assert res.serial_matched is None
