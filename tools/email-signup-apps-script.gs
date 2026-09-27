/**
 * Apps Script für das Download-E-Mail-Formular auf bauchat.ch.
 *
 * Einrichtung (einmalig):
 * 1. Im Google Sheet: Erweiterungen → Apps Script.
 * 2. Diesen kompletten Code einfügen (ersetzt den Beispielcode).
 * 3. In Zeile 1 des Sheets die Kopfzeile anlegen (falls noch nicht vorhanden):
 *      E-Mail | Zeitstempel | Token | Status
 * 4. Oben rechts "Bereitstellen" → "Neue Bereitstellung" → Typ "Web-App".
 *    - Ausführen als: Ich (eigenes Konto)
 *    - Zugriff: Jeder ("Anonymous")
 * 5. Die dabei angezeigte Web-App-URL kopieren und in docs/index.html bei
 *    EMAIL_ENDPOINT_URL einsetzen (siehe Kommentar dort).
 * 6. Beim allerersten Testaufruf fragt Google nach Berechtigungen
 *    (Zugriff aufs Sheet, Mail versenden) — das ist normal, einmal bestätigen.
 *
 * Tabellenspalten: E-Mail | Zeitstempel | Token | Status
 * Status ist "aktiv" oder "abgemeldet". Zeilen werden nie gelöscht.
 */

function doPost(e) {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  var body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return ContentService.createTextOutput('invalid payload');
  }

  if (body.action === 'unsubscribe') {
    return handleUnsubscribe(sheet, body.token);
  }
  return handleSignup(sheet, body.email);
}

function handleSignup(sheet, email) {
  if (!email) return ContentService.createTextOutput('missing email');

  var data = sheet.getDataRange().getValues();
  for (var i = 1; i < data.length; i++) {
    if (data[i][0] === email && data[i][3] === 'aktiv') {
      // Bereits aktiv angemeldet -- nur Zeitstempel auffrischen, keine Dublette anlegen
      sheet.getRange(i + 1, 2).setValue(new Date());
      return ContentService.createTextOutput('updated');
    }
  }

  var token = Utilities.getUuid().replace(/-/g, '').substring(0, 16);
  sheet.appendRow([email, new Date(), token, 'aktiv']);
  return ContentService.createTextOutput('added');
}

function handleUnsubscribe(sheet, token) {
  if (!token) return ContentService.createTextOutput('missing token');

  var data = sheet.getDataRange().getValues();
  for (var i = 1; i < data.length; i++) {
    if (data[i][2] === token) {
      sheet.getRange(i + 1, 4).setValue('abgemeldet');
      var email = data[i][0];
      try {
        MailApp.sendEmail(
          Session.getEffectiveUser().getEmail(),
          'Abmeldung: ' + email,
          'Abmeldung: ' + email + ', ' + new Date()
        );
      } catch (err) {
        // Mail-Versand darf die Abmeldung selbst nie verhindern
      }
      return ContentService.createTextOutput('unsubscribed');
    }
  }
  return ContentService.createTextOutput('not found');
}
