/* h-cli WebSocket client — minimal, native API only. */

let ws = null;
let reconnectTimer = null;
const messages = document.getElementById('messages');
const input = document.getElementById('input');
const statusBadge = document.getElementById('status-badge');
const modelBadge = document.getElementById('model-badge');
const modelBtn = document.getElementById('model-btn');
const abortBtn = document.getElementById('abort-btn');
const activityPanel = document.getElementById('activity-panel');
const activityContent = document.getElementById('activity-content');

let currentModel = 'opus';
let taskRunning = false;
let lastActivityData = null;
let activityTimer = null;

function connect() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(proto + '//' + location.host + '/ws');

    ws.onopen = function() {
        statusBadge.textContent = 'connected';
        statusBadge.className = 'badge';
        if (reconnectTimer) {
            clearTimeout(reconnectTimer);
            reconnectTimer = null;
        }
    };

    ws.onclose = function() {
        statusBadge.textContent = 'disconnected';
        statusBadge.className = 'badge disconnected';
        taskRunning = false;
        abortBtn.style.display = 'none';
        if (lastActivityData && !lastActivityData.done) {
            activityContent.innerHTML = '<span class="running">\u26a0 connection lost</span>';
        }
        lastActivityData = null;
        if (activityTimer) { clearInterval(activityTimer); activityTimer = null; }
        reconnectTimer = setTimeout(connect, 3000);
    };

    ws.onmessage = function(event) {
        const data = JSON.parse(event.data);
        handleMessage(data);
    };
}

function handleMessage(data) {
    switch (data.type) {
        case 'result':
            addBotMessage(data.content, data.stats, data.raw);
            taskRunning = false;
            abortBtn.style.display = 'none';
            activityPanel.style.display = 'none';
            break;
        case 'system':
            addSystemMessage(data.content);
            break;
        case 'error':
            addErrorMessage(data.content);
            taskRunning = false;
            abortBtn.style.display = 'none';
            activityPanel.style.display = 'none';
            break;
        case 'task_queued':
            taskRunning = true;
            abortBtn.style.display = '';
            break;
        case 'activity':
            showActivity(data);
            break;
        case 'image':
            addImage(data.content);
            break;
        case 'history':
            restoreHistory(data.turns);
            break;
        case 'pong':
            break;
    }
}

function addUserMessage(text) {
    const div = document.createElement('div');
    div.className = 'msg user';
    div.textContent = text;
    messages.appendChild(div);
    scrollToBottom();
}

function addBotMessage(html, stats, raw) {
    const div = document.createElement('div');
    div.className = 'msg bot';
    div.innerHTML = html;

    // Add copy buttons to code blocks
    div.querySelectorAll('pre').forEach(function(pre) {
        const btn = document.createElement('button');
        btn.className = 'copy-btn';
        btn.textContent = 'Copy';
        btn.onclick = function() {
            const code = pre.querySelector('code');
            const text = code ? code.textContent : pre.textContent;
            navigator.clipboard.writeText(text).then(function() {
                btn.textContent = 'Copied!';
                setTimeout(function() { btn.textContent = 'Copy'; }, 2000);
            });
        };
        pre.style.position = 'relative';
        pre.appendChild(btn);
    });

    // Stats bar
    if (stats) {
        const statsDiv = document.createElement('div');
        statsDiv.className = 'stats-bar';
        statsDiv.textContent = stats.model + ' \u2191 ' +
            stats.input_tokens.toLocaleString() + ' \u2193 ' +
            stats.output_tokens.toLocaleString() + ' | $' +
            stats.cost_usd.toFixed(4) + ' | ' +
            stats.duration_s.toFixed(1) + 's';
        div.appendChild(statsDiv);
    }

    // Download link for long output
    if (raw && raw.length > 3000) {
        const link = document.createElement('a');
        link.className = 'download-link';
        link.textContent = '\u2913 Download as .md';
        link.href = 'data:text/markdown;charset=utf-8,' + encodeURIComponent(raw);
        link.download = 'response.md';
        div.appendChild(link);
    }

    messages.appendChild(div);
    scrollToBottom();
}

function addSystemMessage(html) {
    const div = document.createElement('div');
    div.className = 'system-msg';
    div.innerHTML = html;
    messages.appendChild(div);
    scrollToBottom();
}

function addErrorMessage(text) {
    const div = document.createElement('div');
    div.className = 'msg error';
    div.textContent = text;
    messages.appendChild(div);
    scrollToBottom();
}

function restoreHistory(turns) {
    if (!turns || turns.length === 0) return;
    // Clear welcome message
    messages.innerHTML = '';
    turns.forEach(function(turn) {
        if (turn.role === 'user') {
            addUserMessage(turn.content);
        } else if (turn.role === 'asst') {
            addBotMessage(turn.content);
        }
    });
    scrollToBottom();
}

function addImage(src) {
    const div = document.createElement('div');
    div.className = 'msg bot';
    const img = document.createElement('img');
    img.src = src;
    img.style.maxWidth = '100%';
    img.style.borderRadius = '6px';
    div.appendChild(img);
    messages.appendChild(div);
    scrollToBottom();
}

function showActivity(data) {
    if (data.done) {
        activityPanel.style.display = 'none';
        lastActivityData = null;
        if (activityTimer) { clearInterval(activityTimer); activityTimer = null; }
        return;
    }
    lastActivityData = data;
    lastActivityData._received_at = Date.now();
    renderActivity(data);

    // Start client-side timer for smooth elapsed updates
    if (!activityTimer) {
        activityTimer = setInterval(function() {
            if (lastActivityData && !lastActivityData.done) {
                renderActivity(lastActivityData);
            } else {
                clearInterval(activityTimer);
                activityTimer = null;
            }
        }, 1000);
    }
}

function renderActivity(data) {
    activityPanel.style.display = '';
    const serverAge = (Date.now() - (data._received_at || Date.now())) / 1000;
    let html = '<span class="running">\u23f3 Task ' + data.task_id + '</span>';
    if (data.commands && data.commands.length > 0) {
        html += '<br>';
        data.commands.forEach(function(c) {
            if (c.done) {
                const dur = c.duration !== null ? ' ' + c.duration.toFixed(1) + 's' : '';
                html += '<span class="done">\u2713' + dur + '</span> <span class="cmd">' + escapeHtml(c.cmd) + '</span><br>';
            } else {
                const elapsed = c.elapsed !== undefined ? Math.round(c.elapsed + serverAge) : Math.round(serverAge);
                if (elapsed >= 30) {
                    html += '<span class="running">\u23f3 still running (' + elapsed + 's)</span> <span class="cmd">' + escapeHtml(c.cmd) + '</span><br>';
                } else {
                    html += '<span class="running">\u23f3 running</span> <span class="cmd">' + escapeHtml(c.cmd) + '</span><br>';
                }
            }
        });
    }
    activityContent.innerHTML = html;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function scrollToBottom() {
    messages.scrollTop = messages.scrollHeight;
}

function handleSubmit(event) {
    event.preventDefault();
    const text = input.value.trim();
    if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;

    addUserMessage(text);
    ws.send(JSON.stringify({ type: 'message', content: text }));
    input.value = '';
    return false;
}

function sendCommand(cmd) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    addUserMessage(cmd);
    ws.send(JSON.stringify({ type: 'message', content: cmd }));
}

function toggleModel() {
    if (currentModel === 'opus') {
        currentModel = 'haiku';
        modelBadge.textContent = 'haiku';
        modelBtn.textContent = 'Fast';
        sendCommand('/model fast');
    } else {
        currentModel = 'opus';
        modelBadge.textContent = 'opus';
        modelBtn.textContent = 'Deep';
        sendCommand('/model deep');
    }
}

// Keepalive ping every 30s
setInterval(function() {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'ping' }));
    }
}, 30000);

// Connect on load
connect();
