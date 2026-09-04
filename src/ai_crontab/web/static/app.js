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

  function submit(form) {
    var body = new URLSearchParams(new FormData(form));
    fetch(form.action, {
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
      window.location = result.data.redirect + "#" + encodeURIComponent(result.data.message);
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
