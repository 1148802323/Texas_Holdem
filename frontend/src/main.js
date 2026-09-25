const app = document.querySelector('#app');
const roomId = location.pathname.startsWith('/r/') ? location.pathname.split('/')[2] : null;
let socket = null;
let roomData = null;
let currentGame = null;
let busy = false;
let roomJoined = false;
let historyData = null;
let historyQuery = '';
let historyKey = '';
let currentHistoryKey = '';
let historyLoading = false;
let historyViewerId = null;
let historyRevision = 0;
let adminRoomsLoading = false;
let audioManifest = {};
let audioEnabled = false;
let audioContext = null;
const activeAudio = new Set();
let lastActionEvent = null;
let lastTurnEvent = null;

async function loadAudioManifest() {
  try { audioManifest = await api('/api/audio-manifest'); } catch { audioManifest = {}; }
}
function playAudio(category) {
  if (!audioEnabled) return;
  const choices = audioManifest[category] || [];
  if (choices.length) {
    const sound = new Audio(choices[Math.floor(Math.random() * choices.length)]);
    activeAudio.add(sound);
    sound.addEventListener('ended', () => activeAudio.delete(sound), { once: true });
    sound.play().catch(() => activeAudio.delete(sound));
  } else if (category === 'turn') {
    try {
      audioContext ||= new (window.AudioContext || window.webkitAudioContext)();
      const oscillator = audioContext.createOscillator(), gain = audioContext.createGain();
      oscillator.type = 'sine'; oscillator.frequency.value = 880;
      gain.gain.setValueAtTime(0.08, audioContext.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, audioContext.currentTime + 0.25);
      oscillator.connect(gain).connect(audioContext.destination);
      oscillator.start(); oscillator.stop(audioContext.currentTime + 0.25);
    } catch { /* Audio may be unavailable in this browser. */ }
  }
}

function handleSounds(game, myId) {
  const actionKey = `${game.hand_id}:${game.history?.length || 0}`;
  if (lastActionEvent !== null && lastActionEvent !== actionKey && game.history?.length) {
    const record = game.history[game.history.length - 1];
    playAudio(record.all_in ? 'allin' : record.action);
  }
  lastActionEvent = actionKey;
  const actor = game.players?.[game.to_act_index];
  const turnKey = actor ? `${game.hand_id}:${game.street}:${game.history?.length || 0}:${actor.player_id}` : null;
  if (turnKey && actor.player_id === myId && lastTurnEvent !== turnKey && game.deadline_at) playAudio('turn');
  lastTurnEvent = turnKey;
}

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

async function copyInviteLink(url) {
  try {
    await navigator.clipboard.writeText(url);
    notice('邀请链接已复制，可以发给朋友。', 'ok');
  } catch {
    const output = document.querySelector('#created');
    if (output) {
      output.replaceChildren(node('p', '复制未成功，请手动复制下面的链接。', 'muted small'));
      const input = document.createElement('input');
      input.value = url; input.readOnly = true; input.className = 'invite-input';
      input.addEventListener('click', () => input.select());
      output.append(input); input.select();
    } else notice(`请手动复制邀请链接：${url}`);
  }
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
  app.innerHTML = `<section class="hero compact"><div class="eyebrow">ROOM CONTROL</div><h1>牌局管理</h1><p>创建私人牌桌，设置规则并分享邀请链接。</p></section><div class="grid"><section class="panel"><h2>创建新牌局</h2><form id="create-room"><div class="row"><label class="field">小盲注<input name="sb" type="number" min="1" value="10" required></label><label class="field">大盲注<input name="bb" type="number" min="2" value="20" required></label></div><div class="row"><label class="field">牌桌人数<input name="seats" type="number" min="2" max="9" value="6" required></label><label class="field">买入后筹码上限<input name="cap" type="number" min="2" value="2000" required></label></div><div class="row"><label class="field">翻前秒数<input name="preflop" type="number" min="5" max="600" value="60" required></label><label class="field">翻牌秒数<input name="flop" type="number" min="5" max="600" value="60" required></label></div><div class="row"><label class="field">转牌秒数<input name="turn" type="number" min="5" max="600" value="120" required></label><label class="field">河牌秒数<input name="river" type="number" min="5" max="600" value="180" required></label></div><label class="field">全下发牌投票秒数<input name="vote" type="number" min="5" max="600" value="60" required></label><label class="field">指定可用昵称（选填，用逗号分隔）<textarea name="names" rows="2" placeholder="留空允许自由输入昵称"></textarea></label><button class="btn" type="submit">创建并生成邀请链接</button></form><div id="created"></div></section><section class="panel"><div class="row"><h2>我的牌局</h2><button id="logout" class="btn secondary mini right">退出管理</button></div><div id="rooms"></div></section></div>`;
  const recoveryPanel = node('section', '', 'panel recovery-panel');
  recoveryPanel.id = 'admin-recovery'; recoveryPanel.hidden = true;
  app.append(recoveryPanel);
  document.querySelector('#create-room').addEventListener('submit', async event => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const names = String(form.get('names')).split(/[,，\n]/).map(x => x.trim()).filter(Boolean);
    try {
      const result = await api('/api/admin/rooms', { method: 'POST', body: JSON.stringify({
        small_blind: Number(form.get('sb')), big_blind: Number(form.get('bb')),
        max_players: Number(form.get('seats')), max_buyin_stack: Number(form.get('cap')),
        preflop_seconds: Number(form.get('preflop')), flop_seconds: Number(form.get('flop')),
        turn_seconds: Number(form.get('turn')), river_seconds: Number(form.get('river')),
        runout_vote_seconds: Number(form.get('vote')),
        allowed_nicknames: names.length ? names : null,
      }) });
      const output = document.querySelector('#created'); output.replaceChildren();
      output.append(node('div', '邀请链接已生成', 'notice ok'));
      const link = document.createElement('input'); link.value = result.invite_url;
      link.readOnly = true; link.className = 'invite-input mono';
      link.addEventListener('click', () => link.select()); output.append(link);
      const copy = node('button', '复制链接', 'btn secondary mini'); copy.type = 'button';
      copy.addEventListener('click', () => copyInviteLink(result.invite_url));
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
  if (adminRoomsLoading) return;
  adminRoomsLoading = true;
  try {
    const rooms = await api('/api/admin/rooms');
    const expandedEvents = new Set([...container.querySelectorAll('details.admin-events[open]')]
      .map(details => details.dataset.roomId));
    container.replaceChildren();
    if (!rooms.length) return container.append(node('p', '还没有牌局。', 'muted'));
    for (const room of rooms) {
      const overview = await api(`/api/admin/rooms/${room.room_id}`);
      const item = node('div', '', 'room-item'); const info = node('div');
      info.append(node('div', `牌局 ${room.room_id.slice(0, 8)}`, 'room-item-title'));
      info.append(node('p', `盲注 ${room.small_blind}/${room.big_blind} · ${room.max_players} 人 · 买入上限 ${money(room.max_buyin_stack)}`));
      info.append(node('p', overview.players.length ?
        `${overview.players.length} 位玩家 · ${overview.players.filter(p => p.seat !== null).length} 人入座` : '尚无玩家'));
      const playerList = node('div', '', 'admin-player-list');
      for (const player of overview.players) {
        const row = node('div', '', 'admin-player-row');
        const identity = node('div', '', 'admin-player-info');
        identity.append(node('strong', `${player.nickname} · ${player.seat === null ? '旁观' : `座位 ${player.seat + 1}`}`));
        identity.append(node('span', `${money(player.stack)} 筹码 · ${player.ready ? '已准备' : '未准备'} · ${player.connected ? '在线' : '离线'}`, 'muted small'));
        if (player.pending_admin_action) identity.append(node('span',
          `本手结算后${player.pending_admin_action === 'stand' ? '离座' : '移出房间'}`, 'admin-pending'));
        const actions = node('div', '', 'row');
        const recovery = node('button', '生成恢复码', 'btn secondary mini'); recovery.type = 'button';
        recovery.addEventListener('click', async () => {
          if (!confirm(`为 ${player.nickname} 生成新的恢复码？之前未使用的恢复码将立即失效。`)) return;
          try {
            const result = await api(`/api/admin/rooms/${room.room_id}/players/${player.player_id}/recovery-code`, { method: 'POST' });
            showRecoveryCode(result);
            await loadAdminRooms();
          } catch (error) { notice(error.message); }
        });
        actions.append(recovery);
        for (const [action, title] of [['stand', '强制离座'], ['remove', '移出房间']]) {
          if (action === 'stand' && player.seat === null) continue;
          const control = node('button', title, `btn ${action === 'remove' ? 'danger' : 'secondary'} mini`);
          control.type = 'button';
          control.addEventListener('click', async () => {
            const effect = action === 'stand' ? '座位会腾出，筹码仍归该昵称' : '凭证会失效，剩余筹码计入离桌兑出';
            if (!confirm(`${title} ${player.nickname}？${effect}。如果他正在参与手牌，将在该手结算后执行。`)) return;
            try {
              const result = await api(`/api/admin/rooms/${room.room_id}/players/${player.player_id}/${action}`, { method: 'POST' });
              notice(result.queued ? '已安排在本手结算后执行。' : '管理操作已执行。', 'ok');
              await loadAdminRooms();
            } catch (error) { notice(error.message); }
          });
          actions.append(control);
        }
        row.append(identity, actions); playerList.append(row);
      }
      info.append(playerList);
      if (overview.admin_events?.length) {
        const details = node('details', '', 'admin-events');
        details.dataset.roomId = room.room_id;
        details.open = expandedEvents.has(room.room_id);
        details.append(node('summary', '最近管理记录'));
        const names = { recovery_issued: '签发恢复码', recovery_used: '恢复身份',
          stand_queued: '安排离座', stand_applied: '完成离座',
          remove_queued: '安排移出', remove_applied: '完成移出', room_archived: '删除牌桌' };
        for (const event of overview.admin_events) details.append(node('div',
          `${new Date(event.created_at).toLocaleString('zh-CN')} · ${event.nickname || '牌桌'} · ${names[event.action] || event.action}`, 'small muted'));
        info.append(details);
      }
      const controls = node('div', '', 'row');
      const link = node('button', '复制邀请链接', 'btn secondary mini'); link.type = 'button';
      link.addEventListener('click', () => copyInviteLink(`${location.origin}/r/${room.room_id}`));
      const start = node('button', '开始游戏', 'btn mini'); start.type = 'button';
      start.addEventListener('click', async () => {
        try {
          await api(`/api/admin/rooms/${room.room_id}/hands`, { method: 'POST', body: JSON.stringify({ request_id: crypto.randomUUID() }) });
          notice('游戏已启动。', 'ok'); await loadAdminRooms();
        } catch (error) { notice(error.message); }
      });
      const pause = node('button', '暂停', 'btn secondary mini'); pause.type = 'button';
      const resume = node('button', '恢复', 'btn secondary mini'); resume.type = 'button';
      start.disabled = overview.play_state !== 'waiting';
      pause.disabled = overview.play_state !== 'running' || overview.latest_hand_status !== 'active';
      resume.disabled = overview.play_state !== 'paused';
      for (const [button, path] of [[pause, 'pause'], [resume, 'resume']]) button.addEventListener('click', async () => {
        try { await api(`/api/admin/rooms/${room.room_id}/${path}`, { method: 'POST' }); await loadAdminRooms(); }
        catch (error) { notice(error.message); }
      });
      info.append(node('p', `状态：${({ waiting: '等待准备', running: '自动进行中', paused: '已暂停' })[overview.play_state]}`));
      const remove = node('button', '删除牌桌', 'btn danger mini'); remove.type = 'button';
      remove.addEventListener('click', async () => {
        if (!confirm(`删除牌桌 ${room.room_id.slice(0, 8)}？\n邀请链接将停用，牌桌会从管理列表移除；买入和手牌记录仍保留。正在进行的手牌会结算完，但不再开下一手。`)) return;
        try {
          await api(`/api/admin/rooms/${room.room_id}`, { method: 'DELETE' });
          notice('牌桌已删除，历史记录已保留。', 'ok');
          await loadAdminRooms();
        } catch (error) { notice(error.message); }
      });
      controls.append(link, start, pause, resume, remove); item.append(info, controls); container.append(item);
    }
  } catch (error) { notice(error.message); }
  finally { adminRoomsLoading = false; }
}
setInterval(() => { if (location.pathname === '/admin' && document.querySelector('#rooms')) loadAdminRooms(); }, 3000);

function showRecoveryCode(result) {
  const panel = document.querySelector('#admin-recovery'); if (!panel) return;
  panel.hidden = false; panel.replaceChildren();
  panel.append(node('h2', `${result.nickname} 的一次性恢复码`));
  panel.append(node('p', `请私下交给本人。有效期至 ${new Date(result.expires_at * 1000).toLocaleString('zh-CN')}，使用一次或再次生成后即失效。`));
  const input = document.createElement('input'); input.value = result.code;
  input.readOnly = true; input.className = 'invite-input mono';
  input.addEventListener('click', () => input.select()); panel.append(input);
  const copy = node('button', '复制恢复码', 'btn mini'); copy.type = 'button';
  copy.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(result.code); notice('恢复码已复制。', 'ok'); }
    catch { input.select(); notice('复制未成功，请手动复制已选中的恢复码。'); }
  });
  const close = node('button', '关闭显示', 'btn secondary mini'); close.type = 'button';
  close.addEventListener('click', () => { panel.replaceChildren(); panel.hidden = true; });
  const controls = node('div', '', 'row'); controls.append(copy, close); panel.append(controls);
}

async function enterRoom() {
  try {
    if (!Object.keys(audioManifest).length) await loadAudioManifest();
    roomData = await api(`/api/rooms/${roomId}`);
    const state = await api(`/api/rooms/${roomId}/state`);
    roomJoined = true;
    renderRoom(state); connectSocket();
    refreshHistoryIfNeeded();
  } catch (error) {
    if (error.status === 401) {
      const wasJoined = roomJoined;
      roomJoined = false;
      if (socket) { socket.close(); socket = null; }
      renderJoin(roomData);
      if (wasJoined) notice('此设备的玩家凭证已失效；可以使用管理员提供的恢复码找回原身份。');
      return;
    }
    app.innerHTML = ''; notice(error.message);
  }
}

function renderJoin(room) {
  if (!room) return;
  if (room.status !== 'open') {
    app.innerHTML = '<section class="hero compact"><h1>牌桌已关闭</h1><p>这个邀请链接已停用，无法再加入。</p></section>';
    return;
  }
  app.innerHTML = `<section class="hero compact"><div class="eyebrow">YOU ARE INVITED</div><h1>加入私人牌桌</h1><p>先设置昵称进入旁观区，点击空座位上的加号即可入座。</p></section><div class="grid"><section class="panel"><h2>设置昵称</h2><form id="join-form"><div id="name-field"></div><button class="btn" type="submit">进入牌桌 →</button></form></section><section class="panel"><h2>牌局设置</h2><div class="statbar"><span class="pill">盲注 <strong>${room.small_blind}/${room.big_blind}</strong></span><span class="pill">人数 <strong>${room.max_players}</strong></span><span class="pill">买入上限 <strong>${money(room.max_buyin_stack)}</strong></span></div><p>昵称在房间中唯一。旁观时不能买入；入座后可以在两手牌之间买入。</p><div id="joined"></div></section></div>`;
  const recoveryPanel = node('section', '', 'panel');
  recoveryPanel.append(node('h2', '恢复原来的玩家身份'));
  recoveryPanel.append(node('p', '换设备或浏览器数据丢失时，请向房主领取一次性恢复码。昵称本身不能找回筹码和手牌记录。'));
  const recoveryForm = document.createElement('form'); recoveryForm.id = 'recover-form';
  const recoveryLabel = node('label', '一次性恢复码', 'field');
  const recoveryInput = document.createElement('input'); recoveryInput.name = 'code';
  recoveryInput.required = true; recoveryInput.autocomplete = 'off'; recoveryInput.minLength = 8;
  recoveryLabel.append(recoveryInput);
  const recoverySubmit = node('button', '恢复身份', 'btn'); recoverySubmit.type = 'submit';
  recoveryForm.append(recoveryLabel, recoverySubmit); recoveryPanel.append(recoveryForm);
  document.querySelector('.grid').append(recoveryPanel);
  recoveryForm.addEventListener('submit', async event => {
    event.preventDefault();
    try {
      await api(`/api/rooms/${roomId}/recover`, { method: 'POST',
        body: JSON.stringify({ code: recoveryInput.value.trim() }) });
      historyData = null; historyKey = ''; historyViewerId = null;
      await enterRoom(); notice('已恢复原身份、筹码和历史记录。', 'ok');
    } catch (error) { notice(error.message); }
  });
  const nameField = document.querySelector('#name-field');
  if (room.nickname_policy === 'preset') {
    const label = node('label', '选择昵称', 'field'); const select = node('select'); select.name = 'nickname';
    for (const name of room.allowed_nicknames.filter(n => !room.players.some(p => p.nickname === n))) {
      const option = node('option', name); option.value = name; select.append(option);
    }
    if (!select.options.length) {
      document.querySelector('#join-form button[type="submit"]').disabled = true;
      notice('预设昵称已全部被使用。');
    }
    label.append(select); nameField.append(label);
  } else {
    nameField.innerHTML = '<label class="field">你的昵称<input name="nickname" maxlength="32" required autocomplete="nickname" placeholder="输入昵称"></label>';
  }
  document.querySelector('#join-form').addEventListener('submit', async event => {
    event.preventDefault(); const data = new FormData(event.currentTarget);
    try {
      await api(`/api/rooms/${roomId}/join`, { method: 'POST', body: JSON.stringify({ nickname: data.get('nickname') }) });
      historyData = null; historyKey = '';
      await enterRoom();
    } catch (error) { notice(error.message); }
  });
  const joined = document.querySelector('#joined');
  for (const p of room.players) joined.append(node('div', `${p.seat === null ? '旁观' : `座位 ${p.seat + 1}`} · ${p.nickname}`, 'history-row'));
}

function connectSocket() {
  if (!roomJoined) return;
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const connection = new WebSocket(`${scheme}//${location.host}/ws/rooms/${roomId}`);
  socket = connection;
  connection.onmessage = event => {
    if (!roomJoined || socket !== connection) return;
    const message = JSON.parse(event.data);
    if (message.type === 'state') {
      renderRoom({ room: message.room, game: message.game });
      refreshHistoryIfNeeded();
    }
  };
  connection.onclose = () => {
    if (socket !== connection) return;
    socket = null;
    setTimeout(() => { if (roomJoined) enterRoom(); }, 2500);
  };
}

function positionAtTable(element, position, count, dealer = false) {
  const portrait = window.matchMedia('(max-width: 900px)').matches;
  const angle = Math.PI - 2 * Math.PI * position / count;
  const radiusX = dealer ? (portrait ? 20 : 27) : (portrait ? 35 : 39);
  const radiusY = dealer ? (portrait ? 27 : 24) : 37;
  element.style.left = `${50 + radiusX * Math.sin(angle)}%`;
  element.style.top = `${50 - radiusY * Math.cos(angle)}%`;
}

function renderRoom(payload) {
  roomData = payload.room;
  const room = payload.room, game = payload.game;
  const closed = room.status !== 'open';
  currentGame = game;
  currentHistoryKey = `${game.viewer_player_id || game.player_id}:${game.hand_id || ''}:${game.version || 0}`;
  const active = game.street && game.street !== 'complete';
  const myId = game.viewer_player_id || game.player_id;
  handleSounds(game, myId);
  if (historyViewerId !== myId) {
    historyViewerId = myId;
    historyData = null;
    historyKey = '';
    historyQuery = '';
  }
  const roomMe = room.players.find(p => p.player_id === myId);
  const seated = roomMe?.seat !== null && roomMe?.seat !== undefined;
  const me = game.players?.find(p => p.player_id === myId);
  const lockedInHand = Boolean(active && me);
  const current = game.players?.[game.to_act_index];
  const previousTableScroll = document.querySelector('.table-scroll')?.scrollLeft;
  const seatedCount = room.players.filter(p => p.seat !== null).length;
  app.innerHTML = `<section class="hero compact room-heading"><div><div class="eyebrow">PRIVATE TABLE · ${room.room_id.slice(0, 8)}</div><h1>朋友牌局</h1><p>盲注 ${room.small_blind}/${room.big_blind} · 买入上限 ${money(room.max_buyin_stack)} · ${seatedCount}/${room.max_players} 人入座</p></div><div class="room-identity"><span id="identity-name"></span><span id="identity-seat"></span><button id="leave-room" class="btn secondary mini" type="button">退出牌桌</button></div></section><div class="room-layout"><aside class="panel profit-panel"><h2>玩家盈亏</h2><p class="muted small">已结算筹码 − 累计买入；离桌筹码计入兑出。</p><div id="profit-list"></div><div class="rule"></div><h3>旁观者</h3><div id="spectator-list"></div></aside><div class="table-column"><div class="table-scroll"><section class="table-wrap"><div class="table-felt"></div><div class="table-top"><span id="street"></span><span id="turn"></span></div><div class="table-center"><div id="board" class="board"></div><div class="pot">总底池<b id="pot">0</b></div></div><div id="seats" class="seats"></div></section></div><p class="scroll-hint">左右滑动牌桌可查看所有座位</p><section class="panel control-panel"><div class="control-header"><div><h2>我的位置与筹码</h2><p id="seat-help" class="muted small"></p></div><div class="stack-number"><span>当前筹码</span><strong id="my-stack"></strong></div></div><div id="seat-controls" class="row"></div><div id="buyin-area"></div><div class="rule"></div><h2>我的操作</h2><div id="action-area"></div></section><section class="panel action-panel"><h2>本手行动</h2><div id="action-history" class="history-list"></div></section></div><aside class="panel records-panel"><div class="row"><h2>我的手牌记录</h2><button id="history-refresh" class="btn secondary mini right" type="button">刷新</button></div><label class="field">查找手牌<input id="history-search" type="search" placeholder="输入手牌编号或牌面"></label><div id="history-content"><p class="muted small">正在载入你的记录…</p></div></aside></div>`;
  if (closed) document.querySelector('.room-heading').after(node('div',
    active ? '牌桌已关闭：当前手会结算完，之后不再开局。' : '牌桌已关闭：邀请已停用，历史记录仍可查看。', 'notice'));
  const tableScroll = document.querySelector('.table-scroll');
  if (tableScroll.scrollWidth > tableScroll.clientWidth) {
    tableScroll.scrollLeft = previousTableScroll ?? (tableScroll.scrollWidth - tableScroll.clientWidth) / 2;
  }
  document.querySelector('#identity-name').textContent = roomMe?.nickname || '旁观者';
  document.querySelector('#identity-seat').textContent = seated ? `座位 ${roomMe.seat + 1}` : '正在旁观';
  const audioToggle = node('button', audioEnabled ? '声音已开启' : '开启声音', 'btn secondary mini');
  audioToggle.type = 'button';
  audioToggle.addEventListener('click', async () => {
    audioEnabled = !audioEnabled;
    if (audioEnabled) { playAudio('turn'); audioContext?.resume?.(); }
    audioToggle.textContent = audioEnabled ? '声音已开启' : '开启声音';
  });
  document.querySelector('.room-identity').prepend(audioToggle);
  document.querySelector('#leave-room').disabled = lockedInHand;
  document.querySelector('#leave-room').addEventListener('click', async () => {
    if (!confirm('确定退出牌桌并注销当前昵称吗？剩余筹码会计入离桌兑出，之后不能用此身份查看历史。')) return;
    try {
      await api(`/api/rooms/${roomId}/leave`, { method: 'POST' });
      roomJoined = false; historyData = null; historyKey = ''; historyViewerId = null;
      if (socket) { socket.close(); socket = null; }
      await enterRoom();
      notice('已退出牌桌。', 'ok');
    } catch (error) { notice(error.message); }
  });
  document.querySelector('#street').textContent = ({ preflop: '翻牌前', flop: '翻牌', turn: '转牌', river: '河牌', runout_vote: '全下发牌投票', complete: '本手已结束' })[game.street] || '等待下一手';
  document.querySelector('#turn').textContent = room.play_state === 'paused' ? '游戏已暂停' : current ? `轮到 ${current.name}` : game.street === 'runout_vote' ? '等待入池玩家投票' : active ? '牌局进行中' : '等待开局';
  document.querySelector('#pot').textContent = money(game.pot);
  const board = document.querySelector('#board');
  for (let i = 0; i < 5; i++) board.append(card(game.community?.[i]));
  if (game.runout_boards?.length > 1) {
    const second = node('div', '', 'second-board');
    second.append(node('span', '第二次', 'small'));
    for (const code of game.runout_boards[1]) second.append(card(code));
    document.querySelector('.table-center').append(second);
  }
  const publicClock = node('div', '', 'public-clock'); publicClock.id = 'public-clock';
  publicClock.append(node('div', '', 'public-clock-title'), node('div', '', 'public-clock-track'));
  document.querySelector('.table-center').append(publicClock);
  updateTimer();
  const seats = document.querySelector('#seats');
  for (let position = 0; position < room.max_players; position++) {
    const person = room.players.find(p => p.seat === position);
    const state = person && game.players?.find(p => p.player_id === person.player_id);
    const item = node('div', '', 'seat');
    item.dataset.position = String(position);
    positionAtTable(item, position, room.max_players);
    if (person?.player_id === myId) item.classList.add('me');
    if (current && state?.player_id === current.player_id) item.classList.add('turn');
    item.append(node('div', `座位 ${position + 1}`, 'seat-head'));
    if (!person) {
      item.classList.add('empty-seat');
      const plus = node('button', '+', 'seat-plus'); plus.type = 'button';
      plus.title = `坐到座位 ${position + 1}`;
      plus.setAttribute('aria-label', plus.title);
      plus.disabled = lockedInHand || closed;
      plus.addEventListener('click', () => takeSeat(position));
      item.append(plus, node('div', '点击入座', 'seat-name'));
    } else {
      item.append(node('div', person.nickname, 'seat-name'));
      if (person.ready && person.seat !== null) item.append(node('span', '已准备', 'ready-badge'));
      if (room.play_state === 'waiting' && person.start_confirmed) item.append(node('span', '已确认开局', 'confirm-badge'));
      if (game.street === 'runout_vote' && game.runout_votes?.[person.player_id]) {
        item.append(node('span', `已选发${game.runout_votes[person.player_id] === 'twice' ? '两次' : '一次'}`, 'confirm-badge'));
      }
      if (person.pending_admin_action) item.append(node('span',
        `本手后${person.pending_admin_action === 'stand' ? '离座' : '退出'}`, 'pending-seat-badge'));
    }
    if (person) {
      const stack = money(active ? state?.stack ?? person.stack : person.stack);
      const wager = state?.bet_this_street ? money(state.bet_this_street) : '';
      const status = state?.folded ? '弃牌' : state?.all_in ? '全下' : '';
      const chips = node('div', '', 'seat-chips');
      chips.append(node('span', `${stack} 筹码${wager ? ` · 本轮 ${wager}` : ''}${status ? ` · ${status}` : ''}`, 'chips-long'));
      chips.append(node('span', `${stack}${wager ? ` / 投${wager}` : ''}${status ? ` · ${status}` : ''}`, 'chips-compact'));
      item.append(chips);
    }
    if (state && (active || game.street === 'complete')) {
      const cards = node('div', '', 'seat-cards');
      const codes = state.hole || ['back', 'back'];
      codes.forEach(code => cards.append(card(code))); item.append(cards);
    }
    seats.append(item);
    if (state && game.button_index !== undefined && state === game.players?.[game.button_index]) {
      const dealer = node('div', 'D', 'dealer-button');
      dealer.dataset.position = String(position);
      dealer.title = '庄家按钮';
      dealer.setAttribute('aria-label', `座位 ${position + 1} 的庄家按钮`);
      positionAtTable(dealer, position, room.max_players, true);
      seats.append(dealer);
    }
  }
  const profitList = document.querySelector('#profit-list');
  for (const player of room.leaderboard || []) {
    const row = node('div', '', 'profit-row');
    row.append(node('span', player.nickname));
    row.append(node('strong', `${player.profit_loss > 0 ? '+' : ''}${money(player.profit_loss)}`,
      player.profit_loss >= 0 ? 'positive' : 'negative'));
    profitList.append(row);
  }
  if (!room.leaderboard?.length) profitList.append(node('p', '暂无玩家。', 'muted small'));
  const spectators = room.players.filter(p => p.seat === null);
  document.querySelector('#spectator-list').textContent = spectators.length ? spectators.map(p => p.nickname).join(' · ') : '暂无旁观者';
  document.querySelector('#my-stack').textContent = money(active && me ? me.stack : roomMe?.stack ?? game.stack ?? 0);
  document.querySelector('#seat-help').textContent = closed ? '牌桌已关闭，不能再入座或买入。' : seated ? '筹码与你的昵称绑定。手牌进行中不能换座或离桌。' : '正在旁观。点击任意空座位上的加号入座。';
  if (seated && !closed) {
    const stand = node('button', '站起旁观', 'btn secondary mini'); stand.type = 'button';
    stand.disabled = lockedInHand;
    stand.addEventListener('click', async () => {
      try { await api(`/api/rooms/${roomId}/stand`, { method: 'POST' }); await enterRoom(); }
      catch (error) { notice(error.message); }
    });
    document.querySelector('#seat-controls').append(stand);
    const ready = node('button', roomMe.ready ? '取消准备' : '准备进入牌局', roomMe.ready ? 'btn secondary mini' : 'btn mini ready-button');
    ready.type = 'button'; ready.disabled = lockedInHand || (!roomMe.ready && roomMe.stack <= 0);
    ready.addEventListener('click', async () => {
      try { await api(`/api/rooms/${roomId}/ready`, { method: 'POST', body: JSON.stringify({ ready: !roomMe.ready }) }); await enterRoom(); }
      catch (error) { notice(error.message); }
    });
    document.querySelector('#seat-controls').append(ready);
    const allSeated = room.players.filter(p => p.seat !== null);
    if (room.play_state === 'waiting' && allSeated.length >= 2 && allSeated.length < room.max_players &&
        allSeated.every(p => p.ready)) {
      const confirm = node('button', roomMe.start_confirmed ? '已确认，等待其他人' : '确认现在开局', 'btn mini confirm-button');
      confirm.type = 'button'; confirm.disabled = roomMe.start_confirmed;
      confirm.addEventListener('click', async () => {
        try { await api(`/api/rooms/${roomId}/confirm-start`, { method: 'POST' }); await enterRoom(); }
        catch (error) { notice(error.message); }
      });
      document.querySelector('#seat-controls').append(confirm);
    }
  }
  renderBuyin(room, game, roomMe);
  renderActions(game, me);
  const history = document.querySelector('#action-history');
  if (!game.history?.length) history.append(node('p', '暂无行动。', 'muted small'));
  for (const record of [...(game.history || [])].reverse()) {
    const line = node('div', '', 'history-row');
    line.append(node('span', `${record.player_name} · ${label(record.action)}`));
    line.append(node('span', record.paid ? `投入 ${money(record.paid)}` : '—')); history.append(line);
  }
  document.querySelector('#history-refresh').addEventListener('click', loadHistory);
  const search = document.querySelector('#history-search');
  search.value = historyQuery;
  search.addEventListener('input', () => { historyQuery = search.value; renderHistory(); });
  renderHistory();
}

async function takeSeat(position) {
  if (busy) return; busy = true;
  try {
    await api(`/api/rooms/${roomId}/seat`, { method: 'POST', body: JSON.stringify({ seat: position }) });
    await enterRoom();
  } catch (error) { notice(error.message); } finally { busy = false; }
}

function renderBuyin(room, game, roomMe) {
  const area = document.querySelector('#buyin-area');
  if (room.status !== 'open') return;
  if (roomMe?.seat === null || roomMe?.seat === undefined) {
    area.append(node('p', '入座后才能买入筹码。', 'muted small')); return;
  }
  if (room.latest_hand_status === 'active') {
    area.append(node('p', '买入只能在两手牌之间进行。', 'muted small')); return;
  }
  const current = roomMe.stack;
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
      historyRevision++; historyKey = '';
      await enterRoom(); notice('买入已记录。', 'ok');
    } catch (error) { notice(error.message); }
  });
}

function renderActions(game, me) {
  const area = document.querySelector('#action-area');
  if (game.status === 'watching') return area.append(node('p', roomData.status !== 'open' ?
    '牌桌已关闭，可以查看自己曾参与的手牌记录。' : '正在旁观。这手牌结束后，如果你已入座并买入筹码，就能参加下一手。', 'muted'));
  if (!game.hand_id) return area.append(node('p', roomData.status !== 'open' ?
    '牌桌已关闭。' : roomData.play_state === 'running' ? '玩家已确认，即将发牌。' : '等待入座玩家准备并确认开局。', 'muted'));
  if (game.street === 'complete') return area.append(node('p', roomData.status !== 'open' ? '本手已结算，牌桌已关闭。' : roomData.play_state === 'running' ? '本手已结算，即将自动开始下一手。' : '本手已结算，等待开局。', 'muted'));
  if (game.street === 'runout_vote') {
    area.append(node('p', '入池玩家选择发一次或发两次。每个底池由有资格争夺该池的玩家共同决定；超时视为选择一次。', 'muted small'));
    if (game.runout_can_vote && roomData.play_state !== 'paused') {
      const controls = node('div', '', 'actions'); area.append(controls);
      for (const [choice, title] of [['once', '发一次'], ['twice', '发两次']]) {
        const button = node('button', title, 'btn'); button.type = 'button';
        button.addEventListener('click', async () => {
          try {
            await api(`/api/rooms/${roomId}/hands/${game.hand_id}/runout-vote`, {
              method: 'POST', body: JSON.stringify({ choice, expected_version: game.version }),
            }); await enterRoom();
          } catch (error) { await enterRoom(); notice(error.message); }
        }); controls.append(button);
      }
    } else area.append(node('p', '等待其他入池玩家投票。', 'muted'));
    return;
  }
  if (roomData.play_state === 'paused') return area.append(node('p', '管理员已暂停，恢复后继续当前玩家的剩余时间。', 'muted'));
  const legal = game.legal_actions;
  if (!legal) return area.append(node('p', `等待 ${game.players?.[game.to_act_index]?.name || '其他玩家'} 行动。`, 'muted'));
  const extend = node('button', '⏱ 延长本次思考时间', 'btn extend-button');
  extend.id = 'extend-decision'; extend.type = 'button'; extend.hidden = true;
  extend.addEventListener('click', async () => {
    try {
      await api(`/api/rooms/${roomId}/hands/${game.hand_id}/extend`, {
        method: 'POST', body: JSON.stringify({ expected_version: game.version }),
      }); await enterRoom();
    } catch (error) { await enterRoom(); notice(error.message); }
  });
  area.append(extend);
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

function updateTimer() {
  const clock = document.querySelector('#public-clock');
  const game = currentGame;
  if (!clock || !game) return;
  const active = game.street === 'runout_vote' || ['preflop', 'flop', 'turn', 'river'].includes(game.street);
  clock.hidden = !active;
  if (!active) return;
  const remaining = roomData.play_state === 'paused' ? game.paused_remaining || 0 :
    Math.max(0, (game.deadline_at || 0) - Date.now() / 1000);
  const total = game.decision_total_seconds ||
    roomData[game.street === 'runout_vote' ? 'runout_vote_seconds' : `${game.street}_seconds`] || 1;
  const percent = Math.max(0, Math.min(100, Math.round(remaining / total * 100)));
  const title = clock.querySelector('.public-clock-title');
  title.textContent = `${roomData.play_state === 'paused' ? '已暂停 · ' : ''}${game.street === 'runout_vote' ? '发牌投票' : '行动倒计时'} ${Math.ceil(remaining)} 秒 · ${percent}%`;
  const track = clock.querySelector('.public-clock-track');
  track.style.setProperty('--remaining', `${percent}%`);
  track.classList.toggle('urgent', remaining <= 5 && roomData.play_state !== 'paused');
  const extend = document.querySelector('#extend-decision');
  if (extend) extend.hidden = !(roomData.play_state !== 'paused' && game.legal_actions &&
    !game.extension_used && remaining > 0 && remaining <= 5);
}
setInterval(() => {
  if (roomData) updateTimer();
}, 250);

function refreshHistoryIfNeeded() {
  if (roomJoined && currentHistoryKey !== historyKey) loadHistory();
}

async function loadHistory() {
  if (historyLoading) return;
  historyLoading = true;
  const requestedKey = currentHistoryKey;
  const requestedRevision = historyRevision;
  let stale = false;
  try {
    const data = await api(`/api/rooms/${roomId}/history`);
    if (!roomJoined || requestedKey !== currentHistoryKey || requestedRevision !== historyRevision) {
      stale = true; return;
    }
    historyData = data;
    historyKey = requestedKey;
    renderHistory();
  } catch (error) { notice(error.message); }
  finally {
    historyLoading = false;
    if (stale && roomJoined) refreshHistoryIfNeeded();
  }
}

function renderHistory() {
  const panel = document.querySelector('#history-content'); if (!panel) return;
  panel.replaceChildren();
  if (!historyData) return panel.append(node('p', '正在载入你的记录…', 'muted small'));
  const query = historyQuery.trim().toLocaleLowerCase();
  const hands = historyData.hands.slice().reverse().filter(hand => {
    const viewer = hand.view.players?.find(p => p.player_id === hand.view.viewer_player_id);
    const words = [`${hand.hand_number}`, ...(viewer?.hole || []), ...(hand.view.community || [])];
    return !query || words.join(' ').toLocaleLowerCase().includes(query);
  });
  panel.append(node('p', `找到 ${hands.length} / ${historyData.hands.length} 手牌`, 'muted small'));
  if (!hands.length) panel.append(node('p', '没有匹配的手牌。', 'muted small'));
  for (const hand of hands) {
    const balance = hand.ending_stack == null ? '进行中' : `结束 ${money(hand.ending_stack)}`;
    const details = document.createElement('details'); details.className = 'record-item';
    details.append(node('summary', `第 ${hand.hand_number} 手 · ${balance} · 派彩 ${money(hand.payout)}`));
    const viewer = hand.view.players?.find(p => p.player_id === hand.view.viewer_player_id);
    details.append(node('p', `我的底牌：${viewer?.hole?.join(' ') || '—'} · 公共牌：${hand.view.community?.join(' ') || '—'}`, 'small'));
    if (hand.view.runout_boards?.length > 1) {
      details.append(node('p', `第二次公共牌：${hand.view.runout_boards[1].join(' ')}`, 'small'));
    }
    for (const [index, pot] of (hand.view.runout_pots || []).entries()) {
      const awards = (pot.awards || []).map(award =>
        `第${award.board}次 ${hand.view.players?.[award.player_index]?.name || '?'} +${money(award.amount)}`).join('；');
      details.append(node('div', `${index === 0 ? '主池' : `边池${index}`} ${money(pot.amount)} · 发${pot.runs}次 · ${awards}`, 'small muted'));
    }
    for (const record of hand.view.history || []) {
      details.append(node('div', `${record.player_name} ${label(record.action)} · 实际投入 ${money(record.paid)}`, 'small muted'));
    }
    panel.append(details);
  }
  panel.append(node('div', '', 'rule'));
  panel.append(node('h3', `买入记录 · ${historyData.buyins.length} 笔`));
  if (!historyData.buyins.length) panel.append(node('p', '暂无买入。', 'muted small'));
  for (const item of historyData.buyins.slice().reverse()) {
    panel.append(node('div', `+${money(item.amount)} · 余额 ${money(item.stack_after)}`, 'history-row'));
  }
}

if (roomId) enterRoom();
else if (location.pathname === '/admin') renderAdmin();
else renderHome();

window.addEventListener('resize', () => {
  if (!roomData) return;
  document.querySelectorAll('.seat[data-position], .dealer-button[data-position]').forEach(element => {
    positionAtTable(element, Number(element.dataset.position), roomData.max_players,
      element.classList.contains('dealer-button'));
  });
});
