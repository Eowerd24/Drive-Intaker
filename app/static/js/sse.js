// SSE Live Streaming for Job Console

let eventSource = null;

function initSSE(jobId) {
    const terminal = document.getElementById('terminal-body');
    const autoscrollChk = document.getElementById('chk-autoscroll');
    const progressBarFill = document.getElementById('progress-bar-fill');
    const progressText = document.getElementById('val-progress-text');
    const statusBadge = document.getElementById('val-status-badge');
    const btnCancel = document.getElementById('btn-cancel-job');
    const btnViewReport = document.getElementById('btn-view-report');

    if (!jobId || jobId === 'None' || jobId === 'pending') {
        jobId = 'current';
    }

    const streamUrl = `/api/jobs/${jobId}/stream`;
    eventSource = new EventSource(streamUrl);

    eventSource.onmessage = function(event) {
        if (!event.data) return;

        try {
            const msg = JSON.parse(event.data);

            if (msg.type === 'log') {
                appendLogLine(msg.data);
            } else if (msg.type === 'stage') {
                updateStageUI(msg.stage, msg.status, msg.progress);
                if (msg.job) {
                    updateJobUI(msg.job);
                }
            } else if (msg.type === 'init' && msg.job) {
                updateJobUI(msg.job);
                if (msg.job.stages_status) {
                    for (const [stage, st] of Object.entries(msg.job.stages_status)) {
                        setStageState(stage, st);
                    }
                }
            }
        } catch (err) {
            console.error("Error parsing SSE event:", err, event.data);
        }
    };

    eventSource.onerror = function(err) {
        console.warn("SSE connection error. Retrying in background...", err);
    };

    function appendLogLine(text) {
        if (!terminal) return;
        const lineDiv = document.createElement('div');
        lineDiv.className = 'terminal-line';
        if (text.includes('[ERROR]') || text.includes('[FATAL]')) {
            lineDiv.classList.add('text-danger');
        } else if (text.includes('[WARN]')) {
            lineDiv.classList.add('text-warning');
        } else if (text.includes('[SUCCESS]')) {
            lineDiv.classList.add('text-success');
        } else if (text.includes('[STATS]')) {
            lineDiv.classList.add('text-highlight');
        }
        lineDiv.innerText = text;
        terminal.appendChild(lineDiv);

        if (autoscrollChk && autoscrollChk.checked) {
            terminal.scrollTop = terminal.scrollHeight;
        }
    }

    function updateStageUI(stage, stageStatus, progress) {
        if (progressBarFill && progress !== undefined) {
            progressBarFill.style.width = progress + '%';
        }
        if (progressText && progress !== undefined) {
            progressText.innerText = progress + '%';
        }
        setStageState(stage, stageStatus);
    }

    function setStageState(stageName, state) {
        const stepElem = document.querySelector(`.stage-step[data-stage="${stageName}"]`);
        if (!stepElem) return;

        stepElem.classList.remove('running', 'passed', 'failed', 'skipped');
        const iconElem = stepElem.querySelector('.stage-icon');

        if (state === 'RUNNING') {
            stepElem.classList.add('running');
            if (iconElem) iconElem.innerHTML = '&#9684;';
        } else if (state === 'PASSED') {
            stepElem.classList.add('passed');
            if (iconElem) iconElem.innerHTML = '&#10004;';
        } else if (state === 'FAILED') {
            stepElem.classList.add('failed');
            if (iconElem) iconElem.innerHTML = '&#10008;';
        } else if (state === 'SKIPPED') {
            stepElem.classList.add('skipped');
            if (iconElem) iconElem.innerHTML = '&#8212;';
        }
    }

    function updateJobUI(job) {
        if (statusBadge) {
            statusBadge.innerText = job.status;
            statusBadge.className = 'badge ' + (
                job.status === 'RUNNING' ? 'badge-running' :
                job.status === 'COMPLETED' ? 'badge-success' :
                job.status === 'FAILED' ? 'badge-danger' :
                job.status === 'CANCELLED' ? 'badge-warning' : 'badge-idle'
            );
        }

        if (job.status === 'COMPLETED') {
            if (btnCancel) btnCancel.disabled = true;
            if (btnViewReport) {
                btnViewReport.href = `/reports/${job.job_id}`;
                btnViewReport.classList.remove('hidden');
            }
        } else if (job.status === 'FAILED' || job.status === 'CANCELLED') {
            if (btnCancel) btnCancel.disabled = true;
            if (btnViewReport) {
                btnViewReport.href = `/reports/${job.job_id}`;
                btnViewReport.classList.remove('hidden');
            }
        }
    }
}
