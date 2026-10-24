/* Snake Neo — مار نئون. Four modes, power-ups, combo, particles, glow, sound.
 * Vanilla canvas 2D, smooth interpolated movement, DPR-aware. No dependencies. */
(function () {
  'use strict';

  const GRID = 21; // logical cells per side
  const DIRS = { up: { x: 0, y: -1 }, down: { x: 0, y: 1 }, left: { x: -1, y: 0 }, right: { x: 1, y: 0 } };
  const COMBO_WINDOW = 3500;
  const POWERUP_EVERY = 4;      // every N foods → spawn one power-up
  const POWERUP_TTL = 9000;

  const MODES = {
    neo: {
      icon: '🐍', label: 'نئو', desc: 'نسخه اصلی: دیوار، کمبو و پاورآپ‌ها', speed: 130,
      ramp: false, portals: false, hunter: false, foodValue: 10,
    },
    rush: {
      icon: '⚡', label: 'راش', desc: 'سرعت لحظه‌به‌لحظه بیشتر می‌شه — تا کجا دووم میاری؟', speed: 150,
      minSpeed: 68, ramp: true, portals: false, hunter: false, foodValue: 10,
    },
    portals: {
      icon: '🌀', label: 'پورتال', desc: 'فقط از دهانه‌های پورتال رد شو؛ بقیه دیوارها کشنده‌ان', speed: 125,
      ramp: false, portals: true, hunter: false, foodValue: 15,
    },
    hunter: {
      icon: '👁', label: 'شکارچی', desc: 'توپ‌های دشمن تعقیبت می‌کنن — با یخ فریزشون کن', speed: 140,
      ramp: false, portals: false, hunter: true, foodValue: 15,
    },
  };

  // portal mouths: [x, y, exitAxis] pairs (top<->bottom, left<->right)
  const PORTALS = [
    { a: { x: 10, y: 0 }, b: { x: 10, y: GRID - 1 } },
    { a: { x: 0, y: 10 }, b: { x: GRID - 1, y: 10 } },
  ];
  const POWERUPS = {
    double: { glyph: '×۲', color: '#fbbf24', label: 'امتیاز دوبل', ms: 12000 },
    ghost:  { glyph: '👻', color: '#93c5fd', label: 'شبح', ms: 7000 },
    slow:   { glyph: '🐢', color: '#a3e635', label: 'زمان آهسته', ms: 8000 },
    shield: { glyph: '🛡', color: '#67e8f9', label: 'سپر', ms: 0 },
    freeze: { glyph: '❄', color: '#7dd3fc', label: 'یخ', ms: 6000 },
  };

  // ---------- audio ----------
  let audioCtx = null;
  let soundOn = true;
  try { soundOn = localStorage.getItem('lg_sound') !== '0'; } catch (e) {}

  function beep(freq, dur, type, gain, when) {
    if (!soundOn) return;
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      if (audioCtx.state === 'suspended') audioCtx.resume();
      const t0 = audioCtx.currentTime + (when || 0);
      const osc = audioCtx.createOscillator();
      const g = audioCtx.createGain();
      osc.type = type || 'square';
      osc.frequency.setValueAtTime(freq, t0);
      g.gain.setValueAtTime(gain || 0.06, t0);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + (dur || 0.08));
      osc.connect(g); g.connect(audioCtx.destination);
      osc.start(t0); osc.stop(t0 + (dur || 0.08) + 0.02);
    } catch (e) {}
  }
  const sfx = {
    eat(combo) { beep(520 + Math.min(combo, 5) * 90, 0.07, 'square', 0.05); },
    power() { beep(660, 0.09, 'triangle', 0.06); beep(880, 0.09, 'triangle', 0.06, 0.08); beep(1100, 0.12, 'triangle', 0.06, 0.16); },
    die() { beep(300, 0.25, 'sawtooth', 0.07); beep(150, 0.4, 'sawtooth', 0.07, 0.12); },
    shield() { beep(980, 0.15, 'sine', 0.07); },
    turn() { beep(240, 0.03, 'sine', 0.02); },
  };

  // ---------- state ----------
  let opts = null;        // {mode, best, canvas, onHud, onEnd}
  let canvas, ctx, dpr = 1, view = { size: 0, cell: 0 };
  let snake, prevSnake, dir, pendingDirs, food, powerup, hunters;
  let effects;            // {kind: untilMs} ; shield handled as boolean
  let score, best, combo, comboUntil, foods, dead, paused, running = false;
  let tickMs, acc, lastFrame, tickCount;
  let particles = [], shake = 0, foodPulse = 0;
  let rafId = 0, lastHudAt = 0;

  function now() { return performance.now(); }

  // ---------- geometry ----------
  function resize() {
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    dpr = Math.min(2.5, window.devicePixelRatio || 1);
    canvas.width = Math.round(rect.width * dpr);
    canvas.height = Math.round(rect.height * dpr);
    const size = Math.min(canvas.width, canvas.height);
    view.size = size;
    view.cell = size / GRID;
    ctx = canvas.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
  }

  function cellCenter(cx, cy) {
    return { x: (cx + 0.5) * view.cell, y: (cy + 0.5) * view.cell };
  }

  function isFree(x, y) {
    if (snake.some((s) => s.x === x && s.y === y)) return false;
    if (food && food.x === x && food.y === y) return false;
    if (powerup && powerup.x === x && powerup.y === y) return false;
    if (hunters.some((h) => h.x === x && h.y === y)) return false;
    return true;
  }

  function randFree() {
    for (let i = 0; i < 400; i++) {
      const x = Math.floor(Math.random() * GRID);
      const y = Math.floor(Math.random() * GRID);
      if (isFree(x, y)) return { x, y };
    }
    return null;
  }

  // ---------- game flow ----------
  function reset() {
    const mid = Math.floor(GRID / 2);
    snake = [{ x: mid, y: mid }, { x: mid - 1, y: mid }, { x: mid - 2, y: mid }];
    prevSnake = snake.map((s) => ({ ...s }));
    dir = DIRS.right;
    pendingDirs = [];
    hunters = [];
    effects = {};
    powerup = null;
    score = 0; combo = 1; comboUntil = 0; foods = 0; tickCount = 0;
    dead = false; paused = false; acc = 0; lastFrame = now(); shake = 0;
    particles = [];
    const M = MODES[opts.mode];
    tickMs = M.speed;
    best = Math.max(best || 0, opts.best || 0);
    food = randFree();
    if (M.portals) food = randFree();
    emitHud();
  }

  function start(options) {
    opts = options || {};
    canvas = opts.canvas;
    best = opts.best || 0;
    resize();
    reset();
    running = true;
    window.removeEventListener('resize', resize);
    window.addEventListener('resize', resize);
    bindInput();
    cancelAnimationFrame(rafId);
    lastFrame = now();
    rafId = requestAnimationFrame(frame);
  }

  function stopLoop() {
    running = false;
    cancelAnimationFrame(rafId);
    window.removeEventListener('resize', resize);
  }

  function quit() { stopLoop(); dead = true; }

  function pause() { if (running && !dead) { paused = true; lastFrame = now(); } }
  function resume() { if (paused) { paused = false; lastFrame = now(); } }
  function isPlaying() { return running && !dead && !paused; }

  function toggleSound() {
    soundOn = !soundOn;
    try { localStorage.setItem('lg_sound', soundOn ? '1' : '0'); } catch (e) {}
    return soundOn;
  }

  // ---------- input ----------
  function turn(name) {
    const d = DIRS[name];
    if (!d || !running || dead || paused) return;
    const last = pendingDirs.length ? pendingDirs[pendingDirs.length - 1] : dir;
    if (last.x === -d.x && last.y === -d.y) return; // no 180°
    if (last.x === d.x && last.y === d.y) return;
    if (pendingDirs.length < 2) { pendingDirs.push(d); sfx.turn(); }
  }

  let touchStart = null;
  function bindInput() {
    canvas.ontouchstart = (e) => {
      const t = e.touches[0];
      touchStart = { x: t.clientX, y: t.clientY };
      e.preventDefault();
    };
    canvas.ontouchmove = (e) => {
      if (!touchStart) return;
      const t = e.touches[0];
      const dx = t.clientX - touchStart.x, dy = t.clientY - touchStart.y;
      if (Math.abs(dx) < 24 && Math.abs(dy) < 24) return;
      turn(Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? 'right' : 'left') : (dy > 0 ? 'down' : 'up'));
      touchStart = { x: t.clientX, y: t.clientY };
      e.preventDefault();
    };
    canvas.ontouchend = () => { touchStart = null; };
    document.onkeydown = (e) => {
      const map = { ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right' };
      if (map[e.key]) { turn(map[e.key]); e.preventDefault(); }
    };
  }

  // ---------- effects ----------
  function active(kind) { return effects[kind] && effects[kind] > now(); }
  function hasShield() { return !!effects.shield; }
  function consumeShield() { effects.shield = 0; popParticles(cellCenter(snake[0].x, snake[0].y), '#67e8f9', 26); shake = 8; sfx.shield(); }

  function applyPowerup(kind) {
    sfx.power();
    if (kind === 'shield') { effects.shield = 1; }
    else effects[kind] = now() + POWERUPS[kind].ms;
    popParticles(cellCenter(powerup.x, powerup.y), POWERUPS[kind].color, 22);
    powerup = null;
    emitHud();
  }

  function currentTickMs() {
    let t = tickMs;
    if (active('slow')) t *= 1.55;
    return t;
  }

  // ---------- tick ----------
  function tick() {
    tickCount += 1;
    if (pendingDirs.length) dir = pendingDirs.shift();
    prevSnake = snake.map((s) => ({ ...s }));

    let nx = snake[0].x + dir.x;
    let ny = snake[0].y + dir.y;
    const M = MODES[opts.mode];

    const offGrid = nx < 0 || ny < 0 || nx >= GRID || ny >= GRID;
    if (offGrid) {
      let handled = false;
      if (M.portals) {
        for (const p of PORTALS) {
          if (p.a.x === snake[0].x && p.a.y === snake[0].y) { nx = p.b.x + dir.x; ny = p.b.y + dir.y; handled = true; break; }
          if (p.b.x === snake[0].x && p.b.y === snake[0].y) { nx = p.a.x + dir.x; ny = p.a.y + dir.y; handled = true; break; }
        }
      }
      if (!handled) return die('wall');
    }

    const selfHit = !active('ghost') && snake.some((s, i) => i > 0 && s.x === nx && s.y === ny);
    if (selfHit) {
      if (hasShield()) { consumeShield(); effects.ghost = now() + 1600; }
      else return die('self');
    }

    snake.unshift({ x: nx, y: ny });

    const ateFood = food && nx === food.x && ny === food.y;
    if (ateFood) {
      combo = now() - comboUntil < COMBO_WINDOW ? Math.min(5, combo + 1) : 1;
      comboUntil = now();
      const gain = M.foodValue * combo * (active('double') ? 2 : 1);
      score += gain;
      foods += 1;
      popParticles(cellCenter(food.x, food.y), '#fb7185', 16);
      sfx.eat(combo);
      food = randFree();
      if (M.ramp) tickMs = Math.max(M.minSpeed, M.speed - foods * 3.2);
      if (foods % POWERUP_EVERY === 0 && !powerup) {
        const pool = M.hunter ? Object.keys(POWERUPS) : Object.keys(POWERUPS).filter((k) => k !== 'freeze');
        const kind = pool[Math.floor(Math.random() * pool.length)];
        const spot = randFree();
        if (spot) powerup = { x: spot.x, y: spot.y, kind, until: now() + POWERUP_TTL };
      }
      if (M.hunter) {
        if (foods >= 3 && hunters.length < 1) spawnHunter();
        if (foods >= 12 && hunters.length < 2) spawnHunter();
      }
    } else {
      snake.pop();
      if (now() - comboUntil >= COMBO_WINDOW && combo !== 1) { combo = 1; emitHud(); }
    }

    if (powerup && nx === powerup.x && ny === powerup.y) applyPowerup(powerup.kind);
    if (powerup && powerup.until < now()) powerup = null;
    Object.keys(effects).forEach((k) => { if (k !== 'shield' && effects[k] < now()) delete effects[k]; });

    if (M.hunter) moveHunters();

    emitHud();
  }

  function spawnHunter() {
    const head = snake[0];
    let spot = null;
    for (let i = 0; i < 300; i++) {
      const x = Math.floor(Math.random() * GRID);
      const y = Math.floor(Math.random() * GRID);
      const dist = Math.abs(x - head.x) + Math.abs(y - head.y);
      if (isFree(x, y) && dist > GRID / 2) { spot = { x, y }; break; }
    }
    if (spot) hunters.push({ x: spot.x, y: spot.y, px: spot.x, py: spot.y });
  }

  function moveHunters() {
    if (active('freeze')) return;
    if (tickCount % 2 !== 0) { hunters.forEach((h) => { h.px = h.x; h.py = h.y; }); return; }
    const head = snake[0];
    hunters.forEach((h) => {
      h.px = h.x; h.py = h.y;
      const dx = head.x - h.x, dy = head.y - h.y;
      if (Math.abs(dx) >= Math.abs(dy)) h.x += dx > 0 ? 1 : (dx < 0 ? -1 : 0);
      else h.y += dy > 0 ? 1 : (dy < 0 ? -1 : 0);
      if (h.x === head.x && h.y === head.y) {
        if (hasShield()) { consumeShield(); respawnHunter(h); }
        else die('hunter');
      }
    });
  }

  function respawnHunter(h) {
    const spot = randFree();
    if (spot) { h.x = spot.x; h.y = spot.y; h.px = spot.x; h.py = spot.y; }
  }

  function die(cause) {
    dead = true;
    running = false;
    shake = 14;
    const head = cellCenter(snake[0].x, snake[0].y);
    popParticles(head, cause === 'hunter' ? '#fb7185' : '#f8fafc', 46);
    sfx.die();
    emitHud(true);
    cancelAnimationFrame(rafId);
    setTimeout(() => {
      if (opts.onEnd) opts.onEnd({ mode: opts.mode, score, cause });
    }, 550);
  }

  // ---------- particles ----------
  function popParticles(center, color, n) {
    for (let i = 0; i < n; i++) {
      const a = Math.random() * Math.PI * 2;
      const sp = 0.6 + Math.random() * 3.2;
      particles.push({
        x: center.x, y: center.y,
        vx: Math.cos(a) * sp, vy: Math.sin(a) * sp,
        life: 1, decay: 0.012 + Math.random() * 0.02,
        size: 1.5 + Math.random() * 3, color,
      });
    }
    if (particles.length > 400) particles.splice(0, particles.length - 400);
  }

  // ---------- render ----------
  function frame(t) {
    if (!running && !dead) return;
    const dt = Math.min(64, t - lastFrame);
    lastFrame = t;
    if (!paused && !dead) {
      acc += dt;
      const step = currentTickMs();
      let guard = 0;
      while (acc >= step && guard++ < 4 && !dead) { acc -= step; tick(); }
    }
    render(t, dt);
    if (running || particles.length) rafId = requestAnimationFrame(frame);
  }

  function lerp(a, b, t) { return a + (b - a) * t; }

  function render(t, dt) {
    if (!ctx) return;
    const w = canvas.width, h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    ctx.save();
    if (shake > 0.2) {
      ctx.translate((Math.random() - 0.5) * shake * dpr, (Math.random() - 0.5) * shake * dpr);
      shake *= 0.86;
    } else shake = 0;

    // arena backdrop
    const M = MODES[opts.mode];
    const grad = ctx.createRadialGradient(w / 2, h / 2, view.size * 0.1, w / 2, h / 2, view.size * 0.75);
    grad.addColorStop(0, '#0d1326');
    grad.addColorStop(1, '#070a12');
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, w, h);

    // grid
    ctx.strokeStyle = 'rgba(148,163,184,0.06)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let i = 1; i < GRID; i++) {
      ctx.moveTo(i * view.cell, 0); ctx.lineTo(i * view.cell, view.size);
      ctx.moveTo(0, i * view.cell); ctx.lineTo(view.size, i * view.cell);
    }
    ctx.stroke();

    // walls (deadly edges) — pulse red slightly in non-portal modes
    if (!M.portals) {
      ctx.strokeStyle = 'rgba(251,113,133,' + (0.22 + 0.08 * Math.sin(t / 500)) + ')';
      ctx.lineWidth = 3 * dpr;
      ctx.strokeRect(1.5 * dpr, 1.5 * dpr, view.size - 3 * dpr, view.size - 3 * dpr);
    }

    // portals
    if (M.portals) {
      PORTALS.forEach((p) => {
        [p.a, p.b].forEach((c) => {
          const ctr = cellCenter(c.x, c.y);
          const r = view.cell * (0.62 + 0.08 * Math.sin(t / 240));
          ctx.beginPath();
          ctx.arc(ctr.x, ctr.y, r, 0, Math.PI * 2);
          ctx.strokeStyle = 'rgba(167,139,250,0.85)';
          ctx.lineWidth = 3 * dpr;
          ctx.shadowColor = '#a78bfa'; ctx.shadowBlur = 18 * dpr;
          ctx.stroke();
          ctx.shadowBlur = 0;
        });
      });
    }

    // food
    if (food) {
      const ctr = cellCenter(food.x, food.y);
      foodPulse = 0.5 + 0.5 * Math.sin(t / 220);
      const r = view.cell * (0.3 + 0.06 * foodPulse);
      ctx.beginPath();
      ctx.arc(ctr.x, ctr.y, r, 0, Math.PI * 2);
      ctx.fillStyle = '#fb7185';
      ctx.shadowColor = '#fb7185'; ctx.shadowBlur = (14 + 10 * foodPulse) * dpr;
      ctx.fill();
      ctx.shadowBlur = 0;
    }

    // powerup
    if (powerup) {
      const meta = POWERUPS[powerup.kind];
      const ctr = cellCenter(powerup.x, powerup.y);
      const blinking = powerup.until - t < 2200 && Math.floor(t / 160) % 2 === 0;
      if (!blinking) {
        ctx.save();
        ctx.translate(ctr.x, ctr.y);
        ctx.rotate(t / 900);
        const r = view.cell * 0.42;
        ctx.beginPath();
        ctx.moveTo(0, -r); ctx.lineTo(r, 0); ctx.lineTo(0, r); ctx.lineTo(-r, 0);
        ctx.closePath();
        ctx.fillStyle = 'rgba(15,20,35,0.9)';
        ctx.fill();
        ctx.strokeStyle = meta.color; ctx.lineWidth = 2 * dpr;
        ctx.shadowColor = meta.color; ctx.shadowBlur = 14 * dpr;
        ctx.stroke();
        ctx.restore();
        ctx.shadowBlur = 0;
        ctx.fillStyle = meta.color;
        ctx.font = 'bold ' + Math.round(view.cell * 0.42) + 'px ' + 'Vazirmatn, sans-serif';
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillText(meta.glyph, ctr.x, ctr.y + view.cell * 0.04);
      }
    }

    // hunters
    const interp = paused || dead ? 1 : Math.min(1, acc / currentTickMs());
    if (M.hunter) {
      hunters.forEach((hun) => {
        const frozen = active('freeze');
        const cx = lerp(hun.px, hun.x, interp), cy = lerp(hun.py, hun.y, interp);
        const ctr = cellCenter(cx, cy);
        ctx.beginPath();
        ctx.arc(ctr.x, ctr.y, view.cell * 0.36, 0, Math.PI * 2);
        ctx.fillStyle = frozen ? 'rgba(125,211,252,0.9)' : 'rgba(251,113,133,0.95)';
        ctx.shadowColor = frozen ? '#7dd3fc' : '#fb7185';
        ctx.shadowBlur = 18 * dpr;
        ctx.fill();
        ctx.shadowBlur = 0;
        ctx.fillStyle = '#0b1020';
        ctx.font = 'bold ' + Math.round(view.cell * 0.36) + 'px sans-serif';
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillText(frozen ? '❄' : '👁', ctr.x, ctr.y + view.cell * 0.02);
      });
    }

    // snake (interpolated, glowing, gradient head→tail)
    const ghost = active('ghost');
    const pts = snake.map((s, i) => {
      const p = prevSnake[i] || s;
      return cellCenter(lerp(p.x, s.x, interp), lerp(p.y, s.y, interp));
    });
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    const bodyGrad = ctx.createLinearGradient(pts[0].x, pts[0].y, pts[pts.length - 1].x, pts[pts.length - 1].y);
    bodyGrad.addColorStop(0, '#22d3ee');
    bodyGrad.addColorStop(1, '#a78bfa');
    ctx.strokeStyle = bodyGrad;
    ctx.globalAlpha = ghost ? 0.38 : 1;
    ctx.shadowColor = '#22d3ee';
    ctx.shadowBlur = (ghost ? 6 : 20) * dpr;
    ctx.lineWidth = view.cell * 0.72;
    ctx.beginPath();
    ctx.moveTo(pts[0].x, pts[0].y);
    for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y);
    ctx.stroke();
    ctx.shadowBlur = 0;

    // head
    const headCtr = pts[0];
    ctx.beginPath();
    ctx.arc(headCtr.x, headCtr.y, view.cell * 0.42, 0, Math.PI * 2);
    ctx.fillStyle = ghost ? 'rgba(147,197,253,0.9)' : '#e0f7ff';
    ctx.shadowColor = '#22d3ee'; ctx.shadowBlur = 16 * dpr;
    ctx.fill();
    ctx.shadowBlur = 0;
    // eyes
    ctx.fillStyle = '#04121a';
    const ex = dir.y !== 0 ? view.cell * 0.14 : 0;
    const ey = dir.x !== 0 ? view.cell * 0.14 : 0;
    ctx.beginPath();
    ctx.arc(headCtr.x + dir.x * view.cell * 0.12 + ex, headCtr.y + dir.y * view.cell * 0.12 + ey, view.cell * 0.06, 0, Math.PI * 2);
    ctx.arc(headCtr.x + dir.x * view.cell * 0.12 - ex, headCtr.y + dir.y * view.cell * 0.12 - ey, view.cell * 0.06, 0, Math.PI * 2);
    ctx.fill();
    if (hasShield()) {
      ctx.beginPath();
      ctx.arc(headCtr.x, headCtr.y, view.cell * 0.62, 0, Math.PI * 2);
      ctx.strokeStyle = 'rgba(103,232,249,0.8)';
      ctx.lineWidth = 2.5 * dpr;
      ctx.setLineDash([6 * dpr, 5 * dpr]);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    ctx.globalAlpha = 1;

    // particles
    for (let i = particles.length - 1; i >= 0; i--) {
      const p = particles[i];
      p.x += p.vx * (dt / 16); p.y += p.vy * (dt / 16);
      p.vy += 0.05 * (dt / 16);
      p.life -= p.decay * (dt / 16);
      if (p.life <= 0) { particles.splice(i, 1); continue; }
      ctx.globalAlpha = Math.max(0, p.life);
      ctx.fillStyle = p.color;
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.size * dpr * p.life, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
    ctx.restore();
  }

  // ---------- HUD ----------
  function emitHud(finalTick) {
    if (!opts.onHud) return;
    if (!finalTick && performance.now() - lastHudAt < 90) return;
    lastHudAt = performance.now();
    const fx = [];
    Object.keys(POWERUPS).forEach((k) => {
      if (k === 'shield' && hasShield()) fx.push({ icon: '🛡', label: 'سپر', left: '∞' });
      else if (k !== 'shield' && active(k)) {
        fx.push({ icon: POWERUPS[k].glyph, label: POWERUPS[k].label, left: Math.ceil((effects[k] - now()) / 1000) });
      }
    });
    best = Math.max(best, score);
    opts.onHud({ score, best, combo, effects: fx });
  }

  // ---------- export ----------
  window.SnakeGame = { MODES, start, pause, resume, quit, isPlaying, toggleSound };
})();
