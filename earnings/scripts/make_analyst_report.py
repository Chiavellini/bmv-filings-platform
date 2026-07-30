#!/usr/bin/env python3
"""make_analyst_report.py — reporte simple en español para los analistas.

Emite outputs/reporte_analistas.html (cuerpo de artifact, sin doctype/head).
Lee únicamente results_v3/simple_*.csv + analyst_eval_company.csv.
DIAGNÓSTICO solamente — nada de esto es una estrategia operada.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs

import pandas as pd

sys.path.insert(0, str(bs.EARNINGS_ROOT / "scripts"))
from make_dashboard import esc  # noqa: E402
from make_dissection_report import bars_f  # noqa: E402

V3 = bs.OUTPUTS_DIR / "results_v3"


def tabla(df: pd.DataFrame, cols: dict[str, str]) -> str:
    """Tabla HTML con encabezados en español (cols: col_origen -> etiqueta)."""
    head = "".join(f"<th>{esc(v)}</th>" for v in cols.values())
    rows = []
    for _, r in df.iterrows():
        tds = []
        for c in cols:
            v = r[c]
            if isinstance(v, float):
                v = "" if pd.isna(v) else (f"{v:.1f}%" if c in ("pct", "cobertura_pct") else f"{v:g}")
            tds.append(f"<td>{esc(v)}</td>")
        rows.append("<tr>" + "".join(tds) + "</tr>")
    return ('<div class="tblwrap"><table><thead><tr>' + head +
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")


def main() -> None:
    assert bs.V3, "set EARNINGS_V3=1"
    conv = pd.read_csv(V3 / "simple_conviction.csv")
    h2h = pd.read_csv(V3 / "simple_headtohead.csv")
    ens = pd.read_csv(V3 / "simple_ensemble.csv")
    sector = pd.read_csv(V3 / "simple_sector.csv")
    dirmag = pd.read_csv(V3 / "dirmag_grid.csv")
    sc = pd.read_csv(V3 / "simple_sector_conviction.csv")
    traits = pd.read_csv(V3 / "simple_traits.csv")
    comp = pd.read_csv(V3 / "analyst_eval_company.csv")

    a = conv[conv["cohorte"] == "analista"].set_index("grupo")
    m = conv[conv["cohorte"] == "modelo"].set_index("grupo")
    mj = conv[conv["cohorte"] == "modelo_emparejado"].set_index("grupo")
    cols = {"grupo": "grupo", "n": "llamadas", "aciertos": "aciertos",
            "pct": "% de acierto"}

    chart_conv = bars_f(
        ["todas (n=67)", "convicción fuerte (n=27)", "convicción suave (n=40)",
         "azar"],
        [68.7, 92.6, 52.5, 50.0],
        ["var(--pos)", "var(--pos)", "var(--pos)", "var(--muted)"],
        unit="% de acierto", fmt="{:.1f}%")

    h2h_a, h2h_m = float(h2h["pct"].iloc[0]), float(h2h["pct"].iloc[1])
    n_hh = int(h2h["n"].iloc[0])
    chart_ens = bars_f(
        [f"ustedes (n={n_hh})", f"modelo, mismos eventos (n={n_hh})",
         "cuando coinciden (n=44)", "convicción fuerte manda (n=73)"],
        [h2h_a, h2h_m, 70.5, 64.4],
        ["var(--pos)", "var(--muted)", "var(--acc)", "var(--acc)"],
        unit="% de acierto", fmt="{:.1f}%")

    sec_a = sector[sector["cohorte"] == "analista"]
    sec_a4 = sec_a[sec_a["n"] >= 4]
    chart_sec = bars_f(
        [g.replace("Consumer - ", "").replace("Transport - ", "")
         .replace("Real Estate - Hotels & Hospitality", "Hoteles (FIBRAs)")
         for g in sec_a4["grupo"]],
        list(sec_a4["pct"]),
        ["var(--pos)"] * len(sec_a4), unit="% de acierto", fmt="{:.0f}%")

    comp4 = comp[comp["n"] >= 4][["ticker", "n", "hits", "hit_rate"]].copy()
    comp4["hit_rate"] = (100 * comp4["hit_rate"]).round(1)
    comp4.columns = ["grupo", "n", "aciertos", "pct"]

    dm = dirmag[(dirmag["sleeve"] == "full-strength")
                & (dirmag["cost_bps"] == 25)].copy()
    dm["pct"] = (100 * dm["hit_rate"]).round(0)
    dm["metodo"] = dm["sizing"].map({
        "equal": "tamaño fijo (tope 10%)",
        "model": "según la señal del modelo",
        "vol": "según la volatilidad del papel",
        "kelly_class": "Kelly por convicción (satura el tope)"})
    dm = dm[["metodo", "trades", "bps_per_trade", "pct", "sharpe_calendar"]]
    tabla_dm = tabla(dm, {"metodo": "tamaño por", "trades": "operaciones",
                          "bps_per_trade": "pb por operación",
                          "pct": "tasa de acierto",
                          "sharpe_calendar": "sharpe (25 pb)"})

    # sector x conviction, wide: "aciertos/n (pct)" per class
    def cell(r):
        return f"{int(r['aciertos'])}/{int(r['n'])} ({r['pct']:.0f}%)"
    scw = sc.assign(txt=sc.apply(cell, axis=1)).pivot(
        index="sector", columns="clase", values="txt").fillna("—")
    scw = scw.reset_index()[["sector", "fuerte", "suave"]]

    body = f"""
<style>
:root {{
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,.10);
  --pos:#2a78d6; --acc:#eb6834; --good:#006300;
  --emph:rgba(42,120,214,.07);
}}
@media (prefers-color-scheme: dark) {{
  :root:where(:not([data-theme="light"])) {{
    --page:#0d0d0d; --surface:#1a1a19; --ink:#ffffff; --ink2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
    --pos:#3987e5; --acc:#d95926; --good:#0ca30c;
    --emph:rgba(57,135,229,.10);
  }}
}}
:root[data-theme="dark"] {{
  --page:#0d0d0d; --surface:#1a1a19; --ink:#ffffff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
  --pos:#3987e5; --acc:#d95926; --good:#0ca30c;
  --emph:rgba(57,135,229,.10);
}}
:root[data-theme="light"] {{
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,.10);
  --pos:#2a78d6; --acc:#eb6834; --good:#006300;
  --emph:rgba(42,120,214,.07);
}}
body {{ background:var(--page); color:var(--ink);
  font:15.5px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif; margin:0; }}
main {{ max-width:900px; margin:0 auto; padding:40px 22px 80px; }}
h1 {{ font-size:26px; line-height:1.25; margin:6px 0 4px; letter-spacing:-.01em; text-wrap:balance; }}
h2 {{ font-size:19px; margin:42px 0 6px; }}
.eyebrow {{ font-size:11px; font-weight:600; letter-spacing:.09em; text-transform:uppercase; color:var(--muted); }}
p {{ color:var(--ink2); max-width:70ch; }}
p b, li b {{ color:var(--ink); }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; margin:18px 0 8px; }}
.tile {{ background:var(--surface); border:1px solid var(--ring); border-radius:10px; padding:14px 16px 12px; }}
.tile .v {{ font-size:28px; font-weight:650; }}
.tile .v.pos {{ color:var(--pos); }} .tile .v.acc {{ color:var(--acc); }}
.tile .k {{ font-size:12.5px; color:var(--muted); margin-top:2px; }}
.card {{ background:var(--surface); border:1px solid var(--ring); border-radius:10px; padding:16px 18px; margin:14px 0; }}
.tblwrap {{ overflow-x:auto; }}
table {{ border-collapse:collapse; width:100%; font-size:14px; font-variant-numeric:tabular-nums; }}
th {{ text-align:left; font-size:11.5px; letter-spacing:.05em; text-transform:uppercase;
  color:var(--muted); font-weight:600; padding:7px 10px; border-bottom:1px solid var(--axis); white-space:nowrap; }}
td {{ padding:6px 10px; border-bottom:1px solid var(--grid); white-space:nowrap; }}
tr:last-child td {{ border-bottom:none; }}
tbody tr:hover td {{ background:var(--emph); }}
.gridline {{ stroke:var(--grid); stroke-width:1; }}
.axisline {{ stroke:var(--axis); stroke-width:1.25; }}
.ticklab {{ font:11px system-ui,sans-serif; fill:var(--muted); }}
.vallab {{ font:12px system-ui,sans-serif; fill:var(--ink2); font-variant-numeric:tabular-nums; }}
ul li {{ margin:6px 0; color:var(--ink2); }}
.note {{ font-size:13px; color:var(--muted); }}
</style>
<title>Reporte para analistas — aciertos por convicción</title>
<main>
<div class="eyebrow">reportes trimestrales BMV · 2T24 – 2T26</div>
<h1>¿Qué tan bien predicen ustedes la reacción del mercado al día siguiente del reporte?</h1>
<p>Evaluamos sus llamadas ("Positive", "Negative", "N to P", "N to N") contra lo que hizo la acción
el día hábil siguiente a cada reporte trimestral. <b>Una llamada acierta si la acción le ganó al IPC
ese día, en la dirección que ustedes dijeron.</b> Se evaluaron 67 llamadas direccionales que
coinciden con un reporte en nuestra base (algunos trimestres están reservados para pruebas y se
excluyen). Generado el {date.today().isoformat()}.</p>

<div class="tiles">
  <div class="tile"><div class="v pos">92.6%</div>
    <div class="k">acierto cuando dicen "Positive" o "Negative" a secas (25 de 27)</div></div>
  <div class="tile"><div class="v">52.5%</div>
    <div class="k">acierto en llamadas suaves "N to P" / "N to N" (21 de 40) — como un volado</div></div>
  <div class="tile"><div class="v acc">70.5%</div>
    <div class="k">acierto cuando ustedes y el modelo coinciden (31 de 44)</div></div>
</div>

<h2>1 · Sus aciertos, por nivel de convicción</h2>
<p>La señal más clara de todo el análisis: <b>su nivel de convicción distingue perfectamente</b>.
Las llamadas fuertes aciertan casi siempre; las suaves, no más que el azar. Las bajistas
(78.6%) aciertan más que las alcistas (61.5%).</p>
<div class="card">{chart_conv}</div>
<div class="card">{tabla(a.reset_index(), cols)}</div>

<h2>2 · El modelo, con la misma vara — y en los mismos eventos</h2>
<p>La comparación justa es sobre <b>los mismos reportes que ustedes llamaron</b>, no sobre todo el
universo. En esos {n_hh} eventos idénticos: <b>ustedes {h2h_a:.1f}% · modelo {h2h_m:.1f}%</b>.
El modelo (sorpresa del reporte vs la historia de cada empresa) le gana al azar, pero queda ~10
puntos debajo de ustedes — y a más de 30 de sus llamadas fuertes.</p>
<div class="card"><div class="eyebrow">modelo · en sus empresas-trimestre (comparable con la
tabla de arriba)</div>{tabla(mj.reset_index(), cols)}
<p class="note">Ojo: en sus empresas la "convicción" del modelo se invierte (53.3% fuerte vs 62.8%
débil, con n de 30/43 — puede ser ruido). Su punto fuerte ahí son las señales bajistas (75%,
n=24), igual que ustedes.</p></div>
<p class="note">Contexto: sobre el universo completo (484 reportes con liquidez suficiente) el
modelo acierta 58.5%, casi lo mismo que en sus empresas — su desempeño es estable, y su
"convicción" tampoco ayuda ahí (57.9% fuerte vs 59.1% débil).</p>

<h2>3 · Ustedes + el modelo (ensamble)</h2>
<p>Dos formas sencillas de combinar. <b>"Cuando coinciden"</b>: solo operar los casos donde ustedes y
el modelo apuntan en la misma dirección (pasa el 66% de las veces). <b>"Convicción fuerte manda"</b>:
se sigue su llamada fuerte cuando la hay; si no, la del modelo — así siempre hay una llamada.</p>
<div class="card">{chart_ens}</div>
<div class="card">{tabla(ens, {**cols, "cobertura_pct": "% de los casos que cubre"})}</div>
<p class="note">Con 44 casos, el 70.5% del ensamble no es estadísticamente distinguible del 68.7%
de ustedes solos — se necesitan más trimestres para saber si la combinación de verdad suma.</p>

<h2>3b · ¿Y si ustedes ponen la dirección y el sistema el tamaño?</h2>
<p>Lo probamos: operar <b>cada llamada fuerte de ustedes, en su dirección</b>, y dejar que el
sistema decida cuánto capital ponerle a cada operación. Resultado neto de costos:
<b>+302 puntos base por operación (27 operaciones, 85% de acierto)</b> — el doble de cualquier
versión del modelo solo. Pero el hallazgo fino es que <b>todo el mérito es de la dirección de
ustedes</b>: barajar las direcciones destruye el resultado (p=0.001), mientras que barajar los
tamaños no cambia nada (p=1.0). Dimensionar por la señal del modelo o por la volatilidad del papel
solo reduce la exposición; con su tasa de acierto, la talla óptima ya está en el tope de riesgo
por posición (10%).</p>
<div class="card"><div class="eyebrow">llamadas fuertes · dirección de ustedes · neto de 25 pb ·
por método de tamaño</div>
{tabla_dm}
<p class="note">27 operaciones es poco — el número luce enorme, pero se confirma de verdad hasta
el 3T26, con las llamadas registradas ANTES de cada reporte (la regla exacta ya quedó escrita en
el estudio). Nada de esto se opera hoy.</p></div>

<h2>4 · Dónde les cuesta más</h2>
<p>A primera vista hay "sectores malos" (hoteles 25%, salud 33%). Pero al separar por convicción,
<b>la debilidad sectorial casi desaparece: las llamadas fuertes aciertan prácticamente en todos los
sectores</b> (25 de 27; los únicos 2 fallos: OMA y CHDRAUI, 1T25). Los sectores flojos lo son porque
ahí sus llamadas fueron mayormente suaves — en hoteles, por ejemplo, <b>no hubo ni una llamada
fuerte</b>. La conclusión operativa no es "eviten hoteles", sino "las llamadas suaves no aportan,
en ningún sector".</p>
<div class="card"><div class="eyebrow">por sector y convicción · aciertos/llamadas (%)</div>
{tabla(scw, {"sector": "sector", "fuerte": "convicción fuerte", "suave": "convicción suave"})}</div>
<div class="card">{chart_sec}</div>
<div class="card"><div class="eyebrow">todos los sectores (llamadas fuertes y suaves juntas)</div>
{tabla(sec_a, cols)}</div>
<div class="card"><div class="eyebrow">por empresa (4+ llamadas evaluadas)</div>
{tabla(comp4, {"grupo": "empresa", "n": "llamadas", "aciertos": "aciertos", "pct": "% de acierto"})}
<p class="note">Con 3–5 llamadas por empresa, un solo error mueve mucho el porcentaje — tómenlo
como indicativo. Los únicos 2 fallos de convicción fuerte fueron OMA y CHDRAUI (1T25, ambas
"Positive" con reacción levemente negativa).</p></div>
<div class="card"><div class="eyebrow">por liquidez de la acción</div>
{tabla(traits[traits["cohorte"] == "analista"], cols)}
<p class="note">Sí: que "media" y "alta" den exactamente 16/22 es coincidencia — son 22 eventos
distintos en cada grupo (verificado evento por evento; cortes en ~$50M y ~$130M de operación
diaria).</p></div>

<h2>5 · Dónde le cuesta más al modelo</h2>
<p>El modelo es débil exactamente donde ustedes no: <b>aeropuertos (47.1%)</b> — donde ustedes
aciertan 71.4% — y <b>acciones muy líquidas (50.9%)</b>, donde el mercado ya incorpora la sorpresa
más rápido. Ustedes flojean en baja liquidez; el modelo en alta: son complementarios.</p>
<div class="card"><div class="eyebrow">modelo · por sector (solo empresas que ustedes cubren)</div>
{tabla(sector[sector["cohorte"] == "modelo"], cols)}</div>
<div class="card"><div class="eyebrow">modelo · por liquidez</div>
{tabla(traits[traits["cohorte"] == "modelo"], cols)}</div>

<h2>6 · Letra chica</h2>
<ul>
<li><b>Muestras chicas.</b> 67 llamadas direccionales en total; los cortes por sector/empresa tienen
3–22 casos. Un acierto más o menos cambia varios puntos porcentuales.</li>
<li><b>Esto es un diagnóstico, no una estrategia.</b> Ninguna de estas combinaciones se opera hoy;
cualquier uso en el proceso de inversión pasa primero por una prueba fuera de muestra en 3T26.</li>
<li><b>El 92.6% se confirma hacia adelante.</b> A partir del 3T26 conviene registrar cada llamada
con fecha y hora ANTES de cada reporte — con eso el siguiente trimestre valida el número de
manera inobjetable.</li>
<li><b>Qué es "ganarle al IPC":</b> el rendimiento de la acción al cierre del día hábil siguiente al
reporte, menos el del índice. Los cortes por sector y liquidez son análisis posteriores al plan
original del estudio.</li>
</ul>
</main>
"""
    out = bs.OUTPUTS_DIR / "reporte_analistas.html"
    out.write_text(body)
    print(f"wrote {out} ({len(body)//1024} KB)")


if __name__ == "__main__":
    main()
