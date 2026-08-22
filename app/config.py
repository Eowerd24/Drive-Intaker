import os
from pathlib import Path
from pydantic import BaseModel, Field

class Settings(BaseModel):
    app_name: str = "Proxmox SSD Intake Station"
    app_version: str = "1.0.0"
    
    # Report and log storage lives strictly inside the container (/app/reports)
    # Nothing is written to the physical host filesystem outside the container
    reports_dir: Path = Field(
        default_factory=lambda: Path(
            os.environ.get("SSD_INTAKE_ROOT_DIR", "/app/reports" if os.path.exists("/app") else "./reports")
        )
    )
    
    # System disk override (e.g. /dev/sda or /dev/nvme0n1)
    system_disk_override: str = Field(
        default_factory=lambda: os.environ.get("SSD_INTAKE_SYSTEM_DISK", "").strip()
    )
    
    # Web server bind address (Default 127.0.0.1 for local host security)
    host: str = Field(
        default_factory=lambda: os.environ.get("SSD_INTAKE_HOST", "127.0.0.1")
    )
    
    # Web server port (Default 7492)
    port: int = Field(
        default_factory=lambda: int(os.environ.get("SSD_INTAKE_PORT", "7492"))
    )
    
    # Enable mock mode for testing/dev environments without real hardware
    mock_mode: bool = Field(
        default_factory=lambda: os.environ.get("SSD_INTAKE_MOCK_MODE", "0").lower() in ("1", "true", "yes")
    )
    
    # Log level
    log_level: str = Field(
        default_factory=lambda: os.environ.get("SSD_INTAKE_LOG_LEVEL", "INFO")
    )

    def ensure_directories(self) -> None:
        self.reports_dir.mkdir(parents=True, exist_ok=True)

settings = Settings()
