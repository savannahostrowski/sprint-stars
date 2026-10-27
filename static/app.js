export const METRICS = {total: 'All activity', closures: 'Closures', prs_opened: 'PRs opened', reviews: 'Reviews'};

export function rankMembers(members, metric, query = '') {
  const sorted = [...members].filter(member => member[metric] > 0)
    .sort((a, b) => b[metric] - a[metric] || a.login.toLowerCase().localeCompare(b.login.toLowerCase()));
  let previous = null, rank = 0;
  const ranked = sorted.map((member, index) => {
    if (member[metric] !== previous) rank = index + 1;
    previous = member[metric];
    return {...member, rank};
  });
  const search = query.toLowerCase().trim();
  return {
    ranked,
    filtered: ranked.filter(member => `${member.name} ${member.login} ${(member.aliases || []).join(' ')}`.toLowerCase().includes(search)),
    totals: Object.fromEntries(Object.keys(METRICS).map(key => [key, members.reduce((sum, m) => sum + m[key], 0)])),
  };
}

export function groupActivities(items, metric) {
  const events = items.filter(item => metric === 'total' ||
    item.activity === ({closures: 'closure', prs_opened: 'pr_opened', reviews: 'review'})[metric]);
  const grouped = new Map();
  for (const event of events) {
    const key = `${(event.repository || "").toLowerCase()}#${event.number}`;
    if (!grouped.has(key)) grouped.set(key, {...event, actions: []});
    grouped.get(key).actions.push(event);
  }
  const groups = [...grouped.values()].map(item => ({...item,
    actions: [...item.actions].sort((a, b) => new Date(a.occurred_at) - new Date(b.occurred_at)),
  })).sort((a, b) => new Date(b.actions.at(-1).occurred_at) - new Date(a.actions.at(-1).occurred_at));
  return {events, groups};
}

export function activityLabel(action) {
  if (action.activity === 'review') return `Reviewed (${{APPROVED: 'approved', CHANGES_REQUESTED: 'changes requested', COMMENTED: 'commented', DISMISSED: 'dismissed'}[action.review_state] || 'submitted'})`;
  return action.activity === 'pr_opened' ? 'Opened' : action.merged ? 'Merged' : 'Closed';
}

export function requestCoordinator() {
  let sequence = 0, active = null;
  return {
    begin(background = false) {
      if (background && active && !active.background) return null;
      active?.controller.abort();
      active = {id: ++sequence, background, controller: new AbortController()};
      return active;
    },
    current(request) { return active?.id === request.id; },
    finish(request) { if (active?.id === request.id) active = null; },
  };
}

export function validRange(start, end) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(start) || !/^\d{4}-\d{2}-\d{2}$/.test(end)) return false;
  const from = Date.parse(start + 'T00:00:00Z'), through = Date.parse(end + 'T00:00:00Z');
  return Number.isFinite(from) && Number.isFinite(through) && through >= from &&
    new Date(from).toISOString().slice(0, 10) === start && new Date(through).toISOString().slice(0, 10) === end &&
    (through - from) / 86400000 <= 92;
}

function startApp() {
  const $ = id => document.getElementById(id);
  const config = JSON.parse($('app-config').textContent);
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || config.timezone;
  const iso = date => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  const week = () => {
    const start = new Date();
    start.setDate(start.getDate() - (start.getDay() + 6) % 7);
    const end = new Date(start); end.setDate(end.getDate() + 6);
    return [iso(start), iso(end)];
  };
  const escape = value => String(value).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const dateLabel = value => new Date(value + 'T12:00:00').toLocaleDateString(undefined, {month: 'short', day: 'numeric', year: 'numeric'});
  const actionDate = value => new Date(value).toLocaleDateString(undefined, {month: 'short', day: 'numeric'});
  const requests = requestCoordinator();
  let data = null, metric = 'total', expanded = null, visibleItems = 10;
  let applied = week(), followCurrentWeek = true, draftVersion = 0, refreshPoll = null;
  let retryRange = applied, retryFollow = true;

  function draftNotice() {
    $('draft-note').hidden = $('start').value === applied[0] && $('end').value === applied[1];
  }
  function syncDates() {
    [$('start').value, $('end').value] = applied;
    draftNotice();
  }
  function message(text = '', kind = '') {
    $('message').textContent = text;
    $('message').className = kind;
  }

  function activityMarkup(member) {
    const {events, groups} = groupActivities(member.items, metric);
    const visible = groups.slice(0, visibleItems);
    return `<tr class="expanded" role="row"><td colspan="7" role="cell"><div class="items" id="activity-${escape(member.login)}">
      <p class="activity-count">${events.length} ${events.length === 1 ? 'action' : 'actions'} across ${groups.length} ${groups.length === 1 ? 'item' : 'items'}</p>
      ${visible.map(item => `<div class="item"><span class="kind status-${escape(item.status)}">${escape(item.status.toUpperCase())}</span>
        <div class="item-content"><a href="${escape(item.url)}" target="_blank" rel="noopener noreferrer"><code>${escape(item.repository)}#${item.number}</code> ${escape(item.title)} ↗</a>
          <div class="item-actions">${item.actions.map(action => `${escape(activityLabel(action))} ${actionDate(action.occurred_at)}${action.review_url ? ` <a class="review-link" href="${escape(action.review_url)}" target="_blank" rel="noopener noreferrer" aria-label="View counted review">↗</a>` : ''}${action.actor_login && action.actor_login.toLowerCase() !== member.login.toLowerCase() ? ` as @${escape(action.actor_login)}` : ''}`).join(' · ')}</div></div>
        <span class="item-type">${item.kind === 'pr' ? 'PR' : 'Issue'}</span></div>`).join('')}
      ${groups.length > visible.length ? `<button class="show-more" data-more="${escape(member.login)}">Show next ${Math.min(10, groups.length - visible.length)} items <span>(${visible.length} of ${groups.length} shown)</span></button>` : ''}
    </div></td></tr>`;
  }

  function render() {
    if (!data) return;
    const search = $('search').value.trim();
    const view = rankMembers(data.members, metric, search);
    const max = Math.max(1, ...view.ranked.map(member => member[metric]));
    const focusedLogin = document.activeElement?.dataset?.login;
    $('rows').innerHTML = view.filtered.map(member => {
      const open = expanded === member.login;
      const stats = [['closures', 'Closures', 'issue-count'], ['prs_opened', 'PRs opened', 'pr-count'], ['reviews', 'Reviews', 'review-count'], ['total', 'Total', 'total-count']];
      return `<tr class="contributor-row ${member.rank === 1 ? 'first' : ''}" role="row">
        <td class="rank" role="cell">${member.rank === 1 ? '<span class="rank-star" aria-hidden="true">★</span>' : ''}${String(member.rank).padStart(2, '0')}</td>
        <td class="person-cell" role="cell"><a class="person" href="https://github.com/${encodeURIComponent(member.login)}" target="_blank" rel="noopener noreferrer"><img class="avatar" alt="" width="34" height="34" src="https://github.com/${encodeURIComponent(member.login)}.png?size=80" loading="lazy"><span class="identity"><span class="person-name">${escape(member.name)}</span><small>@${escape(member.login)}</small></span></a></td>
        ${stats.map(([key, label, color]) => `<td class="number ${color} ${key === metric ? 'selected-metric' : ''}" role="cell"><span class="metric-label" aria-hidden="true">${label}</span><span class="metric-value">${key === metric ? `<span class="bar" aria-hidden="true" style="width:${member[key] / max * 100}%"></span>` : ''}<strong>${member[key]}</strong></span></td>`).join('')}
        <td class="expander-cell" role="cell"><button class="expand" data-login="${escape(member.login)}" aria-label="${open ? 'Hide' : 'Show'} activity by ${escape(member.name)}" aria-controls="activity-${escape(member.login)}" aria-expanded="${open}">${open ? '−' : '+'}</button></td>
      </tr>${open ? activityMarkup(member) : ''}`;
    }).join('');
    if (focusedLogin) [...$('rows').querySelectorAll('[data-login]')].find(button => button.dataset.login === focusedLogin)?.focus({preventScroll: true});
    $('empty').hidden = view.filtered.length > 0;
    $('empty').textContent = search ? 'No contributors match that search.' : 'No activity in this view yet.';
    $('row-count').textContent = `${view.filtered.length} of ${view.ranked.length} contributors${search ? ' · search active' : ''}`;
    for (const [id, key] of [['total', 'total'], ['closure-total', 'closures'], ['opened-total', 'prs_opened'], ['review-total', 'reviews']]) $(id).textContent = view.totals[key].toLocaleString();
    const rangeLabel = `${dateLabel(data.start)} — ${dateLabel(data.end)}`;
    $('range-label').textContent = rangeLabel;
    $('board-subtitle').textContent = `${METRICS[metric]} · ${config.scope_label} · ${rangeLabel}`;
    $('rows').dataset.range = $('total').closest('section').dataset.range = `${data.start}/${data.end}`;
    $('filter-note').hidden = $('clear-search').hidden = !search;
    $('updated').textContent = `${data.stale ? 'Stale · last success' : 'Updated'} ${new Date(data.updated_at).toLocaleString(undefined, {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'})}`;
    $('coverage').textContent = `Coverage: ${data.repository_count} configured public repositories. Roster: ${data.roster_size} members. ${data.excluded} actions by bots, other accounts, or missing actors excluded from this range.`;
    document.querySelectorAll('thead th').forEach((th, index) => {
      const active = ({closures: 2, prs_opened: 3, reviews: 4, total: 5})[metric] === index;
      th.setAttribute('aria-sort', active ? 'descending' : 'none');
      th.querySelector('.sort-arrow')?.remove();
      if (active) {
        const arrow = document.createElement('span'); arrow.className = 'sort-arrow'; arrow.textContent = ' ↓'; th.append(arrow);
      }
    });
  }

  async function load(range = applied, follow = followCurrentWeek, background = false) {
    const request = requests.begin(background);
    if (!request) return;
    clearTimeout(refreshPoll);
    const draftAtRequest = draftVersion;
    const hadDraft = $('start').value !== applied[0] || $('end').value !== applied[1];
    if (!background) {
      retryRange = [...range]; retryFollow = follow;
      $('apply').setAttribute('aria-busy', 'true');
    }
    $('retry').hidden = true;
    message(data ? `Updating ${dateLabel(range[0])} — ${dateLabel(range[1])}…` : 'Loading GitHub activity…');
    try {
      const response = await fetch('/api/leaderboard?' + new URLSearchParams({start: range[0], end: range[1], tz: zone}), {signal: request.controller.signal, cache: 'no-store'});
      const result = await response.json();
      if (!requests.current(request)) return;
      if (response.ok && (result.schema_version !== 7 || result.config_id !== config.config_id)) throw new Error('The server is updating. Please retry in a moment.');
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Choose a valid date range.');
      if (result.start !== range[0] || result.end !== range[1]) throw new Error('The server returned a different date range. Please retry.');
      const changedRange = applied[0] !== range[0] || applied[1] !== range[1];
      applied = [...range]; followCurrentWeek = follow; data = result;
      if (changedRange) { expanded = null; visibleItems = 10; }
      if (draftVersion === draftAtRequest && (!background || !hadDraft)) syncDates();
      render(); draftNotice();
      message(result.warning || (result.stale ? 'This data is older than the refresh interval. An automatic refresh is pending.' : ''), result.stale ? 'warning' : '');
      if (result.refreshing) refreshPoll = setTimeout(() => load(applied, followCurrentWeek, true), 1500);
      const params = new URLSearchParams();
      if (!followCurrentWeek) { params.set('start', applied[0]); params.set('end', applied[1]); }
      history.replaceState(null, '', location.pathname + (params.size ? '?' + params : ''));
    } catch (error) {
      if (!requests.current(request) || error.name === 'AbortError') return;
      message(`${error.message}${data ? ` Showing ${dateLabel(data.start)} — ${dateLabel(data.end)}.` : ''}`, 'error');
      $('retry').hidden = false;
    } finally {
      if (requests.current(request)) {
        requests.finish(request);
        $('apply').removeAttribute('aria-busy');
      }
    }
  }

  $('range').addEventListener('submit', event => {
    event.preventDefault();
    const range = [$('start').value, $('end').value];
    if (!validRange(...range)) {
      $('retry').hidden = true;
      message('Choose 1–93 days, with Through on or after From.', 'error');
      $('end').focus(); return;
    }
    load(range, false);
  });
  for (const id of ['start', 'end']) $(id).addEventListener('input', () => { draftVersion++; draftNotice(); });
  $('retry').addEventListener('click', () => load(retryRange, retryFollow));
  document.querySelectorAll('[data-metric]').forEach(button => button.addEventListener('click', () => {
    metric = button.dataset.metric; expanded = null; visibleItems = 10;
    document.querySelectorAll('[data-metric]').forEach(other => {
      const active = other === button; other.classList.toggle('active', active); other.setAttribute('aria-pressed', active);
    });
    render();
  }));
  $('search').addEventListener('input', render);
  $('clear-search').addEventListener('click', () => { $('search').value = ''; render(); $('search').focus(); });
  $('rows').addEventListener('click', event => {
    const more = event.target.closest('[data-more]');
    const button = event.target.closest('[data-login]');
    if (more) {
      const previousLimit = visibleItems;
      visibleItems += 10; render();
      $('rows').querySelectorAll('.item a')[previousLimit]?.focus({preventScroll: true});
    } else if (button) {
      expanded = expanded === button.dataset.login ? null : button.dataset.login; visibleItems = 10; render();
      [...$('rows').querySelectorAll('[data-login]')].find(b => b.dataset.login === button.dataset.login)?.focus({preventScroll: true});
    }
  });
  function setTheme(theme) {
    document.documentElement.dataset.theme = theme;
    $('theme').setAttribute('aria-label', `Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`);
    $('theme').setAttribute('aria-pressed', theme === 'dark');
    $('theme').textContent = theme === 'dark' ? '☾' : '☼';
    document.querySelector('meta[name="theme-color"]').content = theme === 'dark' ? '#192c2a' : '#eaf0ee';
  }
  try { const theme = localStorage.getItem('theme'); if (['dark', 'light'].includes(theme)) setTheme(theme); } catch {}
  $('theme').addEventListener('click', () => {
    const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'; setTheme(theme);
    try { localStorage.setItem('theme', theme); } catch {}
  });
  const params = new URLSearchParams(location.search);
  if (validRange(params.get('start'), params.get('end'))) {
    applied = [params.get('start'), params.get('end')]; followCurrentWeek = false;
  }
  syncDates(); load();
  setInterval(() => { if (!document.hidden) load(followCurrentWeek ? week() : applied, followCurrentWeek, true); }, config.refresh_seconds * 1000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(followCurrentWeek ? week() : applied, followCurrentWeek, true); });
}

if (typeof document !== 'undefined') startApp();
