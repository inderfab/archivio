/**
 * Apps Script für bauchat.ch — verarbeitet Download-Zählung, E-Mail-Anmeldung
 * beim Download und Abmeldung, alles im selben Sheet (SHEET_ID unten).
 *
 * Dieses Script läuft im Apps-Script-Projekt, das über "Erweiterungen →
 * Apps Script" direkt am Google Sheet hängt (Bereitstellungs-ID
 * AKfycby3IWoq8oU-gZPvwclyks_AubvfkMmxrJvQ1P2P1zplYEBfxbVr8oo31AUc1JTnvhlnQg,
 * exakt diese URL steht in docs/index.html als _TRACKING_URL und in
 * docs/abmelden.html als TRACKING_URL — beide synchron halten, falls sich
 * die Bereitstellung mal ändert).
 *
 * Tabellen im Sheet:
 * - "Downloads"      -- bestehend, unverändert (Zeitstempel, Version, Land)
 * - "Newsletter"      -- alt/unbenutzt, darf unangetastet stehen bleiben
 * - "Email-Updates"   -- neu, für die E-Mail-Pflicht beim Download.
 *   Kopfzeile: E-Mail | Zeitstempel | Token | Status
 *
 * Nach jeder Code-Änderung hier: "Bereitstellen" → "Bereitstellungen
 * verwalten" → Stift-Symbol bei der Web-App-Zeile → "Version: Neue Version"
 * → Bereitstellen (NICHT "Neue Bereitstellung", sonst ändert sich die URL
 * und die Website muss erneut angepasst werden).
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
