(() => {
  const status = document.getElementById('doctrine-buy-status');
  document.querySelectorAll('.doctrine-buy-form').forEach(form => {
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const button = event.submitter;
      if (!button) return;
      const data = new FormData(form);
      data.set('mode', button.value);
      button.disabled = true;
      status.classList.remove('d-none');
      try {
        const response = await fetch(form.action, {method: 'POST', body: data, credentials: 'same-origin'});
        if (!response.ok) {
          const error = await response.json().catch(() => ({}));
          throw new Error(error.error || 'Unable to prepare Multibuy. Refresh the page and try again.');
        }
        const result = await response.json();
        if (!result.text) { status.textContent = 'The market target is already covered. No missing items.'; return; }
        try {
          await navigator.clipboard.writeText(result.text);
        } catch (_) {
          const textarea = document.createElement('textarea');
          textarea.value = result.text;
          status.replaceChildren(textarea);
          textarea.select();
          if (!document.execCommand('copy')) {
            const help = document.createElement('p');
            help.textContent = 'Copy this list and paste it into EVE Multibuy.';
            status.prepend(help);
            return;
          }
        }
        status.textContent = 'Copied. Paste into EVE Multibuy.' + (result.unpriced ? ' Some items have no local sell orders; their original fit variation was used.' : '');
      } catch (error) {
        status.textContent = error.message || 'Unable to prepare Multibuy.';
      } finally {
        button.disabled = false;
      }
    });
  });
})();
