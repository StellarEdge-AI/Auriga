// Move the shared search area out of the way before scrolling either column.
// Once the columns reach the top, their scrolling stays native and independent.
export function setupLibraryScroll() {
  const layout = document.querySelector('.library-layout');
  const filters = document.getElementById('library-filters');
  const showFilters = document.getElementById('library-show-filters');
  const desktop = matchMedia('(min-width: 761px)');
  const inset = 12;

  function update() {
    showFilters.hidden = !desktop.matches || filters.getBoundingClientRect().bottom > 0;
  }
  function nestedScroll(target, panel, delta) {
    for (let node = target; node && node !== panel; node = node.parentElement) {
      if (node.matches('select, textarea') || node.isContentEditable) return true;
      const style = getComputedStyle(node);
      if (/(auto|scroll)/.test(style.overflowY) && node.scrollHeight > node.clientHeight + 1
          && (delta < 0 ? node.scrollTop > 0 : node.scrollTop + node.clientHeight < node.scrollHeight - 1)) return true;
    }
    return false;
  }
  layout.addEventListener('wheel', (event) => {
    if (!desktop.matches || event.defaultPrevented || event.ctrlKey || event.metaKey || event.shiftKey
        || !event.deltaY || Math.abs(event.deltaX) > Math.abs(event.deltaY)) return;
    const panel = event.target.closest('.experiment-list-panel, .experiment-detail');
    if (!panel || nestedScroll(event.target, panel, event.deltaY)) return;
    const delta = event.deltaY * (event.deltaMode === 1 ? 20 : event.deltaMode === 2 ? innerHeight : 1);
    const dock = Math.max(0, Math.min(scrollY + layout.getBoundingClientRect().top - inset,
      document.documentElement.scrollHeight - innerHeight));
    if (delta > 0 && scrollY < dock - 1) {
      event.preventDefault();
      const pageDelta = Math.min(delta, dock - scrollY);
      window.scrollBy({ top: pageDelta, behavior: 'instant' });
      // A large wheel gesture may finish hiding the search area and continue
      // into the hovered column in one movement, without touching its neighbor.
      if (delta > pageDelta) panel.scrollTop += delta - pageDelta;
    } else if (delta < 0 && panel.scrollTop <= 0 && scrollY > 0) {
      event.preventDefault();
      window.scrollBy({ top: delta, behavior: 'instant' });
    }
  }, { passive: false });

  showFilters.addEventListener('click', () => {
    window.scrollTo({ top: 0, behavior: 'instant' });
    document.getElementById('q').focus({ preventScroll: true });
  });
  window.addEventListener('scroll', update, { passive: true });
  window.addEventListener('resize', update);
  desktop.addEventListener('change', update);
  new ResizeObserver(update).observe(filters);
  update();
}
