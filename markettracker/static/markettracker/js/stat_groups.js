$(function () {
  const form = document.querySelector('[data-stat-choices-url]');
  if (!form) return;
  const mode = form.querySelector('[name="group_mode"]');
  const reference = form.querySelector('[name="stats_reference"]');
  const field = name => form.querySelector(`[data-group-field="${name}"]`);
  function toggle() {
    const stats = mode.value === 'stats';
    field('items').hidden = stats;
    field('stats_reference').hidden = !stats;
    field('matching_stats').hidden = !stats;
  }
  mode.addEventListener('change', toggle);
  toggle();
  let generation = 0;
  $(reference).on('change', async function () {
    const current = ++generation;
    const container = field('matching_stats');
    container.querySelectorAll('input[name="matching_stats"]').forEach(input => input.closest('label')?.remove());
    if (!reference.value) return;
    try {
      const url = new URL(form.dataset.statChoicesUrl, window.location.origin);
      url.searchParams.set('reference', reference.value);
      const response = await fetch(url, {credentials: 'same-origin'});
      if (!response.ok) throw new Error('Unable to load stats.');
      const result = await response.json();
      if (current !== generation) return;
      const choices = container.querySelector('[data-stat-options]') || document.createElement('div');
      choices.dataset.statOptions = '';
      choices.replaceChildren();
      // Replace Django's initial widget so stale choices cannot be submitted.
      container.querySelectorAll('input[name="matching_stats"]').forEach(input => input.remove());
      result.stats.forEach(stat => {
        const label = document.createElement('label');
        label.className = 'd-block';
        const input = document.createElement('input');
        input.type = 'checkbox'; input.name = 'matching_stats'; input.value = stat.id; input.checked = true;
        label.append(input, document.createTextNode(' ' + stat.name));
        choices.append(label);
      });
      container.append(choices);
    } catch (error) {
      const warning = document.createElement('p');
      warning.textContent = error.message + ' Saving with no selection matches all stats.';
      container.append(warning);
    }
  });
});
