(function () {
  const root = document.getElementById("open-fly");
  if (!root) return;
  const form = root.querySelector("form");
  const legs = document.getElementById("fly-legs");
  const strikeLabel = document.getElementById("fly-strike");
  const bid = document.getElementById("fly-bid");
  const mid = document.getElementById("fly-mid");
  const ask = document.getElementById("fly-ask");
  const credit = document.getElementById("fly-credit");
  const note = document.getElementById("fly-note");
  const marks = document.getElementById("fly-marks");
  const down = document.getElementById("fly-down");
  const up = document.getElementById("fly-up");
  const reset = document.getElementById("fly-reset");
  const preview = document.getElementById("fly-preview");
  const widths = root.querySelectorAll("[data-width]");
  const watch = document.getElementById("watch");

  let strike = null;
  let width = Number(root.dataset.wings) || 50;
  let spot = null;
  let quotes = {};
  let loadedMinute = null;
  let customCredit = false;
  let request = 0;

  function round5(value) {
    return Math.round(Number(value) / 5) * 5;
  }

  function money(value) {
    return value == null || Number.isNaN(Number(value)) ? "—" : Number(value).toFixed(2);
  }

  function paint() {
    const put = strike == null ? null : strike - width;
    const call = strike == null ? null : strike + width;
    if (legs) legs.textContent = strike == null ? "—" : put + "/" + strike + "/" + call;
    if (strikeLabel) strikeLabel.textContent = strike == null ? "—" : String(strike);
    form.elements.strike.value = strike == null ? "" : String(strike);
    form.elements.wings.value = String(width);
    const quote = strike == null ? null : quotes[strike + ":" + width];
    if (bid) bid.textContent = quote ? money(quote.bid) : "—";
    if (mid) mid.textContent = quote ? money(quote.mid) : "—";
    if (ask) ask.textContent = quote ? money(quote.ask) : "—";
    if (!customCredit) credit.value = quote ? Number(quote.mid).toFixed(2) : "";
    widths.forEach(function (button) {
      const on = Number(button.dataset.width) === width;
      button.setAttribute("aria-pressed", on ? "true" : "false");
      button.classList.toggle("border-accent", on);
      button.classList.toggle("bg-accent/15", on);
      button.classList.toggle("text-accent", on);
      button.classList.toggle("border-line", !on);
      button.classList.toggle("bg-bg", !on);
      button.classList.toggle("text-ink", !on);
    });
  }

  function applyPacket(packet) {
    quotes = (packet && packet.quotes) || {};
    if (packet && packet.spot != null) spot = Number(packet.spot);
    if (packet && packet.source && marks) marks.textContent = "Marks: " + packet.source;
    if (strike == null && spot != null) strike = round5(spot);
    paint();
  }

  function playhead() {
    const field = form.querySelector('input[name="at"]');
    return field && field.value ? field.value : "";
  }

  function load(unix, done) {
    const minute = unix ? Math.floor(Number(unix) / 60) : null;
    if (minute != null && minute === loadedMinute) {
      paint();
      if (done) done();
      return;
    }
    const query = new URLSearchParams();
    if (watch && watch.dataset.day) query.set("date", watch.dataset.day);
    if (unix) query.set("at", String(unix));
    const token = ++request;
    if (note && !note.textContent) note.textContent = "Loading chain…";
    fetch("/desk/chain?" + query.toString())
      .then(function (response) {
        return response.json();
      })
      .then(function (packet) {
        if (token !== request) return;
        if (minute != null) loadedMinute = minute;
        applyPacket(packet);
        if (!packet || !packet.strikes || !packet.strikes.length) {
          if (note) note.textContent = (packet && packet.error) || "No chain at this time.";
        } else if (note && (note.textContent === "No chain at this time." || note.textContent === "Loading chain…")) {
          note.textContent = "";
        }
        if (done) done();
      })
      .catch(function () {
        if (note) note.textContent = "No chain at this time.";
      });
  }

  function quoteNow(done) {
    load(playhead(), done);
  }

  function stepStrike(delta) {
    function move() {
      if (strike == null && spot != null) strike = round5(spot);
      if (strike == null) return;
      strike += delta;
      customCredit = false;
      if (note) note.textContent = "";
      paint();
    }
    if (strike == null && spot == null) {
      quoteNow(move);
      return;
    }
    move();
    quoteNow();
  }

  down.addEventListener("click", function () {
    stepStrike(-5);
  });
  up.addEventListener("click", function () {
    stepStrike(5);
  });
  function saveWings() {
    const box = document.querySelector('#watch select[name="wings"]');
    if (box) box.value = String(width);
    if (root.dataset.locked === "1" || !watch) return;
    const body = new URLSearchParams({
      csrf: watch.dataset.csrf || "",
      wings: String(width),
    });
    fetch("/desk/wings", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body,
    }).catch(function () {});
  }

  widths.forEach(function (button) {
    button.addEventListener("click", function () {
      width = Number(button.dataset.width);
      customCredit = false;
      if (note) note.textContent = "";
      quoteNow();
      saveWings();
    });
  });
  credit.addEventListener("input", function () {
    customCredit = credit.value !== "";
  });
  reset.addEventListener("click", function () {
    width = Number(root.dataset.wings) || 50;
    strike = spot == null ? null : round5(spot);
    customCredit = false;
    if (note) note.textContent = "";
    quoteNow();
  });
  preview.addEventListener("click", function () {
    if (strike == null && spot != null) strike = round5(spot);
    if (strike == null) {
      if (note) note.textContent = "Choose a strike.";
      return;
    }
    paint();
    sendFly("/desk/preview");
  });

  function sendFly(url) {
    if (note) note.textContent = "Sending…";
    const body = new URLSearchParams(new FormData(form));
    fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        Accept: "application/json",
      },
      body: body,
    })
      .then(function (response) {
        return response.json();
      })
      .then(function (frame) {
        const message = frame && frame.notice ? frame.notice : url.indexOf("preview") >= 0 ? "Preview sent." : "Order sent.";
        if (note) note.textContent = message;
        if (url.indexOf("preview") < 0 && frame && !frame.notice) window.location.reload();
      })
      .catch(function () {
        if (note) note.textContent = "The desk did not answer.";
      });
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (strike == null) {
      if (note) note.textContent = "Choose a strike.";
      return;
    }
    paint();
    sendFly("/desk/open");
  });

  let seeded = {};
  try {
    seeded = JSON.parse((document.getElementById("chain-data") || {}).textContent || "{}");
  } catch (err) {
    seeded = {};
  }
  if (seeded && seeded.quotes && Object.keys(seeded.quotes).length) {
    const last = document.getElementById("last-price");
    const fromLast = last ? Number(String(last.textContent).replace(/,/g, "")) : NaN;
    if (!Number.isNaN(fromLast)) seeded.spot = fromLast;
    seeded.source = root.dataset.source;
    applyPacket(seeded);
    const at = playhead();
    loadedMinute = at ? Math.floor(Number(at) / 60) : null;
  } else {
    load("");
  }

  document.addEventListener("iron-fly-wings", function (event) {
    const next = Number(event.detail);
    if (!next) return;
    width = next;
    root.dataset.wings = String(next);
    customCredit = false;
    paint();
  });
  document.addEventListener("iron-fly-frame", function (event) {
    const engine = event.detail && event.detail.engine;
    if (!engine) return;
    root.dataset.locked = engine.use_config ? "1" : "0";
  });
})();
