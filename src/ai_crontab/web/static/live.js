// Keeping a page true while a run is happening.
//
// A run lasts a minute or two, which is long enough that a page rendered when
// it started is wrong by the time you read it. Two pages poll to stay current.
// The schedule asks /api/state for a signature of what it is showing and
// refreshes when that changes; a run's page asks for the turns it has not got
// yet and appends them.
//
// The server stays the only thing that renders. New turns arrive as the HTML
// the page would have been served with, so there is one description of what a
// turn looks like rather than one here and one in a template.
(function () {
  "use strict";

  // How long to wait between polls. Both are small because the server is on
  // this machine and answers in a millisecond, and because a tab nobody is
  // looking at polls not at all. Measured at 15s, the idle wait was long
  // enough that a run started from the CLI took a quarter of a minute to
  // appear on a schedule page somebody was watching.
  var ACTIVE_MS = 2000;   // something is running, so the tail should feel live
  var IDLE_MS = 5000;     // nothing is: this only has to notice one starting
  var HIDDEN_MS = 1000;   // how often to re-check whether the tab came back

  var live = document.getElementById("live");
  var turns = document.querySelector("ol.turns[data-run]");
  var timer = null;
  var stopped = false;

  // Whether the reader has opened or typed into anything a reload would throw
  // away: an expanded turn, a half-written prompt.
  //
  // This watches for the click that opens a turn rather than for the `toggle`
  // event that follows it, because a row appended by the tail fires `toggle`
  // too — text rows arrive already open. Listening for `toggle` made every
  // live run mark itself as being read the moment its first turn landed, so
  // the page never refreshed when the run ended and sat there with a running
  // clock over a run that had finished.
  var touched = false;
  document.addEventListener("click", function (event) {
    if (event.target.closest("summary")) {
      touched = true;
    }
  }, true);
  document.addEventListener("input", function () { touched = true; }, true);

  function isReading() {
    return touched || document.querySelector("details.form[open]") !== null;
  }

  // Say that the page is behind, instead of pulling it out from under someone.
  //
  // Polling stops here: the page is known to be out of date and nothing
  // further is worth learning about it until it is reloaded.
  function sayStale(what) {
    if (document.getElementById("stale")) {
      return;
    }
    var notice = document.createElement("p");
    notice.id = "stale";
    notice.className = "warn";
    notice.textContent = what + " ";
    var link = document.createElement("a");
    link.href = "";
    link.textContent = "Reload";
    notice.appendChild(link);
    document.querySelector("main").prepend(notice);
  }

  function refresh(what) {
    if (isReading()) {
      sayStale(what);
      return;
    }
    window.location.reload();
  }

  // The clock beside a run in flight.
  //
  // Drawn here rather than on the server because it is the one number that
  // changes every second, and a page that asked the server that often would be
  // polling for the sake of a counter. Everything else on these pages is
  // whatever the server last said it was.
  function drawElapsed() {
    var spans = document.querySelectorAll(".elapsed[data-since]");
    for (var i = 0; i < spans.length; i++) {
      var since = Date.parse(spans[i].dataset.since);
      if (isNaN(since)) {
        continue;
      }
      var sec = Math.max(0, Math.round((Date.now() - since) / 1000));
      spans[i].textContent = sec < 60
        ? sec + "s"
        : Math.floor(sec / 60) + "m " + (sec % 60) + "s";
    }
  }

  // Whether the page refreshes from here or waits to be reloaded, it must stop
  // saying the run is still going. Taking the start time off the clock freezes
  // it at its last value rather than leaving a counter running over a run that
  // has ended, and the header's "running for" wrapper goes with it.
  function stopTheClock() {
    var clocks = document.querySelectorAll(".elapsed[data-since]");
    for (var i = 0; i < clocks.length; i++) {
      clocks[i].removeAttribute("data-since");
    }
    var running = document.getElementById("still-running");
    if (running) {
      running.hidden = true;
    }
  }


  function poll(url) {
    return fetch(url, { headers: { Accept: "application/json" } })
      .then(function (reply) {
        if (!reply.ok) {
          throw new Error(reply.status);
        }
        return reply.json();
      });
  }

  // One poll of the schedule. Returns how long to wait before the next, or
  // null to stop.
  function pollSchedule() {
    return poll(live.dataset.poll).then(function (state) {
      if (state.signature !== live.dataset.signature) {
        refresh("The schedule changed.");
        return null;
      }
      return state.active.length ? ACTIVE_MS : IDLE_MS;
    });
  }

  // One poll of a run's transcript, asking only for the turns not on the page.
  //
  // When the run ends the page needs a reload rather than another append: its
  // cost, turn count and closing message come from the `result` event, which
  // was never part of the tail.
  function pollTurns() {
    var url = "/runs/" + encodeURIComponent(turns.dataset.run)
      + "/rows?start=" + encodeURIComponent(turns.dataset.rows);
    return poll(url).then(function (tail) {
      if (tail.html) {
        turns.insertAdjacentHTML("beforeend", tail.html);
        turns.dataset.rows = tail.count;
        var empty = document.getElementById("no-turns");
        if (empty) {
          empty.remove();
        }
      }
      if (!tail.active) {
        stopTheClock();
        refresh("This run has finished.");
        return null;
      }
      return ACTIVE_MS;
    });
  }

  function step(pollOnce) {
    if (stopped) {
      return;
    }
    if (document.hidden) {
      timer = setTimeout(function () { step(pollOnce); }, HIDDEN_MS);
      return;
    }
    pollOnce().then(function (next) {
      if (next === null) {
        stopped = true;
        return;
      }
      timer = setTimeout(function () { step(pollOnce); }, next);
    }, function () {
      // The server is down, restarting, or answered something unexpected.
      // None of that is worth a message on the page: back off and try again.
      timer = setTimeout(function () { step(pollOnce); }, IDLE_MS);
    });
  }

  // A tab left open for a week on a machine that is mostly idle should not be
  // making a request every few seconds to find that out, so polling waits
  // while the tab is hidden and catches up the moment it is looked at again.
  function start(pollOnce) {
    document.addEventListener("visibilitychange", function () {
      if (document.hidden || stopped) {
        return;
      }
      clearTimeout(timer);
      step(pollOnce);
    });
    step(pollOnce);
  }

  drawElapsed();
  if (document.querySelector(".elapsed[data-since]")) {
    setInterval(drawElapsed, 1000);
  }

  if (turns && turns.dataset.live) {
    start(pollTurns);
  } else if (live) {
    start(pollSchedule);
  }
})();
