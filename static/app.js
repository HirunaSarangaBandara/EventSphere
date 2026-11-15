/* EventSphere mobile web client. All decisions and permissions are enforced by the FastAPI API. */
(() => {
  'use strict';
  const $ = (s, root=document) => root.querySelector(s);
  const $$ = (s, root=document) => [...root.querySelectorAll(s)];
  const state = { token: localStorage.getItem('es_token') || '', user: null, authMode: 'login', events: [], venues: [], scanner: null, scanActive: false, toastTimer: null };
  const money = v => `LKR ${Number(v || 0).toLocaleString('en-LK', {minimumFractionDigits:2, maximumFractionDigits:2})}`;
  const safe = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const parseDate = v => { if (!v) return null; const raw = String(v); return new Date(/[zZ]|[+-]\d{2}:?\d{2}$/.test(raw) ? raw : `${raw}Z`); };
  const date = v => v ? parseDate(v).toLocaleString([], {dateStyle:'medium', timeStyle:'short'}) : '—';
  const dtLocal = v => { const d = parseDate(v); d.setMinutes(d.getMinutes() - d.getTimezoneOffset()); return d.toISOString().slice(0,16); };
  const csrfFromCookie = () => document.cookie.split('; ').find(x => x.startsWith('csrf_token='))?.split('=').slice(1).join('=') || '';

  async function api(path, options={}) {
    const method = (options.method || 'GET').toUpperCase();
    const headers = new Headers(options.headers || {});
    if (state.token) headers.set('Authorization', `Bearer ${state.token}`);
    if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
    if (['POST','PUT','PATCH','DELETE'].includes(method)) {
      const csrf = csrfFromCookie();
      if (csrf) headers.set('X-CSRF-Token', csrf);
    }
    const response = await fetch(path, {...options, method, headers, credentials:'same-origin'});
    const type = response.headers.get('content-type') || '';
    const body = type.includes('application/json') ? await response.json() : await response.text();
    if (!response.ok) {
      const message = body && typeof body === 'object' ? (body.detail || body.message || `Request failed (${response.status})`) : `Request failed (${response.status})`;
      if (response.status === 401 && state.token) signOut(false);
      throw new Error(message);
    }
    return body;
  }
  async function refreshCsrf() { try { await fetch('/api/auth/csrf', {credentials:'same-origin'}); } catch (_) {} }
  function toast(message, isError=false) {
    const node = $('#toast'); node.textContent = message; node.style.background = isError ? '#9f3735' : 'var(--navy)'; node.classList.add('show');
    clearTimeout(state.toastTimer); state.toastTimer = setTimeout(() => node.classList.remove('show'), 3500);
  }
  function fail(error) { toast(error.message || String(error), true); }
  function setButtonBusy(button, busy, text='Please wait…') { if (!button) return; if (busy) { button.dataset.oldText = button.textContent; button.disabled = true; button.textContent = text; } else { button.disabled = false; if (button.dataset.oldText) button.textContent = button.dataset.oldText; } }

  function showView(name) {
    const mapping = {events:'eventsView', tickets:'ticketsView', hire:'hireView', waitlist:'waitlistView', notifications:'notificationsView', scanner:'scannerView', admin:'adminView'};
    const id = mapping[name] || 'eventsView';
    $$('.view').forEach(v => v.classList.toggle('active-view', v.id === id));
    $$('.nav-link').forEach(a => a.classList.toggle('active', a.dataset.view === name));
    $('#mainNav').classList.remove('open');
    if (name === 'tickets') loadBookings();
    if (name === 'hire') { loadVenues(); loadMyHire(); }
    if (name === 'waitlist') loadWaitlist();
    if (name === 'notifications') loadNotifications();
    if (name === 'scanner') loadScannerEvents();
    if (name === 'admin') loadAdminOverview();
    window.scrollTo({top:0, behavior:'smooth'});
  }
  function updateAuthUI() {
    const logged = Boolean(state.user);
    $('#loginBtn').classList.toggle('hidden', logged); $('#registerBtn').classList.toggle('hidden', logged); $('#logoutBtn').classList.toggle('hidden', !logged);
    $('#userChip').classList.toggle('hidden', !logged);
    $('#userChip').textContent = logged ? `${state.user.full_name.split(' ')[0]} · ${state.user.role}` : '';
    $$('.signed-in-only').forEach(e => e.classList.toggle('hidden', !logged));
    $$('.attendee-only').forEach(e => e.classList.toggle('hidden', !logged || state.user.role !== 'attendee'));
    $$('.admin-only').forEach(e => e.classList.toggle('hidden', !logged || state.user.role !== 'admin'));
    $$('.staff-only').forEach(e => e.classList.toggle('hidden', !logged || !['staff','admin'].includes(state.user.role)));
    if (!logged) showView('events');
  }
  async function restoreSession() {
    if (!state.token) { updateAuthUI(); return; }
    try { state.user = await api('/api/auth/me'); }
    catch (_) { state.token=''; localStorage.removeItem('es_token'); state.user=null; }
    updateAuthUI();
  }
  function signOut(notify=true) { state.token=''; state.user=null; localStorage.removeItem('es_token'); updateAuthUI(); if (notify) toast('You have signed out.'); }
  function openAuth(mode) {
    state.authMode = mode; $('#authModal').classList.remove('hidden'); setAuthMode(mode); setTimeout(() => $('#authEmail').focus(), 50);
  }
  function closeAuth() { $('#authModal').classList.add('hidden'); $('#authForm').reset(); }
  function setAuthMode(mode) {
    state.authMode = mode; $$('.auth-mode').forEach(b => b.classList.toggle('active', b.dataset.authMode === mode));
    $('#nameField').classList.toggle('hidden', mode !== 'register'); $('#authName').required = mode === 'register';
    $('#authTitle').innerHTML = mode === 'login' ? 'Good to see <em>you.</em>' : 'Join the <em>moment.</em>';
    $('#authSubmit').innerHTML = mode === 'login' ? 'Sign in <span>→</span>' : 'Create account <span>→</span>';
    $('#authPassword').autocomplete = mode === 'login' ? 'current-password' : 'new-password';
  }

  async function loadEvents() {
    const search = $('#eventSearch').value.trim(), category = $('#categoryFilter').value;
    const params = new URLSearchParams(); if (search) params.set('search', search); if (category) params.set('category', category);
    try { state.events = await api(`/api/events?${params}`); renderEvents(); }
    catch (e) { $('#eventGridItems').innerHTML = `<div class="empty-state">Unable to load events: ${safe(e.message)}</div>`; }
  }
  function renderEvents() {
    const grid = $('#eventGridItems');
    if (!state.events.length) { grid.innerHTML = '<div class="empty-state">No published events match your search. Try another category or check again soon.</div>'; return; }
    grid.innerHTML = state.events.map((e, i) => {
      const tone = `tone-${(i%6)+1}`; const start = parseDate(e.start_at);
      const cats = e.ticket_categories.map(c => {
        const available = c.tickets_left > 0;
        return `<div class="category-line"><span><strong>${safe(c.name)}</strong> · ${money(c.price)} <small class="muted">(${c.tickets_left} left)</small></span><span class="category-actions"><button class="mini-btn ${available?'':'wait'}" data-book="${c.id}" data-event="${e.id}" ${available?'':'disabled'}>${available?'Book':'Sold out'}</button>${!available && state.user?.role==='attendee'?`<button class="mini-btn wait" data-wait="${c.id}">Join waitlist</button>`:''}</span></div>`;
      }).join('');
      const visual = e.title.split(/\s+/).slice(0,2).join(' ');
      return `<article class="event-card"><div class="event-visual ${tone}"><span class="visual-word">${safe(visual)}</span><span class="category-stamp">${safe(e.category).toUpperCase()}</span></div><div class="event-info"><div class="event-meta"><span>${safe(e.category)}</span><span>${start.toLocaleDateString([], {month:'short',day:'numeric',year:'numeric'})}</span></div><h3>${safe(e.title)}</h3><div class="event-venue">⌖ ${safe(e.venue_name)} · ${safe(e.venue_location)}</div><p class="event-description">${safe(e.description || 'Join us for this campus event.')}</p><div class="event-categories">${cats}</div></div></article>`;
    }).join('');
  }

  async function bookTicket(categoryId, eventId) {
    if (!state.user) return openAuth('login');
    if (state.user.role !== 'attendee') return toast('Only attendee accounts can book tickets.', true);
    const quantity = Number(prompt('How many tickets would you like to book? (Maximum 6)', '1'));
    if (!Number.isInteger(quantity) || quantity < 1 || quantity > 6) return;
    try { const result = await api('/api/bookings', {method:'POST', body:JSON.stringify({ticket_category_id:Number(categoryId), quantity})}); toast(`Booking confirmed · ${result.reference}`); await loadEvents(); showView('tickets'); }
    catch(e) { fail(e); }
  }
  async function joinWaitlist(categoryId) {
    if (!state.user) return openAuth('login');
    if (state.user.role !== 'attendee') return toast('Only attendees can join a waitlist.', true);
    const quantity = Number(prompt('How many tickets should the agent look for? (Maximum 6)', '1'));
    if (!Number.isInteger(quantity) || quantity < 1 || quantity > 6) return;
    try { const r = await api('/api/waitlist', {method:'POST', body:JSON.stringify({ticket_category_id:Number(categoryId), quantity})}); toast(`Added to waitlist · position ${r.position}`); showView('waitlist'); }
    catch(e) { fail(e); }
  }

  async function loadBookings() {
    if (!state.user || state.user.role !== 'attendee') return;
    try {
      const rows = await api('/api/bookings/mine');
      if (!rows.length) { $('#myBookings').innerHTML = '<div class="empty-state">No bookings yet. Your tickets will appear here after you book an event.</div>'; return; }
      $('#myBookings').innerHTML = rows.map(b => `<article class="stack-card"><div class="stack-main"><div class="event-meta"><span>BOOKING ${safe(b.reference)}</span><span class="pill ${b.status==='confirmed'?'success':'danger'}">${safe(b.status)}</span></div><h3>${safe(b.event_title)}</h3><p class="muted">${safe(b.ticket_category)} · ${b.quantity} ticket(s) · ${money(b.total_amount)}</p><p class="muted">Payment ref: ${safe(b.payment_reference || '—')} · Booked ${date(b.created_at)}</p>${b.refund_amount>0?`<p class="muted">Refund recorded: ${money(b.refund_amount)}</p>`:''}<div class="ticket-row" id="ticketRows-${b.id}">${b.tickets.map(t => `<div class="digital-ticket" data-ticket="${t.id}"><img alt="Ticket QR code" src="/static/qr-placeholder.svg"><div><strong>Digital ticket · ${safe(t.status)}</strong><small>${safe(t.code)}</small><div class="ticket-actions"><button class="mini-btn" data-qr="${t.id}">Load QR code</button></div></div></div>`).join('')}</div></div>${b.status==='confirmed'?`<button class="btn btn-outline" data-cancel-booking="${b.id}">Cancel booking</button>`:''}</article>`).join('');
    } catch(e) { fail(e); }
  }
  async function loadQr(ticketId, button) {
    try {
      const headers = state.token ? {Authorization:`Bearer ${state.token}`} : {};
      const res = await fetch(`/api/tickets/${ticketId}/qr`, {headers}); if(!res.ok) throw new Error('Could not load the ticket QR code');
      const blob = await res.blob(), url = URL.createObjectURL(blob); const wrap = $(`[data-ticket="${ticketId}"]`); const img = $('img', wrap); if(img.dataset.objectUrl) URL.revokeObjectURL(img.dataset.objectUrl); img.src=url; img.dataset.objectUrl=url; if(button) button.remove();
    } catch(e) { fail(e); }
  }
  async function cancelBooking(id) {
    if (!confirm('Cancel this booking? The stated refund policy will be applied.')) return;
    try { const r = await api(`/api/bookings/${id}/cancel`, {method:'POST',body:'{}'}); toast(`Booking cancelled. Refund: ${money(r.refund_amount)}`); await loadBookings(); await loadEvents(); }
    catch(e) { fail(e); }
  }

  async function loadVenues() {
    try {
      state.venues = await api('/api/venues');
      const options = state.venues.map(v => `<option value="${v.id}">${safe(v.name)} · capacity ${v.capacity}</option>`).join('');
      $('#hireVenue').innerHTML = options || '<option value="">No venues available</option>';
      $('#eventVenue').innerHTML = options || '<option value="">Create a venue first</option>';
      $('#venueCards').innerHTML = state.venues.length ? state.venues.map(v => `<article class="venue-card">${v.image_url?`<img class="venue-thumb" src="${safe(v.image_url)}" alt="${safe(v.name)}" onerror="this.outerHTML='<div class=\'venue-thumb\'>⌂</div>'">`:`<div class="venue-thumb">⌂</div>`}<div><h4>${safe(v.name)}</h4><p>${safe(v.location)} · Capacity ${v.capacity}</p><strong>${money(v.hourly_hire_rate)} / hour</strong><p>${v.open_for_hire?'Open for private hire':'Not open for hire'}</p></div></article>`).join('') : '<div class="empty-state">No venues have been added yet.</div>';
    } catch(e) { fail(e); }
  }
  async function loadMyHire() {
    if (!state.user || state.user.role!=='attendee') return;
    try { const rows = await api('/api/venue-hire/mine'); $('#myHireRequests').innerHTML = rows.length ? rows.map(r => `<article class="stack-card"><div><div class="event-meta"><span>REQUEST #${r.id}</span><span class="pill ${r.status==='approved'?'success':r.status==='escalated'?'warning':r.status==='rejected'?'danger':''}">${safe(r.status)}</span></div><h3>${safe(state.venues.find(v=>v.id===r.venue_id)?.name || `Venue #${r.venue_id}`)}</h3><p class="muted">${date(r.start_at)} – ${date(r.end_at)} · ${r.guest_count} guests · ${safe(r.purpose)}</p><p class="muted">${safe(r.reason || '')} ${r.quoted_price!==null?`· Quote: ${money(r.quoted_price)}`:''}</p>${r.alternatives?.length?`<div class="form-hint">Alternatives: ${r.alternatives.map(a=>`${safe(a.venue_name)} (${date(a.start_at)})`).join(' · ')}</div>`:''}</div></article>`).join('') : '<div class="empty-state">You have not submitted a venue hire request yet.</div>'; }
    catch(e) { fail(e); }
  }
  async function submitHire(e) {
    e.preventDefault(); const btn = $('button[type="submit"]', e.currentTarget); setButtonBusy(btn,true);
    try {
      const payload={venue_id:Number($('#hireVenue').value),start_at:new Date($('#hireStart').value).toISOString(),end_at:new Date($('#hireEnd').value).toISOString(),guest_count:Number($('#hireGuests').value),purpose:$('#hirePurpose').value.trim()};
      const r=await api('/api/venue-hire',{method:'POST',body:JSON.stringify(payload)});
      toast(`VenueAgent decision: ${r.status}${r.quoted_price!==null?` · ${money(r.quoted_price)}`:''}`); e.currentTarget.reset(); await loadMyHire();
      if(r.alternatives?.length) alert(`The requested time could not be confirmed. Alternatives:\n${r.alternatives.map(a=>`${a.venue_name}: ${date(a.start_at)} — ${money(a.price)}`).join('\n')}`);
    } catch(err){fail(err);} finally{setButtonBusy(btn,false);}
  }

  function updateCountdowns() {
    $$('[data-expiry]').forEach(node => {
      const remaining = Math.max(0, Number(node.dataset.expiry) - Date.now());
      const minutes = Math.floor(remaining / 60000), seconds = Math.floor((remaining % 60000) / 1000);
      node.textContent = remaining > 0 ? `${String(minutes).padStart(2,'0')}:${String(seconds).padStart(2,'0')}` : 'expired — refreshing status';
    });
  }
  setInterval(updateCountdowns, 1000);

  async function loadWaitlist() {
    if (!state.user || state.user.role!=='attendee') return;
    try { const rows=await api('/api/waitlist/mine'); $('#myWaitlist').innerHTML=rows.length?rows.map(r=>{const event=state.events.find(e=>e.id===r.event_id);return `<article class="stack-card"><div><div class="event-meta"><span>WAITLIST ENTRY #${r.id}</span><span class="pill ${r.status==='offered'?'success':r.status==='waiting'?'warning':''}">${safe(r.status)}</span></div><h3>${safe(event?.title || `Event #${r.event_id}`)}</h3><p class="muted">${r.quantity} ticket(s) · ${r.hold_expires_at?`Claim countdown: <strong class="countdown" data-expiry="${parseDate(r.hold_expires_at).getTime()}">calculating…</strong>`:'Waiting for a seat to become available'}</p>${r.status==='offered'?`<button class="btn btn-primary" data-claim-wait="${r.id}">Claim held tickets</button>`:''}</div></article>`}).join(''):'<div class="empty-state">You have no waitlist entries yet. Join a sold-out ticket category from the event list.</div>'; updateCountdowns(); }
    catch(e){fail(e);}
  }
  async function claimWaitlist(id) { try { const r=await api('/api/waitlist/claim',{method:'POST',body:JSON.stringify({entry_id:Number(id)})}); toast('Waitlist offer claimed. Your booking is confirmed.'); await loadEvents(); showView('tickets'); }
    catch(e){fail(e);} }
  async function loadNotifications() {
    try { const rows=await api('/api/notifications'); const unread=rows.filter(r=>!r.is_read).length; $('#notificationDot').classList.toggle('hidden',!unread); $('#notificationsList').innerHTML=rows.length?rows.map(n=>`<article class="stack-card"><div><div class="event-meta"><span>${date(n.created_at)}</span><span class="pill ${n.is_read?'':'warning'}">${n.is_read?'Read':'New'}</span></div><h3>${safe(n.title)}</h3><p class="muted">${safe(n.message)}</p>${!n.is_read?`<button class="mini-btn" data-read-notice="${n.id}">Mark as read</button>`:''}</div></article>`).join(''):'<div class="empty-state">No notifications yet.</div>'; }
    catch(e){fail(e);}
  }

  function switchAdminTab(tab) {
    $$('.admin-tab').forEach(b=>b.classList.toggle('active',b.dataset.adminTab===tab));
    $$('.admin-pane').forEach(p=>p.classList.toggle('active',p.id===`admin${tab[0].toUpperCase()+tab.slice(1)}`));
    if(tab==='overview')loadAdminOverview(); if(tab==='events')loadAdminEvents(); if(tab==='venues')loadAdminVenues(); if(tab==='hires')loadAdminHires(); if(tab==='agents')loadAgentActivity();
  }
  async function loadAdminOverview() {
    if(state.user?.role!=='admin')return;
    try { const d=await api('/api/admin/dashboard'); const total=d.events.reduce((s,e)=>s+e.tickets_sold,0), rev=d.events.reduce((s,e)=>s+e.simulated_revenue,0), check=d.events.reduce((s,e)=>s+e.check_ins,0);
      $('#adminStats').innerHTML=[['Events',d.events.length],['Tickets sold',total],['Simulated revenue',money(rev)],['Check-ins',check]].map(([a,b])=>`<div class="stat-card"><span>${a}</span><strong>${b}</strong></div>`).join('');
      $('#adminEventStats').innerHTML=d.events.length?`<table class="data-table"><thead><tr><th>Event</th><th>Status</th><th>Tickets sold</th><th>Held</th><th>Capacity</th><th>Revenue</th><th>Check-ins</th></tr></thead><tbody>${d.events.map(e=>`<tr><td>${safe(e.title)}</td><td>${safe(e.status)}</td><td>${e.tickets_sold}</td><td>${e.tickets_held}</td><td>${e.capacity}</td><td>${money(e.simulated_revenue)}</td><td>${e.check_ins}</td></tr>`).join('')}</tbody></table>`:'<div class="empty-state">No events created yet.</div>';
    } catch(e){fail(e);}
  }
  async function loadAdminEvents() {
    try { const rows=await api('/api/admin/events'); $('#adminEventList').innerHTML=rows.length?rows.map(e=>`<article class="admin-list-item"><div class="event-meta"><span>${safe(e.category)}</span><span class="pill ${e.status==='published'?'success':e.status==='cancelled'?'danger':'warning'}">${safe(e.status)}</span></div><h4>${safe(e.title)}</h4><p>${safe(e.venue_name)} · ${date(e.start_at)} · ${e.ticket_categories.reduce((s,c)=>s+c.quantity_sold,0)} sold</p><div class="list-actions"><button class="mini-btn" data-edit-event="${e.id}">Edit</button>${e.status==='draft'?`<button class="mini-btn wait" data-publish-event="${e.id}">Publish</button>`:''}${e.status==='published'?`<button class="mini-btn wait" data-cancel-event="${e.id}">Cancel event</button>`:''}</div></article>`).join(''):'<div class="empty-state">No events created. Use the form to add the first one.</div>'; }
    catch(e){fail(e);}
  }
  async function loadAdminVenues() {
    await loadVenues(); $('#adminVenueList').innerHTML=state.venues.length?state.venues.map(v=>`<article class="admin-list-item"><h4>${safe(v.name)}</h4><p>${safe(v.location)} · Capacity ${v.capacity} · ${money(v.hourly_hire_rate)}/hour</p><p>${v.open_for_hire?'Open for private hire':'Private hire disabled'}</p><div class="list-actions"><button class="mini-btn" data-edit-venue="${v.id}">Edit venue</button></div></article>`).join(''):'<div class="empty-state">No venues added.</div>';
  }
  async function loadAdminHires() {
    try { const rows=await api('/api/admin/venue-hire-requests?status=escalated'); $('#adminHireList').innerHTML=rows.length?rows.map(r=>`<article class="stack-card"><div><div class="event-meta"><span>REQUEST #${r.id}</span><span class="pill warning">Escalated</span></div><h3>${safe(state.venues.find(v=>v.id===r.venue_id)?.name||`Venue #${r.venue_id}`)}</h3><p class="muted">${date(r.start_at)} – ${date(r.end_at)} · ${r.guest_count} guests · ${safe(r.purpose)}</p><p class="muted">${safe(r.reason)}</p></div><div class="list-actions"><button class="mini-btn" data-hire-decision="approve" data-id="${r.id}">Approve</button><button class="mini-btn wait" data-hire-decision="reject" data-id="${r.id}">Reject</button></div></article>`).join(''):'<div class="empty-state">No escalated requests need an organiser decision.</div>'; }
    catch(e){fail(e);}
  }
  async function loadAgentActivity() {
    try { const [states,msgs]=await Promise.all([api('/api/admin/agent-state'),api('/api/admin/agent-messages?limit=100')]);
      $('#agentStates').innerHTML=states.length?states.map(s=>`<article class="agent-state"><strong>${safe(s.agent)}</strong><small>${s.beliefs.messages_handled||0} messages handled<br>Last action: ${safe(s.beliefs.last_action||'—')}<br>Updated: ${date(s.updated_at)}</small></article>`).join(''):'<div class="empty-state">Agent state will appear after an agent handles a message.</div>';
      $('#agentMessages').innerHTML=msgs.length?msgs.map(m=>`<article class="message-item"><div class="message-meta"><strong>${safe(m.performative)}</strong><br>${safe(m.sender)} → ${safe(m.receiver)}<br>${date(m.created_at)}<br><small>${safe(m.conversation_id)}</small></div><div><pre>${safe(JSON.stringify(m.content,null,2))}</pre></div></article>`).join(''):'<div class="empty-state">No agent messages logged yet.</div>';
    }catch(e){fail(e);}
  }
  async function saveEvent(e) {
    e.preventDefault(); const btn=$('button[type="submit"]',e.currentTarget); setButtonBusy(btn,true);
    try {
      const editId=$('#eventEditId').value;
      const payload={title:$('#eventTitle').value.trim(),category:$('#eventCategory').value,venue_id:Number($('#eventVenue').value),start_at:new Date($('#eventStart').value).toISOString(),end_at:new Date($('#eventEnd').value).toISOString(),description:$('#eventDescription').value.trim(),status:$('#eventStatus').value};
      let result;
      if(editId) result=await api(`/api/admin/events/${editId}`,{method:'PATCH',body:JSON.stringify(payload)});
      else { payload.ticket_categories=[{name:$('#ticketTypeName').value.trim(),price:Number($('#ticketTypePrice').value),quantity:Number($('#ticketTypeQty').value)}]; result=await api('/api/admin/events',{method:'POST',body:JSON.stringify(payload)}); }
      toast(editId?'Event updated.':`Event created · ${result.title}`); resetEventForm(); await loadAdminEvents(); await loadAdminOverview(); await loadEvents();
    }catch(err){fail(err);}finally{setButtonBusy(btn,false);}
  }
  function resetEventForm(){ $('#eventForm').reset(); $('#eventEditId').value=''; $('#eventFormTitle').textContent='Create an event'; $('#ticketTypeName').value='Standard'; $('#ticketTypePrice').value='500'; $('#ticketTypeQty').value='100'; $('#editCategoryNote').classList.add('hidden'); $('#ticketTypeName').disabled=false; $('#ticketTypePrice').disabled=false; $('#ticketTypeQty').disabled=false; }
  async function editEvent(id) {
    try { const rows=await api('/api/admin/events'); const e=rows.find(x=>x.id===Number(id)); if(!e)return; $('#eventEditId').value=e.id; $('#eventTitle').value=e.title; $('#eventCategory').value=e.category; $('#eventVenue').value=e.venue_id; $('#eventStart').value=dtLocal(e.start_at); $('#eventEnd').value=dtLocal(e.end_at); $('#eventDescription').value=e.description; $('#eventStatus').value=e.status; $('#eventFormTitle').textContent='Edit event'; $('#editCategoryNote').classList.remove('hidden'); $('#ticketTypeName').disabled=true; $('#ticketTypePrice').disabled=true; $('#ticketTypeQty').disabled=true; $('#eventForm').scrollIntoView({behavior:'smooth',block:'start'}); }
    catch(err){fail(err);}
  }
  async function saveVenue(e) {
    e.preventDefault(); const btn=$('button[type="submit"]',e.currentTarget); setButtonBusy(btn,true);
    try { const id=$('#venueEditId').value; const payload={name:$('#venueName').value.trim(),location:$('#venueLocation').value.trim(),capacity:Number($('#venueCapacity').value),hourly_hire_rate:Number($('#venueRate').value),image_url:$('#venueImage').value.trim()||null,description:$('#venueDescription').value.trim(),open_for_hire:$('#venueOpenHire').checked};
      await api(id?`/api/admin/venues/${id}`:'/api/admin/venues',{method:id?'PATCH':'POST',body:JSON.stringify(payload)}); toast(id?'Venue updated.':'Venue created.'); resetVenueForm(); await loadAdminVenues(); }
    catch(err){fail(err);}finally{setButtonBusy(btn,false);}
  }
  function resetVenueForm(){ $('#venueForm').reset(); $('#venueEditId').value=''; $('#venueOpenHire').checked=true; $('#venueFormTitle').textContent='Add a venue'; }
  function editVenue(id){ const v=state.venues.find(x=>x.id===Number(id));if(!v)return;$('#venueEditId').value=v.id;$('#venueName').value=v.name;$('#venueLocation').value=v.location;$('#venueCapacity').value=v.capacity;$('#venueRate').value=v.hourly_hire_rate;$('#venueImage').value=v.image_url||'';$('#venueDescription').value=v.description||'';$('#venueOpenHire').checked=v.open_for_hire;$('#venueFormTitle').textContent='Edit venue';$('#venueForm').scrollIntoView({behavior:'smooth'}); }
  async function cancelEvent(id){if(!confirm('Cancel this event? All confirmed bookings will be fully refunded and tickets cancelled.'))return;try{const r=await api(`/api/admin/events/${id}/cancel`,{method:'POST',body:'{}'});toast(`Event cancelled. ${r.refund_summary?.bookings_refunded||0} booking(s) refunded.`);await loadAdminEvents();await loadAdminOverview();await loadEvents();}catch(e){fail(e);}}
  async function publishEvent(id){try{await api(`/api/admin/events/${id}`,{method:'PATCH',body:JSON.stringify({status:'published'})});toast('Event published.');await loadAdminEvents();await loadEvents();}catch(e){fail(e);}}
  async function hireDecision(id,decision){const reason=prompt(`Optional reason for ${decision}:`,'Reviewed by organiser')||'';try{await api(`/api/admin/venue-hire-requests/${id}/decision`,{method:'POST',body:JSON.stringify({decision,reason})});toast(`Hire request ${decision}d.`);await loadAdminHires();}catch(e){fail(e);}}

  async function loadScannerEvents() {
    try { const published=await api('/api/events'); $('#scanEvent').innerHTML=published.map(e=>`<option value="${e.id}">${safe(e.title)} · ${date(e.start_at)}</option>`).join('')||'<option value="">No published events</option>'; }
    catch(e){fail(e);}
  }
  async function checkInCode(code) {
    const eventId=Number($('#scanEvent').value); if(!eventId)return toast('Select an event first.',true); if(!code.trim())return toast('Enter a ticket code.',true);
    try { const r=await api('/api/staff/check-in',{method:'POST',body:JSON.stringify({code:code.trim(),event_id:eventId})}); showScanResult(r.reason||'Ticket admitted successfully',true); $('#ticketCode').value=''; }
    catch(e){showScanResult(e.message,false);}
  }
  function showScanResult(message,success){const el=$('#scanResult');el.textContent=(success?'✓ ':'✕ ')+message;el.classList.remove('hidden','error');if(!success)el.classList.add('error');}
  async function stopScanner(){ if(state.scanner){try{await state.scanner.stop();await state.scanner.clear();}catch(_){}} state.scanner=null;state.scanActive=false;$('#reader').style.display='none';$('#scanVideo').classList.remove('active');$('#cameraFrame').classList.remove('scanning');$('#cameraPlaceholder').classList.remove('hidden');$('#startCamera').disabled=false;$('#stopCamera').disabled=true; }
  async function startScanner(){
    $('#scanResult').classList.add('hidden');
    try {
      if(window.Html5Qrcode){
        $('#cameraPlaceholder').classList.add('hidden');$('#reader').style.display='block';$('#cameraFrame').classList.add('scanning');
        state.scanner=new Html5Qrcode('reader'); await state.scanner.start({facingMode:'environment'},{fps:10,qrbox:{width:230,height:230}},decoded=>{ $('#ticketCode').value=decoded; checkInCode(decoded); stopScanner(); },()=>{});
      } else if('BarcodeDetector' in window && navigator.mediaDevices?.getUserMedia) {
        const detector=new BarcodeDetector({formats:['qr_code']});const stream=await navigator.mediaDevices.getUserMedia({video:{facingMode:'environment'}});const video=$('#scanVideo');video.srcObject=stream;await video.play();video.classList.add('active');$('#cameraPlaceholder').classList.add('hidden');$('#cameraFrame').classList.add('scanning');state.scanActive=true;
        const scan=async()=>{if(!state.scanActive)return;try{const codes=await detector.detect(video);if(codes.length){const code=codes[0].rawValue;$('#ticketCode').value=code;state.scanActive=false;stream.getTracks().forEach(t=>t.stop());await checkInCode(code);return;}}catch(_){}requestAnimationFrame(scan);};scan();state.scanner={stop:async()=>{state.scanActive=false;stream.getTracks().forEach(t=>t.stop());}};
      } else throw new Error('This browser does not support the camera QR scanner. Use manual entry, or open this page in a supported mobile browser over HTTPS.');
      $('#startCamera').disabled=true;$('#stopCamera').disabled=false;
    }catch(e){await stopScanner();fail(e);}
  }

  // Navigation and forms
  $('#loginBtn').addEventListener('click',()=>openAuth('login')); $('#registerBtn').addEventListener('click',()=>openAuth('register')); $('#logoutBtn').addEventListener('click',()=>signOut());
  $$('[data-close-modal]').forEach(x=>x.addEventListener('click',closeAuth)); $$('.auth-mode').forEach(x=>x.addEventListener('click',()=>setAuthMode(x.dataset.authMode)));
  $('#authForm').addEventListener('submit',async e=>{e.preventDefault();const payload={email:$('#authEmail').value,password:$('#authPassword').value};if(state.authMode==='register'){payload.full_name=$('#authName').value.trim();}
    try{const r=await api(state.authMode==='login'?'/api/auth/login':'/api/auth/register',{method:'POST',body:JSON.stringify(payload)});state.token=r.access_token;state.user=r.user;localStorage.setItem('es_token',state.token);updateAuthUI();closeAuth();toast(`Welcome${state.user.full_name?`, ${state.user.full_name.split(' ')[0]}`:''}!`);await loadEvents();if(state.user.role==='admin'){await loadVenues();showView('admin');}}catch(err){fail(err);}
  });
  $$('.nav-link').forEach(a=>a.addEventListener('click',e=>{e.preventDefault();showView(a.dataset.view);}));
  $('#mobileMenuBtn').addEventListener('click',()=>$('#mainNav').classList.toggle('open'));
  $('#heroHireBtn').addEventListener('click',()=>state.user?.role==='attendee'?showView('hire'):openAuth('login')); $('#bannerHireBtn').addEventListener('click',()=>state.user?.role==='attendee'?showView('hire'):openAuth('login'));
  let searchTimer;$('#eventSearch').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(loadEvents,250);});$('#categoryFilter').addEventListener('change',loadEvents);$('#clearFilters').addEventListener('click',()=>{$('#eventSearch').value='';$('#categoryFilter').value='';loadEvents();});
  $('#eventGridItems').addEventListener('click',e=>{const b=e.target.closest('[data-book]'),w=e.target.closest('[data-wait]');if(b)bookTicket(b.dataset.book,b.dataset.event);if(w)joinWaitlist(w.dataset.wait);});
  $('#myBookings').addEventListener('click',e=>{const qr=e.target.closest('[data-qr]'),cancel=e.target.closest('[data-cancel-booking]');if(qr)loadQr(qr.dataset.qr,qr);if(cancel)cancelBooking(cancel.dataset.cancelBooking);});
  $('#hireForm').addEventListener('submit',submitHire);$('#eventForm').addEventListener('submit',saveEvent);$('#venueForm').addEventListener('submit',saveVenue);
  $('#resetEventForm').addEventListener('click',resetEventForm);$('#resetVenueForm').addEventListener('click',resetVenueForm);
  $$('.admin-tab').forEach(b=>b.addEventListener('click',()=>switchAdminTab(b.dataset.adminTab)));
  $('#adminEventList').addEventListener('click',e=>{const edit=e.target.closest('[data-edit-event]'),cancel=e.target.closest('[data-cancel-event]'),pub=e.target.closest('[data-publish-event]');if(edit)editEvent(edit.dataset.editEvent);if(cancel)cancelEvent(cancel.dataset.cancelEvent);if(pub)publishEvent(pub.dataset.publishEvent);});
  $('#adminVenueList').addEventListener('click',e=>{const edit=e.target.closest('[data-edit-venue]');if(edit)editVenue(edit.dataset.editVenue);});
  $('#adminHireList').addEventListener('click',e=>{const b=e.target.closest('[data-hire-decision]');if(b)hireDecision(b.dataset.id,b.dataset.hireDecision);});
  $('#myWaitlist').addEventListener('click',e=>{const b=e.target.closest('[data-claim-wait]');if(b)claimWaitlist(b.dataset.claimWait);});
  $('#notificationsList').addEventListener('click',async e=>{const b=e.target.closest('[data-read-notice]');if(b){try{await api(`/api/notifications/${b.dataset.readNotice}/read`,{method:'POST',body:'{}'});await loadNotifications();}catch(err){fail(err);}}});
  $('#manualCheckin').addEventListener('click',()=>checkInCode($('#ticketCode').value));$('#ticketCode').addEventListener('keydown',e=>{if(e.key==='Enter')checkInCode(e.currentTarget.value);});$('#startCamera').addEventListener('click',startScanner);$('#stopCamera').addEventListener('click',stopScanner);
  window.addEventListener('beforeunload',()=>{if(state.scanner)stopScanner();});

  async function init(){ await refreshCsrf(); await restoreSession(); await loadEvents(); if(state.user?.role==='admin'){await loadVenues();} }
  init();
})();
