# Ortsmeisterschaften Tool

Streamlit-App zur Verwaltung von Anmeldungen, Startlisten, Zeitnehmung, Ergebnissen, Urkunden und Auswertungen.

## Lokal starten

```powershell
py -3.14 -m pip install -r requirements.txt
py -3.14 -m streamlit run app.py
```

## Deployment

GitHub Pages kann diese App nicht direkt hosten, weil Streamlit einen Python-Server braucht.

Empfohlen:

1. Repository auf GitHub erstellen.
2. Projektdateien hochladen.
3. Auf Streamlit Community Cloud `app.py` als Startdatei auswählen.

Die lokale `database.db` ist absichtlich per `.gitignore` ausgeschlossen.
