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
    var series = [{ label: "Hora" }];
    var cols = [ts];
    var cores = ["#1d4ed8", "#067647", "#b54708", "#6941c6", "#b42318", "#0e7090"];
    nomes.forEach(function (n, i) {
      var mapa = {};
      dados[n].t.forEach(function (t, j) { mapa[t] = dados[n].y[j]; });
      cols.push(ts.map(function (t) { return t in mapa ? mapa[t] : null; }));
      series.push({ label: n, stroke: cores[i % cores.length], spanGaps: true });
    });
    new uPlot({ width: el.clientWidth || 800, height: 300, series: series,
      axes: [{}, { label: "Capital (R$)" }] }, cols, el);
  });
});
