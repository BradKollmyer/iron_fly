(function () {
  const saved = ["/desk/switches", "/desk/policy", "/desk/flatten", "/desk/close", "/desk/leftover-close"];

  function post(form) {
    if (!form) return;
    const at = form.querySelector('input[name="at"]');
    const queryAt = new URLSearchParams(location.search).get("at");
    if (at && !at.value && queryAt) at.value = queryAt;
    const body = new URLSearchParams(new FormData(form));
    fetch(form.getAttribute("action"), {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        Accept: "application/json",
      },
      body: body,
    })
      .then(function (response) {
        return response.json().then(function (frame) {
          if (!response.ok) throw new Error((frame && frame.notice) || "The desk did not save.");
          return frame;
        });
      })
      .then(function (frame) {
        apply(frame);
        document.dispatchEvent(new CustomEvent("iron-fly-frame", { detail: frame }));
      })
      .catch(function (err) {
        const note = document.getElementById("fly-note");
        if (note) note.textContent = err && err.message ? err.message : "The desk did not answer.";
      });
  }

  window.deskPost = post;

  document.addEventListener("submit", function (event) {
    const form = event.target;
    if (!form || !form.getAttribute) return;
    const action = form.getAttribute("action") || "";
    if (saved.indexOf(action) < 0) return;
    event.preventDefault();
    post(form);
  });

  let ladderStamp = "";
  let blotterStamp = "";
  let refreshTimer = 0;

  window.deskRefresh = function (seconds) {
    if (refreshTimer) {
      clearInterval(refreshTimer);
      refreshTimer = 0;
    }
    const every = Number(seconds);
    if (every !== 1 && every !== 2) return;
    const pull = function () {
      const after = typeof window.deskTapeAfter === "function" ? window.deskTapeAfter() : 0;
      fetch("/refresh?after=" + encodeURIComponent(after), { headers: { Accept: "application/json" } })
        .then(function (response) {
          return response.json().then(function (frame) {
            if (!response.ok) throw new Error("refresh failed");
            return frame;
          });
        })
        .then(apply)
        .catch(function () {});
    };
    pull();
    refreshTimer = setInterval(pull, every * 1000);
  };

  function apply(frame) {
    if (!frame) return;
    const watch = document.getElementById("watch");
    const engine = frame.engine || {};
    if (watch && engine) {
      if (typeof engine.use_config === "boolean") watch.dataset.useConfig = engine.use_config ? "1" : "0";
      if (typeof engine.auto_open === "boolean") watch.dataset.autoOpen = engine.auto_open ? "1" : "0";
      if (typeof engine.auto_drop === "boolean") watch.dataset.autoDrop = engine.auto_drop ? "1" : "0";
      watch.dataset.manual = engine.auto_open || engine.auto_drop ? "0" : "1";
      watch.dataset.hasFly = (frame.flies || []).some(function (fly) {
        return fly.status === "active";
      })
        ? "1"
        : "0";
    }
    const line = document.getElementById("engine-line");
    if (line && engine.line) line.textContent = engine.line;
    const locked = !!engine.use_config;
    document.querySelectorAll("#watch select[name=regime], #watch select[name=stop_pad], #watch select[name=wings]").forEach(function (box) {
      box.disabled = locked;
      if (document.activeElement === box) return;
      if (box.name === "regime" && engine.regime) box.value = engine.regime;
      if (box.name === "stop_pad" && engine.stop != null) box.value = String(engine.stop);
      if (box.name === "wings" && engine.wings != null) box.value = String(engine.wings);
    });
    const applyButton = document.getElementById("policy-apply");
    if (applyButton) applyButton.disabled = locked;
    document.querySelectorAll("#regime-legend [data-band]").forEach(function (row) {
      row.classList.toggle("text-accent", row.dataset.band === engine.regime);
    });
    if (engine.wings != null && engine.wings !== "auto") {
      document.dispatchEvent(new CustomEvent("iron-fly-wings", { detail: engine.wings }));
    }
    const ladder = document.getElementById("ladder-body");
    const ladderNext = JSON.stringify(frame.ladder || []);
    if (ladder && frame.ladder && ladderNext !== ladderStamp) {
      ladderStamp = ladderNext;
      ladder.innerHTML = frame.ladder.map(ladderRow).join("") || emptyRow(5, "Waiting for a quote.");
    }
    const blotter = document.getElementById("blotter-body");
    const blotterNext = JSON.stringify(frame.flies || []);
    if (blotter && frame.flies && blotterNext !== blotterStamp) {
      blotterStamp = blotterNext;
      blotter.innerHTML = frame.flies.map(blotterRow).join("") || emptyRow(10, "No flies yet.");
    }
    paintOpens(engine);
    paintDeadLongs(frame.dead_longs);
    paintLeftovers(frame.leftover_longs);
    const flatten = document.querySelector('form[action="/desk/flatten"] .desk-order');
    if (flatten) {
      const anyOpen = (frame.flies || []).some(function (fly) {
        return fly.status === "active";
      });
      flatten.dataset.blocked = anyOpen ? "0" : "1";
      flatten.disabled = !anyOpen;
    }
    paintPnl(frame.pnl);
    paintQuote(frame);
    if (window.deskTape) window.deskTape(frame.tape, frame.lines, frame.last);
    if (frame.notice) {
      const note = document.getElementById("fly-note");
      if (note) note.textContent = frame.notice;
    }
  }

  function paintOpens(engine) {
    const openBox = document.querySelector("[data-name=auto_open]");
    if (!openBox || engine.open_paused == null) return;
    const paused = !!engine.open_paused;
    const configOn = !!engine.use_config;
    const label = openBox.closest("label");
    // Use Config owns opens from the YAML; Auto Open is for manual trading only.
    if (configOn) {
      openBox.checked = false;
      openBox.disabled = true;
      if (label) label.classList.add("opacity-50");
      openBox.form.elements.auto_open.value = "0";
      return;
    }
    if (paused) {
      openBox.checked = false;
      openBox.disabled = false;
      if (label) label.classList.remove("opacity-50");
      openBox.form.elements.auto_open.value = "0";
      return;
    }
    if (typeof engine.auto_open === "boolean") {
      openBox.checked = engine.auto_open;
      openBox.disabled = false;
      if (label) label.classList.remove("opacity-50");
      openBox.form.elements.auto_open.value = engine.auto_open ? "1" : "0";
    }
  }

  function paintDeadLongs(rows) {
    const slot = document.getElementById("dead-longs");
    const detail = document.getElementById("dead-longs-detail");
    if (!slot) return;
    const notes = rows || [];
    if (!notes.length) {
      slot.hidden = true;
      if (detail) detail.textContent = "";
      return;
    }
    slot.hidden = false;
    if (detail) {
      detail.textContent = notes
        .map(function (row) {
          return "F" + row.slot + " " + row.label + " (" + row.detail + ")";
        })
        .join("; ");
    }
  }

  function paintLeftovers(rows) {
    const slot = document.getElementById("leftover-longs");
    if (!slot) return;
    const lots = rows || [];
    if (!lots.length) {
      slot.hidden = true;
      const body = slot.querySelector("tbody");
      if (body) body.innerHTML = "";
      return;
    }
    slot.hidden = false;
    const body = slot.querySelector("tbody");
    if (!body) return;
    body.innerHTML = lots.map(leftoverRow).join("");
  }

  function leftoverRow(lot) {
    const strike = Math.round(Number(lot.strike));
    const tone = lot.dead ? " text-bad" : "";
    const watch = document.getElementById("watch");
    const csrf = watch ? watch.dataset.csrf || "" : "";
    const day = watch ? watch.dataset.day || "" : "";
    return (
      '<tr class="h-[30px] border-t border-line' +
      tone +
      '">' +
      '<td class="px-3 py-0">' +
      esc(lot.right) +
      " " +
      strike +
      "</td>" +
      '<td class="px-3 py-0 tabular-nums">' +
      esc(lot.quantity) +
      "</td>" +
      '<td class="px-3 py-0 tabular-nums">' +
      quote(lot.bid) +
      "</td>" +
      '<td class="px-3 py-0 tabular-nums">' +
      quote(lot.ask) +
      "</td>" +
      '<td class="px-3 py-0 tabular-nums">' +
      quote(lot.mid) +
      "</td>" +
      '<td class="px-3 py-0 text-right">' +
      '<form method="post" action="/desk/leftover-close" onsubmit="return confirm(\'Sell to close ' +
      esc(lot.right) +
      " " +
      strike +
      "?\');\">" +
      '<input type="hidden" name="csrf" value="' +
      esc(csrf) +
      '" />' +
      '<input type="hidden" name="tape_day" value="' +
      esc(day) +
      '" />' +
      '<input type="hidden" name="at" value="" />' +
      '<input type="hidden" name="occ" value="' +
      esc(lot.occ) +
      '" />' +
      '<button type="submit" class="desk-order h-6 rounded-full border border-bad/50 px-3 text-sm text-bad">Close</button>' +
      "</form></td></tr>"
    );
  }

  function quote(value) {
    if (value == null || value === "") return "—";
    return Number(value).toFixed(2);
  }

  function ladderRow(row) {
    const rule = row.rule ? " bg-accent/10" : "";
    const tone = Number(row.var) < 0 ? "text-bad" : "text-good";
    const strike = row.strike == null ? "" : String(Math.round(Number(row.strike)));
    const action =
      row.kind === "add"
        ? orderForm("/desk/open", "Add", strike, "bg-accent text-bg font-medium", row.enabled)
        : strike
          ? orderForm("/desk/close", "Drop", strike, "border border-line", row.enabled)
          : "";
    return (
      '<tr class="h-[30px] border-t border-line' +
      rule +
      '">' +
      '<td class="px-3 py-0 tabular-nums">' +
      Number(row.watch).toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 }) +
      "</td>" +
      '<td class="px-3 py-0">' +
      esc(row.kind) +
      "</td>" +
      '<td class="px-3 py-0 tabular-nums">' +
      strike +
      "</td>" +
      '<td class="px-3 py-0 tabular-nums ' +
      tone +
      '">' +
      signed(row.var) +
      "</td>" +
      '<td class="px-3 py-0 text-right">' +
      action +
      "</td></tr>"
    );
  }

  function blotterRow(fly) {
    const strike = fly.strike == null ? "" : String(Math.round(Number(fly.strike)));
    const wings = fly.wings == null ? "" : String(Math.round(Number(fly.wings)));
    const pnl = fly.pnl == null ? "" : Math.round(Number(fly.pnl) * 100).toLocaleString("en-US", { signDisplay: "always" });
    const pnlValue = fly.pnl == null ? null : Number(fly.pnl);
    const rowTone = pnlValue == null || Number.isNaN(pnlValue) ? "" : pnlValue < 0 ? " text-bad" : pnlValue > 0 ? " text-good" : "";
    const close = fly.status === "active" && strike ? orderForm("/desk/close", "Close", strike, "border border-line", true, true) : "";
    const cookie = fly.origin === "cookie" ? ' <span class="text-muted">(c)</span>' : "";
    return (
      '<tr class="h-[30px] border-t border-line' +
      rowTone +
      '">' +
      '<td class="whitespace-nowrap px-3 py-0">F' +
      esc(fly.slot) +
      cookie +
      "</td>" +
      '<td class="px-3 py-0 tabular-nums">' +
      strike +
      "</td>" +
      '<td class="px-3 py-0 tabular-nums">' +
      wings +
      "</td>" +
      '<td class="px-3 py-0">' +
      esc(fly.status) +
      "</td>" +
      '<td class="px-3 py-0 tabular-nums">' +
      esc(fly.time_open || "") +
      "</td>" +
      '<td class="px-3 py-0 tabular-nums">' +
      esc(fly.time_close || "") +
      "</td>" +
      '<td class="px-3 py-0 tabular-nums">' +
      money(fly.sto) +
      "</td>" +
      '<td class="px-3 py-0 tabular-nums">' +
      money(fly.btc) +
      "</td>" +
      '<td class="px-3 py-0 tabular-nums">' +
      pnl +
      "</td>" +
      '<td class="px-3 py-0 text-right">' +
      close +
      "</td></tr>"
    );
  }

  function orderForm(action, label, strike, look, enabled, hand) {
    const watch = document.getElementById("watch");
    const day = watch ? watch.dataset.day || "" : "";
    const csrf = watch ? watch.dataset.csrf || "" : "";
    const at = document.querySelector('input[name="at"]');
    const kind = label === "Add" ? "open" : "drop";
    const blocked = enabled ? "0" : "1";
    return (
      '<form method="post" action="' +
      action +
      '">' +
      '<input type="hidden" name="csrf" value="' +
      esc(csrf) +
      '" />' +
      '<input type="hidden" name="tape_day" value="' +
      esc(day) +
      '" />' +
      '<input type="hidden" name="at" value="' +
      esc(at ? at.value : "") +
      '" />' +
      (strike ? '<input type="hidden" name="strike" value="' + strike + '" />' : "") +
      '<button type="submit" class="desk-order h-6 rounded-full px-3 text-sm ' +
      look +
      ' disabled:opacity-40" data-kind="' +
      kind +
      '" data-hand="' +
      (hand ? "1" : "0") +
      '" data-blocked="' +
      blocked +
      '"' +
      (blocked === "1" ? " disabled" : "") +
      ">" +
      esc(label) +
      "</button></form>"
    );
  }

  function emptyRow(span, text) {
    return '<tr><td class="px-3 py-6 text-muted" colspan="' + span + '">' + esc(text) + "</td></tr>";
  }

  function paintPnl(total) {
    const node = document.getElementById("pnl");
    if (!node) return;
    node.classList.remove("text-bad", "text-good");
    if (total == null || total === "") {
      node.textContent = "—";
      return;
    }
    const dollars = Math.round(Number(total) * 100);
    node.textContent = dollars.toLocaleString("en-US", { signDisplay: "always" });
    if (dollars < 0) node.classList.add("text-bad");
    else if (dollars > 0) node.classList.add("text-good");
  }

  function paintQuote(frame) {
    const last = document.getElementById("last-price");
    if (last) last.textContent = frame.last == null ? "—" : Number(frame.last).toFixed(2);
    paintNumber("em-open", frame.em_open, 1);
    paintNumber("em-now", frame.em_now, 1);
    const pct = document.getElementById("em-pct");
    if (pct) {
      pct.classList.toggle("text-bad", frame.em_pct != null && Number(frame.em_pct) <= 50);
      pct.textContent = frame.em_pct == null ? "—" : Math.round(Number(frame.em_pct)) + "%";
    }
    const wings = document.getElementById("wings");
    if (wings) {
      wings.classList.toggle("text-bad", frame.put_40_width != null && Number(frame.put_40_width) < 50);
      wings.textContent = frame.put_40_width == null ? "—" : String(Math.round(Number(frame.put_40_width)));
    }
    const max = document.getElementById("max-pnl");
    if (max) {
      max.classList.toggle("text-bad", frame.max_profit != null && Number(frame.max_profit) < 0);
      if (frame.max_profit == null) {
        max.textContent = "—";
      } else {
        let text = Math.round(Number(frame.max_profit)).toLocaleString("en-US");
        if (frame.max_profit_at != null) {
          text += " " + Math.round(Number(frame.max_profit_at));
          if (frame.max_profit_at_hi != null) text += "–" + Math.round(Number(frame.max_profit_at_hi));
          if (frame.max_profit_n != null) text += " · " + frame.max_profit_n;
        }
        max.textContent = text;
      }
    }
  }

  function paintNumber(id, value, digits) {
    const node = document.getElementById(id);
    if (!node) return;
    node.textContent = value == null || value === "" ? "—" : Number(value).toFixed(digits);
  }

  function money(value) {
    return value == null || value === "" ? "" : Number(value).toFixed(2);
  }

  function signed(value) {
    const number = Number(value);
    return (number >= 0 ? "+" : "") + number.toFixed(2);
  }

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"]/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch];
    });
  }
})();
