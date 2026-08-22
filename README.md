# Proxmox SSD Intake Station (Web GUI)

A lightweight, self-contained **Podman-deployed web application with a GUI** for processing, inspecting, wiping, testing, benchmarking, and grading enterprise SATA, SAS, and NVMe SSDs one drive at a time on a Proxmox VE host.

---

## Key Highlights

- **Proxmox Boot Disk Protection**: Auto-detects parent physical disks for root partitions, LVM volume groups, ZFS pools, and boot/EFI partitions, with optional manual override. Protected disks cannot be selected.
- **Strict Storage Safety Guards**: Refuses mounted disks, active swap, LVM physical volumes, ZFS pool members, and individual partitions.
- **Double Confirmation & Serial Verification**: Requires entering the candidate drive's exact serial number before destructive operations begin.
- **Safe Firmware Handling**: Uses `fwupd` for discovery and update checking; clearly distinguishes `Updatable` from `Update available`; never automatically flashes unknown OEM/server SSDs.
- **Fio Verification & Stress Testing**: Full-device destructive write with CRC32C verification metadata followed by a full-device read verification pass, plus sequential read/write and 4K random mixed benchmarks.
- **Live Real-Time Streaming**: Server-Sent Events (SSE) stream line-by-line terminal output, live progress bars, and stage transitions directly to the browser.
- **Persistent Filesystem Reports**: Canonical `REPORT.md` and structured `run.json` saved to `/root/ssd-intake/YYYYMMDD-HHMMSS-SERIAL/` on the host, fully accessible even if the container is removed.
- **Manual Grading Workflow**: Assign classifications (`PASS-A`, `PASS-B`, `LAB`, `REJECT`) directly in the UI with technician notes.

---

## Architecture Overview

```text
├── app/
│   ├── config.py             # Configuration & environment handling
│   ├── main.py               # FastAPI web server, REST API & SSE streaming
│   ├── core/
│   │   ├── disk_detector.py  # Discovers block devices & enriches metadata
│   │   ├── safety.py         # Strict safety validator & system disk protection
│   │   ├── runner.py         # Subprocess execution engine & log streamer
│   │   ├── smart_parser.py   # Parses SATA/NVMe SMART data & before/after diffs
│   │   ├── firmware.py       # fwupd integration (Updatable vs Update Available)
│   │   ├── fio_runner.py     # Full write + CRC verify & performance benchmarks
│   │   ├── reporter.py       # Report generator (REPORT.md & run.json)
│   │   └── intake_job.py     # Intake state machine & single concurrency lock
│   ├── templates/            # Jinja2 HTML templates
│   └── static/               # Vanilla CSS & JavaScript (zero node build step)
├── deploy/
│   ├── ssd-intake.container  # Quadlet systemd definition for Podman
│   ├── ssd-intake.service    # Alternative standard systemd unit file
│   └── ssd-intake.env.example# Example environment configuration
├── tests/                    # Comprehensive pytest test suite
├── Containerfile             # Rootful Podman container image definition
├── entrypoint.sh             # Container startup script
└── SAFETY.md                 # Detailed threat model & security architecture
```

---

## Deployment & Quickstart

### Prerequisites on Proxmox Host

Ensure `podman` is installed on your Proxmox VE host:

```bash
apt update && apt install -y podman
```

All data and logs live **strictly inside the container**. Nothing touches or remains on the physical host filesystem outside the container.

---

### Method 1: Running Standalone via Podman CLI

#### 1. Build the Container Image

```bash
podman build -t ssd-intake:latest .
```

#### 2. Run the Container

```bash
podman run -d --name ssd-intake \
    --privileged \
    -p 127.0.0.1:7492:7492 \
    -v ssd-intake-data:/app/reports:Z \
    -v /dev:/dev \
    -v /sys:/sys:ro \
    -v /run/udev:/run/udev:ro \
    -v /proc:/proc:ro \
    -e SSD_INTAKE_HOST=0.0.0.0 \
    -e SSD_INTAKE_PORT=7492 \
    -e SSD_INTAKE_ROOT_DIR=/app/reports \
    localhost/ssd-intake:latest
```

> **Data Storage Isolation**: All reports, logs, and diagnostic files are stored within the container volume `ssd-intake-data` (or internal container filesystem). No residual files are written to the physical host. When you remove the container/volume or click **Delete All Logs & Reports** in the GUI, the host remains completely clean.

#### 3. Access the Web GUI

Access the web interface via SSH port forwarding from your workstation:

```bash
ssh -L 7492:127.0.0.1:7492 root@<proxmox-ip>
```

Then open `http://localhost:7492` in your web browser.

---

### Method 2: Running via Podman Quadlet (Systemd Managed)

Podman Quadlet is the recommended method on Proxmox VE / Debian to manage containers as native systemd units.

#### 1. Copy the Quadlet Definition

```bash
cp deploy/ssd-intake.container /etc/containers/systemd/ssd-intake.container
```

#### 2. Reload Systemd and Start Service

```bash
systemctl daemon-reload
systemctl start ssd-intake
systemctl enable ssd-intake
```

#### 3. Check Service Status

```bash
systemctl status ssd-intake
```

---

## Web GUI Walkthrough

### 1. Dashboard (`/`)
- Hostname and protected boot disk display.
- Detected physical storage drives with status badges (`Ready`, `Protected`, `Mounted`, `LVM`, `ZFS`, `Swap`).
- Quick actions to inspect or launch intake.
- Recent completed intake reports with grading badges.

### 2. Device Details (`/drives/{drive_name}`)
- In-depth hardware metadata (Vendor, Model, Serial, Firmware, Interface Speed, Temperature).
- Pre-flight safety eligibility checklist.
- Health attributes (Power-on Hours, Wear Remaining %, Reallocated Sectors, CRC Errors, TBW).
- Dedicated action buttons:
  - **`⚡ Run Long SMART Test (~60m)`**: Dedicated button to trigger a standalone drive internal extended self-test without destructive wiping.
  - **`+ Launch Regular Intake`**: Jumps to the standard intake preparation screen.
- Collapsible raw diagnostics (`smartctl -x`, `hdparm -I`, `udevadm`, `lsblk -O`).

### 3. Intake Setup (`/intake`)
- Candidate drive selection.
- Workflow options:
  - **Regular Intake Pipeline (Recommended)**: Secure Wipe &rarr; SMART Short (~5m) &rarr; fwupd Check &rarr; Full Write+CRC Verify &rarr; Performance Benchmarks &rarr; Report.
  - **SMART Extended Self-Test Only (~60m)**: Standalone, non-destructive drive internal surface & sector test with before/after logging.
  - **Safe Inventory Only**: Non-destructive diagnostic snapshot & firmware check without wipe.
  - **Custom Workflow**: Selective stage toggling.
- **Safety Confirmation**: Explicit comparison of Candidate vs Protected System Disk with exact serial confirmation.
- **Serial Confirmation**: Start button is locked until the exact drive serial is typed.

### 4. Live Progress Console (`/jobs/current`)
- Visual stage tracker.
- Real-time streaming terminal log via Server-Sent Events (SSE).
- Auto-scroll toggle and one-click log copy.
- Safe cancellation support.

### 5. Report Viewer & Grading (`/reports/{run_id}`)
- Full diagnostic report and rendered `REPORT.md`.
- SMART Before vs After comparison table.
- Fio verification and benchmark metrics table.
- Interactive drive grading buttons (`PASS-A`, `PASS-B`, `LAB`, `REJECT`) with technician notes.
- Direct links to raw log artifacts (`before/`, `after/`, `tests/`, `benchmarks/`, `firmware/`).
- One-click **Delete Report** action to purge individual runs.

### 6. Log Management & Storage Purge (`/reports`)
- Persistent storage of all drive data on the host at `/root/ssd-intake/`.
- **Delete All Logs & Reports**: A dedicated purge button on the reports archive page allows wiping all historical run logs, benchmark traces, and diagnostic artifacts in one click with confirmation.
- **REST API**:
  - `DELETE /api/reports/{run_id}`: Delete a single report run.
  - `POST /api/reports/purge`: Purge all historical logs and reports.

---

## Workflow Comparison

| Stage | Full Intake | Inventory Only | Custom |
| :--- | :---: | :---: | :---: |
| 1. Initial Snapshot | &#10004; | &#10004; | &#10004; |
| 2. Pre-flight Safety Checks | &#10004; | &#10004; | &#10004; |
| 3. Serial Number Confirmation | &#10004; | _Bypassed_ | &#10004; (if destructive) |
| 4. Wipe GPT/MBR & Signatures | &#10004; | _Skipped_ | Configurable |
| 5. SMART Short Self-Test | &#10004; | _Skipped_ | Configurable |
| 6. SMART Extended Self-Test | &#10004; | _Skipped_ | Optional Toggle |
| 7. Firmware Availability Check | &#10004; | &#10004; | Optional Toggle |
| 8. Full-Device Write + CRC Verify | &#10004; | _Skipped_ | Optional Toggle |
| 9. Performance Benchmarks | &#10004; | _Skipped_ | Optional Toggle |
| 10. Final SMART Snapshot & Diff | &#10004; | &#10004; | &#10004; |
| 11. Canonical REPORT.md & run.json | &#10004; | &#10004; | &#10004; |

---

## Persistent Report Structure

Each run creates a timestamped folder on the host:

```text
/root/ssd-intake/YYYYMMDD-HHMMSS-SERIAL/
├── REPORT.md                  # Canonical human-readable report
├── run.json                   # Structured machine-readable metadata
├── before/                    # Pre-test baseline hardware state
│   ├── smart.txt
│   ├── smart.json
│   ├── hdparm.txt
│   ├── udev.txt
│   └── lsblk.txt
├── after/                     # Post-test state & final SMART
│   ├── smart.txt
│   └── smart.json
├── tests/                     # Test & verification logs
│   ├── smart-short-result.txt
│   ├── smart-long-result.txt
│   ├── full-write.txt
│   └── full-verify.txt
├── benchmarks/                # Performance outputs
│   ├── seq-read.txt
│   ├── seq-write.txt
│   └── rand-mixed.txt
└── firmware/                  # fwupd outputs
    ├── refresh.txt
    ├── devices.txt
    └── updates.txt
```

Symlink `/root/ssd-intake/latest` always points to the most recent run directory.

---

## Running the Automated Test Suite

To run all unit and safety tests locally:

```bash
pytest -v
```

Tests verify:
- System disk auto-detection across partitions, LVM volume groups, and ZFS pools.
- Protection of system disks against selection.
- Partition rejection (whole disks only).
- Rejection of mounted disks, active swap, LVM physical volumes, and ZFS pool members.
- Strict allowlist validation against path traversal and arbitrary inputs.
- Mandatory exact serial number confirmation for destructive actions.
- Single concurrency locking (rejecting concurrent jobs with HTTP 409).
- Accurate `Updatable` vs `Update Available` firmware parsing.
- Fio output parsing and SMART delta calculations.

---

## Managing the Service

### Updating the Application

```bash
# Pull or edit changes, then rebuild
podman build -t ssd-intake:latest .

# Restart the service (if using Quadlet)
systemctl restart ssd-intake
```

### Viewing Container Logs

```bash
# If using Quadlet / systemd
journalctl -u ssd-intake -f

# If running standalone Podman
podman logs -f ssd-intake
```

### Stopping & Removing

```bash
# Stop standalone container
podman stop ssd-intake && podman rm ssd-intake

# Or disable Quadlet service
systemctl stop ssd-intake && systemctl disable ssd-intake
rm -f /etc/containers/systemd/ssd-intake.container
systemctl daemon-reload
```

---

## Reference Shell Script Migration

The original `ssd-intake.sh` script is preserved in the repository as a fallback and reference implementation. The web GUI retains full parity with its safety checks and report format while adding:
- Interactive web UI with real-time SSE progress streaming.
- Immediate visual indicators for drive eligibility and blocker reasons.
- Before/after SMART delta comparison tables.
- Interactive manual classification grading and persistence.
- Zero risk of typos during command line execution.
