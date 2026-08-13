/* Скрипт-закладка: запускается на странице лота Copart и открывает калькулятор
   уже заполненным.

   Работает потому, что выполняется внутри copart.com: запрос к их API идёт
   с их же origin, поэтому нет ни CORS, ни бот-защиты — для Akamai это обычный
   клик обычного пользователя.

   Плейсхолдер __CALC__ подставляет index.html при сборке ссылки, чтобы
   закладка вела туда же, откуда её перетащили. */
(function () {
  var CALC = "__CALC__";
  var m = location.pathname.match(/\/lot\/(\d+)/);
  if (!m) { location.href = CALC; return; }
  var lot = m[1];

  /* окно открываем сразу, внутри клика — иначе блокировщик popup'ов зарежет */
  var w = window.open("about:blank");

  function after(t, label, maxlines) {
    var lines = t.split("\n").map(function (s) { return s.trim(); });
    var lab = label.toLowerCase();
    for (var i = 0; i < lines.length; i++) {
      var low = lines[i].toLowerCase();
      if (low === lab || low.indexOf(lab + ":") === 0) {
        var tail = lines[i].slice(label.length).replace(/^[:\s]+/, "").trim();
        if (tail) { return tail; }
        var out = [];
        for (var k = i + 1; k < Math.min(i + 1 + (maxlines || 3), lines.length); k++) {
          if (!lines[k] || /:$/.test(lines[k])) { break; }
          out.push(lines[k]);
        }
        return out.join(" ").trim() || null;
      }
    }
    return null;
  }

  function num(s) {
    if (!s) { return null; }
    var x = String(s).match(/-?[\d][\d,]*\.?\d*/);
    return x ? parseFloat(x[0].replace(/,/g, "")) : null;
  }

  function b64(str) {
    var bytes = new TextEncoder().encode(str), bin = "";
    for (var i = 0; i < bytes.length; i++) { bin += String.fromCharCode(bytes[i]); }
    return btoa(bin);
  }

  fetch("/public/data/lotdetails/solr/lotImages/" + lot + "/USA")
    .then(function (r) { return r.json(); })
    .catch(function () { return null; })
    .then(function (j) {
      var raw = (((j || {}).data || {}).imagesList || {}).content || [];
      var shots = [];
      for (var i = 0; i < raw.length; i++) {
        var u = raw[i].highResUrl || raw[i].fullUrl;
        if (u && /\.(jpe?g|png)$/i.test(u)) {
          shots.push({ label: raw[i].imageLabelCode || "IMG", url: u });
        }
      }
      /* общий префикс ссылок выносим один раз — фрагмент URL короче втрое */
      var pref = "";
      if (shots.length) {
        pref = shots[0].url;
        for (var s = 1; s < shots.length; s++) {
          var a = pref, b = shots[s].url, n = 0;
          while (n < a.length && n < b.length && a[n] === b[n]) { n++; }
          pref = a.slice(0, n);
        }
      }

      var t = document.body.innerText;
      var title = t.split("\n").map(function (s) { return s.trim(); })
        .filter(function (l) { return /^(19|20)\d{2}\s+\S/.test(l); })[0] || null;
      var eng = after(t, "Engine type");
      if (eng) { eng = eng.replace(/\s*Listen to engine\s*$/, "").trim(); }
      var lm = eng && eng.match(/([\d.]+)\s*L/i);
      var odo = after(t, "Odometer");
      var bid = t.match(/Current bid[\s\S]{0,120}?\$([\d,]+(?:\.\d+)?)/);
      var fr = (after(t, "Fuel") || "").trim();
      var FUEL = { gas: "petrol", gasoline: "petrol", diesel: "diesel",
                   hybrid: "hybrid", electric: "ev" };
      var mi = num(odo);

      var data = {
        source: "copart", lot: lot, url: location.href, title: title,
        year: title ? +title.slice(0, 4) : null,
        vin: after(t, "VIN"),
        engine_raw: eng, engine_l: lm ? parseFloat(lm[1]) : null,
        fuel_raw: fr || null, fuel: FUEL[fr.toLowerCase()] || null,
        odometer_mi: mi, odometer_km: mi ? Math.round(mi * 1.609344) : null,
        odometer_actual: /actual/i.test(odo || ""),
        title_code: after(t, "Title code", 4),
        damage_primary: after(t, "Primary damage"),
        damage_secondary: after(t, "Secondary damage"),
        est_retail: num(after(t, "Estimated retail value")),
        current_bid: bid ? num(bid[1]) : null,
        has_keys: /^y/i.test(after(t, "Has key") || ""),
        runs_drives: /run and drive/i.test(t),
        location: after(t, "Location"), sale_date: after(t, "Sale date"),
        pref: pref,
        photos: shots.map(function (s) {
          return { label: s.label, tail: s.url.slice(pref.length) };
        })
      };

      var target = CALC + "#lot=" + b64(JSON.stringify(data));
      if (w) { w.location = target; } else { location.href = target; }
    });
})();
