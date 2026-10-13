(function () {
  const node = document.getElementById("tape");
  const dataNode = document.getElementById("tape-data");
  if (!node || !dataNode || !window.LightweightCharts) return;
  let points = [];
  try {
    points = JSON.parse(dataNode.textContent || "[]");
  } catch (err) {
    return;
  }
  const clockFormat = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });

  const chart = LightweightCharts.createChart(node, {
    autoSize: true,
    layout: { background: { color: "transparent" }, textColor: "#9aa7bc" },
    grid: {
      vertLines: { color: "#202a3b" },
      horzLines: { color: "#202a3b" },
    },
    rightPriceScale: { borderColor: "#202a3b" },
    localization: {
      timeFormatter: function (time) {
        return clockFormat.format(new Date(time * 1000));
      },
    },
    timeScale: {
      borderColor: "#202a3b",
      timeVisible: true,
      secondsVisible: false,
      rightOffset: 0,
      minBarSpacing: 0,
      shiftVisibleRangeOnNewBar: false,
      tickMarkFormatter: function (time) {
        return clockFormat.format(new Date(time * 1000)).slice(0, 5);
      },
    },
  });
  const played = chart.addSeries(LightweightCharts.LineSeries, {
    color: "#8b9cf7",
    lineWidth: 2,
    priceLineVisible: false,
    lastValueVisible: true,
  });
  const session = points.length ? sessionBounds(points[0].time) : { from: 0, to: 0 };

  const playButton = document.getElementById("play");
  const playIcon = playButton && playButton.querySelector("[data-icon=play]");
  const pauseIcon = playButton && playButton.querySelector("[data-icon=pause]");
  const speedBox = document.getElementById("play-speed");
  const clock = document.getElementById("tape-clock");
  const last = document.getElementById("last-price");
  const emNow = document.getElementById("em-now");
  const emPct = document.getElementById("em-pct");
  const wings = document.getElementById("wings");

  const watch = document.getElementById("watch");

  let cursor = 0;
  let playing = false;
  let armed = false;
  let startedAt = 0;
  let origin = 0;
  let stepBusy = false;
  let frameBusy = false;
  let stepTimer = 0;
  let stepSentAt = 0;
  let stepHold = 0;
  let movedSinceStep = false;
  let playAt = 0;

  function etUnix(day, hour, minute) {
    const parts = day.split("-").map(Number);
    const wall = new Date(Date.UTC(parts[0], parts[1] - 1, parts[2], hour, minute, 0));
    const shown = new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      hourCycle: "h23",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).formatToParts(wall);
    const value = {};
    shown.forEach(function (part) {
      value[part.type] = part.value;
    });
    const asEt = Date.UTC(+value.year, +value.month - 1, +value.day, +value.hour % 24, +value.minute, +value.second);
    return Math.floor((wall.getTime() - (asEt - wall.getTime())) / 1000);
  }

  function sessionBounds(unix) {
    const day = new Intl.DateTimeFormat("en-CA", {
      timeZone: "America/New_York",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(new Date(unix * 1000));
    return { from: etUnix(day, 9, 30), to: etUnix(day, 16, 15) };
  }

  const windowSeconds = 2 * 60 * 60;
  const futureShare = 1 / 3;
  let followWindow = true;
  const barStep = points.length > 1
    ? (points[points.length - 1].time - points[0].time) / (points.length - 1)
    : 10;

  function logicalAt(unix) {
    if (points.length < 2) return 0;
    if (unix <= points[0].time) return (unix - points[0].time) / barStep;
    const last = points.length - 1;
    if (unix >= points[last].time) return last + (unix - points[last].time) / barStep;
    let lo = 0;
    let hi = last;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (points[mid].time < unix) lo = mid + 1;
      else hi = mid;
    }
    const prev = Math.max(lo - 1, 0);
    const span = points[lo].time - points[prev].time;
    if (span <= 0) return prev;
    return prev + (unix - points[prev].time) / span;
  }

  function framePlayed() {
    if (followWindow) {
      const at = points[cursor].time;
      const future = windowSeconds * futureShare;
      chart.timeScale().setVisibleLogicalRange({
        from: logicalAt(at - (windowSeconds - future)),
        to: logicalAt(at + future),
      });
    }
    queueFlyLines();
  }

  node.addEventListener("pointerdown", function () { followWindow = false; });
  node.addEventListener("wheel", function () { followWindow = false; }, { passive: true });

  let flyState = { flies: [], adds: [], pad: 0 };
  try {
    flyState = JSON.parse((document.getElementById("fly-lines") || {}).textContent || "{}");
  } catch (err) {
    flyState = { flies: [], adds: [], pad: 0 };
  }
  played.applyOptions({
    autoscaleInfoProvider: function (original) {
      const info = original();
      if (!info || !info.priceRange) return info;
      flyLevels().forEach(function (price) {
        if (price < info.priceRange.minValue) info.priceRange.minValue = price;
        if (price > info.priceRange.maxValue) info.priceRange.maxValue = price;
      });
      const minPriceSpan = 10;
      if (info.priceRange.maxValue - info.priceRange.minValue < minPriceSpan) {
        const mid = (info.priceRange.minValue + info.priceRange.maxValue) / 2;
        info.priceRange.minValue = mid - minPriceSpan / 2;
        info.priceRange.maxValue = mid + minPriceSpan / 2;
      }
      return info;
    },
  });

  function queueFlyLines() {
    if (flyPrimitive._request) flyPrimitive._request();
  }

  function flyOpen() {
    return (flyState.flies || []).some(function (fly) {
      return fly && !fly.close && Number.isFinite(Number(fly.strike));
    });
  }

  function flyLevels() {
    const levels = [];
    if (flyOpen()) (flyState.adds || []).forEach(function (watch) { levels.push(Number(watch)); });
    (flyState.flies || []).forEach(function (fly) {
      if (fly.close) return;
      levels.push(Number(fly.strike), Number(fly.hi), Number(fly.lo));
    });
    return levels.filter(function (price) { return Number.isFinite(price); });
  }

  function openUnix(hhmm) {
    const bits = String(hhmm || "").split(":");
    if (bits.length < 2) return session.from;
    const day = new Intl.DateTimeFormat("en-CA", {
      timeZone: "America/New_York",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(new Date(points[0].time * 1000));
    return etUnix(day, Number(bits[0]), Number(bits[1]));
  }

  function priceLabel(price) {
    const number = Number(price);
    if (Math.abs(number - Math.round(number)) < 1e-6) return String(Math.round(number));
    return number.toFixed(1);
  }

  function yOf(price) {
    const y = played.priceToCoordinate(price);
    return y == null ? null : y;
  }

  function xOf(time) {
    let x = chart.timeScale().timeToCoordinate(time);
    if (x == null && points.length) x = chart.timeScale().timeToCoordinate(points[indexAt(time)].time);
    return x == null ? null : x;
  }

  function buildFlyScene() {
    if (!points.length) return null;
    const x0 = xOf(points[0].time);
    const x1 = xOf(points[cursor].time);
    if (x0 == null || x1 == null) {
      retryFlyLines();
      return null;
    }
    const scene = { adds: [], bands: [], strikes: [] };
    if (flyOpen()) {
      (flyState.adds || []).forEach(function (watch) {
        const y = yOf(Number(watch));
        if (y == null) return;
        scene.adds.push({ x0: x0, x1: x1, y: y, text: priceLabel(watch) });
      });
    }
    (flyState.flies || []).forEach(function (fly) {
      if (fly.close) return;
      const start = xOf(openUnix(fly.open));
      const end = x1;
      if (start == null || end == null || end <= start) return;
      const strike = Number(fly.strike);
      const hi = Number(fly.hi);
      const lo = Number(fly.lo);
      const yHi = yOf(hi);
      const yLo = yOf(lo);
      const yStrike = yOf(strike);
      if (yHi != null && yLo != null) {
        scene.bands.push({
          x0: start,
          x1: end,
          yHi: yHi,
          yLo: yLo,
          hi: priceLabel(hi),
          lo: priceLabel(lo),
        });
      }
      if (yStrike != null) {
        scene.strikes.push({ x0: start, x1: end, y: yStrike, text: priceLabel(strike) });
      }
    });
    flyTries = 0;
    return scene;
  }

  function paintFlyScene(scope, scene) {
    if (!scene) return;
    const ctx = scope.context;
    const hx = scope.horizontalPixelRatio;
    const vy = scope.verticalPixelRatio;
    ctx.save();
    ctx.font = (11 * vy) + "px ui-monospace, Menlo, Consolas, monospace";
    ctx.lineWidth = vy;
    function textRight(text, x, y) {
      const width = ctx.measureText(text).width;
      ctx.fillText(text, Math.max(4 * hx, x * hx - width - 4 * hx), y);
    }
    function hline(x0, x1, y, color, dash, weight) {
      ctx.beginPath();
      ctx.strokeStyle = color;
      ctx.lineWidth = (weight || 1) * vy;
      ctx.setLineDash(dash ? [4 * hx, 3 * hx] : []);
      ctx.moveTo(x0 * hx, y * vy);
      ctx.lineTo(x1 * hx, y * vy);
      ctx.stroke();
    }
    scene.bands.forEach(function (band) {
      const top = Math.min(band.yHi, band.yLo);
      ctx.fillStyle = "rgba(139,156,247,0.16)";
      ctx.fillRect(band.x0 * hx, top * vy, (band.x1 - band.x0) * hx, Math.abs(band.yLo - band.yHi) * vy);
      hline(band.x0, band.x1, band.yHi, "#f0556b", true);
      hline(band.x0, band.x1, band.yLo, "#f0556b", true);
      ctx.fillStyle = "#f0556b";
      textRight(band.hi, band.x1, (band.yHi - 2) * vy);
      textRight(band.lo, band.x1, (band.yLo + 10) * vy);
    });
    scene.strikes.forEach(function (line) {
      hline(line.x0, line.x1, line.y, "#8b9cf7", false, 1.6);
      ctx.fillStyle = "#8b9cf7";
      textRight(line.text, line.x1, (line.y - 3) * vy);
    });
    scene.adds.forEach(function (line) {
      hline(line.x0, line.x1, line.y, "#3dd598", true);
      ctx.fillStyle = "#3dd598";
      textRight(line.text, line.x1, (line.y - 3) * vy);
    });
    ctx.restore();
  }

  const flyView = {
    scene: null,
    zOrder: function () { return "top"; },
    renderer: function () {
      const scene = flyView.scene;
      return {
        draw: function (target) {
          target.useBitmapCoordinateSpace(function (scope) {
            paintFlyScene(scope, scene);
          });
        },
      };
    },
  };
  const flyViews = [flyView];
  const flyPrimitive = {
    _request: null,
    attached: function (param) { this._request = param.requestUpdate; },
    detached: function () { this._request = null; },
    updateAllViews: function () { flyView.scene = buildFlyScene(); },
    paneViews: function () { return flyViews; },
  };
  played.attachPrimitive(flyPrimitive);

  let flyTries = 0;
  function retryFlyLines() {
    if (flyTries > 12) return;
    flyTries += 1;
    setTimeout(queueFlyLines, 40 * flyTries);
  }

  function show(index) {
    cursor = Math.max(0, Math.min(points.length - 1, index));
    played.setData(points.slice(0, cursor + 1));
    const point = points[cursor];
    if (last) last.textContent = Number(point.value).toFixed(2);
    if (clock) clock.textContent = clockFormat.format(new Date(point.time * 1000)) + " ET";
    if (armed) paintEm(point);
    stampAt(point.time);
    framePlayed();
    if (playing && isManual()) queueFrame();
  }

  let frameAt = 0;
  function queueFrame() {
    const now = performance.now();
    if (now - frameAt < 1000) return;
    frameAt = now;
    refreshFrame();
  }

  function stampAt(unix) {
    localStorage.setItem("iron-fly-at", String(unix));
    document.querySelectorAll('input[name="at"]').forEach(function (input) {
      input.value = String(unix);
    });
    const params = new URLSearchParams(location.search);
    if (params.get("at") !== String(unix)) {
      params.set("at", String(unix));
      const next = params.toString();
      history.replaceState(null, "", location.pathname + (next ? "?" + next : ""));
    }
    document.dispatchEvent(new CustomEvent("iron-fly-tick", { detail: { time: unix, playing: playing } }));
  }

  function sameSession(unix) {
    const fmt = new Intl.DateTimeFormat("en-CA", {
      timeZone: "America/New_York",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    });
    return fmt.format(new Date(unix * 1000)) === fmt.format(new Date(points[0].time * 1000));
  }

  function indexAt(unix) {
    let found = 0;
    for (let i = 0; i < points.length; i += 1) {
      if (points[i].time <= unix) found = i;
      else break;
    }
    return found;
  }

  function clearEm() {
    if (emNow) emNow.textContent = "—";
    if (emPct) {
      emPct.textContent = "—";
      emPct.classList.remove("text-bad");
    }
    if (wings) {
      wings.textContent = "—";
      wings.classList.remove("text-bad");
    }
  }

  function paintEm(point) {
    if (emNow) emNow.textContent = point.em == null ? "—" : Number(point.em).toFixed(1);
    if (emPct) {
      const pct = point.pct == null ? null : Math.round(Number(point.pct));
      emPct.textContent = pct == null ? "—" : pct + "%";
      emPct.classList.toggle("text-bad", pct != null && pct <= 50);
    }
    if (wings) {
      const width = point.wings == null ? null : Math.round(Number(point.wings));
      wings.textContent = width == null ? "—" : String(width);
      wings.classList.toggle("text-bad", width != null && width < 50);
    }
  }

  function tick(now) {
    if (!playing) return;
    requestAnimationFrame(tick);
    if (!isManual()) return;
    if (stepBusy || frameBusy) {
      if (!stepHold) stepHold = now;
      return;
    }
    if (stepHold) {
      startedAt += now - stepHold;
      stepHold = 0;
    }
    const speed = Number(speedBox && speedBox.value) || 300;
    const span = Math.max(points.length - 1, 1);
    const elapsed = (now - startedAt) / 1000;
    const next = origin + (elapsed / speed) * span;
    if (next >= span) {
      show(span);
      stopPlaying();
      return;
    }
    show(Math.floor(next));
  }

  function stepSlice() {
    const speed = Math.max(Number(speedBox && speedBox.value) || 300, 1);
    const tapeSpan = Math.max(points[points.length - 1].time - points[0].time, 1);
    return Math.max(1, Math.round(tapeSpan / speed));
  }

  function atEnd() {
    return points.length > 0 && cursor >= points.length - 1;
  }

  function play() {
    if (atEnd()) {
      paint("pause");
      return;
    }
    followWindow = true;
    origin = cursor;
    startedAt = performance.now();
    playing = true;
    armed = true;
    paint("play");
    show(cursor);
    playAt = points[cursor].time;
    stepSentAt = 0;
    if (!isManual()) stepDesk();
    requestAnimationFrame(tick);
  }

  function stopPlaying() {
    playing = false;
    window.clearInterval(stepTimer);
    window.clearTimeout(stepTimer);
    stepTimer = 0;
    paint("pause");
    stampAt(points[cursor].time);
    if (isManual()) refreshFrame();
  }

  function isManual() {
    return !watch || watch.dataset.manual === "1";
  }

  function lockOrders() {
    const useConfig = watch && watch.dataset.useConfig === "1";
    document.querySelectorAll(".desk-order").forEach(function (button) {
      button.disabled = playing || button.dataset.blocked === "1";
    });
    document.querySelectorAll("#watch select[name=regime], #watch select[name=stop_pad], #watch select[name=wings]").forEach(function (box) {
      box.disabled = !!useConfig;
    });
    const apply = document.getElementById("policy-apply");
    if (apply) apply.disabled = !!useConfig;
  }

  const onClasses = ["border-accent", "bg-accent/15", "text-accent"];
  const offClasses = ["border-line", "bg-bg", "text-ink"];

  function showIcon(icon, visible) {
    if (!icon) return;
    if (visible) icon.removeAttribute("hidden");
    else icon.setAttribute("hidden", "");
  }

  function paint(mode) {
    const playingNow = mode === "play";
    playButton.classList.remove(...onClasses, ...offClasses);
    playButton.classList.add(...(playingNow ? onClasses : offClasses));
    playButton.setAttribute("aria-pressed", playingNow ? "true" : "false");
    playButton.disabled = !playingNow && atEnd();
    playButton.style.opacity = playButton.disabled ? "0.4" : "";
    playButton.setAttribute("aria-label", playingNow ? "Pause" : "Play");
    showIcon(playIcon, !playingNow);
    showIcon(pauseIcon, playingNow);
    localStorage.setItem("iron-fly-player", mode);
    lockOrders();
  }

  function stepDesk() {
    if (stepBusy || isManual() || !watch) return;
    stepBusy = true;
    stepSentAt = performance.now();
    movedSinceStep = false;
    const at = playAt || points[cursor].time;
    const body = new URLSearchParams({
      csrf: watch.dataset.csrf || "",
      at: String(at),
      duration: String(stepSlice()),
      tape_day: watch.dataset.day || "",
    });
    fetch("/desk/step", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body,
    })
      .then(function (response) {
        return response.json();
      })
      .then(function (frame) {
        renderFrame(frame);
        if (!frame || frame.at == null) return;
        playAt = Number(frame.at);
        origin = indexAt(playAt);
        startedAt = performance.now();
        show(origin);
      })
      .catch(function () {})
      .finally(function () {
        stepBusy = false;
        movedSinceStep = false;
        if (!playing || isManual()) return;
        if (playAt >= points[points.length - 1].time) {
          stopPlaying();
          return;
        }
        const wait = 250 - (performance.now() - stepSentAt);
        stepTimer = window.setTimeout(function () {
          if (playing && !isManual()) stepDesk();
        }, Math.max(0, wait));
      });
  }

  function refreshFrame() {
    if (frameBusy || !watch || !watch.dataset.day) return;
    frameBusy = true;
    const query = new URLSearchParams({
      date: watch.dataset.day,
      at: String(points[cursor].time),
    });
    fetch("/desk/frame?" + query.toString())
      .then(function (response) {
        return response.json();
      })
      .then(renderFrame)
      .catch(function () {})
      .finally(function () {
        frameBusy = false;
      });
  }

  function renderFrame(frame) {
    if (!frame) return;
    const ladder = document.getElementById("ladder-body");
    const blotter = document.getElementById("blotter-body");
    if (ladder && frame.ladder) ladder.innerHTML = frame.ladder.map(ladderRow).join("") || emptyRow(5, "Waiting for a quote.");
    if (blotter && frame.flies) blotter.innerHTML = frame.flies.map(blotterRow).join("") || emptyRow(10, "No flies yet.");
    paintDeadLongs(frame.dead_longs);
    paintPnl(frame.pnl);
    paintMax(frame.max_profit, frame.max_profit_at, frame.max_profit_at_hi, frame.max_profit_n);
    if (watch && typeof frame.manual === "boolean") watch.dataset.manual = frame.manual ? "1" : "0";
    if (frame.engine && watch) {
      watch.dataset.useConfig = frame.engine.use_config ? "1" : "0";
      watch.dataset.autoOpen = frame.engine.auto_open ? "1" : "0";
      watch.dataset.autoDrop = frame.engine.auto_drop ? "1" : "0";
      watch.dataset.hasFly = (frame.flies || []).some(function (fly) { return fly.status && fly.status !== "empty"; }) ? "1" : "0";
      watch.dataset.manual = frame.engine.auto_open || frame.engine.auto_drop ? "0" : "1";
      const line = document.getElementById("engine-line");
      if (line) line.textContent = frame.engine.line || "";
      const regime = document.querySelector("#watch select[name=regime]");
      if (regime && frame.engine.regime) regime.value = frame.engine.regime;
      document.querySelectorAll("#regime-legend [data-band]").forEach(function (row) {
        row.classList.toggle("text-accent", row.dataset.band === frame.engine.regime);
      });
      const stop = document.querySelector("#watch select[name=stop_pad]");
      if (stop && frame.engine.stop != null) stop.value = String(frame.engine.stop);
      const wings = document.querySelector("#watch select[name=wings]");
      if (wings && frame.engine.wings != null) wings.value = String(frame.engine.wings);
    }
    const flatten = document.querySelector('form[action="/desk/flatten"] .desk-order');
    if (flatten) {
      const anyOpen = (frame.flies || []).some(function (fly) { return fly.status === "active"; });
      flatten.dataset.blocked = anyOpen ? "0" : "1";
    }
    lockOrders();
    if (frame.notice) {
      const note = document.getElementById("fly-note");
      if (note) note.textContent = frame.notice;
    }
    if (frame.lines) {
      flyState = frame.lines;
      queueFlyLines();
    }
    stampAt(points[cursor].time);
  }

  function ladderRow(row) {
    const rule = row.rule ? " bg-accent/10" : "";
    const tone = Number(row.var) < 0 ? "text-bad" : "text-good";
    const strike = row.strike == null ? "" : String(Math.round(Number(row.strike)));
    const action = row.kind === "add" ? orderForm("/desk/open", "Add", strike, "bg-accent text-bg font-medium", row.enabled) : strike ? orderForm("/desk/close", "Drop", strike, "border border-line", row.enabled) : "";
    return "<tr class=\"h-[30px] border-t border-line" + rule + "\">" +
      "<td class=\"px-3 py-0 tabular-nums\">" + Number(row.watch).toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + "</td>" +
      "<td class=\"px-3 py-0\">" + esc(row.kind) + "</td>" +
      "<td class=\"px-3 py-0 tabular-nums\">" + strike + "</td>" +
      "<td class=\"px-3 py-0 tabular-nums " + tone + "\">" + signed(row.var) + "</td>" +
      "<td class=\"px-3 py-0 text-right\">" + action + "</td></tr>";
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

  function blotterRow(fly) {
    const strike = fly.strike == null ? "" : String(Math.round(Number(fly.strike)));
    const wings = fly.wings == null ? "" : String(Math.round(Number(fly.wings)));
    const pnl = fly.pnl == null ? "" : Math.round(Number(fly.pnl) * 100).toLocaleString("en-US", { signDisplay: "always" });
    const pnlValue = fly.pnl == null ? null : Number(fly.pnl);
    const rowTone = pnlValue == null || Number.isNaN(pnlValue) ? "" : pnlValue < 0 ? " text-bad" : pnlValue > 0 ? " text-good" : "";
    const close = fly.status === "active" && strike ? orderForm("/desk/close", "Close", strike, "border border-line", true, true) : "";
    const cookie = fly.origin === "cookie" ? " <span class=\"text-muted\">(c)</span>" : "";
    return "<tr class=\"h-[30px] border-t border-line" + rowTone + "\">" +
      "<td class=\"whitespace-nowrap px-3 py-0\">F" + esc(fly.slot) + cookie + "</td>" +
      "<td class=\"px-3 py-0 tabular-nums\">" + strike + "</td>" +
      "<td class=\"px-3 py-0 tabular-nums\">" + wings + "</td>" +
      "<td class=\"px-3 py-0\">" + esc(fly.status) + "</td>" +
      "<td class=\"px-3 py-0 tabular-nums\">" + esc(fly.time_open || "") + "</td>" +
      "<td class=\"px-3 py-0 tabular-nums\">" + esc(fly.time_close || "") + "</td>" +
      "<td class=\"px-3 py-0 tabular-nums\">" + money(fly.sto) + "</td>" +
      "<td class=\"px-3 py-0 tabular-nums\">" + money(fly.btc) + "</td>" +
      "<td class=\"px-3 py-0 tabular-nums\">" + pnl + "</td>" +
      "<td class=\"px-3 py-0 text-right\">" + close + "</td></tr>";
  }

  function orderForm(action, label, strike, look, enabled, hand) {
    const day = watch ? watch.dataset.day || "" : "";
    const csrf = watch ? watch.dataset.csrf || "" : "";
    const kind = label === "Add" ? "open" : "drop";
    const blocked = enabled ? "0" : "1";
    return "<form method=\"post\" action=\"" + action + "\">" +
      "<input type=\"hidden\" name=\"csrf\" value=\"" + esc(csrf) + "\" />" +
      "<input type=\"hidden\" name=\"tape_day\" value=\"" + esc(day) + "\" />" +
      "<input type=\"hidden\" name=\"at\" value=\"" + esc(points[cursor].time) + "\" />" +
      (strike ? "<input type=\"hidden\" name=\"strike\" value=\"" + strike + "\" />" : "") +
      "<button type=\"submit\" class=\"desk-order h-6 rounded-full px-3 text-sm " + look + " disabled:opacity-40\" data-kind=\"" + kind + "\" data-hand=\"" + (hand ? "1" : "0") + "\" data-blocked=\"" + blocked + "\"" +
      (blocked === "1" ? " disabled" : "") + ">" + esc(label) + "</button></form>";
  }

  function emptyRow(span, text) {
    return "<tr><td class=\"px-3 py-6 text-muted\" colspan=\"" + span + "\">" + esc(text) + "</td></tr>";
  }

  function paintMax(dollars, pinLo, pinHi, count) {
    const node = document.getElementById("max-pnl");
    if (!node) return;
    node.classList.remove("text-bad");
    node.replaceChildren();
    if (dollars == null || dollars === "") {
      node.textContent = "—";
      return;
    }
    const amount = Math.round(Number(dollars));
    node.append(document.createTextNode(amount.toLocaleString("en-US")));
    if (amount < 0) node.classList.add("text-bad");
    if (pinLo == null || pinLo === "") return;
    const pin = document.createElement("span");
    pin.className = "text-sm font-normal text-muted";
    let label = String(Math.round(Number(pinLo)));
    if (pinHi != null && pinHi !== "" && Math.round(Number(pinHi)) !== Math.round(Number(pinLo))) {
      label += "–" + Math.round(Number(pinHi));
    }
    pin.textContent = label + " · " + (count == null ? "" : count);
    node.append(pin);
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

  function money(value) {
    return value == null || value === "" ? "" : Number(value).toFixed(2);
  }

  function signed(value) {
    const number = Number(value);
    return (number >= 0 ? "+" : "") + number.toFixed(2);
  }

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"]/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;" }[ch];
    });
  }

  if (!playButton) {
    window.deskTapeAfter = function () {
      return points.length ? Number(points[points.length - 1].time) : 0;
    };
    const barSeconds = Math.max(Math.round(barStep), 1);
    window.deskTape = function (incoming, lines, lastPrice) {
      if (lines && typeof lines === "object") flyState = lines;
      let changed = false;
      (incoming || []).forEach(function (point) {
        const time = Number(point.time);
        const value = Number(point.value);
        if (!Number.isFinite(time) || !Number.isFinite(value)) return;
        const last = points.length ? points[points.length - 1] : null;
        if (!last) {
          points.push({ time: time, value: value });
          played.update({ time: time, value: value });
          changed = true;
          return;
        }
        if (time < Number(last.time)) return;
        if (time - Number(last.time) < barSeconds) {
          if (Number(last.value) === value) return;
          last.value = value;
          played.update({ time: Number(last.time), value: value });
          changed = true;
          return;
        }
        points.push({ time: time, value: value });
        played.update({ time: time, value: value });
        changed = true;
      });
      const price = lastPrice == null || lastPrice === "" ? NaN : Number(lastPrice);
      if (!changed && points.length && Number.isFinite(price) && Number(points[points.length - 1].value) !== price) {
        points[points.length - 1].value = price;
        played.update({ time: Number(points[points.length - 1].time), value: price });
        changed = true;
      }
      if (!changed && !(lines && typeof lines === "object")) return;
      if (!points.length) return;
      cursor = points.length - 1;
      framePlayed();
    };
    if (points.length) {
      cursor = points.length - 1;
      played.setData(points);
      framePlayed();
      requestAnimationFrame(framePlayed);
    }
    return;
  }

  const params = new URLSearchParams(location.search);
  if (params.get("reset") === "1") {
    localStorage.removeItem("iron-fly-at");
    localStorage.setItem("iron-fly-player", "pause");
    params.delete("reset");
    params.delete("at");
    const next = params.toString();
    history.replaceState(null, "", location.pathname + (next ? "?" + next : ""));
  }
  const queryAt = Number(params.get("at") || 0);
  const storedAt = Number(localStorage.getItem("iron-fly-at") || 0);
  const resumeAt = queryAt || storedAt;
  const start = resumeAt && sameSession(resumeAt) ? indexAt(resumeAt) : 0;
  show(start);
  if (localStorage.getItem("iron-fly-player") === "play") play();
  else {
    paint("pause");
    if (start > 0) {
      armed = true;
      paintEm(points[start]);
      refreshFrame();
    } else clearEm();
  }
  let appliedHide = 0;

  function flashConfigApplied() {
    const applied = document.getElementById("spec-applied");
    if (!applied) return;
    applied.classList.remove("hidden");
    window.clearTimeout(appliedHide);
    appliedHide = window.setTimeout(function () {
      applied.classList.add("hidden");
      appliedHide = 0;
    }, 2500);
  }

  function paintConfigResult(frame) {
    const applied = document.getElementById("spec-applied");
    const error = document.getElementById("spec-error");
    const summary = document.getElementById("spec-summary");
    const ok = !!(frame && frame.applied);
    const message = frame && frame.notice ? String(frame.notice) : "";
    if (applied) {
      window.clearTimeout(appliedHide);
      appliedHide = 0;
      if (ok) flashConfigApplied();
      else applied.classList.add("hidden");
    }
    if (error) {
      if (!ok && message) {
        error.textContent = message;
        error.classList.remove("hidden");
      } else {
        error.textContent = "";
        error.classList.add("hidden");
      }
    }
    if (summary && frame && frame.spec_summary) {
      summary.textContent = frame.spec_summary;
      summary.classList.remove("hidden");
    }
  }

  function postDesk(form) {
    if (!watch || !form) return;
    const at = form.querySelector('input[name="at"]');
    if (at && points[cursor]) at.value = String(points[cursor].time);
    const action = form.getAttribute("action") || "";
    const body = new URLSearchParams(new FormData(form));
    fetch(action, {
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
        renderFrame(frame);
        if (action === "/desk/config") paintConfigResult(frame);
        if (frame && frame.reset) {
          playing = false;
          window.clearTimeout(stepTimer);
          stepTimer = 0;
          localStorage.removeItem("iron-fly-at");
          followWindow = true;
          show(0);
          paint("pause");
        }
      })
      .catch(function () {});
  }

  window.deskPost = postDesk;
  document.addEventListener("submit", function (event) {
    const form = event.target;
    if (!form || !form.getAttribute || !watch) return;
    const action = form.getAttribute("action") || "";
    if (action.indexOf("/desk/") !== 0) return;
    if (action === "/desk/switches" || action === "/desk/policy" || action === "/desk/flatten" || action === "/desk/close") return;
    event.preventDefault();
    postDesk(form);
  });

  playButton.addEventListener("click", function () {
    if (playing) {
      stopPlaying();
      return;
    }
    play();
  });
  speedBox.addEventListener("change", function () {
    if (!playing) return;
    origin = cursor;
    startedAt = performance.now();
  });
})();
