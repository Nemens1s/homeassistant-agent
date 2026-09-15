const log = document.getElementById('log');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const resetBtn = document.getElementById('reset');
const form = document.getElementById('row');
const thinkingToggle = document.getElementById('thinking-toggle');

thinkingToggle.addEventListener('click', function () {
  document.body.classList.toggle('show-thinking');
  thinkingToggle.classList.toggle('active');
});

// Thread id persists across page reloads within the browser session.
// crypto.randomUUID() requires a secure context (HTTPS); fall back to
// Math.random()-based UUID v4 when running over plain HTTP (e.g. local HA).
function newUUID() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
    const r = Math.random() * 16 | 0;
    return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
  });
}

let threadId = sessionStorage.getItem('thread');
if (!threadId) {
  threadId = newUUID();
  sessionStorage.setItem('thread', threadId);
}
console.log('[agent] thread', threadId);

// --- Markdown-lite renderer -------------------------------------------------
// Escape HTML first, then render (in order): fenced code blocks, inline code,
// bold, and simple bullet lists. Nothing else.
function escapeHtml(text) {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function renderMarkdown(text) {
  let out = escapeHtml(text);

  // Fenced code blocks: ```...```
  out = out.replace(/```([\s\S]*?)```/g, function (match, code) {
    return '<pre><code>' + code.replace(/^\n/, '') + '</code></pre>';
  });

  // Inline code: `...`
  out = out.replace(/`([^`\n]+)`/g, '<code>$1</code>');

  // Bold: **...**
  out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

  // Bullet lists: consecutive lines starting with "- "
  const lines = out.split('\n');
  const result = [];
  let inList = false;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^- /.test(line)) {
      if (!inList) {
        result.push('<ul>');
        inList = true;
      }
      result.push('<li>' + line.slice(2) + '</li>');
    } else {
      if (inList) {
        result.push('</ul>');
        inList = false;
      }
      result.push(line);
    }
  }
  if (inList) {
    result.push('</ul>');
  }
  return result.join('\n');
}

// --- Message rendering ------------------------------------------------------
function addTurn(role, labelText) {
  const turn = document.createElement('div');
  turn.className = 'turn';
  const label = document.createElement('div');
  label.className = 'role ' + role;
  label.textContent = labelText;
  turn.appendChild(label);
  log.appendChild(turn);
  log.scrollTop = log.scrollHeight;
  return turn;
}

function addUserMsg(text) {
  const turn = addTurn('user', 'You');
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = text;
  turn.appendChild(bubble);
  log.scrollTop = log.scrollHeight;
}

function scrollDown() {
  log.scrollTop = log.scrollHeight;
}

// --- Send / stream ----------------------------------------------------------
let streaming = false;

function setStreaming(on) {
  streaming = on;
  sendBtn.disabled = on;
  input.disabled = on;
}

async function sendMessage() {
  const text = input.value.trim();
  console.log('[agent] sendMessage', { text, streaming });
  if (!text || streaming) return;
  addUserMsg(text);
  input.value = '';
  setStreaming(true);

  // The assistant turn: activity lines (tool calls) sit above the bubble.
  const turn = addTurn('bot', 'Gosling');
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  turn.appendChild(bubble);

  // Track activity lines per tool name so results can update them.
  const activities = {};
  let assistantText = '';
  let thinkingText = '';
  let thinkingDiv = null;

  function ensureActivity(name) {
    if (!activities[name]) {
      const line = document.createElement('div');
      line.className = 'activity';
      line.textContent = '🔍 ' + name;
      turn.insertBefore(line, bubble);
      activities[name] = line;
    }
    return activities[name];
  }

  function showError(message) {
    bubble.className = 'bubble error';
    bubble.textContent = message;
    scrollDown();
  }

  try {
    // Relative path (no leading slash) — required to work correctly
    // behind Home Assistant's ingress path prefix.
    const resp = await fetch('api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, thread_id: threadId }),
    });

    if (!resp.ok || !resp.body) {
      showError('(error) HTTP ' + resp.status);
      setStreaming(false);
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let done = false;

    while (!done) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });

      // SSE frames are separated by a blank line. Keep the trailing
      // incomplete segment in the buffer across reads.
      const frames = buffer.split('\n\n');
      buffer = frames.pop();

      for (let i = 0; i < frames.length; i++) {
        const frame = frames[i].trim();
        if (!frame.startsWith('data:')) continue;
        const payload = frame.slice(frame.indexOf('data:') + 5).trim();
        if (!payload) continue;

        let evt;
        try {
          evt = JSON.parse(payload);
        } catch (parseErr) {
          continue;
        }

        if (evt.type === 'thinking') {
          if (!thinkingDiv) {
            thinkingDiv = document.createElement('div');
            thinkingDiv.className = 'thinking';
            turn.insertBefore(thinkingDiv, bubble);
          }
          thinkingText += evt.text;
          thinkingDiv.textContent = thinkingText;
          scrollDown();
        } else if (evt.type === 'token') {
          assistantText += evt.text;
          bubble.textContent = assistantText;
          scrollDown();
        } else if (evt.type === 'tool_call') {
          ensureActivity(evt.name);
          scrollDown();
        } else if (evt.type === 'tool_result') {
          if (evt.status === 'error') {
            const line = ensureActivity(evt.name);
            line.className = 'activity err';
            line.textContent = '⚠️ ' + evt.name;
          }
        } else if (evt.type === 'done') {
          const finalText = (evt.reply !== undefined && evt.reply !== null)
            ? evt.reply
            : assistantText;
          bubble.className = 'bubble rendered';
          bubble.innerHTML = renderMarkdown(finalText);
          scrollDown();
          done = true;
          break;
        } else if (evt.type === 'error') {
          showError('(error) ' + evt.message);
          done = true;
          break;
        }
      }
    }
  } catch (err) {
    showError('(error) ' + err);
  } finally {
    setStreaming(false);
    input.focus();
  }
}

function newConversation() {
  if (streaming) return;
  threadId = crypto.randomUUID();
  sessionStorage.setItem('thread', threadId);
  log.innerHTML = '';
  input.focus();
}

form.addEventListener('submit', function (e) {
  e.preventDefault();
  sendMessage();
});
resetBtn.addEventListener('click', newConversation);
