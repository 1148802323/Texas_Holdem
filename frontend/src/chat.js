// Volatile room chat. Keep these DOM nodes when the table receives a new state.
function element(tag, text = '', className = '') {
  const item = document.createElement(tag);
  item.textContent = text;
  item.className = className;
  return item;
}

export class RoomChat {
  constructor({ isVisible, onUnreadChange, onSend }) {
    Object.assign(this, { isVisible, onUnreadChange, onSend });
    this.playerId = null;
    this.messages = [];
    this.ids = new Set();
    this.unread = 0;
    this.atBottom = true;
    this.savedScrollTop = 0;
    this.hadHistory = false;
    this.connected = false;
    this.closed = false;
    this.sending = false;
    this.request = null;
    this.root = element('section', '', 'chat-content');
    this.root.id = 'chat-content';
    this.root.setAttribute('role', 'tabpanel');
    this.root.setAttribute('aria-labelledby', 'chat-tab');
    this.list = element('div', '', 'chat-messages');
    this.list.setAttribute('role', 'log');
    this.list.setAttribute('aria-label', '房间聊天消息');
    this.list.setAttribute('aria-live', 'polite');
    this.list.setAttribute('aria-relevant', 'additions');
    this.jump = element('button', '有新消息 ↓', 'btn secondary mini chat-jump');
    this.jump.type = 'button'; this.jump.hidden = true;
    this.jump.addEventListener('click', () => {
      this.atBottom = true; this.syncVisibility();
    });
    this.form = element('form', '', 'chat-compose');
    const label = element('label', '发送消息', 'chat-label');
    this.input = element('textarea');
    this.input.id = 'chat-input'; this.input.rows = 2;
    this.input.placeholder = '聊两句…'; this.input.maxLength = 400;
    label.append(this.input);
    const controls = element('div', '', 'chat-compose-controls');
    this.counter = element('span', '0/200', 'muted small');
    this.sendButton = element('button', '发送', 'btn mini');
    this.sendButton.type = 'submit'; controls.append(this.counter, this.sendButton);
    this.status = element('p', '', 'chat-status muted small');
    this.status.setAttribute('role', 'status');
    this.form.append(label, controls, this.status);
    this.root.append(this.list, this.jump, this.form,
      element('p', '聊天仅保留到房间删除或服务重启。', 'chat-retention muted small'));
    this.form.addEventListener('submit', event => { event.preventDefault(); this.send(); });
    this.input.addEventListener('keydown', event => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
        event.preventDefault(); this.send();
      }
    });
    const inputChanged = event => {
      if (event.isComposing) return;
      this.input.value = Array.from(this.input.value).slice(0, 200).join('');
      this.updateComposer();
    };
    this.input.addEventListener('input', inputChanged);
    this.input.addEventListener('compositionend', inputChanged);
    this.list.addEventListener('scroll', () => {
      if (!this.list.clientHeight || !this.isVisible() || document.hidden) return;
      this.captureScroll();
      if (this.atBottom && this.isVisible()) this.setUnread(0);
    });
    document.addEventListener('visibilitychange', () => this.syncVisibility());
    this.renderMessages(); this.updateComposer();
  }

  reset() {
    clearTimeout(this.sendTimeout);
    this.playerId = null; this.messages = []; this.ids.clear();
    this.hadHistory = false; this.atBottom = true; this.savedScrollTop = 0;
    this.connected = false; this.sending = false; this.request = null;
    this.input.value = ''; this.status.textContent = '';
    delete this.status.dataset.error;
    this.setUnread(0); this.renderMessages(); this.updateComposer();
  }

  captureScroll() {
    if (!this.list.clientHeight || !this.isVisible() || document.hidden) return;
    this.savedScrollTop = this.list.scrollTop;
    this.atBottom = this.list.scrollHeight - this.list.scrollTop - this.list.clientHeight < 24;
  }

  captureView() {
    this.captureScroll();
    this.focus = document.activeElement === this.input
      ? [this.input.selectionStart, this.input.selectionEnd] : null;
  }

  mount(placeholder, playerId, closed) {
    if (this.playerId !== playerId) { this.reset(); this.playerId = playerId; }
    this.closed = closed;
    placeholder.replaceWith(this.root);
    this.updateComposer();
    if (this.focus && !this.root.hidden) {
      this.input.focus({ preventScroll: true });
      this.input.setSelectionRange(...this.focus);
    }
    this.focus = null;
  }

  setConnected(connected) {
    this.connected = connected;
    if (!connected) {
      clearTimeout(this.sendTimeout); this.sending = false;
      delete this.status.dataset.error;
    }
    this.updateComposer();
  }

  updateComposer() {
    const length = Array.from(this.input.value).length;
    this.counter.textContent = `${length}/200`;
    this.input.disabled = this.closed;
    this.sendButton.disabled = this.closed || !this.connected || this.sending || !this.input.value.trim();
    this.sendButton.textContent = this.sending ? '发送中…' : '发送';
    if (this.closed) this.status.textContent = '牌桌已关闭，聊天已清空。';
    else if (!this.connected) this.status.textContent = '聊天连接中，可先填写消息…';
    else if (!this.status.dataset.error) this.status.textContent = 'Enter 发送 · Shift+Enter 换行';
  }

  setUnread(count) {
    const changed = this.unread !== count;
    this.unread = count;
    this.jump.hidden = !count;
    this.jump.textContent = `有 ${count} 条新消息 ↓`;
    if (changed) this.onUnreadChange(count);
  }

  syncVisibility() {
    if (!this.isVisible() || document.hidden) return;
    if (this.atBottom) {
      this.list.scrollTop = this.list.scrollHeight;
      this.setUnread(0);
    } else this.list.scrollTop = this.savedScrollTop;
  }

  messageNode(message) {
    const row = element('article', '', `chat-message${message.player_id === this.playerId ? ' own' : ''}`);
    row.dataset.messageId = message.id;
    const meta = element('div', '', 'chat-meta');
    const stamp = new Date(message.sent_at * 1000);
    const time = element('time', stamp.toLocaleString('zh-CN', {
      month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
    }));
    time.dateTime = stamp.toISOString();
    meta.append(element('strong', message.nickname), time);
    row.append(meta, element('div', message.text, 'chat-bubble'));
    return row;
  }

  renderMessages() {
    const scrollTop = this.savedScrollTop;
    this.list.replaceChildren();
    if (!this.messages.length) this.list.append(element('p', '暂无消息，入座玩家和旁观者都可以聊天。', 'chat-empty muted small'));
    for (const message of this.messages) this.list.append(this.messageNode(message));
    this.list.scrollTop = scrollTop;
    this.syncVisibility();
  }

  acknowledge(message) {
    if (message.player_id !== this.playerId || message.request_id !== this.request?.id) return;
    clearTimeout(this.sendTimeout);
    if (this.input.value.trim() === this.request.text) this.input.value = '';
    this.request = null; this.sending = false;
    delete this.status.dataset.error;
    this.updateComposer();
  }

  receiveHistory(payload) {
    this.captureScroll();
    const newMessages = payload.messages.filter(message => !this.ids.has(message.id));
    const countUnread = this.hadHistory && (!this.isVisible() || !this.atBottom || document.hidden);
    this.closed = payload.closed; this.connected = true;
    this.messages = payload.messages;
    this.ids = new Set(this.messages.map(message => message.id));
    this.hadHistory = true;
    for (const message of this.messages) this.acknowledge(message);
    if (this.closed) this.setUnread(0);
    else if (countUnread) this.setUnread(this.unread + newMessages.filter(message => message.player_id !== this.playerId).length);
    this.renderMessages(); this.updateComposer();
  }

  receiveMessage(message) {
    this.acknowledge(message);
    if (this.ids.has(message.id)) return;
    this.captureScroll();
    this.ids.add(message.id); this.messages.push(message);
    this.list.querySelector('.chat-empty')?.remove();
    this.list.append(this.messageNode(message));
    if (this.atBottom) this.list.scrollTop = this.list.scrollHeight;
    if (message.player_id !== this.playerId && (!this.isVisible() || !this.atBottom || document.hidden))
      this.setUnread(this.unread + 1);
    this.syncVisibility();
  }

  showError(detail, requestId = null) {
    if (requestId && this.request && requestId !== this.request.id) return;
    clearTimeout(this.sendTimeout); this.sending = false;
    this.status.dataset.error = 'true'; this.status.textContent = detail;
    this.updateComposer();
  }

  send() {
    const text = this.input.value.trim();
    if (!text || this.sending || !this.connected || this.closed) return;
    if (Array.from(this.input.value).length > 200) return this.showError('每条消息最多 200 字。');
    if (this.request?.text !== text) this.request = { id: crypto.randomUUID(), text };
    delete this.status.dataset.error;
    this.sending = true; this.updateComposer();
    try {
      this.onSend({ type: 'chat_send', text, request_id: this.request.id });
      this.sendTimeout = setTimeout(() => this.showError('尚未确认发送，可点击发送重试；同一消息不会重复保存。'), 8000);
    } catch {
      this.showError('连接已中断，请等待重连后重试。');
    }
  }
}
