# italia-notes

Panoramica delle note OpenStreetMap aperte in Italia, per regione.

**Sito:** https://ilselvaggio.github.io/italia-notes/

- Elenco regioni con numero di note aperte, colorato per densità (note / 100 km²) e tendenza a 7 giorni
- Per ogni regione: note aperte colorate per età e note chiuse negli ultimi 30 giorni
- Filtro e download GeoJSON per fascia di età

Dati: [OSM Notes API](https://wiki.openstreetmap.org/wiki/API_v0.6#Map_Notes_API), aggiornati ogni notte tramite GitHub Actions (`update.py`).
Le note in mare vicino alla costa (entro ~3 km) vengono assegnate alla regione più vicina.

© OpenStreetMap contributors, ODbL.
