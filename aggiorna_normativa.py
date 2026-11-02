"""Controlla su Normattiva (atti italiani) e su EUR-Lex (regolamenti/direttive UE) gli atti di atti.json e aggiorna solo quelli cambiati.
Per ogni atto italiano: legge la data «ultimo aggiornamento all'atto» dalla pagina; se e' cambiata (o manca il file) scarica il
testo vigente in Akoma Ntoso, lo divide in articoli e scrive dati/<id>.json. Per un atto UE (atti.json: "fonte":"eurlex",
"codice" = numero CELEX, es. "32016R0679" per il GDPR): scarica la pagina EUR-Lex in italiano e divide il testo in articoli
cercando le righe "Articolo N"; riscrive il file solo se il contenuto e' cambiato (confronto per hash, non c'e' una data
comoda come su Normattiva). Alla fine scrive dati/manifest.json.
Uso: python aggiorna_normativa.py [--force] [--solo ID,ID] [--workers N]
(--workers: N atti in parallelo, ma sempre al massimo 1 richiesta al secondo in totale; predefinito 1)"""
import datetime
import hashlib
import json
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup

sys.stdout.reconfigure(encoding="utf-8")
QUI = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(QUI, os.environ.get("NORMATIVA_OUT", "dati"))
STATO = os.path.join(QUI, "stato.json")
NS = {"a": "http://docs.oasis-open.org/legaldocml/ns/akn/3.0"}
BASE = "https://www.normattiva.it"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
      "Accept-Language": "it-IT,it;q=0.9"}
BLOCK = {"paragraph", "point", "list", "item", "blockList", "intro", "wrapUp", "p"}
PAUSA = 1.0  # intervallo minimo tra due richieste a Normattiva, comune a tutti i worker
_ultimo = [0.0]
_lock_rate = threading.Lock()


def attendi():
    with _lock_rate:
        t = max(0.0, _ultimo[0] + PAUSA - time.time())
        _ultimo[0] = time.time() + t
    time.sleep(t)


def tag(e):
    return e.tag.split("}")[1]


def tx(e, top=False):
    """Testo di un elemento; sul nodo articolo salta numero e rubrica (restano quelli dei commi: «1.», «2.»...)."""
    sp = lambda t: re.sub(r"\s+", " ", t or "")  # a capo e rientri dell'XML non sono a capo del testo
    s = sp(e.text)
    for c in e:
        if top and tag(c) in ("num", "heading"):
            s += sp(c.tail)
            continue
        s += tx(c) + (" " if tag(c) == "num" else "") + sp(c.tail)
    return "\n" + s + "\n" if tag(e) in BLOCK else s


def pulisci(t):
    righe = [re.sub(r"[ \t ]+", " ", r).strip() for r in t.split("\n")]
    out, pend = [], ""
    for r in righe:
        if not r:
            continue
        if len(r) <= 9 and re.fullmatch(r"\(?[0-9a-z]+(?:-[a-z]+)?[.)]?\.?", r):  # solo numero di comma/lettera: unisci al testo
            pend += r + " "
            continue
        out.append(pend + r)
        pend = ""
    if pend:
        out.append(pend.strip())
    return "\n".join(out)


def articoli(root):
    out = []
    for a in root.iter("{%s}article" % NS["a"]):
        num = a.find("a:num", NS)
        head = a.find("a:heading", NS)
        n = re.sub(r"^\s*(?:articolo|art\.?)\s*", "", " ".join(num.itertext()).strip(), flags=re.I).strip(" .") if num is not None else ""
        r = re.sub(r"\s+", " ", " ".join(head.itertext())).strip() if head is not None else ""
        t = pulisci(tx(a, True))
        if t:
            out.append({"id": a.get("eId", ""), "n": n, "r": r, "t": t})
    # Codici e testi unici: gli articoli stanno negli annessi («Codice Penale-art. 337 bis»), uno per annesso
    for i, att in enumerate(root.iter("{%s}attachment" % NS["a"])):
        d = att.find("a:doc", NS)
        mb = d.find("a:mainBody", NS) if d is not None else None
        if mb is None:
            continue
        nome = d.get("name", "")
        m = re.search(r"-\s*art(?:icolo|\.)?\s*(.+)$", nome, flags=re.I)
        n = re.sub(r"\s+", "-", m.group(1).strip(" .")) if m else nome
        t = pulisci(tx(mb, True))
        parti = re.split(r"(?m)^-{5,}\s*$", t, maxsplit=1)
        corpo, note = parti[0], (parti[1].strip() if len(parti) > 1 else "")
        corpo = re.sub(r"^Art\.?\s*[0-9A-Za-z\-\s]*?\.\s*", "", corpo, count=1)
        r = ""
        mr = re.match(r"\(\(\s*\(([^()]{3,160})\)\.?\s*\)\)\s*", corpo) or             re.match(r"\(([^()]{3,160}(?:\([^()]*\)[^()]*)?)\)\s*", corpo)
        if mr and not re.match(r"Art", mr.group(1)):
            r, corpo = mr.group(1).strip(), corpo[mr.end():]
        if corpo.strip():
            e = {"id": f"allegato_{i}", "n": n, "r": r, "t": corpo.strip()}
            if note:
                e["note"] = note[:2500]
            out.append(e)
    return out


def sessione():
    s = requests.Session()
    s.headers.update(UA)
    s.get(BASE + "/", timeout=60)
    return s


def pagina(s, atto):
    url = (f"{BASE}/atto/caricaDettaglioAtto?atto.dataPubblicazioneGazzetta={atto['dataGU']}"
           f"&atto.codiceRedazionale={atto['codice']}&tipoDettaglio=multivigenza&qId=&classica=true")
    r = s.get(url, timeout=120)
    r.raise_for_status()
    m = re.search(r"Ultimo aggiornamento all(?:&#39;|')atto pubblicato il (\d\d)/(\d\d)/(\d{4})", r.text)
    agg = f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None
    return agg, url


def akn(s, atto, referer):
    d = atto["dataGU"].replace("-", "")
    oggi = datetime.date.today().strftime("%Y%m%d")
    r = s.get(f"{BASE}/do/atto/caricaAKN?dataGU={d}&codiceRedaz={atto['codice']}&dataVigenza={oggi}",
              headers={"Referer": referer}, timeout=300)
    r.raise_for_status()
    if b"<akomaNtoso" not in r.content[:2000]:
        raise RuntimeError("risposta non Akoma Ntoso")
    return ET.fromstring(r.content)


def eurlex_pagina(s, celex):
    """Pagina HTML in italiano di un atto UE (regolamento, direttiva...) dato il numero CELEX (es. 32016R0679)."""
    r = s.get(f"https://eur-lex.europa.eu/legal-content/IT/TXT/HTML/?uri=CELEX:{celex}", timeout=120)
    r.raise_for_status()
    return r.text


FINE_ARTICOLATO = re.compile(r"(?m)^Fatto a |^Il presente (?:regolamento|atto) è obbligatorio|^La presente direttiva è obbligatoria")


def eurlex_articoli(html):
    """Divide in articoli il testo di una pagina EUR-Lex (cerca le righe «Articolo N», come in Normattiva)."""
    testo = BeautifulSoup(html, "html.parser").get_text("\n")
    testo = re.sub(r"[ \t]+", " ", testo).replace("\xa0", " ")
    testo = re.sub(r"\n{3,}", "\n\n", testo)
    hit = [(m.start(), m.end(), m.group(1)) for m in re.finditer(r"(?m)^Articolo\s+([0-9]+[a-z]?)\s*$", testo)]
    if not hit:
        raise RuntimeError("nessun 'Articolo N' trovato nella pagina EUR-Lex")
    fm = FINE_ARTICOLATO.search(testo, hit[-1][1])
    fine = fm.start() if fm else len(testo)
    out = []
    for i, (_, hstart, n) in enumerate(hit):
        fine_art = hit[i + 1][0] if i + 1 < len(hit) else fine
        righe = [r for r in testo[hstart:fine_art].strip("\n ").split("\n") if r.strip()]
        out.append({"id": f"art{n}", "n": n, "r": righe[0].strip() if righe else "", "t": "\n".join(righe[1:]).strip()})
    return out


def main():
    force = "--force" in sys.argv
    solo = None
    if "--solo" in sys.argv:
        solo = set(sys.argv[sys.argv.index("--solo") + 1].split(","))
    atti = json.load(open(os.path.join(QUI, "atti.json"), encoding="utf-8"))
    stato = json.load(open(STATO, encoding="utf-8")) if os.path.exists(STATO) else {}
    os.makedirs(OUT, exist_ok=True)
    workers = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 1
    loc = threading.local()
    lock = threading.Lock()
    manifest, aggiornati, errori = [], [], []

    def elabora(atto):
        """Scarica un atto (sessione propria per thread); l'intervallo tra richieste e' comune a tutti i worker."""
        fn = os.path.join(OUT, atto["id"] + ".json")
        if atto.get("manuale") and os.path.exists(fn):  # atto non su Normattiva: file curato a mano
            data = json.load(open(fn, encoding="utf-8"))
            return {"id": data["id"], "titolo": data["titolo"], "rif": data["rif"], "sigla": data["sigla"],
                    "aggiornato": data["aggiornato"], "hash": data["hash"], "articoli": len(data["articoli"]),
                    "file": data["id"] + ".json", "kb": os.path.getsize(fn) // 1024}
        try:
            if not hasattr(loc, "s"):
                attendi()
                loc.s = sessione()
            s = loc.s
            old = stato.get(atto["id"], {})
            if atto.get("fonte") == "eurlex":  # atto UE: EUR-Lex non ha una data comoda come Normattiva, si confronta l'hash
                attendi()
                arts = eurlex_articoli(eurlex_pagina(s, atto["codice"]))
                h = hashlib.sha256(json.dumps(arts, ensure_ascii=False).encode()).hexdigest()[:16]
                agg = old.get("aggiornato") if old.get("hash") == h else datetime.date.today().isoformat()
                if not force and os.path.exists(fn) and old.get("hash") == h:
                    print(f"=  {atto['rif']}: invariato (EUR-Lex)")
                    data = json.load(open(fn, encoding="utf-8"))
                else:
                    data = {"id": atto["id"], "titolo": atto["titolo"], "rif": atto["rif"], "sigla": atto["sigla"],
                            "aggiornato": agg, "hash": h, "articoli": arts}
                    json.dump(data, open(fn, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
                    with lock:
                        stato[atto["id"]] = {"aggiornato": agg, "hash": h}
                        aggiornati.append(atto["rif"])
                    print(f"+  {atto['rif']}: {len(arts)} articoli (EUR-Lex), aggiornato al {agg}")
                return {"id": data["id"], "titolo": data["titolo"], "rif": data["rif"], "sigla": data["sigla"],
                        "aggiornato": data["aggiornato"], "hash": data["hash"], "articoli": len(data["articoli"]),
                        "file": data["id"] + ".json", "kb": os.path.getsize(fn) // 1024}
            attendi()
            agg, url = pagina(s, atto)
            if not force and os.path.exists(fn) and agg and old.get("aggiornato") == agg:
                print(f"=  {atto['rif']}: invariato ({agg})")
                data = json.load(open(fn, encoding="utf-8"))
            else:
                attendi()
                root = akn(s, atto, url)
                arts = articoli(root)
                h = hashlib.sha256(json.dumps(arts, ensure_ascii=False).encode()).hexdigest()[:16]
                data = {"id": atto["id"], "titolo": atto["titolo"], "rif": atto["rif"], "sigla": atto["sigla"],
                        "aggiornato": agg, "hash": h, "articoli": arts}
                json.dump(data, open(fn, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
                with lock:
                    stato[atto["id"]] = {"aggiornato": agg, "hash": h}
                    aggiornati.append(atto["rif"])
                print(f"+  {atto['rif']}: {len(arts)} articoli, aggiornato al {agg}")
        except Exception as e:  # un atto che fallisce non blocca gli altri
            with lock:
                errori.append((atto["rif"], str(e)[:120]))
            print(f"!! {atto['rif']}: {e}")
            if not os.path.exists(fn):
                return None
            data = json.load(open(fn, encoding="utf-8"))
        return {"id": data["id"], "titolo": data["titolo"], "rif": data["rif"], "sigla": data["sigla"],
                "aggiornato": data["aggiornato"], "hash": data["hash"], "articoli": len(data["articoli"]),
                "file": data["id"] + ".json", "kb": os.path.getsize(fn) // 1024}

    lista = [a for a in atti if not solo or a["id"] in solo]
    with ThreadPoolExecutor(max(1, workers)) as ex:  # map mantiene l'ordine di atti.json
        manifest = [m for m in ex.map(elabora, lista) if m]
    json.dump(stato, open(STATO, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if not solo:
        json.dump({"generato": datetime.date.today().isoformat(), "atti": manifest},
                  open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"\naggiornati {len(aggiornati)}, errori {len(errori)}")
    for e in errori:
        print("  ERRORE", e)
    return 1 if errori else 0


if __name__ == "__main__":
    sys.exit(main())
