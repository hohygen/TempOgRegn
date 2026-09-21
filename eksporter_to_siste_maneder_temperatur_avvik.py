"""Eksporter temperatur, nedbør og avvik for inneværende og forrige måned fra Frost.

Skriptet skriver én tekstfil per måned og ett felles interaktivt kart. Kartet har
en velger som lar brukeren bytte mellom de to månedene.

Kjoring i PowerShell:
    $env:FROST_CLIENT_ID = "din-klient-id"
    python C:/Users/hansoh/Documents/Pythonscripts/eksporter_to_siste_maneder_temperatur_avvik.py
"""

import base64
import json
import os
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlencode


FROST_URL = "https://frost.met.no"
USER_AGENT = "FrostTemperatureAnomalyMap/2.0 (local Python script)"
NORMALPERIODE = "1991/2020"
TEMPERATUR_ELEMENT = "mean(air_temperature P1M)"
NEDBOR_ELEMENT = "sum(precipitation_amount P1M)"
BATCH_SIZE = 100
KARTFIL = Path(__file__).with_name("to_siste_maneder_temperatur_og_avvik_kart.html")
MANEDSNAVN = (
    "januar", "februar", "mars", "april", "mai", "juni",
    "juli", "august", "september", "oktober", "november", "desember",
)


@dataclass(frozen=True)
class Maned:
    ar: int
    nummer: int

    @property
    def navn(self) -> str:
        return f"{MANEDSNAVN[self.nummer - 1]} {self.ar}"

    @property
    def periode(self) -> str:
        neste = self.neste()
        return f"{self.ar:04d}-{self.nummer:02d}-01/{neste.ar:04d}-{neste.nummer:02d}-01"

    @property
    def filstamme(self) -> str:
        return f"{MANEDSNAVN[self.nummer - 1]}_{self.ar}_temperatur_og_avvik"

    def neste(self) -> "Maned":
        return Maned(self.ar + 1, 1) if self.nummer == 12 else Maned(self.ar, self.nummer + 1)

    def forrige(self) -> "Maned":
        return Maned(self.ar - 1, 12) if self.nummer == 1 else Maned(self.ar, self.nummer - 1)


def frost_headers() -> dict[str, str]:
    klient_id = os.getenv("FROST_CLIENT_ID")
    if not klient_id:
        raise RuntimeError("Miljovariabelen FROST_CLIENT_ID mangler.")
    legitimasjon = base64.b64encode(f"{klient_id}:".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {legitimasjon}", "User-Agent": USER_AGENT}


def hent_json(url: str) -> dict:
    foresporsel = urllib.request.Request(url, headers=frost_headers())
    with urllib.request.urlopen(foresporsel, timeout=60) as respons:
        return json.load(respons)


def hent_alle_sider(url: str) -> list[dict]:
    data = []
    while url:
        respons = hent_json(url)
        data.extend(respons["data"])
        url = respons.get("nextLink")
    return data


def grupper(liste: list[str]) -> list[list[str]]:
    return [liste[indeks : indeks + BATCH_SIZE] for indeks in range(0, len(liste), BATCH_SIZE)]


def hent_stasjons_ider(element: str, maned: Maned) -> list[str]:
    parametere = urlencode({"referencetime": maned.periode, "elements": element})
    tidsserier = hent_alle_sider(f"{FROST_URL}/observations/availableTimeSeries/v0.jsonld?{parametere}")
    return sorted({serie["sourceId"].split(":")[0] for serie in tidsserier})


def hent_manedsverdier(stasjons_ider: list[str], element: str, maned: Maned) -> dict[str, float]:
    verdier = {}
    for gruppe in grupper(stasjons_ider):
        parametere = urlencode({"sources": ",".join(gruppe), "referencetime": maned.periode, "elements": element})
        for tidspunktdata in hent_alle_sider(f"{FROST_URL}/observations/v0.jsonld?{parametere}"):
            for observasjon in tidspunktdata["observations"]:
                if observasjon["elementId"] == element:
                    verdier[tidspunktdata["sourceId"].split(":")[0]] = float(observasjon["value"])
    return verdier


def hent_normaler(stasjons_ider: list[str], element: str, maned: Maned) -> dict[str, float]:
    normaler = {}
    for gruppe in grupper(stasjons_ider):
        parametere = urlencode({"sources": ",".join(gruppe), "elements": element, "period": NORMALPERIODE})
        for normal in hent_alle_sider(f"{FROST_URL}/climatenormals/v0.jsonld?{parametere}"):
            if normal.get("month") == maned.nummer and normal.get("normal") is not None:
                normaler[normal["sourceId"].split(":")[0]] = float(normal["normal"])
    return normaler


def hent_stasjonsmetadata(maned: Maned) -> dict[str, dict]:
    parametere = urlencode({"types": "SensorSystem", "validtime": maned.periode})
    metadata = {}
    for stasjon in hent_alle_sider(f"{FROST_URL}/sources/v0.jsonld?{parametere}"):
        koordinater = stasjon.get("geometry", {}).get("coordinates", [None, None])
        if koordinater[0] is not None and koordinater[1] is not None:
            metadata[stasjon["id"]] = {"navn": stasjon.get("shortName") or stasjon.get("name") or stasjon["id"], "lon": koordinater[0], "lat": koordinater[1]}
    return metadata


def lag_kartpunkter(temperaturer: dict[str, float], temperaturnormaler: dict[str, float], nedbor: dict[str, float], nedbornormaler: dict[str, float], metadata: dict[str, dict]) -> list[dict]:
    kartpunkter = []
    for stasjons_id in sorted(set(temperaturer) | set(nedbor)):
        if stasjons_id not in metadata:
            continue
        har_temperatur = stasjons_id in temperaturer and stasjons_id in temperaturnormaler
        har_nedbor = stasjons_id in nedbor and nedbornormaler.get(stasjons_id, 0) > 0
        if har_temperatur or har_nedbor:
            kartpunkter.append({"id": stasjons_id, **metadata[stasjons_id], "temperatur": temperaturer.get(stasjons_id), "temperaturnormal": temperaturnormaler.get(stasjons_id), "temperaturavvik": temperaturer[stasjons_id] - temperaturnormaler[stasjons_id] if har_temperatur else None, "nedbor": nedbor.get(stasjons_id), "nedbornormal": nedbornormaler.get(stasjons_id), "nedborprosent": nedbor[stasjons_id] / nedbornormaler[stasjons_id] * 100 if har_nedbor else None})
    return kartpunkter


def skriv_tekstfil(kartpunkter: list[dict], maned: Maned) -> Path:
    utdatafil = Path(__file__).with_name(f"{maned.filstamme}.txt")

    def format_verdi(verdi: float | None) -> str:
        return "" if verdi is None else f"{verdi:.1f}"

    with utdatafil.open("w", encoding="utf-8", newline="\n") as fil:
        fil.write("stasjonsID;stasjonsnavn;lengdegrad;breddegrad;temperatur_degC;temperaturnormal_degC;temperaturavvik_degC;nedbor_mm;nedbornormal_mm;nedbor_prosent_av_normal\n")
        for punkt in kartpunkter:
            fil.write(f"{punkt['id']};{punkt['navn']};{punkt['lon']:.5f};{punkt['lat']:.5f};{format_verdi(punkt['temperatur'])};{format_verdi(punkt['temperaturnormal'])};{format_verdi(punkt['temperaturavvik'])};{format_verdi(punkt['nedbor'])};{format_verdi(punkt['nedbornormal'])};{format_verdi(punkt['nedborprosent'])}\n")
    return utdatafil


def skriv_kart(datasett: list[dict]) -> None:
    data = json.dumps(datasett, ensure_ascii=False).replace("</", "<\\/")
    kjort_tid = json.dumps(datetime.now().astimezone().strftime("%d.%m.%Y kl. %H:%M:%S %Z"), ensure_ascii=False)
    html = '''<!doctype html>
<html lang="no"><head><meta charset="utf-8"><title>Temperatur og nedbør - to siste måneder</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>body{{margin:0;font-family:"Segoe UI",sans-serif}}#kart{{height:100vh}}.panel{{background:#fff;padding:10px 12px;line-height:1.45;box-shadow:0 1px 4px #777}}button{{border:1px solid #64748b;background:#fff;padding:6px 9px;cursor:pointer}}button.aktiv{{background:#0f766e;color:#fff}}</style></head>
<body><div id="kart"></div><script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>
const datasett=__DATASETT__;const kart=L.map("kart");let lag=L.layerGroup().addTo(kart);let modus="temperatur";let valgtManed=datasett[0].id;
L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{{z}}/{{y}}/{{x}}",{{maxZoom:19,attribution:"&copy; Esri, HERE, Garmin, (c) OpenStreetMap-bidragsytere"}}).addTo(kart);
function skala(verdier){{return [Math.min(...verdier),Math.max(...verdier)]}}function fargeTemperatur(verdi,min,max){{const a=max===min?.5:(verdi-min)/(max-min);return `hsl(${{240-a*240}},82%,46%)`}}function fargeAvvik(verdi,min,max){{const grense=Math.max(Math.abs(min),Math.abs(max))||1;const a=Math.min(Math.abs(verdi)/grense,1);return verdi>=0?`hsl(${{12+a*18}},85%,${{86-a*35}}%)`:`hsl(220,85%,${{86-a*35}}%)`}}function fargeNedbor(verdi,max){{const a=Math.max(0,Math.min(verdi/(max||1),1));return `hsl(220,100%,${{100-a*68}}%)`}}function fargeNedborprosent(verdi){{const a=Math.max(0,Math.min(verdi,200))/100;return a<=1?`hsl(${{30+a*30}},45%,${{35+a*65}}%)`:`hsl(220,100%,${{100-(a-1)*68}}%)`}}
const visninger={{temperatur:{{tittel:"Temperatur",felt:"temperatur",enhet:"grader C",farge:"temperatur"}},temperaturavvik:{{tittel:"Temperaturavvik",felt:"temperaturavvik",enhet:"grader C",farge:"avvik"}},nedbor:{{tittel:"Nedbør",felt:"nedbor",enhet:"mm",farge:"nedbor"}},nedboravvik:{{tittel:"Nedbør i prosent av normalen",felt:"nedborprosent",enhet:"prosent",farge:"nedborprosent"}}}};function visVerdi(verdi,enhet,fortegn=false){{return verdi===null?"Ikke tilgjengelig":`${{fortegn&&verdi>=0?"+":""}}${{verdi.toFixed(1)}} ${{enhet}`}}
function tegn(){{lag.clearLayers();const valgt=datasett.find(d=>d.id===valgtManed);const visning=visninger[modus];const gyldigePunkter=valgt.punkter.filter(p=>p[visning.felt]!==null);const verdier=gyldigePunkter.map(p=>p[visning.felt]);const [min,max]=skala(verdier);gyldigePunkter.forEach(p=>{{const verdi=p[visning.felt];const farge=visning.farge==="temperatur"?fargeTemperatur(verdi,min,max):visning.farge==="avvik"?fargeAvvik(verdi,min,max):visning.farge==="nedbor"?fargeNedbor(verdi,max):fargeNedborprosent(verdi);L.circleMarker([p.lat,p.lon],{{radius:6,color:"#1f2937",weight:1,fillColor:farge,fillOpacity:.9}}).bindPopup(`<strong>${{p.navn}}</strong> (${{p.id}})<br>Temperatur: ${{visVerdi(p.temperatur,"grader C")}}<br>Temperaturnormal: ${{visVerdi(p.temperaturnormal,"grader C")}}<br>Temperaturavvik: ${{visVerdi(p.temperaturavvik,"grader C",true)}}<br>Nedbør: ${{visVerdi(p.nedbor,"mm")}}<br>Nedbørsnormal: ${{visVerdi(p.nedbornormal,"mm")}}<br>Nedbør: ${{visVerdi(p.nedborprosent,"prosent av normalen")}}`).addTo(lag)}});document.querySelector("#forklaring").innerHTML=`<strong>${{valgt.navn}}: ${{visning.tittel}}</strong><br>${{gyldigePunkter.length}} stasjoner<br>Min: ${{min.toFixed(1)}} ${{visning.enhet}}<br>Maks: ${{max.toFixed(1)}} ${{visning.enhet}}`;document.querySelectorAll("button").forEach(b=>b.classList.toggle("aktiv",b.id===modus||b.id===`maned-${{valgtManed}}`))}}
const panel=L.control({{position:"topright"}});panel.onAdd=()=>{{const e=L.DomUtil.create("div","panel");e.innerHTML=`<div>${{datasett.map(d=>`<button id="maned-${{d.id}}">${{d.navn}}</button>`).join("")}}</div><div><button id="temperatur">Temperatur</button><button id="temperaturavvik">Temperaturavvik</button><button id="nedbor">Nedbør</button><button id="nedboravvik">Nedbørprosent</button></div><div id="forklaring"></div><div id="generert"></div>`;L.DomEvent.disableClickPropagation(e);return e}};panel.addTo(kart);document.querySelector("#generert").textContent=`Generert: ${__KJORT_TID__}`;document.addEventListener("click",e=>{{if(visninger[e.target.id]){{modus=e.target.id;tegn()}}else if(e.target.id.startsWith("maned-")){{valgtManed=e.target.id.slice(6);tegn()}}}});const allePunkter=datasett.flatMap(d=>d.punkter);kart.fitBounds(allePunkter.map(p=>[p.lat,p.lon]),{{padding:[25,25]}});tegn();
</script></body></html>'''
    html = html.replace("{{", "{").replace("}}", "}").replace("__DATASETT__", data).replace("__KJORT_TID__", kjort_tid)
    KARTFIL.write_text(html, encoding="utf-8")


def hent_datasett(maned: Maned) -> list[dict]:
    temperatur_stasjoner = hent_stasjons_ider(TEMPERATUR_ELEMENT, maned)
    nedbor_stasjoner = hent_stasjons_ider(NEDBOR_ELEMENT, maned)
    temperaturer = hent_manedsverdier(temperatur_stasjoner, TEMPERATUR_ELEMENT, maned)
    nedbor = hent_manedsverdier(nedbor_stasjoner, NEDBOR_ELEMENT, maned)
    kartpunkter = lag_kartpunkter(temperaturer, hent_normaler(list(temperaturer), TEMPERATUR_ELEMENT, maned), nedbor, hent_normaler(list(nedbor), NEDBOR_ELEMENT, maned), hent_stasjonsmetadata(maned))
    if not kartpunkter:
        raise RuntimeError(f"Fant ingen stasjoner med komplette kartdata for {maned.navn}.")
    utdatafil = skriv_tekstfil(kartpunkter, maned)
    print(f"Skrev {len(kartpunkter)} stasjoner til {utdatafil}")
    return {"id": f"{maned.ar}-{maned.nummer:02d}", "navn": maned.navn.capitalize(), "punkter": kartpunkter}


def main() -> None:
    idag = date.today()
    innevarende_maned = Maned(idag.year, idag.month)
    datasett = [hent_datasett(innevarende_maned), hent_datasett(innevarende_maned.forrige())]
    skriv_kart(datasett)
    print(f"Skrev kart til {KARTFIL}")
    webbrowser.open(KARTFIL.as_uri())


if __name__ == "__main__":
    main()