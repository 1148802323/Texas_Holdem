const app = document.querySelector('#app');
const roomId = location.pathname.startsWith('/r/') ? location.pathname.split('/')[2] : null;
let socket = null;
let roomData = null;
let busy = false;

function node(tag, value = '', className = '') {
  const element = document.createElement(tag);
  if (className) element.className = className;
  element.textContent = String(value);
  return element;
}

function notice(message, kind = 'error') {
  let box = document.querySelector('#notice');
  if (!box) { box = node('div'); box.id = 'notice'; app.prepend(box); }
  box.className = `notice ${kind}`;
  box.textContent = message;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' }, ...options,
  });
  let data;
  try { data = await response.json(); } catch { data = {}; }
  if (!response.ok) throw Object.assign(new Error(data.detail || `请求失败：${response.status}`), { status: response.status });
  return data;
}

function money(value) { return Number(value || 0).toLocaleString('zh-CN'); }
function label(action) {
  return ({ fold: '弃牌', check: '过牌', call: '跟注', bet: '下注', raise: '加注' })[action] || action;
}
function card(code) {
  if (!code) return node('span', '?', 'playing-card empty');
  if (code === 'back') return node('span', '♠', 'playing-card back');
  const suits = { c: '♣', d: '♦', h: '♥', s: '♠' };
  return node('span', `${code[0]}${suits[code[1]] || '?'}`,
    `playing-card${code[1] === 'd' || code[1] === 'h' ? ' red' : ''}`);
}

function renderHome() {
  app.innerHTML = `<section class="hero"><div class="eyebrow">PRIVATE POKER ROOM</div><h1>属于朋友们的<br>私人德州牌桌</h1><p>由房主创建牌局并分享链接。加入时只需昵称，牌局和买入记录由服务器保存。</p></section><div class="grid"><section class="panel"><h2>已有邀请？</h2><p>粘贴完整邀请链接，或输入房间代码。</p><form id="open-room"><label class="field">邀请链接或房间代码<input name="code" required autocomplete="off" placeholder="https://…/r/…"></label><button class="btn" type="submit">进入牌桌 →</button></form></section><section class="panel"><h2>我是房主</h2><p>从管理页创建牌局、设置盲注和买入上限，再把邀请链接发给朋友。</p><a class="btn secondary" href="/admin">打开管理页面 →</a></section></div>`;
  document.querySelector('#open-room').addEventListener('submit', event => {
    event.preventDefault();
    const input = new FormData(event.currentTarget).get('code').trim();
    let id = input;
    try { if (input.includes('/')) id = new URL(input).pathname.split('/r/')[1] || ''; } catch { id = ''; }
    if (!/^[a-f0-9]{32}$/i.test(id)) return notice('请输入有效的邀请链接或房间代码。');
    location.href = `/r/${id}`;
  });
}

async function renderAdmin() {
  try { await api('/api/admin/session'); } catch { return showAdminLogin(); }
  app.innerHTML = `<section class="hero compact"><div class="eyebrow">ROOM CONTROL</div><h1>牌局管理</h1><p>创建私人牌桌，设置规则并分享邀请链接。</p></section><div class="grid"><section class="panel"><h2>创建新牌局</h2><form id="create-room"><div class="row"><label class="field">小盲注<input name="sb" type="number" min="1" value="10" required></label><label class="field">大盲注<input name="bb" type="number" min="2" value="20" required></label></div><div class="row"><label class="field">牌桌人数<input name="seats" type="number" min="2" max="9" value="6" required></label><label class="field">买入后筹码上限<input name="cap" type="number" min="2" value="2000" required></label></div><label class="field">指定可用昵称（选填，用逗号分隔）<textarea name="names" rows="2" placeholder="留空允许自由输入昵称"></textarea></label><button class="btn" type="submit">创建并生成邀请链接</button></form><div id="created"></div></section><section class="panel"><div class="row"><h2>我的牌局</h2><button id="logout" class="btn secondary mini right">退出管理</button></div><div id="rooms"></div></section></div>`;
  document.querySelector('#create-room').addEventListener('submit', async event => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const names = String(form.get('names')).split(/[,，\n]/).map(x => x.trim()).filter(Boolean);
    try {
      const result = await api('/api/admin/rooms', { method: 'POST', body: JSON.stringify({
        small_blind: Number(form.get('sb')), big_blind: Number(form.get('bb')),
        max_players: Number(form.get('seats')), max_buyin_stack: Number(form.get('cap')),
        allowed_nicknames: names.length ? names : null,
      }) });
      const output = document.querySelector('#created'); output.replaceChildren();
      output.append(node('div', '邀请链接已生成', 'notice ok'));
      const link = node('a', result.invite_url, 'mono'); link.href = result.invite_url;
      output.append(link);
      const copy = node('button', '复制链接', 'btn secondary mini'); copy.type = 'button';
      copy.addEventListener('click', () => navigator.clipboard.writeText(result.invite_url).then(() => notice('邀请链接已复制。', 'ok')));
      output.append(node('div', ''));
      output.append(copy);
      await loadAdminRooms();
    } catch (error) { notice(error.message); }
  });
  document.querySelector('#logout').addEventListener('click', async () => {
    await api('/api/admin/logout', { method: 'POST', body: '{}' }); showAdminLogin();
  });
  await loadAdminRooms();
}

function showAdminLogin() {
  app.innerHTML = `<section class="hero compact"><div class="eyebrow">PRIVATE CONTROL</div><h1>房主验证</h1><p>只有房主可以创建和开始牌局。</p></section><section class="panel" style="max-width:440px"><h2>输入管理密码</h2><form id="admin-login"><label class="field">管理密码<input name="password" type="password" required autocomplete="current-password"></label><button class="btn" type="submit">进入管理页</button></form></section>`;
  document.querySelector('#admin-login').addEventListener('submit', async event => {
    event.preventDefault();
    try {
      await api('/api/admin/login', { method: 'POST', body: JSON.stringify({ password: new FormData(event.currentTarget).get('password') }) });
      await renderAdmin();
    } catch (error) { notice(error.message); }
  });
}

async function loadAdminRooms() {
  const container = document.querySelector('#rooms'); if (!container) return;
  try {
    const rooms = await api('/api/admin/rooms'); container.replaceChildren();
    if (!rooms.length) return container.append(node('p', '还没有牌局。', 'muted'));
    for (const room of rooms) {
      const overview = await api(`/api/admin/rooms/${room.room_id}`);
      const item = node('div', '', 'room-item'); const info = node('div');
      info.append(node('div', `牌局 ${room.room_id.slice(0, 8)}`, 'room-item-title'));
      info.append(node('p', `盲注 ${room.small_blind}/${room.big_blind} · ${room.max_players} 人 · 买入上限 ${money(room.max_buyin_stack)}`));
      info.append(node('p', overview.players.length ? overview.players.map(p =>
        `${p.nickname}：${money(p.stack)} 筹码 / 累计买入 ${money(p.total_buyin)}`).join(' · ') : '尚无人入座'));
      const controls = node('div', '', 'row');
      const link = node('a', '邀请链接', 'btn secondary mini'); link.href = `/r/${room.room_id}`;
      const start = node('button', '开始下一手', 'btn mini'); start.type = 'button';
      start.addEventListener('click', async () => {
        try {
          await api(`/api/admin/rooms/${room.room_id}/hands`, { method: 'POST', body: JSON.stringify({ request_id: crypto.randomUUID() }) });
          notice('手牌已开始。', 'ok'); await loadAdminRooms();
        } catch (error) { notice(error.message); }
      });
      controls.append(link, start); item.append(info, controls); container.append(item);
    }
  } catch (error) { notice(error.message); }
}

async function enterRoom() {
  try {
    roomData = await api(`/api/rooms/${roomId}`);
    const state = await api(`/api/rooms/${roomId}/state`);
    renderRoom(state); connectSocket();
  } catch (error) {
    if (error.status === 401) return renderJoin(roomData);
    app.innerHTML = ''; notice(error.message);
  }
}

function renderJoin(room) {
  if (!room) return;
  const taken = new Set(room.players.map(p => p.seat));
  app.innerHTML = `<section class="hero compact"><div class="eyebrow">YOU ARE INVITED</div><h1>加入私人牌桌</h1><p>选择一个座位和昵称，加入后刷新页面也能回来。</p></section><div class="grid"><section class="panel"><h2>入座</h2><form id="join-form"><div id="name-field"></div><label class="field">座位<select name="seat" required></select></label><button class="btn" type="submit">进入牌桌 →</button></form></section><section class="panel"><h2>牌局设置</h2><div class="statbar"><span class="pill">盲注 <strong>${room.small_blind}/${room.big_blind}</strong></span><span class="pill">人数 <strong>${room.max_players}</strong></span><span class="pill">买入上限 <strong>${money(room.max_buyin_stack)}</strong></span></div><p>同一昵称只能占一个座位。此浏览器会保留你的入座身份。</p><div id="joined"></div></section></div>`;
  const nameField = document.querySelector('#name-field');
  if (room.nickname_policy === 'preset') {
    const label = node('label', '选择昵称', 'field'); const select = node('select'); select.name = 'nickname';
    for (const name of room.allowed_nicknames.filter(n => !room.players.some(p => p.nickname === n))) {
      const option = node('option', name); option.value = name; select.append(option);
    }
    label.append(select); nameField.append(label);
  } else {
    nameField.innerHTML = '<label class="field">你的昵称<input name="nickname" maxlength="32" required autocomplete="nickname" placeholder="输入昵称"></label>';
  }
  const seats = document.querySelector('[name="seat"]');
  for (let i = 0; i < room.max_players; i++) {
    if (taken.has(i)) continue;
    const option = node('option', `座位 ${i + 1}`); option.value = String(i); seats.append(option);
  }
  if (!seats.options.length) notice('牌桌已满。');
  document.querySelector('#join-form').addEventListener('submit', async event => {
    event.preventDefault(); const data = new FormData(event.currentTarget);
    try {
      await api(`/api/rooms/${roomId}/join`, { method: 'POST', body: JSON.stringify({ nickname: data.get('nickname'), seat: Number(data.get('seat')) }) });
      await enterRoom();
    } catch (error) { notice(error.message); }
  });
  const joined = document.querySelector('#joined');
  for (const p of room.players) joined.append(node('div', `座位 ${p.seat + 1} · ${p.nickname}`, 'history-row'));
}

function connectSocket() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  socket = new WebSocket(`${scheme}//${location.host}/ws/rooms/${roomId}`);
  socket.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.type === 'state') renderRoom({ room: message.room, game: message.game });
  };
  socket.onclose = () => { socket = null; setTimeout(() => { if (roomId) connectSocket(); }, 2500); };
}

function renderRoom(payload) {
  roomData = payload.room;
  const room = payload.room, game = payload.game;
  const active = game.street && game.street !== 'complete' && game.street !== 'waiting_next_hand';
  const myId = game.viewer_player_id || game.player_id;
  const me = game.players?.find(p => p.player_id === myId);
  const current = game.players?.[game.to_act_index];
  app.innerHTML = `<section class="hero compact"><div class="eyebrow">PRIVATE TABLE · ${room.room_id.slice(0, 8)}</div><h1>朋友牌局</h1><p>盲注 ${room.small_blind}/${room.big_blind} · 买入后筹码上限 ${money(room.max_buyin_stack)} · ${room.players.length}/${room.max_players} 人入座</p></section><div class="room-layout"><div><section class="table-wrap"><div class="table-top"><span id="street"></span><span id="turn"></span></div><div id="board" class="board"></div><div class="pot">当前底池<b id="pot">0</b></div><div id="seats" class="seats"></div></section><section class="panel" style="margin-top:22px"><h2>你的操作</h2><div id="action-area"></div></section></div><div class="side-stack"><section class="panel"><h2>我的筹码</h2><div id="my-stack" style="font-size:32px;font-weight:800;color:#ecd098"></div><div id="buyin-area"></div></section><section class="panel"><div class="row"><h2>个人记录</h2><button id="history-refresh" class="btn secondary mini right">查看记录</button></div><div id="history-content"><p class="muted small">查看本场买入和每手结果。</p></div></section><section class="panel"><h2>本手行动</h2><div id="action-history" class="history-list"></div></section></div></div>`;
  document.querySelector('#street').textContent = ({ preflop: '翻牌前', flop: '翻牌', turn: '转牌', river: '河牌', complete: '本手已结束' })[game.street] || '等待下一手';
  document.querySelector('#turn').textContent = current ? `轮到 ${current.name}` : active ? '牌局进行中' : '等待开局';
  document.querySelector('#pot').textContent = money(game.pot);
  const board = document.querySelector('#board');
  for (let i = 0; i < 5; i++) board.append(card(game.community?.[i]));
  const seats = document.querySelector('#seats');
  for (let position = 0; position < room.max_players; position++) {
    const person = room.players.find(p => p.seat === position);
    const state = person && game.players?.find(p => p.name === person.nickname);
    const item = node('div', '', 'seat');
    if (state?.player_id === myId) item.classList.add('me');
    if (current && state?.player_id === current.player_id) item.classList.add('turn');
    item.append(node('div', `座位 ${position + 1}${game.button_index !== undefined && state === game.players?.[game.button_index] ? ' · 庄' : ''}`, 'seat-head'));
    item.append(node('div', person?.nickname || '空座位', 'seat-name'));
    if (person) item.append(node('div', `${money(active ? state?.stack ?? person.stack : person.stack)} 筹码${state?.bet_this_street ? ` · 本轮 ${money(state.bet_this_street)}` : ''}${state?.folded ? ' · 已弃牌' : ''}${state?.all_in ? ' · 全下' : ''}`, 'seat-chips'));
    if (state && (active || game.street === 'complete')) {
      const cards = node('div', '', 'seat-cards');
      const codes = state.hole || ['back', 'back'];
      codes.forEach(code => cards.append(card(code))); item.append(cards);
    }
    seats.append(item);
  }
  const roomMe = room.players.find(p => p.nickname === me?.name);
  document.querySelector('#my-stack').textContent = money(active ? me?.stack ?? 0 : roomMe?.stack ?? game.stack ?? 0);
  renderBuyin(room, game, me);
  renderActions(game, me);
  const history = document.querySelector('#action-history');
  if (!game.history?.length) history.append(node('p', '暂无行动。', 'muted small'));
  for (const record of [...(game.history || [])].reverse()) {
    const line = node('div', '', 'history-row');
    line.append(node('span', `${record.player_name} · ${label(record.action)}`));
    line.append(node('span', record.paid ? `投入 ${money(record.paid)}` : '—')); history.append(line);
  }
  document.querySelector('#history-refresh').addEventListener('click', loadHistory);
}

function renderBuyin(room, game, me) {
  const area = document.querySelector('#buyin-area');
  if (room.latest_hand_status === 'active') {
    area.append(node('p', '买入只能在两手牌之间进行。', 'muted small')); return;
  }
  const current = room.players.find(p => p.nickname === me?.name)?.stack ?? game.stack ?? 0;
  const available = Math.max(0, room.max_buyin_stack - current);
  area.append(node('p', `当前最多可补入 ${money(available)} 筹码。`, 'muted small'));
  if (!available) return;
  const form = node('form', '', 'row');
  const labelEl = node('label', '补买筹码', 'field');
  const input = document.createElement('input'); input.type = 'number'; input.min = '1';
  input.max = String(available); input.value = String(available); input.required = true;
  labelEl.append(input); const button = node('button', '确认买入', 'btn'); button.type = 'submit';
  form.append(labelEl, button); area.append(form);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    try {
      await api(`/api/rooms/${roomId}/buyins`, { method: 'POST', body: JSON.stringify({ amount: Number(input.value), request_id: crypto.randomUUID() }) });
      await enterRoom(); notice('买入已记录。', 'ok');
    } catch (error) { notice(error.message); }
  });
}

function renderActions(game, me) {
  const area = document.querySelector('#action-area');
  if (game.status === 'waiting_next_hand') return area.append(node('p', '你将在下一手加入。', 'muted'));
  if (!game.hand_id) return area.append(node('p', '等待房主开始第一手。', 'muted'));
  if (game.street === 'complete') return area.append(node('p', '本手已结束，等待房主开始下一手。', 'muted'));
  const legal = game.legal_actions;
  if (!legal) return area.append(node('p', `等待 ${game.players?.[game.to_act_index]?.name || '其他玩家'} 行动。`, 'muted'));
  const timer = node('p', '', 'muted small'); timer.id = 'timer';
  if (game.deadline_at) timer.dataset.deadline = String(game.deadline_at);
  area.append(timer); updateTimer(game.deadline_at);
  const controls = node('div', '', 'actions'); area.append(controls);
  function button(title, action, amount = 0, style = 'secondary') {
    const control = node('button', title, `btn ${style}`); control.type = 'button';
    control.addEventListener('click', () => sendAction(game, action, amount)); controls.append(control);
  }
  if (legal.can_fold) button('弃牌', 'fold', 0, 'danger');
  if (legal.can_check) button('过牌', 'check');
  if (legal.can_call) button(`跟注 ${money(Math.min(legal.to_call, me?.stack ?? legal.to_call))}`, 'call');
  if (legal.can_bet || legal.can_raise) {
    const isBet = legal.can_bet;
    const minimum = isBet ? legal.min_bet : legal.min_raise_to;
    const maximum = isBet ? legal.max_bet : legal.max_raise_to;
    const field = node('label', isBet ? '下注额' : '加注到', 'field');
    const input = document.createElement('input'); input.type = 'number';
    input.min = String(maximum < minimum ? maximum : minimum); input.max = String(maximum);
    input.value = input.min; input.required = true; field.append(input); controls.append(field);
    const confirm = node('button', isBet ? '下注' : '加注到', 'btn');
    confirm.addEventListener('click', () => sendAction(game, isBet ? 'bet' : 'raise', Number(input.value)));
    controls.append(confirm);
    if (maximum > Number(input.min)) button(`全下 ${money(maximum)}`, isBet ? 'bet' : 'raise', maximum);
  } else if (legal.can_call && me && me.stack <= legal.to_call) {
    // Calling for less than the wager is an all-in call.
    area.append(node('p', '跟注会使用你剩余的全部筹码。', 'muted small'));
  }
}

async function sendAction(game, action, amount) {
  if (busy) return; busy = true;
  try {
    await api(`/api/rooms/${roomId}/hands/${game.hand_id}/actions`, {
      method: 'POST', body: JSON.stringify({ action, amount, expected_version: game.version, request_id: crypto.randomUUID() }),
    });
    await enterRoom();
  } catch (error) { await enterRoom(); notice(error.message); } finally { busy = false; }
}

function updateTimer(deadline) {
  const timer = document.querySelector('#timer'); if (!timer) return;
  if (!deadline) { timer.textContent = '轮到你行动。'; return; }
  timer.textContent = `剩余 ${Math.max(0, Math.ceil(deadline - Date.now() / 1000))} 秒`;
}
setInterval(() => {
  if (!roomData) return;
  const timer = document.querySelector('#timer');
  if (timer?.dataset.deadline) updateTimer(Number(timer.dataset.deadline));
}, 1000);

async function loadHistory() {
  const panel = document.querySelector('#history-content'); if (!panel) return;
  try {
    const data = await api(`/api/rooms/${roomId}/history`);
    panel.replaceChildren();
    panel.append(node('h3', `买入记录 · ${data.buyins.length} 笔`));
    if (!data.buyins.length) panel.append(node('p', '暂无买入。', 'muted small'));
    for (const item of data.buyins) panel.append(node('div', `+${money(item.amount)} · 余额 ${money(item.stack_after)}`, 'history-row'));
    panel.append(node('div', '', 'rule'));
    panel.append(node('h3', `手牌记录 · ${data.hands.length} 手`));
    if (!data.hands.length) panel.append(node('p', '暂无手牌。', 'muted small'));
    for (const hand of data.hands.slice().reverse()) {
      const balance = hand.ending_stack == null ? '进行中' : `结束 ${money(hand.ending_stack)}`;
      const details = document.createElement('details'); details.className = 'history-row';
      const summary = node('summary', `第 ${hand.hand_number} 手 · ${balance} · 派彩 ${money(hand.payout)}`);
      details.append(summary);
      const viewer = hand.view.players?.find(p => p.player_id === hand.view.viewer_player_id);
      details.append(node('p', `我的底牌：${viewer?.hole?.join(' ') || '—'} · 公共牌：${hand.view.community?.join(' ') || '—'}`, 'small'));
      for (const record of hand.view.history || []) {
        details.append(node('div', `${record.player_name} ${label(record.action)} · 实际投入 ${money(record.paid)}`, 'small muted'));
      }
      panel.append(details);
    }
  } catch (error) { notice(error.message); }
}

if (roomId) enterRoom();
else if (location.pathname === '/admin') renderAdmin();
else renderHome();
