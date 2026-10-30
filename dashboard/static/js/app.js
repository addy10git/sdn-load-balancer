/* Dashboard glue: Socket.IO updates -> panels; control buttons -> /control/<action>. */
(() => {
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const timeStr = ts => new Date(ts * 1000).toLocaleTimeString([], {hour12: false});
  const css = getComputedStyle(document.documentElement);
  const STATUS = {good: css.getPropertyValue('--good').trim(), warn: css.getPropertyValue('--warning').trim(),
                  crit: css.getPropertyValue('--critical').trim()};
  let strategyTouched = false;
  let lastUpdate = null;

  Topology.init($('topology'), $('tooltip'));
  Charts.init();

  const socket = io();
  socket.on('update', render);
  socket.on('disconnect', () => setConn(false, false, 'dashboard offline'));

  // ------------------------------------------------------------------ rendering
  function setConn(controllerOk, mininetOk, err) {
    const pc = $('pill-controller'), pm = $('pill-mininet');
    pc.className = 'pill ' + (controllerOk ? 'pill-ok' : 'pill-bad');
    pc.lastChild.textContent = controllerOk ? 'Controller: connected' : 'Controller: disconnected' + (err ? ` (${err})` : '');
    pm.className = 'pill ' + (mininetOk ? 'pill-ok' : 'pill-bad');
    pm.lastChild.textContent = mininetOk ? 'Mininet: connected' : 'Mininet: not running';
    $('banner').classList.toggle('hidden', controllerOk);
  }

  function render(u) {
    lastUpdate = u;
    setConn(u.controller.connected, u.mininet.connected, u.controller.error);
    renderJobs(u.mininet_status);
    if (!u.controller.connected) {
      ['sum-throughput', 'sum-flows', 'sum-healthy', 'sum-strategy', 'sum-attacks', 'sum-recovery', 'sum-model']
        .forEach(id => { $(id).textContent = '–'; });
      Topology.clear(); Charts.clear();
      $('servers').innerHTML = '<p class="muted">Controller disconnected.</p>';
      $('events').innerHTML = ''; $('blocked').innerHTML = '';
      return;
    }
    renderSummary(u.summary);
    Topology.update(u.topology, u.mininet_status ? u.mininet_status.links : []);
    renderServers(u.servers);
    Charts.updateThroughput(u.stats, u.servers);
    Charts.updateUtil(u.stats);
    renderPredSelect(u.predictions);
    Charts.updatePrediction(u.predictions, $('pred-link').value);
    renderEvents(u.events || []);
    renderSecurity(u.security);
    if (!strategyTouched) $('strategy').value = u.servers.strategy;
  }

  function renderSummary(s) {
    $('sum-throughput').textContent = s.total_mbps.toFixed(2) + ' Mbps';
    $('sum-flows').textContent = s.active_flows + ' (' + s.flow_paths + ' paths)';
    $('sum-healthy').textContent = `${s.healthy_servers}/${s.servers_total}`;
    $('sum-strategy').textContent = s.strategy;
    $('sum-attacks').textContent = s.attacks_blocked + (s.blocked_now.length ? ` (${s.blocked_now.length} active)` : '');
    $('sum-recovery').textContent = s.last_recovery_ms === null ? 'none yet' : s.last_recovery_ms.toFixed(1) + ' ms';
    $('sum-model').textContent = s.model;
  }

  function renderServers(data) {
    $('pool-info').textContent = `pool: [${data.pool.join(', ')}] · avg load ${data.avg_load.toFixed(1)}%` +
      (data.standby_active ? ' · standby active' : '');
    $('servers').innerHTML = data.servers.map(s => {
      const loadColor = s.load > 80 ? STATUS.crit : s.load >= 50 ? STATUS.warn : STATUS.good;
      const pred = s.predicted_load === null || s.predicted_load === undefined ? '–' : s.predicted_load.toFixed(1) + '%';
      return `<div class="card">
        <div class="card-head"><b>${esc(s.name)}</b><span class="badge badge-${esc(s.status)}">${esc(s.status)}${s.in_pool ? ' · in pool' : ''}</span></div>
        <div class="kv"><span>Load</span><span>${s.load.toFixed(1)}%</span></div>
        <div class="meter" role="meter" aria-valuenow="${s.load}" aria-valuemin="0" aria-valuemax="100"><div style="width:${Math.min(100, s.load)}%;background:${loadColor}"></div></div>
        <div class="kv"><span>Predicted load</span><span>${pred}</span></div>
        <div class="kv"><span>Active flows</span><span>${s.active_flows}</span></div>
        <div class="kv"><span>Assigned total</span><span>${s.total_assigned}</span></div>
        <div class="kv"><span>Clients</span><span>${s.clients.length ? esc(s.clients.join(', ')) : '–'}</span></div>
        <div class="kv"><span>Health RTT</span><span>${s.rtt_ms === null ? '–' : s.rtt_ms + ' ms'}</span></div>
        <div class="kv"><span>Check</span><span title="${esc(s.reason)}">${esc((s.reason || '').slice(0, 26))}</span></div>
      </div>`;
    }).join('');
  }

  function renderPredSelect(predictions) {
    const sel = $('pred-link');
    const ids = Object.keys(predictions.links).sort();
    const sig = ids.join(',');
    if (sel.dataset.sig === sig) return;
    const current = sel.value || 's3-s5';
    sel.dataset.sig = sig;
    sel.innerHTML = ids.map(id => `<option value="${id}">${id}</option>`).join('');
    sel.value = ids.includes(current) ? current : ids[0] || '';
  }

  function renderEvents(events) {
    $('events').innerHTML = events.slice().reverse().map(e =>
      `<li class="sev-${esc(e.severity)}"><span class="t">${timeStr(e.ts)}</span><span class="ty">${esc(e.type)}</span><span>${esc(e.message)}</span></li>`
    ).join('') || '<li class="muted">No events yet.</li>';
  }

  function renderSecurity(sec) {
    $('blocked').innerHTML = sec.blocked.length ? sec.blocked.map(b =>
      `<div class="blocked-item"><span><b>${esc(b.host)}</b> ${esc(b.ip)} · ${b.remaining}s left<br><span class="muted">${esc(b.reasons.join('; '))}</span></span>
       <button data-unblock="${esc(b.ip)}">Unblock</button></div>`).join('')
      : '<p class="muted small">No source is blocked.</p>';
    const rows = Object.entries(sec.sources);
    $('sources').innerHTML = rows.length ? rows.map(([ip, m]) =>
      `<tr><td>${esc(ip)}</td><td>${m.pps}</td><td>${m.new_flows_per_sec}</td><td>${m.syn}</td><td>${m.ack}</td></tr>`).join('')
      : '<tr><td colspan="5" class="muted">no data</td></tr>';
  }

  function renderJobs(status) {
    const jobs = status ? status.jobs : [];
    $('jobs').innerHTML = jobs.length ? jobs.map(j =>
      `<li>${esc(j.kind)}: ${esc(j.detail)} (${Math.max(0, Math.round(j.duration - (Date.now() / 1000 - j.started)))}s left)</li>`).join('')
      : '<li class="muted">none</li>';
  }

  // ------------------------------------------------------------------ controls
  async function post(action, payload) {
    const out = $('action-result');
    out.className = 'action-result';
    out.textContent = `${action}…`;
    try {
      const resp = await fetch('/control/' + action, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                       body: JSON.stringify(payload)});
      const body = await resp.json();
      if (!resp.ok || body.ok === false || body.error) throw new Error(body.error || resp.statusText);
      out.textContent = `${action}: OK ${body.cmd ? '(' + body.cmd + ')' : ''}`;
    } catch (err) {
      out.className = 'action-result err';
      out.textContent = `${action} failed: ${err.message}`;
    }
  }

  $('strategy').addEventListener('change', () => { strategyTouched = true; });

  document.addEventListener('click', ev => {
    const btn = ev.target.closest('button');
    if (!btn) return;
    if (btn.dataset.unblock) { post('unblock', {ip: btn.dataset.unblock}); return; }
    const [a, b] = $('link-select').value.split('-');
    switch (btn.dataset.action) {
      case 'strategy': strategyTouched = false; post('strategy', {strategy: $('strategy').value}); break;
      case 'link-down': post('link', {a, b, state: 'down'}); break;
      case 'link-up': post('link', {a, b, state: 'up'}); break;
      case 'server-stop': post('server', {name: $('server-select').value, action: 'stop'}); break;
      case 'server-start': post('server', {name: $('server-select').value, action: 'start'}); break;
      case 'traffic': {
        const mode = $('traffic-mode').value;
        const srcs = $('traffic-src').value === 'all' ? ['h1', 'h2', 'h3', 'h4'] : [$('traffic-src').value];
        srcs.forEach(src => post('traffic', {src, mode, duration: +$('traffic-duration').value,
                                             parallel: 4, bw: '8M'}));
        break;
      }
      case 'attack': post('attack', {src: $('attack-src').value, type: $('attack-type').value,
                                     duration: +$('attack-duration').value}); break;
      case 'unblock': post('unblock', {ip: $('unblock-ip').value.trim()}); break;
      case 'stop-traffic': post('stop_traffic', {}); break;
      default: break;
    }
  });

  $('pred-link').addEventListener('change', () => {
    if (lastUpdate && lastUpdate.controller.connected) Charts.updatePrediction(lastUpdate.predictions, $('pred-link').value);
  });
})();
