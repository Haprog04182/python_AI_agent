/* ===================== APP ===================== */
const chatEl = document.getElementById('chat');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('sendBtn');
const statusLine = document.getElementById('statusLine');
const configLine = document.getElementById('configLine');
const modal = document.getElementById('modal');
const modalTitle = document.getElementById('modalTitle');
const modalBody = document.getElementById('modalBody');
const modalClose = document.getElementById('modalClose');
const themeBtn = document.getElementById('themeBtn');
const toolsBtn = document.getElementById('toolsBtn');
const historyBtn = document.getElementById('historyBtn');
const clearBtn = document.getElementById('clearBtn');
const saveBtn = document.getElementById('saveBtn');

const sessionId = (localStorage.getItem('sessionId') || (crypto.randomUUID ? crypto.randomUUID() : String(Date.now() + Math.random())));
localStorage.setItem('sessionId', sessionId);

let isStreaming = false;

/* ===================== THEME ===================== */
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('theme', theme);
  if (themeBtn) {
    themeBtn.textContent = theme === 'dark' ? '🌙' : '☀️';
    themeBtn.title = theme === 'dark' ? 'Светлая тема' : 'Тёмная тема';
  }
}
const savedTheme = localStorage.getItem('theme') || (window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
applyTheme(savedTheme);
themeBtn.addEventListener('click', () => {
  const cur = document.documentElement.getAttribute('data-theme');
  applyTheme(cur === 'dark' ? 'light' : 'dark');
});

/* ===================== UTILS ===================== */
function escapeHtml(s) {
  return String(s)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function simpleMarkdown(text) {
  // Защита от XSS: экранируем ввод, затем аккуратно разрешаем markdown.
  let t = escapeHtml(text);

  // Fenced code blocks
  t = t.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    const safeLang = escapeHtml((lang || '').trim());
    return `<div class="code-wrap"><pre><code class="lang-${safeLang}">${code.trim()}</code><button class="code-copy" type="button">Копировать</button></pre></div>`;
  });
  // Inline code
  t = t.replace(/`([^`\n]+)`/g, (_, code) => `<code>${code}</code>`);
  // Bold
  t = t.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  // Italic
  t = t.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, '<em>$1</em>');
  // Links [text](url)
  t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  // Headings
  t = t.replace(/^#### (.+)$/gm, '<h4>$1</h4>');
  t = t.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  t = t.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  t = t.replace(/^# (.+)$/gm, '<h1>$1</h1>');
  // Unordered lists
  t = t.replace(/(?:^|\n)((?:- .+(?:\n|$))+)/g, (_, block) => {
    const items = block.trim().split('\n').map(l => `<li>${l.replace(/^- /, '')}</li>`).join('');
    return `\n<ul>${items}</ul>\n`;
  });
  // Ordered lists
  t = t.replace(/(?:^|\n)((?:\d+\. .+(?:\n|$))+)/g, (_, block) => {
    const items = block.trim().split('\n').map(l => `<li>${l.replace(/^\d+\. /, '')}</li>`).join('');
    return `\n<ol>${items}</ol>\n`;
  });
  // Line breaks (не внутри блоков)
  t = t.replace(/\n/g, '<br>');
  // Убрать двойные <br> вокруг списков/заголовков
  t = t.replace(/<br>(<\/?(?:ul|ol|li|h[1-4])>)/g, '$1');
  t = t.replace(/(<\/(?:ul|ol|li|h[1-4])>)<br>/g, '$1');
  return t;
}

/* ===================== DOM HELPERS ===================== */
function scrollToBottom() {
  requestAnimationFrame(() => { chatEl.scrollTop = chatEl.scrollHeight; });
}

function addMessage(role, content = '') {
  const msg = document.createElement('div');
  msg.className = `msg ${role}`;
  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.textContent = role === 'user' ? '🧑' : '🤖';
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.innerHTML = simpleMarkdown(content);
  msg.appendChild(avatar);
  msg.appendChild(bubble);
  chatEl.appendChild(msg);
  scrollToBottom();
  return { msg, bubble };
}

function setBubbleContent(bubble, text) {
  bubble.innerHTML = simpleMarkdown(text);
  scrollToBottom();
}

function addActivityContainer(bubble) {
  const act = document.createElement('div');
  act.className = 'activity';
  act.innerHTML = '<span class="status-chip">🔧 инструменты</span>';
  bubble.appendChild(act);
  return act;
}

function addToolCard(activity, name, args) {
  const card = document.createElement('div');
  card.className = 'tool';
  const argsPreview = JSON.stringify(args || {});
  card.innerHTML = `
    <div class="name">🔧 ${escapeHtml(name)}</div>
    <div class="args">${escapeHtml(argsPreview.length > 260 ? argsPreview.slice(0, 260) + '…' : argsPreview)}</div>
    <div class="result" data-result>выполняется…</div>
  `;
  activity.appendChild(card);
  return card;
}

function showEmptyState() {
  if (chatEl.childElementCount > 0) return;
  const el = document.createElement('div');
  el.className = 'empty-state';
  el.id = 'emptyState';
  el.innerHTML = `
    <div class="big">💬</div>
    <h3>Начните разговор</h3>
    <p>Задайте вопрос или предложите задачу. Агент умеет работать с bash, калькулятором, временем и 3D-моделями.</p>
    <div class="chips">
      <button class="chip" data-prompt="Какое сейчас время?">⏰ Время</button>
      <button class="chip" data-prompt="Посчитай sqrt(2) + 3 * 7 и объясни шаги.">🧮 Калькулятор</button>
      <button class="chip" data-prompt="Покажи список доступных инструментов.">🛠 Инструменты</button>
      <button class="chip" data-prompt="Сгенерируй 3D-модель куба и экспортируй в glb.">🎲 3D-модель</button>
    </div>
  `;
  chatEl.appendChild(el);
  el.querySelectorAll('.chip').forEach(c => {
    c.addEventListener('click', () => {
      const p = c.getAttribute('data-prompt');
      if (p) { inputEl.value = p; inputEl.focus(); inputEl.dispatchEvent(new Event('input')); }
    });
  });
}

function removeEmptyState() {
  const e = document.getElementById('emptyState');
  if (e) e.remove();
}

function showTyping(bubble) {
  bubble.innerHTML = '<div class="typing"><span></span><span></span><span></span></div>';
}

/* ===================== API ===================== */
async function loadConfig() {
  try {
    const res = await fetch('/api/config');
    const data = await res.json();
    configLine.textContent = `${data.model} @ ${data.ollama_host}`;
  } catch (e) {
    configLine.textContent = '⚠️ Ollama недоступен?';
  }
  try {
    const res = await fetch('/api/tools');
    const data = await res.json();
    statusLine.textContent = `Инструменты: ${(data.tools || []).join(', ')}`;
  } catch (e) { /* noop */ }
}

function openModal(title, body) {
  modalTitle.textContent = title;
  modalBody.textContent = body;
  modal.classList.remove('hidden');
  document.body.style.overflow = 'hidden';
}
function closeModal() {
  modal.classList.add('hidden');
  document.body.style.overflow = '';
}
modalClose.addEventListener('click', closeModal);
modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !modal.classList.contains('hidden')) closeModal(); });

/* wire modal triggers after DOM ready handled below */
toolsBtn.addEventListener('click', async () => {
  try {
    const res = await fetch('/api/tools');
    const data = await res.json();
    openModal('Доступные инструменты', (data.tools || []).join('\n'));
  } catch (e) { openModal('Ошибка', String(e)); }
});
historyBtn.addEventListener('click', async () => {
  try {
    const res = await fetch(`/api/history?session_id=${encodeURIComponent(sessionId)}`);
    const data = await res.json();
    const lines = (data.messages || []).map(m => `${m.role.toUpperCase()}:\n${m.content}`).join('\n\n———\n\n');
    openModal('История диалога', lines || '(пусто)');
  } catch (e) { openModal('Ошибка', String(e)); }
});
clearBtn.addEventListener('click', async () => {
  try {
    await fetch('/api/clear', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({session_id: sessionId}) });
    chatEl.innerHTML = '';
    showEmptyState();
    statusLine.textContent = 'История очищена.';
  } catch (e) { statusLine.textContent = 'Ошибка очистки.'; }
});
saveBtn.addEventListener('click', async () => {
  try {
    const res = await fetch('/api/save', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({session_id: sessionId}) });
    const data = await res.json();
    statusLine.textContent = data.status || 'Сохранено';
  } catch (e) { statusLine.textContent = 'Ошибка сохранения.'; }
});

/* ===================== COPY CODE ===================== */
document.addEventListener('click', (e) => {
  const btn = e.target.closest('.code-copy');
  if (!btn) return;
  const code = btn.parentElement.querySelector('code');
  if (!code) return;
  const text = code.textContent;
  navigator.clipboard.writeText(text).then(() => {
    const old = btn.textContent;
    btn.textContent = 'Готово ✓';
    setTimeout(() => { btn.textContent = old; }, 1400);
  }).catch(() => {});
});

/* ===================== SSE STREAM ===================== */
async function send() {
  if (isStreaming) return;
  const text = inputEl.value.trim();
  if (!text) return;
  inputEl.value = '';
  inputEl.style.height = 'auto';
  isStreaming = true;
  sendBtn.disabled = true;
  removeEmptyState();

  addMessage('user', text);
  const asst = addMessage('assistant', '');
  showTyping(asst.bubble);
  const activity = addActivityContainer(asst.bubble);
  activity.style.display = 'none';
  let hasTools = false;

  statusLine.textContent = 'Агент думает…';

  try {
    const res = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, message: text }),
    });
    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let firstContent = true;

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const chunk = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const lines = chunk.split('\n');
        let event = 'message';
        let dataStr = '';
        for (const line of lines) {
          if (line.startsWith('event:')) event = line.slice(6).trim();
          else if (line.startsWith('data:')) dataStr += line.slice(5).trim();
        }
        if (!dataStr) continue;
        let data;
        try { data = JSON.parse(dataStr); } catch { data = { raw: dataStr }; }
        handleEvent(event, data, asst, activity, () => { hasTools = true; });
        if (firstContent && (event === 'final' || event === 'assistant')) {
          firstContent = false;
        }
      }
    }
  } catch (e) {
    setBubbleContent(asst.bubble, `⚠️ Ошибка: ${e.message}`);
    activity.remove();
  } finally {
    if (!hasTools && activity.parentNode) activity.remove();
    isStreaming = false;
    sendBtn.disabled = false;
    statusLine.textContent = 'Готов к работе.';
  }
}

function handleEvent(event, data, asst, activity, markTools) {
  if (event === 'start') return;

  if (event === 'tool_call') {
    activity.style.display = '';
    markTools();
    addToolCard(activity, data.name, data.args);
    // оставить место для текста перед activity: вставим typing? уже заменили.
    return;
  }

  if (event === 'tool_result') {
    const cards = activity.querySelectorAll('.tool');
    let target = null;
    for (let i = cards.length - 1; i >= 0; i--) {
      const n = cards[i].querySelector('.name')?.textContent || '';
      if (n.includes(data.name)) { target = cards[i]; break; }
    }
    if (target) {
      const r = target.querySelector('[data-result]');
      if (r) {
        const preview = String(data.result || '');
        r.textContent = preview.length > 420 ? preview.slice(0, 420) + '…' : preview;
        r.classList.toggle('error', !data.ok);
      }
    }
    return;
  }

  if (event === 'final') {
    // Если activity была скрыта — удалить
    // Вставить финальный текст перед activity (или заменить typing)
    const wrapper = document.createElement('div');
    wrapper.innerHTML = simpleMarkdown(data.answer || '');
    // очистить bubble (убрать typing) но сохранить activity
    // Текст вставляем перед activity
    asst.bubble.insertBefore(wrapper, activity);
    // убрать пустой typing? мы вставили typing при старте; удаляем текстовые узлы? typing div
    const typing = asst.bubble.querySelector('.typing');
    if (typing && typing.parentNode !== activity) typing.remove();
    activity.classList.add('done');
    if (!activity.querySelector('.tool')) activity.style.display = 'none';
    scrollToBottom();
    return;
  }

  if (event === 'assistant') {
    // потоковый чанк (если сервер такое шлёт) — пропускаем, используем final
    return;
  }

  if (event === 'error') {
    asst.bubble.innerHTML = '';
    asst.bubble.appendChild(Object.assign(document.createElement('div'), {innerHTML: simpleMarkdown(`⚠️ Ошибка: ${escapeHtml(data.message || 'неизвестно')}`)}));
    activity.remove();
    scrollToBottom();
    return;
  }

  if (event === 'done') return;
}

/* ===================== AUTORESIZE + KEYBOARD ===================== */
inputEl.addEventListener('input', () => {
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 190) + 'px';
});
inputEl.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});
sendBtn.addEventListener('click', send);

/* ===================== INIT ===================== */
loadConfig();
showEmptyState();
inputEl.focus();
