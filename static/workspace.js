/* Workspace switcher — gère l'espace courant.
   Charger AVANT le script inline de la page (dans <head>).
   Principal a un UUID fixe — pas de risque que ce soit null. */
window.SOPHIE_WS = window.SOPHIE_WS || {};
const _WS_KEY = 'sophie_workspace';
const _WS_PRINCIPAL = '00000000-0000-0000-0000-000000000001';
const _WS_AFFECTED = ['acquisition','flotte','emails'];

// Default avant init: Principal — pour qu'aucune query ne parte sans wsId
window.SOPHIE_WS.list = [{ id: _WS_PRINCIPAL, name: 'Principal', emoji: '🏠', color: '#5b6cf5' }];
window.SOPHIE_WS.current = window.SOPHIE_WS.list[0];

window.SOPHIE_WS.id = function() { return window.SOPHIE_WS.current?.id || _WS_PRINCIPAL; };

window.SOPHIE_WS.init = async function(sb) {
  window.SOPHIE_WS._sb = sb;
  // Attendre que le gate ait chargé le membre (max ~3s)
  for (let i = 0; i < 30 && !window.SOPHIE_MEMBER; i++) { await new Promise(r => setTimeout(r, 100)); }
  try {
    const { data, error } = await sb.from('workspaces').select('*').order('position').order('created_at');
    if (error) { console.warn('workspaces fetch failed, fallback Principal', error); return window.SOPHIE_WS.current; }
    let list = data && data.length ? data : window.SOPHIE_WS.list;
    // Restreindre selon les espaces autorisés du membre (si défini)
    const member = window.SOPHIE_MEMBER;
    if (member && member.role !== 'admin' && Array.isArray(member.workspace_ids) && member.workspace_ids.length) {
      const allowed = list.filter(w => member.workspace_ids.includes(w.id));
      if (allowed.length) list = allowed;
    }
    window.SOPHIE_WS.list = list;
  } catch (e) { console.warn('workspaces fetch threw', e); }

  // Restaure la sélection
  let cur = localStorage.getItem(_WS_KEY);
  if (!cur || !window.SOPHIE_WS.list.find(w => w.id === cur)) {
    cur = _WS_PRINCIPAL;
    localStorage.setItem(_WS_KEY, cur);
  }
  window.SOPHIE_WS.current = window.SOPHIE_WS.list.find(w => w.id === cur) || window.SOPHIE_WS.list[0];

  injectSwitcher();
  return window.SOPHIE_WS.current;
};

function pageIsAffected() {
  const seg = location.pathname.replace(/\/+$/, '').split('/').pop() || 'acquisition';
  const page = seg === 'dashboard' ? 'acquisition' : seg;
  return _WS_AFFECTED.includes(page);
}

function injectSwitcher() {
  const topbar = document.querySelector('.topbar > div:last-child');
  if (!topbar) return;
  if (document.getElementById('_wsWrap')) return;
  const wrap = document.createElement('div');
  wrap.id = '_wsWrap';
  wrap.style.cssText = 'position:relative;display:inline-block;margin-right:8px;z-index:9999;';
  const cur = window.SOPHIE_WS.current;
  const affected = pageIsAffected();
  wrap.innerHTML = `
    <button id="_wsBtn" title="${affected?"Changer d'espace":"Cette page n'est pas filtrée par espace"}" style="display:inline-flex;align-items:center;gap:7px;padding:6px 12px;border:1px solid var(--border);border-radius:8px;background:var(--card);color:var(--text);font-family:inherit;font-size:12px;font-weight:500;cursor:pointer;transition:all .15s;${affected?'':'opacity:0.6;'}">
      <span style="display:inline-flex;align-items:center;gap:6px;">
        <span style="font-size:13px;line-height:1;">${cur?.emoji||'🏠'}</span>
        <span>${cur?.name||'Principal'}</span>
      </span>
      <svg width="9" height="9" viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" style="opacity:0.6;"><path d="M2.5 4 L5 6.5 L7.5 4"/></svg>
    </button>
  `;
  topbar.insertBefore(wrap, topbar.firstChild);
  // Popup appended to body for proper stacking (escapes all parent stacking contexts)
  if (!document.getElementById('_wsPop')) {
    const pop = document.createElement('div');
    pop.id = '_wsPop';
    pop.style.cssText = 'display:none;position:fixed;background:var(--card);border:1px solid var(--border);border-radius:10px;box-shadow:0 10px 28px rgba(0,0,0,0.18);padding:4px;min-width:260px;z-index:2147483000;';
    document.body.appendChild(pop);
  }
  const btn = document.getElementById('_wsBtn');
  btn.onclick = e => { e.stopPropagation(); togglePopup(); };
  document.addEventListener('click', e => { if (!e.target.closest('#_wsWrap') && !e.target.closest('#_wsPop')) hidePopup(); });
}

function buildPopupContent() {
  const cur = window.SOPHIE_WS.current;
  const items = window.SOPHIE_WS.list.map(w => `
    <button data-ws="${w.id}" style="width:100%;text-align:left;display:flex;align-items:center;gap:10px;padding:8px 10px;border:0;background:${w.id===cur?.id?'var(--accent-soft)':'transparent'};color:var(--text);font-family:inherit;font-size:13px;font-weight:${w.id===cur?.id?'500':'400'};border-radius:6px;cursor:pointer;">
      <span style="font-size:14px;line-height:1;">${w.emoji||'🏠'}</span>
      <span style="flex:1;">${w.name}</span>
      ${w.id===cur?.id?'<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 8l3 3 7-7"/></svg>':''}
    </button>
  `).join('');
  return `
    <div style="padding:4px;">
      <div style="font-size:10px;font-weight:600;color:var(--text-3);text-transform:uppercase;letter-spacing:0.06em;padding:6px 10px 8px;">Espace de travail</div>
      ${items}
      <div style="height:1px;background:var(--border);margin:4px 0;"></div>
      <button id="_wsAdd" style="width:100%;text-align:left;display:flex;align-items:center;gap:10px;padding:8px 10px;border:0;background:transparent;color:var(--text);font-family:inherit;font-size:13px;cursor:pointer;border-radius:6px;">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M8 3v10M3 8h10"/></svg>
        <span>Nouvel espace</span>
      </button>
      ${cur && cur.id !== '${_WS_PRINCIPAL}'.replace(/'/g,'') ? `<button id="_wsRename" style="width:100%;text-align:left;display:flex;align-items:center;gap:10px;padding:8px 10px;border:0;background:transparent;color:var(--text-2);font-family:inherit;font-size:12px;cursor:pointer;border-radius:6px;">Renommer "${cur.name}"</button>` : ''}
      ${cur && cur.id !== '00000000-0000-0000-0000-000000000001' ? `<button id="_wsDel" style="width:100%;text-align:left;display:flex;align-items:center;gap:10px;padding:8px 10px;border:0;background:transparent;color:var(--negative);font-family:inherit;font-size:12px;cursor:pointer;border-radius:6px;">Supprimer "${cur.name}"</button>` : ''}
    </div>
  `;
}

function togglePopup() {
  const pop = document.getElementById('_wsPop');
  if (pop.style.display === 'block') { hidePopup(); return; }
  pop.innerHTML = buildPopupContent();
  pop.style.display = 'block';
  // Positionne sous le bouton
  const btn = document.getElementById('_wsBtn');
  const r = btn.getBoundingClientRect();
  pop.style.top = (r.bottom + 6) + 'px';
  pop.style.left = r.left + 'px';
  pop.querySelectorAll('[data-ws]').forEach(b => b.onclick = () => {
    localStorage.setItem(_WS_KEY, b.dataset.ws);
    location.reload();
  });
  const addBtn = document.getElementById('_wsAdd');
  if (addBtn) addBtn.onclick = () => { hidePopup(); openWsModal(null); };
  const renameBtn = document.getElementById('_wsRename');
  if (renameBtn) renameBtn.onclick = () => { hidePopup(); openWsModal(window.SOPHIE_WS.current); };
  const delBtn = document.getElementById('_wsDel');
  if (delBtn) delBtn.onclick = async () => {
    const cur = window.SOPHIE_WS.current;
    if (!confirm(`Supprimer "${cur.name}" ?\n⚠️ Toutes ses données seront SUPPRIMÉES.`)) return;
    if (!confirm(`Confirme encore une fois.`)) return;
    await window.SOPHIE_WS._sb.from('workspaces').delete().eq('id', cur.id);
    localStorage.setItem(_WS_KEY, '00000000-0000-0000-0000-000000000001');
    location.reload();
  };
}
function hidePopup() { const p = document.getElementById('_wsPop'); if (p) p.style.display = 'none'; }

/* ============ MODAL CREATION/RENAME ============ */
const _EMOJIS = ['🏠','⚡','🔥','🌟','💎','🎯','🚀','💼','📌','🎨','🌸','🦋','🌈','💫','🌙','☀️','🔮','💰','🎪','🌺','🍀','🎭','🏆','💡','🛒','📱','💻','🎬','📚','🌍'];

function openWsModal(existing) {
  closeWsModal();
  const isEdit = !!existing;
  const initialEmoji = existing?.emoji || '⚡';
  const initialName = existing?.name || '';
  const overlay = document.createElement('div');
  overlay.id = '_wsModalBg';
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center;z-index:100000;';
  overlay.innerHTML = `
    <div style="background:var(--card);border:1px solid var(--border);border-radius:12px;padding:24px;width:100%;max-width:420px;box-shadow:0 10px 25px rgba(0,0,0,0.2);font-family:Geist,sans-serif;color:var(--text);">
      <h3 style="margin:0 0 6px;font-size:18px;font-weight:600;letter-spacing:-0.01em;">${isEdit?'Renommer l\'espace':'Nouvel espace'}</h3>
      <div style="color:var(--text-3);font-size:12px;margin-bottom:20px;">${isEdit?'Modifie le nom et l\'emoji.':'Choisis un nom et un emoji.'}</div>
      <div style="display:flex;flex-direction:column;gap:14px;">
        <div>
          <label style="display:block;font-size:11px;font-weight:500;color:var(--text-3);text-transform:uppercase;letter-spacing:0.04em;margin-bottom:6px;">Nom</label>
          <input id="_wsModalName" placeholder="Spam, Long terme..." value="${initialName}" style="width:100%;font-family:inherit;font-size:14px;padding:9px 11px;border-radius:7px;border:1px solid var(--border);background:var(--card);color:var(--text);outline:none;box-sizing:border-box;" />
        </div>
        <div>
          <label style="display:block;font-size:11px;font-weight:500;color:var(--text-3);text-transform:uppercase;letter-spacing:0.04em;margin-bottom:8px;">Emoji</label>
          <div id="_wsModalEmojis" style="display:grid;grid-template-columns:repeat(10,1fr);gap:4px;">
            ${_EMOJIS.map(e => `<button data-em="${e}" style="font-family:inherit;border:1px solid ${e===initialEmoji?'var(--text-2)':'transparent'};background:${e===initialEmoji?'var(--accent-soft)':'transparent'};font-size:20px;padding:6px 4px;border-radius:6px;cursor:pointer;line-height:1;">${e}</button>`).join('')}
          </div>
        </div>
      </div>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:20px;padding-top:16px;border-top:1px solid var(--border);">
        <button id="_wsModalCancel" style="font-family:inherit;font-size:12px;font-weight:500;padding:7px 14px;border-radius:6px;border:1px solid var(--border);background:var(--card);color:var(--text);cursor:pointer;">Annuler</button>
        <button id="_wsModalSave" style="font-family:inherit;font-size:12px;font-weight:500;padding:7px 14px;border-radius:6px;border:1px solid var(--text);background:var(--text);color:var(--bg);cursor:pointer;">${isEdit?'Enregistrer':'Créer'}</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  let selectedEmoji = initialEmoji;
  overlay.querySelectorAll('[data-em]').forEach(b => {
    b.onclick = () => {
      selectedEmoji = b.dataset.em;
      overlay.querySelectorAll('[data-em]').forEach(x => {
        x.style.background = 'transparent'; x.style.border = '1px solid transparent';
      });
      b.style.background = 'var(--accent-soft)';
      b.style.border = '1px solid var(--text-2)';
    };
  });
  overlay.addEventListener('click', e => { if (e.target === overlay) closeWsModal(); });
  document.getElementById('_wsModalCancel').onclick = closeWsModal;
  document.getElementById('_wsModalName').focus();
  document.getElementById('_wsModalName').addEventListener('keydown', e => { if (e.key === 'Enter') document.getElementById('_wsModalSave').click(); });
  document.getElementById('_wsModalSave').onclick = async () => {
    const name = document.getElementById('_wsModalName').value.trim();
    if (!name) { document.getElementById('_wsModalName').focus(); return; }
    const sb = window.SOPHIE_WS._sb;
    if (isEdit) {
      await sb.from('workspaces').update({ name, emoji: selectedEmoji }).eq('id', existing.id);
    } else {
      const { data, error } = await sb.from('workspaces').insert({
        name, emoji: selectedEmoji,
        color: '#'+Math.floor(Math.random()*16777215).toString(16).padStart(6,'0'),
        position: window.SOPHIE_WS.list.length,
      }).select().single();
      if (error) { alert('Erreur: '+error.message); return; }
      localStorage.setItem(_WS_KEY, data.id);
    }
    location.reload();
  };
}
function closeWsModal() { const m = document.getElementById('_wsModalBg'); if (m) m.remove(); }
