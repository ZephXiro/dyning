# Dyning – södra Östersjön

Kartsida med DMI:s vågprognos (WAM, området *North Sea and Baltic*) för södra Östersjön:
dyningshöjd, -period och -riktning, total våghöjd, vindvågor och vind, timme för timme
ungefär 5 dygn framåt.

- `fetch.py` hämtar senaste kompletta modellkörning från [DMI Open Data](https://opendatadocs.dmi.govcloud.dk/Data/Forecast_Data_Wave_Model_WAM)
  och packar ett utsnitt (10–21,5° Ö, 53,8–58° N) till små binärfiler i `site/data/`.
- `site/index.html` är kartan (Leaflet + OpenStreetMap).
- `.github/workflows/update.yml` kör detta efter varje ny modellkörning och publicerar på GitHub Pages.

Köra lokalt:

```bash
pip install -r requirements.txt
python fetch.py
python -m http.server 8765 --directory site
```

Data: DMI (Danmarks Meteorologiske Institut), fri att använda med källangivelse.
