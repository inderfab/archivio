/**
 * Apps Script für bauchat.ch — ERWEITERUNG des bestehenden Scripts, nicht neu
 * aufsetzen. Ersetzt den kompletten Code deines aktuellen Scripts (das an
 * SHEET_ID gebunden ist und bisher "download" und "newsletter" verarbeitet).
 *
 * Was neu dazukommt: "email-signup" (E-Mail-Pflicht beim Download-Formular)
 * und "unsubscribe" (Abmelde-Link in der Update-Mail). "download" bleibt
 * unverändert bestehen. "newsletter" ist raus, da dieses Feature auf der
 * Website nicht mehr existiert — die alte "Newsletter"-Tabelle darf trotzdem
 * unangetastet stehen bleiben, es wird nur nichts mehr reingeschrieben.
 *
 * Einrichtung:
 * 1. Im selben Google Sheet eine neue Tabelle (Tab) anlegen, Name genau:
 *      Email-Updates
 *    Erste Zeile als Kopfzeile:
 *      E-Mail | Zeitstempel | Token | Status
 * 2. Im Apps-Script-Editor den kompletten bisherigen Code durch diesen hier
 *    ersetzen (SHEET_ID unten auf deinen bestehenden Wert prüfen/anpassen).
 * 3. "Bereitstellen" → "Bereitstellungen verwalten" → Stift-Symbol bei der
 *    bestehenden Web-App-Bereitstellung → "Version: Neue Version" → Bereitstellen.
 *    WICHTIG: über "Bereitstellungen verwalten" (nicht "Neue Bereitstellung"),
 *    damit die bestehende URL gleich bleibt — die Website muss dann nicht
 *    angepasst werden, sie zeigt schon auf diese URL (_TRACKING_URL).
 * 4. Beim ersten Aufruf nach dem Update fragt Google evtl. erneut nach
 *    Berechtigungen (Mail versenden) — einmal bestätigen.
 */

const SHEET_ID = '10CZANjdWeDwINhRh9u5zdkPyl6ORUbgtUFWTndSlLO0';

function doPost(e) {
  const data = JSON.parse(e.postData.contents);
  const ss = SpreadsheetApp.openById(SHEET_ID);

  if (data.type === 'download') {
    const sheet = ss.getSheetByName('Downloads');
    sheet.appendRow([
      new Date(),
      data.version || '1.0',
      data.country || '—'
    ]);
  }

  if (data.type === 'email-signup') {
    handleEmailSignup(ss, data.email);
  }

  if (data.type === 'unsubscribe') {
    handleUnsubscribe(ss, data.token);
  }

  return ContentService
    .createTextOutput(JSON.stringify({status: 'ok'}))
    .setMimeType(ContentService.MimeType.JSON);
}

function handleEmailSignup(ss, email) {
  if (!email) return;
  const sheet = ss.getSheetByName('Email-Updates');
  const rows = sheet.getDataRange().getValues();

  for (let i = 1; i < rows.length; i++) {
    if (rows[i][0] === email && rows[i][3] === 'aktiv') {
      // Bereits aktiv angemeldet -- nur Zeitstempel auffrischen, keine Dublette
      sheet.getRange(i + 1, 2).setValue(new Date());
      return;
    }
  }

  const token = Utilities.getUuid().replace(/-/g, '').substring(0, 16);
  sheet.appendRow([email, new Date(), token, 'aktiv']);
}

function handleUnsubscribe(ss, token) {
  if (!token) return;
  const sheet = ss.getSheetByName('Email-Updates');
  const rows = sheet.getDataRange().getValues();

  for (let i = 1; i < rows.length; i++) {
    if (rows[i][2] === token) {
      sheet.getRange(i + 1, 4).setValue('abgemeldet');
      const email = rows[i][0];
      try {
        MailApp.sendEmail(
          Session.getEffectiveUser().getEmail(),
          'Abmeldung: ' + email,
          'Abmeldung: ' + email + ', ' + new Date()
        );
      } catch (err) {
        // Mail-Versand darf die Abmeldung selbst nie verhindern
      }
      return;
    }
  }
}
