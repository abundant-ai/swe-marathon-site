(function () {
  function csrf() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }

  function updateCounter(textarea) {
    var selector = textarea.getAttribute("data-counter");
    if (!selector) return;
    var counter = document.querySelector(selector);
    if (!counter) return;
    var limit = parseInt(counter.getAttribute("data-limit") || "500", 10);
    var remaining = limit - textarea.value.length;
    counter.textContent = String(remaining);
    counter.classList.toggle("is-over", remaining < 0);
  }

  function wireCounters(root) {
    root.querySelectorAll("textarea[data-counter]").forEach(function (textarea) {
      updateCounter(textarea);
      textarea.addEventListener("input", function () { updateCounter(textarea); });
    });
  }

  function wireTabs(root) {
    root.querySelectorAll(".tab-button[data-timeline]").forEach(function (button) {
      button.addEventListener("click", function () {
        root.querySelectorAll(".tab-button").forEach(function (b) { b.classList.remove("is-active"); });
        button.classList.add("is-active");
        fetch("/fragments/timeline?type=" + encodeURIComponent(button.getAttribute("data-timeline")), {
          headers: { "Accept": "text/html" },
          credentials: "same-origin"
        }).then(function (resp) {
          if (!resp.ok) throw new Error("timeline");
          return resp.text();
        }).then(function (html) {
          var target = document.getElementById("timeline");
          if (target) {
            target.innerHTML = html;
            wireActions(target);
          }
        }).catch(function () {});
      });
    });
  }

  function wireActions(root) {
    root.querySelectorAll("form.js-action").forEach(function (form) {
      if (form.dataset.wired === "1") return;
      form.dataset.wired = "1";
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) return;
        var card = form.closest(".status-card");
        fetch(form.action, {
          method: "POST",
          body: new FormData(form),
          headers: { "X-CSRF-Token": csrf(), "Accept": "text/html" },
          credentials: "same-origin"
        }).then(function (resp) {
          if (!resp.ok) throw new Error("action");
          return resp.text();
        }).then(function (html) {
          if (!card) return;
          if (form.action.indexOf("/delete") !== -1) {
            card.remove();
            return;
          }
          var wrapper = document.createElement("div");
          wrapper.innerHTML = html.trim();
          var next = wrapper.firstElementChild;
          if (next) {
            card.replaceWith(next);
            wireActions(next);
          }
        }).catch(function () {});
      });
    });
  }

  function wireSse() {
    var badge = document.getElementById("notification-badge");
    if (!badge || !window.EventSource) return;
    var source = new EventSource("/events");
    source.addEventListener("notification", function (event) {
      try {
        var data = JSON.parse(event.data);
        badge.textContent = String(data.unread || 0);
      } catch (e) {}
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    wireCounters(document);
    wireTabs(document);
    wireActions(document);
    wireSse();
  });
})();
