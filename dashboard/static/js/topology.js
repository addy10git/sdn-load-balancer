/* Live topology: D3 force graph laid out in tiers (clients -> edge -> core -> edge -> servers). */
{
  // Say so on the page if a library is missing, instead of silently showing "connecting".
  const missing = [['io', 'Socket.IO'], ['d3', 'D3'], ['Chart', 'Chart.js']].filter(([g]) => !window[g]).map(([, n]) => n);
  if (missing.length) {
    const b = document.getElementById('banner');
    b.textContent = `Dashboard libraries failed to load (${missing.join(', ')}): check dashboard/static/vendor/ and the browser console.`;
    b.classList.remove('hidden');
  }
}
const Topology = (() => {
  const css = getComputedStyle(document.documentElement);
  const tok = name => css.getPropertyValue(name).trim();
  const COLORS = {
    good: tok('--good'), warn: tok('--warning'), crit: tok('--critical'),
    muted: tok('--text-muted'), surface: tok('--surface-1'), surface2: tok('--surface-2'),
    border: tok('--border'), switch: '#2b4c75', client: '#1d5b4b', server: '#4a3f86',
  };
  let root, svg, gLinks, gPred, gHit, gNodes, sim, tooltip, container;
  let nodes = new Map();
  let structureKey = '';

  function init(el, tooltipEl) {
    container = el;
    tooltip = tooltipEl;
    svg = d3.select(el).append('svg').attr('role', 'img').attr('aria-label', 'network topology');
    root = svg.append('g');
    gPred = root.append('g');
    gLinks = root.append('g');
    gHit = root.append('g');
    gNodes = root.append('g');
    sim = d3.forceSimulation()
      .force('link', d3.forceLink().id(d => d.id).distance(70).strength(0.05))
      .force('charge', d3.forceManyBody().strength(-120))
      .force('collide', d3.forceCollide(26))
      .on('tick', tick);
    window.addEventListener('resize', () => { if (structureKey) restart(0.3); });
  }

  const utilColor = u => (u > 80 ? COLORS.crit : u >= 50 ? COLORS.warn : COLORS.good);
  const linkWidth = l => (l.type === 'down' ? 2 : 2 + Math.min(8, (l.mbps || 0) * 0.8));

  function tierOf(n, clientSwitches, serverSwitches) {
    if (n.kind === 'host') return n.role === 'server' ? 4 : 0;
    if (clientSwitches.has(n.id)) return 1;
    if (serverSwitches.has(n.id)) return 3;
    return 2;
  }

  const tierX = t => 50 + (container.clientWidth - 100) * t / 4;

  // Every node gets a fixed slot: x by tier, y spread evenly inside its tier.
  function assignSlots(nodeList) {
    const h = container.clientHeight;
    const byTier = d3.group(nodeList, d => d.tier);
    byTier.forEach(list => {
      list.sort((a, b) => d3.ascending(a.sortKey, b.sortKey));
      list.forEach((n, i) => { n.slotY = 30 + (h - 70) * (i + 1) / (list.length + 1); });
    });
  }

  function restart(alpha) {
    assignSlots(sim.nodes());
    sim.force('x', d3.forceX(d => tierX(d.tier)).strength(1))
       .force('y', d3.forceY(d => d.slotY).strength(0.6));
    sim.alpha(alpha).restart();
  }

  function update(topo, mininetLinks) {
    const clientSwitches = new Set(), serverSwitches = new Set();
    topo.hosts.forEach(h => (h.role === 'server' ? serverSwitches : clientSwitches).add(h.switch));

    const newNodes = [];
    topo.switches.forEach(s => newNodes.push({id: s.id, kind: 'switch', data: s}));
    topo.hosts.forEach(h => newNodes.push({id: h.id, kind: 'host', role: h.role, data: h}));
    newNodes.forEach(n => {
      n.tier = tierOf(n, clientSwitches, serverSwitches);
      // keep hosts next to their switch inside a tier
      n.sortKey = n.kind === 'host' ? n.data.switch + '/' + n.id : n.id;
    });
    assignSlots(newNodes);
    newNodes.forEach(n => {
      const old = nodes.get(n.id);
      if (old) { n.x = old.x; n.y = old.y; n.vx = old.vx; n.vy = old.vy; }
      else { n.x = tierX(n.tier); n.y = n.slotY; }
    });
    const ids = new Set(newNodes.map(n => n.id));

    const links = topo.links.filter(l => ids.has(l.source) && ids.has(l.target)).map(l => ({...l}));
    // Links that Mininet reports as administratively down disappear from the controller's
    // graph; draw them greyed out so the failure is visible.
    const present = new Set(links.map(l => l.id));
    (mininetLinks || []).forEach(m => {
      if (m.state !== 'down' || present.has(m.link)) return;
      const [a, b] = m.link.split('-');
      if (ids.has(a) && ids.has(b)) links.push({id: m.link, source: a, target: b, type: 'down'});
    });

    const key = [...ids].sort().join(',') + '|' + links.map(l => l.id + l.type).sort().join(',');
    nodes = new Map(newNodes.map(n => [n.id, n]));
    document.getElementById('topo-empty').classList.toggle('hidden', newNodes.length > 0);

    if (key !== structureKey) {
      structureKey = key;
      sim.nodes(newNodes);
      sim.force('link').links(links);
      draw(links, newNodes);
      restart(0.8);
    } else {
      // Same structure: keep positions, refresh the simulation's link objects' data.
      const byId = new Map(links.map(l => [l.id, l]));
      sim.force('link').links().forEach(l => Object.assign(l, byId.get(l.id), {source: l.source, target: l.target}));
      sim.nodes().forEach(n => { const fresh = nodes.get(n.id); if (fresh) { n.data = fresh.data; n.role = fresh.role; } });
      nodes = new Map(sim.nodes().map(n => [n.id, n]));
      draw(sim.force('link').links(), sim.nodes());
    }
    tick();
  }

  function draw(links, nodeList) {
    gPred.selectAll('line').data(links.filter(l => l.predicted_congested), d => d.id)
      .join('line')
      .attr('stroke', COLORS.crit).attr('stroke-dasharray', '10 6').attr('fill', 'none')
      .attr('stroke-width', d => linkWidth(d) + 8).attr('stroke-opacity', 0.55);

    gLinks.selectAll('line').data(links, d => d.id)
      .join('line')
      .attr('stroke', d => (d.type === 'down' ? COLORS.muted : utilColor(d.util || 0)))
      .attr('stroke-dasharray', d => (d.type === 'down' ? '4 4' : null))
      .attr('stroke-width', linkWidth)
      .attr('stroke-linecap', 'round');

    gHit.selectAll('line').data(links, d => d.id)
      .join('line').attr('class', 'link-hit')
      .on('mousemove', (ev, d) => show(ev, linkTip(d)))
      .on('mouseleave', hide);

    const sel = gNodes.selectAll('g.node').data(nodeList, d => d.id);
    sel.exit().remove();
    const enter = sel.enter().append('g').attr('class', 'node')
      .call(d3.drag()
        .on('start', (ev, d) => { if (!ev.active) sim.alphaTarget(0.2).restart(); d.fx = d.x; d.fy = d.y; })
        .on('drag', (ev, d) => { d.fx = ev.x; d.fy = ev.y; })
        .on('end', (ev, d) => { if (!ev.active) sim.alphaTarget(0); d.fx = null; d.fy = null; }));
    enter.append('g').attr('class', 'shape');
    enter.append('text').attr('class', 'node-label').attr('dy', 4);
    enter.append('text').attr('class', 'node-sub').attr('dy', 30);
    const all = enter.merge(sel);
    all.on('mousemove', (ev, d) => show(ev, nodeTip(d))).on('mouseleave', hide);
    all.each(function (d) { drawNode(d3.select(this), d); });
  }

  function drawNode(g, d) {
    const shape = g.select('g.shape');
    shape.selectAll('*').remove();
    const h = d.data;
    if (d.kind === 'switch') {
      shape.append('rect').attr('x', -17).attr('y', -13).attr('width', 34).attr('height', 26).attr('rx', 6)
        .attr('fill', COLORS.switch).attr('stroke', '#5c8fd0').attr('stroke-width', 1.5);
      g.select('.node-label').text(d.id);
      g.select('.node-sub').text('');
      return;
    }
    if (d.role === 'server') {
      const down = h.status === 'down';
      const standbyIdle = h.standby && !h.in_pool;
      shape.append('rect').attr('x', -14).attr('y', -17).attr('width', 28).attr('height', 34).attr('rx', 4)
        .attr('fill', down ? '#5a2222' : COLORS.server)
        .attr('stroke', down ? COLORS.crit : '#9085e9').attr('stroke-width', 1.5)
        .attr('stroke-dasharray', standbyIdle ? '4 3' : null);
      [-8, 0, 8].forEach(y => shape.append('line').attr('x1', -8).attr('x2', 8).attr('y1', y).attr('y2', y)
        .attr('stroke', '#c9c3f5').attr('stroke-opacity', 0.5));
      g.select('.node-label').text('').attr('dy', 4);
      g.select('.node-sub').text(d.id + (down ? ' (down)' : standbyIdle ? ' (standby)' : '')).attr('dy', 31);
    } else {
      shape.append('circle').attr('r', 14).attr('fill', COLORS.client).attr('stroke', '#199e70').attr('stroke-width', 1.5);
      g.select('.node-label').text(d.id);
      g.select('.node-sub').text(h.ip || '').attr('dy', 28);
    }
    if (h.blocked) {
      shape.append('circle').attr('r', 21).attr('fill', 'none').attr('stroke', COLORS.crit).attr('stroke-width', 3);
      const badge = shape.append('g').attr('transform', 'translate(15,-15)');
      badge.append('circle').attr('r', 7).attr('fill', COLORS.crit);
      badge.append('path').attr('d', 'M-3,-3L3,3M3,-3L-3,3').attr('stroke', '#fff').attr('stroke-width', 2);
    }
  }

  function tick() {
    const w = container.clientWidth, h = container.clientHeight;
    sim.nodes().forEach(n => { n.x = Math.max(24, Math.min(w - 24, n.x || w / 2)); n.y = Math.max(24, Math.min(h - 34, n.y || h / 2)); });
    const pos = sel => sel
      .attr('x1', d => d.source.x).attr('y1', d => d.source.y)
      .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
    pos(gPred.selectAll('line')); pos(gLinks.selectAll('line')); pos(gHit.selectAll('line'));
    gNodes.selectAll('g.node').attr('transform', d => `translate(${d.x},${d.y})`);
  }

  const fmt = (v, digits = 1) => (v === null || v === undefined ? '–' : Number(v).toFixed(digits));
  function linkTip(d) {
    if (d.type === 'down') return `<b>${d.id}</b><br><span class="k">state</span>DOWN`;
    return `<b>${d.id}</b> (${d.type} link)<br>` +
      `<span class="k">utilisation</span>${fmt(d.util)} %<br>` +
      `<span class="k">throughput</span>${fmt(d.mbps, 2)} Mbps<br>` +
      `<span class="k">drops</span>${d.drops ?? '–'}<br>` +
      `<span class="k">flows</span>${d.flows ?? '–'}<br>` +
      `<span class="k">predicted</span>${fmt(d.predicted)} %${d.predicted_congested ? ' (congestion!)' : ''}` +
      (d.weight !== undefined ? `<br><span class="k">route cost</span>${fmt(d.weight, 2)}` : '');
  }
  function nodeTip(d) {
    const h = d.data;
    if (d.kind === 'switch') return `<b>${d.id}</b><br><span class="k">dpid</span>${h.dpid}<br><span class="k">connected</span>${h.connected ? 'yes' : 'no'}`;
    let s = `<b>${d.id}</b> (${h.role})<br><span class="k">IP</span>${h.ip || '–'}<br><span class="k">MAC</span>${h.mac}<br><span class="k">attached</span>${h.switch} port ${h.port}`;
    if (h.role === 'server') s += `<br><span class="k">health</span>${h.status}${h.in_pool ? ' (in pool)' : ''}`;
    if (h.blocked) s += '<br><span class="k">security</span>BLOCKED';
    return s;
  }
  function show(ev, html) {
    tooltip.innerHTML = html;
    tooltip.classList.remove('hidden');
    const box = tooltip.parentElement.getBoundingClientRect();
    let x = ev.clientX - box.left + 14, y = ev.clientY - box.top + 14;
    if (x + tooltip.offsetWidth > box.width) x = ev.clientX - box.left - tooltip.offsetWidth - 14;
    tooltip.style.left = x + 'px';
    tooltip.style.top = y + 'px';
  }
  function hide() { tooltip.classList.add('hidden'); }

  function clear() {
    structureKey = '';
    nodes = new Map();
    sim.nodes([]); sim.force('link').links([]);
    draw([], []);
    document.getElementById('topo-empty').classList.remove('hidden');
  }

  return {init, update, clear};
})();
