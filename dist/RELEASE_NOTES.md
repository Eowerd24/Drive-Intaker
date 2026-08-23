# Drive Intaker v1.1.0 - Enterprise SSD Intake Station & Qualification Web GUI

We are excited to announce the release of **Drive Intaker v1.1.0**, featuring manual SMART workflows, least-privilege sudoers execution, permission fixes, and improved container deployment options.

---

## 🌟 Highlights & Capabilities

### 🛡️ Ironclad Multi-Layer Safety Engine
- **Host Boot Disk Protection**: Auto-detects physical disks backing `/`, `/boot`, `/boot/efi`, LVM root volume groups, and ZFS root pools (`/dev/nvme0n1`). Protected system drives can never be selected or targeted.
- **🔒 Persistent User Disk Locking**: Lock any disk permanently from the GUI (`🔒 Lock Disk`). The lock state persists in container storage and permanently forbids all wiping, full writes, and benchmarks.
- **Whole-Disk Enforcement**: Partitions (`/dev/sda1`) and loop devices are strictly rejected.
- **In-Use Disk Refusal**: Rejects any disk with active mounts, active swap, LVM volume memberships, ZFS pool memberships, or RAID holders.
- **Exact Serial Match**: Mandatory manual serial confirmation before executing any destructive action.
- **Single-Job Concurrency Lock**: Enforces a strict hardware mutex to ensure only one intake job executes at a time.

### ⚡ Modular Intake Workflows
1. **Regular Intake Pipeline**: Complete qualification flow (Secure Wipe &rarr; SMART Short Test &rarr; fwupd Check &rarr; 100% Full-Disk Write + CRC32C Verification &rarr; Fio Performance Benchmarks &rarr; Detailed `REPORT.md`).
2. **⚡ SMART Short Self-Test Only (~5m)**: Standalone, non-destructive quick health verification with before/after diffs.
3. **⚡ SMART Extended Self-Test Only (~60m)**: Standalone, non-destructive deep surface & sector scan.
4. **🛡️ Safe Inventory Mode**: Non-destructive baseline snapshot and firmware availability query.
5. **⚙️ Custom Workflows**: Selectively toggle individual stages.

### 📊 Real-Time Live Web Console & Diagnostics
- **Live SSE Terminal**: Server-Sent Events stream line-by-line terminal output with interactive stage progress indicators.
- **Raw Hardware Diagnostics**: Collapsible raw outputs from `smartctl -x`, `hdparm -I`, `udevadm`, `lsscsi`, and `lsblk`.
- **💻 Manual SMART Terminal Helper & Copy Buttons**: Ready-to-copy terminal commands on every drive page for manual execution when running in unprivileged environments.
- **📋 Paste & Parse SMART Output Box**: Interactive GUI paste box to parse terminal SMART output directly into structured metrics (Health, Power-On Hours, Wear %, TBW, and Self-Test history).
- **🛡️ Least-Privilege Sudoers Whitelist**: Included `deploy/ssd-intake.sudoers` allowing unprivileged service users to execute only specific diagnostic binaries with `SSD_INTAKE_USE_SUDO=1`.
- **fwupd Firmware Review**: Inspects device updatability without risking automatic flashing on OEM drives.

### 📦 Zero-Host Storage Footprint
- All reports, registries, and diagnostic logs live **strictly within the container storage** (or named volume `ssd-intake-data`).
- Single-click **"Delete All Logs & Reports"** purge option in the GUI and REST API to instantly wipe all historical data.

---

## 🚀 Quick Start Deployment

```bash
# 1. Clone & build container as root (or transfer from user storage)
git clone https://github.com/Eowerd24/Drive-Intaker.git
cd Drive-Intaker
sudo podman build -t ssd-intake:latest .

# 2. Run container (Rootful Execution with isolated data volume)
sudo podman run -d --name ssd-intake \
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

Access the Web GUI at **`http://localhost:7492`** (or via `ssh -L 7492:127.0.0.1:7492 root@<host-ip>`).

---

## 📦 Release Assets & Checksums

| File | SHA-256 Checksum |
| :--- | :--- |
| `drive-intaker-1.1.0.tar.gz` | `ce92d002c52c2fd42a91736eeaf2988d3b8fcd2f75d4b52bad2bfd11ab8d4d88` |
| `drive-intaker-1.1.0.zip` | `753c725733383d18803ae4598dc3055d67ac678accd1537cfd319d6a73671fc8` |
| `SHA256SUMS.txt` | Included with release bundle |


