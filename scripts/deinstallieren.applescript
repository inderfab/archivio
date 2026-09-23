-- Archivio Deinstallieren.app
--
-- Grafische Oberfläche für scripts/deinstallieren.sh. Die Büros, die Archivio
-- einsetzen, sind Architekturbüros ohne IT-Abteilung; ein abzutippender
-- Terminal-Befehl ist für sie nicht benutzbar. Deshalb führt dieses Programm
-- mit gewöhnlichen macOS-Dialogen durch den Ablauf.
--
-- Das Shell-Skript liegt als Kopie IN diesem Programm (Contents/Resources) und
-- nicht im Server-Bundle: der Deinstallierer muss weiterlaufen, während er den
-- Server entfernt.

on run
	set skript to POSIX path of (path to resource "deinstallieren.sh")
	set skriptQ to quoted form of skript

	-- Bestandsaufnahme holen: Grösse der Datenbank, Anzahl Dokumente, Zustand
	-- der letzten Sicherung. Verändert nichts.
	try
		set bericht to do shell script "/bin/bash " & skriptQ & " --komplett --zusammenfassung"
	on error fehlertext
		display alert "Archivio Deinstallation" message ¬
			"Die Bestandsaufnahme ist fehlgeschlagen:" & return & return & fehlertext as critical
		return
	end try

	set ohneSicherung to (bericht contains "OHNE_SICHERUNG")
	set bericht to my ohneZeile(bericht, "OHNE_SICHERUNG")

	-- Erste Frage: was soll passieren?
	set frage to "Archivio von diesem Mac entfernen?" & return & return & ¬
		bericht & return & return & ¬
		"«Zum Arbeitsplatz machen» entfernt Server und Datenbank, behält aber den " & ¬
		"Helper — dafür, dass der Server auf einen anderen Mac gezogen ist." & return & return & ¬
		"«Komplett entfernen» nimmt auch den Helper mit."
	try
		set antwort to button returned of (display dialog frage ¬
			with title "Archivio Deinstallation" ¬
			buttons {"Abbrechen", "Zum Arbeitsplatz machen", "Komplett entfernen"} ¬
			default button "Abbrechen" with icon caution)
	on error number -128
		return
	end try
	if antwort is "Abbrechen" then return

	-- Zweite, ausdrückliche Rückfrage. Sie gilt für BEIDE Betriebsarten, denn die
	-- Datenbank wird in beiden gelöscht -- nur der Helper bleibt im Umzugsfall.
	if antwort is "Komplett entfernen" then
		set modus to "--komplett"
		set warnung to "Wollen Sie Archivio wirklich vollständig entfernen und alle Daten löschen?"
	else
		set modus to "--arbeitsplatz"
		set warnung to "Wollen Sie Server und Datenbank wirklich löschen? " & ¬
			"Nur der Helper bleibt auf diesem Mac zurück."
	end if
	set warnung to warnung & return & return & bericht
	if ohneSicherung then
		set warnung to warnung & return & return & ¬
			"ACHTUNG: Von dieser Datenbank gibt es keine bestätigte Sicherung."
	end if
	set warnung to warnung & return & return & ¬
		"Es wird alles in den Papierkorb gelegt und bleibt zurückholbar, " & ¬
		"bis dieser geleert wird."
	try
		set bestaetigung to button returned of (display dialog warnung ¬
			with title "Wirklich entfernen?" ¬
			buttons {"Abbrechen", "Entfernen"} default button "Abbrechen" with icon stop)
	on error number -128
		return
	end try
	if bestaetigung is not "Entfernen" then return

	-- Ausführen. Das Verschieben mehrerer GB kann dauern, deshalb ein grosszügiger
	-- Zeitrahmen statt des AppleScript-Standards.
	try
		with timeout of 900 seconds
			set ergebnis to do shell script "/bin/bash " & skriptQ & " " & modus & " --ohne-rueckfrage"
		end timeout
	on error fehlertext
		display alert "Archivio Deinstallation" message ¬
			"Beim Entfernen ist ein Fehler aufgetreten:" & return & return & fehlertext as critical
		return
	end try

	if modus is "--arbeitsplatz" then
		display alert "Archivio entfernt" message ¬
			"Server und Datenbank wurden entfernt. Dieser Mac ist jetzt Arbeitsplatz." & return & return & ¬
			"Der Helper bleibt installiert. Findet er den neuen Server nicht von selbst, " & ¬
			"im Helper-Menü einmal «Server suchen» wählen." & return & return & ¬
			"Alles Entfernte liegt im Papierkorb und lässt sich zurückholen, bis dieser geleert wird."
	else
		display alert "Archivio entfernt" message ¬
			"Archivio wurde von diesem Mac entfernt." & return & return & ¬
			"Alles liegt im Papierkorb und lässt sich zurückholen, bis dieser geleert wird."
		-- Zum Schluss sich selbst wegräumen, sonst bliebe der Deinstallierer als
		-- einziges Überbleibsel zurück. Verzögert und abgekoppelt, damit das
		-- laufende Programm vorher sauber beendet ist.
		try
			set michSelbst to POSIX path of (path to me)
			do shell script "nohup /bin/sh -c " & quoted form of ¬
				("sleep 3; /usr/bin/osascript -e 'tell application \"Finder\" to delete POSIX file \"" & ¬
					michSelbst & "\"'") & " >/dev/null 2>&1 &"
		end try
	end if
end run

-- Entfernt eine Markierungszeile aus der Ausgabe des Skripts.
on ohneZeile(text_, marke)
	set alt to AppleScript's text item delimiters
	set AppleScript's text item delimiters to return
	set zeilen to paragraphs of text_
	set behalten to {}
	repeat with z in zeilen
		if (z as text) is not marke then set end of behalten to (z as text)
	end repeat
	set AppleScript's text item delimiters to return
	set ergebnis to behalten as text
	set AppleScript's text item delimiters to alt
	return ergebnis
end ohneZeile
