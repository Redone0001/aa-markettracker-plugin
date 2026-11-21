(() => {
  const root = document.getElementById('price-history');
  if (!root) return;
  root.querySelectorAll('[data-history-toggle], [data-history-location]').forEach(input => {
    input.addEventListener('change', () => {
      if (input.form.requestSubmit) input.form.requestSubmit();
      else input.form.submit();
    });
  });
  const data = document.getElementById('price-history-data');
  if (!data || typeof Chart === 'undefined') return;
  const samples = JSON.parse(data.textContent);
  const style = getComputedStyle(root);
  const accent = style.getPropertyValue('--bs-success').trim() || '#198754';
  const foreground = style.color;
  const muted = getComputedStyle(root.querySelector('.history-muted')).color;
  const border = style.getPropertyValue('--bs-border-color').trim() || muted;
  const channels = foreground.match(/^rgba?\(([^)]+)\)$/);
  const grid = channels ? `rgba(${channels[1].split(',').slice(0, 3).join(',')}, 0.12)` : border;
  const first = Date.parse(samples[0].time);
  const last = Date.parse(samples[samples.length - 1].time);
  const minimum = first === last ? first - 1800000 : first;
  const maximum = first === last ? last + 1800000 : last;
  const compact = value => Intl.NumberFormat(undefined, {notation: 'compact', maximumFractionDigits: 1}).format(value);
  const date = value => new Date(value).toLocaleString();
  const charts = [
    ['history-price-chart', 'line', 'price', 'ISK'],
    ['history-volume-chart', 'bar', 'volume', ''],
  ];
  charts.forEach(([id, type, key, unit]) => {
    new Chart(document.getElementById(id), {
      type,
      data: {datasets: [{
        label: unit || root.querySelector('.history-charts > :last-child h3').textContent.trim(),
        data: samples.map(sample => ({x: Date.parse(sample.time), y: sample[key] === null ? null : Number(sample[key])})),
        borderColor: accent, backgroundColor: accent + (accent.startsWith('#') && accent.length === 7 ? '88' : ''),
        borderWidth: type === 'line' ? 2 : 0, pointRadius: samples.length === 1 ? 3 : 0,
        pointHitRadius: 8, spanGaps: false,
      }]},
      options: {
        responsive: true, maintainAspectRatio: false, animation: false, normalized: true,
        interaction: {mode: 'nearest', axis: 'x', intersect: false},
        plugins: {legend: {display: false}, tooltip: {
          callbacks: {
            title: items => items.length ? date(items[0].parsed.x) : '',
            label: item => item.parsed.y === null ? '—' : Number(item.parsed.y).toLocaleString(undefined, {maximumFractionDigits: 2}) + (unit ? ' ' + unit : ''),
          },
        }},
        scales: {
          x: {type: 'linear', min: minimum, max: maximum, offset: type === 'bar', grid: {display: false}, border: {display: false}, ticks: {
            color: muted, maxTicksLimit: 4, callback: value => maximum - minimum < 86400000
              ? new Date(value).toLocaleTimeString(undefined, {hour: '2-digit', minute: '2-digit'})
              : new Date(value).toLocaleDateString(undefined, {day: 'numeric', month: 'short'}),
          }},
          y: {beginAtZero: type === 'bar', grid: {color: grid}, border: {display: false}, ticks: {color: foreground, maxTicksLimit: 4, callback: compact}},
        },
      },
    });
  });
})();
