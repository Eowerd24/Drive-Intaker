# Proxmox SSD Intake Station

## Install dependencies

```bash
apt update
apt install -y smartmontools sg3-utils lsscsi hdparm fio gdisk parted fwupd jq
```

## Install the script

```bash
install -m 0750 ssd-intake.sh /usr/local/sbin/ssd-intake
```

## First run: inventory only

```bash
ssd-intake --disk /dev/sdb --system-disk /dev/sda --inventory-only
```

## Full destructive intake

```bash
ssd-intake --disk /dev/sdb --system-disk /dev/sda
```

The script requires typing the candidate drive's exact serial number before it destroys data.

## Important firmware behavior

The script checks `fwupd` and records whether an update is offered. It does **not**
automatically flash firmware. `Updatable` only means the drive supports a firmware
update mechanism; it does not mean an update is currently available.

Review:

```bash
cat /root/ssd-intake/latest/firmware/updates.txt
```

Only install firmware after confirming the update explicitly matches the complete
model/OEM variant.

## Reports

Each run is stored under:

```text
/root/ssd-intake/YYYYMMDD-HHMMSS-SERIAL/
```

The main summary is:

```text
REPORT.md
```

## Suggested initial use

For the current Samsung 960 GB candidate:

```bash
ssd-intake \
  --disk /dev/sdb \
  --system-disk /dev/sda
```

For quicker testing without a full-device verification:

```bash
ssd-intake \
  --disk /dev/sdb \
  --system-disk /dev/sda \
  --skip-full-verify
```
