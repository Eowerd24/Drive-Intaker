import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ssd_intake.smart_parser")

@dataclass
class SmartAttribute:
    id: Optional[int]
    name: str
    value: Optional[int]
    worst: Optional[int]
    thresh: Optional[int]
    raw_value: Optional[int]
    raw_string: str


@dataclass
class SelfTestEntry:
    num: int
    test_type: str
    status: str
    lifetime_hours: Optional[int]
    lba_first_error: Optional[str]


@dataclass
class SmartReport:
    healthy: Optional[bool]
    health_status_str: str
    power_on_hours: Optional[int]
    temperature_celsius: Optional[int]
    wear_percentage: Optional[int]  # 0 to 100% wear (or remaining life inverted)
    wear_remaining_percentage: Optional[int]  # 100% to 0%
    reallocated_sectors: Optional[int]
    uncorrectable_errors: Optional[int]
    crc_errors: Optional[int]
    tbw_terabytes: Optional[float]
    tbr_terabytes: Optional[float]
    short_test_duration_minutes: int = 2
    long_test_duration_minutes: int = 60
    self_test_entries: List[SelfTestEntry] = field(default_factory=list)
    attributes: Dict[int, SmartAttribute] = field(default_factory=dict)
    raw_json: Optional[Dict[str, Any]] = None
    raw_text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "healthy": self.healthy,
            "health_status_str": self.health_status_str,
            "power_on_hours": self.power_on_hours,
            "temperature_celsius": self.temperature_celsius,
            "wear_percentage": self.wear_percentage,
            "wear_remaining_percentage": self.wear_remaining_percentage,
            "reallocated_sectors": self.reallocated_sectors,
            "uncorrectable_errors": self.uncorrectable_errors,
            "crc_errors": self.crc_errors,
            "tbw_terabytes": self.tbw_terabytes,
            "tbr_terabytes": self.tbr_terabytes,
            "short_test_duration_minutes": self.short_test_duration_minutes,
            "long_test_duration_minutes": self.long_test_duration_minutes,
            "self_test_entries": [
                {
                    "num": e.num,
                    "test_type": e.test_type,
                    "status": e.status,
                    "lifetime_hours": e.lifetime_hours,
                    "lba_first_error": e.lba_first_error,
                }
                for e in self.self_test_entries
            ],
        }


class SmartParser:
    @staticmethod
    def parse(smart_text: str = "", smart_json_str: Optional[str] = None) -> SmartReport:
        """Parses SMART data from json and/or standard smartctl text."""
        json_data = None
        if smart_json_str and smart_json_str.strip():
            try:
                json_data = json.loads(smart_json_str)
            except Exception as e:
                logger.warning(f"Failed to parse SMART JSON: {e}")

        if json_data:
            return SmartParser._parse_json(json_data, raw_text=smart_text)
        return SmartParser._parse_text(smart_text)

    @staticmethod
    def _parse_json(data: Dict[str, Any], raw_text: str = "") -> SmartReport:
        healthy = None
        health_str = "UNKNOWN"
        poh = None
        temp = None
        wear_pct = None
        wear_remain = None
        realloc = None
        uncorrect = None
        crc = None
        tbw = None
        tbr = None
        short_mins = 2
        long_mins = 60
        attributes: Dict[int, SmartAttribute] = {}
        selftests: List[SelfTestEntry] = []

        # Overall health
        smart_status = data.get("smart_status", {})
        if "passed" in smart_status:
            healthy = bool(smart_status["passed"])
            health_str = "PASSED" if healthy else "FAILED"

        # NVMe SMART Log
        nvme_log = data.get("nvme_smart_health_information_log", {})
        if nvme_log:
            poh = nvme_log.get("power_on_hours")
            temp = nvme_log.get("temperature")
            wear_pct = nvme_log.get("percentage_used")
            if wear_pct is not None:
                wear_remain = max(0, 100 - wear_pct)
            
            # Media / Data integrity errors
            uncorrect = nvme_log.get("media_errors", 0)
            
            # TBW / TBR: data_units_written / read (1 unit = 512,000 bytes)
            du_written = nvme_log.get("data_units_written")
            if du_written is not None:
                tbw = round((du_written * 512000) / (1024**4), 2)
            du_read = nvme_log.get("data_units_read")
            if du_read is not None:
                tbr = round((du_read * 512000) / (1024**4), 2)

            crit_warn = nvme_log.get("critical_warning", 0)
            if crit_warn != 0 and healthy is not False:
                healthy = False
                health_str = f"FAILED (Critical Warning 0x{crit_warn:02x})"

        # ATA SMART Attributes table
        ata_table = data.get("ata_smart_attributes", {}).get("table", [])
        for item in ata_table:
            attr_id = item.get("id")
            name = item.get("name", "")
            val = item.get("value")
            worst = item.get("worst")
            thresh = item.get("thresh")
            raw_val = item.get("raw", {}).get("value")
            raw_str = item.get("raw", {}).get("string", str(raw_val or ""))

            if attr_id is not None:
                attributes[attr_id] = SmartAttribute(
                    id=attr_id,
                    name=name,
                    value=val,
                    worst=worst,
                    thresh=thresh,
                    raw_value=raw_val,
                    raw_string=raw_str,
                )

                if attr_id == 5:  # Reallocated_Sector_Ct
                    realloc = raw_val
                elif attr_id == 9:  # Power_On_Hours
                    poh = raw_val
                elif attr_id == 187:  # Reported_Uncorrect
                    uncorrect = raw_val
                elif attr_id == 194:  # Temperature_Celsius
                    temp = raw_val if raw_val is not None else val
                elif attr_id == 199:  # UDMA_CRC_Error_Count
                    crc = raw_val
                elif attr_id in (177, 231, 233):  # Wear indicators
                    # 177: Wear_Leveling_Count (or percentage remaining)
                    # 231: SSD_Life_Left (100 = new, 0 = end of life)
                    # 233: Media_Wearout_Indicator (normalized value)
                    if attr_id == 231:
                        wear_remain = val
                        wear_pct = 100 - val if val is not None else None
                    elif attr_id == 177 and wear_remain is None:
                        wear_remain = val
                        wear_pct = 100 - val if val is not None else None
                    elif attr_id == 233 and wear_remain is None:
                        wear_remain = val
                        wear_pct = 100 - val if val is not None else None
                elif attr_id == 241:  # Total LBAs Written
                    if raw_val is not None:
                        # 1 LBA = 512 bytes
                        tbw = round((raw_val * 512) / (1024**4), 2)
                elif attr_id == 242:  # Total LBAs Read
                    if raw_val is not None:
                        tbr = round((raw_val * 512) / (1024**4), 2)

        # Polling times
        short_rec = data.get("ata_smart_data", {}).get("self_test", {}).get("polling_minutes", {}).get("short")
        if short_rec:
            short_mins = int(short_rec)
        long_rec = data.get("ata_smart_data", {}).get("self_test", {}).get("polling_minutes", {}).get("extended")
        if long_rec:
            long_mins = int(long_rec)

        # Self-test log
        test_table = data.get("ata_smart_self_test_log", {}).get("standard", {}).get("table", [])
        if not test_table:
            test_table = data.get("ata_smart_self_test_log", {}).get("extended", {}).get("table", [])
        for idx, entry in enumerate(test_table):
            t_type = entry.get("type", {}).get("string", entry.get("test_description", "Unknown"))
            t_status = entry.get("status", {}).get("string", entry.get("status", "Unknown"))
            l_hours = entry.get("lifetime_hours")
            lba = str(entry.get("first_lba_error", "None"))
            selftests.append(
                SelfTestEntry(
                    num=idx + 1,
                    test_type=t_type,
                    status=t_status,
                    lifetime_hours=l_hours,
                    lba_first_error=lba,
                )
            )

        return SmartReport(
            healthy=healthy,
            health_status_str=health_str,
            power_on_hours=poh,
            temperature_celsius=temp,
            wear_percentage=wear_pct,
            wear_remaining_percentage=wear_remain,
            reallocated_sectors=realloc,
            uncorrectable_errors=uncorrect,
            crc_errors=crc,
            tbw_terabytes=tbw,
            tbr_terabytes=tbr,
            short_test_duration_minutes=short_mins,
            long_test_duration_minutes=long_mins,
            self_test_entries=selftests,
            attributes=attributes,
            raw_json=data,
            raw_text=raw_text,
        )

    @staticmethod
    def _parse_text(text: str) -> SmartReport:
        healthy = None
        health_str = "UNKNOWN"
        poh = None
        temp = None
        wear_pct = None
        wear_remain = None
        realloc = None
        uncorrect = None
        crc = None
        tbw = None
        tbr = None
        short_mins = 2
        long_mins = 60
        attributes: Dict[int, SmartAttribute] = {}
        selftests: List[SelfTestEntry] = []

        # Overall health check
        m = re.search(r"SMART overall-health self-assessment test result:\s*([A-Za-z0-9_ -]+)", text)
        if m:
            health_str = m.group(1).strip()
            healthy = "PASSED" in health_str.upper() or "OK" in health_str.upper()

        m_nvme_health = re.search(r"SMART/Health Information \(NVMe Log[^)]*\)\s*SMART overall-health:\s*([A-Za-z]+)", text)
        if m_nvme_health:
            health_str = m_nvme_health.group(1).strip()
            healthy = "PASSED" in health_str.upper() or "OK" in health_str.upper()

        # Polling times
        m_short = re.search(r"Short self-test routine recommended polling time:\s*\(\s*([0-9]+)\s*\)\s*minutes", text)
        if m_short:
            short_mins = int(m_short.group(1))
        m_long = re.search(r"Extended self-test routine recommended polling time:\s*\(\s*([0-9]+)\s*\)\s*minutes", text)
        if m_long:
            long_mins = int(m_long.group(1))

        # ATA Attributes Table
        lines = text.splitlines()
        in_attr_table = False
        in_selftest_table = False

        for line in lines:
            if "ID# ATTRIBUTE_NAME" in line:
                in_attr_table = True
                continue
            if in_attr_table:
                if not line.strip() or line.startswith("SMART Error") or line.startswith("SMART Self-test"):
                    in_attr_table = False
                else:
                    parts = line.split()
                    if len(parts) >= 10 and parts[0].isdigit():
                        attr_id = int(parts[0])
                        attr_name = parts[1]
                        val = int(parts[3]) if parts[3].isdigit() else None
                        worst = int(parts[4]) if parts[4].isdigit() else None
                        thresh = int(parts[5]) if parts[5].isdigit() else None
                        raw_str = parts[9]
                        raw_val = int(re.sub(r"[^0-9]", "", raw_str)) if re.search(r"[0-9]", raw_str) else None

                        attributes[attr_id] = SmartAttribute(
                            id=attr_id,
                            name=attr_name,
                            value=val,
                            worst=worst,
                            thresh=thresh,
                            raw_value=raw_val,
                            raw_string=raw_str,
                        )

                        if attr_id == 5:
                            realloc = raw_val
                        elif attr_id == 9:
                            poh = raw_val
                        elif attr_id == 187:
                            uncorrect = raw_val
                        elif attr_id == 194:
                            temp = raw_val
                        elif attr_id == 199:
                            crc = raw_val
                        elif attr_id in (177, 231, 233):
                            if attr_id == 231 and val is not None:
                                wear_remain = val
                                wear_pct = 100 - val
                            elif wear_remain is None and val is not None:
                                wear_remain = val
                                wear_pct = 100 - val
                        elif attr_id == 241 and raw_val is not None:
                            tbw = round((raw_val * 512) / (1024**4), 2)
                        elif attr_id == 242 and raw_val is not None:
                            tbr = round((raw_val * 512) / (1024**4), 2)

            # NVMe text parsing
            if "Power On Hours:" in line:
                m_poh = re.search(r"Power On Hours:\s*([0-9,]+)", line)
                if m_poh:
                    poh = int(m_poh.group(1).replace(",", ""))
            if "Temperature:" in line and "Celsius" in line:
                m_t = re.search(r"Temperature:\s*([0-9]+)\s*Celsius", line)
                if m_t:
                    temp = int(m_t.group(1))
            if "Percentage Used:" in line:
                m_pct = re.search(r"Percentage Used:\s*([0-9]+)%", line)
                if m_pct:
                    wear_pct = int(m_pct.group(1))
                    wear_remain = max(0, 100 - wear_pct)
            if "Data Units Written:" in line:
                m_duw = re.search(r"Data Units Written:\s*([0-9,]+)", line)
                if m_duw:
                    du_count = int(m_duw.group(1).replace(",", ""))
                    tbw = round((du_count * 512000) / (1024**4), 2)
            if "Media and Data Integrity Errors:" in line:
                m_me = re.search(r"Media and Data Integrity Errors:\s*([0-9,]+)", line)
                if m_me:
                    uncorrect = int(m_me.group(1).replace(",", ""))

        return SmartReport(
            healthy=healthy,
            health_status_str=health_str,
            power_on_hours=poh,
            temperature_celsius=temp,
            wear_percentage=wear_pct,
            wear_remaining_percentage=wear_remain,
            reallocated_sectors=realloc,
            uncorrectable_errors=uncorrect,
            crc_errors=crc,
            tbw_terabytes=tbw,
            tbr_terabytes=tbr,
            short_test_duration_minutes=short_mins,
            long_test_duration_minutes=long_mins,
            self_test_entries=selftests,
            attributes=attributes,
            raw_json=None,
            raw_text=text,
        )

    @staticmethod
    def diff_smart(before: SmartReport, after: SmartReport) -> Dict[str, Any]:
        """Compares before and after SMART reports to highlight state changes."""
        def diff_val(b, a):
            if b is None and a is None:
                return {"before": "N/A", "after": "N/A", "changed": False, "delta": 0}
            if b is None:
                return {"before": "N/A", "after": a, "changed": True, "delta": 0}
            if a is None:
                return {"before": b, "after": "N/A", "changed": True, "delta": 0}
            changed = b != a
            delta = (a - b) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
            return {"before": b, "after": a, "changed": changed, "delta": delta}

        return {
            "health": {
                "before": before.health_status_str,
                "after": after.health_status_str,
                "changed": before.health_status_str != after.health_status_str,
            },
            "power_on_hours": diff_val(before.power_on_hours, after.power_on_hours),
            "reallocated_sectors": diff_val(before.reallocated_sectors, after.reallocated_sectors),
            "uncorrectable_errors": diff_val(before.uncorrectable_errors, after.uncorrectable_errors),
            "crc_errors": diff_val(before.crc_errors, after.crc_errors),
            "temperature_celsius": diff_val(before.temperature_celsius, after.temperature_celsius),
            "wear_remaining_percentage": diff_val(before.wear_remaining_percentage, after.wear_remaining_percentage),
            "tbw_terabytes": diff_val(before.tbw_terabytes, after.tbw_terabytes),
        }
