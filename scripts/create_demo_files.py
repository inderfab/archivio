#!/usr/bin/env python3
"""Erstellt einen realistischen Demo-Archiv-Ordner auf dem Schreibtisch."""

import os
from pathlib import Path
from fpdf import FPDF

DESKTOP = Path.home() / "Desktop" / "Demo-Archiv"

# ── Projektstruktur ────────────────────────────────────────────────────────────

PROJEKTE = {
    "2024-012 Neubau MFH Zürich-Höngg": {
        "Dokumente": [
            ("Baugesuch_2024-012_Eingabe.pdf", """BAUGESUCH
Projekt: Neubau Mehrfamilienhaus Zürich-Höngg
Bauherrschaft: Familie Meier, Rosengartenstrasse 14, 8037 Zürich
Architekt: Indergand Architekten GmbH, Zürich
Grundstück: Parzelle 4821, Quartier Höngg
Nutzung: Wohnhaus mit 6 Wohneinheiten, Kellergeschoss mit Einstellhalle
Gebäudekategorie: GK3 gemäss SIA 261
Gebäudehöhe: 11.40 m
Anrechenbare Geschossfläche: 842 m²
Eingereicht am: 14. Februar 2024
Beilage: Situationsplan 1:500, Grundrisse, Schnitte, Fassaden, Brandschutznachweis"""),

            ("Brandschutznachweis_MFH_Höngg.pdf", """BRANDSCHUTZNACHWEIS
nach VKF-Brandschutznorm 2015

Projekt: Neubau MFH Zürich-Höngg, 2024-012
Gebäudekategorie: GK3
Nutzungseinheiten: 6 Wohnungen

Tragsicherheit im Brandfall:
Tragkonstruktion aus Stahlbeton, Feuerwiderstand R90 gemäss SIA 262.
Kellerdecke als Brandabschnittdecke ausgebildet, REI90.

Brandabschnitte:
Jede Wohneinheit bildet einen eigenen Brandabschnitt (max. 600 m² pro Abschnitt).
Treppenhausschacht in Massivbauweise, EI60 zu angrenzenden Räumen.

Flucht- und Rettungswege:
Zwei Treppenhäuser vorhanden. Fluchtweglänge < 35 m.
Notbeleuchtung gemäss SN EN 1838 vorgesehen.

Brandmeldung:
Automatische Brandmeldeanlage (BMA) im Keller und Treppenhaus.
Rauchwarnmelder in allen Wohneinheiten.

Sprinkleranlage: nicht erforderlich (GK3, < 1000 m² NGF)

Unterschrift Brandschutzexperte: _______________
Datum: 14. Februar 2024"""),

            ("Statikbericht_Tragwerk.pdf", """STATIKBERICHT TRAGWERK
Projekt: Neubau MFH Zürich-Höngg
Bericht-Nr.: S-2024-012-01

Grundlagen:
Norm SIA 260, 261, 262 (Ausgabe 2013)
Geotechnisches Gutachten vom 08.01.2024: Baugrund BK II, zul. Bodenpressung 250 kN/m²

Lasten:
Eigenlasten Stahlbetondecke 25 cm: 6.25 kN/m²
Ausbaulast: 1.5 kN/m²
Nutzlast Wohnen: 2.0 kN/m² (SIA 261, Kategorie A)
Schneelast Region Zürich (450 m ü.M.): 1.7 kN/m²
Windlast: 0.9 kN/m² (Windzone W1)

Fundation:
Bodenplatte d=30 cm, Beton C25/30, wasserundurchlässig WU-Beton.
Fundamentsohle auf -3.20 m unter Terrain.

Decken: Stahlbetonflachdecke d=25 cm, Beton C30/37, Bewehrung B500B.
Stützen: 30×30 cm Stahlbeton, EG bis DG.
Aussteifung: Stahlbetonkerne (Treppenhaus + Liftschacht)."""),

            ("Kostenermittlung_Stufe_Bauprojekt.pdf", """KOSTENERMITTLUNG – STUFE BAUPROJEKT
Projekt: Neubau MFH Zürich-Höngg, 2024-012
Datum: März 2024

BKP 1  Vorbereitungsarbeiten          CHF    45'000
BKP 2  Gebäude                        CHF 3'240'000
  211  Baugrube / Fundation            CHF   280'000
  214  Rohbau                          CHF 1'150'000
  221  Dach / Abdichtungen             CHF   195'000
  231  Fassade (Verputz / WDVS)        CHF   310'000
  241  Fenster / Aussentüren           CHF   285'000
  251  Innenausbau                     CHF   620'000
  261  Heizung / Lüftung / Sanitär     CHF   400'000
BKP 4  Umgebung                        CHF    95'000
BKP 5  Baunebenkosten                  CHF   130'000
BKP 9  Ausstattung                     CHF    48'000

TOTAL (exkl. MwSt.)                    CHF 3'558'000
MwSt. 8.1%                             CHF   288'200
TOTAL (inkl. MwSt.)                    CHF 3'846'200

Genauigkeit: ± 10% (Stufe Bauprojekt)"""),
        ],
        "Protokolle": [
            ("Bausitzung_01_2024-04-08.pdf", """BAUSITZUNGSPROTOKOLL NR. 01
Datum: 08. April 2024, 09:00 Uhr
Ort: Baustelle Rosengartenstrasse 14, Zürich-Höngg
Vorsitz: M. Indergand (Architekt)

Anwesend:
- M. Indergand, Indergand Architekten
- P. Meier, Bauherrschaft
- K. Bauer, Bauleitung
- T. Huber, Polier Maurer AG

Traktanden:
1. Aushub abgeschlossen, Sohle auf Kote -3.25 m. Abweichung +5 cm genehmigt.
2. Bodenplatte: Betonage vorgesehen KW 16. Armierungsplan Rev. 3 visiert.
3. Baugesuch rechtskräftig seit 02.04.2024. Baubewilligung liegt vor.
4. Baukran: Aufstellung 15. April geplant, Strassensperrung beantragt.
5. Terminprogramm: Rohbau OK per Ende August 2024 bestätigt.

Nächste Sitzung: 22. April 2024, 09:00 Uhr, Baustelle"""),

            ("Bausitzung_02_2024-04-22.pdf", """BAUSITZUNGSPROTOKOLL NR. 02
Datum: 22. April 2024, 09:00 Uhr
Ort: Baustelle Rosengartenstrasse 14, Zürich-Höngg

Anwesend:
- M. Indergand, Indergand Architekten
- K. Bauer, Bauleitung
- T. Huber, Polier Maurer AG
- S. Keller, Elektroplanung

Traktanden:
1. Bodenplatte betoniert KW 16, Ausschalung erfolgt. Qualität OK.
2. Kelleraussen­wände: Schalung begonnen, Betonage KW 18.
3. Elektro: Leerrohre Kellerdecke — Planrevision erforderlich.
   Zust. Keller bis 30.04. Nachtrag CHF 4'200 anerkannt.
4. Fensterofferten: Angebot Huber Fensterbau CHF 284'500 → Zuschlag erteilt.
5. Kran läuft seit 15.04., keine Beanstandungen.

Nächste Sitzung: 06. Mai 2024, 09:00 Uhr"""),

            ("Bausitzung_05_2024-06-03.pdf", """BAUSITZUNGSPROTOKOLL NR. 05
Datum: Montag, 03. Juni 2024, 09:00 - 11:30 Uhr
Ort: Baustelle Rosengartenstrasse 14, 8049 Zürich-Höngg
Protokoll: K. Bauer (Bauleitung)

Anwesend:
- M. Indergand, Indergand Architekten GmbH (Vorsitz)
- P. und S. Meier, Bauherrschaft
- K. Bauer, Bauer Bauleitung GmbH
- T. Huber, Polier, Maurer AG
- D. Zimmermann, Zimmermann Haustechnik AG (HLKS)
- F. Keller, Keller Elektro GmbH
- B. Senn, Senn Fensterbau AG (Gast)

Entschuldigt: Dr. A. Frei, Tragwerksplanung (schriftliche Stellungnahme liegt vor)

---

TRAKTANDUM 1 — BAUFORTSCHRITT ROHBAU

OG 2 wurde plangemäss in KW 21 betoniert. Druckfestigkeitsprüfung Beton C30/37:
28-Tage-Wert 34.2 N/mm² — Anforderung erfüllt. Ausschalung erfolgt ab 10.06.
OG 3 Schalung zu 60% gestellt, Armierung läuft. Betonage OG 3 vorgesehen KW 24
(17.06.), sofern Wetterprognose keine Hitzeperiode zeigt (Betonierungsverbot ab 32°C).
Dachkonstruktion (Attika): Schalungsplan Rev. 1 liegt vor, von Statik geprüft und
freigegeben. Betonage Attika geplant KW 27.

Beschluss: Terminprogramm angepasst, Rohbau OK neu per 12. Juli 2024 (war 28. Juni).
Verzögerung von 2 Wochen infolge Hitzeperiode Ende Mai akzeptiert. Keine
Konventionalstrafe, da höhere Gewalt (Wetterbericht dokumentiert).

---

TRAKTANDUM 2 — FASSADE UND WÄRMEDÄMMVERBUNDSYSTEM

Submission Fassade (BKP 231, WDVS 16 cm Steinwolle, Edelputz K15) abgeschlossen.
Eingang 4 Offerten:
  Weber AG, Zürich:          CHF 298'400 (Submissionsofferte)
  Fassaden Müller GmbH:      CHF 312'000
  Stuckateure Gasser:        CHF 287'600 (günstigstes Angebot)
  Alp Fassadentechnik:       CHF 334'500

Empfehlung Bauleitung: Zuschlag an Stuckateure Gasser CHF 287'600, Referenzen
geprüft, vergleichbare Objekte vorhanden, Kapazität für Beginn August bestätigt.
Bauherrschaft genehmigt Zuschlag. Werkvertrag wird diese Woche versandt.
Fassadenfarbe: Muster Nr. 7 (Weissgrau, NCS S 1002-G) durch Bauherrschaft
definitiv gewählt. Farbmuster auf Schalung OK, Architekt visiert.

---

TRAKTANDUM 3 — FENSTER UND AUSSENTÜREN

B. Senn (Senn Fensterbau) präsentiert Detailplanung Fenster.
Profil: Holz-Metall, Farbe aussen RAL 7016 Anthrazitgrau, innen weiss lackiert.
Verglasung: 3-fach, Ug = 0.5 W/m²K, g-Wert 0.5.

Offene Punkte:
- Brüstungshöhe Wohnung 1.1 Balkon: Statik fordert Brüstung 110 cm. Architekt
  prüft Glasbrüstung vs. Stahlrohrgeländer. Entscheid bis 17.06. an Senn.
- Haustüre: Einbruchschutz RC2 gemäss Versicherungsanforderung Bauherrschaft.
  Mehrkosten CHF 1'800 gegenüber Offerte — von Bauherrschaft genehmigt.
- Fensterbänke aussen: Chromstahl gebürstet, Bauherrschaft wünscht Überprüfung
  Naturstein. Architekt liefert Preisvergleich bis 10.06.

Liefertermin Fenster: 02. September 2024. Einbau ab KW 37 (09.09.2024).

---

TRAKTANDUM 4 — HAUSTECHNIK HLKS

D. Zimmermann rapportiert Stand Planung Heizung / Lüftung / Klimatisierung / Sanitär:

Heizung:
Wärmepumpe Luft/Wasser, Fabrikat Viessmann Vitocal 200-A, Leistung 12 kW bei A7/W35.
Aufstellung im Technikraum UG, Ausseneinheit Nordfassade.
LÄRMPROBLEM WÄRMEPUMPE: Schallleistungspegel gemessen: 63 dB(A). Grenzwert Lärm
gegenüber Nachbargrundstück (Abstand 4.8 m): 60 dB(A). Grenzwert um 3 dB(A) überschritten.
Lösung offen — Zimmermann prüft zwei Optionen: (A) Schallschutzkapsel nachrüsten
(Mehrkosten ca. CHF 2'400); (B) leiseres Gerät Viessmann Vitocal 252-A, 62 dB(A)
(Mehrkosten Gerät CHF 3'100). Entscheid bis 24.06. — Pendenz P03.

Fussbodenheizung: Verlegepläne alle Geschosse liegen vor, Architekt visiert.
Vorlauftemperatur: max. 35°C, Auslegung für Niedrigtemperatur korrekt.

Lüftung (KWL):
Zentrales Lüftungsgerät Lunos LG 160 mit WRG (η=83%) im UG.
Lüftungskanäle koordiniert mit Decken — alle Konflikte mit Rohbau bereinigt.
Schalldämpfer in Wohn- und Schlafzimmern gemäss SIA 181 (Schallschutz).
Fortluftführung: Dach, Position mit Dachdeckerplanung abgestimmt.

Sanitär:
Hausanschluss Wasser und Abwasser: Stadtwerke Zürich bestätigt Anschlusskosten
CHF 18'400. Baubewilligung Hausanschluss erteilt.
Warmwasserspeicher 300L, Zirkulationsleitung alle Wohnungen.
Regenwasser: Retention gemäss Anforderung Stadt Zürich, Zisterne 10 m³ UG.

---

TRAKTANDUM 5 — ELEKTRO

F. Keller rapportiert:
Erschliessung: EW Zürich bestätigt Hausanschluss 3×25A. Transformator Strassenraum
ausreichend, kein Netzausbau erforderlich.
Photovoltaik: Bauherrschaft wünscht PV-Anlage auf Dach. Keller erstellt Offerte
für 18 Module (monokristalin, 400 Wp, total 7.2 kWp). Einspeisevergütung ZEV
mit allen 6 Wohnungen. Mehrkosten PV ca. CHF 28'000 exkl. Förderung.
Förderantrag Pronovo: Keller stellt Antrag, Wartefrist ca. 6-8 Monate.
Ladestationen E-Auto: 3 Vorinstallationen im Keller für künftige Nachrüstung.
Beleuchtung Treppenhaus: LED, Bewegungsmelder, Notbeleuchtung 1h gemäss VKF.

---

TRAKTANDUM 6 — BRANDSCHUTZ (KONTROLLE VKF-AUFLAGEN)

Brandschutzbehörde hat Baustelle am 28.05. kontrolliert. Bericht liegt vor.
Keine Mängel festgestellt. Folgende Auflagen bis Rohbauabnahme zu erfüllen:
- Treppenhausverkleidung: EI30 Gipskarton im EG noch offen (Maurer AG bis 20.06.)
- Brandabschottungen Decken: Leerrohre Elektro durch Brandabschnittdecke UG/EG
  müssen abgedichtet werden. Keller Elektro koordiniert mit Abdichter bis 28.06.
- Feuerlöscher Baustelle: Kontrolle fällig — Bauer bestellt Nachfüllung.

Rohbauabnahme Brandschutz: geplant für 15. Juli 2024.

---

TRAKTANDUM 7 — TERMINPROGRAMM GESAMTPROJEKT (AKTUALISIERT)

Rohbau OK (Attika betoniert):                12. Juli 2024
Dachdeckerarbeiten / Abdichtung:             Juli - August 2024
Fenstereinbau:                               September 2024
Innenausbau Start (Gipser, Estrich):         Oktober 2024
Küchen- und Schreinerarbeiten:               November - Dezember 2024
Malerarbeiten:                               Januar 2025
Haustechnik Montage / Inbetriebnahme:        Januar - Februar 2025
Umgebungsarbeiten:                           März 2025
Bezug / Übergabe:                            April 2025

Bauherrschaft bestätigt Terminprogramm. Mietverträge werden auf April 2025 ausgelegt.

---

TRAKTANDUM 8 — DIVERSES

8.1 Nachbarschaftsreklamation: Nachbar Nr. 12 hat sich über Baustellenlärm am
    Samstag 25.05. beschwert (Betonage OG 2, 07:00 Uhr). Bauer weist darauf hin
    dass Samstagsarbeit bis 17:00 bewilligt ist, Beginn ab 07:00 zulässig.
    Schriftliche Rückmeldung an Nachbar durch Bauleitung erledigt.

8.2 Bauversicherung: Allrisk-Versicherung läuft bis 31.12.2024. Verlängerung bis
    April 2025 beantragen — Bauer erledigt bis 01.07.

8.3 Sauberkeitszustand Baustelle: Mängel festgestellt (Abfälle im Treppenhaus).
    Huber (Maurer AG) nimmt Rüge entgegen, Reinigung bis morgen.

8.4 Fotodokumentation: Bauer aktualisiert Fotodokumentation wöchentlich.
    Bauherrschaft hat Zugang zu Dropbox-Ordner bestätigt.

---

PENDENZEN AUS DIESER SITZUNG:

Nr.  Beschreibung                              Zuständig    Termin
P01  Entscheid Glasbrüstung vs. Geländer       Indergand    17.06.2024
P02  Preisvergleich Fensterbänke Naturstein     Indergand    10.06.2024
P03  Schallschutz Wärmepumpe: Lösung wählen    Zimmermann   24.06.2024
P04  Offerte PV-Anlage erstellen               Keller       17.06.2024
P05  Brandabschotung EG/UG Leerrohre           Keller       28.06.2024
P06  Treppenhausverkleidung EI30 EG            Maurer AG    20.06.2024
P07  Bauversicherung verlängern                Bauer        01.07.2024
P08  Werkvertrag Stuckateure Gasser versenden  Bauer        07.06.2024

---

NÄCHSTE BAUSITZUNG: Montag, 17. Juni 2024, 09:00 Uhr, Baustelle

Protokoll erstellt: 04.06.2024 / K. Bauer
Verteiler: alle Anwesenden, Tragwerksplanung"""),
        ],
        "Pläne": [
            ("Grundriss_EG_1-100.pdf", """GRUNDRISS ERDGESCHOSS
Massstab 1:100
Projekt: Neubau MFH Zürich-Höngg
Plannummer: A-101, Rev. 4, Datum 10.03.2024

Räume EG:
- Eingang / Treppenhalle: 18.5 m²
- Wohnung 1.1 (3.5 Zi): 89 m² (Wohnzimmer, Küche, 2 Schlafzimmer, Bad, Balkon 12 m²)
- Wohnung 1.2 (2.5 Zi): 64 m² (Wohnzimmer, Küche, Schlafzimmer, Bad, Balkon 8 m²)
- Einstellhalle Zufahrt: Rampe 15%, lichte Höhe 2.20 m
- Velokeller: 24 m², 12 Plätze
Konstruktionsraster: 5.40 m / 7.20 m
Wandstärke Aussenwand: 36 cm (inkl. 16 cm WDVS)"""),

            ("Schnitt_AA_1-50.pdf", """SCHNITT A-A
Massstab 1:50
Plannummer: A-301, Rev. 2

Geschosshöhen:
UG  -3.20 bis -0.10 m (lichte Höhe 2.55 m)
EG   0.00 bis  3.10 m (lichte Höhe 2.62 m)
OG1  3.10 bis  6.20 m (lichte Höhe 2.62 m)
OG2  6.20 bis  9.30 m (lichte Höhe 2.62 m)
DG   9.30 bis 11.40 m (Attika, lichte Höhe 2.55 m)

Dachaufbau (von oben): Extensive Begrünung 12 cm / Schutzlage / Abdichtung 2-lagig /
Wärmedämmung 24 cm (Gefälledämmung min. 8 cm) / Stahlbetondecke 25 cm / Unterputz.

UK Attika = +11.40 m (Höhe über Terrain massgebend für GK3-Einstufung)"""),
        ],
    },

    "2023-045 Umbau Scheunengebäude Elgg": {
        "Dokumente": [
            ("Baubewilligung_Kanton_ZH.pdf", """BAUBEWILLIGUNG
Baudirektion Kanton Zürich – Amt für Raumentwicklung
Referenz: ARE-2023-045-B

Gesuch: Umbau und Erweiterung Scheunengebäude zu Wohnnutzung
Standort: Dorfstrasse 8, 8353 Elgg, Parzelle 1204
Bauherrschaft: Stiftung Wohnraum Winterthur
Architekt: Indergand Architekten GmbH

Die Baubewilligung wird erteilt unter folgenden Auflagen:
1. Dachform und -neigung entsprechend Eingabe (40°, Satteldach) einzuhalten.
2. Fassade: Holzschalung vertikal, naturgrau gestrichen, gemäss Muster Nr. 3.
3. Fensterformate gemäss Fassadenplan A-201 Rev. 2, keine nachträglichen Änderungen.
4. Vor Baubeginn: Archäologische Voruntersuchung erforderlich (Kantonale Denkmalpflege).
5. Energienachweis: Minergie-Standard einzuhalten (THPE ≤ 75 %).

Rechtsmittelfrist: 30 Tage ab Publikation im Amtsblatt (15.09.2023)
Gültig bis: 15.09.2026"""),

            ("Energienachweis_Minergie.pdf", """ENERGIENACHWEIS MINERGIE
Projekt: Umbau Scheunengebäude Elgg, 2023-045
Software: SIA 380/1 Berechnungstool v4.2

Gebäudekategorie: Wohnen MFH (SIA 380/1, Kategorie I)
Energiebezugsfläche (EBF): 412 m²

Wärmeschutz Hülle:
Aussenwand Holzfassade: U = 0.17 W/m²K (Dämmung 18 cm Steinwolle)
Dach: U = 0.14 W/m²K (Dämmung 24 cm zwischen/über Sparren)
Bodenplatte: U = 0.22 W/m²K (Dämmung 12 cm XPS)
Fenster (3-fach Verglasung): U = 0.7 W/m²K, g = 0.5

Heizwärmebedarf Qh: 58.4 MJ/m² (Grenzwert Minergie: 75 MJ/m²) ✓
Heizung: Wärmepumpe Luft/Wasser, COP 3.8
Warmwasser: Solaranlage 12 m² Kollektorfläche, Deckungsgrad 55%
Lüftung: Kontrollierte Wohnungslüftung mit WRG, η = 82%

Minergie-Anforderung erfüllt. Gesuch eingereicht 12.10.2023."""),
        ],
        "Protokolle": [
            ("Planungssitzung_03_2023-11-14.pdf", """PLANUNGSSITZUNGSPROTOKOLL NR. 03
Datum: 14. November 2023
Projekt: Umbau Scheunengebäude Elgg, 2023-045

Teilnehmer: Architektur, Bauherrschaft, Haustechnikplaner, Statiker

1. Bestandsaufnahme Holztragwerk: Hauptbalken Eiche, Querschnitt 24×28 cm, Zustand gut.
   Nachweis gemäss SIA 265 durch Statiker bis 30.11. erstellt.
2. Bodenaufbau EG: Bestehender Betonboden aufgebrochen, Perimeterdämmung 16 cm XPS,
   Bodenheizung 5 cm Estrich. Kältbrückenfreie Ausführung gemäss Energienachweis.
3. Küche / Bäder: Planung freigegeben. Sanitärschema Rev. 1 visiert.
4. Denkmalpflege: Rückmeldung erwartet bis 21.11. Historische Tore bleiben erhalten,
   werden saniert und als Zugang EG genutzt.
5. Ausschreibung Holzbau: Versand 20.11., Submission 10.12.2023."""),
        ],
    },

    "2022-088 Einfamilienhaus Küsnacht": {
        "Dokumente": [
            ("Abschlussrechnung_2022-088.pdf", """ABSCHLUSSRECHNUNG
Projekt: Neubau Einfamilienhaus Küsnacht, 2022-088
Fertigstellung: Oktober 2022

BKP  Bezeichnung                           Budget          Abrechnung    Differenz
1    Vorbereitungsarbeiten               48'000            46'200        +1'800
2    Gebäude                          2'420'000         2'467'800       -47'800
  211 Fundation / Baugrube              210'000           224'500       -14'500  (Mehraufwand Felskuppe)
  214 Rohbau Mauerwerk                  680'000           672'000        +8'000
  221 Dach Ziegeleindeckung             148'000           151'200        -3'200
  231 Fassade Kalkputz                  195'000           198'400        -3'400
  241 Fenster Holz/Metall               320'000           315'600        +4'400
  251 Innenausbau / Schreiner           467'000           506'100       -39'100  (Mehrausbau Nachtrag)
  261 HLKS                              400'000           400'000             0
4    Umgebung / Gartenanlage             92'000            88'500        +3'500
5    Baunebenkosten                     115'000           118'200        -3'200
9    Ausstattung / Küche                 85'000            92'400        -7'400

TOTAL exkl. MwSt.                     2'760'000         2'813'100       -53'100
MwSt. 7.7%                              212'520           216'609
TOTAL inkl. MwSt.                     2'972'520         3'029'709       -57'189

Schlussguthaben Bauherrschaft nach Rückbehalt: CHF 0 (vollständig abgerechnet)"""),

            ("Werkvertrag_Maurer_AG.pdf", """WERKVERTRAG
zwischen
Bauherrschaft: Familie Gross, Seestrasse 88, 8700 Küsnacht
und
Unternehmung: Maurer AG, Bauleistungen, 8600 Dübendorf

Objekt: Neubau EFH Küsnacht, Parzelle 2201, BKP 214 Rohbau

Leistungsumfang:
- Ausführung sämtlicher Mauerwerks- und Betonarbeiten gemäss Plänen Rev. 5
- Lieferung und Einbau Schalsteine KS-Planblock 20 cm für Aussenwände
- Stahlbeton Decken und Treppen gemäss Bewehrungsplänen
- Keller WU-Beton d=30 cm, Aussenabdichtung Bitumenbahn 2-lagig

Vertragssumme: CHF 672'000 exkl. MwSt.
Ausführungsfrist: 01.02.2022 bis 31.07.2022
Zahlungsbedingungen: 30 Tage netto nach Rechnungsstellung
Garantiefrist: 5 Jahre gemäss SIA 118

Unterschriften: _______________ / _______________
Datum: 15. Januar 2022"""),
        ],
        "Protokolle": [
            ("Abnahmeprotokoll_Oktober_2022.pdf", """ABNAHMEPROTOKOLL
Datum: 14. Oktober 2022, 10:00 Uhr
Projekt: Neubau EFH Küsnacht, 2022-088

Anwesend: Bauherrschaft (Familie Gross), Architekt Indergand, Bauleiter Bauer

Das Gebäude wird wie folgt abgenommen:

MÄNGELLISTE:
M01 Küche: Schublade 3. Reihe links klemmt → Schreiner bis 28.10.
M02 Bad OG: Silikon Dusche unvollständig → Sanitär bis 21.10. ✓
M03 Aussenbereich: Pflasterung Zufahrt Absenkung 3 cm → Gärtner bis 04.11.
M04 Estrich: Bodenluke Scharniere lose → Schreiner bis 28.10.
M05 Heizung: Raumthermostat OG reagiert verzögert → Heizungsbauer Kontrolle bis 21.10.

Alle übrigen Leistungen werden mängelfrei abgenommen.
Übergabe Schlüssel: 2× Hausschlüssel, 3× Briefkastenschlüssel, 1× Garagentor-Fernbedienung

Garantiebeginn: 14. Oktober 2022"""),
        ],
    },
}


_LATIN1_REPLACEMENTS = str.maketrans({
    "η": "n", "α": "a", "β": "b", "γ": "g", "λ": "l", "μ": "u",
    "≤": "<=", "≥": ">=", "≠": "!=", "≈": "~",
    "✓": "OK", "✗": "X", "→": "->", "←": "<-", "↑": "^", "↓": "v",
    "–": "-",   # en dash
    "—": "-",   # em dash
    "‘": "'",   # left single quote
    "’": "'",   # right single quote
    "“": '"',   # left double quote
    "”": '"',   # right double quote
})


def _sanitize(text: str) -> str:
    """Ersetze Zeichen ausserhalb latin-1 durch ASCII-Alternativen."""
    text = text.translate(_LATIN1_REPLACEMENTS)
    return text.encode("latin-1", errors="replace").decode("latin-1")


def make_pdf(path: Path, title: str, content: str):
    pdf = FPDF()
    pdf.set_margins(25, 25, 25)
    pdf.set_auto_page_break(auto=True, margin=25)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 12)
    pdf.multi_cell(0, 7, _sanitize(title.replace(".pdf", "").replace("_", " ")),
                   align="L", new_x="LEFT", new_y="NEXT")
    pdf.ln(4)

    pdf.set_font("Helvetica", "", 9)
    for line in content.strip().splitlines():
        safe = _sanitize(line[:120])
        try:
            pdf.multi_cell(0, 5, safe, align="L", new_x="LEFT", new_y="NEXT")
        except Exception:
            pass
    pdf.ln(2)

    pdf.output(str(path))


def main():
    created = 0
    for projekt, ordner in PROJEKTE.items():
        for unterordner, dateien in ordner.items():
            folder = DESKTOP / projekt / unterordner
            folder.mkdir(parents=True, exist_ok=True)
            for filename, content in dateien:
                filepath = folder / filename
                make_pdf(filepath, filename, content)
                print(f"  ✓ {projekt}/{unterordner}/{filename}")
                created += 1

    print(f"\n{created} Dateien erstellt in: {DESKTOP}")


if __name__ == "__main__":
    main()
