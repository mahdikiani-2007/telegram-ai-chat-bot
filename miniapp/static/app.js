/* L Games — app shell: Telegram glue, screens, API client, leaderboard.
 * Game logic lives in /games/snake.js (window.SnakeGame). */
(function () {
  'use strict';

  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  if (tg) {
    try { tg.ready(); tg.expand(); tg.setHeaderColor('#070a12'); tg.setBackgroundColor('#070a12'); } catch (e) {}
    try { if (tg.disableVerticalSwipes) tg.disableVerticalSwipes(); } catch (e) {}
  }

  const user = (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) || null;
  const initData = (tg && tg.initData) || '';

  // Trusted (signed) launch scope: t.me/...?startapp=g<chat_id> → group board.
  const startParam = (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) || '';
  const sm = /^g(-?\d+)$/.exec(startParam || '');
  const scope = sm ? 'g' + sm[1] : 'global';

  const state = {
    screen: 'lobby',
    lbGame: 'snake_neo',
    lastRun: null, // {game, score}
  };

  const $ = (id) => document.getElementById(id);
  const fmt = (n) => (typeof n === 'number' ? n.toLocaleString('fa-IR') : '—');

  // ---------- API ----------
  async function api(path, extra) {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({ initData }, extra || {})),
    });
    if (!res.ok) throw new Error('http ' + res.status);
    return res.json();
  }

  // ---------- haptics / sound ----------
  const haptic = {
    tap() { try { tg && tg.HapticFeedback && tg.HapticFeedback.impactOccurred('light'); } catch (e) {} },
    win() { try { tg && tg.HapticFeedback && tg.HapticFeedback.notificationOccurred('success'); } catch (e) {} },
    fail() { try { tg && tg.HapticFeedback && tg.HapticFeedback.notificationOccurred('error'); } catch (e) {} },
  };

  // ---------- screens ----------
  function show(name) {
    state.screen = name;
    document.querySelectorAll('.screen').forEach((el) => el.classList.remove('active'));
    document.querySelectorAll('.game-wrap, .overlay').forEach((el) => el.classList.remove('active'));
    if (name === 'game') {
      // the play surface is a fixed .game-wrap, not a .screen section
      const wrap = $('game-wrap');
      if (wrap) wrap.classList.add('active');
    } else {
      const el = $('screen-' + name);
      if (el) el.classList.add('active');
    }
    try {
      if (tg && tg.BackButton) {
        if (name === 'lobby') tg.BackButton.hide();
        else tg.BackButton.show();
      }
    } catch (e) {}
    if (name === 'lobby') renderLobbyBests();
    if (name === 'board') renderBoard();
  }

  if (tg && tg.BackButton) {
    try {
      tg.BackButton.onClick(() => {
        if (state.screen === 'game') { window.SnakeGame.quit(); show('mode'); }
        else if (state.screen === 'mode') show('lobby');
        else if (state.screen === 'board') show('lobby');
        else show('lobby');
      });
    } catch (e) {}
  }

  // ---------- lobby ----------
  function renderLobby() {
    const name = user ? (user.first_name || 'کارآگاه') : 'مهمان';
    $('lb-chip-user').textContent = name;
    const scopeChip = $('lb-chip-scope');
    scopeChip.style.display = scope === 'global' ? 'none' : 'inline-flex';
  }

  async function renderLobbyBests() {
    if (!initData) return;
    try {
      const lb = await api('/api/leaderboard', { game: 'snake_neo' });
      const mine = lb.you && lb.you.best ? lb.you.best : 0;
      document.querySelectorAll('[data-best="snake_neo"]').forEach((el) => {
        el.innerHTML = '🏆 رکورد شما: <b>' + fmt(mine) + '</b>';
      });
    } catch (e) { /* offline: leave cards as-is */ }
  }

  // ---------- leaderboard ----------
  const MODE_LABELS = {
    snake_neo: 'نئو', snake_rush: 'راش', snake_portals: 'پورتال', snake_hunter: 'شکارچی',
  };

  async function renderBoard() {
    const game = state.lbGame;
    document.querySelectorAll('.mchip').forEach((c) => c.classList.toggle('on', c.dataset.game === game));
    const box = $('lb-rows');
    box.innerHTML = '<div class="lb-empty">در حال گرفتن پرونده‌ها…</div>';
    try {
      const lb = await api('/api/leaderboard', { game });
      $('lb-title').textContent = lb.title || 'رتبه‌بندی';
      if (!lb.rows || !lb.rows.length) {
        box.innerHTML = '<div class="lb-empty">هنوز کسی امتیاز ثبت نکرده!<br>اولین قهرمان تو باش.</div>';
        return;
      }
      const medals = ['🥇', '🥈', '🥉'];
      box.innerHTML = lb.rows.map((row, i) => {
        const me = user && row.uid === user.id;
        const medal = medals[i] || '<span style="color:var(--dim)">' + (i + 1).toLocaleString('fa-IR') + '</span>';
        return '<div class="lb-row' + (me ? ' me' : '') + '">' +
          '<div class="lb-rank">' + medal + '</div>' +
          '<div class="lb-name">' + escapeHtml(row.name || 'کاربر') + (me ? ' (تو)' : '') + '</div>' +
          '<div class="lb-score">' + fmt(row.best) + '</div></div>';
      }).join('');
    } catch (e) {
      box.innerHTML = '<div class="lb-empty">اتصال برقرار نشد. کمی بعد دوباره امتحان کن.</div>';
    }
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  // ---------- snake flow ----------
  const modeBests = {};

  function openModeSelect() {
    haptic.tap();
    const wrap = $('mode-list');
    wrap.innerHTML = '';
    Object.keys(window.SnakeGame.MODES).forEach((key) => {
      const mode = window.SnakeGame.MODES[key];
      const card = document.createElement('div');
      card.className = 'mode-card';
      card.innerHTML =
        '<div class="mode-ico">' + mode.icon + '</div>' +
        '<div><div class="mode-name">' + mode.label + '</div>' +
        '<div class="mode-desc">' + mode.desc + '</div></div>' +
        '<div class="mode-best"><b id="mb-' + key + '">—</b><span>رکورد</span></div>';
      card.onclick = () => startGame(key);
      wrap.appendChild(card);
      api('/api/leaderboard', { game: 'snake_' + key })
        .then((lb) => {
          modeBests[key] = (lb.you && lb.you.best) || 0;
          const el = $('mb-' + key);
          if (el) el.textContent = fmt(modeBests[key]);
        })
        .catch(() => { modeBests[key] = 0; });
    });
    show('mode');
  }

  function startGame(modeKey) {
    haptic.tap();
    state.lastRun = { mode: modeKey };
    show('game');
    $('fx-gameover').classList.remove('active');
    $('fx-pause').classList.remove('active');
    window.SnakeGame.start({
      mode: modeKey,
      best: modeBests[modeKey] || 0,
      canvas: $('gameCanvas'),
      onHud(hud) {
        $('hud-score').textContent = fmt(hud.score);
        $('hud-best').textContent = 'رکورد: ' + fmt(hud.best);
        const c = $('hud-combo');
        if (hud.combo > 1) { c.textContent = 'کمبو ×' + hud.combo.toLocaleString('fa-IR'); c.classList.add('on'); }
        else c.classList.remove('on');
        $('fx-list').innerHTML = (hud.effects || []).map((ef) =>
          '<div class="pf">' + ef.icon + ' ' + ef.label + ' <i>' + ef.left + 's</i></div>').join('');
      },
      onEnd(payload) { gameOver(payload); },
    });
  }

  async function gameOver(payload) {
    // payload: {mode, score}
    haptic.fail();
    let best = payload.score, isNew = false, rank = null;
    let submitted = false;
    if (initData) {
      try {
        const res = await api('/api/submit', { game: 'snake_' + payload.mode, score: payload.score });
        best = res.best; isNew = res.new_best; rank = res.rank; submitted = true;
        if (isNew) haptic.win();
      } catch (e) { /* keep local values */ }
    }
    $('go-score').textContent = fmt(payload.score);
    $('go-newbest').style.display = isNew ? 'inline-flex' : 'none';
    $('go-best').textContent = fmt(best);
    $('go-rank').textContent = rank ? '#' + rank.toLocaleString('fa-IR') : '—';
    $('go-saved').textContent = submitted ? 'امتیازت ثبت شد ✅' : 'امتیاز محلی ذخیره شد (اتصال نبود)';
    $('fx-gameover').classList.add('active');
  }

  // ---------- global wiring ----------
  window.LGames = { show, state, scope, user };

  document.addEventListener('DOMContentLoaded', () => {
    renderLobby();
    show('lobby');
    const chessBtn = $('btn-open-chess');
    if (chessBtn) {
      chessBtn.onclick = () => {
        haptic.tap();
        window.location.href = '/chess/' + (window.location.search || '');
      };
    }
    const hokmBtn = $('btn-open-hokm');
    if (hokmBtn) {
      hokmBtn.onclick = () => {
        haptic.tap();
        window.location.href = '/hokm/' + (window.location.search || '');
      };
    }
    $('btn-board').onclick = () => { haptic.tap(); show('board'); };
    $('btn-open-snake').onclick = openModeSelect;
    $('btn-back-lobby').onclick = () => { haptic.tap(); show('lobby'); };
    $('btn-back-mode').onclick = () => { haptic.tap(); show('lobby'); };
    document.querySelectorAll('.mchip').forEach((c) => {
      c.onclick = () => { state.lbGame = c.dataset.game; haptic.tap(); renderBoard(); };
    });
    $('btn-replay').onclick = () => {
      $('fx-gameover').classList.remove('active');
      startGame(state.lastRun && state.lastRun.mode ? state.lastRun.mode : 'neo');
    };
    $('btn-menu').onclick = () => {
      $('fx-gameover').classList.remove('active');
      show('mode');
    };
    $('btn-resume').onclick = () => {
      $('fx-pause').classList.remove('active');
      window.SnakeGame.resume();
    };
    $('btn-quit').onclick = () => {
      $('fx-pause').classList.remove('active');
      window.SnakeGame.quit();
      show('mode');
    };
    $('btn-sound').onclick = () => {
      const on = window.SnakeGame.toggleSound();
      $('btn-sound').textContent = on ? '🔊' : '🔇';
    };
    $('btn-pause').onclick = () => {
      if (!window.SnakeGame.isPlaying()) return;
      $('fx-pause').classList.add('active');
      window.SnakeGame.pause();
    };
    document.addEventListener('visibilitychange', () => {
      if (document.hidden && state.screen === 'game' && window.SnakeGame.isPlaying()) {
        $('fx-pause').classList.add('active');
        window.SnakeGame.pause();
      }
    });
  });
})();
