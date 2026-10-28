// Painel robowin: confirmação do kill switch e gráficos uPlot (dados locais, sem CDN).
document.addEventListener("submit", function (e) {
  var msg = e.target.getAttribute && e.target.getAttribute("data-confirmar");
  if (msg && !window.confirm(msg)) e.preventDefault();
});
document.addEventListener("DOMContentLoaded", function () {
  var el = document.getElementById("grafico-capital");
  if (!el || !window.uPlot) return;
  fetch(el.dataset.url).then(function (r) { return r.json(); }).then(function (dados) {
    var nomes = Object.keys(dados);
    if (!nomes.length) { document.getElementById("capital-vazio").hidden = false; return; }
    var ts = [];
    nomes.forEach(function (n) { dados[n].t.forEach(function (t) { ts.push(t); }); });
    ts = Array.from(new Set(ts)).sort(function (a, b) { return a - b; });
    // rotulos em pt-BR e em UTC (como o restante do painel); o padrao do uPlot e en-US (am/pm) e fuso do navegador
    var fmtEixo = new Intl.DateTimeFormat("pt-BR", { timeZone: "UTC", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
    var fmtCompleto = new Intl.DateTimeFormat("pt-BR", { timeZone: "UTC", dateStyle: "short", timeStyle: "medium", hour12: false });
    var series = [{ label: "Hora (UTC)", value: function (u, v) { return v == null ? "--" : fmtCompleto.format(new Date(v * 1000)); } }];
    var cols = [ts];
    var cores = ["#1d4ed8", "#067647", "#b54708", "#6941c6", "#b42318", "#0e7090"];
    nomes.forEach(function (n, i) {
      var mapa = {};
      dados[n].t.forEach(function (t, j) { mapa[t] = dados[n].y[j]; });
      cols.push(ts.map(function (t) { return t in mapa ? mapa[t] : null; }));
      series.push({ label: n, stroke: cores[i % cores.length], spanGaps: true });
    });
    new uPlot({ width: el.clientWidth || 800, height: 300, series: series,
      axes: [{ values: function (u, ticks) { return ticks.map(function (v) { return fmtEixo.format(new Date(v * 1000)); }); } },
        { label: "Capital (R$)" }] }, cols, el);
  });
});
