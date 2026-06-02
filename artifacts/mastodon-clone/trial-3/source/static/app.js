// Chirp client-side glue (no inline scripts; CSP safe)
(function(){
  function getCookie(n){
    var p = document.cookie.split('; ').filter(function(x){return x.indexOf(n+'=')===0});
    return p.length? decodeURIComponent(p[0].slice(n.length+1)): '';
  }
  function postJSON(url, body){
    var h = {'Content-Type':'application/json','Accept':'application/json'};
    return fetch(url, {method:'POST', headers:h, credentials:'same-origin', body: body? JSON.stringify(body): undefined});
  }
  function deleteReq(url){
    return fetch(url, {method:'DELETE', credentials:'same-origin', headers:{'Accept':'application/json'}});
  }
  // Compose counter
  document.addEventListener('input', function(e){
    var t = e.target;
    if(t && t.matches && t.matches('textarea[name="status"]')){
      var form = t.closest('form');
      var counter = form && form.querySelector('[data-counter]');
      if(counter){
        var max = parseInt(t.getAttribute('maxlength')||'500',10);
        var left = max - t.value.length;
        counter.textContent = left;
        counter.classList.toggle('over', left < 0);
      }
    }
  });
  // Action buttons
  document.addEventListener('click', function(e){
    var btn = e.target.closest && e.target.closest('button');
    if(!btn) return;
    var sid = btn.getAttribute('data-id');
    function bumpCount(sel, delta){
      var card = btn.closest('.status-card');
      var node = card && card.querySelector(sel);
      if(node){ node.textContent = (parseInt(node.textContent||'0',10) + delta); }
    }
    function toggle(active, sel, deltaOn, urlOn, urlOff){
      var pressed = btn.getAttribute('aria-pressed') === 'true';
      var url = pressed ? urlOff : urlOn;
      bumpCount(sel, pressed ? -deltaOn : deltaOn);
      btn.setAttribute('aria-pressed', pressed ? 'false':'true');
      postJSON(url, null).catch(function(){});
    }
    if(btn.hasAttribute('data-fav')){
      e.preventDefault();
      toggle(true, '[data-favs]', 1, '/api/v1/statuses/'+sid+'/favourite', '/api/v1/statuses/'+sid+'/unfavourite');
    } else if(btn.hasAttribute('data-boost')){
      e.preventDefault();
      toggle(true, '[data-reblogs]', 1, '/api/v1/statuses/'+sid+'/reblog', '/api/v1/statuses/'+sid+'/unreblog');
    } else if(btn.hasAttribute('data-bookmark')){
      e.preventDefault();
      var pressed = btn.getAttribute('aria-pressed')==='true';
      btn.setAttribute('aria-pressed', pressed?'false':'true');
      postJSON('/api/v1/statuses/'+sid+'/'+(pressed?'unbookmark':'bookmark'), null).catch(function(){});
    } else if(btn.hasAttribute('data-delete')){
      e.preventDefault();
      if(!confirm('Delete this post?')) return;
      deleteReq('/api/v1/statuses/'+sid).then(function(r){
        if(r.ok){ var c = btn.closest('.status-card'); if(c) c.remove(); }
      });
    } else if(btn.hasAttribute('data-follow')){
      e.preventDefault();
      var aid = sid;
      var label = btn.textContent.trim();
      if(label === 'Follow' || label === 'Follow back'){
        postJSON('/api/v1/accounts/'+aid+'/follow', null).then(function(){ btn.textContent='Unfollow'; });
      } else {
        postJSON('/api/v1/accounts/'+aid+'/unfollow', null).then(function(){ btn.textContent='Follow'; });
      }
    } else if(btn.hasAttribute('data-mute')){
      e.preventDefault();
      postJSON('/api/v1/accounts/'+sid+'/mute', null);
    } else if(btn.hasAttribute('data-block')){
      e.preventDefault();
      postJSON('/api/v1/accounts/'+sid+'/block', null);
    }
  });
  // Notification badge bump from SSE (htmx-ext-sse + listener)
  document.body.addEventListener('htmx:sseMessage', function(e){
    if(e.detail && e.detail.type === 'notification'){
      var b = document.querySelector('[data-unread]');
      if(b){ b.hidden = false; b.textContent = (parseInt(b.textContent||'0',10)+1); }
    }
  });
})();
