(() => {
  let status, dismiss;
  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-copy-price]');
    if (!button || !/^-?\d+$/.test(button.dataset.copyPrice)) return;
    const value = button.dataset.copyPrice;
    let copied = false;
    try {
      await navigator.clipboard.writeText(value);
      copied = true;
    } catch (_) {
      const input = document.createElement('textarea');
      input.value = value;
      input.style.position = 'fixed';
      input.style.opacity = '0';
      document.body.append(input);
      input.select();
      try { copied = document.execCommand('copy'); } catch (_) { /* Show manual copy below. */ }
      input.remove();
      button.focus();
    }
    if (!status) {
      status = document.createElement('div');
      status.className = 'market-price-copy-status';
      status.setAttribute('role', 'status');
      status.setAttribute('aria-live', 'polite');
      document.body.append(status);
    }
    clearTimeout(dismiss);
    status.hidden = false;
    status.textContent = copied ? `Copied ${value} ISK` : 'Copy this whole ISK amount:';
    if (!copied) {
      const input = document.createElement('input');
      input.readOnly = true;
      input.value = value;
      input.setAttribute('aria-label', 'Whole ISK amount to copy');
      status.append(input);
      input.focus();
      input.select();
    } else {
      dismiss = setTimeout(() => { status.hidden = true; }, 3500);
    }
  });
})();
