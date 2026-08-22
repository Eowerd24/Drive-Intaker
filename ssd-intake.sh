#!/usr/bin/env bash
set -Eeuo pipefail

# Proxmox SSD intake station
# One candidate drive per run.
# Destructive operations are gated by multiple checks and explicit confirmation.

VERSION="0.1.0"
ROOT_DIR="/root/ssd-intake"
SYSTEM_DISK=""
DISK=""
RUN_MODE="full"
YES=0
SKIP_FW=0
SKIP_LONG=0
SKIP_FULL_VERIFY=0
SKIP_BENCH=0

log()  { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }
warn() { printf '[%s] WARNING: %s\n' "$(date '+%F %T')" "$*" >&2; }
die()  { printf '[%s] ERROR: %s\n' "$(date '+%F %T')" "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage:
  ssd-intake.sh --disk /dev/sdX [options]

Required:
  --disk DEVICE          Candidate drive, e.g. /dev/sdb

Options:
  --system-disk DEVICE   Explicit protected Proxmox boot disk
  --root-dir PATH        Report root (default: /root/ssd-intake)
  --inventory-only       Collect reports only; no wipe/tests/benchmarks
  --skip-firmware        Skip fwupd refresh/get-updates
  --skip-long            Skip SMART extended self-test
  --skip-full-verify     Skip full-device fio write+verify
  --skip-bench           Skip short performance benchmarks
  --yes                  Non-interactive confirmation after safety checks
  -h, --help             Show help

Examples:
  ./ssd-intake.sh --disk /dev/sdb
  ./ssd-intake.sh --disk /dev/sdb --system-disk /dev/sda --skip-bench
EOF
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing command: $1"
}

base_disk_from_partition() {
  local src="$1"
  lsblk -no PKNAME "$src" 2>/dev/null | head -n1
}

detect_system_disk() {
  local root_src pk
  root_src="$(findmnt -rn -o SOURCE /)" || die "Cannot determine root filesystem"
  pk="$(base_disk_from_partition "$root_src" || true)"

  if [[ -n "$pk" ]]; then
    SYSTEM_DISK="/dev/$pk"
    return
  fi

  # Root commonly sits on LVM. Resolve PV backing the VG containing root.
  local vg pv pvpk
  vg="$(lvs --noheadings -o vg_name "$root_src" 2>/dev/null | xargs || true)"
  [[ -n "$vg" ]] || die "Could not auto-detect Proxmox system disk; pass --system-disk"

  pv="$(pvs --noheadings -o pv_name,vg_name 2>/dev/null \
      | awk -v vg="$vg" '$2==vg {print $1; exit}')"
  [[ -n "$pv" ]] || die "Could not find PV for root VG '$vg'; pass --system-disk"

  pvpk="$(lsblk -no PKNAME "$pv" 2>/dev/null | head -n1)"
  [[ -n "$pvpk" ]] || die "Could not resolve parent disk for $pv; pass --system-disk"
  SYSTEM_DISK="/dev/$pvpk"
}

canonical_disk() {
  readlink -f "$1"
}

assert_whole_disk() {
  [[ -b "$DISK" ]] || die "$DISK is not a block device"
  local typ
  typ="$(lsblk -dn -o TYPE "$DISK")"
  [[ "$typ" == "disk" ]] || die "$DISK is not a whole disk (TYPE=$typ)"
}

assert_not_system_disk() {
  local d s
  d="$(canonical_disk "$DISK")"
  s="$(canonical_disk "$SYSTEM_DISK")"
  [[ "$d" != "$s" ]] || die "Refusing: candidate $DISK is the protected system disk $SYSTEM_DISK"

  if lsblk -nrpo NAME "$SYSTEM_DISK" | grep -Fxq "$d"; then
    die "Refusing: candidate belongs to protected system disk"
  fi
}

assert_not_in_use() {
  local blockers=0

  if findmnt -rn -S "${DISK}*" 2>/dev/null | grep -q .; then
    warn "Mounted filesystem detected on $DISK"
    findmnt -rn -S "${DISK}*" >&2 || true
    blockers=1
  fi

  if swapon --noheadings --show=NAME 2>/dev/null | grep -Eq "^${DISK}([0-9p]|$)"; then
    warn "Swap detected on $DISK"
    blockers=1
  fi

  if pvs --noheadings -o pv_name 2>/dev/null | xargs -r -n1 \
      | grep -Eq "^${DISK}([0-9p]|$)"; then
    warn "LVM physical volume detected on $DISK"
    blockers=1
  fi

  if zpool status -P 2>/dev/null | grep -Fq "$DISK"; then
    warn "ZFS appears to reference $DISK"
    blockers=1
  fi

  [[ "$blockers" -eq 0 ]] || die "Candidate disk is in use; detach it from storage first"
}

safe_id() {
  printf '%s' "$1" | tr -cs 'A-Za-z0-9._-' '_'
}

collect_identity() {
  MODEL="$(smartctl -i "$DISK" | awk -F: '/Device Model|Product/ {sub(/^[ \t]+/,"",$2); print $2; exit}')"
  SERIAL="$(smartctl -i "$DISK" | awk -F: '/Serial Number/ {sub(/^[ \t]+/,"",$2); print $2; exit}')"
  FW="$(smartctl -i "$DISK" | awk -F: '/Firmware Version/ {sub(/^[ \t]+/,"",$2); print $2; exit}')"
  CAPACITY="$(lsblk -dn -o SIZE "$DISK" | xargs)"
  TRAN="$(lsblk -dn -o TRAN "$DISK" | xargs)"
  [[ -n "$SERIAL" ]] || SERIAL="$(udevadm info --query=property --name="$DISK" | sed -n 's/^ID_SERIAL_SHORT=//p' | head -n1)"
  [[ -n "$SERIAL" ]] || SERIAL="$(basename "$DISK")-unknown-serial"
}

setup_run_dir() {
  RUN_ID="$(date '+%Y%m%d-%H%M%S')-$(safe_id "$SERIAL")"
  RUN_DIR="$ROOT_DIR/$RUN_ID"
  mkdir -p "$RUN_DIR"/{before,after,tests,benchmarks,firmware}
  chmod 700 "$RUN_DIR"
  ln -sfn "$RUN_DIR" "$ROOT_DIR/latest"
}

collect_snapshot() {
  local phase="$1"
  local dir="$RUN_DIR/$phase"
  mkdir -p "$dir"

  smartctl -x "$DISK" | tee "$dir/smart.txt"
  smartctl -x -j "$DISK" > "$dir/smart.json" || true
  hdparm -I "$DISK" > "$dir/hdparm.txt" 2>&1 || true
  udevadm info --query=property --name="$DISK" > "$dir/udev.txt" 2>&1 || true
  lsblk -O "$DISK" > "$dir/lsblk.txt"
  lsscsi -g > "$dir/lsscsi.txt" 2>&1 || true
}

show_target() {
  cat <<EOF

Candidate:
  Device:       $DISK
  Model:        $MODEL
  Serial:       $SERIAL
  Capacity:     $CAPACITY
  Firmware:     $FW
  Transport:    $TRAN

Protected system disk:
  $SYSTEM_DISK

Reports:
  $RUN_DIR
EOF
}

confirm_destroy() {
  [[ "$RUN_MODE" == "inventory" ]] && return 0

  if [[ "$YES" -eq 1 ]]; then
    return 0
  fi

  echo
  echo "THIS WILL PERMANENTLY DESTROY ALL DATA ON $DISK."
  echo "Type the exact serial number to continue:"
  read -r answer
  [[ "$answer" == "$SERIAL" ]] || die "Serial confirmation did not match"
}

wipe_disk() {
  log "Unmounting any stale partitions"
  while read -r part; do
    umount "$part" 2>/dev/null || true
  done < <(lsblk -lnpo NAME,TYPE "$DISK" | awk '$2=="part"{print $1}')

  log "Destroying GPT/MBR metadata"
  sgdisk --zap-all "$DISK"
  wipefs -a "$DISK" || true

  if command -v partprobe >/dev/null 2>&1; then
    partprobe "$DISK" || true
  else
    blockdev --rereadpt "$DISK" || true
  fi

  udevadm settle
  sleep 2

  # Retry after kernel reread.
  wipefs -a "$DISK"
  sync

  if wipefs "$DISK" | grep -q .; then
    die "Signatures remain after wipe; inspect manually"
  fi
}

run_short_test() {
  log "Starting SMART short self-test"
  smartctl -t short "$DISK" | tee "$RUN_DIR/tests/smart-short-start.txt"
  local mins
  mins="$(smartctl -c "$DISK" | awk '/Short self-test routine recommended polling time/ {gsub(/[()]/,""); print $(NF-1); exit}')"
  [[ "$mins" =~ ^[0-9]+$ ]] || mins=2
  sleep $((mins * 60 + 15))
  smartctl -l selftest "$DISK" | tee "$RUN_DIR/tests/smart-short-result.txt"
}

run_long_test() {
  log "Starting SMART extended self-test"
  smartctl -t long "$DISK" | tee "$RUN_DIR/tests/smart-long-start.txt"
  local mins
  mins="$(smartctl -c "$DISK" | awk '/Extended self-test routine recommended polling time/ {gsub(/[()]/,""); print $(NF-1); exit}')"
  [[ "$mins" =~ ^[0-9]+$ ]] || mins=60
  log "Waiting approximately $mins minutes for the drive's internal test"
  sleep $((mins * 60 + 30))
  smartctl -l selftest "$DISK" | tee "$RUN_DIR/tests/smart-long-result.txt"
}

firmware_check() {
  if [[ "$SKIP_FW" -eq 1 ]]; then
    log "Firmware check skipped"
    return
  fi

  log "Refreshing fwupd metadata"
  fwupdmgr refresh --force > "$RUN_DIR/firmware/refresh.txt" 2>&1 || true
  fwupdmgr get-devices > "$RUN_DIR/firmware/devices.txt" 2>&1 || true
  fwupdmgr get-updates > "$RUN_DIR/firmware/updates.txt" 2>&1 || true

  # Deliberately do not auto-flash. Enterprise/OEM SSD firmware must be reviewed.
  log "Firmware availability recorded; automatic flashing is intentionally disabled"
}

full_verify() {
  [[ "$SKIP_FULL_VERIFY" -eq 1 ]] && { log "Full-device verification skipped"; return; }

  log "Running destructive full-device write with CRC verification metadata"
  fio \
    --name=full-write \
    --filename="$DISK" \
    --direct=1 \
    --rw=write \
    --bs=1M \
    --iodepth=16 \
    --verify=crc32c \
    --verify_state_save=1 \
    --group_reporting \
    | tee "$RUN_DIR/tests/full-write.txt"

  log "Running full-device verification read"
  fio \
    --name=full-verify \
    --filename="$DISK" \
    --direct=1 \
    --rw=read \
    --bs=1M \
    --iodepth=16 \
    --verify=crc32c \
    --verify_only=1 \
    --group_reporting \
    | tee "$RUN_DIR/tests/full-verify.txt"
}

benchmarks() {
  [[ "$SKIP_BENCH" -eq 1 ]] && { log "Benchmarks skipped"; return; }

  log "Sequential read benchmark"
  fio \
    --name=seq-read \
    --filename="$DISK" \
    --direct=1 \
    --rw=read \
    --bs=1M \
    --iodepth=32 \
    --numjobs=1 \
    --runtime=60 \
    --time_based \
    --group_reporting \
    | tee "$RUN_DIR/benchmarks/seq-read.txt"

  log "Sequential write benchmark"
  fio \
    --name=seq-write \
    --filename="$DISK" \
    --direct=1 \
    --rw=write \
    --bs=1M \
    --iodepth=32 \
    --numjobs=1 \
    --runtime=60 \
    --time_based \
    --group_reporting \
    | tee "$RUN_DIR/benchmarks/seq-write.txt"

  log "4K random mixed benchmark"
  fio \
    --name=rand-mixed \
    --filename="$DISK" \
    --direct=1 \
    --rw=randrw \
    --rwmixread=70 \
    --bs=4k \
    --iodepth=32 \
    --numjobs=1 \
    --runtime=120 \
    --time_based \
    --group_reporting \
    | tee "$RUN_DIR/benchmarks/rand-mixed.txt"
}

generate_report() {
  local smart_after="$RUN_DIR/after/smart.txt"
  local health realloc uncorrect crc temp poh fw_after selftests fw_status

  health="$(awk -F: '/SMART overall-health self-assessment test result/ {sub(/^[ \t]+/,"",$2); print $2; exit}' "$smart_after")"
  realloc="$(awk '$1==5 {print $NF; exit}' "$smart_after")"
  uncorrect="$(awk '$1==187 {print $NF; exit}' "$smart_after")"
  crc="$(awk '$1==199 {print $NF; exit}' "$smart_after")"
  temp="$(awk '$1==194 {print $10; exit}' "$smart_after")"
  poh="$(awk '$1==9 {print $NF; exit}' "$smart_after")"
  fw_after="$(awk -F: '/Firmware Version/ {sub(/^[ \t]+/,"",$2); print $2; exit}' "$smart_after")"
  selftests="$(sed -n '/SMART Extended Self-test Log/,/SMART Selective self-test log/p' "$smart_after" | head -n 20 || true)"

  fw_status="not checked"
  if [[ -f "$RUN_DIR/firmware/updates.txt" ]]; then
    if grep -qiE 'No updatable devices|no available firmware updates' "$RUN_DIR/firmware/updates.txt"; then
      fw_status="no update offered by enabled fwupd remotes"
    elif grep -qiE 'Upgrade|Update available|New version' "$RUN_DIR/firmware/updates.txt"; then
      fw_status="update may be available; manual review required"
    else
      fw_status="fwupd result inconclusive; inspect firmware/updates.txt"
    fi
  fi

  cat > "$RUN_DIR/REPORT.md" <<EOF
# SSD Intake Report

- Run: \`$RUN_ID\`
- Device during test: \`$DISK\`
- Model: \`$MODEL\`
- Serial: \`$SERIAL\`
- Capacity: \`$CAPACITY\`
- Transport: \`$TRAN\`
- Firmware before: \`$FW\`
- Firmware after: \`$fw_after\`
- Firmware status: $fw_status
- Protected system disk: \`$SYSTEM_DISK\`

## Final SMART summary

- Overall health: \`${health:-unknown}\`
- Power-on hours: \`${poh:-unknown}\`
- Reallocated sectors: \`${realloc:-unknown}\`
- Uncorrectable errors: \`${uncorrect:-unknown}\`
- CRC errors: \`${crc:-unknown}\`
- Temperature: \`${temp:-unknown}\`

## Self-test extract

\`\`\`text
$selftests
\`\`\`

## Files

- \`before/\`: initial SMART, hdparm, udev and lsblk data
- \`after/\`: final state
- \`tests/\`: SMART and full verification results
- \`benchmarks/\`: fio benchmark output
- \`firmware/\`: fwupd discovery and update availability

## Classification

Set manually after reviewing the report:

- [ ] PASS-A — clean tests, healthy SMART, low/moderate wear
- [ ] PASS-B — usable, but older/high-wear or with minor caveats
- [ ] LAB — disposable/scratch workloads only
- [ ] REJECT — errors, resets, failed self-test, increasing defect counts
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --disk) DISK="${2:-}"; shift 2 ;;
    --system-disk) SYSTEM_DISK="${2:-}"; shift 2 ;;
    --root-dir) ROOT_DIR="${2:-}"; shift 2 ;;
    --inventory-only) RUN_MODE="inventory"; shift ;;
    --skip-firmware) SKIP_FW=1; shift ;;
    --skip-long) SKIP_LONG=1; shift ;;
    --skip-full-verify) SKIP_FULL_VERIFY=1; shift ;;
    --skip-bench) SKIP_BENCH=1; shift ;;
    --yes) YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "Run as root"
[[ -n "$DISK" ]] || { usage; exit 2; }

for cmd in smartctl lsblk findmnt pvs lvs sgdisk wipefs udevadm hdparm fio lsscsi fwupdmgr; do
  need_cmd "$cmd"
done

DISK="$(canonical_disk "$DISK")"
[[ -n "$SYSTEM_DISK" ]] || detect_system_disk
SYSTEM_DISK="$(canonical_disk "$SYSTEM_DISK")"

assert_whole_disk
assert_not_system_disk
assert_not_in_use
collect_identity
setup_run_dir
show_target
collect_snapshot before

if [[ "$RUN_MODE" == "inventory" ]]; then
  cp -a "$RUN_DIR/before" "$RUN_DIR/after"
  generate_report
  log "Inventory complete: $RUN_DIR/REPORT.md"
  exit 0
fi

confirm_destroy
wipe_disk
run_short_test
[[ "$SKIP_LONG" -eq 1 ]] || run_long_test
firmware_check
full_verify
benchmarks
collect_snapshot after
generate_report

log "SSD intake complete"
log "Report: $RUN_DIR/REPORT.md"
