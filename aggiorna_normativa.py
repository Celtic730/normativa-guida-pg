"""Controlla su Normattiva gli atti di atti.json e aggiorna solo quelli cambiati.
Per ogni atto: legge la data «ultimo aggiornamento all'atto» dalla pagina; se e' cambiata (o manca il file) scarica il
testo vigente in Akoma Ntoso, lo divide in articoli e scrive dati/<id>.json. Alla fine scrive dati/manifest.json.
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
        try:
            if not hasattr(loc, "s"):
                attendi()
                loc.s = sessione()
            s = loc.s
            attendi()
            agg, url = pagina(s, atto)
            old = stato.get(atto["id"], {})
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
