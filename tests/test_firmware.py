import pytest
from app.core.firmware import FirmwareManager

def test_fwupd_updatable_but_no_update_available():
    """Device is updatable, but enabled remotes do not have a newer update."""
    devices_txt = """
    Samsung SSD 860 PRO 512GB:
      DeviceId:             7b2ff93fb2b6a22c544e3cb4
      Guid:                 411db18c-32b0-51a8-8b89-fbe69ef66a41
      Plugin:               ata
      Flags:                updatable|supported-on-battery
      Version:              RVM02B6Q
    """
    updates_txt = """
    Devices with no available firmware updates: 
     • Samsung SSD 860 PRO 512GB
    No updatable devices
    """

    fw_mgr = FirmwareManager()
    res = fw_mgr.parse_fwupd_results(
        device_path="/dev/sdb",
        serial="S42WNF0M123456",
        current_fw="RVM02B6Q",
        devices_text=devices_txt,
        updates_text=updates_txt,
    )

    assert res.is_updatable is True
    assert res.update_available is False
    assert "no update offered" in res.summary_status.lower()


def test_fwupd_update_available():
    """Device has an actual newer firmware update available."""
    devices_txt = """
    SAMSUNG MZ7KM960HMJP-00005:
      DeviceId:             a8109d94
      Plugin:               ata
      Flags:                updatable
      Version:              GXM5004Q
    """
    updates_txt = """
    • SAMSUNG MZ7KM960HMJP-00005:
      Update available:
      New version:          GXM5104Q
      Summary:              Firmware update for enterprise SSD
    """

    fw_mgr = FirmwareManager()
    res = fw_mgr.parse_fwupd_results(
        device_path="/dev/sdb",
        serial="S2HCNX0H123456",
        current_fw="GXM5004Q",
        devices_text=devices_txt,
        updates_text=updates_txt,
    )

    assert res.is_updatable is True
    assert res.update_available is True
    assert res.latest_version == "GXM5104Q"
    assert "Firmware update available — manual review required" in res.summary_status


def test_fwupd_reboot_and_power_cycle_flagging():
    """Test flagging of reboot or power cycle requirements."""
    devices_txt = """
    NVMe SSD Controller:
      Plugin:               nvme
      Flags:                updatable|reboot-required|power-cycle-required
    """
    updates_txt = """
    Update available:
    New version: 1.2.3
    Reboot required
    """

    fw_mgr = FirmwareManager()
    res = fw_mgr.parse_fwupd_results(
        device_path="/dev/nvme1n1",
        serial="S123",
        current_fw="1.0.0",
        devices_text=devices_txt,
        updates_text=updates_txt,
    )

    assert res.reboot_required is True
    assert res.power_cycle_required is True
