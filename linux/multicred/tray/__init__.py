"""Tray icon: a StatusNotifierItem with a com.canonical.dbusmenu menu, over Gio D-Bus.

GTK3/AppIndicator cannot live in a GTK4 process, so the two protocols are served
directly; the panel (GNOME AppIndicator extension, COSMIC, KDE) draws the menu.
"""
