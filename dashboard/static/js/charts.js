/* Chart.js line charts. Series colours are assigned to entities in a fixed order. */
const Charts = (() => {
  const css = getComputedStyle(document.documentElement);
  const tok = name => css.getPropertyValue(name).trim();
  const SERIES = [1, 2, 3, 4, 5, 6, 7, 8].map(i => tok('--series-' + i));
  const MUTED = tok('--text-muted'), GRID = tok('--border'), TEXT = tok('--text-secondary');
  const SERVER_ORDER = ['srv1', 'srv2', 'srv3', 'srv4'];
  const MAX_UTIL_LINKS = 4;
  let throughput, util, pred;
  let utilSelection = ['s3-s5', 's3-s6', 's4-s5', 's4-s6'];

  Chart.defaults.color = TEXT;
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.font.size = 11;

  const timeLabel = ts => new Date(ts * 1000).toLocaleTimeString([], {hour12: false});

  function baseOptions(yTitle, yMax) {
    return {
      responsive: true, maintainAspectRatio: false, animation: false, spanGaps: true,
      interaction: {mode: 'index', intersect: false},
      elements: {point: {radius: 0, hoverRadius: 4}, line: {borderWidth: 2, tension: 0.25}},
      scales: {
        x: {grid: {color: GRID, drawTicks: false}, ticks: {maxTicksLimit: 6, color: MUTED}},
        y: {beginAtZero: true, max: yMax, grid: {color: GRID}, ticks: {color: MUTED},
            title: {display: true, text: yTitle, color: MUTED}},
      },
      plugins: {legend: {position: 'bottom', labels: {boxWidth: 14, boxHeight: 2, usePointStyle: false}},
                tooltip: {callbacks: {label: c => `${c.dataset.label}: ${c.parsed.y === null ? '–' : c.parsed.y.toFixed(2)}`}}},
    };
  }

  function init() {
    throughput = new Chart(document.getElementById('chart-throughput'), {
      type: 'line', data: {labels: [], datasets: []}, options: baseOptions('Mbps')});
    util = new Chart(document.getElementById('chart-util'), {
      type: 'line', data: {labels: [], datasets: []}, options: baseOptions('% utilisation', 100)});
    pred = new Chart(document.getElementById('chart-pred'), {
      type: 'line', data: {labels: [], datasets: []}, options: baseOptions('% utilisation', 100)});
  }

  // Align a link's history to the reference timestamps (series may start later).
  function align(refTs, hist, field) {
    if (!hist || !hist.ts) return refTs.map(() => null);
    const byTs = new Map(hist.ts.map((t, i) => [t.toFixed(1), hist[field][i]]));
    return refTs.map(t => (byTs.has(t.toFixed(1)) ? byTs.get(t.toFixed(1)) : null));
  }

  function updateThroughput(stats, servers) {
    const ref = stats.total_history.ts;
    const datasets = [{label: 'Total served', data: stats.total_history.mbps, borderColor: MUTED,
                       borderDash: [5, 4], backgroundColor: MUTED}];
    const linkOf = {};
    Object.entries(stats.links).forEach(([id, l]) => { if (l.type === 'host') linkOf[l.target] = id; });
    SERVER_ORDER.forEach((name, i) => {
      if (!linkOf[name]) return;
      datasets.push({label: name, data: align(ref, stats.links[linkOf[name]].history, 'mbps'),
                     borderColor: SERIES[i], backgroundColor: SERIES[i]});
    });
    throughput.data.labels = ref.map(timeLabel);
    throughput.data.datasets = datasets;
    throughput.update('none');
  }

  function renderUtilPicker(linkIds) {
    const box = document.getElementById('util-links');
    const ids = linkIds.filter(id => /^s\d+-s\d+$/.test(id)).sort();
    const sig = ids.join(',') + '|' + utilSelection.join(',');
    if (box.dataset.sig === sig) return;
    box.dataset.sig = sig;
    box.innerHTML = ids.map(id => `<label><input type="checkbox" value="${id}" ${utilSelection.includes(id) ? 'checked' : ''}> ${id}</label>`).join('');
    box.querySelectorAll('input').forEach(cb => cb.addEventListener('change', () => {
      if (cb.checked) {
        if (utilSelection.length >= MAX_UTIL_LINKS) { cb.checked = false; return; }
        utilSelection.push(cb.value);
      } else {
        utilSelection = utilSelection.filter(v => v !== cb.value);
      }
      box.dataset.sig = '';
    }));
  }

  function updateUtil(stats) {
    renderUtilPicker(Object.keys(stats.links));
    const ref = stats.total_history.ts;
    util.data.labels = ref.map(timeLabel);
    // Colour follows the selection slot, which only changes when the user changes it.
    util.data.datasets = utilSelection.map((id, i) => ({
      label: id, data: align(ref, (stats.links[id] || {}).history, 'util'),
      borderColor: SERIES[i], backgroundColor: SERIES[i]}));
    util.update('none');
  }

  function updatePrediction(predictions, linkId) {
    const p = predictions.links[linkId];
    const note = document.getElementById('pred-note');
    if (!p) { pred.data.labels = []; pred.data.datasets = []; pred.update('none'); note.textContent = 'No data for this link yet.'; return; }
    const h = p.history;
    pred.data.labels = h.ts.map(timeLabel);
    pred.data.datasets = [
      {label: 'Actual', data: h.actual, borderColor: SERIES[0], backgroundColor: SERIES[0]},
      {label: `Predicted (${predictions.horizon_s}s earlier)`, data: h.predicted, borderColor: SERIES[1],
       backgroundColor: SERIES[1], borderDash: [6, 3]},
      {label: `Congestion threshold ${predictions.threshold}%`, data: h.ts.map(() => predictions.threshold),
       borderColor: MUTED, backgroundColor: MUTED, borderDash: [2, 4], borderWidth: 1, pointHoverRadius: 0},
    ];
    pred.update('none');
    const next = p.predicted === null || p.predicted === undefined ? '–' : p.predicted.toFixed(1);
    note.textContent = `Model: ${predictions.model}. Now ${p.actual.toFixed(1)}%, predicted in ${predictions.horizon_s}s: ${next}%` +
      (p.congested ? ' (congestion predicted, new flows steered away)' : '');
  }

  function clear() {
    [throughput, util, pred].forEach(c => { c.data.labels = []; c.data.datasets = []; c.update('none'); });
  }

  return {init, updateThroughput, updateUtil, updatePrediction, clear};
})();
