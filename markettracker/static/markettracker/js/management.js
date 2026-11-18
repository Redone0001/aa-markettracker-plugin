$(function () {
  document.querySelectorAll('.stock-record img').forEach(image => {
    const fallback = () => {
      const icon = document.createElement('span');
      icon.className = 'stock-record-icon';
      icon.setAttribute('aria-hidden', 'true');
      icon.innerHTML = '<i class="fa-solid fa-cube"></i>';
      image.replaceWith(icon);
    };
    image.addEventListener('error', fallback, {once: true});
    if (image.complete && !image.naturalWidth) fallback();
  });
  const root = document.querySelector('.stock-management');
  const nativeControl = root && (root.querySelector('.form-control') || root.querySelector('.form-select'));
  if (nativeControl) {
    const style = getComputedStyle(nativeControl);
    root.style.setProperty('--stock-control-bg', style.backgroundColor);
    root.style.setProperty('--stock-control-color', style.color);
    root.style.setProperty('--stock-control-border', style.borderColor);
  }
  document.querySelectorAll('[data-contract-form]').forEach(form => {
    const mode = form.querySelector('[name="mode"]');
    const toggle = () => {
      form.querySelectorAll('[data-contract-field]').forEach(field => {
        field.hidden = field.dataset.contractField !== mode.value;
      });
    };
    mode.addEventListener('change', toggle);
    toggle();
  });
  document.querySelectorAll('[data-doctrine-source-form]').forEach(form => {
    const kind = form.querySelector('[name="source_kind"]');
    const toggle = () => {
      ['doctrine', 'fitting'].forEach(name => {
        const field = form.querySelector(`[data-source-field="${name}"]`);
        if (field) field.hidden = kind.value !== name;
      });
    };
    kind.addEventListener('change', () => {
      const inactive = form.querySelector(`[name="${kind.value === 'doctrine' ? 'fitting' : 'doctrine'}"]`);
      if (inactive) $(inactive).val(null).trigger('change');
      toggle();
    });
    toggle();
  });
  const error = document.querySelector('.stock-editor .invalid-feedback');
  if (error) {
    const field = error.closest('.stock-field');
    const input = field && (field.querySelector('.select2-selection') || field.querySelector('input:not([type="hidden"]), select, textarea'));
    if (input) input.focus();
  }
});
