// SSD Intake Station - Client App Logic

function onDriveSelected(diskPath) {
    selectedDisk = diskPath;
    const drive = candidateDrives[diskPath];

    const dispDisk = document.getElementById('disp-target-disk');
    const dispModel = document.getElementById('disp-target-model');
    const dispSerial = document.getElementById('disp-target-serial');
    const dispSize = document.getElementById('disp-target-size');

    if (drive) {
        if (dispDisk) dispDisk.innerText = diskPath;
        if (dispModel) dispModel.innerText = drive.model;
        if (dispSerial) dispSerial.innerText = drive.serial;
        if (dispSize) dispSize.innerText = drive.size;
    } else {
        if (dispDisk) dispDisk.innerText = 'None selected';
        if (dispModel) dispModel.innerText = '-';
        if (dispSerial) dispSerial.innerText = '-';
        if (dispSize) dispSize.innerText = '-';
    }

    // Reset serial input
    const serialInput = document.getElementById('input-confirm-serial');
    if (serialInput) {
        serialInput.value = '';
    }
    onSerialInputChanged();
}

function onWorkflowModeChanged() {
    const selectedMode = document.querySelector('input[name="workflow_mode"]:checked')?.value || 'full';
    const customOptions = document.getElementById('custom-options');
    const destructiveBox = document.getElementById('destructive-confirmation-box');
    const inventoryBox = document.getElementById('inventory-start-box');

    if (selectedMode === 'custom') {
        if (customOptions) customOptions.classList.remove('hidden');
    } else {
        if (customOptions) customOptions.classList.add('hidden');
    }

    const isNonDestructive = (selectedMode === 'inventory' || selectedMode === 'smart_long' || selectedMode === 'smart_short');
    const startInvBtn = document.getElementById('btn-start-inventory');

    if (isNonDestructive) {
        if (destructiveBox) destructiveBox.classList.add('hidden');
        if (inventoryBox) inventoryBox.classList.remove('hidden');
        if (startInvBtn) {
            if (selectedMode === 'smart_short') {
                startInvBtn.innerText = "⚡ START SHORT SMART TEST (~5M)";
            } else if (selectedMode === 'smart_long') {
                startInvBtn.innerText = "⚡ START EXTENDED SMART TEST (~60M)";
            } else {
                startInvBtn.innerText = "START SAFE INVENTORY ONLY";
            }
        }
    } else {
        if (destructiveBox) destructiveBox.classList.remove('hidden');
        if (inventoryBox) inventoryBox.classList.add('hidden');
        onSerialInputChanged();
    }
}

async function startSmartShortTest(diskPath) {
    if (!confirm(`Launch SMART Short Self-Test (~5 min) on ${diskPath}?\n\nThis will trigger the drive's internal quick electrical/mechanical test and record full diagnostic results.`)) {
        return;
    }

    try {
        const resp = await fetch('/api/jobs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                target_disk: diskPath,
                workflow_mode: 'smart_short'
            })
        });

        const data = await resp.json();
        if (resp.ok) {
            window.location.href = '/jobs/current';
        } else {
            alert("Could not start SMART Short Test: " + (data.detail || JSON.stringify(data)));
        }
    } catch (e) {
        alert("Error starting test: " + e.message);
    }
}

async function startSmartLongTest(diskPath) {
    if (!confirm(`Launch SMART Extended Self-Test (~60 min) on ${diskPath}?\n\nThis will trigger the drive's internal long self-test and record full diagnostic results.`)) {
        return;
    }

    try {
        const resp = await fetch('/api/jobs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                target_disk: diskPath,
                workflow_mode: 'smart_long'
            })
        });

        const data = await resp.json();
        if (resp.ok) {
            window.location.href = '/jobs/current';
        } else {
            alert("Could not start SMART Extended Test: " + (data.detail || JSON.stringify(data)));
        }
    } catch (e) {
        alert("Error starting test: " + e.message);
    }
}

function onSerialInputChanged() {
    const serialInput = document.getElementById('input-confirm-serial');
    const hint = document.getElementById('serial-match-hint');
    const btnStart = document.getElementById('btn-start-destructive');
    const drive = candidateDrives[selectedDisk];

    if (!serialInput || !btnStart || !hint) return;

    if (!drive) {
        btnStart.disabled = true;
        hint.innerText = "Select an eligible candidate drive first.";
        hint.className = "input-hint text-danger mt-1";
        return;
    }

    const entered = serialInput.value.trim();
    const expected = drive.serial.trim();

    if (entered === expected && entered.length > 0) {
        btnStart.disabled = false;
        hint.innerText = "✓ Serial number matches. Destructive intake unlocked.";
        hint.className = "input-hint text-success mt-1";
    } else {
        btnStart.disabled = true;
        if (entered.length === 0) {
            hint.innerText = `Type exact serial '${expected}' to unlock.`;
            hint.className = "input-hint text-secondary mt-1";
        } else {
            hint.innerText = `✗ Serial mismatch. Expected: ${expected}`;
            hint.className = "input-hint text-danger mt-1";
        }
    }
}

async function submitIntakeJob() {
    if (!selectedDisk) {
        alert("Please select a target candidate drive.");
        return;
    }

    const drive = candidateDrives[selectedDisk];
    if (!drive || !drive.is_eligible) {
        alert("The selected drive is not eligible for intake.");
        return;
    }

    const workflowMode = document.querySelector('input[name="workflow_mode"]:checked')?.value || 'full';
    const enteredSerial = document.getElementById('input-confirm-serial')?.value.trim() || '';

    const payload = {
        target_disk: selectedDisk,
        workflow_mode: workflowMode,
        entered_serial: workflowMode !== 'inventory' ? enteredSerial : null,
        skip_firmware: document.getElementById('opt-skip-firmware')?.checked || false,
        skip_long_smart: document.getElementById('opt-skip-long')?.checked || false,
        skip_full_verify: document.getElementById('opt-skip-full-verify')?.checked || false,
        skip_bench: document.getElementById('opt-skip-bench')?.checked || false,
    };

    const activeBtn = workflowMode === 'inventory' 
        ? document.getElementById('btn-start-inventory') 
        : document.getElementById('btn-start-destructive');

    if (activeBtn) {
        activeBtn.disabled = true;
        activeBtn.innerText = "Validating & Starting Job...";
    }

    try {
        const resp = await fetch('/api/jobs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        const data = await resp.json();

        if (resp.ok) {
            window.location.href = '/jobs/current';
        } else {
            const errorDetail = data.detail;
            let msg = "Job start refused:\n";
            if (typeof errorDetail === 'object' && errorDetail.reasons) {
                msg += errorDetail.reasons.join('\n');
            } else if (typeof errorDetail === 'string') {
                msg += errorDetail;
            } else {
                msg += JSON.stringify(errorDetail);
            }
            alert(msg);
            if (activeBtn) {
                activeBtn.disabled = false;
                activeBtn.innerText = workflowMode === 'inventory' ? "START INVENTORY ONLY" : "CONFIRM & START DESTRUCTIVE INTAKE";
            }
        }
    } catch (err) {
        alert("Network or system error: " + err.message);
        if (activeBtn) {
            activeBtn.disabled = false;
            activeBtn.innerText = workflowMode === 'inventory' ? "START INVENTORY ONLY" : "CONFIRM & START DESTRUCTIVE INTAKE";
        }
    }
}

async function cancelActiveJob() {
    if (!confirm("Are you sure you want to cancel the active intake job? This will terminate running tests immediately.")) {
        return;
    }

    const btn = document.getElementById('btn-cancel-job');
    if (btn) btn.disabled = true;

    try {
        const resp = await fetch('/api/jobs/current/cancel', { method: 'POST' });
        const res = await resp.json();
        if (resp.ok) {
            alert("Cancellation signal sent.");
        } else {
            alert("Failed to cancel: " + (res.detail || "Unknown error"));
        }
    } catch (e) {
        alert("Error: " + e.message);
    }
}

async function deleteReport(runId, redirectToList = false) {
    if (!confirm(`Are you sure you want to permanently delete report '${runId}' and all its raw logs?`)) {
        return;
    }

    try {
        const resp = await fetch(`/api/reports/${runId}`, {
            method: 'DELETE'
        });
        const res = await resp.json();
        if (resp.ok) {
            if (redirectToList) {
                window.location.href = '/reports';
            } else {
                const row = document.getElementById(`row-${runId}`);
                if (row) {
                    row.remove();
                } else {
                    window.location.reload();
                }
            }
        } else {
            alert("Could not delete report: " + (res.detail || "Unknown error"));
        }
    } catch (e) {
        alert("Error: " + e.message);
    }
}

async function purgeAllReports() {
    if (!confirm("⚠️ PERMANENT LOG PURGE WARNING\n\nAre you sure you want to delete ALL historical reports, SMART logs, and benchmark files?\nThis action cannot be undone.")) {
        return;
    }

    try {
        const resp = await fetch('/api/reports/purge', {
            method: 'POST'
        });
        const res = await resp.json();
        if (resp.ok) {
            alert(`Successfully purged ${res.deleted_count} report records.`);
            window.location.reload();
        } else {
            alert("Failed to purge reports: " + (res.detail || "Unknown error"));
        }
    } catch (e) {
        alert("Error: " + e.message);
    }
}

function copyTerminalLogs() {
    const terminal = document.getElementById('terminal-body');
    if (!terminal) return;
    const text = terminal.innerText;
    navigator.clipboard.writeText(text).then(() => {
        alert("Logs copied to clipboard!");
    }).catch(err => {
        alert("Could not copy logs: " + err);
    });
}

async function lockDrive(driveName) {
    const confirmation = prompt(
        `🔒 PERMANENT DISK LOCK CONFIRMATION\n\n` +
        `Locking this disk will permanently disable all data destruction operations (Secure Wipe, Full Write & Verify, and Benchmarks).\n\n` +
        `Type "LOCK" to permanently protect this disk:`
    );

    if (confirmation !== "LOCK") {
        if (confirmation !== null) {
            alert("Lock cancelled: Confirmation did not match 'LOCK'.");
        }
        return;
    }

    try {
        const resp = await fetch(`/api/drives/${driveName}/lock`, {
            method: 'POST'
        });
        const data = await resp.json();
        if (resp.ok) {
            alert(`🔒 Disk ${driveName} is now PERMANENTLY LOCKED against data destruction.`);
            window.location.reload();
        } else {
            alert("Could not lock disk: " + (data.detail || JSON.stringify(data)));
        }
    } catch (e) {
        alert("Error locking disk: " + e.message);
    }
}
