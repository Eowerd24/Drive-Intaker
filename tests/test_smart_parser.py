import pytest
from app.core.smart_parser import SmartParser

def test_parse_sata_smart_text():
    sample_text = """
    smartctl 7.3 2022-02-28 r5338 [x86_64-linux-6.8.4-2-pve] (local build)
    === START OF READ SMART DATA SECTION ===
    SMART overall-health self-assessment test result: PASSED

    Short self-test routine recommended polling time:        (   2) minutes.
    Extended self-test routine recommended polling time:     (  60) minutes.

    ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE
      5 Reallocated_Sector_Ct   0x0033   100   100   010    Pre-fail  Always       -       0
      9 Power_On_Hours          0x0032   095   095   000    Old_age   Always       -       18250
    177 Wear_Leveling_Count     0x0013   096   096   000    Pre-fail  Always       -       48
    187 Reported_Uncorrect      0x0032   100   100   000    Old_age   Always       -       0
    194 Temperature_Celsius     0x0022   072   058   000    Old_age   Always       -       28
    199 UDMA_CRC_Error_Count    0x003e   100   100   000    Old_age   Always       -       0
    231 SSD_Life_Left           0x0013   096   096   000    Pre-fail  Always       -       96
    241 Total_LBAs_Written      0x0032   099   099   000    Old_age   Always       -       45000000000
    """

    report = SmartParser.parse(smart_text=sample_text)
    assert report.healthy is True
    assert report.health_status_str == "PASSED"
    assert report.power_on_hours == 18250
    assert report.reallocated_sectors == 0
    assert report.uncorrectable_errors == 0
    assert report.crc_errors == 0
    assert report.temperature_celsius == 28
    assert report.wear_remaining_percentage == 96
    assert report.wear_percentage == 4
    assert report.short_test_duration_minutes == 2
    assert report.long_test_duration_minutes == 60


def test_parse_nvme_smart_json():
    sample_json = """
    {
      "smart_status": {
        "passed": true
      },
      "nvme_smart_health_information_log": {
        "critical_warning": 0,
        "temperature": 34,
        "available_spare": 100,
        "percentage_used": 5,
        "data_units_read": 120000000,
        "data_units_written": 85000000,
        "power_on_hours": 8760,
        "media_errors": 0,
        "num_err_log_entries": 0
      }
    }
    """

    report = SmartParser.parse(smart_json_str=sample_json)
    assert report.healthy is True
    assert report.health_status_str == "PASSED"
    assert report.power_on_hours == 8760
    assert report.temperature_celsius == 34
    assert report.wear_percentage == 5
    assert report.wear_remaining_percentage == 95
    assert report.uncorrectable_errors == 0
    assert report.tbw_terabytes is not None
    assert report.tbw_terabytes > 0


def test_diff_smart():
    before = SmartParser.parse(smart_text="""
    SMART overall-health self-assessment test result: PASSED
    ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE
      5 Reallocated_Sector_Ct   0x0033   100   100   010    Pre-fail  Always       -       0
      9 Power_On_Hours          0x0032   095   095   000    Old_age   Always       -       1000
    199 UDMA_CRC_Error_Count    0x003e   100   100   000    Old_age   Always       -       0
    """)

    after = SmartParser.parse(smart_text="""
    SMART overall-health self-assessment test result: PASSED
    ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE
      5 Reallocated_Sector_Ct   0x0033   100   100   010    Pre-fail  Always       -       2
      9 Power_On_Hours          0x0032   095   095   000    Old_age   Always       -       1002
    199 UDMA_CRC_Error_Count    0x003e   100   100   000    Old_age   Always       -       0
    """)

    diff = SmartParser.diff_smart(before, after)
    assert diff["reallocated_sectors"]["before"] == 0
    assert diff["reallocated_sectors"]["after"] == 2
    assert diff["reallocated_sectors"]["changed"] is True
    assert diff["reallocated_sectors"]["delta"] == 2

    assert diff["crc_errors"]["changed"] is False
