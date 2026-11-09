# Meteo Locale — previsioni iper-locali per Roma e il Lazio

Previsioni di temperatura, umidità e vento **punto per punto** sul Lazio, fino a 48 ore.
Il sistema parte dalle previsioni dei modelli globali e le corregge con le osservazioni reali
delle stazioni sul territorio, per cogliere i microclimi che una griglia da 10–25 km non vede:
l'isola di calore del centro, la brezza di Ostia, le inversioni notturne nelle valli.

- **Mappa live:** <https://filippopetto-maker.github.io/meteo_locale/>
  (temperatura, umidità e vento, ora per ora fino a +48 h, con scheda per ogni luogo)
- **Dashboard dei risultati:** <https://filippopetto-maker.github.io/meteo_locale/dashboard.html>
  (previsto contro osservato per stazione, errore per anticipo da 1 a 48 h)

## Risultati

Il modello corretto (M2) è in produzione dal **31/10/2026** e si aggiorna ogni ora.
Errore medio assoluto sulle stazioni, anticipi da 1 a 48 ore:

| Grandezza | Modello globale grezzo (ECMWF IFS) | Meteo Locale (M2) |
|:--|--:|--:|
| Temperatura | 1,67 °C | **0,91 °C** |
| Umidità relativa | 7,8 punti | **4,2 punti** |

I numeri aggiornati, stazione per stazione, sono nella dashboard.

## Come funziona, in breve

1. Ogni 30 minuti si raccolgono le osservazioni delle stazioni e si controlla la loro qualità.
2. Ogni ora si scarica l'ultima corsa del modello globale e si applica la correzione locale
   appresa dallo storico, per ogni stazione e per ogni ora fino a +48 h.
3. Le previsioni diventano una griglia sul Lazio (con la temperatura del mare sulle coste)
   e il sito statico si ripubblica da solo.

## Stack a costo zero

GitHub Actions (calcolo programmato), GitHub Pages (sito), Supabase (database PostgreSQL),
Python con LightGBM, Leaflet e Chart.js nel browser. Nessun server da mantenere.

Questo repository contiene solo il codice operativo e il sito. Modelli, dati, addestramento
e sperimentazione non sono pubblici.

## Dati e attribuzioni

- Previsioni e dati marini: [Open-Meteo](https://open-meteo.com/) (CC BY 4.0), con dati di
  [ECMWF](https://www.ecmwf.int/) e dei servizi meteorologici nazionali
- Osservazioni: stazioni [Netatmo](https://weathermap.netatmo.com/) e METAR aeroportuali
  (Iowa Environmental Mesonet)
- Radar sulla mappa: [RainViewer](https://www.rainviewer.com/)
- Mappa di base: © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors,
  © [CARTO](https://carto.com/attributions)
- Satellite: [EUMETSAT](https://www.eumetsat.int/)

## Diritti

© 2026 Filippo Turco. Tutti i diritti riservati.

Questo repository è pubblico solo per la lettura di questo README e per far funzionare la mappa
e la dashboard, liberamente consultabili. Nessuna licenza è concessa sul codice: non è consentito
copiarlo, modificarlo, distribuirlo o usarlo, in tutto o in parte, senza autorizzazione scritta.
