// Chirp UI helpers — strict CSP-safe, file-only.
(function () {
  'use strict';

  function csrfToken() {
    const m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute('content') : '';
  }

  // Live character counter for compose.
  function bindCounter(textarea) {
    if (!textarea || textarea.dataset.bound === '1') return;
    textarea.dataset.bound = '1';
    const counterId = textarea.dataset.counter;
    const counter = counterId ? document.getElementById(counterId) : null;
    const max = parseInt(textarea.dataset.max || '500', 10);
    function update() {
      const len = textarea.value.length;
      const remaining = max - len;
      if (counter) {
        counter.textContent = String(remaining);
        counter.classList.toggle('over', remaining < 0);
      }
      const submit = textarea.form ? textarea.form.querySelector('button[type=submit]') : null;
      if (submit) submit.disabled = len === 0 || remaining < 0;
    }
    textarea.addEventListener('input', update);
    update();
  }

  function bindAll() {
    document.querySelectorAll('textarea[data-counter]').forEach(bindCounter);
  }

  document.addEventListener('DOMContentLoaded', bindAll);
  document.addEventListener('htmx:afterSwap', bindAll);

  // Add CSRF token as a header on htmx requests
  document.addEventListener('htmx:configRequest', function (evt) {
    const tok = csrfToken();
    if (tok) evt.detail.headers['X-CSRF-Token'] = tok;
  });

  // Optimistic action toggling for fav/boost/bookmark/follow.
  function bumpCounter(el, delta) {
    if (!el) return;
    const n = parseInt(el.textContent.replace(/[^0-9]/g, '') || '0', 10);
    el.textContent = String(Math.max(0, n + delta));
  }

  document.addEventListener('click', function (evt) {
    const btn = evt.target.closest('button[data-action]');
    if (!btn) return;
    const action = btn.dataset.action;
    const id = btn.dataset.id;
    if (!action || !id) return;

    if (action === 'delete-status') {
      if (!confirm('Delete this post?')) {
        evt.preventDefault();
        return;
      }
    }

    const optimistic = btn.dataset.optimistic === '1';
    if (!optimistic) return;
    evt.preventDefault();

    let url = '';
    let prevActive = btn.classList.contains('active');
    let counter = btn.querySelector('.count');
    let delta = prevActive ? -1 : 1;

    if (action === 'favourite') {
      url = '/web/statuses/' + id + (prevActive ? '/unfavourite' : '/favourite');
    } else if (action === 'reblog') {
      url = '/web/statuses/' + id + (prevActive ? '/unreblog' : '/reblog');
    } else if (action === 'bookmark') {
      url = '/web/statuses/' + id + (prevActive ? '/unbookmark' : '/bookmark');
    } else if (action === 'follow') {
      url = '/web/accounts/' + id + (prevActive ? '/unfollow' : '/follow');
    } else {
      return;
    }

    btn.classList.toggle('active');
    bumpCounter(counter, delta);

    fetch(url, {
      method: 'POST',
      headers: {
        'X-CSRF-Token': csrfToken(),
        'Accept': 'application/json',
      },
      credentials: 'same-origin',
    }).then(function (r) {
      if (!r.ok) {
        // revert
        btn.classList.toggle('active');
        bumpCounter(counter, -delta);
      }
      return r.json().catch(function () { return {}; });
    }).then(function (data) {
      if (data && typeof data.favourites_count === 'number' && action === 'favourite' && counter) {
        counter.textContent = String(data.favourites_count);
      }
      if (data && typeof data.reblogs_count === 'number' && action === 'reblog' && counter) {
        counter.textContent = String(data.reblogs_count);
      }
    }).catch(function () {
      btn.classList.toggle('active');
      bumpCounter(counter, -delta);
    });
  });

  // SSE connection for notifications badge.
  function startSSE() {
    if (!window.EventSource) return;
    const meta = document.querySelector('meta[name="user-id"]');
    if (!meta || !meta.getAttribute('content')) return;
    const es = new EventSource('/web/sse');
    es.addEventListener('notification', function () {
      const badge = document.getElementById('nav-notif-badge');
      if (badge) {
        const n = parseInt(badge.textContent || '0', 10) + 1;
        badge.textContent = String(n);
        badge.style.display = 'inline-block';
      }
    });
    es.addEventListener('update', function () {
      // ping; clients can refresh timeline tab manually
    });
  }
  document.addEventListener('DOMContentLoaded', startSSE);
})();
