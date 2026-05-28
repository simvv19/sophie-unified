/* Custom select — remplace les dropdowns natifs macOS par un menu stylé.
   Marche automatiquement sur tous les <select> sauf ceux avec class="no-cs". */
(function () {
  // Injecte le style une seule fois
  const style = document.createElement('style');
  style.textContent = `
    select.cs-hidden { display: none !important; }
    .cs { position: relative; display: inline-block; vertical-align: middle; }
    .cs select { display: none !important; }
    .cs-btn {
      font-family: inherit; font-size: 13px; font-weight: 400;
      padding: 8px 30px 8px 10px;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: var(--card); color: var(--text);
      cursor: pointer; text-align: left;
      min-width: 110px; width: 100%;
      position: relative;
      transition: all .15s;
      display: flex; align-items: center; gap: 6px;
      line-height: 1.3;
    }
    .cs-btn:hover { border-color: var(--border-strong); background: var(--hover); }
    .cs-btn:focus { outline: 0; border-color: var(--text-2); box-shadow: 0 0 0 3px var(--accent-soft); }
    .cs-btn .cs-label { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .cs-btn::after {
      content: ''; position: absolute; right: 11px; top: 50%;
      width: 6px; height: 6px;
      border-right: 1.5px solid var(--text-3);
      border-bottom: 1.5px solid var(--text-3);
      transform: translateY(-70%) rotate(45deg);
      pointer-events: none;
    }
    .cs-btn.small { font-size: 12px; padding: 5px 26px 5px 9px; }
    .cs-list {
      position: absolute; top: calc(100% + 5px); left: 0;
      min-width: 100%;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.12), 0 2px 6px rgba(0,0,0,0.06);
      padding: 4px;
      max-height: 280px;
      overflow-y: auto;
      z-index: 2000;
      display: none;
      animation: csIn .12s ease;
    }
    html[data-theme="dark"] .cs-list { box-shadow: 0 10px 30px rgba(0,0,0,0.5); }
    @keyframes csIn { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: translateY(0); } }
    .cs.open .cs-list { display: block; }
    .cs.up .cs-list { top: auto; bottom: calc(100% + 5px); transform-origin: bottom; }
    .cs-item {
      padding: 7px 10px;
      border-radius: 5px;
      font-size: 13px;
      cursor: pointer;
      display: flex; align-items: center; gap: 8px;
      white-space: nowrap;
      color: var(--text);
      transition: background .1s;
    }
    .cs-item:hover { background: var(--hover); }
    .cs-item.selected { background: var(--accent-soft); font-weight: 500; }
    .cs-item.selected::before {
      content: ''; width: 12px; height: 6px;
      border-left: 1.5px solid var(--text); border-bottom: 1.5px solid var(--text);
      transform: rotate(-45deg) translateY(-2px);
      flex-shrink: 0;
    }
    .cs-item:not(.selected)::before { content: ''; width: 12px; flex-shrink: 0; }
    .cs-item[data-disabled="1"] { color: var(--text-4); cursor: default; pointer-events: none; }
  `;
  document.head.appendChild(style);

  function enhance(sel) {
    if (sel._cs) return;
    // Si le select est dans un .cs déjà construit (recréation), skip
    if (sel.parentNode && sel.parentNode.classList && sel.parentNode.classList.contains('cs')) return;

    const wrap = document.createElement('div');
    wrap.className = 'cs';

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cs-btn';
    const lbl = document.createElement('span');
    lbl.className = 'cs-label';
    btn.appendChild(lbl);

    const list = document.createElement('div');
    list.className = 'cs-list';

    // Place wrap avant le select, puis bouge le select dans le wrap
    sel.parentNode.insertBefore(wrap, sel);
    wrap.appendChild(btn);
    wrap.appendChild(list);
    wrap.appendChild(sel);
    sel.classList.add('cs-hidden');

    function render() {
      const opt = sel.options[sel.selectedIndex];
      lbl.textContent = opt ? opt.text : '';
      list.innerHTML = '';
      Array.from(sel.options).forEach((o, i) => {
        const item = document.createElement('div');
        item.className = 'cs-item' + (i === sel.selectedIndex ? ' selected' : '');
        if (o.disabled) item.dataset.disabled = '1';
        item.textContent = o.text;
        item.onclick = e => {
          e.stopPropagation();
          if (o.disabled) return;
          sel.selectedIndex = i;
          sel.dispatchEvent(new Event('change', { bubbles: true }));
          close();
        };
        list.appendChild(item);
      });
    }

    function open() {
      closeAll();
      // Décide haut ou bas selon l'espace dispo
      const r = btn.getBoundingClientRect();
      const spaceBelow = window.innerHeight - r.bottom;
      const spaceAbove = r.top;
      wrap.classList.remove('up');
      if (spaceBelow < 200 && spaceAbove > spaceBelow) wrap.classList.add('up');
      render();
      wrap.classList.add('open');
    }
    function close() { wrap.classList.remove('open'); }

    btn.onclick = e => {
      e.stopPropagation();
      e.preventDefault();
      if (wrap.classList.contains('open')) close();
      else open();
    };

    sel.addEventListener('change', render);

    // Re-render quand les options changent (innerHTML repopulé etc.)
    new MutationObserver(() => { if (!sel.dataset._csInternal) render(); })
      .observe(sel, { childList: true, characterData: true, subtree: true });

    sel._cs = { render, open, close };
    render();
  }

  function closeAll() {
    document.querySelectorAll('.cs.open').forEach(w => w.classList.remove('open'));
  }

  function enhanceAll(root = document) {
    root.querySelectorAll('select:not(.no-cs):not(.cs-hidden)').forEach(enhance);
  }

  document.addEventListener('click', e => {
    if (!e.target.closest('.cs')) closeAll();
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeAll(); });

  // Init
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => enhanceAll());
  } else {
    enhanceAll();
  }

  // Re-enhance nouveaux selects créés dynamiquement
  new MutationObserver(muts => {
    muts.forEach(m => {
      m.addedNodes && m.addedNodes.forEach(n => {
        if (n.nodeType !== 1) return;
        if (n.tagName === 'SELECT' && !n.classList.contains('no-cs')) enhance(n);
        else enhanceAll(n);
      });
    });
  }).observe(document.body, { childList: true, subtree: true });

  // Override value setter pour rafraîchir l'affichage quand code fait sel.value = ...
  const desc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value');
  Object.defineProperty(HTMLSelectElement.prototype, 'value', {
    set(v) { desc.set.call(this, v); if (this._cs) this._cs.render(); },
    get() { return desc.get.call(this); },
    configurable: true
  });
  // Pareil pour selectedIndex
  const desc2 = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'selectedIndex');
  Object.defineProperty(HTMLSelectElement.prototype, 'selectedIndex', {
    set(v) { desc2.set.call(this, v); if (this._cs) this._cs.render(); },
    get() { return desc2.get.call(this); },
    configurable: true
  });
})();
