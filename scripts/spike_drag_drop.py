#!/usr/bin/env python3
"""Spike: Drag & Drop einer Datei auf ein Menüleisten-Icon (rumps + PyObjC).

NUR zum Testen der technischen Machbarkeit -- kein Produktionscode, nicht Teil der
eigentlichen App. Ersetzt den von rumps automatisch erzeugten Status-Button durch eine
eigene NSView (Standardtechnik dafuer, da NSStatusItem-Buttons selbst keine Moeglichkeit
bieten, nachtraeglich Drag&Drop-Callbacks anzuhaengen -- nur eine komplett eigene View
kann NSDraggingDestination direkt implementieren). Die eigene View zeichnet einen
simplen Text statt eines echten Icons und muss den Linksklick manuell abfangen, um wie
gewohnt das Menü zu zeigen (das übernimmt sonst automatisch der ersetzte Button).

Test: `./.venv/bin/python3 scripts/spike_drag_drop.py`, dann eine beliebige Datei vom
Finder auf "[Archivio]" in der Menüleiste ziehen. Erwartet: Konsole zeigt den Dateipfad,
ein Klick (ohne Drag) zeigt ein simples Menü mit "Beenden".
"""
from __future__ import annotations

import objc
import rumps
from AppKit import (
    NSView, NSStatusBar, NSDragOperationCopy, NSDragOperationNone,
    NSPasteboardTypeFileURL, NSMenu, NSMenuItem, NSFont, NSColor,
)
from Foundation import NSURL, NSMakeRect


class DropView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(DropView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.registerForDraggedTypes_([NSPasteboardTypeFileURL])
        self._menu = None
        return self

    def setMenu_(self, menu):
        self._menu = menu

    def drawRect_(self, rect):
        # Bewusst simpel gehalten (nur ein grauer Block statt echtem Icon/Text) -- der
        # Spike soll nur Drag&Drop + Klick-Verhalten pruefen, nicht das Aussehen.
        from AppKit import NSRectFill
        NSColor.darkGrayColor().set()
        NSRectFill(rect)

    # ── Drag-and-Drop ────────────────────────────────────────────────────────
    def draggingEntered_(self, sender):
        print("[spike] draggingEntered")
        return NSDragOperationCopy

    def draggingUpdated_(self, sender):
        return NSDragOperationCopy

    def prepareForDragOperation_(self, sender):
        return True

    def performDragOperation_(self, sender):
        pboard = sender.draggingPasteboard()
        classes = [NSURL]
        items = pboard.readObjectsForClasses_options_(classes, None)
        paths = []
        if items:
            for url in items:
                p = url.path()
                if p:
                    paths.append(str(p))
        print(f"[spike] performDragOperation -- {len(paths)} Datei(en):")
        for p in paths:
            print(f"  -> {p}")
        return True

    def concludeDragOperation_(self, sender):
        print("[spike] concludeDragOperation (fertig)")

    # ── Klick zeigt Menü (normalerweise automatisch vom Button erledigt) ──────
    def mouseDown_(self, event):
        print("[spike] Klick erkannt -- zeige Menü")
        if self._menu is not None:
            self._menu.popUpMenuPositioningItem_atLocation_inView_(None, (0, 0), self)


class SpikeApp(rumps.App):
    def __init__(self):
        super().__init__("ArchivioDropSpike", quit_button=None)

    @rumps.clicked("Beenden")
    def quit(self, _):
        rumps.quit_application()


def _replace_with_drop_view():
    # app._nsapp/initializeStatusBar() existieren erst waehrend App.run(), NICHT vorher --
    # rumps.events.before_start feuert genau dazwischen: Status-Item existiert schon,
    # die blockierende Event-Loop laeuft noch nicht.
    app = getattr(rumps.App, '*app_instance')
    status_item = app._nsapp.nsstatusitem
    frame = NSMakeRect(0, 0, 90, 22)
    drop_view = DropView.alloc().initWithFrame_(frame)

    menu = NSMenu.alloc().init()
    quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Beenden", "terminate:", "")
    menu.addItem_(quit_item)
    drop_view.setMenu_(menu)

    status_item.setView_(drop_view)
    print("[spike] Bereit -- Datei auf '[Archivio]' in der Menüleiste ziehen, oder anklicken zum Beenden.")


rumps.events.before_start.register(_replace_with_drop_view)


if __name__ == "__main__":
    SpikeApp().run()
