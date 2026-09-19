// Every write on these pages goes through here.
//
// A plain HTML form cannot do it: the server refuses a mutating request that
// does not carry X-AI-Crontab, and a form has no way to add a header. That
// refusal is the whole defence against a page you have open somewhere else
// posting to this port, so the awkwardness is the point rather than an
// oversight. `fetch` can set the header, so the forms are submitted from here.
(function () {
  "use strict";

  var flash = null;

  // Say what happened where the eye already is, at the top of the page.
  //
  // A write either navigates somewhere, in which case the message rides along
  // in the URL fragment, or it failed, in which case this is the only place the
  // reason appears.
  function say(text, isError) {
    if (!flash) {
      flash = document.createElement("p");
      flash.id = "flash";
      document.querySelector("main").prepend(flash);
    }
    flash.textContent = text;
    flash.className = isError ? "error" : "";
    flash.hidden = false;
  }

  // Go where the write said to go, carrying its message in the fragment.
  //
  // Most writes send you back to the page you were already on: an edit, an
  // enable, anything on the inbox. Assigning a URL that differs from this one
  // only in its fragment is a same-document navigation, so the browser moves
  // the fragment and does not reload — the page keeps showing what it showed
  // before the write, and `say` never runs again to explain why. Asking for
  // the reload outright is what makes a save visible. The fragment survives
  // one, so the message still arrives the way it does from any other page.
  function go(where, message) {
    var target = new URL(where, window.location.href);
    target.hash = encodeURIComponent(message);
    if (target.pathname === window.location.pathname
        && target.search === window.location.search) {
      window.location.hash = target.hash;
      window.location.reload();
    } else {
      window.location.assign(target.href);
    }
  }

  // Where a form posts, read from its markup rather than from `form.action`.
  //
  // A control named `action` shadows the form's own property: the inbox forms
  // carry `<input name="action" value="delete">`, so `form.action` there is
  // that input, which stringifies to "[object HTMLInputElement]" and resolves
  // to a path nothing serves. The attribute is what the template wrote.
  function target(form) {
    return new URL(form.getAttribute("action") || "", window.location.href).href;
  }

  function submit(form) {
    var body = new URLSearchParams(new FormData(form));
    fetch(target(form), {
      method: "POST",
      headers: {
        "X-AI-Crontab": "1",
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: body,
    }).then(function (reply) {
      return reply.json().then(function (data) {
        return { ok: reply.ok, data: data };
      });
    }).then(function (result) {
      if (!result.ok) {
        say(result.data.detail || "the server refused that", true);
        return;
      }
      go(result.data.redirect, result.data.message);
    }).catch(function (err) {
      say(String(err), true);
    });
  }

  document.addEventListener("submit", function (event) {
    event.preventDefault();
    var form = event.target;
    var confirmation = form.dataset.confirm;
    if (confirmation && !window.confirm(confirmation)) {
      return;
    }
    submit(form);
  });

  // The message from the write that brought us here, left in the fragment
  // because the page it lands on is a fresh load with nothing else to carry it.
  if (window.location.hash.length > 1) {
    say(decodeURIComponent(window.location.hash.slice(1)), false);
    history.replaceState(null, "", window.location.pathname + window.location.search);
  }
})();
