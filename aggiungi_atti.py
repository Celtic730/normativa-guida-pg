"""Aggiunge atti nuovi ad atti.json cercandoli su Normattiva (tipo, numero, anno).
Uso: python aggiungi_atti.py "Legge|170|2010|dsa" "D.P.R.|249|1998|statuto studenti" ...  (tipo|numero|anno|titolo breve)
Poi: python aggiorna_normativa.py --solo ID,ID  per scaricare il testo."""
import json, os, sys
import requests

sys.stdout.reconfigure(encoding="utf-8")
API = "https://api.normattiva.it/t/normattiva.api/bff-opendata/v1/api/v1/ricerca/avanzata"
DEN = {"R.D.": "REGIO DECRETO", "D.P.R.": "DECRETO DEL PRESIDENTE DELLA REPUBBLICA", "D.Lgs.": "DECRETO LEGISLATIVO",
       "Legge": "LEGGE", "D.L.": "DECRETO-LEGGE", "D.M.": "DECRETO", "R.D.L.": "REGIO DECRETO-LEGGE"}
QUI = os.path.dirname(os.path.abspath(__file__))
P = os.path.join(QUI, "atti.json")
atti = json.load(open(P, encoding="utf-8"))
ids = {a["id"] for a in atti}
nuovi = []
for arg in sys.argv[1:]:
    tipo, num, anno, titolo = arg.split("|")
    body = {"denominazioneAtto": DEN[tipo], "numeroProvvedimento": num, "annoProvvedimento": anno, "orderType": "recente",
            "paginazione": {"paginaCorrente": 1, "numeroElementiPerPagina": 5}}
    r = requests.post(API, json=body, timeout=60)
    hit = [a for a in (r.json().get("listaAtti") or []) if a["numeroProvvedimento"] == num and a["annoProvvedimento"] == anno]
    if len(hit) != 1:
        print("NON TROVATO", arg, len(hit)); continue
    a = hit[0]; i = f"{anno}-{num}-{tipo.replace('.', '').lower()}"
    if i in ids:
        print("gia' presente", i); continue
    atti.append({"id": i, "titolo": titolo, "rif": a["descrizioneAtto"].title().replace("Del", "del"), "sigla": "",
                 "file": "", "codice": a["codiceRedazionale"], "dataGU": a["dataGU"], "descrizione": a["descrizioneAtto"]})
    nuovi.append(i); print("ok", i, a["codiceRedazionale"], a["dataGU"], a["descrizioneAtto"])
if nuovi:
    json.dump(atti, open(P, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("aggiunti:", ",".join(nuovi))
